import { Loader2 } from "lucide-react";
import clsx from "clsx";

export function Spinner({ size = 14, className }: { size?: number; className?: string }) {
  return <Loader2 size={size} className={clsx("shrink-0 animate-spin", className)} aria-hidden="true" />;
}
