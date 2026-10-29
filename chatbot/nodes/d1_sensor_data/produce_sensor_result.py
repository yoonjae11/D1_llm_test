"""Convert sensor evidence to the common domain result."""

from chatbot.policies.result_processing import UNAVAILABLE_RESULT_STATUSES, _has_records, _parse_query_result
from chatbot.schemas.domain_execution_result import DomainExecutionResult


def produce_sensor_result(state: dict) -> dict:
    plan = state.get("plan") or {}
    rows = _parse_query_result(state.get("query_result"))
    missing = []
    if state.get("error"):
        status = "error"
        summary = "Sensor query failed or was blocked"
        rows = []
    elif plan.get("execution_ready") is False:
        status = "insufficient_information"
        summary = "Sensor query needs clarification"
        missing.append(
            str(plan.get("followup_question") or "A valid sensor query plan")
        )
        rows = []
    elif not _has_records(rows):
        status = "insufficient_information"
        summary = "No sensor records for the requested scope"
        missing.append("Sensor measurements for the requested metrics and period")
    else:
        status = "success"
        summary = "Sensor query completed; use the supplied values and coverage"

    coverage = state.get("coverage_context") or {}
    recheck_status = coverage.get("recheck_status")
    if recheck_status and recheck_status != "completed":
        missing.append("Flat activity evidence could not be reconciled with the daily aggregate")
        if status == "success":
            summary = "Daily activity result is provisional because the flat recheck did not complete"
    if coverage.get("daily_flat_coverage_unverified"):
        missing.append("Daily-to-flat activity coverage could not be verified")
        if status == "success":
            summary = "Daily activity result is provisional because flat coverage could not be verified"
    if coverage.get("has_gap"):
        missing.append("Some requested dates have no recorded measurements")
    failed_metrics = coverage.get("failed_metrics")
    if isinstance(failed_metrics, list) and failed_metrics:
        missing.append(
            "Some requested sensor metrics could not be queried: "
            + ", ".join(str(metric) for metric in failed_metrics)
        )
        if status == "success":
            summary = "Sensor query completed with partial metric coverage"

    typed_rows = rows if isinstance(rows, list) else []
    unavailable = [
        row
        for row in typed_rows
        if isinstance(row, dict)
        and (str(row.get("ResultStatus") or "").lower() in UNAVAILABLE_RESULT_STATUSES
             or (row.get("Value") is None
                 and row.get("DiagnosticReason") == "insufficient_time_resolution"))
    ]
    if unavailable:
        missing.append(
            "Some requested values lack measurement/condition evidence, usable units, "
            "enough observations or sufficient time resolution; null is not zero"
        )
        if status == "success" and len(unavailable) == len(typed_rows):
            status = "insufficient_information"
            summary = (
                "Query completed but requested values are unavailable or lack sufficient time resolution"
            )
    return {
        "d1_result": DomainExecutionResult(
            domain="D1",
            status=status,
            summary=summary,
            structured_result={
                "records": rows,
                "coverage_context": coverage,
                "metrics": plan.get("metrics", []),
                "time_range": plan.get("time_range", {}),
                "comparison_time_range": plan.get("comparison_time_range"),
                "time_of_day": plan.get("time_of_day"),
                "computation": plan.get("computation", {}),
            },
            missing_information=missing,
            provenance={
                "service": "existing_sensor_analytics",
                "guard_status": state.get("guard_status"),
                "execution_status": state.get("execution_status"),
                "kql_generation_trace": state.get("kql_generation_trace"),
            },
        ).model_dump()
    }
