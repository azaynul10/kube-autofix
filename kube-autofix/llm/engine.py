"""
Kube-AutoFix — GPT-4o LLM Reasoning Engine.

Responsible for:
  • Constructing a carefully engineered system + user prompt.
  • Calling GPT-4o via the OpenAI SDK's structured output mode
    (client.beta.chat.completions.parse) to guarantee a valid
    LLMDiagnosis Pydantic object is returned.
  • Validating the corrected YAML before returning it.
  • Retry logic for transient OpenAI API errors.

The system prompt instructs GPT-4o to act as a Staff-Level Kubernetes
SRE with strict operational boundaries.
"""

from __future__ import annotations

import logging

import yaml
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import KUBE_NAMESPACE, Settings
from core.models import DebugBundle, LLMDiagnosis

logger = logging.getLogger("kube-autofix.llm")

# ── System Prompt ─────────────────────────────────────────────────────
# This prompt defines GPT-4o's persona, capabilities, and hard boundaries.
# It is intentionally long and explicit to minimise hallucination and
# ensure the model stays within the operational guardrails.

_SYSTEM_PROMPT = f"""\
You are a Staff-Level Kubernetes Site Reliability Engineer (SRE) embedded \
inside an autonomous debugging agent called "Kube-AutoFix". Your job is to \
diagnose why a Kubernetes deployment has failed and produce a corrected \
YAML manifest that will fix the issue.

## Your Capabilities
- You can read and understand Kubernetes YAML manifests (Deployments, \
Services, ConfigMaps, Pods, Secrets).
- You can interpret `kubectl describe pod` output, container logs, and \
namespace events.
- You can identify common Kubernetes failure modes: ImagePullBackOff, \
CrashLoopBackOff, OOMKilled, CreateContainerError, misconfigured probes, \
invalid resource requests/limits, missing ConfigMaps/Secrets, port \
mismatches, and selector mismatches.

## Strict Operational Boundaries
You MUST follow these rules without exception:

1. **Namespace Lock**: The corrected YAML MUST NOT change the namespace. \
All resources operate in the "{KUBE_NAMESPACE}" namespace. Never add, \
remove, or modify namespace fields beyond ensuring they match \
"{KUBE_NAMESPACE}".

2. **Minimal Changes**: Make the SMALLEST possible change to fix the root \
cause. Do not refactor, restructure, or "improve" the manifest beyond \
what is necessary to resolve the failure.

3. **Resource Requests/Limits**: Do NOT modify CPU or memory \
requests/limits UNLESS the diagnostics clearly indicate an OOMKilled \
or resource-related failure. If you do change them, explain why.

4. **Image Tags**: When fixing image-related errors (ImagePullBackOff, \
ErrImagePull), prefer well-known stable tags (e.g., "nginx:latest", \
"nginx:1.27", "python:3.12-slim"). Never invent or guess image tags.

5. **Valid YAML Only**: The corrected_yaml field MUST contain a complete, \
valid Kubernetes YAML manifest that can be directly applied with \
`kubectl apply -f`. Do not include YAML front-matter fences (```yaml).

6. **Preserve Structure**: Keep all labels, annotations, selectors, and \
metadata from the original manifest unless they are the root cause.

7. **No New Resources**: Do not add new resource kinds (e.g., don't add \
a Service if only a Deployment was provided) unless the error explicitly \
requires it (e.g., a missing ConfigMap reference).

## Response Format
You MUST respond with a JSON object matching the provided schema exactly. \
The fields are:
- reasoning: Step-by-step root cause analysis (be thorough).
- root_cause: One-line summary of the issue.
- confidence_score: 0.0 (pure guess) to 1.0 (certain).
- corrected_yaml: The full corrected manifest as a YAML string.
- changes_made: Bullet-point list of every change you made.

Be precise. Be minimal. Be correct.\
"""


def _build_user_prompt(
    current_yaml: str,
    debug_bundle: DebugBundle,
    iteration: int,
    max_iterations: int,
) -> str:
    """
    Construct the user-role message containing the current manifest,
    debug diagnostics, and iteration context.
    """
    context = debug_bundle.to_prompt_context()

    iteration_warning = ""
    if iteration > 1:
        iteration_warning = (
            f"\n\n⚠️  IMPORTANT: This is attempt {iteration} of "
            f"{max_iterations}. Previous fix attempts have FAILED. "
            f"The YAML below is the result of the last failed fix. "
            f"Carefully review what went wrong and try a DIFFERENT approach."
        )

    if iteration >= max_iterations - 1:
        iteration_warning += (
            "\n🚨 CRITICAL: This is one of the FINAL attempts. "
            "Be extremely careful and conservative with your fix."
        )

    return f"""\
## Current Manifest (Iteration {iteration}/{max_iterations})
{iteration_warning}

```yaml
{current_yaml.strip()}
```

## Cluster Diagnostics

{context}

## Task
Analyze the diagnostics above. Identify the root cause of the deployment \
failure. Produce a corrected YAML manifest that fixes the issue with \
minimal changes. Return your response as the structured JSON object.\
"""


