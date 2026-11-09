"""
Task CORE-2.1 / live retest — does a real Gemini model drive a real multi-step agent loop?

Everything else in the suite scripts the model's answers. This script refuses to: it uses the configured
reasoning provider itself, and it stops rather than substituting the offline chooser if the configured
provider is not Gemini. The question it answers is whether a live model chooses a declared tool, the
guarded operation really runs (against the configured product-search provider — here, real SerpApi Google
Shopping), and the *next* live model call is made against the state that run produced.

One architectural fact shapes the evidence: `search_products` is a turn-ending tool (`STOP_AFTER_TOOLS` in
`app/agents/loop.py`). Its whole purpose is to put a real list in front of the buyer, so the loop stops
there and the next move is the buyer's. A discovery-then-decision chain therefore crosses two
conversational turns, and that is what this script measures: turn 1 makes Gemini choose the search, the
real provider answers, and turn 2's very first Gemini request must already carry those products. The
hand-off is proved on the products themselves, not on a counter.

The evidence is taken at the one seam every reasoning request passes through (`_post_for_json`), wrapped
here purely to observe. A step choice is a request that carries `tools`.

What this script never does, by construction: no payment endpoint, no order creation or confirmation, no
Razorpay call, no Shopify write, no return/replacement provider, no credential printed. A disposable user
and mission are created. Preparing a checkout is allowed, because the model may legitimately choose it;
confirming or paying is not.

Run it from the compose network (it needs the real Postgres and the key already in the environment):

    docker compose run --rm backend python verify_core21_live_chain.py

Overridable without editing the file:

    LIVE_CHAIN_PROMPT   the mission's opening request
    LIVE_CHAIN_TURN     the turn that must make Gemini choose the search
    LIVE_CHAIN_TURN_2   the follow-up turn that must reason from those real products

Exit 0 — a genuine multi-step Gemini chain was observed.
Exit 2 — the provider refused, or fewer than two live decisions happened. Bounded behaviour is reported;
         nothing is claimed. This is a result, not a pass.
Exit 1 — a boundary or an invariant broke. That is a real failure.
"""
import asyncio
import json
import os
import sys
import uuid

import httpx
from httpx import ASGITransport

from app.agents import reasoning_provider as provider_module
from app.config import get_settings

PASSWORD = "SecurePassword123!"
PROMPT = os.environ.get(
    "LIVE_CHAIN_PROMPT", "I want to order a Dove shampoo under ₹600 for anti-hairfall."
)
TURN = os.environ.get("LIVE_CHAIN_TURN", "Find me wireless earbuds under 3000 rupees.")
TURN_2 = os.environ.get(
    "LIVE_CHAIN_TURN_2",
    "From those results, select the cheapest one and tell me its exact price. Do not pay anything.",
)
DEGRADATION_MARKER = "temporarily unavailable"
# Money states a conversational turn must never reach on its own.
TERMINAL_STATES = {"payment_pending", "payment_verified", "order_created", "order_confirmed"}


