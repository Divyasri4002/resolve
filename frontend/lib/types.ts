export type MissionStatus =
  | "monitoring"
  | "deviation_detected"
  | "investigating"
  | "negotiating"
  | "awaiting_human_approval"
  | "executing"
  | "verifying"
  | "resolved"
  | "escalated"
  | "shopping_intent"
  | "needs_clarification"
  | "ready_for_discovery"
  | "discovery_completed"
  | "product_selected"
  | "checkout_review"
  | "checkout_confirmed"
  | "payment_pending"
  | "payment_verified"
  | "payment_failed"
  | "order_created"
  | "order_confirmed"
  | "awaiting_post_purchase_context";

export interface VariantOption {
  name: string;
  value: string;
}

export interface VariantClarification {
  question: string;
  option_names?: string[];
  options?: VariantOption[];
}

export interface VariantOption {
  name: string;
  value: string;
}

export interface VariantClarification {
  question: string;
  option_names?: string[];
  options?: VariantOption[];
}

export interface ProductResult {
  product_id?: string | null;
  title: string;
  price?: string | null;
  extracted_price?: number | null;
  currency: string;
  merchant?: string | null;
  product_url?: string | null;
  image_url?: string | null;
  rating?: number | null;
  review_count?: number | null;
  availability?: string | null;
  source?: string;
  extensions?: string[];
  // Display mirrors of the backend's authoritative Shopify identity (Task 14C.7D). Render-only:
  // selection is still sent as `product_id` alone and the server supplies these values, so nothing
  // here is accepted back as authoritative. Absent for non-Shopify (Google Shopping) results, and
  // that absence is correct, not a gap.
  shopify_product_gid?: string | null;
  shopify_variant_gid?: string | null;
  shopify_store_domain?: string | null;
  requires_variant_clarification?: boolean;
  // The Shopify option values a buyer has to choose between (Task 14C.7E). Every value shown comes
  // from Shopify's own `selectedOptions`; the backend resolves the variant from the answer, so the
  // answer is sent as text and never as a variant id.
  variant_clarification?: VariantClarification | null;
}

export interface CheckoutConfirmation {
  quantity: number;
  subtotal?: string | null;
  currency: string;
  payment_provider: string;
  confirmed_at?: string;
}

export interface CheckoutSummary {
  product: ProductResult;
  quantity: number;
  subtotal?: string | null;
  currency: string;
  payment_provider: string;
  payment_note?: string;
}

export interface CheckoutReviewResponse {
  mission_id: string;
  checkout: CheckoutSummary;
  confirmed: boolean;
  mission_status: MissionStatus;
  shopping_intent: ShoppingIntent;
  total_recovery_spend: string;
}

export interface ShoppingPayment {
  razorpay_order_id: string;
  amount: string | number;
  amount_paise: number;
  currency: string;
  status: "pending" | "verified" | "failed";
  razorpay_payment_id?: string | null;
  failure_reason?: string | null;
  created_at?: string;
  verified_at?: string | null;
  /** Server-attested fields on create-order responses — never client-supplied. */
  provider?: string;
  test_mode?: boolean;
  key_id?: string;
}

export interface PaymentOrderResponse {
  mission_id: string;
  payment: ShoppingPayment;
  mission_status: MissionStatus;
  shopping_intent: ShoppingIntent;
  agent_message?: string | null;
}

export interface ResolveOrder {
  order_id: string;
  mission_id: string;
  status: "order_created" | "order_confirmed" | "delivered" | "awaiting_post_purchase_context";
  product: {
    product_id?: string | null;
    title: string;
    merchant?: string | null;
  };
  amount: string;
  currency: string;
  razorpay_order_id: string;
  razorpay_payment_id: string;
  /**
   * Demo delivery facts, server-persisted. `delivery_mode` is "demo_simulated" when the demo
   * transition fired — never a carrier or merchant confirmation — and both stay null until the
   * customer's reload persists it.
   */
  delivered_at?: string | null;
  delivery_mode?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  test_mode?: boolean;
  /**
   * Server-derived, and the only merchant-order fact the client is given. `merchant_order_linkage`
   * is computed from RESOLVE's own persisted linkage column; the merchant-side order id itself is
   * deliberately not sent, because the browser has no use for it and nothing it holds can authorise.
   */
  merchant_order_linked: boolean;
  merchant_order_linkage: "linked" | "awaiting_linkage" | "unlinked";
  /** Server-attested reminder that this is RESOLVE's order record, not merchant fulfillment. */
  fulfillment_note?: string;
  agent_message?: string | null;
}

