"""Common classification of repairable Kusto query/compiler diagnostics."""

from __future__ import annotations

import re
from typing import Any


def is_query_validation_error(error: Any) -> bool:
    """Allow compiler repair, never an arbitrary unsuccessful service request."""
    text = str(error or "")
    global_failure_markers = (
        "authentication", "unauthorized", "forbidden", "credential", "certificate",
        "network", "connection", "timed out", "timeout", "temporarily unavailable",
        "too many requests", "429", "503",
    )
    if any(marker in text.lower() for marker in global_failure_markers):
        return False
    return bool(re.search(
        r"\b(?:syntax|semantic) error\b|\b(?:SYN|SEM)\d{4}\b|"
        r"\bfailed to resolve (?:scalar|tabular|column|table|function|attribute)\b",
        text, flags=re.IGNORECASE,
    ))


def extend_dependency_hints(query: str) -> list[str]:
    """List candidate extend/project sibling aliases for repair, never rewrite KQL.

    This is a conservative diagnostic, not a parser or proof: an identically
    named input column might already exist. Only use it alongside an actual
    compiler diagnostic. Strings/comments and nested expression commas are
    ignored so they cannot manufacture alias dependencies.
    """
    from chatbot.policies.kql_safety import _mask_kql_literals_and_comments
    code = _mask_kql_literals_and_comments(str(query or ""))
    hints = []
    for operator in re.finditer(r"\|\s*(extend|project)\b([^|;]*)", code, re.I):
        body = operator.group(2)
        entries, start, depth = [], 0, 0
        for position, char in enumerate(body):
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == "," and depth == 0:
                entries.append(body[start:position])
                start = position + 1
        entries.append(body[start:])
        assignments = []
        for entry in entries:
            match = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*", entry, re.S)
            if match:
                assignments.append((match.group(1), match.group(2)))
        siblings = {alias for alias, _ in assignments}
        for alias, expression in assignments:
            # Ignore function names and object property keys.
            tokens = {m.group(0) for m in re.finditer(r"(?<![.\w])[A-Za-z_]\w*(?![\w(])", expression)}
            references = (tokens & siblings) - {alias}
            if references:
                hints.append(f"{operator.group(1)}: {alias} references sibling aliases "
                             f"{', '.join(sorted(references))}")
    return hints
