"""Factual feedback when a query found nothing because it selected unobserved records.

This is not a rule about which measurement to use. It only compares the record
Types and activity labels a query filters on with what the bounded inventory
actually observed for this user and window. When a query returned no evidence
and every value it filtered on is absent from that inventory, the model gets
one chance to reconsider with that fact; the model alone decides whether an
observed record answers the question or the empty result stands.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

_LITERALS = r"((?:\s*(?:'[^']*'|\"[^\"]*\")\s*,?)+)"
_TYPE_FILTER = re.compile(r"\bType\s*(?:==|=~|in~?|has)\s*\(?" + _LITERALS, re.I)
_LABEL_FILTER = re.compile(r"\bActivity(?:Name|Code)\s*(?:==|=~|in~?|has|contains)\s*\(?" + _LITERALS, re.I)


def _values(match_text: str) -> set[str]:
    found = set()
    for single, double in re.findall(r"'([^']*)'|\"([^\"]*)\"", match_text):
        value = (single or double).strip().upper()
        if value:
            found.add(value)
    return found


def _referenced(pattern: re.Pattern, kql: str) -> set[str]:
    found: set[str] = set()
    for match in pattern.finditer(kql or ""):
        found |= _values(match.group(1))
    return found


def _observed(observations: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    types, labels = set(), set()
    for entry in observations.get("observations", []) or []:
        if entry.get("Type") and (entry.get("RowCount") or 0) > 0:
            types.add(str(entry["Type"]).upper())
        for key in ("ActivityNames", "ActivityCodes"):
            for label in entry.get(key) or []:
                labels.add(str(label).upper())
    return types, labels


def result_has_no_evidence(query_result: Any) -> bool:
    """True when the executed query returned no rows or only rows without evidence."""
    try:
        rows = json.loads(query_result) if isinstance(query_result, str) else query_result
    except ValueError:
        return False
    if isinstance(rows, dict):
        rows = rows.get("rows", [rows])
    if not isinstance(rows, list):
        return False
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return True
    for row in rows:
        evidence = row.get("EvidenceRows")
        if evidence not in (None, 0, 0.0) or (evidence is None and row.get("Value") is not None):
            return False
    return True


def unobserved_selection(kql: str, observations: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Return the selected Types/labels that have no records here, if all of them are absent."""
    if not isinstance(observations, Mapping) or observations.get("status") != "observed":
        return None
    observed_types, observed_labels = _observed(observations)
    finding = {}
    types = _referenced(_TYPE_FILTER, kql)
    if types and observed_types and not (types & observed_types):
        finding["types"] = sorted(types)
    labels = _referenced(_LABEL_FILTER, kql)
    if labels and observed_labels and not (labels & observed_labels):
        finding["labels"] = sorted(labels)
    if not finding:
        return None
    finding["observed_types"] = sorted(observed_types)
    finding["observed_labels"] = sorted(observed_labels)
    return finding


def feedback_message(finding: Mapping[str, Any]) -> str:
    parts = []
    if finding.get("types"):
        parts.append("record Types " + ", ".join(finding["types"]))
    if finding.get("labels"):
        parts.append("activity labels " + ", ".join(finding["labels"]))
    return ("The query ran but found no evidence: it selects " + " and ".join(parts)
            + ", which have no records for this user in this window. Observed Types: "
            + (", ".join(finding.get("observed_types") or []) or "none")
            + ". Observed activity labels: " + (", ".join(finding.get("observed_labels") or []) or "none")
            + ". If an observed Type or label records what the question asks about, use it and name it "
              "in ResultStatus. If none does, keep the empty result (no value; a count is 0).")


__all__ = ["result_has_no_evidence", "unobserved_selection", "feedback_message"]
