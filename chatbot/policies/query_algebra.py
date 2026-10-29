"""Typed, physical-schema-free computation plans for sensor questions.

The conversational planner needs to describe *what* should be computed without
being allowed to choose ADX tables, columns, joins, or KQL fragments.  This
module defines that small semantic boundary.  A v2 plan is represented in the
outer plan as::

    {
        "schema_version": 2,
        "computation": {
            "entity": "workout_session",
            "outputs": [
                {"metric": "workout_duration", "aggregation": "sum"}
            ],
            "filters": [
                {
                    "dimension": "activity_name",
                    "operator": "eq",
                    "value": "INTERVAL_WORKOUT",
                }
            ],
            "conditions": [],
            "selector": {"mode": "first", "n": 4},
        },
    }

Only semantic metric names from :mod:`domain_catalog` are accepted. The selected
query producer resolves evidence against the shared schema; the LLM path is
not limited to combinations implemented by the optional deterministic compiler.
A separate query guard validates executable KQL scope and read-only behavior.

``sanitize_computation`` accepts either the outer envelope shown above or the
direct value of its ``computation`` member.  It always returns the canonical
computation body; callers retain ``schema_version`` in the enclosing plan.
Explicit unknown or future schema versions are rejected rather than silently
being interpreted as the current schema.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .domain_catalog import METRIC_DEFINITIONS, PHONE_PROFILE_METRICS
from .metric_normalization import _normalize_metric_name
from .semantic_resources import ACTIVITY_DAILY, ACTIVITY_HOURLY, HEALTH_SAMPLES, SLEEP_NIGHTLY, WORKOUT_SESSIONS


SCHEMA_VERSION = 2
"""The only computation-plan schema version understood by this module."""

MAX_OUTPUTS = 8
MAX_FILTERS = 8
MAX_CONDITIONS = 8
MAX_GROUP_BY = 4
MAX_IN_VALUES = 20
MAX_SELECTOR_N = 100
MAX_LABEL_LENGTH = 64
MAX_DIMENSION_VALUE_LENGTH = 128
MAX_ABS_NUMERIC_VALUE = 1e15

ENTITY_ORDER: Tuple[str, ...] = (
    "day",
    "night",
    "workout_session",
    "sleep_session",
    "sample",
    "hour",
)
ALLOWED_ENTITIES = frozenset(ENTITY_ORDER)
ALLOWED_METRICS = frozenset(METRIC_DEFINITIONS)

AGGREGATION_ORDER: Tuple[str, ...] = (
    "none",
    "sum",
    "mean",
    "min",
    "max",
    "median",
    "stddev_sample",
    "stddev_population",
    "count",
    "count_distinct",
)
ALLOWED_AGGREGATIONS = frozenset(AGGREGATION_ORDER)

DIMENSION_ORDER: Tuple[str, ...] = (
    "activity_name",
    "source_name",
    "source_platform",
)
ALLOWED_DIMENSIONS = frozenset(DIMENSION_ORDER)
ALLOWED_DIMENSION_OPERATORS = frozenset({"eq", "ne", "in"})
ALLOWED_FILTER_OPERATORS = ALLOWED_DIMENSION_OPERATORS
ALLOWED_CONDITION_OPERATORS = frozenset(
    {"eq", "ne", "lt", "lte", "gt", "gte", "between"}
)
ALLOWED_NUMERIC_OPERATORS = ALLOWED_CONDITION_OPERATORS
ALLOWED_SELECTOR_MODES = frozenset({"first", "last", "top", "bottom"})

# A condition must use one of these canonical units.  ``native`` means the
# canonical unit declared for a metric by the deterministic compiler; it is
# useful where a question does not spell out a unit.  This remains a closed
# vocabulary and therefore cannot smuggle an expression into generated KQL.
UNIT_ORDER: Tuple[str, ...] = (
    "native",
    "count",
    "step",
    "percent",
    "second",
    "minute",
    "hour",
    "day",
    "millisecond",
    "bpm",
    "count_per_minute",
    "meter",
    "kilometer",
    "mile",
    "meter_per_second",
    "meter_per_minute",
    "kilometer_per_hour",
    "mile_per_hour",
    "calorie",
    "kilocalorie",
    "joule",
    "kilojoule",
    "gram",
    "kilogram",
    "pound",
    "celsius",
    "fahrenheit",
    "mmhg",
    "mg_per_dl",
    "mmol_per_l",
    "liter",
    "milliliter",
    "score",
)
ALLOWED_UNITS = frozenset(UNIT_ORDER)

# Grouping is deliberately limited to semantic entities and dimensions.  A
# raw sample is omitted because grouping by individual samples is both
# redundant and an easy way to create unbounded result sets.
GROUP_BY_ORDER: Tuple[str, ...] = (
    "day",
    "night",
    "workout_session",
    "sleep_session",
    "hour",
    "activity_name",
    "source_name",
    "source_platform",
)
ALLOWED_GROUP_BY = frozenset(GROUP_BY_ORDER)

_UNIT_ALIASES = {
    "default": "native",
    "canonical": "native",
    "unitless": "native",
    "counts": "count",
    "steps": "step",
    "%": "percent",
    "percentage": "percent",
    "percentages": "percent",
    "sec": "second",
    "secs": "second",
    "seconds": "second",
    "min": "minute",
    "mins": "minute",
    "minutes": "minute",
    "hr": "hour",
    "hrs": "hour",
    "hours": "hour",
    "days": "day",
    "ms": "millisecond",
    "milliseconds": "millisecond",
    "beat_per_minute": "bpm",
    "beats_per_minute": "bpm",
    "counts_per_minute": "count_per_minute",
    "breaths_per_minute": "count_per_minute",
    "m": "meter",
    "meters": "meter",
    "metre": "meter",
    "metres": "meter",
    "km": "kilometer",
    "kilometers": "kilometer",
    "kilometre": "kilometer",
    "kilometres": "kilometer",
    "mi": "mile",
    "miles": "mile",
    "m/s": "meter_per_second",
    "meter/second": "meter_per_second",
    "meters/second": "meter_per_second",
    "m/min": "meter_per_minute",
    "meter/minute": "meter_per_minute",
    "meters/minute": "meter_per_minute",
    "km/h": "kilometer_per_hour",
    "kph": "kilometer_per_hour",
    "mph": "mile_per_hour",
    "calories": "calorie",
    "kcal": "kilocalorie",
    "kilocalories": "kilocalorie",
    "j": "joule",
    "kj": "kilojoule",
    "g": "gram",
    "grams": "gram",
    "kg": "kilogram",
    "kilograms": "kilogram",
    "lb": "pound",
    "lbs": "pound",
    "pounds": "pound",
    "deg_c": "celsius",
    "degrees_celsius": "celsius",
    "deg_f": "fahrenheit",
    "degrees_fahrenheit": "fahrenheit",
    "mm_hg": "mmhg",
    "mg/dl": "mg_per_dl",
    "mg_dl": "mg_per_dl",
    "mmol/l": "mmol_per_l",
    "mmol_l": "mmol_per_l",
    "l": "liter",
    "liters": "liter",
    "litre": "liter",
    "litres": "liter",
    "ml": "milliliter",
    "milliliters": "milliliter",
    "millilitres": "milliliter",
    "scores": "score",
}

_COMPUTATION_FIELDS = frozenset(
    {"entity", "outputs", "filters", "conditions", "selector", "group_by"}
)
_OUTPUT_FIELDS = frozenset({"metric", "entity", "aggregation"})
_FILTER_FIELDS = frozenset(
    {"dimension", "operator", "value", "applies_to", "field", "op"}
)
_CONDITION_FIELDS = frozenset(
    {"metric", "operator", "value", "values", "unit", "op"}
)
_SELECTOR_FIELDS = frozenset({"mode", "n", "by_metric"})

ValidationError = Dict[str, str]
SanitizeResult = Tuple[Optional[Dict[str, Any]], List[ValidationError]]


def _error(code: str, path: str, message: str) -> ValidationError:
    """Build a stable, presentation-neutral validation error."""

    return {"code": code, "path": path, "message": message}


def _identifier(value: Any) -> str:
    """Return a conservative lowercase identifier for enum comparisons."""

    if not isinstance(value, str) or len(value) > 128:
        return ""
    token = unicodedata.normalize("NFKC", value).strip().lower()
    token = re.sub(r"[\s.-]+", "_", token)
    token = re.sub(r"_+", "_", token)
    return token.strip("_")


def _canonical_metric(value: Any) -> str:
    """Resolve a catalog alias while rejecting unbounded/non-string input."""

    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return ""
    metric = _normalize_metric_name(value)
    return metric if metric in ALLOWED_METRICS else ""


def _canonical_activity_label(value: Any) -> str:
    """Normalize a human activity label into a bounded semantic token.

    The result is data, never a column or expression.  Converting punctuation
    and whitespace to underscores gives the compiler a predictable token such
    as ``INTERVAL_WORKOUT`` while removing query metacharacters.
    """

    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value).strip()
    if not text or len(text) > 256:
        return ""
    chars: List[str] = []
    pending_separator = False
    for char in text:
        if char.isalnum():
            if pending_separator and chars:
                chars.append("_")
            chars.append(char.upper())
            pending_separator = False
        elif char == "_":
            pending_separator = bool(chars)
        else:
            pending_separator = bool(chars)
    label = "".join(chars).strip("_")
    if not label or len(label) > MAX_LABEL_LENGTH:
        return ""
    return label


def _canonical_dimension_value(dimension: str, value: Any) -> str:
    """Return a safe literal for a supported dimension, or ``""``."""

    if dimension == "activity_name":
        return _canonical_activity_label(value)
    if not isinstance(value, str):
        return ""
    text = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not text or len(text) > MAX_DIMENSION_VALUE_LENGTH:
        return ""
    # Preserve readable source labels while rejecting control characters and
    # query metacharacters.  The compiler must still quote this as a literal.
    safe_punctuation = frozenset(" ._:+-/()@")
    if any(not (char.isalnum() or char in safe_punctuation) for char in text):
        return ""
    return text


def _canonical_unit(value: Any) -> str:
    """Normalize a unit token into the closed v2 unit vocabulary."""

    if not isinstance(value, str):
        return ""
    raw = unicodedata.normalize("NFKC", value).strip().lower()
    if raw in _UNIT_ALIASES:
        return _UNIT_ALIASES[raw]
    token = re.sub(r"[\s.-]+", "_", raw)
    token = re.sub(r"_+", "_", token).strip("_")
    token = _UNIT_ALIASES.get(token, token)
    return token if token in ALLOWED_UNITS else ""


def _canonical_number(value: Any) -> Optional[float]:
    """Return a finite JSON numeric value, preserving integral inputs."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or abs(float(value)) > MAX_ABS_NUMERIC_VALUE:
        return None
    return value


