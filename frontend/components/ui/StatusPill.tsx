import clsx from "clsx";
import type { MissionStatus } from "@/lib/types";
import { missionStatusLabel } from "@/lib/format";
import { MISSION_STATUS_TONES, TONE_CLASSES } from "@/lib/statusTone";

export function StatusPill({ status }: { status: MissionStatus }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center whitespace-nowrap rounded-full px-3 py-1 text-xs font-medium",
        TONE_CLASSES[MISSION_STATUS_TONES[status]]
      )}
    >
      {missionStatusLabel(status)}
    </span>
  );
}
