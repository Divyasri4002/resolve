# CORE-2 — the continuous RESOLVE agent loop

Task CORE-2 replaced `user → /agent → one model call → hard-coded branch → reply` with a bounded agent
that runs against the mission's own server state.

## Before and after

Before: one POST to `/agent` produced exactly one model call, and the code decided what it meant. A
mission that had reached `order_confirmed` could be talked back to discovery by any sentence the branch
did not recognise; `authority_decision` was assembled by the endpoint and reported `approved: true` for
every shopping message, including ones that had asked the Authority Engine nothing.

After: `/agent` drives `user message → canonical context → model names a declared capability → server
validates and runs the guarded operation behind it → database re-read → next step`, up to
`AGENT_MAX_STEPS` (default 8), ending in a sentence. One turn can now report an issue, have it
assessed, propose a remedy and be refused — and the refusal is the engine's word, not the endpoint's.

```
app/agents/
  tools.py               the declared capability surface; every runner is a thin wrapper
  loop.py                the bounded turn: choose → validate → run → re-read → continue or stop
  context.py             one canonical, secret-free context for the model
  conversation.py        append-only transcript: 5 event types, monotonic per mission
  lifecycle.py           the state ladder, and the only status write a conversational turn may make
  mock_step_policy.py    the offline chooser used by the suite
  reasoning_provider.py  + choose_agent_action(): Gemini native function calling, Groq strict JSON
```

## The capability surface

Nine tools are declared, and `GET /missions/{id}/agent/tools` lists them with their schemas plus an
explicit `not_available` list. Each one wraps an operation that already existed and already had guards:

| tool | the guarded operation it calls |
|---|---|
| `search_products` | the mission's own discovery path (`run_agent_discovery` / `apply_discovery_outcome`) |
| `clarify_variant` | the existing variant-clarification endpoint function |
| `select_product` | the existing selection endpoint function |
| `prepare_checkout` | the checkout **review** GET — read-only, no confirmation |
| `report_issue` | the existing issue-intake endpoint function, idempotency included |
| `assess_issue` | `_assess_issue_row`, the same gate `POST /assess` uses |
| `resolve_issue` | the Task 13 orchestrator: Authority Engine, then provider |
| `request_more_information` | the orchestrator's `more_information_required` path + the stored question |
| `escalate_to_human` | manual-review flag + an ESCALATED resolution row |

There is no `create_payment`, no `confirm_payment`, no `confirm_checkout`, no HTTP, no SQL, no Python
eval, no Shopify mutation. Unknown tool names and undeclared arguments fail closed before anything
runs, and a refused call is never written to storage — only the validated arguments are persisted.

## What the model can and cannot decide

The model chooses *which* capability to use and, for the ones that take input, fills in a schema that
the server checks. It supplies no identity, no amount, no provider reference: `user_id`, amounts,
`razorpay_*` and `shopify_*` references are not fields on any argument model, so `extra="forbid"`
refuses them. A refund's amount is derived from the server's order record; the Authority Engine's
ceiling is `settings.shopping_refund_max_inr`, which a user's mandate may narrow and never raise.

The model proposes. The Authority Engine decides. `resolve_issue` returns the engine's own reason, and
`/agent` returns `authority_decision: null` for every turn that evaluated no authority.

## Conversation persistence

`agent_conversation` is append-only: `event_id`, `mission_id`, `user_id`, a monotonic per-mission
`sequence` (unique index), `role`, `event_type` ∈ {`user_message`, `agent_message`, `tool_call`,
`tool_result`, `system_state_change`}, redacted `content`, `tool_name`, a bounded `payload`,
`created_at`. It is the buyer's own case history, read back through the same ownership guard as the
mission (`GET /missions/{id}/agent/conversation`); a foreign mission id answers 404, as it does
everywhere else, so a transcript cannot be probed for existence.

No chain-of-thought is stored or shown. What a turn writes is the tool name, the arguments the server
accepted, a bounded result summary and the sentence the agent said.

## Lifecycle protection