def _unknown_field_errors(
    value: Mapping[str, Any], allowed: frozenset, path: str
) -> List[ValidationError]:
    """Report unknown keys in deterministic order."""

    return [
        _error(
            "unknown_field",
            f"{path}.{key}",
            f"Field '{key}' is not part of the v2 computation schema.",
        )
        for key in sorted(str(key) for key in value.keys() if key not in allowed)
    ]


def _aliased_field(
    item: Mapping[str, Any],
    canonical: str,
    alias: str,
    path: str,
    errors: List[ValidationError],
) -> Any:
    """Read a public field and one legacy spelling without ambiguity."""

    has_canonical = canonical in item
    has_alias = alias in item
    if has_canonical and has_alias and item[canonical] != item[alias]:
        errors.append(
            _error(
                "conflicting_aliases",
                f"{path}.{canonical}",
                f"'{canonical}' and its alias '{alias}' disagree.",
            )
        )
        return None
    if has_canonical:
        return item[canonical]
    return item.get(alias)


def _sanitize_output(
    item: Any, index: int, errors: List[ValidationError]
) -> Optional[Dict[str, Any]]:
    path = f"computation.outputs[{index}]"
    if not isinstance(item, Mapping):
        errors.append(_error("invalid_type", path, "Each output must be an object."))
        return None
    errors.extend(_unknown_field_errors(item, _OUTPUT_FIELDS, path))

    # Exclusivity is based on field presence, not truthiness.  Otherwise an
    # attacker (or malformed model output) could include a second field with an
    # empty value and bypass the structural contract.
    has_metric = "metric" in item
    has_entity = "entity" in item
    if has_metric == has_entity:
        errors.append(
            _error(
                "exclusive_fields_required",
                path,
                "An output must contain exactly one of 'metric' or 'entity'.",
            )
        )

    aggregation = _identifier(item.get("aggregation"))
    if aggregation not in ALLOWED_AGGREGATIONS:
        errors.append(
            _error(
                "invalid_aggregation",
                f"{path}.aggregation",
                "Aggregation is missing or is not supported by schema v2.",
            )
        )

    output: Dict[str, Any] = {}
    if has_metric:
        metric = _canonical_metric(item.get("metric"))
        if not metric:
            errors.append(
                _error(
                    "unknown_metric",
                    f"{path}.metric",
                    "The output metric is not in the semantic metric catalog.",
                )
            )
        else:
            output["metric"] = metric
    elif has_entity:
        entity = _identifier(item.get("entity"))
        if entity not in ALLOWED_ENTITIES:
            errors.append(
                _error(
                    "invalid_entity",
                    f"{path}.entity",
                    "The output entity is not supported by schema v2.",
                )
            )
        else:
            output["entity"] = entity
            if aggregation not in {"count", "count_distinct"}:
                errors.append(
                    _error(
                        "incompatible_aggregation",
                        f"{path}.aggregation",
                        "Entity outputs support only count or count_distinct.",
                    )
                )
    if aggregation in ALLOWED_AGGREGATIONS:
        output["aggregation"] = aggregation
    return output or None


