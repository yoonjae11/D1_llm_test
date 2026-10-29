"""Reference-table clocks are opt-in, scoped, and independent of metric labels."""
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import json

from local_llm_test import resolve_local_clock, trace_record
from chatbot.policies.time_ranges import (
    EvaluationTimeContext, active_evaluation_time, evaluation_time_family, use_evaluation_time,
)
from chatbot.policies.kql_safety import inspect_query_safety
from chatbot.services.local_data_clock import (
    LocalClockDiscoveryError, build_referenced_clock_metadata_kql, discover_referenced_data_clock,
)


def row(start, end, count=12):
    return [{"DataStartLocal": start, "AsOfLocal": end, "ClockRecordCount": count}]


class ObservedMeasurementScopeTests(unittest.TestCase):
    def fixture(self):
        return {"schema_version": 2, "metrics": ["workout_steps", "heart_rate"],
                "time_range": {"mode": "absolute", "value": 3, "unit": "day", "anchor": "absolute",
                               "start_date": "2022-08-01", "end_date": "2022-08-03"},
                "computation": {"entity": "workout_session", "outputs": [{"metric": "workout_steps", "aggregation": "mean"}],
                                "conditions": [{"metric": "heart_rate", "operator": "gt", "value": 12, "unit": "native"}]}}

    def review(self, plan, chain, availability=None, observed=None):
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _review_observed_measurement_scope
        state = {"user_id": "synthetic-user", "timezone_iana": "Asia/Seoul",
                 "availability_context": availability}
        with patch("chatbot.services.sensor_inventory.observe_sensor_schema", return_value=observed) as probe:
            result = _review_observed_measurement_scope(plan, plan, state, chain,
                        {"question": "An unqualified attribute of a workout"},
                        "An unqualified attribute of a workout", {}, None)
        return result, probe

    def test_llm_sees_observed_schema_before_cross_stream_scope_is_final(self):
        plan = self.fixture()
        revised = self.fixture()
        revised["metrics"] = ["workout_steps", "workout_average_heart_rate"]
        revised["computation"]["conditions"][0]["metric"] = "workout_average_heart_rate"
        observed = {"status": "observed", "observations": [{"resource": "workout.sessions", "AverageHeartRateBpmRows": 2}]}
        chain = Mock()
        chain.invoke.return_value = json.dumps(revised)
        (checked, raw, trace), probe = self.review(plan, chain, {"WORKOUT": {}}, observed)
        self.assertTrue(trace["accepted"])
        self.assertEqual(checked["computation"]["conditions"][0]["metric"], "workout_average_heart_rate")
        self.assertEqual(checked["computation"]["conditions"][0]["value"], 12)
        feedback = json.loads(chain.invoke.call_args.args[0]["validation_feedback"])
        self.assertEqual(feedback["observed_schema"]["resource_fields"], [{
            "resource": "workout.sessions", "fields": ["AverageHeartRateBpm"]}])
        self.assertNotIn("AverageHeartRateBpmRows", str(feedback["observed_schema"]))
        self.assertEqual(trace["schema_observations"], observed)
        self.assertEqual(probe.call_args.args[0], "synthetic-user")
        self.assertNotIn("answer", feedback)
        # A scope review should interpret the original request independently,
        # not anchor on the candidate interpretation it is meant to check.
        self.assertNotIn("previous_plan", feedback)
        self.assertEqual(chain.invoke.call_args.args[0]["question"],
                         "An unqualified attribute of a workout")

    def test_unknown_or_failed_schema_does_not_change_plan(self):
        plan, chain = self.fixture(), Mock()
        (checked, raw, trace), probe = self.review(plan, chain)
        self.assertIs(checked, plan)
        probe.assert_not_called()
        chain.invoke.assert_not_called()
        (checked, raw, trace), probe = self.review(plan, chain, {"WORKOUT": {}}, {"status": "unavailable"})
        self.assertIs(checked, plan)
        chain.invoke.assert_not_called()

    def test_invalid_review_metric_objects_preserve_the_original_plan(self):
        plan, chain = self.fixture(), Mock()
        invalid = {**plan, "metrics": [{"metric": "workout_steps", "unit": "count"}]}
        chain.invoke.return_value = json.dumps(invalid)
        (checked, raw, trace), _ = self.review(
            plan, chain, {"WORKOUT": {}}, {"status": "observed"})
        self.assertIs(checked, plan)
        self.assertFalse(trace["accepted"])
        self.assertEqual(trace["review_errors"][0]["code"], "unsupported_review_metrics")

    def test_scope_patch_rebuilds_dependencies_without_replacing_owned_dates(self):
        plan, chain = self.fixture(), Mock()
        computation = self.fixture()["computation"]
        computation["conditions"][0]["metric"] = "workout_average_heart_rate"
        chain.invoke.return_value = json.dumps({"computation": computation,
            "assumptions": ["Same session observations"], "needs_followup": False,
            "followup_question": "", "time_range": {"start_date": "2099-01-01"}})
        (checked, raw, trace), _ = self.review(
            plan, chain, {"WORKOUT": {}}, {"status": "observed"})
        self.assertTrue(trace["accepted"])
        self.assertEqual(trace["review_output_kind"], "scope_patch")
        self.assertEqual(checked["metrics"], ["workout_steps", "workout_average_heart_rate"])
        self.assertEqual(checked["time_range"], plan["time_range"])

    def test_review_failure_preserves_original_scope_and_query_ready_plan(self):
        plan, chain = self.fixture(), Mock()
        chain.invoke.side_effect = RuntimeError("synthetic review failure")
        (checked, raw, trace), probe = self.review(plan, chain, {"WORKOUT": {}}, {"status": "observed"})
        self.assertIs(checked, plan)
        self.assertEqual(trace["reason"], "review_unavailable")

    def test_reviewer_can_request_clarification_without_executing_a_guess(self):
        plan, chain = self.fixture(), Mock()
        revised = {**plan, "needs_followup": True, "followup_question": "Which measurement scope?"}
        chain.invoke.return_value = json.dumps(revised)
        (checked, raw, trace), probe = self.review(plan, chain, {"WORKOUT": {}}, {"status": "observed"})
        self.assertTrue(checked["needs_followup"])
        self.assertTrue(trace["accepted"])

    def test_reviewed_dependencies_reselect_local_table_clock(self):
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _review_observed_measurement_scope
        plan = self.fixture()
        reviewed = self.fixture()
        reviewed["metrics"] = ["workout_steps", "workout_average_heart_rate"]
        reviewed["computation"]["conditions"][0]["metric"] = "workout_average_heart_rate"
        chain = Mock()
        chain.invoke.return_value = json.dumps(reviewed)
        clock = EvaluationTimeContext(summary_min_date=date(2022,8,1), summary_reference_date=date(2022,8,29),
                    workout_min_date=date(2022,8,3), workout_reference_date=date(2022,8,12))
        state = {"user_id": "synthetic-user", "timezone_iana": "Asia/Seoul", "availability_context": {"WORKOUT": {}}}
        with use_evaluation_time(clock), patch("chatbot.services.sensor_inventory.observe_sensor_schema", return_value={"status": "observed"}):
            checked, _, trace = _review_observed_measurement_scope(plan, plan, state, chain, {},
                "A workout question", {}, {"mode": "all_available", "anchor": "all_available"})
        self.assertTrue(trace["accepted"])
        self.assertEqual(checked["time_range"]["end_date"], "2022-08-12")
        self.assertEqual(checked["time_range"]["start_date"], "2022-08-03")

    def test_explicit_separate_stream_can_be_preserved_despite_session_data(self):
        plan, chain = self.fixture(), Mock()
        chain.invoke.return_value = json.dumps(plan)
        (checked, raw, trace), _ = self.review(plan, chain, {"WORKOUT": {}}, {"status": "observed"})
        self.assertTrue(trace["accepted"])
        self.assertEqual(checked["computation"]["conditions"][0]["metric"], "heart_rate")