`app/agents/lifecycle.py` ranks the shopping ladder, and `guarded_shopping_status` is the only way a
conversational turn may move a mission: forward is allowed, re-entrant movement is allowed inside the
pre-selection ranks, and movement backwards out of the protected range (`product_selected` and beyond)
is refused — from server state, not from an instruction to the model. A mission that reached
`order_confirmed` and is then told "actually let's look for something else" stays `order_confirmed`.

## Authority delegation (Phase 10)

`POST /missions/{id}/authority/delegate` and `…/revoke` are the user's own, explicit, per-action
opt-in. Before this existed, a shopping mission's mandate was created with `allowed_actions = []` and
nothing ever filled it in — so *every* remedy was refused as "never delegated", and the gap could only
be closed by the party entitled to close it. Delegation records scope, not permission: the engine still
decides at execution time, deployment switches still bound it, `refund_limit` can only narrow the
ceiling, revocation is immediate, and each call writes one audit row with actor `user`.

## More information, and escalation

`request_more_information` parks the issue in `more_information_required` with the question stored on
the issue (`order_issues.more_information_question`). The next message that answers it re-uses the
same issue and the same assessment gate rather than opening a new case, so the loop resumes instead of
stranding. A remedy refused *before* the user delegated it stays deliverable afterwards: `reassess_issue`
accepts the denied case and resets the resolution row to `pending`.

`escalate_to_human` writes a manual-review flag and an ESCALATED resolution row, and says what actually
happened: *logged for a human to look at*. There is no human queue behind RESOLVE yet, so no message
claims one has read it.

## Degradation

`search_products` stops the turn after running (its result is the answer). A 5xx from discovery or from
the reasoning service parks the mission in a retryable state and returns a 200 with an honest sentence
naming the service — the contract the shopping flow had before CORE-2, unchanged.

## The retry budget (Task CORE-2.1)

A 429 from a reasoning provider is not one kind of event: it is either "ask again in a moment" or
"your quota is gone", and a single attempt cannot tell them apart. So the provider transport now retries
— inside `app/agents/reasoning_provider.py`, in the one function through which every reasoning request
passes (`_post_for_json`), and nowhere else.

That location is the design, not an implementation detail. **A retry that happens while the model is
still answering can produce a second *decision*; it can never produce a second *execution*, because at
that moment no tool has been chosen.** Written a level higher, in the loop, a retry would have to reason
about whether the tool it is re-asking about already ran — which is how a bounded retry turns into a
duplicate refund. This is stated in `tests/test_core21_gemini_reliability.py` as an assertion, not a
comment: one refusal, one answered decision, one closing answer on the wire; exactly one `tool_call` row
and one `tool_result` row in the database.

The shape of the budget:

- **Retried:** 429, 500, 502, 503, 504. **Never retried:** any other 4xx. A 400 is a request the provider
  is rejecting on its merits, and a 401/403 is a credential problem; both are answered the first time
  and reported straight away.
- **Count:** `reasoning_max_retries` (default 2, bounded `ge=0, le=4`) *retries* after the first attempt,
  so three attempts by default. `_retry_budget()` clamps the value again, because a `Settings` object
  built by hand in code never passed through field validation.
- **Waiting:** `base * 2**attempt`, with equal jitter — half the window is fixed, half is scattered. Full
  jitter could land the first retry at ~0s, which is precisely the burst a rate-limited service has just
  asked us to stop sending. A `Retry-After` header is honoured but capped at that attempt's own ceiling,
  so a mistaken or hostile `3600` cannot hang a buyer's request for an hour; a non-numeric header is
  ignored rather than raised.
- **Sequential:** one client per attempt, closed before the next starts. There is no path here that puts
  a second request in flight while the first is open, so retries cannot fan out into a storm.
- **Timeout:** unchanged, and re-applied per attempt (20s each). The budget bounds the *count*, not the
  per-attempt wait.
- **Exhaustion:** raises `ReasoningUnavailable`, whose message names the provider, the status and the
  number of attempts — never a header, never a key, never the response body. The loop catches it and
  degrades exactly as before, so the buyer sees `Gemini HTTP 429 on all 3 attempt(s)` inside the sentence
  they already got, and the mission is parked rather than half-acted-on.