def _sanitize_filter(
    item: Any, index: int, errors: List[ValidationError]
) -> Optional[Dict[str, Any]]:
    path = f"computation.filters[{index}]"
    if not isinstance(item, Mapping):
        errors.append(_error("invalid_type", path, "Each filter must be an object."))
        return None
    errors.extend(_unknown_field_errors(item, _FILTER_FIELDS, path))

    raw_dimension = _aliased_field(item, "dimension", "field", path, errors)
    dimension = _identifier(raw_dimension)
    if dimension not in ALLOWED_DIMENSIONS:
        errors.append(
            _error(
                "invalid_dimension",
                f"{path}.dimension",
                "Only approved semantic dimensions may be filtered.",
            )
        )

    raw_operator = _aliased_field(item, "operator", "op", path, errors)
    operator = _identifier(raw_operator)
    if operator not in ALLOWED_DIMENSION_OPERATORS:
        errors.append(
            _error(
                "invalid_operator",
                f"{path}.operator",
                "The dimension-filter operator is not supported.",
            )
        )

    canonical_value: Any = None
    raw_value = item.get("value")
    if operator == "in":
        if not isinstance(raw_value, (list, tuple)) or isinstance(raw_value, (str, bytes)):
            errors.append(
                _error(
                    "invalid_value",
                    f"{path}.value",
                    "The 'in' operator requires a non-empty list of literals.",
                )
            )
        elif not raw_value or len(raw_value) > MAX_IN_VALUES:
            errors.append(
                _error(
                    "invalid_value_count",
                    f"{path}.value",
                    f"An 'in' filter accepts 1 to {MAX_IN_VALUES} literals.",
                )
            )
        elif dimension in ALLOWED_DIMENSIONS:
            normalized = [_canonical_dimension_value(dimension, value) for value in raw_value]
            if any(not value for value in normalized):
                errors.append(
                    _error(
                        "invalid_value",
                        f"{path}.value",
                        "A dimension literal is empty, unsafe, or too long.",
                    )
                )
            else:
                canonical_value = sorted(set(normalized))
    elif operator in {"eq", "ne"}:
        if dimension in ALLOWED_DIMENSIONS:
            canonical_value = _canonical_dimension_value(dimension, raw_value)
            if not canonical_value:
                errors.append(
                    _error(
                        "invalid_value",
                        f"{path}.value",
                        "The dimension literal is empty, unsafe, or too long.",
                    )
                )

    if (
        dimension in ALLOWED_DIMENSIONS
        and operator in ALLOWED_DIMENSION_OPERATORS
        and canonical_value is not None
        and canonical_value != ""
    ):
        predicate = {
            "dimension": dimension,
            "operator": operator,
            "value": canonical_value,
        }
        if "applies_to" in item and item.get("applies_to") not in (None, ""):
            applies_to = _canonical_metric(item.get("applies_to"))
            if not applies_to:
                errors.append(
                    _error(
                        "unknown_metric",
                        f"{path}.applies_to",
                        "Filter scope must name a metric in the semantic catalog.",
                    )
                )
                return None
            predicate["applies_to"] = applies_to
        return predicate
    return None


