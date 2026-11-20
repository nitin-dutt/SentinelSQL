import type { ReactNode } from "react";

export type Tone = "ok" | "warn" | "bad" | "info" | "idle";

/** The stamp every answer, queue item and log row carries: a status dot, the outcome, and the check that produced it. */
export default function Verdict({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className="verdict">
      <span className={`dot dot-${tone}`} aria-hidden="true" />
      <span>{children}</span>
    </span>
  );
}
