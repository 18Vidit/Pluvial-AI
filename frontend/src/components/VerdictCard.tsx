"use client";

import { useState } from "react";
import { QueueCard } from "@/lib/types";

const PRIORITY_COLOR: Record<string, string> = {
  critical: "border-red-500",
  high: "border-orange-500",
  medium: "border-yellow-500",
  low: "border-neutral-300 dark:border-neutral-700",
};

export function VerdictCard({ card }: { card: QueueCard }) {
  const [expanded, setExpanded] = useState(false);
  const borderColor = PRIORITY_COLOR[card.priority ?? "low"] ?? PRIORITY_COLOR.low;

  return (
    <div className={`rounded-lg border-l-4 ${borderColor} border-t border-r border-b border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 shadow-sm`}>
      <button
        className="w-full text-left p-4"
        onClick={() => setExpanded((v) => !v)}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-medium text-sm truncate">
            {card.segment_name ?? `Segment ${card.segment_id}`}
          </span>
          {card.reawakened && (
            <span className="shrink-0 text-xs font-semibold px-2 py-0.5 rounded-full bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200">
              Reawakened
            </span>
          )}
        </div>
        <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400 line-clamp-2">
          {card.reasoning.adjudicator_explanation}
        </p>
        <p className="mt-2 text-xs text-neutral-400">
          {new Date(card.decided_at).toLocaleString()} · priority: {card.priority ?? "n/a"}
        </p>
      </button>

      {expanded && (
        <div className="border-t border-neutral-200 dark:border-neutral-800 p-4 text-sm space-y-4">
          {card.reasoning.investigator && (
            <section>
              <h4 className="font-semibold text-neutral-700 dark:text-neutral-300">Investigator</h4>
              <p className="mt-1 text-neutral-600 dark:text-neutral-400">{card.reasoning.investigator.argument}</p>
            </section>
          )}
          {card.reasoning.skeptic && (
            <section>
              <h4 className="font-semibold text-neutral-700 dark:text-neutral-300">
                Skeptic
                {card.reasoning.skeptic.soil_claim_vetoed && (
                  <span className="ml-2 text-xs font-normal text-amber-700 dark:text-amber-400">
                    (soil claim vetoed: {card.reasoning.skeptic.veto_reason})
                  </span>
                )}
              </h4>
              <p className="mt-1 text-neutral-600 dark:text-neutral-400">{card.reasoning.skeptic.argument}</p>
            </section>
          )}

          <section>
            <h4 className="font-semibold text-neutral-700 dark:text-neutral-300">Decisive evidence</h4>
            <ul className="mt-1 space-y-1">
              {card.cited_evidence.map((claim, i) => (
                <li key={i} className="text-neutral-600 dark:text-neutral-400">
                  <span className="font-mono text-xs bg-neutral-100 dark:bg-neutral-800 px-1 rounded">
                    {claim.field}={claim.value}
                  </span>{" "}
                  {claim.interpretation}
                  {claim.source && <span className="text-neutral-400"> — {claim.source}</span>}
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h4 className="font-semibold text-neutral-700 dark:text-neutral-300">Rejected counter-argument</h4>
            <p className="mt-1 text-neutral-600 dark:text-neutral-400">{card.rejected_counter_argument}</p>
          </section>

          {card.invalidation_condition && (
            <section>
              <h4 className="font-semibold text-neutral-700 dark:text-neutral-300">Re-opens if</h4>
              <p className="mt-1 text-neutral-600 dark:text-neutral-400">{card.invalidation_condition.plain_english}</p>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