def _sanitize_condition(
    item: Any, index: int, errors: List[ValidationError]
) -> Optional[Dict[str, Any]]:
    path = f"computation.conditions[{index}]"
    if not isinstance(item, Mapping):
        errors.append(_error("invalid_type", path, "Each condition must be an object."))
        return None
    errors.extend(_unknown_field_errors(item, _CONDITION_FIELDS, path))

    metric = _canonical_metric(item.get("metric"))
    if not metric:
        errors.append(
            _error(
                "unknown_metric",
                f"{path}.metric",
                "The condition metric is not in the semantic metric catalog.",
            )
        )

    raw_operator = _aliased_field(item, "operator", "op", path, errors)
    operator = _identifier(raw_operator)
    if operator not in ALLOWED_CONDITION_OPERATORS:
        errors.append(
            _error(
                "invalid_operator",
                f"{path}.operator",
                "The numeric-condition operator is not supported.",
            )
        )

    # A threshold without an explicit display unit is interpreted in the
    # metric's canonical/native unit. This keeps ordinary questions such as
    # "speed above 80" representable while the compiler still owns conversion.
    unit = _canonical_unit(item.get("unit", "native"))
    if not unit:
        errors.append(
            _error(
                "invalid_unit",
                f"{path}.unit",
                "A numeric condition requires an approved unit.",
            )
        )

    condition: Dict[str, Any] = {}
    if metric:
        condition["metric"] = metric
    if operator in ALLOWED_CONDITION_OPERATORS:
        condition["operator"] = operator

    if operator == "between":
        if "value" in item and "values" in item and item["value"] != item["values"]:
            errors.append(
                _error(
                    "conflicting_aliases",
                    f"{path}.values",
                    "'values' and its alias 'value' disagree.",
                )
            )
            raw_values: Any = None
        else:
            raw_values = item.get("values", item.get("value"))
        if not isinstance(raw_values, (list, tuple)) or len(raw_values) != 2:
            errors.append(
                _error(
                    "invalid_value",
                    f"{path}.values",
                    "A between condition requires exactly two finite numbers.",
                )
            )
        else:
            low = _canonical_number(raw_values[0])
            high = _canonical_number(raw_values[1])
            if low is None or high is None:
                errors.append(
                    _error(
                        "invalid_value",
                        f"{path}.values",
                        "Between bounds must be finite, bounded numbers.",
                    )
                )
            else:
                condition["values"] = [min(low, high), max(low, high)]
    elif operator in ALLOWED_CONDITION_OPERATORS:
        if "values" in item:
            errors.append(
                _error(
                    "invalid_value",
                    f"{path}.values",
                    "Only a between condition may use 'values'.",
                )
            )
        value = _canonical_number(item.get("value"))
        if value is None:
            errors.append(
                _error(
                    "invalid_value",
                    f"{path}.value",
                    "A numeric condition requires one finite number.",
                )
            )
        else:
            condition["value"] = value

    if unit:
        condition["unit"] = unit
    required_value = "values" if operator == "between" else "value"
    if metric and operator in ALLOWED_CONDITION_OPERATORS and unit and required_value in condition:
        return condition
    return None


