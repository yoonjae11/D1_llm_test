"""Logical health-data resources and their trusted ADX bindings.

The chatbot plans against stable logical resource identifiers.  A
``DataProfile`` is the only place where those identifiers are bound to trusted
Kusto objects.  The default profile binds the six core resources to the shared
``Sensor*`` function contract exposed by both production and evaluation
databases; tests may temporarily install another validated profile without
changing prompts or metric semantics.

Profile names are intentionally not loaded from environment variables and
physical names must be simple Kusto identifiers.  This keeps the binding
surface explicit and prevents arbitrary query text from entering KQL through
configuration.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, Iterator, Mapping, Optional, Tuple


# Stable logical IDs.  These values are application contracts, not ADX names.
ACTIVITY_DAILY = "activity.daily"
ACTIVITY_HOURLY = "activity.hourly"
ACTIVITY_INTERVAL = "activity.interval"
SLEEP_NIGHTLY = "sleep.nightly"
WORKOUT_SESSIONS = "workout.sessions"
SLEEP_SESSION_DEDUP = "sleep.session_dedup"
SLEEP_STAGE_DEDUP = "sleep.stage_dedup"
HEALTH_SAMPLES = "health.samples"
CONVERSATION_HISTORY = "conversation.history"

# Optional vendor-specific resources.  Core metric resolution does not fall
# back to these resources unless the caller explicitly enables that behavior.
WITHINGS_ACTIVITY = "withings.activity"
WITHINGS_SLEEP = "withings.sleep"
WITHINGS_INTRADAY = "withings.intraday"
WITHINGS_SLEEP_DETAIL = "withings.sleep_detail"
WITHINGS_WORKOUTS = "withings.workouts"
WITHINGS_NIGHT_EVENTS = "withings.night_events"


SOURCE_SCOPE_CURATED = "curated"
SOURCE_SCOPE_CONVERSATION = "conversation"
SOURCE_SCOPE_WITHINGS = "withings"

OBJECT_TABLE = "table"
OBJECT_MATERIALIZED_VIEW = "materialized_view"
OBJECT_FUNCTION = "function"


# One deterministic order is shared by profile validation, prompt rendering,
# safety allowlists, and diagnostics.  Internal sleep resources follow the
# public nightly contract that consumes them.
ORDERED_LOGICAL_RESOURCES: Tuple[str, ...] = (
    ACTIVITY_DAILY,
    ACTIVITY_HOURLY,
    ACTIVITY_INTERVAL,
    SLEEP_NIGHTLY,
    WORKOUT_SESSIONS,
    SLEEP_SESSION_DEDUP,
    SLEEP_STAGE_DEDUP,
    HEALTH_SAMPLES,
    WITHINGS_ACTIVITY,
    WITHINGS_SLEEP,
    WITHINGS_INTRADAY,
    WITHINGS_SLEEP_DETAIL,
    WITHINGS_WORKOUTS,
    WITHINGS_NIGHT_EVENTS,
    CONVERSATION_HISTORY,
)

CURATED_RESOURCE_IDS = frozenset(
    {
        ACTIVITY_DAILY,
        ACTIVITY_HOURLY,
        ACTIVITY_INTERVAL,
        SLEEP_NIGHTLY,
        WORKOUT_SESSIONS,
        SLEEP_SESSION_DEDUP,
        SLEEP_STAGE_DEDUP,
        HEALTH_SAMPLES,
    }
)
CONVERSATION_RESOURCE_IDS = frozenset({CONVERSATION_HISTORY})
WITHINGS_RESOURCE_IDS = frozenset(
    {
        WITHINGS_ACTIVITY,
        WITHINGS_SLEEP,
        WITHINGS_INTRADAY,
        WITHINGS_SLEEP_DETAIL,
        WITHINGS_WORKOUTS,
        WITHINGS_NIGHT_EVENTS,
    }
)
CORE_RESOURCE_IDS = CURATED_RESOURCE_IDS | CONVERSATION_RESOURCE_IDS
OPTIONAL_RESOURCE_IDS = WITHINGS_RESOURCE_IDS

# Compatibility/source-routing aliases.  ``SMARTPHONE`` refers to the current
# UserId-scoped curated ingestion contract, even though the logical IDs remain
# vendor neutral.
SMARTPHONE_RESOURCE_IDS = CURATED_RESOURCE_IDS
APP_USER_RESOURCE_IDS = WITHINGS_RESOURCE_IDS | CONVERSATION_RESOURCE_IDS
USER_ID_RESOURCE_IDS = CURATED_RESOURCE_IDS
SOURCE_SCOPE_RESOURCE_IDS = MappingProxyType(
    {
        SOURCE_SCOPE_CURATED: CURATED_RESOURCE_IDS,
        SOURCE_SCOPE_WITHINGS: WITHINGS_RESOURCE_IDS,
        SOURCE_SCOPE_CONVERSATION: CONVERSATION_RESOURCE_IDS,
    }
)

_SIMPLE_KUSTO_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VALID_OBJECT_KINDS = frozenset(
    {OBJECT_TABLE, OBJECT_MATERIALIZED_VIEW, OBJECT_FUNCTION}
)
_VALID_SOURCE_SCOPES = frozenset(
    {SOURCE_SCOPE_CURATED, SOURCE_SCOPE_CONVERSATION, SOURCE_SCOPE_WITHINGS}
)


@dataclass(frozen=True, slots=True)
class ResourceBinding:
    """Immutable binding between one logical resource and one Kusto object."""

    logical_id: str
    physical_name: str
    object_kind: str
    source_scope: str
    scope_column: str
    optional: bool = False

    def __post_init__(self) -> None:
        if self.logical_id not in ORDERED_LOGICAL_RESOURCES:
            raise ValueError(f"Unknown logical resource: {self.logical_id!r}")
        if not isinstance(self.physical_name, str):
            raise TypeError("Physical Kusto object name must be a string")
        if not _SIMPLE_KUSTO_IDENTIFIER_RE.fullmatch(self.physical_name):
            raise ValueError(
                "Physical Kusto object names must be simple identifiers: "
                f"{self.physical_name!r}"
            )
        if self.object_kind not in _VALID_OBJECT_KINDS:
            raise ValueError(f"Unsupported Kusto object kind: {self.object_kind!r}")
        if self.source_scope not in _VALID_SOURCE_SCOPES:
            raise ValueError(f"Unsupported source scope: {self.source_scope!r}")
        if not _SIMPLE_KUSTO_IDENTIFIER_RE.fullmatch(self.scope_column):
            raise ValueError(f"Invalid scope column: {self.scope_column!r}")


@dataclass(frozen=True, slots=True)
class DataProfile:
    """Complete, immutable set of physical bindings for one data backend."""

    profile_id: str
    bindings: Mapping[str, ResourceBinding]

    def __post_init__(self) -> None:
        profile_id = str(self.profile_id or "").strip()
        if not profile_id:
            raise ValueError("Data profile id must not be empty")

        supplied = dict(self.bindings)
        known = set(ORDERED_LOGICAL_RESOURCES)
        unknown = set(supplied) - known
        missing = known - set(supplied)
        if unknown:
            raise ValueError(f"Unknown logical resources: {sorted(unknown)!r}")
        if missing:
            raise ValueError(f"Missing logical resources: {sorted(missing)!r}")

        ordered: Dict[str, ResourceBinding] = {}
        physical_to_logical: Dict[str, str] = {}
        for logical_id in ORDERED_LOGICAL_RESOURCES:
            binding = supplied[logical_id]
            if not isinstance(binding, ResourceBinding):
                raise TypeError(
                    f"Binding for {logical_id!r} must be a ResourceBinding"
                )
            if binding.logical_id != logical_id:
                raise ValueError(
                    f"Binding key {logical_id!r} does not match "
                    f"{binding.logical_id!r}"
                )
            folded_name = binding.physical_name.casefold()
            duplicate = physical_to_logical.get(folded_name)
            if duplicate is not None:
                raise ValueError(
                    f"Physical name {binding.physical_name!r} is already bound "
                    f"to {duplicate!r}"
                )
            physical_to_logical[folded_name] = logical_id
            ordered[logical_id] = binding

        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "bindings", MappingProxyType(ordered))

    def binding(self, logical_id: str) -> ResourceBinding:
        try:
            return self.bindings[logical_id]
        except KeyError as exc:
            raise KeyError(f"Unknown logical resource: {logical_id!r}") from exc

    def physical_name(self, logical_id: str) -> str:
        return self.binding(logical_id).physical_name

    def logical_name(self, physical_name: str) -> str:
        sought = str(physical_name or "").casefold()
        for logical_id, binding in self.bindings.items():
            if binding.physical_name.casefold() == sought:
                return logical_id
        raise KeyError(
            f"Physical object {physical_name!r} is not bound in {self.profile_id!r}"
        )

    def logical_ids(
        self,
        *,
        scope_column: Optional[str] = None,
        optional: Optional[bool] = None,
        source_scope: Optional[str] = None,
        object_kind: Optional[str] = None,
    ) -> Tuple[str, ...]:
        """Return matching logical IDs in the canonical resource order."""

        return tuple(
            logical_id
            for logical_id, binding in self.bindings.items()
            if (scope_column is None or binding.scope_column == scope_column)
            and (optional is None or binding.optional is optional)
            and (source_scope is None or binding.source_scope == source_scope)
            and (object_kind is None or binding.object_kind == object_kind)
        )

    def physical_names(
        self,
        *,
        scope_column: Optional[str] = None,
        optional: Optional[bool] = None,
        source_scope: Optional[str] = None,
        object_kind: Optional[str] = None,
    ) -> frozenset[str]:
        """Return the physical names matching the supplied binding filters."""

        return frozenset(
            self.physical_name(logical_id)
            for logical_id in self.logical_ids(
                scope_column=scope_column,
                optional=optional,
                source_scope=source_scope,
                object_kind=object_kind,
            )
        )

    def with_overrides(
        self,
        profile_id: str,
        overrides: Mapping[str, str],
    ) -> "DataProfile":
        """Create a validated profile by changing trusted physical names only."""

        unknown = set(overrides) - set(ORDERED_LOGICAL_RESOURCES)
        if unknown:
            raise ValueError(f"Unknown logical resources: {sorted(unknown)!r}")

        replacements = dict(self.bindings)
        for logical_id, physical in overrides.items():
            if not isinstance(physical, str):
                raise TypeError(
                    f"Physical name override for {logical_id!r} must be a string"
                )
            current = self.binding(logical_id)
            replacements[logical_id] = ResourceBinding(
                logical_id=logical_id,
                physical_name=physical,
                object_kind=current.object_kind,
                source_scope=current.source_scope,
                scope_column=current.scope_column,
                optional=current.optional,
            )
        return DataProfile(profile_id=profile_id, bindings=replacements)


_RESOURCE_METADATA = {
    ACTIVITY_DAILY: (OBJECT_MATERIALIZED_VIEW, SOURCE_SCOPE_CURATED, "UserId", False),
    ACTIVITY_HOURLY: (OBJECT_MATERIALIZED_VIEW, SOURCE_SCOPE_CURATED, "UserId", False),
    ACTIVITY_INTERVAL: (OBJECT_MATERIALIZED_VIEW, SOURCE_SCOPE_CURATED, "UserId", False),
    SLEEP_NIGHTLY: (OBJECT_FUNCTION, SOURCE_SCOPE_CURATED, "UserId", False),
    WORKOUT_SESSIONS: (OBJECT_FUNCTION, SOURCE_SCOPE_CURATED, "UserId", False),
    SLEEP_SESSION_DEDUP: (OBJECT_MATERIALIZED_VIEW, SOURCE_SCOPE_CURATED, "UserId", False),
    SLEEP_STAGE_DEDUP: (OBJECT_MATERIALIZED_VIEW, SOURCE_SCOPE_CURATED, "UserId", False),
    HEALTH_SAMPLES: (OBJECT_TABLE, SOURCE_SCOPE_CURATED, "UserId", False),
    WITHINGS_ACTIVITY: (OBJECT_TABLE, SOURCE_SCOPE_WITHINGS, "AppUserId", True),
    WITHINGS_SLEEP: (OBJECT_TABLE, SOURCE_SCOPE_WITHINGS, "AppUserId", True),
    WITHINGS_INTRADAY: (OBJECT_TABLE, SOURCE_SCOPE_WITHINGS, "AppUserId", True),
    WITHINGS_SLEEP_DETAIL: (OBJECT_TABLE, SOURCE_SCOPE_WITHINGS, "AppUserId", True),
    WITHINGS_WORKOUTS: (OBJECT_TABLE, SOURCE_SCOPE_WITHINGS, "AppUserId", True),
    WITHINGS_NIGHT_EVENTS: (OBJECT_TABLE, SOURCE_SCOPE_WITHINGS, "AppUserId", True),
    CONVERSATION_HISTORY: (OBJECT_TABLE, SOURCE_SCOPE_CONVERSATION, "AppUserId", False),
}


def build_profile(
    profile_id: str,
    physical_names: Mapping[str, str],
    *,
    object_kinds: Optional[Mapping[str, str]] = None,
) -> DataProfile:
    """Build a complete validated profile from logical-to-physical names."""

    supplied = dict(physical_names)
    known = set(ORDERED_LOGICAL_RESOURCES)
    unknown = set(supplied) - known
    missing = known - set(supplied)
    if unknown:
        raise ValueError(f"Unknown logical resources: {sorted(unknown)!r}")
    if missing:
        raise ValueError(f"Missing logical resources: {sorted(missing)!r}")

    kind_overrides = dict(object_kinds or {})
    unknown_kinds = set(kind_overrides) - known
    if unknown_kinds:
        raise ValueError(
            f"Object-kind overrides contain unknown resources: {sorted(unknown_kinds)!r}"
        )
    invalid_kinds = {
        logical_id: kind
        for logical_id, kind in kind_overrides.items()
        if kind not in _VALID_OBJECT_KINDS
    }
    if invalid_kinds:
        raise ValueError(f"Unsupported object-kind overrides: {invalid_kinds!r}")

    bindings = {}
    for logical_id in ORDERED_LOGICAL_RESOURCES:
        physical = supplied[logical_id]
        if not isinstance(physical, str):
            raise TypeError(f"Physical name for {logical_id!r} must be a string")
        object_kind, source_scope, scope_column, optional = _RESOURCE_METADATA[logical_id]
        object_kind = kind_overrides.get(logical_id, object_kind)
        bindings[logical_id] = ResourceBinding(
            logical_id=logical_id,
            physical_name=physical,
            object_kind=object_kind,
            source_scope=source_scope,
            scope_column=scope_column,
            optional=optional,
        )
    return DataProfile(profile_id=profile_id, bindings=bindings)


SENSEI_ADX_V1 = build_profile(
    "sensei_adx_v1",
    {
        ACTIVITY_DAILY: "PhoneCumulativeDailyMV",
        ACTIVITY_HOURLY: "PhoneCumulativeHourlyMV",
        ACTIVITY_INTERVAL: "PhoneCumulativeIntervalDedupMV",
        SLEEP_NIGHTLY: "PhoneSleepNights",
        WORKOUT_SESSIONS: "SensorWorkoutSessions",
        SLEEP_SESSION_DEDUP: "PhoneSleepSessionDedupMV",
        SLEEP_STAGE_DEDUP: "PhoneSleepStageDedupMV",
        HEALTH_SAMPLES: "HealthDataFlat",
        WITHINGS_ACTIVITY: "WithingsActivity",
        WITHINGS_SLEEP: "WithingsSleep",
        WITHINGS_INTRADAY: "WithingsIntraday",
        WITHINGS_SLEEP_DETAIL: "WithingsSleepDetailMinute",
        WITHINGS_WORKOUTS: "WithingsWorkouts",
        WITHINGS_NIGHT_EVENTS: "WithingsSleepNightEventSegment",
        CONVERSATION_HISTORY: "TelegramConversationLog",
    },
)

# Canonical cross-database contract. Both production ``healthdb`` and an
# evaluation database expose these same six stored-function names and output
# shapes; only the selected ADX database changes. Internal/optional resources
# retain their legacy bindings but are not part of the conversational D1 path.
_SENSOR_FUNCTION_RESOURCES = frozenset(
    {
        ACTIVITY_DAILY,
        ACTIVITY_HOURLY,
        ACTIVITY_INTERVAL,
        HEALTH_SAMPLES,
        SLEEP_NIGHTLY,
        WORKOUT_SESSIONS,
    }
)
SENSEI_SENSOR_CONTRACT_V1 = build_profile(
    "sensei_sensor_contract_v1",
    {
        ACTIVITY_DAILY: "SensorActivityDaily",
        ACTIVITY_HOURLY: "SensorActivityHourly",
        ACTIVITY_INTERVAL: "SensorActivityInterval",
        SLEEP_NIGHTLY: "SensorSleepNights",
        WORKOUT_SESSIONS: "SensorWorkoutSessions",
        SLEEP_SESSION_DEDUP: "PhoneSleepSessionDedupMV",
        SLEEP_STAGE_DEDUP: "PhoneSleepStageDedupMV",
        HEALTH_SAMPLES: "SensorHealthSamples",
        WITHINGS_ACTIVITY: "WithingsActivity",
        WITHINGS_SLEEP: "WithingsSleep",
        WITHINGS_INTRADAY: "WithingsIntraday",
        WITHINGS_SLEEP_DETAIL: "WithingsSleepDetailMinute",
        WITHINGS_WORKOUTS: "WithingsWorkouts",
        WITHINGS_NIGHT_EVENTS: "WithingsSleepNightEventSegment",
        CONVERSATION_HISTORY: "TelegramConversationLog",
    },
    object_kinds={
        resource_id: OBJECT_FUNCTION for resource_id in _SENSOR_FUNCTION_RESOURCES
    },
)

# Keep explicitly forbidden raw input objects centralized too.  They are not
# logical resources and can never be enabled through a DataProfile.
DISALLOWED_PHYSICAL_NAMES = frozenset(
    {
        "HealthDataRaw",
        "HealthDataFlat",
        "PhoneCumulativeDailyMV",
        "PhoneCumulativeHourlyMV",
        "PhoneCumulativeIntervalDedupMV",
        "PhoneSleepNights",
        "PhoneSleepSessionDedupMV",
        "PhoneSleepStageDedupMV",
        "PHIASummary",
        "PHIAExercise",
    }
)
WITHINGS_PHYSICAL_PREFIX = "Withings"

_ACTIVE_DATA_PROFILE: ContextVar[DataProfile] = ContextVar(
    "sensei_active_data_profile",
    default=SENSEI_SENSOR_CONTRACT_V1,
)


def active_data_profile() -> DataProfile:
    """Return the profile active in the current async/thread context."""

    return _ACTIVE_DATA_PROFILE.get()


@contextmanager
def use_data_profile(profile: DataProfile) -> Iterator[DataProfile]:
    """Temporarily install a trusted profile for tests or offline evaluation."""

    if not isinstance(profile, DataProfile):
        raise TypeError("profile must be a DataProfile")
    token = _ACTIVE_DATA_PROFILE.set(profile)
    try:
        yield profile
    finally:
        _ACTIVE_DATA_PROFILE.reset(token)


def with_overrides(
    profile_id: str,
    overrides: Mapping[str, str],
    *,
    base: Optional[DataProfile] = None,
) -> DataProfile:
    """Create a trusted derivative of ``base`` (the default profile by default)."""

    return (base or SENSEI_SENSOR_CONTRACT_V1).with_overrides(profile_id, overrides)


def physical_name(logical_id: str, profile: Optional[DataProfile] = None) -> str:
    """Resolve a logical resource using an explicit or context-local profile."""

    return (profile or active_data_profile()).physical_name(logical_id)


def tabular_call(
    logical_id: str,
    *arguments: str,
    profile: Optional[DataProfile] = None,
) -> str:
    """Render a trusted stored-function call for one logical resource.

    Arguments are deliberately limited to simple KQL identifiers such as
    ``TargetUserId`` and ``StartLocal``. Values, expressions, and arbitrary KQL
    fragments cannot enter through this helper.
    """

    selected = profile or active_data_profile()
    binding = selected.binding(logical_id)
    if binding.object_kind != OBJECT_FUNCTION:
        raise ValueError(
            f"Logical resource {logical_id!r} is not function-bound in "
            f"profile {selected.profile_id!r}."
        )
    # Every function in the shared Sensor contract has the same
    # (user, inclusive-start, exclusive-end) signature.  Enforcing the arity at
    # the trusted renderer prevents a caller from accidentally producing a bare
    # or partially bounded function source.
    if len(arguments) != 3:
        raise ValueError(
            "A Sensor tabular function call requires exactly three trusted "
            "user/start/end arguments."
        )
    normalized_arguments = []
    for argument in arguments:
        if not isinstance(argument, str) or not _SIMPLE_KUSTO_IDENTIFIER_RE.fullmatch(
            argument
        ):
            raise ValueError(
                "Tabular function arguments must be simple trusted identifiers."
            )
        normalized_arguments.append(argument)
    return f"{binding.physical_name}({', '.join(normalized_arguments)})"


def logical_name(physical: str, profile: Optional[DataProfile] = None) -> str:
    """Resolve a physical Kusto identifier back to its logical resource ID."""

    return (profile or active_data_profile()).logical_name(physical)


def ordered_logical_resources() -> Tuple[str, ...]:
    """Return all logical resources in deterministic contract order."""

    return ORDERED_LOGICAL_RESOURCES


_PROMPT_RESOURCE_IDS = MappingProxyType(
    {
        "activity_daily_resource": ACTIVITY_DAILY,
        "activity_hourly_resource": ACTIVITY_HOURLY,
        "activity_interval_resource": ACTIVITY_INTERVAL,
        "sleep_nightly_resource": SLEEP_NIGHTLY,
        "workout_sessions_resource": WORKOUT_SESSIONS,
        "health_samples_resource": HEALTH_SAMPLES,
        "conversation_history_resource": CONVERSATION_HISTORY,
        "withings_activity_resource": WITHINGS_ACTIVITY,
        "withings_sleep_resource": WITHINGS_SLEEP,
        "withings_intraday_resource": WITHINGS_INTRADAY,
        "withings_sleep_detail_resource": WITHINGS_SLEEP_DETAIL,
        "withings_workouts_resource": WITHINGS_WORKOUTS,
        "withings_night_events_resource": WITHINGS_NIGHT_EVENTS,
    }
)


def prompt_resource_values(
    profile: Optional[DataProfile] = None,
) -> Dict[str, str]:
    """Render the complete backend-resource placeholder contract for prompts."""

    selected = profile or active_data_profile()
    values = {
        placeholder: selected.physical_name(logical_id)
        for placeholder, logical_id in _PROMPT_RESOURCE_IDS.items()
    }
    values["disallowed_raw_resource"] = ", ".join(
        sorted(DISALLOWED_PHYSICAL_NAMES)
    )
    return values


__all__ = [
    "ACTIVITY_DAILY",
    "ACTIVITY_HOURLY",
    "ACTIVITY_INTERVAL",
    "APP_USER_RESOURCE_IDS",
    "CONVERSATION_HISTORY",
    "CONVERSATION_RESOURCE_IDS",
    "CORE_RESOURCE_IDS",
    "CURATED_RESOURCE_IDS",
    "DISALLOWED_PHYSICAL_NAMES",
    "DataProfile",
    "HEALTH_SAMPLES",
    "OBJECT_FUNCTION",
    "OBJECT_MATERIALIZED_VIEW",
    "OBJECT_TABLE",
    "OPTIONAL_RESOURCE_IDS",
    "ORDERED_LOGICAL_RESOURCES",
    "ResourceBinding",
    "SENSEI_ADX_V1",
    "SENSEI_SENSOR_CONTRACT_V1",
    "SLEEP_NIGHTLY",
    "SLEEP_SESSION_DEDUP",
    "SLEEP_STAGE_DEDUP",
    "SMARTPHONE_RESOURCE_IDS",
    "SOURCE_SCOPE_CONVERSATION",
    "SOURCE_SCOPE_CURATED",
    "SOURCE_SCOPE_RESOURCE_IDS",
    "SOURCE_SCOPE_WITHINGS",
    "USER_ID_RESOURCE_IDS",
    "WITHINGS_ACTIVITY",
    "WITHINGS_INTRADAY",
    "WITHINGS_NIGHT_EVENTS",
    "WITHINGS_PHYSICAL_PREFIX",
    "WITHINGS_RESOURCE_IDS",
    "WITHINGS_SLEEP",
    "WITHINGS_SLEEP_DETAIL",
    "WITHINGS_WORKOUTS",
    "WORKOUT_SESSIONS",
    "active_data_profile",
    "build_profile",
    "logical_name",
    "ordered_logical_resources",
    "physical_name",
    "prompt_resource_values",
    "use_data_profile",
    "tabular_call",
    "with_overrides",
]
