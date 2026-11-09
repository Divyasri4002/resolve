/**
 * Razorpay Checkout.js loader (Task 07 — TEST MODE only).
 *
 * The script is Razorpay's documented browser SDK. Only the publishable key id
 * ever reaches this layer — the key secret stays backend-only and is never
 * imported here.
 */
export interface RazorpaySuccessResponse {
  razorpay_order_id: string;
  razorpay_payment_id: string;
  razorpay_signature: string;
}

export interface RazorpayCheckoutOptions {
  key: string;
  order_id: string;
  /** Amount in the smallest currency unit (paise) — always server-derived. */
  amount: number;
  currency: string;
  name: string;
  description?: string;
  theme?: { color?: string };
  prefill?: { name?: string; email?: string };
  handler: (response: RazorpaySuccessResponse) => void;
  modal?: { ondismiss?: () => void };
}

interface RazorpayInstance {
  open: () => void;
}

declare global {
  interface Window {
    Razorpay?: new (options: RazorpayCheckoutOptions) => RazorpayInstance;
  }
}

const CHECKOUT_SCRIPT_URL = "https://checkout.razorpay.com/v1/checkout.js";

export function loadRazorpayCheckout(): Promise<boolean> {
  return new Promise((resolve) => {
    if (typeof window === "undefined") {
      resolve(false);
      return;
    }
    if (window.Razorpay) {
      resolve(true);
      return;
    }
    const existing = document.querySelector<HTMLScriptElement>(
      "script[data-razorpay-checkout='true']"
    );
    if (existing) {
      existing.addEventListener("load", () => resolve(true));
      existing.addEventListener("error", () => resolve(false));
      return;
    }
    const script = document.createElement("script");
    script.src = CHECKOUT_SCRIPT_URL;
    script.async = true;
    script.dataset.razorpayCheckout = "true";
    script.onload = () => resolve(true);
    script.onerror = () => resolve(false);
    document.body.appendChild(script);
  });
}