def _sanitize_selector(
    item: Any, errors: List[ValidationError]
) -> Optional[Dict[str, Any]]:
    path = "computation.selector"
    if not isinstance(item, Mapping):
        errors.append(_error("invalid_type", path, "Selector must be an object."))
        return None
    errors.extend(_unknown_field_errors(item, _SELECTOR_FIELDS, path))

    mode = _identifier(item.get("mode"))
    if mode not in ALLOWED_SELECTOR_MODES:
        errors.append(
            _error(
                "invalid_selector_mode",
                f"{path}.mode",
                "Selector mode must be first, last, top, or bottom.",
            )
        )

    n = item.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_SELECTOR_N:
        errors.append(
            _error(
                "invalid_selector_limit",
                f"{path}.n",
                f"Selector n must be an integer from 1 to {MAX_SELECTOR_N}.",
            )
        )

    selector: Dict[str, Any] = {}
    if mode in ALLOWED_SELECTOR_MODES:
        selector["mode"] = mode
    if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= MAX_SELECTOR_N:
        selector["n"] = n

    has_by_metric = "by_metric" in item and item.get("by_metric") not in (None, "")
    if mode in {"top", "bottom"}:
        metric = _canonical_metric(item.get("by_metric"))
        if not metric:
            errors.append(
                _error(
                    "selector_metric_required",
                    f"{path}.by_metric",
                    "Top and bottom selectors require a catalog metric.",
                )
            )
        else:
            selector["by_metric"] = metric
    elif mode in {"first", "last"} and has_by_metric:
        errors.append(
            _error(
                "selector_metric_not_allowed",
                f"{path}.by_metric",
                "First and last use the entity's canonical time; by_metric is not allowed.",
            )
        )

    if mode in ALLOWED_SELECTOR_MODES and "n" in selector:
        if mode in {"top", "bottom"} and "by_metric" not in selector:
            return None
        if mode in {"first", "last"} and has_by_metric:
            return None
        return selector
    return None


def _sanitize_group_by(
    value: Any, errors: List[ValidationError]
) -> Optional[List[str]]:
    path = "computation.group_by"
    if isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        errors.append(_error("invalid_type", path, "group_by must be a list."))
        return None
    if not values or len(values) > MAX_GROUP_BY:
        errors.append(
            _error(
                "invalid_group_count",
                path,
                f"group_by accepts 1 to {MAX_GROUP_BY} semantic fields.",
            )
        )
        return None
    groups: List[str] = []
    for index, value_item in enumerate(values):
        group = _identifier(value_item)
        if group not in ALLOWED_GROUP_BY:
            errors.append(
                _error(
                    "invalid_group",
                    f"{path}[{index}]",
                    "The grouping field is not supported by schema v2.",
                )
            )
        elif group not in groups:
            groups.append(group)
    if not groups:
        return None
    order = {name: index for index, name in enumerate(GROUP_BY_ORDER)}
    return sorted(groups, key=order.__getitem__)


def _predicate_key(value: Dict[str, Any]) -> str:
    """Return a stable sort/deduplication key for a canonical predicate."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dedupe_sorted(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort predicates canonically and remove exact duplicates."""

    by_key = {_predicate_key(item): item for item in items}
    return [by_key[key] for key in sorted(by_key)]


