# DATABASE SCHEMA

PostgreSQL 16. The running deployment has exactly **14 tables** (verified via
`pg_tables` on 2026-11-14). Column lists below were read from
`information_schema.columns` on the same day. The optional ledger tables
(`purchase_events`, `ledger_publish_state`, deployment ledger) are **not installed** on this
database — the ledger feature is flag-off by default; see SYSTEM_ARCHITECTURE.md.

## Identity & conversation

### users
`id, name, email, password_hash, created_at`

### missions
`mission_id, user_id, status, mandate, transaction, evidence, total_recovery_spend,
resolution_summary, created_at, updated_at, mission_type, shopping_intent`
- `mandate` holds delegated authority (which actions the agent may perform, bounded by
  deployment ceilings); `shopping_intent` carries the structured shopping goal + constraints.

### messages
`message_id, mission_id, sender, type, payload, created_at`

### agent_conversation
`event_id, mission_id, user_id, sequence, role, event_type, content, tool_name, payload,
created_at` — append-only agent conversation events (per-mission sequence).

## Commerce

### resolve_orders
`order_id, mission_id, user_id, product_id, title, merchant, product, amount, currency,
razorpay_order_id, razorpay_payment_id, status, created_at, updated_at,
provider_order_reference, shopify_product_gid, shopify_variant_gid, shopify_store_domain,
delivered_at, delivery_mode, shipping_address, shipping_address_confirmed_at,
review_rating, review_body, review_created_at, review_updated_at, delivery_stage,
delivery_stage_at`
- `delivery_mode` records `demo_simulated` for demo deliveries — no carrier/merchant shipment
  is ever claimed. `product` is excluded from ledger payload redaction handling.

### order_issues
`issue_id, order_id, mission_id, user_id, issue_type, description, requested_resolution,
status, evidence, created_at, updated_at, manual_review_requested,
manual_review_requested_at, more_information_question, answers`
- `status` lifecycle: `reported` → `under_review` → terminal states.
- `evidence` (JSON): server-built metadata per item — `kind` (`uploaded_file`),
  `mime`, `sha256`, `size_bytes`, `storage_key`, `bucket`, `collected_at`, `display_name`,
  `description`. **`storage_key`/`bucket` are server-side references only** and are never
  sent to clients; the UI receives compact display metadata.
- `answers` (JSON): normalized category intake answers (accepted only while `reported`).

### resolution_assessments
`assessment_id, issue_id, mission_id, order_id, user_id, proposed_action, issue_assessment,
reasoning_summary, requested_amount, currency, confidence, needs_escalation,
proposal_source, authority_decision, decision_reason, created_at, updated_at,
remedy_eligibility`
- **UNIQUE index on `issue_id`** — one assessment row per issue; reassessment updates the row
  in place (preserving `created_at` as "first assessed").
- `remedy_eligibility` (JSON): per-remedy Layer A verdicts
  (`{"refund": {"eligibility": "eligible|needs_review|not_eligible", "reason": …}, …}`).
- `authority_decision`: `allow` / `escalate` / `deny` from the deterministic engine (Layer B).
- `requested_amount`: canonical order amount (Decimal(12,2)) — server-derived, never
  client-supplied.

### purchase_authorizations
`authorization_id, order_id, mission_id, user_id, authority_decision, decision_reason,
status, submission_state, submission_owner, attempt_count, last_attempt_at,
last_submission_at, trigger_reported_outcome_class, trigger_reported_outcome_status,
trigger_reported_correlation_id, trigger_reported_external_order_id,
trigger_reported_event_id, trigger_reported_reason, error_note, created_at, updated_at`
- Submission lease columns (`submission_state`, `submission_owner`, `attempt_count`) give
  crash-safe, monotonic submission for merchant-channel purchase intents.

## Remedies

### refunds
`refund_id, issue_id, mission_id, order_id, user_id, razorpay_payment_id,
razorpay_refund_id, amount, amount_paise, currency, status, provider_status, error_reason,
verified_at, created_at, updated_at`
- Money is stored both as `amount` (Decimal) and `amount_paise` (integer); `verified_at` is
  set only when the provider itself reports the refund processed.

### replacements
`replacement_id, issue_id, mission_id, order_id, user_id, original_product_id,
original_title, original_merchant, replacement_product_id, quantity, value, currency,
provider, provider_reference, provider_outcome, status, error_reason, confirmed_at,
created_at, updated_at`

### returns
`return_id, issue_id, mission_id, order_id, user_id, original_product_id, original_title,
provider, provider_reference, provider_outcome, status, failure_reason, requested_at,
confirmed_at, created_at, updated_at, provider_order_reference, provider_line_reference,
provider_quantity, provider_reason, submission_claim, submission_claimed_at`

### resolutions
`resolution_id, issue_id, mission_id, order_id, user_id, action, authority_decision,
executor, executor_record_id, resolution_state, provider, provider_reference,
provider_attempted, message, created_at, updated_at`
- Autonomous-resolution record: `authority_decision` + `executor` (`none` when nothing ran).

## Cross-cutting

### audit_log
`entry_id, mission_id, actor, action, requested_amount, decision, reason, created_at`
- Append-only. Every authority decision, execution attempt/refusal, issue/evidence change,
  and lifecycle transition is recorded with actor (`user`, `buyer_agent`, `system`) and a
  human-readable reason.

### integration_events
`event_id, event_type, body_sha256, mission_id, resolve_order_id, provider_order_reference,
outcome, detail, received_at, processed_at` — linkage/outbox events for the merchant channel.

## Constraints of note (verified on the live DB)

- `resolution_assessments`: `UNIQUE (issue_id)` (in-place reassessment semantics).
- `order_issues`: indexes on `mission_id`, `order_id`, `status`, `user_id`; PK `issue_id`.
- `audit_log`: indexes on `entry_id` (PK), `mission_id`, `created_at`.
- Role separation: the app runs as `resolve_app` (no DDL rights; cannot delete protected
  rows); migrations run as `resolve_owner` (NOLOGIN, operator-run). Startup verifies the
  topology and gates with 503 when enforcement fails.
