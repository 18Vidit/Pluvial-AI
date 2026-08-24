"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Disposition, QueueCard } from "@/lib/types";

const COLUMNS: { keys: Disposition[]; title: string; hint: string; rule: string }[] = [
  { keys: ["dispatch"], title: "Dispatch", hint: "Send a crew today", rule: "bg-oxide" },
  { keys: ["inspect"], title: "Inspect", hint: "Worth a camera or manual check this week", rule: "bg-ochre" },
  { keys: ["monitor", "close"], title: "Monitor / closed", hint: "No action, still watched", rule: "bg-bone-faint" },
];

const PRIORITY_RULE: Record<string, string> = {
  critical: "border-l-oxide",
  high: "border-l-oxide",
  medium: "border-l-ochre",
  low: "border-l-ground-700",
};

function decidedAt(iso: string) {
  return new Date(iso).toLocaleString("en-US", {
    timeZone: "America/Chicago",
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function QueueBoard({ cards }: { cards: QueueCard[] }) {
  const [query, setQuery] = useState("");
  const [reawakenedOnly, setReawakenedOnly] = useState(false);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return cards.filter((c) => {
      if (reawakenedOnly && !c.reawakened) return false;
      if (!q) return true;
      return (
        (c.segment_name ?? "").toLowerCase().includes(q) ||
        (c.reasoning.adjudicator_explanation ?? "").toLowerCase().includes(q)
      );
    });
  }, [cards, query, reawakenedOnly]);

  const reawakenedCount = cards.filter((c) => c.reawakened).length;

  return (
    <>
      <div className="flex flex-wrap items-center gap-3 mb-8">
        <div className="relative flex-1 min-w-[240px] max-w-md">
          <label htmlFor="queue-filter" className="sr-only">
            Filter cases by street or reasoning
          </label>
          <input
            id="queue-filter"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by street or reasoning…"
            className="w-full rounded border border-ground-700 bg-ground-850 px-3.5 py-2.5 text-sm text-bone placeholder:text-bone-faint transition-colors duration-200 focus:border-moisture focus:outline-none"
          />
        </div>

        <button
          type="button"
          onClick={() => setReawakenedOnly((v) => !v)}
          aria-pressed={reawakenedOnly}
          className={`inline-flex items-center gap-2 rounded border px-3.5 py-2.5 text-sm transition-colors duration-200 ${
            reawakenedOnly
              ? "border-moisture bg-moisture/15 text-moisture"
              : "border-ground-700 bg-ground-850 text-bone-dim hover:text-bone"
          }`}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${reawakenedOnly ? "bg-moisture" : "bg-bone-faint"}`}
            aria-hidden="true"
          />
          Re-opened only
          <span className="data text-xs tabular-nums opacity-70">{reawakenedCount}</span>
        </button>

        <span className="data text-xs text-bone-faint tabular-nums ml-auto">
          {filtered.length} / {cards.length} cases
        </span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {COLUMNS.map((col) => {
          const colCards = filtered.filter((c) => col.keys.includes(c.disposition));
          return (
            <section key={col.title} aria-labelledby={`col-${col.title}`}>
              <div className="mb-4">
                <div className="flex items-center gap-2.5">
                  <span className={`h-2.5 w-2.5 rounded-sm ${col.rule}`} aria-hidden="true" />
                  <h2 id={`col-${col.title}`} className="display text-lg text-bone">
                    {col.title}
                  </h2>
                  <span className="data text-xs text-bone-faint tabular-nums">{colCards.length}</span>
                </div>
                <p className="mt-1 text-xs text-bone-dim pl-5">{col.hint}</p>
              </div>

              <div className="space-y-3">
                {colCards.length === 0 && (
                  <p className="rounded border border-dashed border-ground-700 px-4 py-6 text-center text-sm text-bone-faint">
                    {query || reawakenedOnly ? "Nothing matches this filter" : "No cases"}
                  </p>
                )}
                {colCards.map((card) => (
                  <Link
                    key={card.verdict_id}
                    href={`/case/${card.verdict_id}`}
                    className={`block rounded-r border-l-2 border-y border-r border-y-ground-700 border-r-ground-700 bg-ground-800 p-4 transition-colors duration-200 hover:bg-ground-750 ${
                      PRIORITY_RULE[card.priority ?? "low"] ?? PRIORITY_RULE.low
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <span className="text-sm font-medium text-bone leading-snug">
                        {card.segment_name ?? `Segment ${card.segment_id}`}
                      </span>
                      {card.reawakened && (
                        <span className="shrink-0 data rounded-sm bg-moisture/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-moisture">
                          Re-opened
                        </span>
                      )}
                    </div>
                    <p className="mt-2 text-[13px] leading-relaxed text-bone-dim line-clamp-3">
                      {card.reasoning.adjudicator_explanation}
                    </p>
                    <div className="mt-3 flex items-center justify-between gap-3">
                      <span className="data text-[10.5px] text-bone-faint">
                        {decidedAt(card.decided_at)}
                      </span>
                      <span className="data text-[10.5px] text-bone-faint uppercase tracking-wider">
                        {card.priority ?? "n/a"}
                      </span>
                    </div>
                  </Link>
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </>
  );
}