def _extract_body(candidate: Any) -> Tuple[Optional[Dict[str, Any]], List[ValidationError], bool]:
    """Extract a direct body or a versioned envelope.

    The boolean result distinguishes "no v2 computation supplied" from an
    explicitly supplied but invalid computation.
    """

    if candidate is None:
        return None, [], False
    if not isinstance(candidate, Mapping):
        return (
            None,
            [_error("invalid_type", "computation", "Computation must be an object.")],
            True,
        )
    if not candidate:
        return None, [], False

    has_envelope = "computation" in candidate
    has_version = "schema_version" in candidate
    if has_envelope:
        version = candidate.get("schema_version")
        if not has_version:
            return (
                None,
                [
                    _error(
                        "missing_schema_version",
                        "schema_version",
                        "A computation envelope must declare schema_version 2.",
                    )
                ],
                True,
            )
        if isinstance(version, bool) or str(version).strip() != str(SCHEMA_VERSION):
            return (
                None,
                [
                    _error(
                        "unsupported_schema_version",
                        "schema_version",
                        "Only computation schema_version 2 is supported.",
                    )
                ],
                True,
            )
        body = candidate.get("computation")
        if not isinstance(body, Mapping):
            return (
                None,
                [_error("invalid_type", "computation", "Computation must be an object.")],
                True,
            )
        return dict(body), [], True

    # A direct body may carry its version for convenience.  Strip it from the
    # canonical result, but apply the same fail-closed version check.
    if has_version:
        version = candidate.get("schema_version")
        if isinstance(version, bool) or str(version).strip() != str(SCHEMA_VERSION):
            return (
                None,
                [
                    _error(
                        "unsupported_schema_version",
                        "schema_version",
                        "Only computation schema_version 2 is supported.",
                    )
                ],
                True,
            )
        body = {key: value for key, value in candidate.items() if key != "schema_version"}
        return body, [], True
    return dict(candidate), [], True


def _sanitize_body(body: Dict[str, Any]) -> SanitizeResult:
    """Validate and canonicalize one extracted computation body."""

    errors: List[ValidationError] = []
    errors.extend(_unknown_field_errors(body, _COMPUTATION_FIELDS, "computation"))

    entity = _identifier(body.get("entity"))
    if entity not in ALLOWED_ENTITIES:
        errors.append(
            _error(
                "invalid_entity",
                "computation.entity",
                "Computation entity is missing or unsupported.",
            )
        )

    raw_outputs = body.get("outputs")
    outputs: List[Dict[str, Any]] = []
    if not isinstance(raw_outputs, (list, tuple)) or isinstance(raw_outputs, (str, bytes)):
        errors.append(
            _error("invalid_type", "computation.outputs", "outputs must be a list.")
        )
    elif not raw_outputs or len(raw_outputs) > MAX_OUTPUTS:
        errors.append(
            _error(
                "invalid_output_count",
                "computation.outputs",
                f"A computation requires 1 to {MAX_OUTPUTS} outputs.",
            )
        )
    else:
        for index, item in enumerate(raw_outputs):
            output = _sanitize_output(item, index, errors)
            if output and output not in outputs:
                outputs.append(output)

    raw_filters = body.get("filters", [])
    filters: List[Dict[str, Any]] = []
    if not isinstance(raw_filters, (list, tuple)) or isinstance(raw_filters, (str, bytes)):
        errors.append(
            _error("invalid_type", "computation.filters", "filters must be a list.")
        )
    elif len(raw_filters) > MAX_FILTERS:
        errors.append(
            _error(
                "too_many_filters",
                "computation.filters",
                f"At most {MAX_FILTERS} dimension filters are allowed.",
            )
        )
    else:
        for index, item in enumerate(raw_filters):
            predicate = _sanitize_filter(item, index, errors)
            if predicate:
                filters.append(predicate)

    raw_conditions = body.get("conditions", [])
    conditions: List[Dict[str, Any]] = []
    if not isinstance(raw_conditions, (list, tuple)) or isinstance(raw_conditions, (str, bytes)):
        errors.append(
            _error("invalid_type", "computation.conditions", "conditions must be a list.")
        )
    elif len(raw_conditions) > MAX_CONDITIONS:
        errors.append(
            _error(
                "too_many_conditions",
                "computation.conditions",
                f"At most {MAX_CONDITIONS} metric conditions are allowed.",
            )
        )
    else:
        for index, item in enumerate(raw_conditions):
            predicate = _sanitize_condition(item, index, errors)
            if predicate:
                conditions.append(predicate)

    output_metrics = {
        output["metric"] for output in outputs if "metric" in output
    }
    output_entities = {
        output["entity"] for output in outputs if "entity" in output
    }
    condition_metrics = {
        condition["metric"] for condition in conditions if "metric" in condition
    }
    declared_metrics = output_metrics | condition_metrics
    for index, predicate in enumerate(filters):
        applies_to = predicate.get("applies_to")
        if applies_to and applies_to not in declared_metrics:
            errors.append(
                _error(
                    "unbound_filter_scope",
                    f"computation.filters[{index}].applies_to",
                    "Filter scope must refer to an output or condition metric.",
                )
            )
        if predicate.get("dimension") != "activity_name":
            continue
        workout_targets = {
            metric
            for metric in declared_metrics
            if metric.startswith("workout_") or metric.startswith("phone_workout_")
        }
        scoped_to_workout = bool(
            applies_to
            and (
                applies_to.startswith("workout_")
                or applies_to.startswith("phone_workout_")
            )
        )
        activity_compatible = (
            scoped_to_workout
            if applies_to
            else (
                # At day grain an activity filter means "days containing at
                # least one matching workout".  This is required for exact
                # distinct-day questions even when the output is the day
                # entity rather than a workout-valued metric.
                entity in {"day", "workout_session"}
                or "workout_session" in output_entities
                or bool(workout_targets)
            )
        )
        if not activity_compatible:
            errors.append(
                _error(
                    "incompatible_dimension",
                    f"computation.filters[{index}].dimension",
                    "activity_name requires day/workout_session grain or a workout metric target.",
                )
            )

    selector: Optional[Dict[str, Any]] = None
    if "selector" in body and body.get("selector") is not None:
        selector = _sanitize_selector(body.get("selector"), errors)

    group_by: Optional[List[str]] = None
    if "group_by" in body and body.get("group_by") is not None:
        group_by = _sanitize_group_by(body.get("group_by"), errors)

    if errors:
        return None, errors

    computation: Dict[str, Any] = {
        "entity": entity,
        "outputs": outputs,
        "filters": _dedupe_sorted(filters),
        "conditions": _dedupe_sorted(conditions),
    }
    if selector is not None:
        computation["selector"] = selector
    if group_by is not None:
        computation["group_by"] = group_by
    return computation, []