class ReferencedDataClockTests(unittest.TestCase):
    def setUp(self):
        self.clock = EvaluationTimeContext(date(2022, 8, 31), date(2022, 8, 1),
                                           date(2022, 8, 12), date(2022, 8, 3))

    def test_discovery_reads_table_dates_not_specific_activity_or_metric(self):
        port = Mock()
        port.execute_query.side_effect = [row("2022-08-01", "2022-08-31"),
                                          row("2022-08-03", "2022-08-12")]
        result = discover_referenced_data_clock("synthetic", port=port, today=date(2026, 10, 11))
        self.assertEqual(result.context.bounds("workout"), (date(2022, 8, 3), date(2022, 8, 12)))
        self.assertEqual(result.context.bounds("summary"), (date(2022, 8, 1), date(2022, 8, 31)))
        self.assertEqual(result.mode, "referenced-data")
        self.assertEqual(port.execute_query.call_count, 2)
        for query in result.queries:
            self.assertTrue(inspect_query_safety(query, "synthetic")["is_safe"])
            self.assertNotIn("ActivityName", query)
            self.assertNotIn("DateTo)", query)

    def test_unknown_daytime_types_still_contribute_to_summary_table_clock(self):
        query = build_referenced_clock_metadata_kql("synthetic", date(2022, 8, 1), date(2022, 9, 1), "summary")
        self.assertNotIn("Type in (", query)
        self.assertIn('not(Type startswith "SLEEP_")', query)
        self.assertNotIn("IngestionTime", query)

    def test_v1_workout_and_cross_conditions_choose_dependencies(self):
        self.assertEqual(evaluation_time_family({"metrics": ["workout_speed"]}), "workout")
        plan = {"metrics": ["workout_speed"], "computation": {"entity": "workout_session",
                 "conditions": [{"metric": "rmssd"}]}}
        self.assertEqual(evaluation_time_family(plan), "combined")
        self.assertEqual(self.clock.bounds("combined"), (date(2022, 8, 1), date(2022, 8, 31)))
        self.assertEqual(evaluation_time_family({"metrics": ["steps"],
                         "resources_priority": ["activity.daily", "workout.sessions"]}), "summary")

    def test_workout_newer_than_summary_cross_uses_maximum(self):
        clock = EvaluationTimeContext(date(2022, 8, 12), date(2022, 8, 1),
                                      date(2022, 8, 31), date(2022, 8, 3))
        self.assertEqual(clock.bounds("combined")[1], date(2022, 8, 31))

    def test_graph_resolves_workout_relative_window_after_semantic_planning(self):
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _plan_node_result
        from chatbot.policies.kql_generation import _kql_phone_window_for_plan
        plan = {"schema_version": 2, "metrics": ["workout_duration"],
                "computation": {"entity": "workout_session"}, "timezone_iana": "Asia/Seoul",
                "time_range": {"mode": "relative", "value": 7, "unit": "day", "anchor": "rolling"}}
        with use_evaluation_time(self.clock):
            result = _plan_node_result(plan, {}, plan)
            self.assertEqual(_kql_phone_window_for_plan(result["plan"]),
                             ("datetime(2022-08-05)", "datetime(2022-08-12)"))
            self.assertEqual(result["plan"]["reference_clock_local"], "2022-08-12")
        self.assertIsNone(active_evaluation_time())

    def test_live_graph_keeps_real_time_even_after_a_local_evaluation(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _plan_node_result
        from chatbot.policies.reactive_query_scope import reference_clock_local
        with use_evaluation_time(self.clock):
            self.assertEqual(reference_clock_local({"metrics": ["workout_speed"]}), "2022-08-12")
        plan = {"metrics": ["workout_speed"], "timezone_iana": "Asia/Seoul",
                "time_range": {"mode": "relative", "value": 1, "unit": "day", "anchor": "today"}}
        live = _plan_node_result(plan, {}, plan)["plan"]
        self.assertEqual(live["time_range"]["mode"], "relative")
        self.assertEqual(reference_clock_local(live)[:10], datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat())

    def test_trace_records_question_clock_and_both_table_clocks(self):
        answer = SimpleNamespace(text="answer", execution_status="success", payload={"final_state": {
            "plan": {"metrics": ["workout_speed"]}, "kql_query": "query"}})
        trace = trace_record(answer, "synthetic", "model", "question", self.clock)
        self.assertEqual(trace["clock"]["as_of"], "2022-08-12")
        self.assertEqual(trace["clock"]["table_clocks"]["summary"]["as_of"], "2022-08-31")

    def test_unavailable_family_is_disclosed_not_claimed_observed(self):
        port = Mock()
        port.execute_query.side_effect = [row("2022-08-01", "2022-08-31"), row(None, None, 0)]
        result = discover_referenced_data_clock("synthetic", port=port, today=date(2026, 10, 11))
        self.assertEqual(result.missing_families, ("workout",))
        self.assertIn("No clock evidence", result.limitation)

    def test_no_evidence_fails_closed_and_current_mode_does_not_discover(self):
        port = Mock()
        port.execute_query.return_value = row(None, None, 0)
        with self.assertRaises(LocalClockDiscoveryError):
            discover_referenced_data_clock("synthetic", port=port, today=date(2026, 10, 11))
        port.reset_mock()
        context, details = resolve_local_clock("synthetic", "current", port=port)
        self.assertIsNone(context)
        self.assertEqual(details["mode"], "current")
        port.execute_query.assert_not_called()

    def test_dependent_date_lookup_does_not_arbitrarily_select_a_target_record(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _selection_evidence_conflicts
        plan = {"computation": {"entity": "day", "outputs": [{"metric": "rmssd", "aggregation": "none"}],
                "filters": [{"dimension": "activity_name", "operator": "eq", "value": "Rowing"}],
                "selector": {"mode": "last", "n": 1}}}
        query = ('let Targets = Samples | summarize arg_max(DateTo, ValueNumber) by Day; '
                 'SelectedDates | join kind=leftouter Targets on Day '
                 '| project Period=Day, EvidenceRows=1, ResultStatus="ok"')
        self.assertTrue(_selection_evidence_conflicts(query, plan))
        # Revision selection retains the same observations rather than picking
        # one observation by its event timestamp.
        self.assertEqual(_selection_evidence_conflicts(
            query.replace('arg_max(DateTo, ValueNumber)', 'arg_max(IngestionTime, *)'), plan), [])

    def test_alias_repair_preserves_all_references_and_payload_text(self):
        from chatbot.policies.kql_safety import repair_query_deterministically
        query = ('let Eligible=Samples | project Date=startofday(DateFrom) | distinct Date;\n'
                 'Targets | extend Date=startofday(DateFrom), SourceDate=Payload.date\n'
                 '| join kind=leftsemi Eligible on Date\n'
                 '| project Period=Date, Note="date", DateFrom\n'
                 '// keep date in comment\n| sort by Date asc')
        fixed, repairs = repair_query_deterministically(query)
        self.assertIn('distinct metric_date', fixed)
        self.assertIn('on metric_date', fixed)
        self.assertIn('Period=metric_date', fixed)
        self.assertIn('SourceDate=Payload.date', fixed)
        self.assertIn('Note="date"', fixed)
        self.assertIn('// keep date in comment', fixed)
        self.assertIn('DateFrom', fixed)
        self.assertTrue(repairs)

    def test_unrequested_physiology_subtype_is_not_a_missing_stream_fallback(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _measurement_scope_conflicts
        plan = {"metrics": ["heart_rate"], "computation": {
            "entity": "sample", "outputs": [{"metric": "heart_rate", "aggregation": "mean"}]}}
        self.assertTrue(_measurement_scope_conflicts(
            'Samples | where Type == "RESTING_HEART_RATE"', plan))
        self.assertEqual(_measurement_scope_conflicts(
            'Samples | where Type == "HEART_RATE"', plan), [])
        self.assertEqual(_measurement_scope_conflicts(
            '// where Type == "RESTING_HEART_RATE"\nSamples | where Type == "HEART_RATE"', plan), [])
        plan["metrics"].append("phone_resting_heart_rate")
        self.assertEqual(_measurement_scope_conflicts(
            'Samples | where Type == "RESTING_HEART_RATE"', plan), [])

    def test_activity_energy_scopes_are_not_interchangeable(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _measurement_scope_conflicts
        plan = {"metrics": ["calories"]}
        self.assertTrue(_measurement_scope_conflicts(
            'Samples | where Type == "TOTAL_CALORIES_BURNED"', plan))
        self.assertEqual(_measurement_scope_conflicts(
            'Samples | where Type == "ACTIVE_ENERGY_BURNED"', plan), [])

    def test_condition_dependency_cannot_be_replaced_by_a_session_attribute(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _measurement_scope_conflicts
        plan = {"metrics": ["workout_steps", "calories"], "computation": {
            "conditions": [{"metric": "calories", "operator": "gt", "value": 29, "unit": "native"}]}}
        self.assertTrue(_measurement_scope_conflicts(
            'Sessions | where CaloriesKcal > 29 | summarize sum(Steps)', plan))
        self.assertEqual(_measurement_scope_conflicts(
            'Flat | where Type in ("ACTIVE_ENERGY_BURNED") | where ValueNumber > 29', plan), [])
        plan["computation"]["conditions"][0]["metric"] = "workout_calories"
        plan["metrics"] = ["workout_steps", "workout_calories"]
        self.assertEqual(_measurement_scope_conflicts(
            'Sessions | where CaloriesKcal > 29 | summarize sum(Steps)', plan), [])

    def test_empty_rollup_feedback_depends_on_successful_inventory(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _empty_rollup_conflicts
        from chatbot.policies.semantic_resources import prompt_resource_values
        query = prompt_resource_values()["activity_daily_resource"] + '(TargetUserId, StartLocal, EndLocal)'
        for daily_status in ("observed", "unknown", "unavailable"):
            self.assertEqual(_empty_rollup_conflicts(query, {"resource_status": {
                "activity.daily": daily_status, "health.samples": "observed"}}), [])
        self.assertTrue(_empty_rollup_conflicts(query, {"resource_status": {
            "activity.daily": "empty", "health.samples": "observed"}}))
        self.assertEqual(_empty_rollup_conflicts(query, {"resource_status": {
            "activity.daily": "empty", "health.samples": "unknown"}}), [])

    def test_historical_metadata_is_not_erased_by_current_freshness(self):
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _format_user_available_metrics
        metadata = {"WORKOUT": {"is_available": False, "latest_date_to_local": "2022-06-19"},
                    "RESTING_HEART_RATE": {"is_available": False, "latest_date_to_local": "2022-06-21"}}
        text = _format_user_available_metrics(metadata)
        self.assertIn("workout_average_heart_rate", text)
        self.assertIn("phone_resting_heart_rate", text)
        self.assertIn("Currently available keys: none", text)
        self.assertIn("not proof of current measurements", text)
        self.assertFalse(metadata["WORKOUT"]["is_available"])

    def test_structured_session_availability_uses_record_type_not_scalar_expression(self):
        from chatbot.policies.availability import metric_availability_keys
        self.assertIn("WORKOUT", metric_availability_keys("workout_average_heart_rate"))
        self.assertIn("SLEEP_SESSION", metric_availability_keys("total_sleep_time"))

    def test_query_model_gets_field_meanings_not_catalog_query_templates(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _metric_schema_context
        context = _metric_schema_context({"metrics": ["workout_average_heart_rate"],
            "computation": {"conditions": [{"metric": "calories"}]}})
        by_metric = {entry["metric"]: entry for entry in context}
        self.assertEqual(by_metric["calories"]["catalog_flat_types"], ["ACTIVE_ENERGY_BURNED"])
        self.assertEqual(by_metric["workout_average_heart_rate"]["logical_resource"], "workout.sessions")
        self.assertNotIn("summary", str(context))
        self.assertNotIn("sumif", str(context))

    def test_measurement_scope_does_not_override_authorization_guard(self):
        import importlib
        from unittest.mock import patch
        node = importlib.import_module("chatbot.nodes.d1_sensor_data.guard_sensor_query")
        query = (
            "let TargetUserId='synthetic-account'; "
            "SensorHealthSamples(TargetUserId, datetime(2025-04-02), datetime(2025-04-09)) "
            "| where UserId == TargetUserId and Type == 'RESTING_HEART_RATE' "
            "| summarize Value=avg(ValueNumber)"
        )
        state = {"user_id": "synthetic-account", "kql_query": query,
                 "plan": {"metrics": ["heart_rate"]}, "kql_generation_trace": {"method": "llm"},
                 "repair_attempts": 1, "max_repair_attempts": 1}
        with patch("chatbot.nodes.d1_sensor_data.generate_sensor_query._measurement_scope_conflicts",
                   side_effect=AssertionError("Question semantics do not belong in guard")) as review:
            result = node.guard_sensor_query(state)
        self.assertEqual(result["guard_status"], "execute")
        self.assertIsNone(result["error"])
        self.assertEqual(result["kql_query"], query)
        review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
