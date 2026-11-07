"""Complete model outputs used by diagnosis tests; not operational runbooks."""
import json


def shown_evidence(prompt):
    if prompt.startswith("{"):
        return json.loads(prompt)["evidence"]
    return json.JSONDecoder().raw_decode(prompt.split("Citable observations (only these IDs may be cited):\n", 1)[1].lstrip())[0]


def action_plan(refs):
    return {
        "version": 1,
        "supporting_evidence": [{"claim": "Collected incident observation", "evidence_refs": refs}],
        "missing_evidence": ["Failing runtime input has not been reproduced"],
        "checks": [{"id": "", "instruction": "Compare the failing runtime input with the documented configuration",
                    "context": "Read-only view of the failing service configuration",
                    "command": None, "placeholders": [], "evidence_to_collect": "Input and relevant setting, with secrets removed",
                    "supports_hypothesis": "The failing input violates the configured requirement",
                    "weakens_hypothesis": "The input satisfies the configured requirement"}],
        "runbooks": [], "mitigations": [],
    }
