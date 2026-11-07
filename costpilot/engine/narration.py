"""Slot-based narration: the model may arrange facts, never digits.

A rationale is a template of ``{{fact_id}}`` or ``{{fact_id|FORMAT}}`` slots.
The renderer substitutes formatted engine facts. The validator rejects unknown
slots, hand-written numerals, spelled quantities, verdict claims, and missing
citations, then falls back to a deterministic sentence built from the trace.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from costpilot.engine.money import HUNDRED, ZERO, display_amount, fmt_money
from costpilot.engine.trace import CalcResult, Fact

ALLOWED_FORMATS = {
    "INR",
    "INR0",
    "PCT1",
    "PCT2",
    "QTY0",
    "QTY2",
    "RATIO2",
    "HOURS1",
}
_FORMAT_HINT = ", ".join(sorted(ALLOWED_FORMATS))
_SLOT = re.compile(r"\{\{\s*([a-z0-9_]+)\s*(?:\|\s*([A-Za-z0-9_]+)\s*)?\}\}")
_RULE_ID = re.compile(r"\b[A-Z]{2,4}(?:-[A-Z0-9]+){1,3}\b")
_DATE = re.compile(r"\b20\d{2}-\d{2}(?:-\d{2})?\b")
_STEP_ID = re.compile(r"\b[sS]\d{1,3}\b")
_DECISION_WORDS = re.compile(
    r"\b(accept|accepted|reject|rejected|investigate|investigated|favorable|favourable|"
    r"adverse|mixed|recommend|recommendation|verdict)\b",
    re.IGNORECASE,
)
_CAUSAL = re.compile(
    r"\b(because|since|therefore|thus|hence|driven by|due to|as a result|resulting in)\b",
    re.IGNORECASE,
)
_OPTIMISTIC = re.compile(
    r"\b(gain|gains|gainful|improve|improves|improved|headroom|upside|opportunity|"
    r"benefit|beneficial|strong|healthy|comfortable)\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    "fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|lakhs|crore|crores|"
    "million|billion"
)
_SPELLED = re.compile(
    rf"\b(?:{_NUMBER_WORDS})(?:\s+(?:{_NUMBER_WORDS})){{0,3}}\s+"
    r"(?:rupees?|rs\.?|inr|units?|pieces?|hours?|days?|weeks?|months?|percent|per\s*cent|%)",
    re.IGNORECASE,
)
RAW_SYMBOLS = re.compile(r"[0-9₹%]")
MAX_WORDS = 120


@dataclass
class NarrationValidation:
    ok: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": self.checks,
            "issues": self.issues,
            "max_words": MAX_WORDS,
        }


def _group(value: Decimal, places: int) -> str:
    quant = Decimal(1).scaleb(-places)
    rounded = value.quantize(quant)
    text = f"{abs(rounded):,.{places}f}"
    return f"({text})" if rounded < ZERO else text


def format_fact(fact: Fact, fmt: str | None = None) -> str:
    """Format one engine fact. Percent formats treat ratio facts as ratios."""
    selected = (fmt or "").upper() or None
    if selected is not None and selected not in ALLOWED_FORMATS:
        raise ValueError(
            f"unknown narration format {selected!r}; use one of: {_FORMAT_HINT}"
        )
    value = fact.value
    unit = fact.unit
    if selected is None:
        # Keep the exact display the trace shows, so the rationale and the
        # evidence block can never diverge.
        return display_amount(value, unit)
    if selected in {"PCT1", "PCT2"}:
        places = 1 if selected == "PCT1" else 2
        points = value * HUNDRED if unit in {"ratio", "score"} else value
        return f"{points:,.{places}f}%"
    if selected == "INR":
        return fmt_money(value)
    if selected == "INR0":
        return _group(value, 0)
    if selected == "QTY0":
        return _group(value, 0)
    if selected == "QTY2":
        return _group(value, 2)
    if selected == "RATIO2":
        return _group(value, 2)
    if selected == "HOURS1":
        return f"{_group(value, 1)} hours"
    raise ValueError(f"unhandled narration format {selected!r}")


def render(template: str, result: CalcResult) -> str:
    """Substitute fact slots. Unknown facts and formats fail loudly."""
    facts = result.fact_map()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in facts:
            raise ValueError(f"rationale cites unknown fact {key}")
        return format_fact(facts[key], match.group(2))

    return _SLOT.sub(replace, template)


def is_slot_only(template: str) -> bool:
    stripped = _SLOT.sub("", template)
    stripped = _RULE_ID.sub("", stripped)
    stripped = _DATE.sub("", stripped)
    return not RAW_SYMBOLS.search(stripped)


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text))


def validate(
    template: str,
    result: CalcResult,
    *,
    strict_citations: bool = False,
) -> NarrationValidation:
    """Run every provenance check against a rationale template."""
    checks: list[dict[str, Any]] = []
    issues: list[str] = []
    facts = result.fact_map()

    def check(name: str, passed: bool, detail: str, hard: bool = True) -> None:
        checks.append({"check": name, "status": "pass" if passed else ("fail" if hard else "warn"), "detail": detail})
        if not passed:
            issues.append(f"{name}: {detail}")

    unknown = [match.group(1) for match in _SLOT.finditer(template) if match.group(1) not in facts]
    bad_format = [
        match.group(2)
        for match in _SLOT.finditer(template)
        if match.group(2) and match.group(2) not in ALLOWED_FORMATS
    ]
    check(
        "unknown_slot",
        not unknown,
        "every placeholder references a fact in the run" if not unknown else f"unknown facts: {', '.join(sorted(set(unknown)))}",
    )
    check(
        "placeholder_format",
        not bad_format,
        "all formats are in the allowlist" if not bad_format else f"unknown formats: {', '.join(sorted(set(bad_format)))}",
    )

    stripped = _SLOT.sub("", template)
    stripped = _RULE_ID.sub("", stripped)
    stripped = _DATE.sub("", stripped)
    raw = sorted(set(RAW_SYMBOLS.findall(stripped)))
    check(
        "raw_numerals",
        not raw,
        "no hand-written digits, currency signs, or percent signs" if not raw else f"raw symbols found: {''.join(raw)}",
    )

    spelled = _SPELLED.findall(template)
    check(
        "spelled_quantities",
        not spelled,
        "no spelled-out quantities adjacent to a unit" if not spelled else f"spelled quantities: {', '.join(spelled[:3])}",
    )

    decision_hits = sorted({match.group(1).lower() for match in _DECISION_WORDS.finditer(stripped)})
    check(
        "verdict_fidelity",
        not decision_hits,
        "decision words stay in the deterministic verdict block" if not decision_hits else f"decision words: {', '.join(decision_hits)}",
    )

    cited_facts = [match.group(1) for match in _SLOT.finditer(template)]
    cited_steps = sorted(set(_STEP_ID.findall(template))) or sorted(
        {facts[key].step_id for key in cited_facts if key in facts}
    )
    causal = bool(_CAUSAL.search(stripped))
    if strict_citations and causal:
        check(
            "citations",
            bool(cited_steps),
            "causal sentences cite a step id" if cited_steps else "a causal claim needs a step id such as (s3)",
        )
    elif strict_citations:
        checks.append(
            {
                "check": "citations",
                "status": "pass",
                "detail": "no causal claim; no citation required",
            }
        )
    else:
        checks.append(
            {
                "check": "citations",
                "status": "pass" if cited_steps else "warn",
                "detail": f"cites {', '.join(cited_steps[:4])}" if cited_steps else "deterministic template; step links come from the fact table",
            }
        )

    net_values = [facts[key].value for key in cited_facts if key in facts and facts[key].unit != "units"]
    if net_values and all(value < ZERO for value in net_values):
        optimistic = sorted({match.group(1).lower() for match in _OPTIMISTIC.finditer(stripped)})
        check(
            "polarity",
            not optimistic,
            "negative net facts are described without optimistic language" if not optimistic else f"optimistic words on negative facts: {', '.join(optimistic)}",
            hard=False,
        )
    else:
        checks.append({"check": "polarity", "status": "pass", "detail": "cited net facts are not uniformly negative"})

    word_count = _words(stripped)
    check(
        "length",
        word_count <= MAX_WORDS,
        f"{word_count} words, limit {MAX_WORDS}" if word_count <= MAX_WORDS else f"{word_count} words exceeds {MAX_WORDS}",
        hard=False,
    )

    hard_failures = [item for item in checks if item["status"] == "fail"]
    return NarrationValidation(ok=not hard_failures, checks=checks, issues=issues)


def truncate(text: str, limit: int = MAX_WORDS) -> str:
    """Truncate at a sentence boundary, then at a word boundary."""
    if _words(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    for sentence in sentences:
        candidate = " ".join(kept + [sentence])
        if _words(candidate) > limit:
            break
        kept.append(sentence)
    if kept:
        return " ".join(kept)
    words = text.split()
    return " ".join(words[:limit]).rstrip(",;:") + "."


def fallback_template(result: CalcResult, intent: str = "") -> str:
    """A prose-safe rationale that cites only facts and registered rules."""
    facts = result.facts
    if not facts:
        return "The engine completed the analysis. Every step, rule, and source is listed in the trace."
    headline = facts[0]
    rule = result.steps[0].rule_id if result.steps else "registered rule"
    label = intent.replace("_", " ").strip() or "analysis"
    second = f" The result follows {rule} and the full step chain is in the audit trace."
    if len(facts) > 1:
        second = (
            f" The report also records {facts[1].label.lower()} at "
            f"{{{{{facts[1].fact_id}}}}}. The result follows {rule} and the full step chain is in the audit trace."
        )
    return (
        f"The {label} headline figure is {headline.label.lower()} at "
        f"{{{{{headline.fact_id}}}}}.{second}"
    )


def render_validated(
    template: str,
    result: CalcResult,
    *,
    intent: str = "",
    regenerate: Callable[[NarrationValidation], str] | None = None,
    strict_citations: bool = False,
) -> dict[str, Any]:
    """Validate, allow one regeneration, then ship deterministic prose."""
    attempts: list[dict[str, Any]] = []
    candidate = template or fallback_template(result, intent)
    report = validate(candidate, result, strict_citations=strict_citations)
    attempts.append({"template": candidate, "validation": report.to_dict()})
    fallback_used = False
    if not report.ok and regenerate is not None:
        candidate = regenerate(report) or ""
        if candidate:
            report = validate(candidate, result, strict_citations=strict_citations)
            attempts.append({"template": candidate, "validation": report.to_dict()})
    if not report.ok:
        fallback_used = True
        candidate = fallback_template(result, intent)
        report = validate(candidate, result, strict_citations=False)
        attempts.append({"template": candidate, "validation": report.to_dict(), "fallback": True})
    text = truncate(render(candidate, result))
    return {
        "text": text,
        "template": candidate,
        "fallback_used": fallback_used,
        "validation": report.to_dict(),
        "attempts": attempts,
    }
