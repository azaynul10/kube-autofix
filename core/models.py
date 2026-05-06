"""
Kube-AutoFix — Pydantic Data Models.

Defines all structured data types used across the system:
  • ContainerStatus / PodStatus  — typed views of K8s pod state
  • DeploymentResult             — outcome of a manifest apply/delete
  • DebugBundle                  — aggregated diagnostics for the LLM
  • LLMDiagnosis                 — structured output schema for GPT-4o
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────


class PodPhase(str, Enum):
    """Possible high-level pod phases reported by the Kubelet."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class FailureReason(str, Enum):
    """Known container-level failure reasons the monitor watches for."""

    CRASH_LOOP_BACK_OFF = "CrashLoopBackOff"
    IMAGE_PULL_BACK_OFF = "ImagePullBackOff"
    ERR_IMAGE_PULL = "ErrImagePull"
    OOM_KILLED = "OOMKilled"
    CREATE_CONTAINER_ERROR = "CreateContainerError"
    CONTAINER_CANNOT_RUN = "ContainerCannotRun"
    RUN_CONTAINER_ERROR = "RunContainerError"
    PENDING_STUCK = "PendingStuck"  # Custom — pod stuck in Pending too long


# ── Kubernetes state models ───────────────────────────────────────────


class ContainerStatus(BaseModel):
    """Typed snapshot of a single container's status within a pod."""

    name: str
    ready: bool = False
    restart_count: int = 0
    state: str = "unknown"  # e.g., "running", "waiting", "terminated"
    reason: str | None = None  # e.g., "CrashLoopBackOff"
    message: str | None = None
    image: str = ""


class PodStatus(BaseModel):
    """Typed snapshot of a single pod's status."""

    name: str
    phase: PodPhase = PodPhase.UNKNOWN
    ready: bool = False
    restart_count: int = 0
    container_statuses: list[ContainerStatus] = Field(default_factory=list)
    node_name: str | None = None
    start_time: datetime | None = None
    conditions: list[dict] = Field(default_factory=list)

    @property
    def has_failure(self) -> bool:
        """Return True if any container reports a known failure reason."""
        failure_reasons = {e.value for e in FailureReason}
        return any(
            cs.reason in failure_reasons
            for cs in self.container_statuses
            if cs.reason
        )

    @property
    def failure_reasons(self) -> list[str]:
        """Collect all failure reasons across containers."""
        known = {e.value for e in FailureReason}
        return [
            cs.reason
            for cs in self.container_statuses
            if cs.reason and cs.reason in known
        ]


# ── Deployment result ─────────────────────────────────────────────────


class DeploymentResult(BaseModel):
    """Outcome of a manifest apply or delete operation."""

    success: bool
    message: str
    resources_created: list[str] = Field(default_factory=list)
    resources_failed: list[str] = Field(default_factory=list)


# ── Debug bundle (fed to the LLM) ────────────────────────────────────


class PodDebugInfo(BaseModel):
    """Debug information for a single pod."""

    pod_name: str
    describe_output: str = ""
    logs: str = ""
    events: str = ""


class DebugBundle(BaseModel):
    """
    Aggregated diagnostics collected from the cluster for all
    failing pods.  This is the primary context payload sent to GPT-4o.
    """

    namespace: str
    pod_debug_infos: list[PodDebugInfo] = Field(default_factory=list)
    namespace_events: str = ""
    summary: str = ""  # One-line summary for logging

    def to_prompt_context(self) -> str:
        """Format the bundle as a text block suitable for an LLM prompt."""
        sections: list[str] = []
        sections.append(f"=== Namespace: {self.namespace} ===")

        if self.namespace_events:
            sections.append(f"\n--- Namespace Events ---\n{self.namespace_events}")

        for info in self.pod_debug_infos:
            sections.append(f"\n--- Pod: {info.pod_name} ---")
            if info.describe_output:
                sections.append(f"\n[Describe]\n{info.describe_output}")
            if info.logs:
                sections.append(f"\n[Logs]\n{info.logs}")
            if info.events:
                sections.append(f"\n[Events]\n{info.events}")

        return "\n".join(sections)


# ── LLM structured output ────────────────────────────────────────────


class LLMDiagnosis(BaseModel):
    """
    The structured JSON response expected from GPT-4o.

    This schema is used with OpenAI's structured output mode
    (response_format) to guarantee parseable, typed results.
    """

    reasoning: str = Field(
        ...,
        description=(
            "Step-by-step root cause analysis of why the deployment failed."
        ),
    )
    root_cause: str = Field(
        ...,
        description="One-line summary of the identified root cause.",
    )
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence in the fix (0.0 = guess, 1.0 = certain).",
    )
    corrected_yaml: str = Field(
        ...,
        description=(
            "The full corrected Kubernetes YAML manifest. "
            "Must be valid YAML that can be applied directly."
        ),
    )
    changes_made: list[str] = Field(
        default_factory=list,
        description="Bullet-point list of changes made to the original YAML.",
    )
