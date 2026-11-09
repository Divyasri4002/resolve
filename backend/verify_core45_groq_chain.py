"""
CORE-4.5 Phases 8-10 — does a real Groq model drive a real, grounded shopping conversation?

Everything else in the suite scripts the model's answers. This script refuses to: it uses the
configured reasoning provider itself, and it stops rather than substituting anything if the
configured provider is not Groq. The question it answers is whether a live Groq model, through the
ONE Agent Loop and the closed tool registry, can take the milestone's brief end to end:

    "Find me white shirts under ₹1000."            (creation: intent -> decide -> real SerpApi)
    "Which one would you recommend for office use?" (a grounded, tool-free answer)
    "What about the second one?"                    (resolution against the real returned set)
    "Show me ones below ₹700 instead."              (a new constraint, observed honestly)

Grounding is measured, not asserted: the products the conversation talks about are read back from
the server (source google_shopping, from the configured search provider), and the model's answers
are checked against that set — a mentioned price must belong to a real product, and "the second
one" must land on the product the search actually returned in that position.

What this script never does, by construction: no payment endpoint, no order creation or
confirmation, no Razorpay call, no Shopify call of any kind, no credential printed. A disposable
user and mission are created. Turn 4's outcome is OBSERVED, not forced: whether Groq re-searches
with a ₹700 query or narrows in its answer, both are recorded — the architecture's support for a
new constraint is the intent re-parse plus a declared search_products tool, never a hard-coded
branch.

Run it from the compose network (it needs the real Postgres, SerpApi, and the Groq key in the
environment):

    docker compose run --rm --no-deps -e PYTHONIOENCODING=utf-8 -e REASONING_PROVIDER=groq \
        -v "$(pwd)/verify_core45_groq_chain.py:/app/verify_core45_groq_chain.py:ro" \
        backend python verify_core45_groq_chain.py

Exit 0 — a genuine grounded Groq conversation was observed.
Exit 2 — the provider refused, or too few live decisions happened. Bounded behaviour is reported;
         nothing is claimed. This is a result, not a pass.
Exit 1 — a boundary or an invariant broke. That is a real failure.
"""
import asyncio
import json
import os
import re
import sys
import uuid

import httpx
from httpx import ASGITransport

from app.agents import reasoning_provider as provider_module
from app.agents.reasoning_provider import RESPOND_TOOL_NAME
from app.agents.tools import tool_names
from app.config import get_settings

PASSWORD = "SecurePassword123!"
PROMPT = "Find me white shirts under ₹1000."
TURN_OFFICE = "Which one would you recommend for office use?"
TURN_SECOND = "What about the second one?"
TURN_BUDGET = "Show me ones below ₹700 instead."
# The free tier refills its per-minute token budget slower than a four-turn conversation spends
# it, so the VERIFIER paces its own turns. The app itself is never paced — this knob exists only
# so a 429 burst cannot be mistaken for a provider gap.
TURN_GAP_SECONDS = float(os.environ.get("LIVE_CHAIN_TURN_GAP", "30"))
TERMINAL_STATES = {"payment_pending", "payment_verified", "order_created", "order_confirmed"}
STOP_WORDS = {
    "the", "and", "for", "with", "shirt", "shirts", "pack", "packs", "of", "men", "women",
    "slim", "fit", "cotton", "solid", "free", "size", "sizes", "buy", "online", "best",
}


