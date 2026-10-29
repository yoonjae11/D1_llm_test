"""LangGraph node that requests a timezone before answering data questions.

This node only runs when strict timezone onboarding is enabled and the user has
no resolved timezone (see ``prepare_context``). It replies with the site-selection
menu instead of running the plan/KQL/ADX path, so a health-data question is never
answered against an unknown timezone.
"""

from langchain_core.messages import AIMessage

from chatbot.graph.state import AgentState
from chatbot.timezone_onboarding import build_menu_message


def onboarding_responder(state: AgentState):
    message = build_menu_message(False, intro=True)
    return {
        "messages": [AIMessage(content=message)],
        "route": "onboarding_required",
        "execution_status": "onboarding_required",
    }
