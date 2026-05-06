"""
Kube-AutoFix -- Autonomous Agent Loop.

Orchestrates the deploy -> monitor -> debug -> LLM -> fix cycle
with Rich terminal output for beautiful, demo-ready visuals.

Safety guardrails enforced:
  - MAX_ITERATIONS hard limit (default 5, cap 10)
  - Namespace isolation (autofix-agent-env only)
  - Dry-run mode (print corrected YAML without applying)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from config import KUBE_NAMESPACE, Settings
from core.models import DebugBundle, LLMDiagnosis
from k8s.debugger import KubeDebugger
from k8s.deployer import KubeDeployer
from k8s.monitor import KubeMonitor, MonitorResult
from llm.engine import LLMEngine, LLMEngineError


# ── Iteration record ─────────────────────────────────────────────────


@dataclass
class IterationRecord:
    """Tracks what happened in a single iteration for the summary table."""

    iteration: int
    outcome: str  # "deployed", "failed", "fixed", "dry_run"
    failure_reason: str = ""
    llm_root_cause: str = ""
    llm_confidence: float = 0.0
    duration_seconds: float = 0.0


@dataclass
class LoopResult:
    """Final outcome of the entire agent loop."""

    success: bool
    total_iterations: int
    records: list[IterationRecord] = field(default_factory=list)
    final_yaml: str = ""
    final_diagnosis: LLMDiagnosis | None = None


# ── Agent Loop ────────────────────────────────────────────────────────


class AgentLoop:
    """
    The autonomous debugging loop that ties together all modules.

    Lifecycle:
      1. Deploy the manifest
      2. Monitor pod status
      3. On failure: collect debug info, consult LLM, get fix
      4. Apply corrected manifest and repeat
      5. Exit on success or after MAX_ITERATIONS
    """

    def __init__(
        self,
        deployer: KubeDeployer,
        monitor: KubeMonitor,
        debugger: KubeDebugger,
        llm_engine: LLMEngine,
        settings: Settings,
        console: Console,
    ) -> None:
        self._deployer = deployer
        self._monitor = monitor
        self._debugger = debugger
        self._llm = llm_engine
        self._settings = settings
        self._console = console

    # ── Main entry point ──────────────────────────────────────────

    def run(self, initial_yaml: str) -> LoopResult:
        """
        Execute the autonomous fix loop.

        Args:
            initial_yaml: The initial (potentially broken) YAML manifest.

        Returns:
            LoopResult with success status, iteration records, and final YAML.
        """
        console = self._console
        settings = self._settings
        max_iter = settings.max_iterations
        current_yaml = initial_yaml
        records: list[IterationRecord] = []

        console.print()
        console.print(
            Rule(
                f"[bold cyan]Starting Autonomous Fix Loop[/] "
                f"[dim](max {max_iter} iterations)[/dim]",
                style="cyan",
            )
        )
        console.print()

        for iteration in range(1, max_iter + 1):
            iter_start = time.time()
            record = IterationRecord(iteration=iteration, outcome="unknown")

            # ── Iteration header ──────────────────────────────────
            self._print_iteration_header(iteration, max_iter)

            # ── Step 1: Deploy ────────────────────────────────────
            if settings.dry_run and iteration > 1:
                # In dry-run mode, don't apply after iteration 1
                console.print(
                    "[yellow]  DRY RUN:[/] Skipping deployment. "
                    "Corrected YAML displayed above."
                )
                record.outcome = "dry_run"
                record.duration_seconds = time.time() - iter_start
                records.append(record)
                break

            deploy_result = self._step_deploy(current_yaml)
            if not deploy_result.success:
                console.print(
                    f"[bold red]  Deploy failed:[/] {deploy_result.message}"
                )
                record.outcome = "deploy_error"
                record.failure_reason = deploy_result.message
                record.duration_seconds = time.time() - iter_start
                records.append(record)
                continue

            # ── Step 2: Monitor ───────────────────────────────────
            label_selector = KubeMonitor.label_selector_from_manifest(
                current_yaml
            )
            if not label_selector:
                console.print(
                    "[bold red]  Could not extract label selector "
                    "from manifest.[/]"
                )
                record.outcome = "selector_error"
                record.duration_seconds = time.time() - iter_start
                records.append(record)
                continue

            monitor_result = self._step_monitor(label_selector)

            # ── Step 3: Evaluate ──────────────────────────────────
            if monitor_result.is_success:
                record.outcome = "success"
                record.duration_seconds = time.time() - iter_start
                records.append(record)
                self._print_success_panel(iteration, records)
                return LoopResult(
                    success=True,
                    total_iterations=iteration,
                    records=records,
                    final_yaml=current_yaml,
                )

            # ── Failure path ──────────────────────────────────────
            record.failure_reason = monitor_result.message
            self._print_failure_details(monitor_result)

            # ── Step 4: Debug ─────────────────────────────────────
            failing_pods = monitor_result.failing_pods or monitor_result.pod_statuses
            debug_bundle = self._step_debug(failing_pods)

            # ── Step 5: LLM Reasoning ─────────────────────────────
            try:
                diagnosis = self._step_llm(
                    current_yaml, debug_bundle, iteration, max_iter
                )
            except (LLMEngineError, Exception) as e:
                console.print(
                    f"[bold red]  LLM Error:[/] {escape(str(e))}"
                )
                record.outcome = "llm_error"
                record.duration_seconds = time.time() - iter_start
                records.append(record)
                continue

            record.llm_root_cause = diagnosis.root_cause
            record.llm_confidence = diagnosis.confidence_score
            self._print_diagnosis_panel(diagnosis)

            # ── Step 6: Show corrected YAML ───────────────────────
            self._print_corrected_yaml(diagnosis.corrected_yaml)

            if settings.dry_run:
                console.print(
                    Panel(
                        "[yellow bold]DRY RUN MODE[/]\n\n"
                        "The corrected YAML above was [bold]NOT[/] applied.\n"
                        "Re-run without [cyan]--dry-run[/] to apply.",
                        title="Dry Run",
                        border_style="yellow",
                        padding=(1, 2),
                    )
                )
                record.outcome = "dry_run"
                record.duration_seconds = time.time() - iter_start
                records.append(record)
                return LoopResult(
                    success=False,
                    total_iterations=iteration,
                    records=records,
                    final_yaml=diagnosis.corrected_yaml,
                    final_diagnosis=diagnosis,
                )

            # ── Step 7: Update manifest for next iteration ────────
            current_yaml = diagnosis.corrected_yaml
            record.outcome = "retrying"
            record.duration_seconds = time.time() - iter_start
            records.append(record)

            if iteration < max_iter:
                console.print(
                    f"\n[dim]  Applying corrected manifest in "
                    f"next iteration...[/]\n"
                )

        # ── Exhausted all iterations ──────────────────────────────
        self._print_exhausted_panel(max_iter, records)
        return LoopResult(
            success=False,
            total_iterations=max_iter,
            records=records,
            final_yaml=current_yaml,
        )

    # ── Step implementations ──────────────────────────────────────

    def _step_deploy(self, yaml_str: str):
        """Deploy the manifest with a Rich spinner."""
        console = self._console
        with console.status(
            "[bold cyan]  Deploying manifest to cluster...[/]",
            spinner="dots",
        ):
            result = self._deployer.apply_manifest(yaml_str)

        if result.success:
            for res in result.resources_created:
                console.print(f"  [green]\u2714[/] {res}")
        else:
            for fail in result.resources_failed:
                console.print(f"  [red]\u2718[/] {fail}")

        return result

    def _step_monitor(self, label_selector: str) -> MonitorResult:
        """Monitor pod status — logs flow naturally via RichHandler."""
        console = self._console
        console.print(
            f"\n  [bold cyan]\u23f3 Monitoring pods[/] "
            f"[dim](timeout={self._settings.poll_timeout_seconds}s, "
            f"interval={self._settings.poll_interval_seconds}s)[/]"
        )
        return self._monitor.poll_until_ready(
            label_selector=label_selector,
            timeout=self._settings.poll_timeout_seconds,
            interval=self._settings.poll_interval_seconds,
        )

    def _step_debug(self, failing_pods) -> DebugBundle:
        """Collect debug information with a spinner."""
        console = self._console
        with console.status(
            "[bold cyan]  Collecting debug information...[/]",
            spinner="dots",
        ):
            bundle = self._debugger.collect_debug_bundle(failing_pods)

        console.print(
            f"  [green]\u2714[/] Debug bundle collected "
            f"({len(bundle.pod_debug_infos)} pod(s))"
        )
        return bundle

    def _step_llm(
        self,
        current_yaml: str,
        debug_bundle: DebugBundle,
        iteration: int,
        max_iterations: int,
    ) -> LLMDiagnosis:
        """Consult GPT-4o with a spinner."""
        console = self._console
        with console.status(
            f"[bold magenta]  \U0001f9e0 Consulting {self._settings.openai_model}...[/]",
            spinner="dots",
        ):
            diagnosis = self._llm.diagnose(
                current_yaml=current_yaml,
                debug_bundle=debug_bundle,
                iteration=iteration,
                max_iterations=max_iterations,
            )
        return diagnosis

    # ── Rich output helpers ───────────────────────────────────────

    def _print_iteration_header(self, iteration: int, max_iter: int) -> None:
        style = "bold white" if iteration == 1 else "bold yellow"
        label = "Initial Deployment" if iteration == 1 else "Fix Attempt"
        self._console.print(
            Rule(
                f"[{style}]Iteration {iteration}/{max_iter} \u2014 {label}[/]",
                style="blue" if iteration == 1 else "yellow",
            )
        )
        self._console.print()

    def _print_failure_details(self, result: MonitorResult) -> None:
        console = self._console
        lines: list[str] = []

        for pod in result.pod_statuses:
            for cs in pod.container_statuses:
                status_color = "red" if cs.reason else "yellow"
                lines.append(
                    f"[bold]{pod.name}[/] / {cs.name}\n"
                    f"  State:  [{status_color}]{cs.state}[/]\n"
                    f"  Reason: [{status_color}]{cs.reason or 'N/A'}[/]\n"
                    f"  Image:  [dim]{cs.image}[/]"
                )
                if cs.message:
                    lines.append(
                        f"  Detail: [dim]{escape(cs.message[:200])}[/]"
                    )

        content = "\n\n".join(lines) if lines else result.message
        console.print()
        console.print(
            Panel(
                content,
                title="\u274c Failure Detected",
                title_align="left",
                border_style="red",
                padding=(1, 2),
            )
        )

    def _print_diagnosis_panel(self, diagnosis: LLMDiagnosis) -> None:
        console = self._console

        # Confidence color
        conf = diagnosis.confidence_score
        if conf >= 0.8:
            conf_color = "green"
        elif conf >= 0.5:
            conf_color = "yellow"
        else:
            conf_color = "red"

        # Build content
        sections: list[str] = []
        sections.append(
            f"[bold]Root Cause:[/] {escape(diagnosis.root_cause)}"
        )
        sections.append(
            f"[bold]Confidence:[/] [{conf_color}]{conf:.0%}[/]"
        )
        sections.append("")
        sections.append("[bold]Reasoning:[/]")
        # Wrap reasoning text for readability
        for line in diagnosis.reasoning.split("\n"):
            sections.append(f"  [dim]{escape(line)}[/]")

        if diagnosis.changes_made:
            sections.append("")
            sections.append("[bold]Changes Made:[/]")
            for change in diagnosis.changes_made:
                sections.append(f"  [cyan]\u2022[/] {escape(change)}")

        console.print()
        console.print(
            Panel(
                "\n".join(sections),
                title="\U0001f9e0 LLM Diagnosis",
                title_align="left",
                border_style="magenta",
                padding=(1, 2),
            )
        )

    def _print_corrected_yaml(self, yaml_str: str) -> None:
        console = self._console
        console.print()
        console.print("  [bold cyan]Corrected Manifest:[/]")
        syntax = Syntax(
            yaml_str.strip(),
            "yaml",
            theme="monokai",
            line_numbers=True,
            padding=1,
        )
        console.print(syntax)

    def _print_success_panel(
        self, iteration: int, records: list[IterationRecord]
    ) -> None:
        console = self._console

        lines: list[str] = []
        if iteration == 1:
            lines.append(
                "[bold green]The manifest deployed successfully "
                "on the first attempt![/]"
            )
        else:
            lines.append(
                f"[bold green]Deployment fixed in "
                f"{iteration} iteration(s)![/]"
            )

        # Show the fix that worked (from the previous iteration)
        fix_records = [r for r in records if r.llm_root_cause]
        if fix_records:
            last_fix = fix_records[-1]
            lines.append("")
            lines.append(
                f"[bold]Root Cause:[/] {escape(last_fix.llm_root_cause)}"
            )
            lines.append(
                f"[bold]Fix Confidence:[/] {last_fix.llm_confidence:.0%}"
            )

        lines.append("")
        lines.append(
            f"[dim]Namespace:[/] [cyan]{KUBE_NAMESPACE}[/]"
        )
        lines.append(
            "[dim]Resources left running for inspection.[/]"
        )

        console.print()
        console.print(
            Panel(
                "\n".join(lines),
                title="\u2705 SUCCESS",
                title_align="left",
                border_style="green",
                padding=(1, 2),
            )
        )
        console.print()

        # Print iteration summary table
        self._print_summary_table(records)

    def _print_exhausted_panel(
        self, max_iter: int, records: list[IterationRecord]
    ) -> None:
        console = self._console
        console.print()
        console.print(
            Panel(
                f"[bold red]Exhausted all {max_iter} iterations "
                f"without a successful fix.[/]\n\n"
                f"The agent was unable to autonomously resolve the "
                f"deployment failure.\n"
                f"Manual intervention is required.\n\n"
                f"[dim]Namespace:[/] [cyan]{KUBE_NAMESPACE}[/]\n"
                f"[dim]Check resources with:[/] "
                f"[cyan]kubectl get all -n {KUBE_NAMESPACE}[/]",
                title="\u274c FAILED \u2014 Max Iterations Reached",
                title_align="left",
                border_style="red",
                padding=(1, 2),
            )
        )
        console.print()
        self._print_summary_table(records)

    def _print_summary_table(self, records: list[IterationRecord]) -> None:
        """Print a summary table of all iterations."""
        console = self._console
        table = Table(
            title="Iteration Summary",
            title_style="bold",
            show_lines=True,
            border_style="dim",
        )
        table.add_column("#", style="bold", width=3, justify="center")
        table.add_column("Outcome", width=14)
        table.add_column("Failure Reason", max_width=35, overflow="fold")
        table.add_column("LLM Root Cause", max_width=35, overflow="fold")
        table.add_column("Confidence", width=10, justify="center")
        table.add_column("Duration", width=8, justify="right")

        for r in records:
            # Color-code outcome
            outcome_colors = {
                "success": "green",
                "retrying": "yellow",
                "dry_run": "cyan",
                "deploy_error": "red",
                "llm_error": "red",
                "selector_error": "red",
                "unknown": "dim",
            }
            color = outcome_colors.get(r.outcome, "white")
            outcome_str = f"[{color}]{r.outcome}[/]"

            conf_str = f"{r.llm_confidence:.0%}" if r.llm_confidence else "-"
            dur_str = f"{r.duration_seconds:.1f}s"

            table.add_row(
                str(r.iteration),
                outcome_str,
                escape(r.failure_reason[:80]) if r.failure_reason else "-",
                escape(r.llm_root_cause[:80]) if r.llm_root_cause else "-",
                conf_str,
                dur_str,
            )

        console.print(table)
        console.print()