class Observation:
    """
    A passive record of the reasoning traffic on the OpenAI-compatible wire.

    It re-raises everything the transport re-raises, so the app's own retry and degradation
    behaviour is what runs; this only notes what went by.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.waits: list[float] = []
        self.refusals: list[str] = []
        self.statuses: list[str] = []

    def install(self) -> None:
        original_post = provider_module._post_for_json

        async def watched(url, *, headers, body, label):
            # One _post_for_json call is one model call: the entry records its own outcome, so an
            # answered step choice is counted exactly, never inferred from turn shape.
            entry = {"label": label, "url": url, "body": body, "outcome": "ok"}
            self.requests.append(entry)
            try:
                data = await original_post(url, headers=headers, body=body, label=label)
            except provider_module.ReasoningUnavailable as exc:
                entry["outcome"] = f"HTTP {exc.status_code} after {exc.attempts} attempt(s)"
                self.refusals.append(str(exc))
                self.statuses.append(f"{label} {entry['outcome']}")
                raise
            except Exception as exc:  # noqa: BLE001 — a non-transient refusal is reported, never retried
                entry["outcome"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                self.statuses.append(f"{label} {entry['outcome']}")
                raise
            return data

        original_pause = provider_module._pause

        async def watched_pause(seconds: float) -> None:
            self.waits.append(seconds)
            await original_pause(seconds)

        provider_module._post_for_json = watched
        provider_module._pause = watched_pause

    def decisions(self) -> list[dict]:
        """The step-chooser requests, in order across every turn: the only ones with `tools`."""
        return [entry for entry in self.requests if "tools" in entry["body"]]

    def context_of(self, entry: dict) -> dict:
        """The canonical agent context a request carried, parsed back out of the user message."""
        return json.loads(entry["body"]["messages"][1]["content"])

    def declared_names_of(self, entry: dict) -> set[str]:
        return {declaration["function"]["name"] for declaration in entry["body"]["tools"]}

    def describe(self) -> None:
        print(f"model calls        : {len(self.requests)} total, {len(self.decisions())} step choices")
        print(f"retry waits        : {len(self.waits)} {[round(w, 2) for w in self.waits]}")
        for status in self.statuses:
            print(f"provider answer    : {status}")
        for refusal in self.refusals:
            print(f"provider refusal   : {refusal}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    return condition


def products_of(context: dict) -> list[dict]:
    return ((context.get("shopping") or {}).get("discovered_products")) or []


def rupees(value) -> float | None:
    """A price in rupees from whatever shape the search provider recorded ('₹1,299', 1299, '1299')."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.]", "", str(value))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def significant_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z]{3,}", str(text).lower())
        if token not in STOP_WORDS
    }


def amounts_mentioned(message: str) -> list[float]:
    """Rupee amounts the model's answer names, in the shapes a human would write them."""
    amounts = [rupees(match) for match in re.findall(r"₹\s*[\d,]+(?:\.\d+)?", message)]
    amounts += [rupees(match) for match in re.findall(r"\b\d[\d,]{2,}(?:\.\d+)?\s*(?:rupees|rs\.?|inr)", message, re.IGNORECASE)]
    return [amount for amount in amounts if amount]


def positions_named(message: str, products: list[dict]) -> list[tuple[int, list[str]]]:
    """Which of the real set's positions (1-based, the order the buyer saw) the answer engages."""
    message_tokens = significant_tokens(message)
    named: list[tuple[int, list[str]]] = []
    for index, product in enumerate(products):
        overlap = message_tokens & significant_tokens(product.get("title") or "")
        if len(overlap) >= 2:
            named.append((index + 1, sorted(overlap)[:4]))
    return named


def describe_products(products: list[dict]) -> str:
    return " / ".join(
        f"{p.get('title')!s:.38} {p.get('price')}{p.get('currency')} @ {p.get('merchant')}"
        for p in products[:3]
    )


async def agent_turn(client: httpx.AsyncClient, mission_id: str, user_input: str) -> dict:
    print()
    print(f"turn to /agent     : {user_input[:100]}")
    response = await client.post(f"/missions/{mission_id}/agent", json={"user_input": user_input})
    if response.status_code != 200:
        print(f"  HTTP {response.status_code}: {response.text[:200]}")
        return {}
    body = response.json()
    print(f"  Groq chose        : {[step['tool'] for step in (body.get('agent_tools') or [])]}")
    print(f"  agent message     : {(body.get('agent_message') or '')[:220]}")
    return body


