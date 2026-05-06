"""
Kube-AutoFix — Debug Info Collector.

Responsible for:
  • Fetching pod-level describe output (conditions, events, volumes).
  • Fetching container logs (last N lines).
  • Fetching namespace-level events.
  • Bundling all diagnostics into a DebugBundle for the LLM.

All queries scoped to 'autofix-agent-env' namespace.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from config import KUBE_NAMESPACE
from core.models import DebugBundle, PodDebugInfo, PodStatus

logger = logging.getLogger("kube-autofix.debugger")

# Maximum number of log lines to fetch per container
_MAX_LOG_LINES = 100


class KubeDebugger:
    """
    Collects diagnostic information from failing pods and namespace events.
    Produces a DebugBundle that is fed to the LLM reasoning engine.
    """

    def __init__(self) -> None:
        try:
            config.load_kube_config()
        except config.ConfigException:
            config.load_incluster_config()
        self._core_v1 = client.CoreV1Api()

    # ── Pod describe ──────────────────────────────────────────────

    def describe_pod(self, pod_name: str) -> str:
        """
        Build a human-readable 'describe' output for a pod.

        This replicates the most useful sections of `kubectl describe pod`:
        status, conditions, container states, and recent events.
        """
        try:
            pod: client.V1Pod = self._core_v1.read_namespaced_pod(
                name=pod_name, namespace=KUBE_NAMESPACE
            )
        except ApiException as e:
            msg = f"Failed to describe pod '{pod_name}': {e.status} {e.reason}"
            logger.error(msg)
            return msg

        lines: list[str] = []
        lines.append(f"Name:       {pod.metadata.name}")
        lines.append(f"Namespace:  {pod.metadata.namespace}")
        lines.append(f"Node:       {pod.spec.node_name or 'N/A'}")
        lines.append(f"Phase:      {pod.status.phase}")
        lines.append(f"Start Time: {pod.status.start_time or 'N/A'}")

        # Labels
        if pod.metadata.labels:
            labels_str = ", ".join(
                f"{k}={v}" for k, v in pod.metadata.labels.items()
            )
            lines.append(f"Labels:     {labels_str}")

        # Conditions
        if pod.status.conditions:
            lines.append("\nConditions:")
            lines.append(f"  {'Type':<25} {'Status':<10} {'Reason'}")
            lines.append(f"  {'----':<25} {'------':<10} {'------'}")
            for c in pod.status.conditions:
                lines.append(
                    f"  {c.type:<25} {c.status:<10} {c.reason or ''}"
                )

        # Container statuses
        all_cs = []
        if pod.status.init_container_statuses:
            all_cs.extend(
                ("init", cs) for cs in pod.status.init_container_statuses
            )
        if pod.status.container_statuses:
            all_cs.extend(
                ("container", cs) for cs in pod.status.container_statuses
            )

        if all_cs:
            lines.append("\nContainers:")
            for kind, cs in all_cs:
                prefix = f"  [{kind}] {cs.name}:"
                lines.append(prefix)
                lines.append(f"    Image:          {cs.image}")
                lines.append(f"    Ready:          {cs.ready}")
                lines.append(f"    Restart Count:  {cs.restart_count}")

                if cs.state:
                    if cs.state.running:
                        lines.append(
                            f"    State:          Running "
                            f"(since {cs.state.running.started_at})"
                        )
                    elif cs.state.waiting:
                        lines.append(
                            f"    State:          Waiting"
                        )
                        lines.append(
                            f"    Reason:         {cs.state.waiting.reason or 'N/A'}"
                        )
                        if cs.state.waiting.message:
                            lines.append(
                                f"    Message:        {cs.state.waiting.message}"
                            )
                    elif cs.state.terminated:
                        lines.append(
                            f"    State:          Terminated"
                        )
                        lines.append(
                            f"    Reason:         {cs.state.terminated.reason or 'N/A'}"
                        )
                        lines.append(
                            f"    Exit Code:      {cs.state.terminated.exit_code}"
                        )

        # Pod-level events
        events_text = self._get_pod_events(pod_name)
        if events_text:
            lines.append(f"\nEvents:\n{events_text}")

        return "\n".join(lines)

    # ── Container logs ────────────────────────────────────────────

    def get_pod_logs(
        self,
        pod_name: str,
        container: str | None = None,
        tail_lines: int = _MAX_LOG_LINES,
        previous: bool = False,
    ) -> str:
        """
        Fetch the last N lines of logs from a pod's container.

        Args:
            pod_name:   Name of the pod.
            container:  Container name (optional if single-container pod).
            tail_lines: Number of lines to fetch from the tail.
            previous:   If True, fetch logs from the previous terminated container.
        """
        kwargs: dict = {
            "name": pod_name,
            "namespace": KUBE_NAMESPACE,
            "tail_lines": tail_lines,
            "previous": previous,
        }
        if container:
            kwargs["container"] = container

        try:
            logs = self._core_v1.read_namespaced_pod_log(**kwargs)
            return logs or "(empty log output)"
        except ApiException as e:
            if e.status == 400:
                # Common: container not started yet, or previous log unavailable
                return f"(logs unavailable: {e.reason})"
            msg = (
                f"Failed to fetch logs for pod '{pod_name}'"
                f"{f' container {container}' if container else ''}: "
                f"{e.status} {e.reason}"
            )
            logger.warning(msg)
            return msg

    # ── Namespace events ──────────────────────────────────────────

    def get_namespace_events(self, limit: int = 30) -> str:
        """
        Fetch recent events from the agent namespace, sorted by last
        timestamp (most recent first).
        """
        try:
            events = self._core_v1.list_namespaced_event(
                namespace=KUBE_NAMESPACE
            )
        except ApiException as e:
            msg = f"Failed to list namespace events: {e.status} {e.reason}"
            logger.error(msg)
            return msg

        if not events.items:
            return "(no events)"

        # Sort by last_timestamp descending, handling None timestamps
        now = datetime.now(timezone.utc)
        sorted_events = sorted(
            events.items,
            key=lambda ev: ev.last_timestamp or now,
            reverse=True,
        )

        lines: list[str] = []
        for ev in sorted_events[:limit]:
            ts = (
                ev.last_timestamp.strftime("%H:%M:%S")
                if ev.last_timestamp
                else "??:??:??"
            )
            obj = (
                f"{ev.involved_object.kind}/{ev.involved_object.name}"
                if ev.involved_object
                else "?"
            )
            lines.append(
                f"  {ts}  {ev.type:<8}  {ev.reason:<25}  "
                f"{obj:<40}  {ev.message or ''}"
            )

        return "\n".join(lines)

    # ── Pod-specific events ───────────────────────────────────────

    def _get_pod_events(self, pod_name: str) -> str:
        """Fetch events specifically related to a pod."""
        try:
            events = self._core_v1.list_namespaced_event(
                namespace=KUBE_NAMESPACE,
                field_selector=f"involvedObject.name={pod_name}",
            )
        except ApiException:
            return ""

        if not events.items:
            return "  (no events for this pod)"

        lines: list[str] = []
        for ev in events.items:
            ts = (
                ev.last_timestamp.strftime("%H:%M:%S")
                if ev.last_timestamp
                else "??:??:??"
            )
            lines.append(
                f"  {ts}  {ev.type:<8}  {ev.reason:<25}  "
                f"{ev.message or ''}"
            )
        return "\n".join(lines)

    # ── Bundle collection ─────────────────────────────────────────

    def collect_debug_bundle(
        self, pod_statuses: list[PodStatus]
    ) -> DebugBundle:
        """
        Aggregate all diagnostic information for the given failing pods
        into a single DebugBundle ready for the LLM.

        For each pod:
          1. Describe output (conditions, container states, events)
          2. Container logs (last 100 lines per container)

        Plus namespace-wide events.
        """
        logger.info(
            f"🔍 Collecting debug info for {len(pod_statuses)} pod(s)..."
        )

        pod_infos: list[PodDebugInfo] = []

        for pod in pod_statuses:
            # Describe
            describe = self.describe_pod(pod.name)

            # Logs — fetch from each container
            log_sections: list[str] = []
            for cs in pod.container_statuses:
                # Current logs
                current = self.get_pod_logs(pod.name, container=cs.name)
                log_sections.append(f"[{cs.name}] Current:\n{current}")

                # Previous container logs (useful for CrashLoopBackOff)
                if cs.restart_count > 0:
                    prev = self.get_pod_logs(
                        pod.name, container=cs.name, previous=True
                    )
                    log_sections.append(f"[{cs.name}] Previous:\n{prev}")

            logs = "\n\n".join(log_sections) if log_sections else "(no logs)"

            # Pod-specific events
            events = self._get_pod_events(pod.name)

            pod_infos.append(
                PodDebugInfo(
                    pod_name=pod.name,
                    describe_output=describe,
                    logs=logs,
                    events=events,
                )
            )
            logger.debug(f"  Collected debug info for pod '{pod.name}'")

        # Namespace events
        ns_events = self.get_namespace_events()

        # Build summary
        failure_reasons = []
        for pod in pod_statuses:
            failure_reasons.extend(pod.failure_reasons)
        summary = (
            f"{len(pod_statuses)} failing pod(s); "
            f"reasons: {', '.join(set(failure_reasons)) or 'unknown'}"
        )

        bundle = DebugBundle(
            namespace=KUBE_NAMESPACE,
            pod_debug_infos=pod_infos,
            namespace_events=ns_events,
            summary=summary,
        )

        logger.info(f"  📦 Debug bundle ready: {summary}")
        return bundle
