"""
Kube-AutoFix — Pod Status Monitor.

Polls pod status, detects failures, evaluates deployment health.
All queries scoped to 'autofix-agent-env' namespace.
"""

from __future__ import annotations

import logging
import time
from enum import Enum

import yaml as _yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from config import KUBE_NAMESPACE
from core.models import ContainerStatus, FailureReason, PodPhase, PodStatus

logger = logging.getLogger("kube-autofix.monitor")


class DeploymentState(str, Enum):
    HEALTHY = "healthy"
    PROGRESSING = "progressing"
    FAILED = "failed"
    NO_PODS = "no_pods"


class MonitorResult:
    """Outcome of a monitoring poll cycle."""

    def __init__(
        self,
        state: DeploymentState,
        pod_statuses: list[PodStatus],
        message: str = "",
    ) -> None:
        self.state = state
        self.pod_statuses = pod_statuses
        self.message = message

    @property
    def is_success(self) -> bool:
        return self.state == DeploymentState.HEALTHY

    @property
    def is_failed(self) -> bool:
        return self.state == DeploymentState.FAILED

    @property
    def failing_pods(self) -> list[PodStatus]:
        return [p for p in self.pod_statuses if p.has_failure]

    def __repr__(self) -> str:
        return (
            f"MonitorResult(state={self.state.value}, "
            f"pods={len(self.pod_statuses)}, message='{self.message}')"
        )