def sanitize_computation(
    candidate: Any,
    last_computation: Any = None,
    inherit: bool = False,
) -> SanitizeResult:
    """Return a canonical v2 computation body and structured validation errors.

    Parameters
    ----------
    candidate:
        Either a direct computation body or an enclosing object with
        ``schema_version`` and ``computation``.  Explicit versions other than
        2 are always rejected.
    last_computation:
        A previously sanitized body (or v2 envelope).  It is consulted only
        when ``inherit`` is true.
    inherit:
        Enables bounded follow-up inheritance.  Missing top-level computation
        fields are copied from a valid prior body; supplied fields replace the
        corresponding prior field atomically.  Nested objects are never
        implicitly merged.

    Returns
    -------
    tuple
        ``(body, [])`` for a valid computation, ``(None, [])`` when no v2
        computation was supplied, or ``(None, errors)`` for an explicit but
        invalid computation.  Each error has stable ``code``, ``path``, and
        ``message`` members suitable for execution gating and follow-up text.

    The function does not mutate either argument and is idempotent: sanitizing
    its canonical result again returns an equal object.
    """

    body, extraction_errors, supplied = _extract_body(candidate)
    if extraction_errors:
        return None, extraction_errors

    inherited: Optional[Dict[str, Any]] = None
    inherited_errors: List[ValidationError] = []
    if inherit and last_computation is not None:
        previous_body, previous_extraction_errors, previous_supplied = _extract_body(
            last_computation
        )
        if previous_extraction_errors:
            inherited_errors = previous_extraction_errors
        elif previous_supplied and previous_body is not None:
            inherited, inherited_errors = _sanitize_body(previous_body)

    if not supplied or body is None:
        if inherit and inherited is not None:
            return deepcopy(inherited), []
        if inherit and inherited_errors:
            return None, inherited_errors
        return None, []

    if inherit and inherited is not None:
        merged = deepcopy(inherited)
        # Top-level replacement is intentional: inheriting half of a selector
        # or predicate would make follow-up semantics difficult to audit.
        merged.update(deepcopy(body))
        body = merged
    return _sanitize_body(body)


def computation_dependencies(computation: Any = None) -> Dict[str, List[str]]:
    """Return semantic dependencies for a valid v2 computation.

    Dependencies contain catalog metric names, semantic entities, and approved
    dimensions only.  They deliberately contain no table, column, function,
    join, or query text.  Invalid input returns an empty manifest so callers
    cannot accidentally compile unchecked model output.
    """

    sanitized, errors = sanitize_computation(computation)
    if sanitized is None or errors:
        return {"metrics": [], "entities": [], "dimensions": []}

    metrics = {
        output["metric"]
        for output in sanitized["outputs"]
        if "metric" in output
    }
    metrics.update(condition["metric"] for condition in sanitized["conditions"])
    metrics.update(
        item["applies_to"]
        for item in sanitized["filters"]
        if item.get("applies_to")
    )
    selector = sanitized.get("selector") or {}
    if selector.get("by_metric"):
        metrics.add(selector["by_metric"])

    entities = {sanitized["entity"]}
    entities.update(
        output["entity"]
        for output in sanitized["outputs"]
        if "entity" in output
    )
    dimensions = {item["dimension"] for item in sanitized["filters"]}
    for group in sanitized.get("group_by", []):
        if group in ALLOWED_ENTITIES:
            entities.add(group)
        elif group in ALLOWED_DIMENSIONS:
            dimensions.add(group)

    entity_rank = {name: index for index, name in enumerate(ENTITY_ORDER)}
    dimension_rank = {name: index for index, name in enumerate(DIMENSION_ORDER)}
    return {
        "metrics": sorted(metrics),
        "entities": sorted(entities, key=entity_rank.__getitem__),
        "dimensions": sorted(dimensions, key=dimension_rank.__getitem__),
    }