export interface ResolutionAssessment {
  assessment_id: string;
  issue_id: string;
  mission_id: string;
  order_id: string;
  user_id?: string;
  /** Proposed action only (e.g. request_refund) — nothing is executed in this system. */
  proposed_action: string;
  issue_assessment: string;
  reasoning_summary: string;
  requested_amount?: string | number | null;
  currency?: string | null;
  confidence?: number | null;
  needs_escalation: boolean;
  /** "reasoning_model" | "safe_fallback" */
  proposal_source: string;
  /** Gate outcome only: "allow" means passed the Authority Engine, NOT executed. */
  authority_decision: "allow" | "deny" | "escalate" | string;
  decision_reason: string;
  created_at?: string | null;
  updated_at?: string | null;
  already_existed?: boolean;
  execution_note?: string;
  agent_message?: string | null;
}

export interface RefundRecord {
  refund_id: string;
  issue_id: string;
  mission_id: string;
  order_id: string;
  razorpay_payment_id: string;
  razorpay_refund_id?: string | null;
  /** Canonical amount — always derived server-side; the client never supplies it. */
  amount: string | number;
  amount_paise: number;
  currency: string;
  status: "pending" | "verified" | "failed" | string;
  provider_status?: string | null;
  error_reason?: string | null;
  verified_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  already_existed?: boolean;
  test_mode?: boolean;
  refund_note?: string;
  agent_message?: string | null;
}

export interface ReplacementRecord {
  replacement_id: string;
  issue_id: string;
  mission_id: string;
  order_id: string;
  user_id?: string;
  /** Canonical original product — copied from the persisted order, never client-supplied. */
  original_product_id?: string | null;
  original_title: string;
  original_merchant?: string | null;
  replacement_product_id?: string | null;
  quantity: number;
  value: string | number;
  currency: string;
  provider: string;
  /** Non-null only when a connected provider actually accepted the request. */
  provider_reference?: string | null;
  provider_outcome?: string | null;
  /**
   * 'pending' means RESOLVE recorded the intent and NO merchant replacement exists — with no
   * provider connected (the current reality) this is where it stays.
   */
  status: "pending" | "requested" | "confirmed" | "failed" | string;
  error_reason?: string | null;
  confirmed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  already_existed?: boolean;
  replacement_note?: string;
  agent_message?: string | null;
  detail?: string;
}

export interface ReturnRecord {
  return_id: string;
  issue_id: string;
  mission_id: string;
  order_id: string;
  user_id?: string;
  /** Canonical product being returned — copied from the persisted order, never client-supplied. */
  original_product_id?: string | null;
  original_title: string;
  provider: string;
  /** Non-null only when a connected provider actually accepted the return. */
  provider_reference?: string | null;
  provider_outcome?: string | null;
  /**
   * 'pending' means RESOLVE recorded the intent and NO merchant return exists — with no provider
   * connected (the current reality) this is where it stays.
   */
  status: "pending" | "requested" | "confirmed" | "failed" | string;
  failure_reason?: string | null;
  requested_at?: string | null;
  confirmed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  already_existed?: boolean;
  return_note?: string;
  agent_message?: string | null;
  detail?: string;
}

export interface ResolutionRecord {
  resolution_id: string;
  issue_id: string;
  mission_id: string;
  order_id: string;
  /** Chosen by the backend from the persisted assessment — never by the client. */
  action: string;
  authority_decision: string;
  /** 'refund' | 'replacement' | 'return' | 'none' — the existing controlled executor used. */
  executor: string;
  executor_record_id?: string | null;
  /**
   * A SUMMARY of what the executor achieved. 'pending' and 'requested' explicitly are NOT
   * completions — only provider-verified/confirmed states mean an outcome exists.
   */
  resolution_state:
    | "denied"
    | "pending"
    | "requested"
    | "verified"
    | "confirmed"
    | "failed"
    | "escalated"
    | "more_information_required"
    | "no_action"
    | string;
  provider?: string | null;
  provider_reference?: string | null;
  provider_attempted?: boolean;
  already_existed?: boolean;
  issue_status?: string | null;
  message?: string | null;
  agent_message?: string | null;
  resolution_note?: string;
  execution?: Record<string, unknown> | null;
  created_at?: string | null;
  updated_at?: string | null;
  detail?: string;
}