class KubeMonitor:
    """
    Polls Kubernetes pod statuses and evaluates deployment health.

    Usage:
        monitor = KubeMonitor()
        result = monitor.poll_until_ready(
            label_selector="app=autofix-test-nginx",
            timeout=120, interval=5,
        )
    """

    def __init__(self) -> None:
        try:
            config.load_kube_config()
        except config.ConfigException:
            config.load_incluster_config()
        self._core_v1 = client.CoreV1Api()

    # ── Pod status extraction ─────────────────────────────────────

    def get_pod_statuses(self, label_selector: str) -> list[PodStatus]:
        """Fetch pods matching the label selector and return PodStatus list."""
        try:
            pod_list = self._core_v1.list_namespaced_pod(
                namespace=KUBE_NAMESPACE,
                label_selector=label_selector,
            )
        except ApiException as e:
            logger.error(f"Failed to list pods: {e.status} {e.reason}")
            return []

        statuses: list[PodStatus] = []
        for pod in pod_list.items:
            cs_list = self._extract_container_statuses(pod)
            all_ready = all(c.ready for c in cs_list) and len(cs_list) > 0
            total_restarts = sum(c.restart_count for c in cs_list)

            conditions: list[dict] = []
            if pod.status.conditions:
                conditions = [
                    {
                        "type": c.type,
                        "status": c.status,
                        "reason": c.reason or "",
                        "message": c.message or "",
                    }
                    for c in pod.status.conditions
                ]

            statuses.append(
                PodStatus(
                    name=pod.metadata.name,
                    phase=PodPhase(pod.status.phase or "Unknown"),
                    ready=all_ready,
                    restart_count=total_restarts,
                    container_statuses=cs_list,
                    node_name=pod.spec.node_name,
                    start_time=pod.status.start_time,
                    conditions=conditions,
                )
            )
        return statuses

    @staticmethod
    def _extract_container_statuses(pod: client.V1Pod) -> list[ContainerStatus]:
        """Extract ContainerStatus models from a V1Pod object."""
        result: list[ContainerStatus] = []
        all_cs = []
        if pod.status.init_container_statuses:
            all_cs.extend(pod.status.init_container_statuses)
        if pod.status.container_statuses:
            all_cs.extend(pod.status.container_statuses)

        for cs in all_cs:
            state = "unknown"
            reason = None
            message = None
            if cs.state:
                if cs.state.running:
                    state = "running"
                elif cs.state.waiting:
                    state = "waiting"
                    reason = cs.state.waiting.reason
                    message = cs.state.waiting.message
                elif cs.state.terminated:
                    state = "terminated"
                    reason = cs.state.terminated.reason
                    message = cs.state.terminated.message

            result.append(
                ContainerStatus(
                    name=cs.name,
                    ready=cs.ready or False,
                    restart_count=cs.restart_count or 0,
                    state=state,
                    reason=reason,
                    message=message,
                    image=cs.image or "",
                )
            )
        return result

    # ── Failure detection ─────────────────────────────────────────

    @staticmethod
    def detect_failures(pod_statuses: list[PodStatus]) -> list[str]:
        """Return human-readable failure descriptions. Empty list = no failures."""
        failures: list[str] = []
        known = {r.value for r in FailureReason}

        for pod in pod_statuses:
            for cs in pod.container_statuses:
                if cs.reason and cs.reason in known:
                    detail = cs.message or "no additional detail"
                    failures.append(
                        f"Pod '{pod.name}' container '{cs.name}': "
                        f"{cs.reason} — {detail}"
                    )
            # Stuck Pending with no containers scheduled
            if pod.phase == PodPhase.PENDING and not pod.container_statuses:
                failures.append(
                    f"Pod '{pod.name}': stuck in Pending (no containers scheduled)"
                )
        return failures

    # ── Evaluate deployment state ─────────────────────────────────

    @staticmethod
    def evaluate(pod_statuses: list[PodStatus]) -> DeploymentState:
        """Evaluate aggregate deployment state from pod statuses."""
        if not pod_statuses:
            return DeploymentState.NO_PODS

        for pod in pod_statuses:
            if pod.has_failure:
                return DeploymentState.FAILED

        all_ready = all(
            p.ready and p.phase == PodPhase.RUNNING for p in pod_statuses
        )
        return DeploymentState.HEALTHY if all_ready else DeploymentState.PROGRESSING

    # ── Main polling loop ─────────────────────────────────────────

    def poll_until_ready(
        self,
        label_selector: str,
        timeout: int = 120,
        interval: int = 5,
    ) -> MonitorResult:
        """
        Poll pod statuses until all HEALTHY, FAILURE detected, or timeout.

        Args:
            label_selector: e.g. "app=autofix-test-nginx"
            timeout: max seconds to poll (default 120)
            interval: seconds between polls (default 5)
        """
        start = time.time()
        iteration = 0

        logger.info(
            f"⏳ Monitoring pods (selector='{label_selector}', "
            f"timeout={timeout}s, interval={interval}s)..."
        )

        while (elapsed := time.time() - start) < timeout:
            iteration += 1
            pod_statuses = self.get_pod_statuses(label_selector)
            state = self.evaluate(pod_statuses)

            summary = ", ".join(
                f"{p.name}={p.phase.value}(ready={p.ready})"
                for p in pod_statuses
            ) or "no pods found"
            logger.info(
                f"  Poll #{iteration} [{elapsed:.0f}s]: "
                f"state={state.value} | {summary}"
            )

            if state == DeploymentState.HEALTHY:
                return MonitorResult(
                    state=state,
                    pod_statuses=pod_statuses,
                    message="All pods are running and ready.",
                )

            if state == DeploymentState.FAILED:
                failures = self.detect_failures(pod_statuses)
                msg = "; ".join(failures)
                logger.warning(f"  ❌ Failure detected: {msg}")
                return MonitorResult(
                    state=state, pod_statuses=pod_statuses, message=msg,
                )

            time.sleep(interval)

        # Timeout
        pod_statuses = self.get_pod_statuses(label_selector)
        logger.warning(f"  ⏰ Timed out after {timeout}s.")
        return MonitorResult(
            state=DeploymentState.FAILED,
            pod_statuses=pod_statuses,
            message=f"Timed out after {timeout}s waiting for pods.",
        )

    # ── Utility ───────────────────────────────────────────────────

    @staticmethod
    def label_selector_from_manifest(yaml_str: str) -> str | None:
        """Extract pod label selector from a Deployment manifest."""
        try:
            docs = list(_yaml.safe_load_all(yaml_str))
        except _yaml.YAMLError:
            return None

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") == "Deployment":
                labels = (
                    doc.get("spec", {})
                    .get("selector", {})
                    .get("matchLabels", {})
                )
                if labels:
                    return ",".join(f"{k}={v}" for k, v in labels.items())
        return None