async def main() -> int:
    settings = get_settings()
    provider_name = os.environ.get("REASONING_PROVIDER", settings.reasoning_provider)
    search_name = os.environ.get("PRODUCT_SEARCH_PROVIDER", settings.product_search_provider)
    groq_model = settings.reasoning_groq_model
    print(f"reasoning provider  : {provider_name}")
    print(f"reasoning model     : {groq_model}")
    print(f"product search      : {search_name}")
    print(f"retry budget        : {settings.reasoning_max_retries} retries, "
          f"{settings.reasoning_retry_base_seconds}s base")
    print(f"agent step budget   : {settings.agent_max_steps}")

    if provider_name != "groq":
        print()
        print("STOP: this verifier exists to watch the real Groq model choose tools. With")
        print("REASONING_PROVIDER set to anything else it would be watching a different vendor or")
        print("a rule table, and neither is allowed to stand in here.")
        return 1

    api_key = settings.reasoning_groq_api_key or settings.reasoning_api_key
    if not api_key:
        print()
        print("STOP: no Groq API key is configured in this environment. Nothing is printed about")
        print("it, and no second credential is created.")
        return 1

    from app.main import app

    observation = Observation()
    observation.install()

    failures: list[str] = []
    chain_seen = False
    turns: list[dict] = []
    email = f"core45.live.{uuid.uuid4().hex[:10]}@resolve.test"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=300.0
    ) as client:
        registered = await client.post(
            "/auth/register", json={"name": "Groq Chain Owner", "email": email, "password": PASSWORD}
        )
        if not check("disposable user registered", registered.status_code == 201,
                     str(registered.status_code)):
            return 1

        created = await client.post("/missions/shopping", json={"prompt": PROMPT})
        if not check("shopping mission created", created.status_code == 200, str(created.status_code)):
            print(f"   {created.text[:200]}")
            return 1
        mission_id = created.json()["mission_id"]
        opening = (await client.get(f"/missions/{mission_id}")).json()
        intent = opening["shopping_intent"] or {}
        real_products = intent.get("discovered_products") or []
        real_ids = {str(p.get("product_id")) for p in real_products if p.get("product_id")}
        second_product = real_products[1] if len(real_products) > 1 else None
        print(f"disposable mission  : {mission_id}")
        print(f"status at creation  : {opening['status']}")
        print(f"products at creation: {len(real_products)}")
        if real_products:
            print(f"  examples          : {describe_products(real_products)}")
        print(f"intent captured     : category={intent.get('category')} "
              f"budget={intent.get('budget')} preferences={intent.get('preferences')}")

        # --- creation: the milestone's opening line, parsed and searched for real -----------------
        print()
        budget_value = rupees(intent.get("budget"))
        creation_checks = [
            ("the opening line parsed as a shirt search",
             "shirt" in str(intent.get("category") or "").lower(), str(intent.get("category"))),
            ("the budget was captured inside ₹1000",
             budget_value is not None and 0 < budget_value <= 1000, str(intent.get("budget"))),
            ("white was captured in the structured intent",
             any("white" in str(preference).lower() for preference in (intent.get("preferences") or [])),
             str(intent.get("preferences"))),
            ("the search produced real products", len(real_products) > 0, f"{len(real_products)} products"),
            ("those products carry a real provider source, not a mock one",
             {str(p.get("source")) for p in real_products} == {"google_shopping"},
             str(sorted({str(p.get("source")) for p in real_products}))),
            ("every product has a provider title, price and merchant",
             all(p.get("title") and p.get("price") and p.get("merchant") for p in real_products),
             f"{len(real_products)} checked"),
            ("every product is priced in INR",
             {str(p.get("currency")) for p in real_products} <= {"INR"},
             str(sorted({str(p.get("currency")) for p in real_products}))),
        ]
        for label, condition, detail in creation_checks:
            if not check(label, condition, detail):
                failures.append(label)

        # --- the three follow-up turns, one at a time, against the live model ---------------------
        decisions_after_creation = len(observation.decisions())
        turn2 = await agent_turn(client, mission_id, TURN_OFFICE)
        turns.append(turn2)
        await asyncio.sleep(TURN_GAP_SECONDS)
        turn3 = await agent_turn(client, mission_id, TURN_SECOND)
        turns.append(turn3)
        await asyncio.sleep(TURN_GAP_SECONDS)
        turn4 = await agent_turn(client, mission_id, TURN_BUDGET)
        turns.append(turn4)
        print()
        print(f"verifier pacing    : {TURN_GAP_SECONDS}s between turns (harness only, never the app)")
        observation.describe()
        print()

        # --- what the live wire actually showed ----------------------------------------------------
        decisions = observation.decisions()
        live_choices = len(decisions) - decisions_after_creation
        answered = [
            entry for entry in decisions[decisions_after_creation:] if entry["outcome"] == "ok"
        ]
        answered_choices = len(answered)

        all_executed = [
            step["tool"]
            for turn in turns
            for step in (turn.get("agent_tools") or [])
        ]
        if decisions:
            declared = observation.declared_names_of(decisions[0])
            wire_checks = [
                ("every live choice asked over the configured Groq model",
                 all(entry["body"].get("model") == groq_model for entry in decisions),
                 str(sorted({str(entry["body"].get("model")) for entry in decisions}))),
                ("every live choice demanded a tool (tool_choice=required)",
                 all(entry["body"].get("tool_choice") == "required" for entry in decisions),
                 str(sorted({str(entry["body"].get("tool_choice")) for entry in decisions}))),
                ("the declared surface is the closed registry plus the one response tool",
                 declared == set(tool_names()) | {RESPOND_TOOL_NAME},
                 str(sorted(declared))),
                ("every tool that ran was declared to the model", set(all_executed) <= declared,
                 str(sorted(set(all_executed) - declared)) or "all declared"),
                ("live Groq answered at least two step choices",
                 answered_choices >= 2, f"{answered_choices} answered of {live_choices} requested"),
            ]
            for label, condition, detail in wire_checks:
                if not check(label, condition, detail):
                    failures.append(label)
        else:
            print("FAIL  no live step choice was ever requested — the chain cannot be judged")
            failures.append("no live step choices")

        # --- grounding: the state the searches produced, read back from the server ----------------
        searched = (await client.get(f"/missions/{mission_id}")).json()
        current_intent = searched["shopping_intent"] or {}
        current_products = current_intent.get("discovered_products") or []
        grounding_checks = [
            ("every product on record still carries a real provider source",
             {str(p.get("source")) for p in current_products} == {"google_shopping"},
             str(sorted({str(p.get("source")) for p in current_products}))),
            ("white still travels with the mission's structured intent",
             any("white" in str(preference).lower()
                 for preference in (current_intent.get("preferences") or [])),
             str(current_intent.get("preferences"))),
        ]
        for label, condition, detail in grounding_checks:
            if not check(label, condition, detail):
                failures.append(label)

        # the hand-off: a live chooser request must have carried the products the search produced
        carried = False
        for entry in decisions[decisions_after_creation:]:
            carried_ids = {
                str(p.get("product_id")) for p in products_of(observation.context_of(entry))
                if p.get("product_id")
            }
            if real_ids and real_ids <= carried_ids:
                carried = True
                break
        if not check("a live Groq request carried the products the search returned",
                     carried, f"{len(real_ids)} product ids in play"):
            failures.append("context hand-off")

        # --- turn 2 (office use): a grounded answer, every rupee it names belongs to the context ---
        message2 = str(turn2.get("agent_message") or "")
        real_prices = {rupees(p.get("price")) for p in real_products}
        budget_value = rupees(intent.get("budget"))
        # An amount the answer may honestly name: a real product price, the mission's own budget,
        # or a figure the BUYER themselves put in the turn's words (their constraint, restated).
        grounded_amounts = real_prices | {b for b in (budget_value,) if b is not None}
        named2 = amounts_mentioned(message2)
        ungrounded2 = [amount for amount in named2
                       if not any(abs(amount - price) < 1.0 for price in grounded_amounts)]
        office_checks = [
            ("the office-use answer names no invented price",
             not ungrounded2, f"named {named2}; ungrounded {ungrounded2 or 'none'}"),
        ]
        for label, condition, detail in office_checks:
            if not check(label, condition, detail):
                failures.append(label)
        print(f"full answer        : {message2}")

        # --- turn 3 ("the second one"): resolution against the ACTUAL returned set ----------------
        message3 = str(turn3.get("agent_message") or "")
        selected_after = ((turn3.get("shopping_intent") or {}).get("selected_product")) or {}
        selected_id = str(selected_after.get("product_id") or "")
        second_id = str((second_product or {}).get("product_id") or "")
        second_price = rupees((second_product or {}).get("price"))
        named3 = amounts_mentioned(message3)
        ungrounded3 = [amount for amount in named3
                       if not any(abs(amount - price) < 1.0 for price in grounded_amounts)]
        named_positions = positions_named(message3, real_products)
        resolved = (
            (selected_id and selected_id == second_id)
            or any(position == 2 for position, _ in named_positions)
            or (second_price is not None
                and any(abs(amount - second_price) < 1.0 for amount in named3))
        )
        if not check("'the second one' resolved to the product the search returned in position 2",
                     resolved,
                     f"engaged positions {named_positions or 'none'}; named {named3}; "
                     f"selected={selected_id or 'none'}"):
            failures.append("second-one resolution")
        if not check("the second-one answer names no invented price", not ungrounded3,
                     f"named {named3}; ungrounded {ungrounded3 or 'none'}"):
            failures.append("second-one price grounding")
        print(f"full answer        : {message3}")

        # --- turn 4 (below ₹700): OBSERVED, never forced ------------------------------------------
        turn4_tools = [step["tool"] for step in (turn4.get("agent_tools") or [])]
        message4 = str(turn4.get("agent_message") or "")
        budget_after = rupees(current_intent.get("budget"))
        buyer_amounts = set(amounts_mentioned(TURN_BUDGET))
        search_query = None
        transcript = await client.get(f"/missions/{mission_id}/agent/conversation?limit=200")
        events = transcript.json().get("events") if transcript.status_code == 200 else []
        events = events or []
        for event in events:
            if (event.get("event_type") == "tool_call" and event.get("tool_name") == "search_products"):
                arguments = (event.get("payload") or {}).get("arguments") or {}
                search_query = str(arguments.get("query") or "")
        print()
        print("turn 4 observation (recorded, not forced):")
        print(f"  tools executed    : {turn4_tools}")
        print(f"  intent budget now : {current_intent.get('budget')} (was {intent.get('budget')})")
        print(f"  search query used : {search_query or '(no loop search ran)'}")
        print(f"  message excerpt   : {message4[:300]}")
        grounded4 = grounded_amounts | buyer_amounts | {
            rupees(p.get("price")) for p in current_products
        }
        named4 = amounts_mentioned(message4)
        ungrounded4 = [amount for amount in named4
                       if not any(abs(amount - price) < 1.0 for price in grounded4)]
        observed_notes = [
            ("the below-₹700 turn ran only declared tools (whatever it chose)",
             set(turn4_tools) <= set(tool_names()), str(turn4_tools)),
            ("the below-₹700 answer names no invented price", not ungrounded4,
             f"named {named4}; ungrounded {ungrounded4 or 'none'}"),
        ]
        for label, condition, detail in observed_notes:
            if not check(label, condition, detail):
                failures.append(label)
        constraint_supported = (
            (search_query and "700" in search_query)
            or (budget_after is not None and budget_after <= 700)
            or "700" in message4
        )
        print(f"  constraint route  : "
              f"{'loop re-search' if search_query and '700' in search_query else ''}"
              f"{'intent re-parse' if budget_after is not None and budget_after <= 700 else ''}"
              f"{'answer-side narrowing' if '700' in message4 else ''}"
              f"{' — none recorded' if not constraint_supported else ''}")
        if not constraint_supported:
            print("  GAP               : the live model neither re-searched with ₹700, re-parsed the")
            print("                      intent budget below ₹700, nor named ₹700 in its answer. The")
            print("                      architecture offers all three routes; this conversation used")
            print("                      none. Recorded as an honest gap, not papered over.")

        # --- boundaries that must hold whatever the model chose --------------------------------
        boundary = [
            ("no payment record exists on the mission", current_intent.get("payment") is None,
             str(current_intent.get("payment"))),
            ("the mission never reached a payment or order state",
             searched["status"] not in TERMINAL_STATES, searched["status"]),
            ("nothing was spent",
             str(searched.get("total_recovery_spend") or 0) in {"0", "0.0", "0.00"},
             str(searched.get("total_recovery_spend"))),
            ("no turn reported an authority decision it had not made",
             all(turn.get("authority_decision") is None for turn in turns),
             str([turn.get("authority_decision") for turn in turns])[:80]),
            ("the key never appears in a request body or URL",
             api_key not in json.dumps(
                 [{"body": entry["body"], "url": entry["url"]} for entry in observation.requests],
                 default=str),
             "checked every recorded request"),
        ]
        for label, condition, detail in boundary:
            if not check(label, condition, detail):
                failures.append(label)

        # --- the persisted transcript, and each operation exactly once --------------------------
        calls = [event for event in events if event["event_type"] == "tool_call"]
        results = [event for event in events if event["event_type"] == "tool_result"]
        stored = json.dumps(events, default=str)
        print()
        print(f"persisted events    : {sorted({event['event_type'] for event in events})}")
        print(f"counts              : calls={len(calls)} results={len(results)} total={len(events)}")
        persisted = [
            ("the buyer's messages and the answers were stored",
             "user_message" in {event["event_type"] for event in events}
             and "agent_message" in {event["event_type"] for event in events},
             str(sorted({event["event_type"] for event in events}))),
            ("each executed tool was called exactly once", len(calls) == len(all_executed),
             f"calls={len(calls)} executed={len(all_executed)}"),
            ("each executed tool produced exactly one result", len(results) == len(all_executed),
             f"results={len(results)}"),
            ("no chain-of-thought is stored",
             not any(key in stored.lower() for key in
                     ("reasoning_content", "chain_of_thought", '"thinking"')),
             "scanned the whole transcript"),
            ("no credential is stored",
             api_key not in stored and "api_key" not in stored.lower() and "gsk_" not in stored.lower(),
             "scanned the whole transcript"),
        ]
        for label, condition, detail in persisted:
            if not check(label, condition, detail):
                failures.append(label)

        chain_seen = bool(
            len(real_products) > 0
            and {str(p.get("source")) for p in real_products} == {"google_shopping"}
            and answered_choices >= 2
            and carried
            and resolved
            and not failures
        )

        if not chain_seen and not failures:
            print()
            print("The provider refused, or the chain never got enough live decisions. Retry counts")
            print("are above; the chain is not claimed.")

    print()
    print(f"final mission state : {searched['status']}")
    if failures:
        print(f"RESULT              : {len(failures)} failure(s): {failures}")
        return 1
    if not chain_seen:
        print("RESULT              : grounded Groq conversation NOT observed. Bounded behaviour "
              "reported above; nothing is claimed.")
        return 2
    print("RESULT              : a real Groq model parsed the brief, searched through the closed "
          "registry, and every follow-up answer stayed grounded in the products that search "
          "actually returned.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
