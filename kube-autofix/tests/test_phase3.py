"""Phase 3 validation tests — run with: python tests/test_phase3.py"""

import sys
sys.path.insert(0, ".")

from llm.engine import LLMEngine, LLMEngineError, _SYSTEM_PROMPT, _build_user_prompt
from core.models import DebugBundle, PodDebugInfo, LLMDiagnosis
import yaml

passed = 0
failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        print(f"[PASS] {name}")
        passed += 1
    else:
        print(f"[FAIL] {name}")
        failed += 1


# Test 1: System prompt guardrails
check(
    "System prompt contains namespace guardrail",
    "autofix-agent-env" in _SYSTEM_PROMPT,
)
check(
    "System prompt contains minimal-changes rule",
    "Minimal Changes" in _SYSTEM_PROMPT,
)
check(
    "System prompt contains resource limits rule",
    "Resource Requests/Limits" in _SYSTEM_PROMPT,
)
check(
    "System prompt contains image tag guidance",
    "nginx:latest" in _SYSTEM_PROMPT or "nginx:1.27" in _SYSTEM_PROMPT,
)
check(
    "System prompt contains no-new-resources rule",
    "No New Resources" in _SYSTEM_PROMPT,
)

# Test 2: User prompt construction
bundle = DebugBundle(
    namespace="autofix-agent-env",
    pod_debug_infos=[
        PodDebugInfo(
            pod_name="test-pod",
            describe_output="Phase: Pending",
            logs="image pull error",
        )
    ],
    namespace_events="ImagePullBackOff event",
    summary="1 failing pod(s)",
)
prompt = _build_user_prompt("apiVersion: apps/v1", bundle, iteration=3, max_iterations=5)
check("User prompt contains iteration warning", "attempt 3 of 5" in prompt)
check("User prompt contains diagnostics", "ImagePullBackOff" in prompt)
check("User prompt contains manifest", "apiVersion: apps/v1" in prompt)

# Test 3: Final-attempt escalation
prompt_final = _build_user_prompt("apiVersion: apps/v1", bundle, iteration=4, max_iterations=5)
check("Final-attempt escalation triggers", "FINAL attempts" in prompt_final)

# Test 4: First iteration has NO warning
prompt_first = _build_user_prompt("apiVersion: apps/v1", bundle, iteration=1, max_iterations=5)
check("First iteration has no retry warning", "Previous fix attempts" not in prompt_first)

# Test 5: YAML validation — valid manifest
d1 = LLMDiagnosis(
    reasoning="test",
    root_cause="test",
    confidence_score=0.9,
    corrected_yaml="apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: test",
    changes_made=["test"],
)
try:
    LLMEngine._validate_corrected_yaml(d1)
    check("YAML validation accepts valid manifest", True)
except Exception as e:
    check(f"YAML validation accepts valid manifest (got {e})", False)

# Test 6: YAML validation — strips markdown fences
fenced_yaml = "```yaml\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: test\n```"
d2 = LLMDiagnosis(
    reasoning="test",
    root_cause="test",
    confidence_score=0.9,
    corrected_yaml=fenced_yaml,
    changes_made=["test"],
)
try:
    LLMEngine._validate_corrected_yaml(d2)
    check("YAML validation strips markdown fences", "```" not in d2.corrected_yaml)
except Exception as e:
    check(f"YAML validation strips fences (got {e})", False)

# Test 7: YAML validation — rejects invalid YAML
try:
    bad = LLMDiagnosis(
        reasoning="test",
        root_cause="test",
        confidence_score=0.9,
        corrected_yaml="not: valid: yaml: {{{",
        changes_made=[],
    )
    LLMEngine._validate_corrected_yaml(bad)
    check("YAML validation rejects invalid YAML", False)
except LLMEngineError:
    check("YAML validation rejects invalid YAML", True)

# Test 8: Namespace override protection
d3 = LLMDiagnosis(
    reasoning="test",
    root_cause="test",
    confidence_score=0.9,
    corrected_yaml="apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: test\n  namespace: default",
    changes_made=[],
)
LLMEngine._validate_corrected_yaml(d3)
parsed = list(yaml.safe_load_all(d3.corrected_yaml))
ns = parsed[0]["metadata"]["namespace"]
check("Namespace override protection works", ns == "autofix-agent-env")

# Test 9: YAML validation — rejects empty
try:
    d4 = LLMDiagnosis(
        reasoning="test",
        root_cause="test",
        confidence_score=0.9,
        corrected_yaml="---",
        changes_made=[],
    )
    LLMEngine._validate_corrected_yaml(d4)
    check("YAML validation rejects empty document", False)
except LLMEngineError:
    check("YAML validation rejects empty document", True)

# Test 10: YAML validation — rejects missing apiVersion
try:
    d5 = LLMDiagnosis(
        reasoning="test",
        root_cause="test",
        confidence_score=0.9,
        corrected_yaml="kind: Deployment\nmetadata:\n  name: test",
        changes_made=[],
    )
    LLMEngine._validate_corrected_yaml(d5)
    check("YAML validation rejects missing apiVersion", False)
except LLMEngineError:
    check("YAML validation rejects missing apiVersion", True)

# Summary
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if failed == 0:
    print("All Phase 3 validation checks passed!")
else:
    print("SOME TESTS FAILED")
    sys.exit(1)
