"""Authorization guard for generated KQL, with non-blocking syntax advisories.

The guard protects user scope, approved data sources and read-only access.
Query style, temporal-expression recognition and result shape are not security
decisions: ADX compiles the LLM's query and the existing repair route handles
server errors. No dataset-specific query templates are imposed here.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Dict, List, Tuple

from .semantic_resources import (
    ACTIVITY_DAILY,
    ACTIVITY_HOURLY,
    ACTIVITY_INTERVAL,
    DISALLOWED_PHYSICAL_NAMES,
    HEALTH_SAMPLES,
    SENSEI_ADX_V1,
    SLEEP_NIGHTLY,
    WITHINGS_RESOURCE_IDS,
    WORKOUT_SESSIONS,
    active_data_profile,
    physical_name,
)

# Default-profile compatibility constants for existing imports and tests.
# Runtime checks below always derive their sets from ``active_data_profile``.
WITHINGS_TABLES = set(SENSEI_ADX_V1.physical_names(optional=True))
PHONE_TABLES = set(SENSEI_ADX_V1.physical_names(scope_column="UserId"))
TELEGRAM_TABLES = set()
DISALLOWED_TABLES = set(DISALLOWED_PHYSICAL_NAMES)
ALLOWED_TABLES = PHONE_TABLES | WITHINGS_TABLES | TELEGRAM_TABLES

_SENSOR_CONTRACT_RESOURCES = (
    ACTIVITY_DAILY,
    ACTIVITY_HOURLY,
    ACTIVITY_INTERVAL,
    HEALTH_SAMPLES,
    SLEEP_NIGHTLY,
    WORKOUT_SESSIONS,
)
_MAX_QUERY_WINDOW_DAYS = 3660

TIME_COLUMNS = {
    "DateLocal",
    "SleepDateLocal",
    "SleepDay",
    "OnsetLocal",
    "WakeLocal",
    "WorkoutStartLocal",
    "WorkoutEndLocal",
    "SleepStartLocal",
    "SleepEndLocal",
    "ChunkStartLocal",
    "ChunkEndLocal",
    "SampleLocal",
    "SampleMinuteLocal",
    "TimestampLocal",
    "StartLocal",
    "EndLocal",
    "DateFrom",
    "DateTo",
    "LocalDate",
    "HourStartLocal",
    "EventTimeUtc",
    "StartLocal",
    "EndLocal",
    "EndExclusiveLocal",
    "StartUtc",
    "EndUtc",
    "EndExclusiveUtc",
    "IngestedAt",
    "IngestionTime",
}

DANGEROUS_MARKERS = {
    ".drop",
    ".delete",
    ".alter",
    ".create",
    ".set",
    ".ingest",
    ".clear",
    ".rename",
    ".execute",
    ".update",
    ".export",
    ".execute database script",
    "set-or-replace",
    "set-or-append",
    "delete from",
    "drop table",
    "alter table",
    "create table",
    "ingest into",
}

EXTERNAL_ACCESS_MARKERS = {
    "externaldata",
    "external_table",
    "http://",
    "https://",
    "hdfs://",
    "ftp://",
    "sql_request",
    "mysql_request",
    "postgresql_request",
    "cosmosdb_sql_request",
    "evaluate python",
}

INVALID_FORMAT_DATETIME_RE = re.compile(
    r"format_datetime\(\s*(?P<expr>.*?)\s*,\s*(?P<quote>['\"])[oO](?P=quote)\s*\)",
    flags=re.IGNORECASE | re.DOTALL,
)

PROJECT_DATE_ALIAS_RE = re.compile(
    r"(?im)(?P<prefix>\|\s*project(?:-[\w]+)?\s+(?:[^\n|;]*?,\s*)?)date\s*="
)
PROJECT_DATE_CONTINUATION_RE = re.compile(r"(?im)^(?P<prefix>\s*)date\s*=")
ORDER_BY_DATE_RE = re.compile(
    r"(?im)(?P<prefix>\|\s*(?:order|sort)\s+by\s+)\bdate\b(?P<suffix>\s+(?:asc|desc)\b)?"
)
SUMMARIZE_BY_DATE_RE = re.compile(
    r"(?im)(?P<prefix>\|\s*summarize[^\n|;]*\s+by\s+)\bdate\b(?P<suffix>\s*(?:,|$))"
)


def _legacy_sources_enabled(requested: bool) -> bool:
    return bool(requested) and str(
        os.environ.get("CHATBOT_ENABLE_OPTIONAL_WITHINGS_TABLES", "false")
    ).strip().lower() in {"1", "true", "yes", "on"}


def _active_contract_functions() -> frozenset[str]:
    # Conversational D1 KQL may read only the six shared Sensor-contract
    # functions.  Optional vendor, conversation, and internal MV bindings stay
    # available to trusted application jobs but are not query-generator
    # capabilities.
    profile = active_data_profile()
    return frozenset(profile.physical_name(item) for item in _SENSOR_CONTRACT_RESOURCES)


def _active_allowed_tables(*, allow_legacy_sources: bool = False) -> frozenset[str]:
    names = set(_active_contract_functions())
    if _legacy_sources_enabled(allow_legacy_sources):
        profile = active_data_profile()
        names.update(profile.physical_name(item) for item in WITHINGS_RESOURCE_IDS)
    return frozenset(names)


def _active_scope_tables(
    scope_column: str,
    *,
    allow_legacy_sources: bool = False,
) -> frozenset[str]:
    profile = active_data_profile()
    return frozenset(
        profile.physical_name(item)
        for item in (
            _SENSOR_CONTRACT_RESOURCES
            + (
                tuple(WITHINGS_RESOURCE_IDS)
                if _legacy_sources_enabled(allow_legacy_sources)
                else ()
            )
        )
        if profile.binding(item).scope_column == scope_column
    )


def _binding_scope_column(table: str) -> str:
    sought = str(table or "").casefold()
    for binding in active_data_profile().bindings.values():
        if binding.physical_name.casefold() == sought:
            return binding.scope_column
    return ""


def _table_uses_app_user_id(table: str) -> bool:
    return _binding_scope_column(table) == "AppUserId"


def _table_uses_mobile_user_id(table: str) -> bool:
    return _binding_scope_column(table) == "UserId"


def _normalized(kql: str) -> str:
    return re.sub(r"\s+", " ", str(kql or "")).strip()


def _deterministic_repair(kql: str) -> Tuple[str, List[str]]:
    """Repair known deterministic ADX failure patterns before execution."""

    repaired = str(kql or "")
    repairs: List[str] = []

    def replace_format_datetime(match: re.Match[str]) -> str:
        expr = match.group("expr").strip()
        return f"tostring({expr})"

    next_repaired, count = INVALID_FORMAT_DATETIME_RE.subn(replace_format_datetime, repaired)
    if count:
        repaired = next_repaired
        repairs.append("replaced unsupported ISO-style format_datetime with tostring(...)")

    code = _mask_kql_literals_and_comments(repaired)
    if _has_unsafe_date_alias(code):
        # Renaming only a declaration leaves distinct/join/project references
        # dangling. Rename the lexical symbol consistently, not question logic.
        # JSON property paths and quoted text are payload data, not this alias.
        alias, suffix = "metric_date", 2
        while re.search(r"\b" + alias + r"\b", code, re.I):
            alias, suffix = f"metric_date_{suffix}", suffix + 1
        matches = []
        for match in re.finditer(r"\bdate\b", code, re.I):
            prefix = code[:match.start()].rstrip()
            if prefix.endswith(".") and not re.search(r"\$(?:left|right)\s*\.$", prefix, re.I):
                continue
            matches.append(match)
        for match in reversed(matches):
            repaired = repaired[:match.start()] + alias + repaired[match.end():]
        repairs.append(f"renamed date alias and dependent references -> {alias}")

    return repaired.strip(), repairs


def _split_statements(kql: str) -> List[str]:
    # Semicolons inside quoted values or comments are data, not statement
    # boundaries.  The masked text preserves character positions, allowing the
    # original statement text to be sliced without trusting comment contents.
    text = str(kql or "")
    masked = _mask_kql_literals_and_comments(text)
    parts: List[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(masked):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0:
            part = text[start:index].strip()
            if part:
                parts.append(part)
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _has_multiple_unrelated_statements(kql: str) -> bool:
    statements = _split_statements(kql)
    if len(statements) <= 1:
        return False
    def is_binding(statement: str) -> bool:
        # Comments before a let binding are whitespace, not a second query.
        # Keep the actual statements intact for all subsequent safety checks.
        code = _mask_kql_literals_and_comments(statement).lstrip().lower()
        return code.startswith(("let ", "declare query_parameters"))
    non_let = [
        statement
        for statement in statements
        if not is_binding(statement)
    ]
    if len(non_let) > 1:
        return True
    if statements and statements[-1] not in non_let:
        return True
    return any(not is_binding(statement) for statement in statements[:-1])


def _uses_table(kql: str, table: str) -> bool:
    # Diagnostic fields such as SourceTable="SensorHealthSamples" and prose in
    # comments are not executable tabular references.  Mask them before the
    # broad presence checks so they cannot either spoof an allowlisted source or
    # trigger a false backing-table rejection.
    code = _mask_kql_literals_and_comments(kql)
    return re.search(rf"\b{re.escape(table)}\b", code, flags=re.IGNORECASE) is not None


def _mask_kql_literals_and_comments(kql: str, *, mask_literals: bool = True) -> str:
    """Hide text that cannot name a tabular source while preserving layout."""

    pattern = re.compile(
        r"/\*.*?\*/|//[^\r\n]*|\"(?:\\.|\"\"|[^\"])*\"|'(?:\\.|''|[^'])*'",
        flags=re.DOTALL,
    )
    return pattern.sub(
        lambda match: " " * len(match.group(0))
        if mask_literals or match.group(0).startswith(("//", "/*"))
        else match.group(0),
        str(kql or ""),
    )


def _leading_identifier(expression: str) -> str:
    text = str(expression or "").lstrip()
    while text.startswith("("):
        text = text[1:].lstrip()
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", text)
    return match.group(1) if match else ""


def _declared_let_names(kql: str) -> set[str]:
    names: set[str] = set()
    for statement in _split_statements(_mask_kql_literals_and_comments(kql)):
        match = re.match(
            r"(?is)^\s*let\s+([A-Za-z_][A-Za-z0-9_]*)\s*=",
            statement,
        )
        if match:
            names.add(match.group(1).lower())
    return names


def _direct_tabular_sources(kql: str) -> set[str]:
    """Return identifiers used in direct table/function/alias positions.

    This is intentionally narrower than extracting every KQL identifier: column
    names and scalar functions are not data sources.  The covered positions are
    statement roots, tabular ``let`` right-hand sides, and union/join operands.
    """

    code = _mask_kql_literals_and_comments(kql)
    sources: set[str] = set()
    source_keywords = {
        "print",
        "range",
        "datatable",
        "union",
        "materialize",
        "toscalar",
    }

    for statement in _split_statements(code):
        expression = statement
        let_match = re.match(
            r"(?is)^\s*let\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.*)$",
            statement,
        )
        if let_match:
            expression = let_match.group(1)
        elif statement.lstrip().lower().startswith("declare query_parameters"):
            continue

        unwrapped_expression = _strip_wrapping_parentheses(expression)
        name = _leading_identifier(unwrapped_expression)
        if name and name.lower() not in source_keywords:
            stripped = str(unwrapped_expression or "").lstrip()
            tail = stripped[len(name) :].lstrip()
            # A scalar let such as ``let EndLocal=StartLocal+1d`` is not a
            # tabular reference. Table names, stored functions, and aliases are
            # followed by a pipeline, a call, or the end of the expression.
            piped_call = bool(
                tail.startswith("(") and re.search(r"\)\s*\|", tail)
            )
            if not let_match or not tail or tail.startswith("|") or piped_call:
                sources.add(name)

    # Scalar wrappers can contain a tabular expression at arbitrary pipeline
    # positions. Inspect their balanced argument even when it has no pipe.
    for wrapper in ("materialize", "toscalar"):
        for arguments in _all_tabular_function_arguments(kql, wrapper):
            if len(arguments) != 1:
                continue
            inner = _strip_wrapping_parentheses(arguments[0])
            name = _leading_identifier(inner)
            if name and name.lower() not in source_keywords | {"count"}:
                sources.add(name)

    for union_match in re.finditer(r"(?is)\bunion\b([^|;]*)", code):
        body = union_match.group(1)
        for operand in _split_top_level(body, ","):
            cleaned = operand
            while True:
                option = re.match(
                    r"(?is)^\s*(?:kind|withsource|isfuzzy)\s*=\s*(?:[A-Za-z_][A-Za-z0-9_]*|\([^)]*\))\s*",
                    cleaned,
                )
                if not option:
                    break
                cleaned = cleaned[option.end() :]
            name = _leading_identifier(cleaned)
            if name:
                sources.add(name)

    for relation_match in re.finditer(
        r"(?is)\b(?:join|lookup)\b(?:\s+kind\s*=\s*[A-Za-z_][A-Za-z0-9_]*)?\s*\(?\s*([A-Za-z_][A-Za-z0-9_]*)",
        code,
    ):
        sources.add(relation_match.group(1))

    for nested_match in re.finditer(
        r"(?is)\b(?:materialize|toscalar)\s*\(\s*\(*\s*([A-Za-z_][A-Za-z0-9_]*)\s*\|",
        code,
    ):
        sources.add(nested_match.group(1))

    return sources


def _unknown_direct_tabular_sources(
    kql: str,
    *,
    allow_legacy_sources: bool = False,
) -> List[str]:
    allowed = {
        table.lower()
        for table in _active_allowed_tables(
            allow_legacy_sources=allow_legacy_sources
        )
    }
    declared = _declared_let_names(kql)
    unknown = {
        source
        for source in _direct_tabular_sources(kql)
        if source.lower() not in allowed and source.lower() not in declared
    }
    return sorted(unknown, key=str.lower)


def _all_tabular_function_arguments(kql: str, function_name: str) -> List[List[str]]:
    """Return the arguments for every executable occurrence of a function.

    Unlike the direct-branch helper, this balanced scan also sees calls nested
    inside ``toscalar``/``materialize``.  Strings and comments are masked, so a
    diagnostic label cannot manufacture an apparently safe source call.
    """

    text = str(kql or "")
    masked = _mask_kql_literals_and_comments(text)
    calls: List[List[str]] = []
    pattern = re.compile(rf"(?i)\b{re.escape(function_name)}\s*\(")
    for match in pattern.finditer(masked):
        opening = masked.find("(", match.start(), match.end())
        if opening < 0:
            calls.append([])
            continue
        depth = 0
        closing = -1
        for index in range(opening, len(masked)):
            char = masked[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing < 0:
            calls.append([])
            continue
        calls.append(_split_top_level(text[opening + 1 : closing], ","))
    return calls


def _split_top_level(text: str, delimiter: str) -> List[str]:
    """Split KQL at a delimiter that is outside parentheses/brackets/braces."""

    code = _mask_kql_literals_and_comments(text)
    parts: List[str] = []
    start = 0
    depth = 0
    opening = {"(", "[", "{"}
    closing = {")", "]", "}"}
    for index, char in enumerate(code):
        if char in opening:
            depth += 1
        elif char in closing:
            depth = max(0, depth - 1)
        elif char == delimiter and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _tabular_function_arguments(expression: str, function_name: str) -> List[str]:
    """Return top-level arguments from a direct stored-function source."""

    text = str(expression or "").lstrip()
    # A parenthesized function pipeline is still a direct source, for example
    # ``(SensorSleepNights(...) | where ...) | summarize ...``.
    while text.startswith("("):
        text = text[1:].lstrip()
    match = re.match(
        rf"(?is)^\s*{re.escape(function_name)}\s*\(",
        text,
    )
    if not match:
        return []
    opening = match.end() - 1
    masked = _mask_kql_literals_and_comments(text)
    depth = 0
    for index in range(opening, len(masked)):
        char = masked[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return _split_top_level(text[opening + 1 : index], ",")
    return []


_SIMPLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_TIME_IDENTIFIERS = frozenset(
    {
        "ago",
        "datetime",
        "datetime_add",
        "datetime_utc_to_local",
        "dayofweek",
        "endofday",
        "now",
        "startofday",
        "startofmonth",
        "startofweek",
        "toint",
        # Kusto duration suffixes are found as identifiers by the deliberately
        # simple lexer below (for example the ``d`` in ``7d``).
        "d",
        "h",
        "m",
        "s",
    }
)


def _let_expressions(kql: str, variable: str) -> List[str]:
    """Return executable top-level assignments for one scalar/alias name."""

    results: List[str] = []
    for statement in _split_statements(kql):
        masked = _mask_kql_literals_and_comments(statement)
        match = re.match(
            rf"(?is)^\s*let\s+{re.escape(variable)}\s*=",
            masked,
        )
        if match:
            results.append(statement[match.end() :].strip())
    return results


def _user_literals(user_id: str) -> frozenset[str]:
    value = str(user_id)
    literals = {
            "'" + value.replace("'", "''") + "'",
            json.dumps(value, ensure_ascii=False),
        }
    # A canonical integer literal identifies this same numeric-string account.
    # It may still need an ADX type repair, but is not another-user access.
    # Never collapse leading zeros, decimals, signs or computed expressions.
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", value):
        literals.add(value)
    return frozenset(literals)


def _target_user_binding_is_exact(kql: str, user_id: str) -> bool:
    assignments = _let_expressions(kql, "TargetUserId")
    return bool(assignments) and all(
        _strip_wrapping_parentheses(item) in _user_literals(user_id)
        for item in assignments
    )


def _function_user_argument_is_scoped(
    argument: str,
    user_id: str,
    full_kql: str,
) -> bool:
    """Prove the account argument from a literal or unique literal alias chain."""

    value = _strip_wrapping_parentheses(str(argument or "").strip())
    seen: set[str] = set()
    for _ in range(16):
        if value in _user_literals(user_id):
            return True
        if not _SIMPLE_IDENTIFIER_RE.fullmatch(value) or value.casefold() in seen:
            return False
        seen.add(value.casefold())
        bindings = _let_expressions(full_kql, value)
        if bindings and all(_strip_wrapping_parentheses(item) in _user_literals(user_id)
                            for item in bindings):
            return True
        if len(bindings) != 1:
            return False
        value = _strip_wrapping_parentheses(bindings[0])
    return False


def _resolved_time_expression(
    argument: str,
    full_kql: str,
    seen: frozenset[str] = frozenset(),
) -> str | None:
    """Resolve and validate a compiler-style finite time-bound expression."""

    expression = _strip_wrapping_parentheses(str(argument or "").strip())
    if not expression or len(expression) > 768:
        return None
    if _SIMPLE_IDENTIFIER_RE.fullmatch(expression):
        key = expression.casefold()
        if key in seen:
            return None
        assignments = _let_expressions(full_kql, expression)
        if len(assignments) != 1:
            return None
        nested = _resolved_time_expression(
            assignments[0], full_kql, seen | frozenset({key})
        )
        return nested

    masked = _mask_kql_literals_and_comments(expression)
    if any(marker in masked for marker in (";", "|", "{", "}")):
        return None
    # ISO timestamps are datetime literals, not identifiers such as T00 or Z.
    for match in reversed(list(re.finditer(
        r"(?i)\bdatetime\s*\(\s*(\d{4}-\d{2}-\d{2}[^()]*)\)", masked
    ))):
        try:
            datetime.datetime.fromisoformat(match.group(1).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        begin, finish = match.span(1)
        masked = masked[:begin] + " " * (finish - begin) + masked[finish:]

    replacements = []
    for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", masked):
        identifier = match.group(0)
        folded = identifier.casefold()
        if folded in _ALLOWED_TIME_IDENTIFIERS:
            continue
        if folded in seen:
            return None
        assignments = _let_expressions(full_kql, identifier)
        if len(assignments) != 1:
            return None
        nested = _resolved_time_expression(
            assignments[0], full_kql, seen | frozenset({folded})
        )
        if nested is None:
            return None
        replacements.append((match.start(), match.end(), "(" + nested + ")"))

    # Expand bindings only for validation, never rewrite the generated KQL.
    # Otherwise duration limits can miss arithmetic hidden behind aliases.
    for begin, finish, value in reversed(replacements):
        expression = expression[:begin] + value + expression[finish:]
    if len(expression) > 768:
        return None
    masked = _mask_kql_literals_and_comments(expression)

    # Reject accidentally gigantic relative spans while retaining the bounded
    # windows produced by the v1/v2 compilers.
    unit_seconds = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    max_seconds = _MAX_QUERY_WINDOW_DAYS * 86400
    for amount, unit in re.findall(r"(?i)(\d+)\s*([dhms])\b", masked):
        if int(amount) * unit_seconds[unit.casefold()] > max_seconds:
            return None
    # datetime_add budgets are checked by unit in
    # _time_expression_budget_seconds, not by an arbitrary amount/name limit.
    for multiplier, duration, suffix in re.findall(
        r"(?i)(\d+)\s*\*\s*(\d+)\s*([dhms])\b", masked
    ):
        if (
            int(multiplier)
            * int(duration)
            * unit_seconds[suffix.casefold()]
            > max_seconds
        ):
            return None
    reverse_products = re.findall(
        r"(?i)(\d+)\s*([dhms])\s*\*\s*(\d+)", masked
    )
    for duration, suffix, multiplier in reverse_products:
        if int(duration) * int(multiplier) * unit_seconds[suffix.casefold()] > max_seconds:
            return None
    return expression


def _datetime_dates(expression: str) -> List[datetime.date]:
    dates: List[datetime.date] = []
    for value in re.findall(
        r"(?i)\bdatetime\(\s*(\d{4}-\d{2}-\d{2})(?:[T ][^)]*)?\s*\)",
        expression,
    ):
        try:
            dates.append(datetime.date.fromisoformat(value))
        except ValueError:
            return []
    return dates


def _time_expression_budget_seconds(expression: str) -> int | None:
    """Conservatively bound explicit duration arithmetic in one expression."""

    text = str(expression or "")
    unit_seconds = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    terms = [
        (int(amount), suffix.casefold())
        for amount, suffix in re.findall(r"(?i)(\d+)\s*([dhms])\b", text)
    ]
    budget = sum(amount * unit_seconds[suffix] for amount, suffix in terms)

    # Replace the single-duration contribution with its explicit numeric
    # product. Both operand orders occur in generated calendar expressions.
    products: List[Tuple[int, str, int]] = []
    products.extend(
        (int(duration), suffix.casefold(), int(multiplier))
        for multiplier, duration, suffix in re.findall(
            r"(?i)(\d+)\s*\*\s*(\d+)\s*([dhms])\b", text
        )
    )
    products.extend(
        (int(duration), suffix.casefold(), int(multiplier))
        for duration, suffix, multiplier in re.findall(
            r"(?i)(\d+)\s*([dhms])\s*\*\s*(\d+)", text
        )
    )
    for duration, suffix, multiplier in products:
        base = duration * unit_seconds[suffix]
        budget += base * multiplier - base

    # A dynamic multiplier is accepted only for the compiler's bounded
    # day-of-week expression. Other parenthesized arithmetic cannot be proven.
    if re.search(r"(?i)\)\s*\*\s*\d+\s*[dhms]\b", text) and not re.search(
        r"(?i)dayofweek\s*\(", text
    ):
        return None

    datetime_add_calls = list(re.finditer(r"(?i)\bdatetime_add\s*\(", text))
    recognized_adds = list(
        re.finditer(
            r"(?i)\bdatetime_add\s*\(\s*(['\"])(day|week|month)\1\s*,\s*[-+]?\s*(\d+)",
            text,
        )
    )
    if len(datetime_add_calls) != len(recognized_adds):
        return None
    add_days = {"day": 1, "week": 7, "month": 31}
    budget += sum(
        int(match.group(3)) * add_days[match.group(2).casefold()] * 86400
        for match in recognized_adds
    )
    return budget


def _function_time_arguments_are_bounded(
    start_argument: str,
    end_argument: str,
    full_kql: str,
) -> bool:
    start = _resolved_time_expression(start_argument, full_kql)
    end = _resolved_time_expression(end_argument, full_kql)
    if start is None or end is None:
        return False
    start_budget = _time_expression_budget_seconds(start)
    end_budget = _time_expression_budget_seconds(end)
    if start_budget is None or end_budget is None:
        return False
    if start_budget + end_budget > _MAX_QUERY_WINDOW_DAYS * 86400:
        return False
    start_dates = _datetime_dates(start)
    end_dates = _datetime_dates(end)
    if start_dates and end_dates:
        span = (end_dates[0] - start_dates[0]).days
        return 0 <= span <= _MAX_QUERY_WINDOW_DAYS + 1
    if start_dates and re.search(r"(?i)\bnow\s*\(", end):
        span = (datetime.datetime.now(datetime.timezone.utc).date() - start_dates[0]).days
        return 0 <= span <= _MAX_QUERY_WINDOW_DAYS + 1
    return True


def _where_clauses(kql: str) -> List[Tuple[str, str]]:
    text = str(kql or "")
    masked = _mask_kql_literals_and_comments(text)
    clauses: List[Tuple[str, str]] = []
    for match in re.finditer(
        r"(?is)(?:^|\|)\s*where\s+(?P<clause>[^|;]+)", masked
    ):
        span = match.span("clause")
        clauses.append((text[span[0] : span[1]], masked[span[0] : span[1]]))
    return clauses


def _strip_union_options(operand: str) -> str:
    cleaned = str(operand or "")
    while True:
        option = re.match(
            r"(?is)^\s*(?:kind|withsource|isfuzzy)\s*=\s*(?:[A-Za-z_][A-Za-z0-9_]*|\([^)]*\))\s*",
            cleaned,
        )
        if not option:
            return cleaned.strip()
        cleaned = cleaned[option.end() :]


def _strip_wrapping_parentheses(expression: str) -> str:
    text = str(expression or "").strip()
    while text.startswith("(") and text.endswith(")"):
        code = _mask_kql_literals_and_comments(text)
        depth = 0
        closing_index = -1
        for index, char in enumerate(code):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    closing_index = index
                    break
        if closing_index != len(text) - 1:
            break
        text = text[1:-1].strip()
    return text


def _branch_has_user_filter(
    branch: str,
    full_kql: str,
    column: str,
    user_id: str,
) -> bool:
    variable = "TargetAppUserId" if column == "AppUserId" else "TargetUserId"
    variable_is_exact = (
        variable == "TargetUserId"
        and _target_user_binding_is_exact(full_kql, user_id)
    )
    literal_pattern = "(?:" + "|".join(
        re.escape(value) for value in _user_literals(user_id)
    ) + ")"
    for raw_clause, masked_clause in _where_clauses(branch):
        # A scope equality inside an OR expression does not restrict the branch.
        # Requiring an AND-only where clause is conservative and keeps the
        # authorization rule auditable without evaluating arbitrary KQL logic.
        if re.search(r"(?i)\bor\b", masked_clause):
            continue
        if variable_is_exact and re.search(
            rf"(?i)\b{re.escape(column)}\s*==\s*{variable}\b",
            masked_clause,
        ):
            return True
        if re.search(
            rf"(?i)\b{re.escape(column)}\s*==\s*{literal_pattern}",
            raw_clause,
        ):
            return True
    return False


def _direct_source_branch_issues(
    kql: str,
    user_id: str,
    *,
    allow_legacy_sources: bool = False,
) -> Tuple[List[str], bool]:
    """Validate scope and time bounds on every direct allowlisted source branch."""

    allowed_by_name = {
        table.lower(): table
        for table in _active_allowed_tables(
            allow_legacy_sources=allow_legacy_sources
        )
    }
    declared = _declared_let_names(kql)
    issues: List[str] = []
    has_scope_violation = False

    def validate_branch(source_name: str, branch: str) -> None:
        nonlocal has_scope_violation
        canonical = allowed_by_name.get(source_name.lower(), source_name)
        if _table_uses_mobile_user_id(canonical):
            column = "UserId"
        elif _table_uses_app_user_id(canonical):
            column = "AppUserId"
        else:
            return
        # The canonical function's account argument is checked independently
        # for EVERY call. Its output is already scoped; a second where clause is
        # not needed to authorize the same read. Legacy direct tables still need
        # their own explicit account predicate.
        is_scoped_function = canonical in _active_contract_functions()
        if not is_scoped_function and not _branch_has_user_filter(branch, kql, column, user_id):
            issues.append(f"KQL source branch {canonical} is missing its {column} filter.")
            has_scope_violation = True
        if not _has_bounded_time_range(branch, full_kql=kql):
            issues.append(f"KQL source branch {canonical} is missing a bounded time range.")

    def audit_expression(expression: str, depth: int = 0) -> None:
        if depth > 8:
            return
        expression = _strip_wrapping_parentheses(expression)
        segments = _split_top_level(expression, "|")
        if not segments:
            return

        root = segments[0]
        root_name = _leading_identifier(root)
        root_lower = root_name.lower()
        if root_lower in allowed_by_name and root_lower not in declared:
            branch_parts = [root]
            for segment in segments[1:]:
                if re.match(r"(?is)^\s*(?:union|join|lookup)\b", segment):
                    break
                branch_parts.append(segment)
            validate_branch(root_name, " | ".join(branch_parts))

        for segment in segments:
            union_match = re.match(r"(?is)^\s*union\b(.*)$", segment)
            if union_match:
                for operand in _split_top_level(union_match.group(1), ","):
                    candidate = _strip_union_options(operand)
                    candidate_name = _leading_identifier(candidate).lower()
                    if candidate_name in allowed_by_name and candidate_name not in declared:
                        audit_expression(candidate, depth + 1)
                continue

            relation_match = re.match(
                r"(?is)^\s*(?:join|lookup)\b(?:\s+kind\s*=\s*[A-Za-z_][A-Za-z0-9_]*)?\s*(.*)$",
                segment,
            )
            if not relation_match:
                continue
            candidate = relation_match.group(1).strip()
            if candidate.startswith("("):
                candidate = candidate[1:]
                closing_index = candidate.rfind(")")
                if closing_index >= 0:
                    candidate = candidate[:closing_index]
            else:
                candidate = re.split(r"(?i)\s+on\s+", candidate, maxsplit=1)[0]
            candidate_name = _leading_identifier(candidate).lower()
            if candidate_name in allowed_by_name and candidate_name not in declared:
                audit_expression(candidate, depth + 1)

    for statement in _split_statements(kql):
        let_match = re.match(
            r"(?is)^\s*let\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.*)$",
            statement,
        )
        if let_match:
            audit_expression(let_match.group(1))
        elif not statement.lstrip().lower().startswith("declare query_parameters"):
            audit_expression(statement)

    return list(dict.fromkeys(issues)), has_scope_violation


def _canonical_function_call_issues(
    kql: str,
    user_id: str,
) -> Tuple[List[str], bool]:
    """Validate every canonical call, including calls nested in scalar helpers."""

    issues: List[str] = []
    dangerous = False
    for function_name in sorted(_active_contract_functions(), key=str.casefold):
        for arguments in _all_tabular_function_arguments(kql, function_name):
            if len(arguments) != 3:
                issues.append(
                    f"KQL source {function_name} must be called with exactly "
                    "three user/start/end arguments."
                )
                # An incomplete call cannot execute, but arity is a compiler
                # error. A replacement still has to pass all scope checks.
                if arguments and not _function_user_argument_is_scoped(arguments[0], user_id, kql):
                    issues.append(
                        f"KQL source {function_name} does not use the authenticated "
                        "TargetUserId scope."
                    )
                    dangerous = True
                continue
            if not _function_user_argument_is_scoped(
                arguments[0], user_id, kql
            ):
                issues.append(
                    f"KQL source {function_name} does not use the authenticated "
                    "TargetUserId scope."
                )
                dangerous = True
            if not _function_time_arguments_are_bounded(
                arguments[1], arguments[2], kql
            ):
                issues.append(
                    f"KQL source {function_name} does not use trusted finite "
                    "start/end bounds."
                )
                dangerous = True
    return list(dict.fromkeys(issues)), dangerous


def _uses_unsupported_source_syntax(kql: str) -> bool:
    """Reject opaque data sources, not arrays or quoted payload/column keys."""

    text = str(kql or "")
    code = _mask_kql_literals_and_comments(text)
    source_text = _mask_kql_literals_and_comments(text, mask_literals=False)
    # Inspect bracketed identifiers ONLY in tabular-source positions. Looking
    # for [' or [" anywhere also rejects dynamic(["label"]) and Value["key"].
    def quoted_source(expression: str, depth: int = 0) -> bool:
        if depth > 16:
            return True
        segments = _split_top_level(_strip_wrapping_parentheses(expression), "|")
        if not segments:
            return False
        if re.match(r"(?is)^\s*\[\s*['\"]", segments[0]):
            return True
        for segment in segments:
            union = re.match(r"(?is)^\s*union\b(.*)$", segment)
            if union:
                if any(quoted_source(_strip_union_options(operand), depth + 1)
                       for operand in _split_top_level(union.group(1), ",")):
                    return True
            relation = re.match(
                r"(?is)^\s*(?:join|lookup)\b"
                r"(?:\s+kind\s*=\s*[A-Za-z_][A-Za-z0-9_]*)?\s*(.*)$",
                segment,
            )
            if relation:
                operand = relation.group(1).strip()
                if operand.startswith("("):
                    closing = operand.rfind(")")
                    operand = operand[1:closing] if closing >= 0 else operand[1:]
                else:
                    operand = re.split(r"(?i)\s+on\s+", operand, maxsplit=1)[0]
                if quoted_source(operand, depth + 1):
                    return True
        return False

    for statement in _split_statements(source_text):
        binding = re.match(
            r"(?is)^\s*let\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.*)$",
            statement,
        )
        expression = binding.group(1) if binding else statement
        if quoted_source(expression):
            return True
    for wrapper in ("materialize", "toscalar"):
        for arguments in _all_tabular_function_arguments(source_text, wrapper):
            if any(quoted_source(argument) for argument in arguments):
                return True
    # Generated conversational KQL has no need to define tabular lambdas or to
    # resolve a source dynamically by string/database/cluster name.
    if re.search(
        r"(?is)\blet\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*\([^;{}]*\)\s*\{",
        code,
    ):
        return True
    if re.search(
        r"(?is)\blet\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*view\s*\([^;{}]*\)\s*\{",
        code,
    ):
        return True
    if re.search(
        r"(?is)\b(?:cluster|database|table|materialized_view|entity_group)\s*\(",
        code,
    ):
        return True
    if re.search(r"(?im)(?:^|[;|])\s*(?:find|search|macro-expand|invoke)\b", code):
        return True
    return False


def _has_user_filter(kql: str, column: str, user_id: str) -> bool:
    return _branch_has_user_filter(kql, kql, column, user_id)


def _has_any_known_table(
    kql: str,
    *,
    allow_legacy_sources: bool = False,
) -> bool:
    if any(
        _all_tabular_function_arguments(kql, function_name)
        for function_name in _active_contract_functions()
    ):
        return True
    if not _legacy_sources_enabled(allow_legacy_sources):
        return False
    legacy_names = _active_allowed_tables(allow_legacy_sources=True) - _active_contract_functions()
    return any(_uses_table(kql, table) for table in legacy_names)


def _uses_disallowed_table(kql: str) -> bool:
    return any(_uses_table(kql, table) for table in DISALLOWED_TABLES)


def _uses_wildcard_source_discovery(kql: str) -> bool:
    """Reject wildcard entity discovery such as ``union *`` or ``search *``.

    The direct-source extractor intentionally looks for identifiers.  A bare
    wildcard has no identifier, so it needs an explicit guard rather than being
    allowed to hide beside an otherwise valid Sensor-function branch.
    """

    code = _mask_kql_literals_and_comments(kql)
    return re.search(
        r"(?is)\b(?:union|search)\b(?:[^|;])*?(?:^|[\s,(])\*(?=$|[\s,);])",
        code,
    ) is not None


def _has_required_user_filters(
    kql: str,
    user_id: str,
    *,
    allow_legacy_sources: bool = False,
) -> bool:
    canonical_functions = _active_contract_functions()
    user_id_tables = _active_scope_tables(
        "UserId", allow_legacy_sources=allow_legacy_sources
    ) - canonical_functions
    app_user_id_tables = _active_scope_tables(
        "AppUserId", allow_legacy_sources=allow_legacy_sources
    ) - canonical_functions
    uses_phone = any(_uses_table(kql, table) for table in user_id_tables)
    uses_app_user_id = any(_uses_table(kql, table) for table in app_user_id_tables)
    if uses_phone and not _has_user_filter(kql, "UserId", user_id):
        return False
    if uses_app_user_id and not _has_user_filter(kql, "AppUserId", user_id):
        return False
    if uses_phone and not uses_app_user_id and re.search(r"\bAppUserId\b", kql, flags=re.IGNORECASE):
        return False
    if uses_app_user_id and not uses_phone and re.search(r"(?<!App)\bUserId\b", kql, flags=re.IGNORECASE):
        return False
    return uses_phone or uses_app_user_id or any(
        _all_tabular_function_arguments(kql, name) for name in canonical_functions
    )


def _conjunctive_time_atoms(expression: str) -> List[str]:
    """Extract mandatory predicates; an OR branch cannot prove a time bound."""
    text = _strip_wrapping_parentheses(expression)
    code = _mask_kql_literals_and_comments(text)
    depth = 0
    parts = []
    start = 0
    for match in re.finditer(r"[()\[\]{}]|\b(?:and|or)\b", code, re.I):
        token = match.group(0).lower()
        if token in ("(", "[", "{"):
            depth += 1
        elif token in (")", "]", "}"):
            depth -= 1
        elif depth == 0:
            if token == "or":
                return []
            parts.append(text[start:match.start()])
            start = match.end()
    if not parts:
        return [text] if text else []
    parts.append(text[start:])
    return [atom for part in parts for atom in _conjunctive_time_atoms(part)]


def _between_time_bounds(expression: str) -> List[str]:
    code = _mask_kql_literals_and_comments(expression)
    depth = 0
    splits = []
    for index, char in enumerate(code):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and code[index:index + 2] == "..":
            splits.append(index)
    if len(splits) != 1:
        return []
    index = splits[0]
    return [expression[:index].strip(), expression[index + 2:].strip()]


def _has_bounded_time_range(kql: str, *, full_kql: str | None = None) -> bool:
    """Prove actual finite bounds, independent of the LLM's variable names.

    Canonical Sensor functions already apply their three-argument query
    contract. All calls in the inspected branch must have trusted finite
    bounds. The caller separately checks every source branch and user scope.
    """
    context = kql if full_kql is None else full_kql
    canonical_names = _active_contract_functions()
    calls = [
        arguments
        for name in canonical_names
        for arguments in _all_tabular_function_arguments(kql, name)
    ]
    scoped_sources = {name.casefold() for name in canonical_names} | _declared_let_names(context)
    # A bounded nested scalar lookup must not authorize an unbounded outer
    # legacy/raw source. Only function-scoped data reads can use this proof.
    if calls and all(source.casefold() in scoped_sources for source in _direct_tabular_sources(kql)):
        return all(
            len(arguments) == 3
            and _function_time_arguments_are_bounded(arguments[1], arguments[2], context)
            for arguments in calls
        )

    columns = {column.casefold() for column in TIME_COLUMNS}
    lower = []
    upper = []
    for raw_clause, _masked in _where_clauses(kql):
        for atom in _conjunctive_time_atoms(raw_clause):
            between = re.fullmatch(r"(?is)([A-Za-z_][A-Za-z0-9_]*)\s+between\s*\((.*)\)", atom)
            if between and between.group(1).casefold() in columns:
                bounds = _between_time_bounds(between.group(2))
                if len(bounds) == 2 and _function_time_arguments_are_bounded(*bounds, context):
                    return True
                continue
            comparison = re.fullmatch(r"(?is)(.*?)\s*(>=|<=|>|<)\s*(.+)", atom)
            if not comparison:
                continue
            left, operator, right = comparison.groups()
            left = _strip_wrapping_parentheses(left)
            right = _strip_wrapping_parentheses(right)
            if left.casefold() in columns:
                value = right
            elif right.casefold() in columns:
                value = left
                operator = {">": "<", ">=": "<=", "<": ">", "<=": ">="}[operator]
            else:
                continue
            if _resolved_time_expression(value, context) is not None:
                (lower if operator.startswith(">") else upper).append(value)
    return any(
        _function_time_arguments_are_bounded(start, end, context)
        for start in lower for end in upper
    )


def _is_phone_sleep_only_result(kql: str) -> bool:
    """Recognize the row-bounded nightly stored-function result contract."""

    code = _mask_kql_literals_and_comments(kql)
    sleep_nightly = physical_name(SLEEP_NIGHTLY).lower()
    sleep_aliases: set[str] = set()
    final_expression = ""
    for statement in _split_statements(code):
        let_match = re.match(
            r"(?is)^\s*let\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$",
            statement,
        )
        if not let_match:
            if not statement.lstrip().lower().startswith("declare query_parameters"):
                final_expression = statement
            continue
        alias = let_match.group(1).lower()
        expression = let_match.group(2)
        source = _leading_identifier(expression).lower()
        if source == sleep_nightly or source in sleep_aliases:
            if re.search(r"(?i)\b(?:union|join|lookup)\b", expression) is None:
                sleep_aliases.add(alias)

    source = _leading_identifier(final_expression).lower()
    if source != sleep_nightly and source not in sleep_aliases:
        return False
    return re.search(r"(?i)\b(?:union|join|lookup)\b", final_expression) is None


def _has_result_bound_or_summary(kql: str) -> bool:
    """Prove that every reachable final result branch is row-bounded.

    A summary in an unused ``let`` must not authorize an unbounded final Sensor
    stream. Conversely, deterministic v2 queries finish with ``union OutputN``
    aliases whose own definitions contain the summaries, so those aliases are
    followed transitively.
    """

    code = _mask_kql_literals_and_comments(kql)
    definitions: Dict[str, str] = {}
    final_expression = ""
    for statement in _split_statements(code):
        match = re.match(
            r"(?is)^\s*let\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$",
            statement,
        )
        if match:
            definitions[match.group(1).casefold()] = match.group(2).strip()
        elif not statement.lstrip().lower().startswith("declare query_parameters"):
            final_expression = statement.strip()
    if not final_expression:
        return False

    bound_operator = re.compile(
        r"(?im)(?:^|\|)\s*(?:summarize\b|top\s+\d+\b|take\s+\d+\b|limit\s+\d+\b|print\b)"
    )
    sleep_name = physical_name(SLEEP_NIGHTLY).casefold()

    def bounded(expression: str, seen: frozenset[str] = frozenset()) -> bool:
        candidate = _strip_wrapping_parentheses(expression)
        if bound_operator.search(candidate):
            return True
        segments = _split_top_level(candidate, "|")
        root = segments[0].strip() if segments else candidate
        union_match = re.match(r"(?is)^union\b(.*)$", root)
        if union_match:
            operands = [
                _strip_union_options(item)
                for item in _split_top_level(union_match.group(1), ",")
            ]
            return bool(operands) and all(bounded(item, seen) for item in operands)
        name = _leading_identifier(root).casefold()
        if not name:
            return False
        if name == sleep_name and not re.search(
            r"(?i)\b(?:union|join|lookup)\b", candidate
        ):
            # The nightly contract emits at most one MAIN row per local day.
            return True
        if name in definitions and name not in seen:
            return bounded(definitions[name], seen | frozenset({name}))
        return False

    return bounded(final_expression)


def _has_invalid_multi_sort_top(kql: str) -> bool:
    return re.search(r"(?im)^\s*\|\s*top\s+\d+\s+by\s+[^\n;|]*,[^\n;|]*$", kql or "") is not None


def _has_invalid_format_datetime(kql: str) -> bool:
    return INVALID_FORMAT_DATETIME_RE.search(kql or "") is not None


def _has_unsafe_date_alias(kql: str) -> bool:
    text = _mask_kql_literals_and_comments(kql or "")
    return any(
        pattern.search(text)
        for pattern in [
            PROJECT_DATE_ALIAS_RE,
            PROJECT_DATE_CONTINUATION_RE,
            ORDER_BY_DATE_RE,
            SUMMARIZE_BY_DATE_RE,
        ]
    )


def _guard_decision(
    kql: str,
    user_id: str,
    *,
    allow_legacy_sources: bool = False,
) -> Dict[str, object]:
    issues: List[str] = []
    warnings: List[str] = []
    dangerous = False
    # Account-scope problems always block execution. Unlike writes, external
    # access or forbidden tables, they are usually a model slip (an unproven
    # user argument, a misbound TargetUserId), so the LLM may propose a
    # replacement; that replacement is authorized again from scratch.
    scope_violation = False
    executable = _mask_kql_literals_and_comments(kql)
    lowered = _normalized(executable).lower()

    if not kql or not kql.strip():
        issues.append("KQL is empty.")

    if not user_id:
        issues.append("Missing user_id for required data scoping.")
        dangerous = True

    if any(marker in lowered for marker in DANGEROUS_MARKERS):
        issues.append("KQL contains a management or write command.")
        dangerous = True

    if any(marker in lowered for marker in EXTERNAL_ACCESS_MARKERS):
        issues.append("KQL contains external data access.")
        dangerous = True

    if _has_multiple_unrelated_statements(kql):
        # Each non-let statement returns its own result table and only the
        # first is read, so the answer would come from the wrong table.
        issues.append("KQL contains more than one result statement; each returns a separate "
                      "table and only the first is read. Bind intermediate results with let "
                      "(let Name = ...;) and end with one final query.")

    target_bindings = _let_expressions(kql, "TargetUserId")
    if target_bindings and not _target_user_binding_is_exact(kql, user_id):
        issues.append(
            "KQL redefines or misbinds TargetUserId instead of using the "
            "authenticated user."
        )
        scope_violation = True

    if _has_invalid_multi_sort_top(kql):
        warnings.append(
            "KQL uses top N by with multiple sort expressions; use sort by ... then take N."
        )

    if _has_invalid_format_datetime(kql):
        warnings.append("KQL uses unsupported ISO-style format_datetime; use tostring(...).")

    if _has_unsafe_date_alias(kql):
        warnings.append("KQL uses alias date; ADX will validate this expression.")

    if not _has_any_known_table(
        kql, allow_legacy_sources=allow_legacy_sources
    ):
        issues.append("KQL does not call a canonical Sensor function.")

    if _uses_unsupported_source_syntax(kql):
        issues.append("KQL uses an unsupported or dynamically resolved tabular source.")
        dangerous = True

    unknown_sources = _unknown_direct_tabular_sources(
        kql, allow_legacy_sources=allow_legacy_sources
    )
    if unknown_sources:
        issues.append(
            "KQL uses a direct tabular source outside the health-chatbot allowlist: "
            + ", ".join(unknown_sources)
            + "."
        )
        # Unknown names still BLOCK execution. They can also be a misspelled
        # alias or malformed model output; let the existing LLM repair propose
        # a replacement and authorize it from scratch. Writes, external access
        # and an explicit account-scope violation remain non-repairable below.

    if _uses_disallowed_table(kql):
        issues.append("KQL uses a table outside the health-chatbot allowlist.")
        dangerous = True

    if _uses_wildcard_source_discovery(kql):
        issues.append("KQL uses wildcard tabular-source discovery.")
        dangerous = True

    call_issues, _ = _canonical_function_call_issues(kql, user_id)
    # A small local date parser is not the authority on valid KQL datetime
    # expressions. Keep call arity/user authorization, but defer date compilation
    # to ADX instead of labelling unrecognized expressions dangerous.
    scope_call_issues = []
    for issue in call_issues:
        if issue.endswith("does not use trusted finite start/end bounds."):
            warnings.append(issue)
        else:
            scope_call_issues.append(issue)
    issues.extend(scope_call_issues)
    if any(not issue.endswith("three user/start/end arguments.") for issue in scope_call_issues):
        scope_violation = True

    if user_id:
        branch_issues, has_scope_violation = _direct_source_branch_issues(
            kql,
            user_id,
            allow_legacy_sources=allow_legacy_sources,
        )
        for issue in branch_issues:
            if issue.endswith("is missing a bounded time range."):
                warnings.append(issue)
            else:
                issues.append(issue)
        if has_scope_violation:
            scope_violation = True

    if user_id and not _has_required_user_filters(
        kql,
        user_id,
        allow_legacy_sources=allow_legacy_sources,
    ):
        issues.append("KQL is missing the required user filter.")

    if not _has_bounded_time_range(kql):
        warnings.append("Local parser could not prove the KQL time range; ADX will validate it.")

    if not _has_result_bound_or_summary(kql):
        warnings.append("KQL has no locally recognized result bound or aggregation.")

    is_safe = not issues
    should_repair = bool(issues and not dangerous)
    return {
        "is_safe": is_safe,
        "reason": "safe" if is_safe else " ".join(issues),
        "should_repair": should_repair,
        "scope_violation": scope_violation,
        "issues": issues,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _repair_evidence_aggregate_scope(kql: str) -> Tuple[str, List[str]]:
    """Place misplaced evidence metadata in its immediate aggregate input.

    This does not choose a measurement, filter, time window or answer reduction.
    Only the result-contract EvidenceRows alias is handled. Unproven predicate
    dependencies and non-adjacent operators are left to the LLM compiler repair.
    """
    segments = _split_top_level(kql, "|")
    changes = []
    for i in range(1, len(segments)):
        previous = _mask_kql_literals_and_comments(segments[i - 1])
        current = _mask_kql_literals_and_comments(segments[i])
        if not re.match(r"\s*summarize\b", previous, re.I) or ";" in previous:
            continue
        operator = re.match(r"\s*(project|extend)\s+", current, re.I)
        if not operator:
            continue
        # Leave comment-sensitive layouts and custom scalar overrides alone.
        if any(token in segments[i - 1] + segments[i] for token in ("//", "/*")):
            continue
        if _let_expressions(kql, "count") or _let_expressions(kql, "countif"):
            continue
        aliases = set(re.findall(r"\b([A-Za-z_]\w*)\s*=", previous))
        input_names = set(re.findall(r"\b([A-Za-z_]\w*)\b(?!\s*\()", previous)) - aliases
        entries = _split_top_level(segments[i][operator.end():], ",")
        for j, entry in enumerate(entries):
            assigned = re.match(r"\s*EvidenceRows\s*=\s*(.+)\Z", entry, re.S)
            if not assigned:
                continue
            expression = _strip_wrapping_parentheses(assigned.group(1).strip())
            if re.match(r"toscalar\s*\(", expression, re.I):
                wrapped = _all_tabular_function_arguments(expression, "toscalar")
                if len(wrapped) != 1 or len(wrapped[0]) != 1:
                    continue
                expression = wrapped[0][0].strip()
            call = re.fullmatch(r"(count|countif)\s*\((.*)\)", expression, re.I | re.S)
            if not call:
                continue
            args = _all_tabular_function_arguments(expression, call.group(1))
            if len(args) != 1:
                continue
            if call.group(1).lower() == "count":
                if args[0]:
                    continue
            else:
                if len(args[0]) != 1:
                    continue
                predicate = _mask_kql_literals_and_comments(args[0][0])
                names = set(re.findall(r"\b([A-Za-z_]\w*)\b(?!\s*\()", predicate)) - {"true", "false", "and", "or", "not"}
                if names & aliases or not names <= input_names:
                    continue
            alias = "__SenseiEvidenceRows"
            while re.search(rf"\b{re.escape(alias)}\b", _mask_kql_literals_and_comments(kql)):
                alias += "_"
            # Add before by; grouping and the model's answer aggregates stay intact.
            by = re.search(r"\bby\b", previous, re.I)
            at = by.start() if by else len(segments[i - 1])
            segments[i - 1] = (segments[i - 1][:at].rstrip() + f", {alias}={expression} "
                               + segments[i - 1][at:])
            entries[j] = f"EvidenceRows = {alias}"
            segments[i] = segments[i][:operator.end()] + ", ".join(entries)
            changes.append("move misplaced EvidenceRows aggregate into adjacent summarize")
            break
    return ("\n| ".join(segments) if changes else kql), changes


def repair_query_deterministically(kql: str) -> Tuple[str, List[str]]:
    """Repair known ADX syntax issues without invoking an LLM."""

    repaired, changes = _deterministic_repair(kql)
    repaired, evidence_changes = _repair_evidence_aggregate_scope(repaired)
    return repaired, [*changes, *evidence_changes]


def inspect_query_safety(
    kql: str,
    user_id: str,
    *,
    allow_legacy_sources: bool = False,
) -> Dict[str, object]:
    """Block unauthorized sources/scope/writes; report style/time as warnings."""

    return _guard_decision(
        kql,
        user_id,
        allow_legacy_sources=allow_legacy_sources,
    )


__all__ = [
    "inspect_query_safety",
    "repair_query_deterministically",
]