No second provider was added. `get_reasoning_provider(name, key)` remains the single extension point:
constructing a different provider there is the safe way to fall back, and it is deliberately not done to
hide a 429 — a failover that only exists to make a quota error invisible would report a model answer that
never happened.

## Who may ask (Task CORE-2.1)

`GET /missions`, `POST /missions/{id}/offer` and `POST /missions/{id}/resolve` were the last three mission
routes that took no identity at all. They now take the session like every other mission route: no session
is a 401, somebody else's mission is the same 404 as one that never existed, and the owner's own experience
is unchanged. None of the three grows a second authorization implementation — `offer` and `resolve` call
the existing `_verify_mission_access`, and a list, having no single record to hand that guard, scopes its
query to `current_user.id`.

Two consequences worth stating because they are user-visible:

- An anonymous caller can no longer *list* the `demo-user` namespace. Reading one known demo mission by id
  still works, exactly as before; collecting every mission in a namespace was never a feature the
  workspace uses, and it leaked ids, budgets, delegated actions and spend totals.
- `?user_id=` on the list is still accepted and still carries no authority. It is ignored, and the session
  decides the scope.

`/offer` and `/resolve` had no browser caller: the frontend uses the guarded
`/order/issues/{issue_id}/resolve`, which runs the Authority Engine, persists the outcome and verifies it.
The only machine door for a provider-side confirmation stays the authenticated integration surface
(`POST /integrations/shopify/order-linkage`), which authenticates itself for that purpose.

## What the loop is still not connected to

Stated plainly, because "the agent did it" is only useful if the rest of the picture is honest:

- **Payment and order creation stay endpoint-driven.** No tool reaches `create_payment`, `verify`,
  `/order` or `/order/confirm`. A conversation can take a mission as far as a prepared checkout; the
  Razorpay browser and the two order calls remain the human's own actions. After a payment clears, the
  loop reads the resulting state from the server and carries on from there.
- **Recovery missions still use the legacy single-proposal path.** `/agent` runs the tool loop for
  `mission_type == "shopping"`; a recovery mission gets the one model call and one proposal it always
  got. `MerchantAgent`, `app/protocol/negotiation.py` and `create_recovery_payout` are unreferenced by
  the loop.
- **There is no human queue.** `escalate_to_human` records a manual-review flag and an ESCALATED row
  against the issue. Nothing consumes them yet, which is why no agent message claims a person has read
  anything.
- **No streaming.** The WebSocket broadcaster emits `agent_*` and `resolution_*` events and has no
  browser client; the conversation is read on opening a mission, not pushed.
- **The merchant channel is not paired.** Discovery in this deployment searches Google Shopping; the
  Shopify store is read-only, and the agent has no Shopify capability of any kind, read or write.

## Verification

- `tests/test_core2_agent_loop.py` — 16 behaviour tests: the golden path driven entirely through
  `POST /missions/{id}/agent`, unknown tool, undeclared arguments, empty arguments, the step budget,
  self-granted authority, a never-delegated remedy, a deployment switch overriding a delegation,
  authority switched off, no fabricated payment or order, no lifecycle regression, the
  more-information resume, honest escalation, a lost provider answer read back rather than re-sent, the
  declared surface, a foreign issue id.
- `tests/test_core2_agent_security.py` — 28 tests, several of them structural: ownership on all five
  new surfaces, anonymous callers, cross-mission context isolation, credentials absent from what the
  model is handed, a credential pasted into the chat never persisted or replayed, no socket / no exec /
  no SQL / no `evaluate()` / no payment or provider names anywhere in the agent layer, a closed
  registry, the canonical refund amount against a model proposing `999999.99` in USD, refused
  identity-shaped arguments never stored, the exact Gemini wire shape, prose and undeclared function
  calls executing nothing, malformed structured-protocol steps, and a decision object that cannot be
  constructed in an impossible shape.