class LLMEngine:
    """
    GPT-4o reasoning engine for Kubernetes manifest diagnosis and repair.

    Uses OpenAI's structured output mode to guarantee responses conform
    to the LLMDiagnosis Pydantic schema.

    Usage:
        engine = LLMEngine(settings)
        diagnosis = engine.diagnose(
            current_yaml=yaml_str,
            debug_bundle=bundle,
            iteration=1,
            max_iterations=5,
        )
        print(diagnosis.root_cause)
        print(diagnosis.corrected_yaml)
    """

    def __init__(self, settings: Settings) -> None:
        self._client = OpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=settings.openai_api_key,
        )
        self._model = settings.openai_model
        logger.info(f"LLM Engine initialized (model={self._model})")

    # ── Main diagnosis method ─────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(
            (APIConnectionError, APITimeoutError, RateLimitError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
        before_sleep=lambda retry_state: logger.warning(
            f"  OpenAI API error, retrying in "
            f"{retry_state.next_action.sleep:.1f}s... "  # type: ignore[union-attr]
            f"(attempt {retry_state.attempt_number}/3)"
        ),
    )
    def diagnose(
        self,
        current_yaml: str,
        debug_bundle: DebugBundle,
        iteration: int = 1,
        max_iterations: int = 5,
    ) -> LLMDiagnosis:
        """
        Send the current manifest and debug diagnostics to GPT-4o
        and receive a structured diagnosis with a corrected YAML fix.

        Args:
            current_yaml:   The current (broken) YAML manifest string.
            debug_bundle:   Aggregated pod diagnostics from KubeDebugger.
            iteration:      Current retry iteration (1-based).
            max_iterations: Maximum allowed iterations.

        Returns:
            LLMDiagnosis with reasoning, root_cause, confidence_score,
            corrected_yaml, and changes_made.

        Raises:
            openai.APIConnectionError: On network issues (retried 3x).
            openai.APITimeoutError:    On timeout (retried 3x).
            openai.RateLimitError:     On rate limit (retried 3x).
            LLMEngineError:            On invalid/unparseable response.
        """
        user_prompt = _build_user_prompt(
            current_yaml=current_yaml,
            debug_bundle=debug_bundle,
            iteration=iteration,
            max_iterations=max_iterations,
        )

        logger.info(
            f"🧠 Sending diagnosis request to {self._model} "
            f"(iteration {iteration}/{max_iterations})..."
        )
        logger.debug(f"  User prompt length: {len(user_prompt)} chars")

        # ── Call GPT-4o with structured output parsing ────────────
        completion = self._client.beta.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=LLMDiagnosis,
            temperature=0.2,  # Low temperature for deterministic fixes
        )

        # Extract the parsed Pydantic object
        parsed = completion.choices[0].message.parsed

        if parsed is None:
            # This can happen if the model refused or returned empty
            refusal = completion.choices[0].message.refusal
            raise LLMEngineError(
                f"GPT-4o returned no structured output. "
                f"Refusal: {refusal or 'none'}"
            )

        diagnosis: LLMDiagnosis = parsed

        # ── Post-validation ───────────────────────────────────────
        self._validate_corrected_yaml(diagnosis)

        # ── Log the result ────────────────────────────────────────
        logger.info(f"  Root cause: {diagnosis.root_cause}")
        logger.info(f"  Confidence: {diagnosis.confidence_score:.0%}")
        if diagnosis.changes_made:
            for change in diagnosis.changes_made:
                logger.info(f"    - {change}")

        # Log token usage if available
        if completion.usage:
            logger.debug(
                f"  Tokens: prompt={completion.usage.prompt_tokens}, "
                f"completion={completion.usage.completion_tokens}, "
                f"total={completion.usage.total_tokens}"
            )

        return diagnosis

    # ── YAML validation ───────────────────────────────────────────

    @staticmethod
    def _validate_corrected_yaml(diagnosis: LLMDiagnosis) -> None:
        """
        Validate that the LLM's corrected YAML is syntactically valid
        and contains required Kubernetes fields.

        Raises LLMEngineError if validation fails.
        """
        corrected = diagnosis.corrected_yaml.strip()

        # Strip markdown fences if the model included them despite instructions
        # Handle formats: ```yaml\n...\n```, ```\n...\n```, etc.
        if corrected.startswith("```"):
            lines = corrected.split("\n")
            # Remove the opening fence line (first line)
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            # Remove the closing fence line (last line)
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            corrected = "\n".join(lines)
            diagnosis.corrected_yaml = corrected

        # Parse YAML
        try:
            docs = list(yaml.safe_load_all(corrected))
        except yaml.YAMLError as e:
            raise LLMEngineError(
                f"LLM returned invalid YAML: {e}"
            ) from e

        # Filter out empty documents
        docs = [d for d in docs if d is not None]

        if not docs:
            raise LLMEngineError("LLM returned empty YAML document.")

        # Validate each document has basic K8s structure
        for i, doc in enumerate(docs):
            if not isinstance(doc, dict):
                raise LLMEngineError(
                    f"YAML document {i} is not a mapping "
                    f"(got {type(doc).__name__})."
                )
            for field in ("apiVersion", "kind"):
                if field not in doc:
                    raise LLMEngineError(
                        f"YAML document {i} missing required "
                        f"field '{field}'."
                    )

        # Verify namespace was not changed
        for doc in docs:
            ns = doc.get("metadata", {}).get("namespace")
            if ns and ns != KUBE_NAMESPACE:
                logger.warning(
                    f"  LLM changed namespace to '{ns}' — "
                    f"overriding back to '{KUBE_NAMESPACE}'."
                )
                doc["metadata"]["namespace"] = KUBE_NAMESPACE
                # Re-serialize with the fixed namespace
                diagnosis.corrected_yaml = yaml.dump_all(
                    docs, default_flow_style=False
                )

        logger.debug("  Corrected YAML passed validation.")


class LLMEngineError(Exception):
    """Raised when the LLM returns an invalid or unusable response."""

    pass
