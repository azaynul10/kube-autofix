"""
Kube-AutoFix -- CLI Entrypoint.

An autonomous Kubernetes debugging agent that deploys, diagnoses,
and auto-corrects application manifests using GPT-4o reasoning.

Usage:
    python main.py --manifest manifests/sample_broken.yaml
    python main.py --manifest manifests/sample_broken.yaml --dry-run
    python main.py --manifest manifests/sample_broken.yaml --max-iterations 3
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.text import Text

from config import KUBE_NAMESPACE, Settings
from core.agent_loop import AgentLoop
from k8s.debugger import KubeDebugger
from k8s.deployer import KubeDeployer
from k8s.monitor import KubeMonitor
from llm.engine import LLMEngine


# ── Banner ────────────────────────────────────────────────────────────

_BANNER = r"""
[bold cyan]
  _  __      _              _         _        _____ _
 | |/ /     | |            / \  _   _| |_ ___ |  ___(_)_  __
 | ' / _   _| |__   ___   / _ \| | | | __/ _ \| |_  | \ \/ /
 | . \| | | | '_ \ / _ \ / ___ \ |_| | || (_) |  _| | |>  <
 |_|\_\_,_,_|_.__/ \___//_/   \_\__,_|\__\___/|_|   |_/_/\_\
[/]
[dim]  Autonomous Kubernetes Debugging Agent — Powered by GPT-4o[/dim]
"""


def _setup_logging(console: Console, level: str) -> None:
    """Configure Rich-based logging for all modules."""
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console,
                rich_tracebacks=True,
                tracebacks_show_locals=False,
                show_path=False,
                markup=True,
            )
        ],
        force=True,
    )


def _print_config_panel(console: Console, settings: Settings, manifest_path: str) -> None:
    """Print a startup configuration panel."""
    lines = [
        f"[bold]Manifest:[/]       [cyan]{manifest_path}[/]",
        f"[bold]Namespace:[/]      [cyan]{KUBE_NAMESPACE}[/] [dim](locked)[/]",
        f"[bold]Model:[/]          [cyan]{settings.openai_model}[/]",
        f"[bold]Max Iterations:[/] [cyan]{settings.max_iterations}[/]",
        f"[bold]Dry Run:[/]        {'[yellow]Yes[/]' if settings.dry_run else '[green]No[/]'}",
        f"[bold]Poll Timeout:[/]   [cyan]{settings.poll_timeout_seconds}s[/]",
        f"[bold]Poll Interval:[/]  [cyan]{settings.poll_interval_seconds}s[/]",
        f"[bold]Log Level:[/]      [cyan]{settings.log_level}[/]",
    ]
    
    if settings.enable_mlflow:
        lines.append(f"[bold]MLflow Enabled:[/]  [green]Yes[/]")
        lines.append(f"[bold]MLflow URI:[/]      [cyan]{settings.mlflow_tracking_uri}[/]")
        lines.append(f"[bold]MLflow Exp:[/]      [cyan]{settings.mlflow_experiment_name}[/]")
    else:
        lines.append(f"[bold]MLflow Enabled:[/]  [yellow]No[/]")
        
    console.print(
        Panel(
            "\n".join(lines),
            title="\u2699\ufe0f  Configuration",
            title_align="left",
            border_style="blue",
            padding=(1, 2),
        )
    )


# ── CLI ───────────────────────────────────────────────────────────────


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--manifest", "-m",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to the Kubernetes YAML manifest file.",
)
@click.option(
    "--dry-run", "-d",
    is_flag=True,
    default=False,
    help="Print the LLM's corrected YAML without applying it.",
)
@click.option(
    "--max-iterations", "-n",
    type=click.IntRange(1, 10),
    default=None,
    help="Maximum autonomous retry loops (1-10, default: 5).",
)
@click.option(
    "--log-level", "-l",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    help="Logging verbosity (default: INFO).",
)
@click.option(
    "--enable-mlflow",
    is_flag=True,
    default=False,
    help="Enable Databricks/MLflow observability tracking.",
)
def main(
    manifest: str,
    dry_run: bool,
    max_iterations: int | None,
    log_level: str | None,
    enable_mlflow: bool,
) -> None:
    """
    Kube-AutoFix: Autonomous Kubernetes Debugging Agent.

    Deploys a YAML manifest to an isolated EKS namespace, monitors for
    failures, and uses GPT-4o to diagnose and auto-correct issues.
    """
    console = Console(force_terminal=True)

    # ── Banner ────────────────────────────────────────────────────
    console.print(_BANNER)

    # ── Load configuration ────────────────────────────────────────
    try:
        # CLI flags override env/config values
        overrides: dict = {}
        if dry_run:
            overrides["dry_run"] = True
        if max_iterations is not None:
            overrides["max_iterations"] = max_iterations
        if log_level is not None:
            overrides["log_level"] = log_level.upper()
        if enable_mlflow:
            overrides["enable_mlflow"] = True

        settings = Settings(**overrides)  # type: ignore[arg-type]
    except Exception as e:
        console.print(
            Panel(
                f"[bold red]Configuration Error[/]\n\n{e}\n\n"
                "[dim]Ensure OPENAI_API_KEY is set in your .env file "
                "or environment.[/]",
                border_style="red",
                padding=(1, 2),
            )
        )
        sys.exit(1)

    # ── Setup logging ─────────────────────────────────────────────
    _setup_logging(console, settings.log_level)
    logger = logging.getLogger("kube-autofix.main")

    # ── Print config panel ────────────────────────────────────────
    _print_config_panel(console, settings, manifest)

    # ── Load manifest ─────────────────────────────────────────────
    manifest_path = Path(manifest)
    try:
        initial_yaml = manifest_path.read_text(encoding="utf-8")
    except Exception as e:
        console.print(f"[bold red]Failed to read manifest:[/] {e}")
        sys.exit(1)

    if not initial_yaml.strip():
        console.print("[bold red]Manifest file is empty.[/]")
        sys.exit(1)

    console.print(
        f"\n  [green]\u2714[/] Loaded manifest: "
        f"[bold]{manifest_path.name}[/] "
        f"[dim]({len(initial_yaml)} bytes)[/]\n"
    )

    # ── Initialize components ─────────────────────────────────────
    try:
        with console.status(
            "[bold cyan]Connecting to Kubernetes cluster...[/]",
            spinner="dots",
        ):
            deployer = KubeDeployer()
            monitor = KubeMonitor()
            debugger = KubeDebugger()

        console.print(
            "  [green]\u2714[/] Connected to Kubernetes cluster\n"
        )
    except Exception as e:
        console.print(
            Panel(
                f"[bold red]Kubernetes Connection Failed[/]\n\n{e}\n\n"
                "[dim]Ensure your kubeconfig is configured:\n"
                "  aws eks update-kubeconfig "
                "--name autofix-cluster --region us-east-1[/]",
                border_style="red",
                padding=(1, 2),
            )
        )
        sys.exit(1)

    try:
        llm_engine = LLMEngine(settings)
        console.print(
            f"  [green]\u2714[/] LLM Engine ready "
            f"[dim]({settings.openai_model})[/]\n"
        )
    except Exception as e:
        console.print(
            f"[bold red]Failed to initialize LLM Engine:[/] {e}"
        )
        sys.exit(1)

    try:
        from observability.mlflow_tracker import MLflowTracker
        tracker = MLflowTracker(settings)
    except Exception as e:
        logger.warning(f"Failed to initialize MLflow tracker: {e}")
        tracker = None

    agent = AgentLoop(
        deployer=deployer,
        monitor=monitor,
        debugger=debugger,
        llm_engine=llm_engine,
        settings=settings,
        console=console,
        tracker=tracker,
    )

    try:
        result = agent.run(initial_yaml, manifest_name=manifest_path.name)
    except KeyboardInterrupt:
        console.print(
            "\n[bold yellow]Interrupted by user (Ctrl+C).[/]"
        )
        console.print(
            f"[dim]Resources may still exist in namespace "
            f"'{KUBE_NAMESPACE}'.\n"
            f"Clean up with: kubectl delete all --all "
            f"-n {KUBE_NAMESPACE}[/]"
        )
        sys.exit(130)
    except Exception as e:
        logger.exception("Unexpected error in agent loop")
        console.print(
            Panel(
                f"[bold red]Unexpected Error[/]\n\n{e}",
                border_style="red",
                padding=(1, 2),
            )
        )
        sys.exit(1)

    # ── Exit code ─────────────────────────────────────────────────
    if result.success:
        console.print(
            "[bold green]Agent completed successfully.[/] "
            ":tada:"
        )
        sys.exit(0)
    else:
        console.print(
            "[bold red]Agent could not resolve the issue.[/]"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
