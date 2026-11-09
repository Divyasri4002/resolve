"""
CORE-4.5 Phase 7 — one controlled live request to the configured Groq provider.

This is the minimum live preflight, run once by hand, never part of the test suite:

    docker compose run --rm --no-deps -e PYTHONIOENCODING=utf-8 \
        backend python verify_core45_groq_preflight.py

What it proves, with a single HTTP attempt against the real vendor:
  A. authentication is accepted (the Groq key works)
  B. the configured model is available (REASONING_GROQ_MODEL)
  C. the native OpenAI-style `tools` + `tool_choice: "required"` body is accepted
  D. a declared tool call can come back (preferred: search_products)

It is one request by construction: the in-process retry budget is set to 0 for
this run, so the transport makes exactly one attempt and cannot even attempt a
second. Nothing consequential is reachable from here — no app, no database, no
tool execution; the only call is `choose_agent_action`, whose answer is parsed,
never run.

What it never does: print or persist the API key, any header, or the request
body. The key is read only to (a) check it exists, (b) assert it stayed out of
the URL and the body. If the answer is 429, this script stops — it does not
retry, and it says so.

Exit codes:
  0 — the provider answered (HTTP 200) and no credential leaked onto the wire
  2 — rate limited (429): STOP live verification; a result, not a pass
  3 — the provider refused for another transient reason (5xx family)
  1 — configuration or request problem (no key, 401/403, 404, 400),
      or a boundary broke (credential seen in URL/body)
"""
import asyncio
import json
import sys

import httpx

from app.agents import reasoning_provider as provider_module
from app.agents.reasoning_provider import GroqReasoningProvider, ReasoningUnavailable
from app.agents.tools import declarations_for_model
from app.config import get_settings

PREFLIGHT_MESSAGE = "Find me white shirts under ₹1000."


class OneShotObservation:
    """Watches the single request at the `_post_for_json` seam without altering it."""

    def __init__(self, api_key: str) -> None:
        self.attempts = 0
        self.http_status: int | None = None
        self.tool_call_name: str | None = None
        self._api_key = api_key
        self.key_in_url = False
        self.key_in_body = False

    def install(self) -> None:
        original = provider_module._post_for_json

        async def watched(url, *, headers, body, label):
            self.attempts += 1
            if self._api_key:
                self.key_in_url = self.key_in_url or self._api_key in url
                self.key_in_body = self.key_in_body or self._api_key in json.dumps(body, default=str)
            try:
                data = await original(url, headers=headers, body=body, label=label)
            except provider_module.ReasoningUnavailable as exc:
                self.http_status = exc.status_code
                raise
            except httpx.HTTPStatusError as exc:
                self.http_status = exc.response.status_code
                raise
            self.http_status = 200
            choices = (data or {}).get("choices") or []
            if choices:
                for call in (choices[0].get("message") or {}).get("tool_calls") or []:
                    name = str(((call.get("function") or {}).get("name")) or "").strip()
                    if name:
                        self.tool_call_name = name
                        break
            return data

        provider_module._post_for_json = watched


