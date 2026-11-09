"""
CORE-4.5 diagnostic — one controlled replay of the chooser request Groq answered 400.

During the live chain, the step-chooser request made AFTER a failed search_products (SerpApi
timeout) was answered HTTP 400 Bad Request — a non-transient refusal the app correctly did not
retry. Before that can be called a provider quirk or a provider defect, Groq's own error body has
to be read. This probe makes that ONE call: the same canonical context shape the loop sends after
a failed tool, the same declarations, the same wire code — and prints Groq's error message.

Nothing consequential is reachable: no app, no database, no tool execution; the answer is parsed,
never run. The credential is never printed; the error body is scanned for it before display.

    docker compose run --rm --no-deps -e PYTHONIOENCODING=utf-8 -e REASONING_PROVIDER=groq \
        -v "$(pwd)/probe_core45_groq_400.py:/app/probe_core45_groq_400.py:ro" \
        backend python probe_core45_groq_400.py
"""
import asyncio
import json
import sys

import httpx

from app.agents import reasoning_provider as provider_module
from app.agents.reasoning_provider import GroqReasoningProvider
from app.agents.tools import declarations_for_model
from app.config import get_settings


def post_failure_context() -> dict:
    """The canonical context as the loop builds it after a failed search_products step."""
    return {
        "mission": {
            "mission_id": "core45-probe",
            "mission_type": "shopping",
            "status": "needs_clarification",
            "resolution_summary": None,
        },
        "shopping": {
            "intent_state": "needs_clarification",
            "category": "shirts",
            "budget": "700",
            "currency": "INR",
            "brand": None,
            "preferences": ["white", "office use", "office"],
            "size_or_variant": None,
            "missing_info": [],
            "clarification_prompt": None,
            "discovered_products": [
                {
                    "product_id": f"probe-product-{index}",
                    "title": f"Men's Solid Casual White Shirt option {index}",
                    "price": f"{700 + index * 50}",
                    "currency": "INR",
                    "merchant": "Example Merchant",
                    "source": "google_shopping",
                    "requires_variant_clarification": False,
                    "variant_options": [],
                }
                for index in range(1, 6)
            ],
            "selected_product": None,
            "checkout_confirmed_by_user": False,
            "payment_status": None,
        },
        "order": None,
        "issue": None,
        "open_issues": [],
        "assessment": None,
        "resolution": None,
        "authority": {
            "delegated": False,
            "allowed_actions": [],
            "recovery_budget": "0",
            "spent_so_far": "0",
            "remaining_budget": "0",
            "refund_limit": None,
            "expires_at": None,
        },
        "conversation": [
            {"role": "user", "content": "Find me white shirts under ₹1000."},
            {"role": "agent", "content": "I found 5 white shirts under ₹1000."},
            {"role": "user", "content": "Which one would you recommend for office use?"},
            {"role": "agent", "content": "The Peter England option at ₹800 suits office use."},
            {"role": "user", "content": "What about the second one?"},
            {"role": "agent", "content": "The second shirt is priced at ₹800."},
        ],
        "current_user_message": "Show me ones below ₹700 instead.",
        "this_turn": {
            "steps_used": 1,
            "steps_remaining": 7,
            "tools_already_called": [
                {
                    "tool": "search_products",
                    "success": False,
                    "state": "needs_clarification",
                    "error_code": "search_failed",
                }
            ],
        },
        "boundaries": {
            "payment": "Payment is a human boundary. RESOLVE can prepare a checkout review; the user confirms it and completes the payment themselves.",
            "authority": "The Authority Engine decides whether a remedy may run. A proposal is never permission.",
            "grounding": "Say only what this context supports.",
        },
    }


async def main() -> int:
    settings = get_settings()
    api_key = settings.reasoning_groq_api_key or settings.reasoning_api_key
    if not api_key:
        print("STOP: no Groq key configured; nothing is printed about it.")
        return 1
    settings.reasoning_max_retries = 0  # one attempt, by construction

    provider = GroqReasoningProvider(api_key=api_key, model=settings.reasoning_groq_model)
    declarations = declarations_for_model()
    try:
        decision = await provider.choose_agent_action(post_failure_context(), declarations)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:800]
        if api_key in body:
            body = body.replace(api_key, "[redacted]")
        print(f"HTTP {exc.response.status_code}")
        print(f"error body       : {body}")
        print("RESULT           : the 400 is reproducible with a post-failed-tool context; "
              "the body above names the reason.")
        return 2
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)[:300].replace(api_key, "[redacted]")
        print(f"{type(exc).__name__}: {detail}")
        return 3
    print(f"answered cleanly : action={decision.action} "
          f"tool={decision.tool_name} message={str(decision.message)[:160]}")
    print("RESULT           : the post-failed-tool chooser request is accepted now — the earlier "
          "400 did not reproduce with this context shape.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