def validate_llm_computation_support(computation: Any) -> List[ValidationError]:
    """Check LLM query capabilities without imposing legacy compiler grains.

    Validate evidence families, not which combinations a template compiler
    implements. KQL scope and syntax remain subject to the separate guard.
    The deterministic compiler's narrower capability check is unchanged.
    """

    sanitized, errors = sanitize_computation(computation)
    if errors:
        return errors
    if sanitized is None:
        return [_error("invalid_computation", "computation", "Computation must be an object.")]
    entity = sanitized["entity"]
    metrics = computation_dependencies(sanitized)["metrics"]
    if not metrics and entity in {"sample", "hour", "day"} and not any(
        item.get("dimension") == "activity_name" for item in sanitized.get("filters", [])
    ):
        errors.append(_error(
            "missing_entity_source", "computation",
            "This calculation needs a metric or activity identifying its evidence stream.",
        ))
    compatible_resources = {
        "sample": {HEALTH_SAMPLES},
        "hour": {ACTIVITY_DAILY, ACTIVITY_HOURLY},
        "night": {SLEEP_NIGHTLY},
        "sleep_session": {SLEEP_NIGHTLY},
        "workout_session": {WORKOUT_SESSIONS},
        "day": {HEALTH_SAMPLES, ACTIVITY_DAILY, ACTIVITY_HOURLY, SLEEP_NIGHTLY, WORKOUT_SESSIONS},
    }[entity]
    # Conditions on another stream can select exact eligible dates for sleep
    # or workout sessions. They must not be forced into the output's family.
    # Sample/hour retain their existing single-stream semantic restriction.
    output_metrics = {output["metric"] for output in sanitized["outputs"] if output.get("metric")}
    constrained_metrics = metrics if entity in {"sample", "hour"} else output_metrics
    for metric in constrained_metrics:
        resource = METRIC_DEFINITIONS.get(metric, {}).get("resource")
        if resource not in compatible_resources or metric in PHONE_PROFILE_METRICS:
            errors.append(_error(
                "incompatible_entity_metric", "computation",
                f"Metric '{metric}' has no supported {entity}-grain numeric evidence stream.",
            ))
    for metric in set(metrics) - set(constrained_metrics):
        if metric in PHONE_PROFILE_METRICS:
            errors.append(_error("incompatible_entity_metric", "computation",
                                 "Profile fields are not numeric sensor evidence."))
    for index, output in enumerate(sanitized["outputs"]):
        if output.get("entity") and output["entity"] not in {entity, "day"}:
            errors.append(_error(
                "incompatible_output_entity", f"computation.outputs[{index}].entity",
                "This evidence stream supports its own entity or distinct calendar-day counts.",
            ))
    for group in sanitized.get("group_by", []):
        if group not in {entity, "day", "source_name", "source_platform"} | ({"hour"} if entity in {"sample", "hour"} else set()):
            errors.append(_error(
                "incompatible_grouping", "computation.group_by",
                "This evidence stream cannot establish the requested grouping entity.",
            ))
    return errors


def has_v2_intent(candidate: Any = None) -> bool:
    """Return whether input is attempting to use the typed computation schema.

    An explicit version-1 outer plan without computation fields remains on the
    established compatibility path.  A computation object/body, version 2, or
    an unknown/future version counts as typed intent so malformed typed plans
    fail closed instead of falling through to a permissive legacy query.
    """

    if not isinstance(candidate, Mapping):
        return False
    if "computation" in candidate:
        return True
    if any(field in candidate for field in _COMPUTATION_FIELDS):
        return True
    if "schema_version" not in candidate:
        return False
    version = candidate.get("schema_version")
    return isinstance(version, bool) or str(version).strip() != "1"


__all__ = [
    "AGGREGATION_ORDER",
    "ALLOWED_AGGREGATIONS",
    "ALLOWED_CONDITION_OPERATORS",
    "ALLOWED_DIMENSIONS",
    "ALLOWED_DIMENSION_OPERATORS",
    "ALLOWED_ENTITIES",
    "ALLOWED_FILTER_OPERATORS",
    "ALLOWED_GROUP_BY",
    "ALLOWED_METRICS",
    "ALLOWED_NUMERIC_OPERATORS",
    "ALLOWED_SELECTOR_MODES",
    "ALLOWED_UNITS",
    "DIMENSION_ORDER",
    "ENTITY_ORDER",
    "GROUP_BY_ORDER",
    "MAX_CONDITIONS",
    "MAX_ABS_NUMERIC_VALUE",
    "MAX_FILTERS",
    "MAX_GROUP_BY",
    "MAX_IN_VALUES",
    "MAX_OUTPUTS",
    "MAX_SELECTOR_N",
    "SCHEMA_VERSION",
    "UNIT_ORDER",
    "ValidationError",
    "computation_dependencies",
    "has_v2_intent",
    "sanitize_computation",
    "validate_llm_computation_support",
]