def preflight_context() -> dict:
    """A minimal, honest, canonical-shaped context for one harmless step choice."""
    return {
        "mission": {
            "mission_id": "core45-preflight",
            "mission_type": "shopping",
            "status": "ready_for_discovery",
            "resolution_summary": None,
        },
        "shopping": {
            "intent_state": "ready_for_discovery",
            "category": None,
            "budget": None,
            "currency": "INR",
            "brand": None,
            "preferences": [],
            "size_or_variant": None,
            "missing_info": [],
            "clarification_prompt": None,
            "discovered_products": [],
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
        "conversation": [],
        "current_user_message": PREFLIGHT_MESSAGE,
        "this_turn": {"steps_used": 0, "steps_remaining": 8, "tools_already_called": []},
        "boundaries": {
            "payment": "Payment is a human boundary. RESOLVE can prepare a checkout review; the user confirms it and completes the payment themselves.",
            "authority": "The Authority Engine decides whether a remedy may run. A proposal is never permission.",
            "grounding": "Say only what this context supports.",
        },
    }


async def main() -> int:
    settings = get_settings()
    print(f"reasoning provider : {settings.reasoning_provider} (this preflight targets groq directly)")
    print(f"groq model         : {settings.reasoning_groq_model}")

    api_key = settings.reasoning_groq_api_key or settings.reasoning_api_key
    if not api_key:
        print("STOP: no Groq API key is configured. Nothing about it is printed,")
        print("and no second credential is created.")
        return 1
    if not settings.reasoning_groq_api_key:
        print("note: REASONING_GROQ_API_KEY is unset; the legacy shared-key fallback is in play.")

    # Exactly one HTTP attempt: the transport reads its budget from settings, so a
    # zeroed in-process budget means one attempt, and no second one can be made.
    settings.reasoning_max_retries = 0
    print("retry budget       : 0 (this preflight makes exactly one attempt, no retry)")

    declarations = declarations_for_model()
    declared_names = [str(entry.get("name")) for entry in declarations]
    print(f"declared tools     : {len(declared_names)} -> {sorted(declared_names)}")

    observation = OneShotObservation(api_key)
    observation.install()
    provider = GroqReasoningProvider(api_key=api_key, model=settings.reasoning_groq_model)

    decision = None
    failure: str | None = None
    try:
        decision = await provider.choose_agent_action(preflight_context(), declarations)
    except ReasoningUnavailable as exc:
        failure = f"transient refusal after {exc.attempts} attempt(s): {exc}"
    except httpx.HTTPStatusError as exc:
        failure = f"HTTP {exc.response.status_code} refused the request"
    except Exception as exc:  # noqa: BLE001 — reported with a bounded, key-scanned description
        detail = str(exc)[:160]
        if api_key in detail:
            detail = detail.replace(api_key, "[redacted]")
        failure = f"{type(exc).__name__}: {detail}"

    print()
    print(f"HTTP attempts made : {observation.attempts}")
    print(f"HTTP status        : {observation.http_status}")
    if observation.tool_call_name is not None:
        print(f"native tool_call   : yes, name={observation.tool_call_name}")
    else:
        print("native tool_call   : no")

    if decision is not None:
        if decision.action == "tool":
            args = json.dumps(decision.arguments, default=str)[:160]
            print(f"parsed decision    : tool -> {decision.tool_name} args={args}")
        else:
            print(f"parsed decision    : respond/refusal -> {str(decision.message)[:170]}")
    if failure is not None:
        print(f"provider refusal   : {failure}")

    print()
    leak_free = not observation.key_in_url and not observation.key_in_body
    print(f"credential in URL  : {'LEAK' if observation.key_in_url else 'no'}")
    print(f"credential in body : {'LEAK' if observation.key_in_body else 'no'}")
    if not leak_free:
        print("BOUNDARY BROKEN: the API key reached the URL or the request body.")
        return 1
    if observation.attempts > 1:
        print("BOUNDARY BROKEN: more than one HTTP attempt left this process.")
        return 1

    status = observation.http_status
    if status == 200 and decision is not None:
        chosen = decision.tool_name if decision.action == "tool" else "(answer the user)"
        print("RESULT             : preflight answered — auth accepted, model available,")
        print(f"                     tools accepted, declared choice: {chosen}")
        return 0
    if status == 429:
        print("RESULT             : RATE LIMITED. Stopping live verification here — no")
        print("                     retry, no second key, no second provider.")
        return 2
    if status in (401, 403):
        print("RESULT             : authentication refused. Diagnose the credential")
        print("                     configuration only; nothing else is attempted.")
        return 1
    if status == 404:
        print("RESULT             : endpoint/model not found. Diagnose REASONING_GROQ_MODEL")
        print("                     or endpoint configuration only.")
        return 1
    if status is not None:
        print(f"RESULT             : refused with HTTP {status}; live verification stops here.")
        return 3
    print("RESULT             : the request never completed; live verification stops here.")
    return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
