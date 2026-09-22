from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mcp_doctor.models import ToolDefinition

WORD = re.compile(r"[A-Za-z0-9]+")
CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "information",
    "id",
    "is",
    "of",
    "on",
    "or",
    "record",
    "records",
    "result",
    "results",
    "return",
    "returns",
    "supplied",
    "that",
    "the",
    "this",
    "to",
    "tool",
    "use",
    "using",
    "when",
    "with",
}

ACTION_ALIASES = {
    "add": "create",
    "browse": "list",
    "change": "update",
    "create": "create",
    "delete": "delete",
    "edit": "update",
    "fetch": "get",
    "find": "search",
    "get": "get",
    "list": "list",
    "lookup": "search",
    "modify": "update",
    "patch": "update",
    "put": "update",
    "remove": "delete",
    "search": "search",
    "update": "update",
}

MUTATING_ACTIONS = {"create", "delete", "update"}

POSITIVE_GUIDANCE = re.compile(
    r"\b(?:use|choose|call)\s+(?:this(?:\s+tool)?\s+)?(?:when|for|to)\b"
    r"|\bbest\s+(?:used\s+)?(?:when|for)\b",
    re.I,
)
NEGATIVE_GUIDANCE = re.compile(
    r"\b(?:do\s+not|don't|never)\s+use\b"
    r"|\bnot\s+(?:intended\s+)?for\b"
    r"|\b(?:use|choose)\s+[A-Za-z0-9_-]+\s+instead\b"
    r"|\binstead\s+(?:use|choose)\b"
    r"|\bunlike\s+[A-Za-z0-9_-]+\b",
    re.I,
)


@dataclass(frozen=True)
class ToolSignals:
    name_terms: frozenset[str]
    description_terms: frozenset[str]
    parameter_terms: frozenset[str]
    action: str | None
    object_terms: frozenset[str]


@dataclass(frozen=True)
class PairSimilarity:
    left: str
    right: str
    score: float
    name_score: float
    description_score: float
    parameter_score: float
    matched_terms: tuple[str, ...]
    shared_parameters: tuple[str, ...]


def _singularize(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def text_terms(value: str) -> list[str]:
    separated = CAMEL_BOUNDARY.sub(r"\1 \2", value).replace("_", " ").replace("-", " ")
    terms = []
    for raw in WORD.findall(separated.casefold()):
        term = ACTION_ALIASES.get(raw, _singularize(raw))
        if term not in STOP_WORDS:
            terms.append(term)
    return terms


def _schema_parameter_terms(schema: dict[str, Any]) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    context: set[str] = set()
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return names, context
    for name, definition in properties.items():
        names.update(text_terms(str(name)))
        if isinstance(definition, dict):
            for key in ("title", "description"):
                value = definition.get(key)
                if isinstance(value, str):
                    context.update(text_terms(value))
    return names, context


def tool_signals(tool: ToolDefinition) -> ToolSignals:
    raw_name_terms = text_terms(tool.name)
    title_terms = text_terms(tool.title or "")
    action = (
        raw_name_terms[0]
        if raw_name_terms and raw_name_terms[0] in ACTION_ALIASES.values()
        else None
    )
    object_terms = raw_name_terms[1:] if action else raw_name_terms
    parameter_names, parameter_context = _schema_parameter_terms(tool.input_schema)
    return ToolSignals(
        name_terms=frozenset(raw_name_terms + title_terms),
        description_terms=frozenset(text_terms(tool.description or "")),
        parameter_terms=frozenset(parameter_names | parameter_context),
        action=action,
        object_terms=frozenset(object_terms),
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def compare_tools(left: ToolDefinition, right: ToolDefinition) -> PairSimilarity:
    left_signals = tool_signals(left)
    right_signals = tool_signals(right)
    name_score = _jaccard(left_signals.name_terms, right_signals.name_terms)
    description_score = _jaccard(left_signals.description_terms, right_signals.description_terms)
    parameter_score = _jaccard(left_signals.parameter_terms, right_signals.parameter_terms)
    score = 0.45 * name_score + 0.35 * description_score + 0.20 * parameter_score

    # Shared entity words do not make distinct state-changing operations interchangeable.
    if (
        left_signals.action is not None
        and right_signals.action is not None
        and left_signals.action != right_signals.action
        and {left_signals.action, right_signals.action} & MUTATING_ACTIONS
    ):
        score = 0.0

    matched = (left_signals.name_terms & right_signals.name_terms) | (
        left_signals.description_terms & right_signals.description_terms
    )
    shared_parameters = left_signals.parameter_terms & right_signals.parameter_terms
    left_name, right_name = sorted((left.name, right.name))
    return PairSimilarity(
        left=left_name,
        right=right_name,
        score=round(score, 3),
        name_score=round(name_score, 3),
        description_score=round(description_score, 3),
        parameter_score=round(parameter_score, 3),
        matched_terms=tuple(sorted(matched)),
        shared_parameters=tuple(sorted(shared_parameters)),
    )


def overlapping_pairs(tools: list[ToolDefinition], threshold: float) -> list[PairSimilarity]:
    pairs = []
    ordered = sorted(tools, key=lambda tool: tool.name)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            similarity = compare_tools(left, right)
            if similarity.score >= threshold:
                pairs.append(similarity)
    return pairs


def has_positive_guidance(description: str | None) -> bool:
    return bool(description and POSITIVE_GUIDANCE.search(description))


def has_negative_guidance(description: str | None) -> bool:
    return bool(description and NEGATIVE_GUIDANCE.search(description))


def pair_has_selection_boundary(left: ToolDefinition, right: ToolDefinition) -> bool:
    return (
        has_positive_guidance(left.description)
        and has_positive_guidance(right.description)
        and (has_negative_guidance(left.description) or has_negative_guidance(right.description))
    )
