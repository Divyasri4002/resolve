import { AlertCircle, X } from "lucide-react";

interface ErrorBannerProps {
  message: string;
  onDismiss?: () => void;
}

/** role="alert" so screen readers announce failures immediately. */
export function ErrorBanner({ message, onDismiss }: ErrorBannerProps) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2.5 rounded-xl border border-status-failure/20 bg-status-failure-soft p-3.5 text-sm text-status-failure"
    >
      <AlertCircle size={16} className="mt-0.5 shrink-0" />
      <p className="min-w-0 flex-1 break-words font-medium">{message}</p>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss error"
          className="rounded p-0.5 text-status-failure/70 transition hover:text-status-failure"
        >
          <X size={14} />
        </button>
      )}
    </div>
  );
}
