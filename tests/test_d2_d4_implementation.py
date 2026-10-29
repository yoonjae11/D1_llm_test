"""Offline contract tests for the D2-D4 implementation work specification."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from langchain_core.messages import HumanMessage

from chatbot.nodes.d2_coaching_strategy.validate_coaching_plan import validate_coaching_strategy_plan
from chatbot.nodes.d3_health_knowledge.workflow import run_d3
from chatbot.nodes.d4_multiturn_context.workflow import run_d4
from chatbot.nodes.common.generate_final_response import generate_final_response
from chatbot.resources.coaching.bct_catalog import load_bct_catalog
from chatbot.resources.coaching.d2_source_registry import load_d2_source_registry
from chatbot.resources.health_knowledge.registry import ROOT, load_approved_corpus
from chatbot.schemas.d2_coaching_strategy_plan import D2CoachingStrategyPlan, EvidenceRef


class D2TraceabilityTests(unittest.TestCase):
    def test_non_coaching_request_is_normalized_to_information_only(self):
        plan = D2CoachingStrategyPlan(
            coaching_applicable=False, interaction_depth="COACHING_CONVERSATION",
            dialogue_objective="COLLABORATIVE_PLANNING", comb_status="IDENTIFIED",
            comb_components=["REFLECTIVE_MOTIVATION"], comb_evidence_summary="unsupported",
            selected_bcts=["1.1"], bct_rationale={"1.1": "unsupported"}, strategy_rationale="test",
        )
        validated, _ = validate_coaching_strategy_plan(plan, load_bct_catalog())
        self.assertEqual(validated.interaction_depth, "INFORMATION_ONLY")
        self.assertEqual(validated.dialogue_objective, "NONE")
        self.assertEqual(validated.selected_bcts, [])

    def test_identified_barrier_without_evidence_ref_becomes_unknown(self):
        plan = D2CoachingStrategyPlan(
            coaching_applicable=True, interaction_depth="BRIEF_COACHING",
            dialogue_objective="COLLABORATIVE_PLANNING", comb_status="IDENTIFIED",
            comb_components=["PHYSICAL_OPPORTUNITY"], comb_evidence_summary="Schedule appears difficult.",
            barrier_relevant=True, selected_bcts=[], strategy_rationale="test",
        )
        validated, _ = validate_coaching_strategy_plan(plan, load_bct_catalog())
        self.assertEqual(validated.comb_status, "UNKNOWN")
        self.assertEqual(validated.dialogue_objective, "EXPLORE_BARRIER")

    def test_missing_source_registry_is_explicit(self):
        with self.assertRaises(FileNotFoundError):
            load_d2_source_registry(Path("definitely-missing-d2-registry.json"))

    def test_registry_distinguishes_source_and_operational_rules(self):
        origins = {item.rule_origin for item in load_d2_source_registry().sources}
        self.assertEqual(origins, {"SOURCE_DERIVED", "SENSEI_OPERATIONAL"})

    def test_subset_is_explicit_and_not_complete_taxonomy(self):
        catalogue = load_bct_catalog()
        rationale = json.loads((Path(__file__).parents[1] / "chatbot/resources/coaching/bct_subset_rationale.json").read_text())
        self.assertEqual(len(catalogue.entries), 38)
        self.assertIn("not the complete 93-technique", rationale["scope_statement"])

    def test_validator_removes_unknown_bct_and_unallowlisted_evidence(self):
        plan = D2CoachingStrategyPlan(
            coaching_applicable=True, interaction_depth="BRIEF_COACHING",
            dialogue_objective="COLLABORATIVE_PLANNING", comb_status="IDENTIFIED",
            comb_components=["REFLECTIVE_MOTIVATION"], comb_evidence_summary="User said motivation is low.",
            comb_evidence_refs=[EvidenceRef(source_domain="D1", evidence_id="D1:invented")],
            selected_bcts=["1.1", "99.9"], bct_rationale={"1.1": "Supports a chosen goal", "99.9": "invalid"},
            bct_evidence_refs={"1.1": [EvidenceRef(source_domain="USER", evidence_id="USER_CURRENT_TURN")]},
            strategy_rationale="Collaborative planning was directly requested.",
        )
        validated, _ = validate_coaching_strategy_plan(
            plan, load_bct_catalog(), concrete_plan_requested=True,
            allowed_evidence_ids={"USER_CURRENT_TURN"}, d1_success=False,
        )
        self.assertEqual(validated.selected_bcts, ["1.1"])
        self.assertEqual(validated.comb_evidence_refs, [])

    def test_d3_emergency_precedence_disables_coaching(self):
        plan = D2CoachingStrategyPlan(
            coaching_applicable=True, interaction_depth="BRIEF_COACHING",
            dialogue_objective="EXPLORE_GOAL", comb_status="NOT_APPLICABLE",
            selected_bcts=["1.1"], bct_rationale={"1.1": "goal"}, strategy_rationale="test",
        )
        validated, _ = validate_coaching_strategy_plan(
            plan, load_bct_catalog(), allowed_evidence_ids={"USER_CURRENT_TURN"},
            d3_safety_level="EMERGENCY",
        )
        self.assertFalse(validated.coaching_applicable)
        self.assertEqual(validated.selected_bcts, [])


class D3KnowledgeTests(unittest.TestCase):
    def test_only_approved_sources_are_loaded(self):
        _, documents = load_approved_corpus()
        self.assertTrue(documents)
        self.assertEqual({item["source_id"] for item in documents}, {"NHS_UK"})

    def test_unknown_topic_abstains(self):
        output = run_d3({"messages": [HumanMessage(content="Tell me about an unsupported obscure topic")]}).result
        self.assertEqual(output.status, "insufficient_information")
        self.assertEqual(output.structured_result["status"], "INSUFFICIENT_EVIDENCE")

    def test_chest_pain_red_flags_escalate(self):
        output = run_d3({"messages": [HumanMessage(content="I have chest pain and shortness of breath")]}).result
        self.assertEqual(output.structured_result["status"], "SAFETY_ESCALATION")
        self.assertIn("999", output.structured_result["safety_message"])
        self.assertTrue(output.structured_result["escalation_required"])
        self.assertTrue(output.provenance["retrieved_chunk_ids"])

    def test_response_composer_preserves_safety_precedence(self):
        d3 = run_d3({"messages": [HumanMessage(content="I have chest pain and shortness of breath")]}).result.model_dump()
        answer = generate_final_response({
            "intent": "chat", "messages": [HumanMessage(content="Help me push through it")],
            "domain_results": {"D2": {"selected": True, "status": "success"}, "D3": d3},
        })["messages"][-1].content
        self.assertIn("999", answer)
        self.assertNotIn("goal", answer.lower())

    def test_tampered_snapshot_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "snapshots").mkdir()
            for name in ("source_registry.json", "corpus_manifest.json"):
                (root / name).write_text((ROOT / name).read_text(), encoding="utf-8")
            for source in (ROOT / "snapshots").glob("*.json"):
                payload = json.loads(source.read_text())
                if source.name == "nhs_chest_pain.json":
                    payload["chunks"][0]["text"] += " tampered"
                (root / "snapshots" / source.name).write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_approved_corpus(root)


class D4ContextTests(unittest.TestCase):
    def test_prior_barrier_is_resolved_from_source_turn(self):
        output = run_d4({
            "messages": [HumanMessage(content="What barrier did I mention earlier?")],
            "conversation_recent_turns": [
                {"turn_id": "T2", "role": "user", "message_text": "Evening exercise is hard because I am too tired.", "event_time_utc": "2026-01-01T00:00:00Z"},
            ],
        }).result
        item = output.structured_result["resolved_items"][0]
        self.assertEqual(item["item_type"], "BARRIER")
        self.assertEqual(item["source_turn_ids"], ["T2"])

    def test_prior_query_condition_uses_declared_active_context(self):
        output = run_d4({
            "messages": [HumanMessage(content="Use the same period as the prior query.")],
            "conversation_active_context": {"metrics": ["steps"], "time_range": {"days": 7}},
        }).result
        item = output.structured_result["resolved_items"][0]
        self.assertEqual(item["item_type"], "QUERY_CONDITION")
        self.assertTrue(item["source_turn_ids"][0].startswith("active_"))

    def test_latest_goal_supersedes_older_value(self):
        output = run_d4({
            "messages": [HumanMessage(content="Compare last week with the goal I set earlier.")],
            "conversation_recent_turns": [
                {"turn_id": "T5", "role": "user", "message_text": "My goal is 8,000 steps.", "event_time_utc": "2026-01-01T00:00:00Z"},
                {"turn_id": "T12", "role": "user", "message_text": "Change my goal to 6,000 steps.", "event_time_utc": "2026-01-02T00:00:00Z"},
            ],
        }).result
        item = output.structured_result["resolved_items"][0]
        self.assertEqual(item["value"], 6000)
        self.assertEqual(item["source_turn_ids"], ["T12"])
        self.assertEqual(item["supersedes_turn_ids"], ["T5"])
        self.assertTrue(output.structured_result["latest_value_applied"])

    def test_ambiguous_then_is_not_guessed(self):
        output = run_d4({
            "messages": [HumanMessage(content="How was it then?")],
            "conversation_recent_turns": [
                {"turn_id": "T1", "role": "user", "message_text": "I walked Monday.", "event_time_utc": "2026-01-01T00:00:00Z"},
                {"turn_id": "T2", "role": "user", "message_text": "I slept Tuesday.", "event_time_utc": "2026-01-02T00:00:00Z"},
            ],
        }).result
        self.assertEqual(output.structured_result["status"], "AMBIGUOUS")
        self.assertEqual(output.structured_result["resolved_items"], [])

    def test_nonexistent_prior_claim_is_rejected(self):
        output = run_d4({
            "messages": [HumanMessage(content="I told you my goal was 5,000 steps earlier.")],
            "conversation_recent_turns": [
                {"turn_id": "T1", "role": "user", "message_text": "My goal is 8,000 steps.", "event_time_utc": "2026-01-01T00:00:00Z"},
            ],
        }).result
        self.assertEqual(output.status, "insufficient_information")
        self.assertTrue(output.structured_result["hallucinated_context_detected"])

    def test_reply_to_has_priority_and_source_id(self):
        output = run_d4({
            "messages": [HumanMessage(content="What about that?")],
            "reply_to_text": "Your walking goal was 20 minutes.",
            "conversation_recent_turns": [{"role": "assistant", "message_text": "A different topic", "event_time_utc": "2026-01-01T00:00:00Z"}],
        }).result
        item = output.structured_result["resolved_items"][0]
        self.assertEqual(item["resolution"], "EXPLICIT_REPLY")
        self.assertTrue(item["source_turn_ids"][0].startswith("reply_"))

    def test_missing_referent_requests_clarification(self):
        output = run_d4({"messages": [HumanMessage(content="What about that?")]}).result
        self.assertEqual(output.status, "insufficient_information")
        self.assertTrue(output.structured_result["clarification_question"])


if __name__ == "__main__":
    unittest.main()
