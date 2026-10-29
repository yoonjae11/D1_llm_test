"""Offline proof of the README's shared-graph pre-answer interruption."""
import unittest
from unittest.mock import Mock, patch

from langgraph.checkpoint.memory import MemorySaver
from chatbot.graph import workflow


class LocalEvaluationBoundaryTests(unittest.TestCase):
    def test_interrupt_preserves_structured_result_without_answer_or_persistence(self):
        answer = Mock(return_value={})
        persist = Mock(return_value={})
        rows = '[{"Metric":"synthetic","Value":12.5,"Unit":"COUNT"}]'
        with patch.multiple(
            workflow,
            prepare_context=lambda state: {},
            plan_domain_execution=lambda state: {"intent": "data"},
            execute_domains=lambda state: {"query_result": rows, "execution_status": "success"},
            aggregate_domain_results=lambda state: {},
            generate_final_response=answer,
            persist_trace_and_context=persist,
        ):
            graph = workflow.build_graph(checkpointer=MemorySaver())
            config = {"configurable": {"thread_id": "synthetic-evaluation-boundary"}}
            graph.invoke({"user_id": "synthetic", "messages": []}, config=config,
                         interrupt_before=["generate_final_response"])
            snapshot = graph.get_state(config)
        self.assertEqual(snapshot.next, ("generate_final_response",))
        self.assertEqual(snapshot.values["query_result"], rows)
        answer.assert_not_called()
        persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
