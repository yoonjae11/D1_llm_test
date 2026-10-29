"""Aggregate domain wrappers without writing the user-facing answer."""
from chatbot.schemas.domain_execution_result import DomainExecutionResult

def aggregate_domain_results(state: dict) -> dict:
    results = {
        key: DomainExecutionResult.model_validate(value).model_dump()
        for key, value in (state.get("domain_results") or {}).items()
    }
    d2 = results.get("D2", {})
    d3 = results.get("D3", {})
    d4 = results.get("D4", {})
    trace = dict(state.get("orchestration_trace") or {})
    trace["domains"] = {
        key: {"selected": value["selected"], "status": value["status"],
              "summary": value["summary"], "provenance": value["provenance"]}
        for key, value in results.items()
    }
    update = {
        "domain_results": results,
        "coaching_strategy": (
            d2.get("structured_result")
            if d2.get("status") == "success"
            else None
        ),
        "health_knowledge_result": d3.get("structured_result") if d3.get("status") == "success" else None,
        "resolved_context": d4.get("structured_result") if d4.get("status") == "success" else None,
        "orchestration_trace": trace,
        "response_domain_context": {
            key: {"status": value["status"], "summary": value["summary"],
                  "structured_result": value["structured_result"] if (
                      value["status"] == "success" or (
                          key == "D1" and value["status"] == "insufficient_information"
                          and value["provenance"].get("execution_status") == "success"
                      )
                  ) else {},
                  "missing_information": value["missing_information"]}
            for key, value in results.items() if value["selected"]
        },
    }
    # Keep existing D1 error wording even when failure occurred outside its nodes.
    if results.get("D1", {}).get("status") == "error" and not state.get("error"):
        update.update({"error": "Sensor domain execution failed", "query_result": "[]", "execution_status": "error"})
    return update
