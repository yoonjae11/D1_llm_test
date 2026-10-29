"""Explicit local evaluation unit metadata, never imported by production routes.

No dataset names, IDs, labels, or KQL templates belong here. The caller declares
the recorded-unit contract and the question author's unit convention separately.
"""
from dataclasses import dataclass
import re


SPEED_UNITS = {"m/s": "METER_PER_SECOND", "m/min": "METER_PER_MINUTE",
               "km/h": "KILOMETER_PER_HOUR"}
_TO_MPS = {"m/s": 1.0, "m/min": 1.0 / 60.0, "km/h": 1.0 / 3.6}


def convert_speed(value, source_unit: str, target_unit: str):
    """Scorer-only dimensional conversion; not a query or answer correction."""
    if source_unit not in SPEED_UNITS or target_unit not in SPEED_UNITS:
        raise ValueError("Unsupported speed unit")
    return None if value is None else float(value) * _TO_MPS[source_unit] / _TO_MPS[target_unit]


@dataclass(frozen=True)
class LocalEvaluationUnits:
    stored_speed_unit: str | None = None
    question_speed_unit: str | None = None

    def __post_init__(self):
        for unit in (self.stored_speed_unit, self.question_speed_unit):
            if unit is not None and unit not in SPEED_UNITS:
                raise ValueError("Unsupported speed unit")
        if self.question_speed_unit and not self.stored_speed_unit:
            raise ValueError("--question-speed-unit requires --stored-speed-unit")

    def prepare_question(self, question: str) -> str:
        """Clarify omitted units, leaving explicit question units authoritative.

        Only metadata is added. The common LLM still interprets the threshold,
        constructs KQL and chooses the operators; no query/result is rewritten.
        """
        if not self.stored_speed_unit or not re.search(r"\bspeed\b", question, re.I):
            return question
        notes = [f"Recorded workout speed has a declared producer unit of {self.stored_speed_unit}."]
        if self.question_speed_unit:
            notes.append(f"Numeric speed thresholds WITHOUT an explicit unit in the original question "
                         f"are expressed in {self.question_speed_unit}. Explicit units in the original "
                         "question take precedence.")
        notes.append(f"Report recorded speed outputs in {self.stored_speed_unit}; keep the stored "
                     "quantity, including zero. This metadata does not request distance/time-derived speed.")
        return question + "\n\nEvaluation measurement metadata: " + " ".join(notes)

    def diagnostics(self) -> dict:
        return {"stored_speed_unit": self.stored_speed_unit,
                "unqualified_question_speed_unit": self.question_speed_unit,
                "basis": "explicit_local_caller_contract", "rewrites_stored_values": False}

