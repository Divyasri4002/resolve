"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { Sidebar } from "@/components/workspace/Sidebar";
import { PromptBox } from "@/components/workspace/PromptBox";
import { Card } from "@/components/ui/Card";
import { StatusPill } from "@/components/ui/StatusPill";
import { MissionContextPanel } from "@/components/workspace/MissionContextPanel";
import { ProductResults } from "@/components/workspace/ProductResults";
import { CheckoutReview } from "@/components/workspace/CheckoutReview";
import { PaymentPanel } from "@/components/workspace/PaymentPanel";
import { OrderPanel } from "@/components/workspace/OrderPanel";
import { IssuePanel } from "@/components/workspace/IssuePanel";
import { loadRazorpayCheckout } from "@/lib/razorpay";
import {
  ApiError,
  apiRequest,
  createMission,
  createShoppingMission,
  delegateAuthority,
  getAgentConversation,
  getMission,
  listMissions,
  revokeAuthority,
} from "@/lib/api";
import { formatMoney, missionStatusLabel } from "@/lib/format";
import type {
  AgentConversationEvent,
  AgentToolTraceEntry,
  CheckoutReviewResponse,
  CheckoutSummary,
  IntentReceipt,
  MissionStatus,
  OrderIssue,
  OrderIssueListResponse,
  PaymentOrderResponse,
  ProductResult,
  ResolveOrder,
} from "@/lib/types";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Menu,
  PanelRightOpen,
  RefreshCw,
  Shield,
  ShieldCheck,
  ShoppingBag,
  Sparkles,
  X,
} from "lucide-react";

/**
 * One line of this mission's conversation.
 *
 * `agent` lines are sentences RESOLVE returned, `user` lines are what the buyer typed, and `system`
 * lines state a recorded fact — an operation and the state the server reports now. The three are
 * rendered apart so a status line can never be read as something the agent said.
 */
interface ConversationMessage {
  role: "user" | "agent" | "system";
  content: string;
  proposal?: { action: string; amount: string | null } | null;
  authority?: { approved: boolean; reason: string } | null;
  products?: ProductResult[];
  tools?: AgentToolTraceEntry[];
}

interface AgentTurnResponse {
  mission_id: string;
  agent_proposal?: { action: string; amount: string | null } | null;
  authority_decision?: { approved: boolean; reason: string } | null;
  mission_status?: MissionStatus;
  agent_message?: string | null;
  agent_tools?: AgentToolTraceEntry[] | null;
  discovered_products?: ProductResult[] | null;
}

interface SelectProductResponse {
  selected_product?: ProductResult | null;
  agent_message?: string | null;
}

interface CheckoutConfirmResponse {
  checkout?: CheckoutSummary | null;
  agent_message?: string | null;
}

interface PaymentVerifyResponse {
  payment?: { status?: string } | null;
  agent_message?: string | null;
}

/** The reply shape every guarded post-purchase operation shares. */
interface OperationReply {
  agent_message?: string | null;
}

/** Buyer-facing openers, offered as prompts only — no product data lives in this file. */
const SHOPPING_PROMPTS = [
  "Find me wireless earbuds under ₹3,000 with long battery life.",
  "I want to order shampoo under ₹600 for anti-hairfall.",
  "I want to buy running shoes from Nike size 9.",
];

const RECOVERY_PROMPTS = [
  "Camera arrived with cracked lens. Merchant refuses return.",
  "Order delayed 10 days past deadline. Requesting full refund or expedited replacement.",
  "Delivered package was empty. Need carrier claim and immediate refund.",
];

/**
 * A proposal is labelled as a proposal only for a step that would spend or claim something.
 *
 * `/agent` answers every turn with a proposal action, and for a search or a clarification that
 * action is `clarify_intent` — a description, not a decision. Printing it in a proposal box would
 * put a gate-shaped badge on an ordinary sentence.
 */
const CONSEQUENTIAL_ACTIONS = new Set([
  "request_refund",
  "request_replacement",
  "request_return",
  "negotiate",
  "pay_recovery_cost",
]);

const CHECKOUT_STAGES: MissionStatus[] = ["checkout_review", "checkout_confirmed"];

/** From a verified payment onward the mission owns an order record, and possibly issues. */
const ORDER_STAGES: MissionStatus[] = [
  "payment_verified",
  "order_created",
  "order_confirmed",
  "awaiting_post_purchase_context",
];

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : "The request did not complete.";
}

function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

function humanize(value?: string | null): string {
  return value ? value.replace(/_/g, " ") : "";
}

/**
 * The stored transcript, in the shape this chat renders (Task CORE-2, Phase 16).
 *
 * What RESOLVE kept is the record of a turn — the buyer's message, each guarded tool it led to, the
 * sentence the agent ended with. A tool row is not a message on its own, so the tools between two
 * agent sentences are attached to the second one, which is what the live conversation already shows.
 */
function transcriptFromEvents(events: AgentConversationEvent[]): ConversationMessage[] {
  const messages: ConversationMessage[] = [];
  let pendingTools: AgentToolTraceEntry[] = [];

  for (const event of events) {
    if (event.event_type === "user_message") {
      messages.push({ role: "user", content: event.content ?? "" });
    } else if (event.event_type === "tool_result") {
      const payload = (event.payload ?? {}) as {
        success?: boolean;
        next_state?: string | null;
        error_code?: string | null;
      };
      pendingTools.push({
        tool: event.tool_name ?? "tool",
        success: payload.success === true,
        state: payload.next_state ?? null,
        error_code: payload.error_code ?? null,
      });
    } else if (event.event_type === "agent_message") {
      messages.push({
        role: "agent",
        content: event.content ?? "",
        tools: pendingTools.length > 0 ? pendingTools : undefined,
      });
      pendingTools = [];
    }
  }

  if (pendingTools.length > 0) {
    messages.push({ role: "agent", content: "", tools: pendingTools });
  }
  return messages.filter((message) => message.content.length > 0 || message.tools);
}