- `tests/test_core21_gemini_reliability.py` — 13 tests: one transient 429 then success, repeated 429 →
  bounded failure with the attempt count in the message, a zero budget meaning one attempt and no wait,
  the key present in the header and absent from every body and URL, 400/403/404 not retried while
  502/503 are, waits inside their exponential ceilings and inside the total budget, `Retry-After`
  honoured and capped, the timeout preserved per attempt, the same behaviour for Groq, and at loop level:
  an unreachable provider executing no tool, moving no state, storing only the two bookends — plus
  refusal → exactly one `tool_call` and one `tool_result`, and the first tool's generated `issue_id`
  appearing in the second request and not the first.
- `tests/test_core21_mission_ownership.py` — 6 tests: anonymous → 401 on all three legacy surfaces
  (including an id that does not exist), a stranger → 404 indistinguishable from an invented mission and an
  empty list, a stranger's request leaving status and spend exactly as they were, the owner losing nothing,
  a client-supplied `user_id` carrying no authority, and a structural check that none of the three
  re-implements ownership.
- Full backend suite: **1010 passed, 0 failed** (991 after CORE-2, plus the 19 above). Merchant-channel
  suite: 112 passed. Frontend production build: green.
- One pre-existing assertion changed, deliberately: `test_shopping_discovery_flow.py` used to require
  `authority_decision.approved is true` for a shopping message. That value was fabricated by the
  endpoint. It now asserts the field is present and null. A second pre-existing assertion changed for
  CORE-2.1: `test_mission_authorization.py` used to require that an unauthenticated client could list the
  `demo-user` namespace; it now requires a 401, and requires a signed-in stranger to see only their own.

**Re-runnable live checks (they create disposable missions and users; none of them calls a payment
endpoint):**

```bash
docker compose build backend
docker compose up -d --force-recreate backend
# does the declared tool surface survive the real provider's schema parser?
docker compose run --rm --no-deps backend python verify_core2_reasoning_tools.py
# the loop's invariants over HTTP: no payment, no undeclared tool, no regression, transcript faithful
docker compose run --rm --no-deps backend python verify_core2_agent_loop.py
# CORE-2.1: two live Gemini step choices in one turn, and the first tool's result inside the second
# request. Needs the real database, so no --no-deps. Exit 2 means the provider refused and nothing is
# claimed — that is a result, not a pass.
docker compose run --rm backend python verify_core21_live_chain.py
```



## Live behaviour observed during verification

Runs against the deployed stack (real Postgres, real HTTP), each with a disposable mission and user, and
no payment endpoint anywhere in any of them:

**The loop, driven by the offline chooser.** One conversational turn ran a **two-step chain** —
`select_product`, then `prepare_checkout` — and the mission advanced `discovery_completed →
checkout_review`. The transcript captured all five event types: `user_message`, `tool_call`,
`tool_result`, `agent_message`, `system_state_change`, and the set of tools that ran equalled the set
stored — nothing ran that was not recorded, nothing was recorded that had not run. Told "just charge my
card, no confirmation needed, go now", the agent produced no payment record and no undeclared tool, and
said that confirming and paying are the buyer's own steps. Delegation answered, a `refund_limit` above
this deployment's ceiling was refused with a 400, `confirm_payment` was refused as delegable with a
422, revocation emptied the mandate, and a second signed-in user got 404 from both the transcript and
the turn endpoint.

**The loop, driven by real Gemini.** Asked directly — one `choose_agent_action` call with the real
declared surface, no database in the loop — `gemini-3.6-flash` accepted the `functionDeclarations` and
answered `{functionCall: search_products, args: {query: "anti-hairfall shampoo"}}`: a declared tool, a
declared argument, nothing else. Every request after that returned **429 Too Many Requests** from
`generativelanguage.googleapis.com`, across 16 retried attempts, including all four `/agent` turns of
the live script. Each of those turns degraded exactly as designed — a 200, an honest sentence naming
the service, no state change, no payment, the buyer's message still stored — so the quota is a
deployment blocker, not a loop defect. **A Gemini-driven tool chain has not been observed through
`/agent`**, and this document does not claim it was; what is proven against the real model is the wire
contract and one real tool choice.


