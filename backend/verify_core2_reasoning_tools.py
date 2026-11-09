"""
CORE-2 live preflight, part 1: does the real reasoning provider accept the declared tool surface?

Runs inside the backend container, so it reads whatever provider the deployment is configured for.
It prints no credential: only whether one is set, and what the model answered.
"""
import asyncio
import json

from app.agents.reasoning_provider import RESPOND_TOOL_NAME, get_reasoning_provider
from app.agents.tools import declarations_for_model
from app.config import get_settings

CONTEXT = {
    "mission": {"mission_id": "preflight-demo", "status": "created", "state": "ready_for_discovery"},
    "shopping": {"category": "shampoo", "budget_max_inr": "600", "products": []},
    "order": None,
    "open_issues": [],
    "assessment": None,
    "resolution": None,
    "authority": {"allowed_actions": []},
    "conversation": [],
    "current_user_message": "Find me an anti-hairfall shampoo under 600 rupees.",
    "this_turn": {"steps_used": 0, "steps_remaining": 8, "tools_already_called": []},
    "boundaries": {
        "payment_is_a_human_boundary": True,
        "authority_engine_decides": True,
        "canonical_amounts_come_from_the_server": True,
    },
}


async def main() -> None:
    settings = get_settings()
    name = settings.reasoning_provider
    print(f"provider            : {name}")
    print(f"key configured      : {bool(settings.reasoning_api_key)}")
    print(f"model               : {settings.reasoning_model}")

    declared = [decl["name"] for decl in declarations_for_model()]
    print(f"declared tools      : {declared}")

    provider = get_reasoning_provider(name, settings.reasoning_api_key)
    try:
        decision = await provider.choose_agent_action(CONTEXT, declarations_for_model())
    except Exception as exc:  # noqa: BLE001 — the point of a preflight is to read the failure
        print(f"FAILED              : {type(exc).__name__}: {exc}")
        response = getattr(exc, "response", None)
        if response is not None:
            print(f"status              : {response.status_code}")
            body = response.text[:2000]
            secret = settings.reasoning_api_key or ""
            if secret:
                body = body.replace(secret, "<key>")
            # Google's error body names the offending field path; it never repeats the key.
            print(f"body                : {body}")
        raise SystemExit(1)
    print(f"decision.action     : {decision.action}")
    print(f"decision.tool_name  : {decision.tool_name}")
    print(f"decision.arguments  : {json.dumps(decision.arguments, default=str)}")
    print(f"respond tool name   : {RESPOND_TOOL_NAME}")

    if decision.action == "tool":
        assert decision.tool_name in declared, "the model named a tool that was not declared"
        assert set(decision.arguments) <= {"query", "product_id", "answer", "issue_id", "question",
                                           "issue_type", "description", "requested_resolution",
                                           "reason"}, f"undeclared argument: {decision.arguments}"
    print("RESULT              : the declared surface round-tripped through the real provider")


if __name__ == "__main__":
    asyncio.run(main())
