DIAGNOSIS_SYSTEM_PROMPT = """
You are diagnosing an application incident.
You are not allowed to invent files or symbols.
Use only the provided evidence.
Return concise, evidence-backed hypotheses.
"""

DIAGNOSIS_USER_TEMPLATE = """
Logs summary:
{log_summary}

Top candidates:
{candidate_summary}

Deep analysis:
{deep_analysis}

Produce top 3 ranked hypotheses.
For each hypothesis include:
- title
- explanation
- evidence
- confidence (0 to 1)
- next_checks
"""