class Observation:
    """
    A passive record of the reasoning traffic.

    It re-raises everything the transport re-raises, so the app's own retry and degradation behaviour is
    what runs; this only notes what went by.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.waits: list[float] = []
        self.refusals: list[str] = []
        self.statuses: list[str] = []

    def install(self) -> None:
        original_post = provider_module._post_for_json

        async def watched(url, *, headers, body, label):
            self.requests.append({"label": label, "url": url, "body": body})
            try:
                return await original_post(url, headers=headers, body=body, label=label)
            except provider_module.ReasoningUnavailable as exc:
                self.refusals.append(str(exc))
                self.statuses.append(f"{label} HTTP {exc.status_code} after {exc.attempts} attempt(s)")
                raise
            except Exception as exc:  # noqa: BLE001 — a non-transient refusal is reported, never retried
                self.statuses.append(f"{label} {type(exc).__name__}: {str(exc)[:160]}")
                raise

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
        """The canonical agent context a request carried, parsed back out of the message."""
        return json.loads(entry["body"]["contents"][0]["parts"][0]["text"])

    def declared_names_of(self, entry: dict) -> set[str]:
        return {
            declaration["name"]
            for group in entry["body"]["tools"]
            for declaration in group["functionDeclarations"]
        }

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


def this_turn(context: dict) -> dict:
    return context.get("this_turn") or {}


def identity(products: list[dict]) -> set[str]:
    """Provider-supplied identity only: the id and title the search actually returned."""
    return {f"{p.get('product_id')}|{p.get('title')}" for p in products}


def describe_products(products: list[dict]) -> str:
    return " / ".join(
        f"{p.get('title')!s:.40} {p.get('price')}{p.get('currency')} @ {p.get('merchant')}"
        for p in products[:2]
    )


async def main() -> int:
    settings = get_settings()
    provider_name = os.environ.get("REASONING_PROVIDER", settings.reasoning_provider)
    search_name = os.environ.get("PRODUCT_SEARCH_PROVIDER", settings.product_search_provider)
    print(f"reasoning provider  : {provider_name}")
    print(f"reasoning model     : {settings.reasoning_model}")
    print(f"product search      : {search_name}")
    print(f"retry budget        : {settings.reasoning_max_retries} retries, "
          f"{settings.reasoning_retry_base_seconds}s base")
    print(f"agent step budget   : {settings.agent_max_steps}")

    if provider_name != "gemini":
        print()
        print("STOP: this verifier exists to watch the real Gemini model choose tools. With")
        print("REASONING_PROVIDER set to anything else it would be watching a rule table, and")
        print("the offline chooser is explicitly not allowed to stand in here.")
        return 1

    api_key = settings.reasoning_api_key
    if not api_key:
        print()
        print("STOP: no reasoning API key is configured in this environment. Nothing is")
        print("printed about it, and no second credential is created.")
        return 1

    from app.main import app

    observation = Observation()
    observation.install()

    failures: list[str] = []
    chain_seen = False
    turns: list[dict] = []
    email = f"core21.live.{uuid.uuid4().hex[:10]}@resolve.test"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=300.0
    ) as client:
        registered = await client.post(
            "/auth/register", json={"name": "Live Chain Owner", "email": email, "password": PASSWORD}
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
        opening_products = ((opening["shopping_intent"] or {}).get("discovered_products")) or []
        print(f"disposable mission  : {mission_id}")
        print(f"status at creation  : {opening['status']}")
        print(f"products at creation: {len(opening_products)}")

        # --- turn 1: the buyer asks for something the mission has not searched for ----------------
        print()
        print(f"turn 1 to /agent    : {TURN[:110]}")
        response = await client.post(f"/missions/{mission_id}/agent", json={"user_input": TURN})
        if not check("turn 1 returned 200", response.status_code == 200, str(response.status_code)):
            return 1
        first = response.json()
        turns.append(first)
        first_decisions = len(observation.decisions())
        executed_first = [step["tool"] for step in (first.get("agent_tools") or [])]
        print(f"  Gemini chose      : {executed_first}")
        print(f"  agent message     : {(first.get('agent_message') or '')[:170]}")

        # The products the tool actually produced, read back from the server, not from the model.
        searched = (await client.get(f"/missions/{mission_id}")).json()
        real_products = ((searched["shopping_intent"] or {}).get("discovered_products")) or []
        real_ids = identity(real_products)
        print(f"  products on record: {len(real_products)} "
              f"(source {sorted({str(p.get('source')) for p in real_products})})")
        if real_products:
            print(f"  examples          : {describe_products(real_products)}")

        # --- turn 2: the buyer acts on those results, so the next model call must carry them ------
        print()
        print(f"turn 2 to /agent    : {TURN_2[:110]}")
        response = await client.post(f"/missions/{mission_id}/agent", json={"user_input": TURN_2})
        if not check("turn 2 returned 200", response.status_code == 200, str(response.status_code)):
            return 1
        second = response.json()
        turns.append(second)
        decisions = observation.decisions()
        executed_second = [step["tool"] for step in (second.get("agent_tools") or [])]
        print(f"  Gemini chose      : {executed_second}")
        print(f"  agent message     : {(second.get('agent_message') or '')[:170]}")
        print()
        observation.describe()
        print()

        # --- the chain, and the hand-off ----------------------------------------------------------
        if len(decisions) < 2:
            print(f"NOT SHOWN           : only {len(decisions)} live step choice was ever requested,")
            print("                      so nothing was chained. Nothing is claimed.")
        else:
            opener = observation.context_of(decisions[0])               # turn 1's first choice
            # Turn 2 may never reach the chooser at all (a throttled intent parse stops the turn before
            # any step is asked), so index defensively rather than assuming a second turn's request.
            turn2_index = first_decisions if len(decisions) > first_decisions else len(decisions) - 1
            carrier = observation.context_of(decisions[turn2_index])     # the NEXT live request
            declared = observation.declared_names_of(decisions[0])
            all_executed = executed_first + executed_second
            print(f"step choices per turn: turn 1 = {first_decisions}, "
                  f"turn 2 = {len(decisions) - first_decisions}")
            print(f"execution detail      : "
                  f"{[(s.get('tool'), s.get('success'), s.get('error_code')) for s in (first.get('agent_tools') or [])]} "
                  f"{[(s.get('tool'), s.get('success'), s.get('error_code')) for s in (second.get('agent_tools') or [])]}")

            opener_ids = identity(products_of(opener))
            carried_ids = identity(products_of(carrier))
            # A chooser request the provider refused is not a decision. Per turn, every request but the
            # last was answered (the loop only continues after an answer), and a degraded turn means its
            # final request was refused rather than answered.
            degraded_first = DEGRADATION_MARKER in (first.get("agent_message") or "").lower()
            degraded_second = DEGRADATION_MARKER in (second.get("agent_message") or "").lower()
            turn2_requests = len(decisions) - first_decisions
            answered_choices = (
                max(first_decisions - (1 if degraded_first else 0), 0)
                + max(turn2_requests - (1 if degraded_second else 0), 0)
            )
            answered_second = answered_choices >= 2

            checks = [
                ("a live step choice happened in each turn",
                 first_decisions >= 1 and len(decisions) >= 2,
                 f"turn 1: {first_decisions}, turn 2: {len(decisions) - first_decisions}"),
                ("a guarded tool executed in the first turn", bool(executed_first),
                 str(executed_first)),
                ("the real search was the tool that ran", "search_products" in executed_first,
                 str(executed_first)),
                ("every tool that ran was declared to the model", set(all_executed) <= declared,
                 str(sorted(set(all_executed) - declared)) or "all declared"),
                ("turn 1's first request saw no execution yet",
                 this_turn(opener).get("tools_already_called") == [],
                 str(this_turn(opener).get("tools_already_called"))[:80]),
                ("the search produced real products", len(real_products) > 0,
                 f"{len(real_products)} products"),
                ("those products carry a real provider source, not a mock one",
                 {str(p.get("source")) for p in real_products} == {"google_shopping"},
                 str(sorted({str(p.get("source")) for p in real_products}))),
                ("every product has a provider title, price and merchant",
                 all(p.get("title") and p.get("price") and p.get("merchant") for p in real_products),
                 f"{len(real_products)} checked"),
                ("the NEXT Gemini request carried the products the tool returned",
                 bool(real_ids) and real_ids <= carried_ids,
                 f"{len(real_ids & carried_ids)}/{len(real_ids)} present"),
                ("the FIRST Gemini request could not already have had them",
                 not real_ids <= opener_ids,
                 f"first carried {len(real_ids & opener_ids)}/{len(real_ids)}"),
                ("the follow-up request shows the state the search moved to",
                 searched["status"] == (carrier.get("mission") or {}).get("status"),
                 f"mission {searched['status']} / context "
                 f"{(carrier.get('mission') or {}).get('status')}"),
                ("the earlier conversation was carried into it", bool(carrier.get("conversation")),
                 f"{len(carrier.get('conversation') or [])} turns"),
                ("Gemini ANSWERED at least two live step choices (decisions, not just sends)",
                 answered_second, f"{answered_choices} answered of {len(decisions)} requested"),
            ]
            for label, condition, detail in checks:
                if not check(label, condition, detail):
                    failures.append(label)

            if executed_second and len(decisions) > first_decisions + 1:
                third = observation.context_of(decisions[first_decisions + 1])
                record = (this_turn(third).get("tools_already_called") or [{}])[0]
                more = [
                    ("a third live choice followed turn 2's tool", True,
                     f"{len(decisions)} choices total"),
                    ("that request carried turn 2's execution record",
                     [run.get("tool") for run in this_turn(third).get("tools_already_called") or []]
                     == executed_second[:1],
                     str([run.get("tool") for run in
                          this_turn(third).get("tools_already_called") or []])),
                    ("and the record says the operation succeeded", record.get("success") is True,
                     str(record)[:150]),
                    ("and the step budget moved inside the turn",
                     this_turn(third).get("steps_used", 0) > this_turn(carrier).get("steps_used", 0)
                     and this_turn(third).get("steps_remaining", 0)
                     < this_turn(carrier).get("steps_remaining", 0),
                     f"{this_turn(carrier).get('steps_used')} -> "
                     f"{this_turn(third).get('steps_used')} used, "
                     f"{this_turn(third).get('steps_remaining')} remaining"),
                ]
                for label, condition, detail in more:
                    if not check(label, condition, detail):
                        failures.append(label)
            elif executed_second:
                print("NOTE                : turn 2's tool ended its turn, so no third live choice was")
                print("                      due — that is the loop's design, not a shortfall.")

            chain_seen = bool(
                executed_first
                and "search_products" in executed_first
                and len(decisions) >= 2
                and answered_second
                and real_ids & carried_ids
                and not real_ids <= opener_ids
            )

        # --- boundaries that must hold whatever the model chose --------------------------------
        after = (await client.get(f"/missions/{mission_id}")).json()
        after_intent = after["shopping_intent"] or {}
        boundary = [
            ("no payment record exists on the mission", after_intent.get("payment") is None,
             str(after_intent.get("payment"))),
            ("the mission never reached a payment or order state",
             after["status"] not in TERMINAL_STATES, after["status"]),
            ("nothing was spent",
             str(after.get("total_recovery_spend") or 0) in {"0", "0.0", "0.00"},
             str(after.get("total_recovery_spend"))),
            ("no turn reported an authority decision it had not made",
             all(t.get("authority_decision") is None for t in turns),
             str([t.get("authority_decision") for t in turns])[:80]),
            ("the key never appears in a request body",
             api_key not in json.dumps([entry["body"] for entry in observation.requests]),
             "checked every recorded body"),
        ]
        for label, condition, detail in boundary:
            if not check(label, condition, detail):
                failures.append(label)

        # --- the persisted transcript, and each operation exactly once --------------------------
        executed_all = executed_first + executed_second
        transcript = await client.get(f"/missions/{mission_id}/agent/conversation?limit=200")
        if not check("conversation endpoint answers", transcript.status_code == 200,
                     str(transcript.status_code)):
            failures.append("transcript read")
        events = transcript.json().get("events") or []
        types = sorted({event["event_type"] for event in events})
        calls = [event for event in events if event["event_type"] == "tool_call"]
        results = [event for event in events if event["event_type"] == "tool_result"]
        stored = json.dumps(events)
        print(f"persisted events    : {types}")
        print(f"counts              : calls={len(calls)} results={len(results)} total={len(events)}")
        persisted = [
            ("the buyer's messages and the answers were stored",
             "user_message" in types and "agent_message" in types, str(types)),
            ("each executed tool was called exactly once", len(calls) == len(executed_all),
             f"calls={len(calls)} executed={len(executed_all)}"),
            ("each executed tool produced exactly one result", len(results) == len(executed_all),
             f"results={len(results)}"),
            ("the stored tool names match what ran",
             sorted({event["tool_name"] for event in results}) == sorted(set(executed_all)),
             str(sorted({event["tool_name"] for event in results}))),
            ("no chain-of-thought is stored",
             not any(key in stored.lower() for key in
                     ("reasoning_content", "chain_of_thought", '"thinking"')),
             "scanned the whole transcript"),
            ("no credential is stored",
             api_key not in stored and "api_key" not in stored.lower(),
             "scanned the whole transcript"),
        ]
        for label, condition, detail in persisted:
            if not check(label, condition, detail):
                failures.append(label)

        if not chain_seen:
            print()
            print("The provider refused, or the chain never got a second live decision. Retry counts")
            print("are above; the chain is not claimed.")

    print()
    print(f"final mission state : {after['status']}")
    if failures:
        print(f"RESULT              : {len(failures)} failure(s): {failures}")
        return 1
    if not chain_seen:
        print("RESULT              : multi-step Gemini chain NOT observed. Bounded behaviour reported "
              "above; nothing is claimed.")
        return 2
    print("RESULT              : a real Gemini model chose the search, the real provider answered, and "
          "the next live Gemini request was made against the state that search produced.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