export interface OrderIssue {
  issue_id: string;
  order_id: string;
  mission_id: string;
  user_id?: string;
  issue_type: string;
  description: string;
  /** The user's stated preference only — never an authorization. */
  requested_resolution: "none" | "refund" | "replacement" | string;
  status:
    | "reported"
    | "under_review"
    | "resolution_pending"
    | "replacement_requested"
  | "return_requested";
  evidence?: { kind?: string; url?: string | null; description?: string | null }[];
  /** M22 §2: the deterministic per-category intake questions, with the buyer's recorded answers. */
  questions?: { question_id: string; prompt: string }[];
  answers?: Record<string, string>;
  created_at?: string | null;
  updated_at?: string | null;
  already_existed?: boolean;
  assessment?: ResolutionAssessment | null;
  refund?: RefundRecord | null;
  replacement?: ReplacementRecord | null;
  return?: ReturnRecord | null;
  /** Task 13 orchestrator summary — never replaces the executor records above. */
  resolution?: ResolutionRecord | null;
  /** Task 14B.5A: customer opt-out from automated resolution decisions. */
  manual_review?: {
    requested: boolean;
    requested_at?: string | null;
    blocks_autonomous_resolution?: boolean;
  };
  /** M22: live per-remedy Authority verdicts — which remedies the buyer may choose.
   *  Advisory only; the executor re-checks authority on every click. */
  remedy_authority?: {
    request_refund?: { decision: "allow" | "blocked"; reason?: string };
    request_replacement?: { decision: "allow" | "blocked"; reason?: string };
  };
  resolution_note?: string;
  agent_message?: string | null;
}

export interface OrderIssueListResponse {
  mission_id: string;
  order_id: string;
  issues: OrderIssue[];
}

export interface ShoppingIntent {
  raw_prompt: string;
  category?: string | null;
  budget?: string | number | null;
  currency: string;
  preferences: string[];
  brand?: string | null;
  size_or_variant?: string | null;
  color?: string | null;
  material?: string | null;
  gender?: string | null;
  intended_use?: string | null;
  quantity?: number | null;
  exclusions?: string[];
  missing_info: string[];
  clarification_prompt?: string | null;
  state:
    | "shopping_intent"
    | "needs_clarification"
    | "ready_for_discovery"
    | "discovery_completed"
    | "product_selected"
    | "checkout_review"
    | "checkout_confirmed"
    | "payment_pending"
    | "payment_verified"
    | "payment_failed"
    | "order_created"
    | "order_confirmed"
    | "awaiting_post_purchase_context";
  discovered_products?: ProductResult[];
  selected_product?: ProductResult | null;
  checkout?: CheckoutConfirmation | null;
  payment?: ShoppingPayment | null;
}

export interface IntentReceipt {
  mission_id: string;
  mission_type?: "recovery" | "shopping";
  objective: string;
  deadline: string | null;
  recovery_budget: string;
  allowed_actions: string[];
  status: MissionStatus;
  total_recovery_spend: string;
  resolution_summary: string | null;
  created_at?: string | null;
  shopping_intent?: ShoppingIntent | null;
}

export interface AgentConversationEvent {
  sequence: number;
  role: "user" | "agent" | "system";
  event_type:
    | "user_message"
    | "agent_message"
    | "tool_call"
    | "tool_result"
    | "system_state_change"
    | "product_reference";
  content: string | null;
  tool_name: string | null;
  payload: Record<string, unknown> | null;
  created_at: string | null;
}

export interface AuthorityDelegationResponse {
  mission_id: string;
  authority: {
    allowed_actions: string[];
    recovery_budget: string;
    spent_so_far: string;
    refund_limit: string | null;
    expires_at: string | null;
  };
  previously_delegated: string[];
  deployment_bounds: Record<string, unknown>;
  decision: { approved: boolean; reason: string };
  note: string;
}

export interface AgentToolTraceEntry {
  tool: string;
  success: boolean;
  state: string | null;
  error_code: string | null;
}

export interface ActivityEvent {  event: string;
  [key: string]: unknown;
  ts?: number;
}

export interface NegotiationEntry {
  sender: "buyer_agent" | "merchant_agent" | "human_merchant";
  text: string;
  ts: string;
}

export interface User {
  id: string;
  name: string;
  email: string;
  created_at: string;
}
