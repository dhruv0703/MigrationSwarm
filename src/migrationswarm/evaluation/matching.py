"""Deterministic service-name normalization and one-to-one matching."""

import re
from dataclasses import dataclass

from migrationswarm.evaluation.models import BoundaryMetrics

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_SERVICE_SUFFIXES = {"service", "services"}


def normalize_service_name(value: str) -> str:
    """Normalize separators, a trailing service suffix, and simple plurals."""
    tokens = _NON_ALPHANUMERIC.sub(" ", value.casefold()).split()
    if tokens and tokens[-1] in _SERVICE_SUFFIXES:
        tokens.pop()
    if tokens:
        last = tokens[-1]
        if last.endswith("ies") and len(last) > 4:
            tokens[-1] = f"{last[:-3]}y"
        elif last.endswith("s") and not last.endswith(("ss", "us", "is")) and len(last) > 3:
            tokens[-1] = last[:-1]
    return " ".join(tokens)


def _aliases_for(expected: str, aliases: dict[str, list[str]]) -> set[str]:
    values = {normalize_service_name(expected)}
    for key, alternatives in aliases.items():
        if normalize_service_name(key) == normalize_service_name(expected):
            values.update(normalize_service_name(item) for item in alternatives)
    return values


@dataclass(frozen=True)
class ServiceMatch:
    """One deterministic expected-to-predicted match."""

    expected: str
    predicted: str


def match_services(
    expected: list[str],
    predicted: list[str],
    aliases: dict[str, list[str]] | None = None,
) -> tuple[list[ServiceMatch], list[str], list[str]]:
    """Match each predicted service at most once in stable input order."""
    alias_map = aliases or {}
    unmatched = list(predicted)
    matches: list[ServiceMatch] = []
    missed: list[str] = []
    for expected_name in expected:
        acceptable = _aliases_for(expected_name, alias_map)
        index = next(
            (
                position
                for position, candidate in enumerate(unmatched)
                if normalize_service_name(candidate) in acceptable
            ),
            None,
        )
        if index is None:
            missed.append(expected_name)
            continue
        matches.append(ServiceMatch(expected_name, unmatched.pop(index)))
    return matches, missed, unmatched


def boundary_metrics(
    expected: list[str],
    predicted: list[str],
    aliases: dict[str, list[str]] | None = None,
) -> BoundaryMetrics:
    """Calculate standard precision, recall, and F1 without fake zeroes."""
    matches, missed, unexpected = match_services(expected, predicted, aliases)
    matched = len(matches)
    precision = matched / len(predicted) if predicted else None
    recall = matched / len(expected) if expected else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else None
    )
    return BoundaryMetrics(
        expected_service_count=len(expected),
        predicted_service_count=len(predicted),
        matched_service_count=matched,
        matched_services=[item.expected for item in matches],
        missed_services=missed,
        unexpected_services=unexpected,
        precision=precision,
        recall=recall,
        f1=f1,
    )