export default function WorkspacePage() {
  const router = useRouter();
  const { user, isLoading: isAuthLoading } = useAuth();

  const [missions, setMissions] = useState<IntentReceipt[]>([]);
  const [isMissionsLoading, setIsMissionsLoading] = useState(true);
  const [missionError, setMissionError] = useState<string | null>(null);

  const [activeCreationMode, setActiveCreationMode] = useState<"shopping" | "recovery">("shopping");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Record<string, ConversationMessage[]>>({});
  const [conversationLoadedFor, setConversationLoadedFor] = useState<string | null>(null);
  const [isAgentThinking, setIsAgentThinking] = useState(false);
  const [isCreatingMission, setIsCreatingMission] = useState(false);

  const [selectingProductId, setSelectingProductId] = useState<string | null>(null);
  const [clarifyingProductId, setClarifyingProductId] = useState<string | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);

  const [checkoutSummary, setCheckoutSummary] = useState<CheckoutSummary | null>(null);
  const [checkoutDismissed, setCheckoutDismissed] = useState(false);
  const [checkoutError, setCheckoutError] = useState<string | null>(null);
  const [isConfirmingCheckout, setIsConfirmingCheckout] = useState(false);

  const [isPaying, setIsPaying] = useState(false);
  const [paymentError, setPaymentError] = useState<string | null>(null);

  const [orderRecord, setOrderRecord] = useState<ResolveOrder | null>(null);
  const [isOrderBusy, setIsOrderBusy] = useState(false);
  const [orderError, setOrderError] = useState<string | null>(null);

  const [orderIssues, setOrderIssues] = useState<OrderIssue[]>([]);
  const [isIssueBusy, setIsIssueBusy] = useState(false);
  const [issueError, setIssueError] = useState<string | null>(null);

  const [isDelegating, setIsDelegating] = useState(false);
  const [delegationError, setDelegationError] = useState<string | null>(null);
  const [delegationNotice, setDelegationNotice] = useState<string | null>(null);

  const [missionsDrawerOpen, setMissionsDrawerOpen] = useState(false);
  const [contextDrawerOpen, setContextDrawerOpen] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);

  const selected = missions.find((m) => m.mission_id === selectedId) ?? null;
  const isShoppingMission = selected?.mission_type === "shopping";
  const intent = selected?.shopping_intent ?? null;
  const selectedProduct = intent?.selected_product ?? null;
  const selectedProductId = selectedProduct?.product_id ?? null;
  const discoveredProducts = intent?.discovered_products ?? [];
  const checkoutConfirmation = intent?.checkout ?? null;
  const isCheckoutConfirmed = checkoutConfirmation != null;
  const paymentRecord = intent?.payment ?? null;
  const currentConversation = selectedId ? conversations[selectedId] ?? [] : [];
  const productsShownInTranscript = currentConversation.some(
    (message) => (message.products?.length ?? 0) > 0
  );

  // Protected workspace: redirect to /login if unauthenticated
  useEffect(() => {
    if (!isAuthLoading && !user) {
      router.replace("/login");
    }
  }, [user, isAuthLoading, router]);

  const refreshMissions = useCallback(
    async (quiet = false) => {
      if (!user) return;
      if (!quiet) {
        setIsMissionsLoading(true);
        setMissionError(null);
      }
      try {
        // The backend scopes this list to the cookie's user; the page never asks for another id.
        setMissions(await listMissions());
      } catch {
        if (!quiet) setMissionError("Unable to load your missions. Please check backend connection.");
      } finally {
        if (!quiet) setIsMissionsLoading(false);
      }
    },
    [user]
  );

  useEffect(() => {
    if (user) {
      refreshMissions();
    }
  }, [user, refreshMissions]);

  /**
   * Re-read one mission plus the checkout / order / issue records its stage owns.
   *
   * This is the only way any panel's data changes: nothing here patches a status, an amount or a
   * lifecycle state in the browser. So a refusal that still persisted a record is displayed exactly
   * as the server wrote it, and a state the server never reached is never displayed as reached.
   */
  const refreshStageData = useCallback(async (mission: IntentReceipt) => {
    const missionId = mission.mission_id;
    if (mission.mission_type !== "shopping") {
      setOrderRecord(null);
      setOrderIssues([]);
      return;
    }
    const status = mission.status;
    const hasProduct = mission.shopping_intent?.selected_product?.product_id != null;

    // Only the stages that already hold a persisted review: opening the review is itself a state
    // transition on the backend, so it stays a user action and never happens as a side effect here.
    if (hasProduct && CHECKOUT_STAGES.includes(status)) {
      try {
        const data = await apiRequest<CheckoutReviewResponse>(
          `/missions/${missionId}/shopping/checkout`
        );
        setCheckoutSummary(data.checkout);
      } catch (err) {
        setCheckoutSummary(null);
        setCheckoutError(messageOf(err));
      }
    }

    let order: ResolveOrder | null = null;
    if (ORDER_STAGES.includes(status)) {
      try {
        order = await apiRequest<ResolveOrder>(`/missions/${missionId}/order`);
      } catch (err) {
        if (!isNotFound(err)) setOrderError(messageOf(err));
      }
    }
    setOrderRecord(order);

    if (order) {
      try {
        const data = await apiRequest<OrderIssueListResponse>(
          `/missions/${missionId}/order/issues`
        );
        setOrderIssues(data.issues ?? []);
      } catch (err) {
        setOrderIssues([]);
        if (!isNotFound(err)) setIssueError(messageOf(err));
      }
    } else {
      setOrderIssues([]);
    }
  }, []);

  const syncMission = useCallback(
    async (missionId: string): Promise<IntentReceipt | null> => {
      let mission: IntentReceipt;
      try {
        mission = await getMission(missionId);
      } catch (err) {
        // 404 means it is not this user's mission any more. The authenticated list decides what is
        // shown, so the selection goes back to the dashboard rather than holding a foreign row.
        if (isNotFound(err)) {
          setSelectedId(null);
          setMissionError("That mission is not in your list.");
        } else {
          setMissionError(messageOf(err));
        }
        return null;
      }
      setMissions((prev) =>
        prev.some((m) => m.mission_id === missionId)
          ? prev.map((m) => (m.mission_id === missionId ? mission : m))
          : [mission, ...prev]
      );
      await refreshStageData(mission);
      void refreshMissions(true);
      return mission;
    },
    [refreshStageData, refreshMissions]
  );

  const appendMessage = useCallback((missionId: string, message: ConversationMessage) => {
    setConversations((prev) => ({
      ...prev,
      [missionId]: [...(prev[missionId] ?? []), message],
    }));
  }, []);

  /**
   * Record one operation's outcome in the thread, then re-read the server's answer.
   *
   * When RESOLVE returned a sentence, that sentence is shown as the agent's. When it returned none,
   * the line is a system note naming the operation and the state the server reports now — the
   * browser never writes an agent's words for it.
   */
  const finishStep = useCallback(
    async (missionId: string, label: string, serverText?: string | null) => {
      const text = serverText?.trim();
      if (text) {
        appendMessage(missionId, { role: "agent", content: text });
        await syncMission(missionId);
        return;
      }
      const mission = await syncMission(missionId);
      appendMessage(missionId, {
        role: "system",
        content: `${label} — current state: ${missionStatusLabel(mission?.status)}`,
      });
    },
    [appendMessage, syncMission]
  );

  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [currentConversation.length, isAgentThinking, selectedId]);

  // Switching missions clears every transient, including a checkout review or order record that
  // belongs to the mission being left. The state itself is then re-read, not remembered.
  useEffect(() => {
    setSelectionError(null);
    setSelectingProductId(null);
    setClarifyingProductId(null);
    setCheckoutSummary(null);
    setCheckoutDismissed(false);
    setCheckoutError(null);
    setIsConfirmingCheckout(false);
    setPaymentError(null);
    setIsPaying(false);
    setOrderRecord(null);
    setOrderError(null);
    setIsOrderBusy(false);
    setOrderIssues([]);
    setIssueError(null);
    setIsIssueBusy(false);
    setDelegationError(null);
    setDelegationNotice(null);
    setMissionError(null);
    if (!selectedId) return;
    let cancelled = false;
    (async () => {
      const mission = await syncMission(selectedId);
      if (!cancelled && mission && CHECKOUT_STAGES.includes(mission.status)) {
        setCheckoutDismissed(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId, syncMission]);

  // The conversation the agent actually had (Task CORE-2, Phase 16): reopening a mission reads back
  // what RESOLVE persisted. Fetched once per mission per visit — a turn the user just had is richer
  // than the stored rows, so it is never replaced underneath them.
  useEffect(() => {
    if (!selectedId || !user || conversationLoadedFor === selectedId) return;
    const missionId = selectedId;
    let cancelled = false;
    (async () => {
      try {
        const events = await getAgentConversation(missionId);
        if (cancelled || events.length === 0) return;
        setConversations((prev) =>
          prev[missionId]?.length ? prev : { ...prev, [missionId]: transcriptFromEvents(events) }
        );
      } catch {
        // A conversation that will not load is not a reason to lose the mission's panels.
      } finally {
        if (!cancelled) setConversationLoadedFor(missionId);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId, user, conversationLoadedFor]);

  const handleNewMission = async (text: string) => {
    if (!text.trim() || isCreatingMission) return;

    const prompt = text.trim();
    setIsCreatingMission(true);
    setMissionError(null);

    try {
      const receipt =
        activeCreationMode === "shopping"
          ? await createShoppingMission(prompt)
          : await createMission({
              mission_type: "recovery",
              objective: { description: prompt, success_criteria: [] },
              authority: {
                allowed_actions: [
                  "request_refund",
                  "request_replacement",
                  "negotiate",
                  "pay_recovery_cost",
                ],
                recovery_budget: "10000",
              },
              transaction: {
                merchant_name: "Merchant",
                order_id: `order_${Date.now()}`,
                amount_paid: "0",
              },
            });

      // The buyer's own words, then RESOLVE's — which are the sentence the receipt grounded and the
      // products the configured search provider actually returned. Only where the server handed back
      // neither does the line become a system note about the state it is now in.
      const createdProducts = receipt.shopping_intent?.discovered_products ?? [];
      const opening = (
        receipt.resolution_summary ||
        receipt.shopping_intent?.clarification_prompt ||
        ""
      ).trim();
      setConversations((prev) => ({
        ...prev,
        [receipt.mission_id]: [
          { role: "user", content: prompt },
          opening
            ? {
                role: "agent",
                content: opening,
                products: createdProducts.length > 0 ? createdProducts : undefined,
              }
            : {
                role: "system",
                content: `Mission created — current state: ${missionStatusLabel(receipt.status)}`,
                products: createdProducts.length > 0 ? createdProducts : undefined,
              },
        ],
      }));
      setConversationLoadedFor(receipt.mission_id);
      await refreshMissions(true);
      setSelectedId(receipt.mission_id);
    } catch (err) {
      setMissionError(messageOf(err));
    } finally {
      setIsCreatingMission(false);
    }
  };

  const handleAgentMessage = async (text: string) => {
    if (!text.trim() || !selectedId || isAgentThinking) return;

    const missionId = selectedId;
    appendMessage(missionId, { role: "user", content: text });
    setIsAgentThinking(true);

    try {
      const data = await apiRequest<AgentTurnResponse>(`/missions/${missionId}/agent`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_input: text }),
      });

      const tools = data.agent_tools ?? [];
      const reply = data.agent_message?.trim();
      appendMessage(missionId, {
        role: reply ? "agent" : "system",
        content:
          reply ||
          `The agent returned no sentence for this turn — current state: ${missionStatusLabel(
            data.mission_status
          )}`,
        proposal: data.agent_proposal ?? null,
        authority: data.authority_decision ?? null,
        tools: tools.length > 0 ? tools : undefined,
        products: data.discovered_products?.length ? data.discovered_products : undefined,
      });
      await syncMission(missionId);
    } catch (err) {
      appendMessage(missionId, {
        role: "system",
        content: `The agent did not answer: ${messageOf(err)}`,
      });
      await syncMission(missionId);
    } finally {
      setIsAgentThinking(false);
    }
  };

  /**
   * Delegate one named remedy to this mission (Task CORE-2, Phase 10).
   *
   * The user's decision is about scope, never about an outcome: the record says what the agent is
   * allowed to propose, the Authority Engine still decides whether a proposal runs, and a switch that
   * is off in this deployment refuses whatever the mandate says. The reason the server returns is
   * shown as it came back, because the honest wording is the point.
   */
  const handleDelegate = async (action: string) => {
    if (!selectedId || isDelegating) return;
    setIsDelegating(true);
    setDelegationError(null);
    setDelegationNotice(null);
    try {
      const data = await delegateAuthority(selectedId, [action]);
      setDelegationNotice(data.decision.reason);
      await syncMission(selectedId);
    } catch (err) {
      setDelegationError(messageOf(err));
    } finally {
      setIsDelegating(false);
    }
  };

  const handleRevokeAuthority = async () => {
    if (!selectedId || isDelegating) return;
    setIsDelegating(true);
    setDelegationError(null);
    setDelegationNotice(null);
    try {
      const data = await revokeAuthority(selectedId);
      setDelegationNotice(data.decision.reason);
      await syncMission(selectedId);
    } catch (err) {
      setDelegationError(messageOf(err));
    } finally {
      setIsDelegating(false);
    }
  };

  const handleSelectProduct = async (product: ProductResult) => {
    if (!selectedId || !product.product_id || selectingProductId) return;

    const missionId = selectedId;
    setSelectingProductId(product.product_id);
    setSelectionError(null);
    try {
      const data = await apiRequest<SelectProductResponse>(
        `/missions/${missionId}/shopping/select-product`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ product_id: product.product_id }),
        }
      );
      await finishStep(missionId, "Selection recorded", data.agent_message);
    } catch (err) {
      setSelectionError(messageOf(err));
    } finally {
      setSelectingProductId(null);
    }
  };

  /**
   * Answer a Shopify variant question with one of the option values Shopify published (Task 14C.7E).
   * Only the product handle and the buyer's value are sent: the backend re-asks the configured
   * provider and Shopify's own catalog decides which ProductVariant that names, so no variant id is
   * ever chosen here.
   */
  const handleClarifyVariant = async (product: ProductResult, answer: string) => {
    if (!selectedId || !product.product_id || clarifyingProductId) return;

    const missionId = selectedId;
    setClarifyingProductId(product.product_id);
    setSelectionError(null);
    try {
      const data = await apiRequest<{ agent_message?: string | null }>(
        `/missions/${missionId}/shopping/clarify-variant`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ product_id: product.product_id, answer }),
        }
      );
      appendMessage(missionId, { role: "user", content: answer });
      await finishStep(missionId, "Variant answer recorded", data.agent_message);
    } catch (err) {
      setSelectionError(messageOf(err));
    } finally {
      setClarifyingProductId(null);
    }
  };

  const fetchCheckout = useCallback(async (missionId: string): Promise<boolean> => {
    setCheckoutError(null);
    try {
      const data = await apiRequest<CheckoutReviewResponse>(
        `/missions/${missionId}/shopping/checkout`
      );
      setCheckoutSummary(data.checkout);
      return true;
    } catch (err) {
      setCheckoutError(messageOf(err));
      return false;
    }
  }, []);

  const handleOpenCheckout = async () => {
    if (!selectedId || !selectedProduct) return;
    setCheckoutDismissed(false);
    if (checkoutSummary) return;
    await fetchCheckout(selectedId);
    // Opening the review advanced the mission on the server (product_selected → checkout_review),
    // so the state shown has to follow that read rather than a guess made here.
    await syncMission(selectedId);
  };

  const handleConfirmCheckout = async () => {
    if (!selectedId || isConfirmingCheckout) return;

    const missionId = selectedId;
    setIsConfirmingCheckout(true);
    setCheckoutError(null);
    try {
      // The body carries NO authoritative data — the backend derives product, price, quantity and
      // subtotal from the mission's persisted selected_product.
      const data = await apiRequest<CheckoutConfirmResponse>(
        `/missions/${missionId}/shopping/checkout/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      );
      if (data.checkout) setCheckoutSummary(data.checkout);
      await finishStep(missionId, "Checkout confirmed", data.agent_message);
    } catch (err) {
      setCheckoutError(messageOf(err));
    } finally {
      setIsConfirmingCheckout(false);
    }
  };

  // Task 08: RESOLVE order lifecycle. The backend creates the persistent order record from the
  // already-persisted verified payment and selected product — the frontend supplies nothing
  // authoritative, and repeated calls are idempotent server-side.
  const createResolveOrder = useCallback(
    async (missionId: string): Promise<boolean> => {
      setIsOrderBusy(true);
      setOrderError(null);
      try {
        const data = await apiRequest<ResolveOrder & OperationReply>(`/missions/${missionId}/order`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        await finishStep(missionId, "Order record created", data.agent_message);
        return true;
      } catch (err) {
        setOrderError(messageOf(err));
        return false;
      } finally {
        setIsOrderBusy(false);
      }
    },
    [finishStep]
  );

  // Razorpay TEST MODE payment (Task 07). The server creates the order and derives the amount; the
  // frontend only opens Razorpay Checkout with the server-issued order id and publishable key id,
  // then submits Razorpay's response to the backend for signature verification. The client never
  // invents amounts or ids, and Razorpay's browser response is never treated as proof of payment.
  const handleProceedToPayment = async () => {
    if (!selectedId || !user || isPaying) return;

    const missionId = selectedId;
    setIsPaying(true);
    setPaymentError(null);

    try {
      const data = await apiRequest<PaymentOrderResponse>(
        `/missions/${missionId}/shopping/payment/create-order`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      );
      await syncMission(missionId);

      const loaded = await loadRazorpayCheckout();
      if (!loaded || !window.Razorpay) {
        throw new Error("Could not load Razorpay Checkout. Check your internet connection.");
      }
      if (!data.payment.key_id || !data.payment.razorpay_order_id) {
        throw new Error("The server did not return complete checkout details.");
      }

      const rzp = new window.Razorpay({
        key: data.payment.key_id,
        order_id: data.payment.razorpay_order_id,
        amount: data.payment.amount_paise,
        currency: data.payment.currency,
        name: "RESOLVE",
        description: "TEST MODE — no real money",
        theme: { color: "#D27D54" },
        prefill: { name: user.name, email: user.email },
        handler: async (rzpResponse) => {
          try {
            // Only the backend's HMAC check makes this a paid mission; a refusal throws instead.
            const verified = await apiRequest<PaymentVerifyResponse>(
              `/missions/${missionId}/shopping/payment/verify`,
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  razorpay_order_id: rzpResponse.razorpay_order_id,
                  razorpay_payment_id: rzpResponse.razorpay_payment_id,
                  razorpay_signature: rzpResponse.razorpay_signature,
                }),
              }
            );
            setPaymentError(null);
            await finishStep(missionId, "Payment", verified.agent_message);
            if (verified.payment?.status === "verified") {
              await createResolveOrder(missionId);
            }
          } catch (err) {
            setPaymentError(messageOf(err));
          } finally {
            setIsPaying(false);
            await syncMission(missionId);
          }
        },
        modal: {
          ondismiss: () => {
            setIsPaying(false);
          },
        },
      });
      rzp.open();
    } catch (err) {
      setPaymentError(messageOf(err));
      setIsPaying(false);
      await syncMission(missionId);
    }
  };

  const handleCreateOrder = async () => {
    if (!selectedId || isOrderBusy) return;
    await createResolveOrder(selectedId);
  };

  const handleConfirmOrder = async () => {
    if (!selectedId || isOrderBusy) return;
    const missionId = selectedId;
    setIsOrderBusy(true);
    setOrderError(null);
    try {
      const data = await apiRequest<ResolveOrder & OperationReply>(
        `/missions/${missionId}/order/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      );
      await finishStep(missionId, "Order confirmed", data.agent_message);
    } catch (err) {
      setOrderError(messageOf(err));
      await syncMission(missionId);
    } finally {
      setIsOrderBusy(false);
    }
  };

  // Task 09: post-purchase issue intake. The client sends only the user's issue details; the
  // backend resolves the canonical order, ownership and lifecycle.
  const handleReportIssue = async (
    issueType: string,
    description: string,
    requestedResolution: string
  ): Promise<boolean> => {
    if (!selectedId || isIssueBusy) return false;
    const missionId = selectedId;
    setIsIssueBusy(true);
    setIssueError(null);
    try {
      const data = await apiRequest<OrderIssue>(`/missions/${missionId}/order/issues`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          issue_type: issueType,
          description,
          requested_resolution: requestedResolution,
        }),
      });
      await finishStep(missionId, "Issue recorded", data.agent_message);
      return true;
    } catch (err) {
      setIssueError(messageOf(err));
      await syncMission(missionId);
      return false;
    } finally {
      setIsIssueBusy(false);
    }
  };

  /**
   * One controlled post-purchase operation (Tasks 10, 11A, 12A, 12B, 13, 14B.5A).
   *
   * Every one of them is a POST with no body: the backend re-checks the Authority Engine, derives
   * amounts and identities from canonical rows, and refuses what this deployment has not enabled. A
   * refusal is still re-read from the server, because it may have persisted a record — so "allowed"
   * and "done" stay whatever the backend said and are never what this file hoped.
   */
  const runIssueOperation = useCallback(
    async (issueId: string, operation: string, label: string): Promise<boolean> => {
      if (!selectedId || isIssueBusy) return false;
      const missionId = selectedId;
      setIsIssueBusy(true);
      setIssueError(null);
      try {
        const data = await apiRequest<OperationReply>(
          `/missions/${missionId}/order/issues/${issueId}/${operation}`,
          { method: "POST" }
        );
        await finishStep(missionId, label, data.agent_message);
        return true;
      } catch (err) {
        setIssueError(messageOf(err));
        await finishStep(missionId, `${label} refused`, null);
        return false;
      } finally {
        setIsIssueBusy(false);
      }
    },
    [selectedId, isIssueBusy, finishStep]
  );

  const handleAssessIssue = (issueId: string) =>
    runIssueOperation(issueId, "assess", "Assessment recorded");
  const handleReassessIssue = (issueId: string) =>
    runIssueOperation(issueId, "reassess", "Reassessment recorded");
  const handleExecuteRefund = (issueId: string) =>
    runIssueOperation(issueId, "refund", "Refund attempt");
  const handleExecuteReplacement = (issueId: string) =>
    runIssueOperation(issueId, "replacement", "Replacement attempt");
  const handleExecuteReturn = (issueId: string) =>
    runIssueOperation(issueId, "return", "Return attempt");
  const handleResolveIssue = (issueId: string) =>
    runIssueOperation(issueId, "resolve", "Resolution attempt");
  const handleManualReview = (issueId: string) =>
    runIssueOperation(issueId, "manual-review", "Manual review request");

  if (isAuthLoading || !user) {
    return (
      <div className="flex h-screen items-center justify-center bg-surface">
        <div className="flex items-center gap-2.5 text-text-charcoal/60">
          <Loader2 size={20} className="animate-spin text-accent-terracotta" />
          <span className="text-sm font-medium">Authenticating session...</span>
        </div>
      </div>
    );
  }

  const shoppingMissionsCount = missions.filter((m) => m.mission_type === "shopping").length;
  const activeRecoveryCount = missions.filter(
    (m) => m.mission_type !== "shopping" && m.status !== "resolved"
  ).length;
  const resolvedMissionsCount = missions.filter((m) => m.status === "resolved").length;

  const sidebar = (
    <Sidebar
      missions={missions}
      selectedId={selectedId}
      isLoading={isMissionsLoading}
      onSelect={(id) => {
        setSelectedId(id);
        setMissionsDrawerOpen(false);
      }}
      onNew={() => {
        setSelectedId(null);
        setMissionsDrawerOpen(false);
      }}
    />
  );

  const contextPanel = (
    <MissionContextPanel
      mission={selected}
      checkout={checkoutSummary}
      order={orderRecord}
      issues={orderIssues}
      delegationNotice={delegationNotice}
      delegationError={delegationError}
      isDelegating={isDelegating}
      onDelegate={isShoppingMission ? handleDelegate : undefined}
      onRevoke={
        isShoppingMission && (selected?.allowed_actions?.length ?? 0) > 0
          ? handleRevokeAuthority
          : undefined
      }
    />
  );

  return (
    <div className="flex h-screen overflow-hidden bg-surface">
      {/* LEFT — the buyer's own missions, exactly as the authenticated list returns them */}
      <div className="hidden h-full md:block">{sidebar}</div>
      {missionsDrawerOpen && (
        <div className="fixed inset-0 z-40 flex md:hidden">
          <button
            aria-label="Close mission list"
            onClick={() => setMissionsDrawerOpen(false)}
            className="flex-1 bg-text-charcoal/30"
          />
          <div className="h-full w-80 max-w-[85%] bg-surface">{sidebar}</div>
        </div>
      )}

      {/* CENTER — one continuous conversation: the primary surface of the product */}
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex shrink-0 items-center gap-3 border-b border-text-charcoal/8 bg-surface px-4 py-3 sm:px-6">
          <button
            onClick={() => setMissionsDrawerOpen(true)}
            aria-label="Open mission list"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-text-charcoal/10 text-text-charcoal/55 transition hover:bg-neutral-sand/25 md:hidden"
          >
            <Menu size={15} />
          </button>

          <div className="flex min-w-0 items-center gap-2">
            <span className="hidden shrink-0 items-center gap-2 font-display text-sm sm:flex">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary-rose text-[10px] text-surface">
                R
              </span>
              RESOLVE
            </span>
            {selected ? (
              <>
                <span className="truncate font-display text-base">{selected.objective}</span>
                <StatusPill status={selected.status} />
              </>
            ) : (
              <span className="truncate text-xs font-semibold uppercase tracking-wider text-accent-terracotta">
                Authenticated workspace
              </span>
            )}
          </div>

          <div className="ml-auto flex shrink-0 items-center gap-2">
            {selected && (
              <button
                onClick={() => setContextDrawerOpen(true)}
                aria-label="Open mission context"
                className="flex h-8 w-8 items-center justify-center rounded-lg border border-text-charcoal/10 text-text-charcoal/55 transition hover:bg-neutral-sand/25 xl:hidden"
              >
                <PanelRightOpen size={15} />
              </button>
            )}
            <button
              onClick={() => refreshMissions()}
              title="Refresh from RESOLVE"
              className="flex h-8 w-8 items-center justify-center rounded-full border border-text-charcoal/10 text-text-charcoal/50 transition hover:bg-neutral-sand/20 hover:text-text-charcoal"
            >
              <RefreshCw size={14} className={isMissionsLoading ? "animate-spin" : ""} />
            </button>
            {selected && (
              <button
                onClick={() => setSelectedId(null)}
                aria-label="Close this mission"
                className="flex h-8 w-8 items-center justify-center rounded-lg border border-text-charcoal/10 text-text-charcoal/55 transition hover:bg-neutral-sand/25"
              >
                <X size={15} />
              </button>
            )}
          </div>
        </header>

        {missionError && (
          <div className="flex items-center justify-between gap-3 bg-primary-rose/10 px-4 py-2 text-xs text-primary-rose sm:px-6">
            <div className="flex items-center gap-2">
              <AlertCircle size={14} className="shrink-0" />
              <span>{missionError}</span>
            </div>
            <button
              onClick={() => refreshMissions()}
              className="font-semibold underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        )}

        <div className="scrollbar-thin flex-1 overflow-y-auto px-4 py-5 sm:px-6">
          {selected ? (
            <div className="mx-auto w-full max-w-3xl space-y-4">
              {currentConversation.length === 0 && (
                <Card className="p-4 text-xs leading-relaxed text-text-charcoal/60">
                  RESOLVE holds no stored conversation for this mission yet. Ask the agent something
                  below — its answers, the tools it ran and the states it reached are recorded from
                  here on.
                </Card>
              )}

              {currentConversation.map((message, index) =>
                message.role === "user" ? (
                  <div key={`user-${index}`} className="flex justify-end">
                    <div className="max-w-xl rounded-2xl bg-accent-terracotta px-4 py-3 text-surface shadow-soft">
                      <p className="text-sm">{message.content}</p>
                    </div>
                  </div>
                ) : message.role === "system" ? (
                  <div key={`system-${index}`} className="flex justify-center">
                    <p className="max-w-xl rounded-lg bg-neutral-sand/25 px-3 py-1.5 text-center text-[11px] text-text-charcoal/55">
                      {message.content}
                    </p>
                  </div>
                ) : (
                  <div key={`agent-${index}`} className="flex justify-start">
                    <Card className="w-full max-w-xl p-4 shadow-card">
                      <div className="mb-2 flex items-center gap-2">
                        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-primary-rose text-[10px] font-bold text-surface">
                          R
                        </span>
                        <span className="text-xs font-semibold text-text-charcoal/70">
                          RESOLVE agent
                        </span>
                      </div>

                      {message.content && (
                        <p className="text-sm text-text-charcoal/85">{message.content}</p>
                      )}

                      {message.tools && message.tools.length > 0 && (
                        <div className="mt-3 rounded-xl bg-neutral-sand/20 p-3">
                          <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/60">
                            Steps the agent ran
                          </p>
                          <ul className="mt-1.5 space-y-1">
                            {message.tools.map((step, stepIndex) => (
                              <li
                                key={`${step.tool}-${stepIndex}`}
                                className="flex items-start gap-1.5 text-xs text-text-charcoal/70"
                              >
                                {step.success ? (
                                  <CheckCircle2
                                    size={13}
                                    className="mt-0.5 shrink-0 text-emerald-700"
                                  />
                                ) : (
                                  <AlertCircle
                                    size={13}
                                    className="mt-0.5 shrink-0 text-primary-rose"
                                  />
                                )}
                                <span>
                                  {humanize(step.tool)}
                                  {step.state ? ` → ${humanize(step.state)}` : ""}
                                  {step.error_code
                                    ? ` (refused: ${humanize(step.error_code)})`
                                    : ""}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {(message.authority ||
                        (message.proposal &&
                          message.proposal.action &&
                          CONSEQUENTIAL_ACTIONS.has(message.proposal.action))) && (
                        <div className="mt-3 space-y-2">
                          {message.proposal?.action &&
                            CONSEQUENTIAL_ACTIONS.has(message.proposal.action) && (
                              <div className="flex flex-wrap items-center gap-2 rounded-xl bg-neutral-sand/20 p-3">
                                <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/60">
                                  Proposed — not executed
                                </p>
                                <span className="text-xs font-semibold text-text-charcoal">
                                  {humanize(message.proposal.action)}
                                </span>
                                {message.proposal.amount && (
                                  <span className="rounded bg-accent-terracotta/15 px-2 py-0.5 text-xs font-semibold text-accent-terracotta">
                                    {formatMoney(message.proposal.amount, "INR")}
                                  </span>
                                )}
                              </div>
                            )}
                          {message.authority && (
                            <div
                              className={`rounded-xl p-3 ${
                                message.authority.approved
                                  ? "bg-emerald-100/50 text-emerald-800"
                                  : "bg-primary-rose/10 text-primary-rose"
                              }`}
                            >
                              <div className="flex items-start gap-2">
                                {message.authority.approved ? (
                                  <CheckCircle2
                                    size={15}
                                    className="mt-0.5 shrink-0 text-emerald-700"
                                  />
                                ) : (
                                  <AlertCircle
                                    size={15}
                                    className="mt-0.5 shrink-0 text-primary-rose"
                                  />
                                )}
                                <div>
                                  <p className="text-xs font-semibold">
                                    {message.authority.approved
                                      ? "Authority Engine: allowed"
                                      : "Authority Engine: refused"}
                                  </p>
                                  <p className="mt-0.5 text-xs opacity-90">
                                    {message.authority.reason || "No reason was returned."}
                                  </p>
                                </div>
                              </div>
                            </div>
                          )}
                        </div>
                      )}

                      {message.products && message.products.length > 0 && (
                        <div className="mt-4 border-t border-text-charcoal/8 pt-3.5">
                          <ProductResults
                            products={message.products}
                            selectedProductId={selectedProductId}
                            selectingProductId={selectingProductId}
                            clarifyingProductId={clarifyingProductId}
                            error={selectionError}
                            onSelectProduct={isShoppingMission ? handleSelectProduct : undefined}
                            onClarifyVariant={isShoppingMission ? handleClarifyVariant : undefined}
                          />
                        </div>
                      )}
                    </Card>
                  </div>
                )
              )}

              {/* A real request is in flight; nothing is animated here that is not actually running. */}
              {isAgentThinking && (
                <div className="flex justify-start">
                  <Card className="p-3.5 shadow-card">
                    <div className="flex items-center gap-2.5 text-text-charcoal/60">
                      <Loader2 size={15} className="animate-spin text-accent-terracotta" />
                      <span className="text-xs font-medium">
                        Waiting for the agent — the answer appears when RESOLVE returns it.
                      </span>
                    </div>
                  </Card>
                </div>
              )}

              {/* Discovery the transcript does not carry: current server state, labelled as state
                  rather than as something the agent said. */}
              {isShoppingMission &&
                discoveredProducts.length > 0 &&
                !productsShownInTranscript && (
                  <Card className="p-4 shadow-card">
                    <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/60">
                      Discovered on this mission
                    </p>
                    <ProductResults
                      products={discoveredProducts}
                      selectedProductId={selectedProductId}
                      selectingProductId={selectingProductId}
                      clarifyingProductId={clarifyingProductId}
                      error={selectionError}
                      onSelectProduct={handleSelectProduct}
                      onClarifyVariant={handleClarifyVariant}
                    />
                  </Card>
                )}

              {isShoppingMission &&
                selectedProduct &&
                selected.status === "product_selected" && (
                  <Card className="flex flex-wrap items-center justify-between gap-3 border-emerald-500/25 bg-emerald-500/10 p-4">
                    <p className="text-xs text-emerald-800">
                      A product is selected on this mission. Nothing has been ordered or paid —
                      opening the checkout review is the next step, and confirming it stays yours.
                    </p>
                    <button
                      onClick={handleOpenCheckout}
                      className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-accent-terracotta px-3.5 py-2 text-[11px] font-semibold text-surface shadow-soft transition hover:opacity-90"
                    >
                      Review checkout
                    </button>
                  </Card>
                )}

              {isShoppingMission &&
                selectedProduct &&
                CHECKOUT_STAGES.includes(selected.status) &&
                !checkoutDismissed &&
                (checkoutSummary ? (
                  <CheckoutReview
                    checkout={checkoutSummary}
                    confirmed={isCheckoutConfirmed}
                    confirmedAt={checkoutConfirmation?.confirmed_at ?? null}
                    isConfirming={isConfirmingCheckout}
                    error={checkoutError}
                    onBack={() => setCheckoutDismissed(true)}
                    onConfirm={handleConfirmCheckout}
                  />
                ) : (
                  <Card className="flex items-center gap-2.5 p-5">
                    <Loader2 size={16} className="animate-spin text-accent-terracotta" />
                    <p className="text-xs font-medium text-text-charcoal/60">
                      Reading the checkout review the server holds for this mission…
                    </p>
                  </Card>
                ))}

              {isShoppingMission &&
                selectedProduct &&
                checkoutSummary &&
                selected.status === "checkout_review" &&
                checkoutDismissed && (
                  <button
                    onClick={() => setCheckoutDismissed(false)}
                    className="w-full rounded-xl border border-accent-terracotta/40 px-4 py-2.5 text-xs font-semibold text-accent-terracotta transition hover:bg-accent-terracotta hover:text-surface"
                  >
                    Resume checkout review
                  </button>
                )}

              {isShoppingMission && selectedProduct && isCheckoutConfirmed && (
                <PaymentPanel
                  payment={paymentRecord}
                  missionStatus={selected.status}
                  checkout={checkoutSummary}
                  isProcessing={isPaying}
                  error={paymentError}
                  onProceed={handleProceedToPayment}
                />
              )}

              {isShoppingMission &&
                selectedProduct &&
                (orderRecord || paymentRecord?.status === "verified") && (
                  <OrderPanel
                    order={orderRecord}
                    canCreate={!orderRecord && selected.status === "payment_verified"}
                    isProcessing={isOrderBusy}
                    error={orderError}
                    onCreate={handleCreateOrder}
                    onConfirm={handleConfirmOrder}
                  />
                )}

              {isShoppingMission &&
                orderRecord &&
                (orderRecord.status === "order_confirmed" ||
                  orderRecord.status === "awaiting_post_purchase_context") && (
                  <IssuePanel
                    issues={orderIssues}
                    isProcessing={isIssueBusy}
                    error={issueError}
                    delegatedActions={selected?.allowed_actions ?? []}
                    onReport={handleReportIssue}
                    onAssess={handleAssessIssue}
                    onReassess={handleReassessIssue}
                    onRefund={handleExecuteRefund}
                    onReplace={handleExecuteReplacement}
                    onReturn={handleExecuteReturn}
                    onResolve={handleResolveIssue}
                    onManualReview={handleManualReview}
                  />
                )}

              <div ref={scrollRef} />
            </div>
          ) : (
            /* No mission open: start one, or pick an existing one back up. */
            <div className="mx-auto w-full max-w-4xl space-y-8 pb-10">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                <Card className="p-5">
                  <p className="text-xs font-medium uppercase tracking-wider text-text-charcoal/50">
                    Shopping Intents
                  </p>
                  <p className="mt-2 font-display text-3xl text-accent-terracotta">
                    {shoppingMissionsCount}
                  </p>
                  <p className="mt-1 text-xs text-text-charcoal/50">Purchase missions structured</p>
                </Card>

                <Card className="p-5">
                  <p className="text-xs font-medium uppercase tracking-wider text-text-charcoal/50">
                    Active Recovery Cases
                  </p>
                  <p className="mt-2 font-display text-3xl text-primary-rose">
                    {activeRecoveryCount}
                  </p>
                  <p className="mt-1 text-xs text-text-charcoal/50">Monitored for deviations</p>
                </Card>

                <Card className="p-5">
                  <p className="text-xs font-medium uppercase tracking-wider text-text-charcoal/50">
                    Resolved Missions
                  </p>
                  <p className="mt-2 font-display text-3xl text-emerald-700">
                    {resolvedMissionsCount}
                  </p>
                  <p className="mt-1 text-xs text-text-charcoal/50">
                    Closed out on RESOLVE&apos;s own records
                  </p>
                </Card>
              </div>

              <div className="rounded-2xl border border-text-charcoal/10 bg-surface/50 p-6 shadow-soft">
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-text-charcoal/8 pb-4">
                  <div>
                    <h2 className="font-display text-base text-text-charcoal">
                      Start an Autonomous Mission
                    </h2>
                    <p className="mt-0.5 text-xs text-text-charcoal/55">
                      Describe a purchase, or report a problem with one you already paid for.
                    </p>
                  </div>

                  <div className="flex rounded-lg border border-text-charcoal/8 bg-neutral-sand/20 p-1">
                    <button
                      onClick={() => setActiveCreationMode("shopping")}
                      className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold transition ${
                        activeCreationMode === "shopping"
                          ? "bg-surface text-accent-terracotta shadow-soft"
                          : "text-text-charcoal/60 hover:text-text-charcoal"
                      }`}
                    >
                      <ShoppingBag size={13} />
                      Shopping Intent
                    </button>
                    <button
                      onClick={() => setActiveCreationMode("recovery")}
                      className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold transition ${
                        activeCreationMode === "recovery"
                          ? "bg-surface text-primary-rose shadow-soft"
                          : "text-text-charcoal/60 hover:text-text-charcoal"
                      }`}
                    >
                      <ShieldCheck size={13} />
                      Recovery Dispute
                    </button>
                  </div>
                </div>

                <div className="mt-4">
                  <div className="flex items-center gap-1.5 pb-2.5">
                    <Sparkles size={14} className="text-accent-terracotta" />
                    <span className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/70">
                      {activeCreationMode === "shopping"
                        ? "Example requests"
                        : "Example recovery cases"}
                    </span>
                  </div>

                  <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
                    {(activeCreationMode === "shopping" ? SHOPPING_PROMPTS : RECOVERY_PROMPTS).map(
                      (prompt, i) => (
                        <button
                          key={i}
                          onClick={() => handleNewMission(prompt)}
                          disabled={isCreatingMission}
                          className="rounded-xl border border-text-charcoal/10 bg-surface p-3 text-left text-xs text-text-charcoal/80 transition hover:border-accent-terracotta/50 hover:bg-neutral-sand/15 disabled:opacity-50"
                        >
                          &quot;{prompt}&quot;
                        </button>
                      )
                    )}
                  </div>

                  {isCreatingMission && (
                    <p className="mt-3 flex items-center gap-2 text-xs text-text-charcoal/60">
                      <Loader2 size={13} className="animate-spin text-accent-terracotta" />
                      Creating the mission on RESOLVE…
                    </p>
                  )}
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between pb-3">
                  <h2 className="font-display text-lg tracking-tight text-text-charcoal">
                    My Missions
                  </h2>
                  <span className="text-xs text-text-charcoal/50">{missions.length} total</span>
                </div>

                {isMissionsLoading ? (
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <div className="h-28 animate-pulse rounded-xl2 bg-neutral-sand/20" />
                    <div className="h-28 animate-pulse rounded-xl2 bg-neutral-sand/20" />
                  </div>
                ) : missions.length === 0 ? (
                  <Card className="p-8 text-center shadow-card">
                    <Shield size={32} className="mx-auto text-accent-terracotta/70" />
                    <h3 className="mt-3 font-display text-base text-text-charcoal">
                      No missions yet
                    </h3>
                    <p className="mx-auto mt-1.5 max-w-sm text-xs text-text-charcoal/60">
                      Describe a purchase or report an issue above to start one.
                    </p>
                  </Card>
                ) : (
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    {missions.map((m) => {
                      const isShop = m.mission_type === "shopping";
                      return (
                        <Card
                          key={m.mission_id}
                          onClick={() => setSelectedId(m.mission_id)}
                          className="cursor-pointer p-5 transition hover:shadow-soft hover:border-accent-terracotta/40 active:scale-[0.99]"
                        >
                          <div className="flex items-start justify-between gap-2">
                            <div className="flex min-w-0 items-start gap-1.5">
                              {isShop ? (
                                <ShoppingBag
                                  size={15}
                                  className="mt-0.5 shrink-0 text-accent-terracotta"
                                />
                              ) : (
                                <ShieldCheck
                                  size={15}
                                  className="mt-0.5 shrink-0 text-primary-rose"
                                />
                              )}
                              <p className="line-clamp-2 text-sm font-medium text-text-charcoal">
                                {m.objective}
                              </p>
                            </div>
                            <StatusPill status={m.status} />
                          </div>
                          <div className="mt-4 flex items-center justify-between border-t border-text-charcoal/8 pt-3 text-xs text-text-charcoal/50">
                            <span>
                              {isShop
                                ? `Budget: ${formatMoney(m.shopping_intent?.budget, m.shopping_intent?.currency)}`
                                : `Spend: ${formatMoney(m.total_recovery_spend, "INR")} of ${formatMoney(m.recovery_budget, "INR")}`}
                            </span>
                            <span className="font-semibold text-accent-terracotta">Open →</span>
                          </div>
                        </Card>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="shrink-0">
          <PromptBox
            onSubmit={selected ? handleAgentMessage : handleNewMission}
            disabled={selected ? isAgentThinking : isCreatingMission}
            placeholder={
              selected
                ? isShoppingMission
                  ? "Tell the agent what to do next — it can search, explain, or prepare a step."
                  : "Describe what happened with this purchase."
                : activeCreationMode === "shopping"
                  ? "Describe what you want to buy (e.g. wireless earbuds under ₹3,000 with long battery life)..."
                  : "Describe a purchase issue to start a recovery mission (e.g. damaged package, late delivery)..."
            }
          />
        </div>
      </main>

      {/* RIGHT — the mission context RESOLVE is currently holding */}
      {selected && (
        <aside className="scrollbar-thin hidden w-[22rem] shrink-0 overflow-y-auto border-l border-text-charcoal/8 bg-neutral-sand/10 px-4 py-4 xl:block">
          {contextPanel}
        </aside>
      )}
      {selected && contextDrawerOpen && (
        <div className="fixed inset-0 z-40 flex justify-end xl:hidden">
          <button
            aria-label="Close mission context"
            onClick={() => setContextDrawerOpen(false)}
            className="flex-1 bg-text-charcoal/30"
          />
          <div className="scrollbar-thin h-full w-[22rem] max-w-[85%] overflow-y-auto bg-surface px-4 py-4">
            {contextPanel}
          </div>
        </div>
      )}
    </div>
  );
}