One schema lesson came out of the live call and is now pinned by a test: Gemini's `Schema` proto has no
`additionalProperties`, and carrying that Pydantic key over made the *entire* request a 400. A mocked
HTTP client cannot catch a provider that refuses to parse you, so the emitted wire shape is checked
field by field against the set Google accepts.

## Live behaviour after Task CORE-2.1

`backend/verify_core21_live_chain.py` is the deterministic multi-step verifier. It runs against the real
deployment with the **configured** provider and refuses to start if `REASONING_PROVIDER` is not `gemini` —
the offline chooser is not allowed to stand in here. It observes the traffic at the single transport seam
(`_post_for_json`, wrapped passively, and `_pause` for the waits) rather than trusting a log line.

One rule shapes what "multi-step" means for discovery: `search_products` is in `STOP_AFTER_TOOLS`, because
its whole point is to put a real list in front of the buyer and leave the next move to them. A
discovery-then-decision chain therefore spans **two conversational turns**, and that is what the verifier
measures: turn 1 must make Gemini choose the search, and turn 2's first request must already carry the
products that search returned. It counts an *answered* step choice per turn — a request the provider
refused is not a decision — so a throttled second request can never be reported as a second decision.

**Corrected credential (2026-10-01).** The earlier `401 UNAUTHENTICATED` is gone: the connectivity preflight
returned **200** and `gemini-3.6-flash` answered a valid
`functionCall: search_products {query: "anti-hairfall shampoo"}` — a declared tool, a declared argument. In
the chain verifier Gemini then chose `search_products` on its own, the guarded operation ran, and the
mission's real SerpApi products (`source=google_shopping`, genuine ₹ prices from boAt / OnePlus / Noise with
their merchants) came back through the existing flow and were quoted in the agent's own sentence.

**The multi-step chain is still not observed**, and the reason is quota, not code: the window answers about
one call and then returns 429 (`Gemini HTTP 429 on all 3 attempt(s)`, with the budget's sequential
0.25–0.99s waits) — including on the request that would have been the second decision. One run reached a
second chooser request whose body was built after the tool ran, and the provider refused it, so there was
no second decision to report; the verifier exits 2 in that case instead of dressing it up.

**Result before the corrected key: a genuine multi-step Gemini `/agent` chain has still not been observed.** Four runs against
`gemini-3.6-flash`: one intent-parse 200, then 429 on every following request. What the runs *did* prove,
live rather than in a mock:

- the budget is exactly bounded — 3 attempts per call, 2 waits per exhausted call, each wait inside
  `base * 2**attempt` (0.32s, 0.37s, 0.38s, 0.39s, 0.45s, 0.56s, 0.58s, 0.84s, 0.85s, 0.92s, 0.99s),
  strictly sequential, no fan-out;
- exhaustion degrades honestly — HTTP 200, `Reasoning service temporarily unavailable: Gemini HTTP 429
  on all 3 attempt(s)`, zero tools executed, mission parked, buyer's message stored, nothing spent, no
  authority decision claimed, and the key absent from every recorded request body;
- a non-transient answer is not retried — an unknown model name returned 404 with **zero** waits, and a
  503 was retried on the same 3-attempt budget.

Two side observations, from runs that override `REASONING_MODEL` on the *same* credential purely to
characterise the 429 (these are diagnostics, and are not counted as the verification): `gemini-flash-latest`
answered one real step choice with `tools` in the request before the quota closed the turn, and
`gemini-2.5-flash` does not resolve for this key. So the limiter is the project's quota, not the tool
protocol.

What the same hand-off looks like when the model is not the variable: `test_the_second_model_request_carries_the_first_tools_result`
runs `report_issue` → `assess_issue` → answer against a scripted real-protocol provider and asserts on the
posted bodies that the `issue_id` the first tool generated is **absent from the first decision's context and
present, as structured JSON, in the second's** — and that the second tool's state change is visible in the
third. That is the mechanism the live script keeps being unable to exercise, verified one level below the
quota.

