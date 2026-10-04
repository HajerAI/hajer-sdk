"""A deterministic grader for `llm-rubric`, so the example needs no model key and gives the same verdict every run.

promptfoo lets an assertion name its own grading provider (`provider: file://grader.py`), and `rubricPrompt` in
the config renders the rubric and the output as two labelled lines. The rule is the one a human reviewer would
apply to a refund answer: a rubric says `must not say: <phrase>[, <phrase>...]`, and the output passes when none
of those phrases appears in it. The verdict is the JSON object `llm-rubric` expects back.
"""

from __future__ import annotations

import json

from hajer._json import JsonObject

RUBRIC_LINE = "RUBRIC:"
OUTPUT_LINE = "OUTPUT:"
FORBIDDEN = "must not say:"


def _section(prompt: str, label: str) -> str:
    for line in prompt.splitlines():
        if line.startswith(label):
            return line[len(label) :].strip()
    return ""


def call_api(prompt: str, options: JsonObject, context: JsonObject) -> JsonObject:
    del options, context
    rubric = _section(prompt, RUBRIC_LINE)
    output = _section(prompt, OUTPUT_LINE).lower()
    forbidden = [phrase.strip().lower() for phrase in rubric.partition(FORBIDDEN)[2].split(",") if phrase.strip()]
    offending = [phrase for phrase in forbidden if phrase in output]
    verdict = {
        "pass": not offending,
        "score": 0.0 if offending else 1.0,
        "reason": f"the output says {offending!r}, which the rubric forbids"
        if offending
        else "none of the forbidden phrases appears in the output",
    }
    return {"output": json.dumps(verdict)}
