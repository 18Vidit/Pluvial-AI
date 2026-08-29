"use client";

import { useEffect, useRef } from "react";

import { LaneEntry, LaneState, Severity, Threat } from "@/lib/address-types";

export const THREAT_LABEL: Record<Threat, string> = {
  foundation: "Foundation",
  service_lines: "Service lines",
  subsidence: "Subsidence",
};

export const THREAT_MECHANISM: Record<Threat, string> = {
  foundation: "shrink-swell · bedrock depth · trigger state",
  service_lines: "shrink-swell · erodibility · drainage",
  subsidence: "erodibility · hydrologic group · karst",
};

/* `unresolved` is deliberately not styled as an absence. It is an assertive
   finding — no soil answer exists at these points — and greying it out would
   read as "we didn't get to this one". */
const SEVERITY_STYLE: Record<Severity, { dot: string; text: string; label: string }> = {
  high: { dot: "bg-oxide", text: "text-oxide-bright", label: "High" },
  elevated: { dot: "bg-ochre", text: "text-ochre", label: "Elevated" },
  low: { dot: "bg-moisture", text: "text-moisture", label: "Low" },
  unresolved: { dot: "bg-clay-light", text: "text-clay-light", label: "Unresolved" },
};

const STAGE_LABEL: Record<string, string> = {
  investigator: "Investigator building the case",
  skeptic: "Skeptic testing it",
  adjudicator: "Adjudicator ruling",
};

function EntryRow({
  entry,
  onHoverSample,
  onSelectSample,
}: {
  entry: LaneEntry;
  onHoverSample?: (id: number | null) => void;
  onSelectSample?: (id: number) => void;
}) {
  const ids = entry.sample_ids ?? (entry.sample_id != null ? [entry.sample_id] : []);

  if (entry.kind === "tool_call") {
    return (
      <li className="animate-rise flex items-baseline gap-2 py-1 text-[12.5px] text-bone-faint">
        <span aria-hidden className="text-moisture-deep">▸</span>
        <span className="data">{entry.text}</span>
        {entry.sample_id != null && <PointChip id={entry.sample_id} onSelectSample={onSelectSample} />}
      </li>
    );
  }

  if (entry.kind === "stage") {
    return (
      <li className="animate-rise py-1.5 text-[12px] eyebrow" style={{ color: "var(--moisture)" }}>
        {STAGE_LABEL[entry.text] ?? entry.text}
      </li>
    );
  }

  if (entry.kind === "veto") {
    return (
      <li className="animate-rise my-1.5 rounded border border-oxide/45 bg-oxide/10 p-2.5">
        <div className="flex items-center gap-2 text-[12.5px] text-oxide-bright">
          <span aria-hidden>✕</span>
          <span className="font-medium">{entry.text}</span>
        </div>
        {entry.detail && <p className="mt-1 text-[12.5px] leading-snug text-bone-dim">{entry.detail}</p>}
        {ids.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {ids.map((id) => (
              <PointChip key={id} id={id} tone="veto" onSelectSample={onSelectSample} />
            ))}
          </div>
        )}
      </li>
    );
  }

  if (entry.kind === "message") {
    return (
      <li className="animate-rise my-1 border-l-2 border-ground-700 py-0.5 pl-2.5 text-[12.5px] leading-snug text-bone-dim">
        {entry.text}
      </li>
    );
  }

  if (entry.kind === "ruling") return null; // rendered as the lane's header

  const sideColor = entry.side === "skeptic" ? "border-ochre/50" : "border-moisture/50";
  return (
    <li
      className={`animate-rise my-1 border-l-2 ${sideColor} py-0.5 pl-2.5`}
      onMouseEnter={() => entry.sample_id != null && onHoverSample?.(entry.sample_id)}
      onMouseLeave={() => onHoverSample?.(null)}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="data text-[12.5px] text-bone">{entry.text}</span>
        {entry.sample_id != null ? (
          <PointChip id={entry.sample_id} onSelectSample={onSelectSample} />
        ) : (
          <span className="eyebrow" style={{ fontSize: "0.625rem" }}>
            regional
          </span>
        )}
      </div>
      {entry.detail && <p className="mt-0.5 text-[12.5px] leading-snug text-bone-dim">{entry.detail}</p>}
      {entry.source && (
        <p className="mt-0.5 data text-[11px] text-bone-faint">source: {entry.source}</p>
      )}
    </li>
  );
}

function PointChip({
  id,
  tone = "cited",
  onSelectSample,
}: {
  id: number;
  tone?: "cited" | "veto";
  onSelectSample?: (id: number) => void;
}) {
  const cls =
    tone === "veto"
      ? "border-oxide/50 text-oxide-bright"
      : "border-moisture/50 text-moisture";
  return (
    <button
      type="button"
      onClick={() => onSelectSample?.(id)}
      className={`data rounded border ${cls} px-1.5 py-px text-[10.5px] transition-colors hover:bg-ground-750`}
      title="Show this point's raw Mireye values"
    >
      point {id}
    </button>
  );
}

export function ThreatLane({
  lane,
  isExpanded = true,
  onToggle,
  onHoverSample,
  onSelectSample,
}: {
  lane: LaneState;
  isExpanded?: boolean;
  onToggle?: () => void;
  onHoverSample?: (id: number | null) => void;
  onSelectSample?: (id: number) => void;
}) {
  const scroller = useRef<HTMLDivElement | null>(null);

  /* Follow the tail while the argument is still being built, but stop the
     moment a ruling lands — that is when someone starts reading rather than
     watching, and yanking the scroll out from under them would be rude. */
  useEffect(() => {
    if (lane.ruling) return;
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lane.entries.length, lane.ruling]);

  const severity = lane.ruling ? SEVERITY_STYLE[lane.ruling.severity] : null;

  return (
    <section
      className={[
        "flex flex-col rounded-lg border border-ground-700 bg-ground-850",
        "transition-all duration-300 ease-in-out overflow-hidden",
        isExpanded ? "flex-[3] min-h-0" : "flex-1 min-h-0",
      ].join(" ")}
    >
      <header
        className={[
          "border-b border-ground-700 px-3.5 py-3",
          onToggle ? "cursor-pointer select-none hover:bg-ground-800 transition-colors" : "",
        ].join(" ")}
        onClick={onToggle}
        role={onToggle ? "button" : undefined}
        tabIndex={onToggle ? 0 : undefined}
        onKeyDown={onToggle ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(); } } : undefined}
      >
        <div className="flex items-baseline justify-between gap-2">
          <div className="flex items-baseline gap-2">
            {onToggle && (
              <span
                className={[
                  "inline-block text-[11px] text-bone-faint transition-transform duration-300",
                  isExpanded ? "rotate-90" : "rotate-0",
                ].join(" ")}
                aria-hidden
              >
                ▶
              </span>
            )}
            <h3 className="display text-[15px] text-bone">{THREAT_LABEL[lane.threat]}</h3>
          </div>
          {severity ? (
            <span className={`flex items-center gap-1.5 text-[12px] ${severity.text}`}>
              <span className={`h-2 w-2 rounded-full ${severity.dot}`} aria-hidden />
              {severity.label}
            </span>
          ) : lane.stage ? (
            <span className="eyebrow animate-pulse" style={{ color: "var(--moisture)" }}>
              {lane.stage}
            </span>
          ) : (
            <span className="eyebrow">waiting</span>
          )}
        </div>
        <p className="data mt-1 text-[11px] text-bone-faint">{THREAT_MECHANISM[lane.threat]}</p>
      </header>

      {/* Evidence trail — expanded lanes get generous space, collapsed lanes
          still show a scrollable peek so they're never fully hidden. */}
      <div
        ref={scroller}
        className={[
          "overflow-y-auto px-3.5 py-2 transition-all duration-300",
          isExpanded ? "flex-1 min-h-[80px]" : "min-h-[40px] max-h-[100px]",
        ].join(" ")}
      >
        {lane.entries.length === 0 && (
          <p className="py-6 text-center text-[12.5px] text-bone-faint">
            Waiting for the ground to arrive.
          </p>
        )}
        <ul>
          {lane.entries.map((entry) => (
            <EntryRow
              key={entry.seq}
              entry={entry}
              onHoverSample={onHoverSample}
              onSelectSample={onSelectSample}
            />
          ))}
        </ul>
      </div>

      {lane.ruling && (
        // min-h-0 + overflow-y-auto + max-h together, not any one alone: a
        // plain block child of a flex column has an implicit min-height
        // equal to its own content size unless overflow is anything but
        // visible, so flexbox refuses to shrink this box below its content
        // height (observed live: 244px of ruling text in a 111px section)
        // and starves the evidence scroller above it down to nothing. The
        // cap keeps a long ruling from doing the same in reverse; the
        // overflow makes it scroll internally instead of visually bleeding
        // past its own border into the lane below it.
        <div
          className={[
            "min-h-0 overflow-y-auto border-t border-ground-700 px-3.5 py-3 transition-all duration-300",
            isExpanded ? "max-h-60" : "max-h-20",
          ].join(" ")}
        >
          <p className="text-[13px] leading-snug text-bone">{lane.ruling.explanation}</p>

          {isExpanded && lane.ruling.unknowns.length > 0 && (
            <div className="mt-2.5">
              <p className="eyebrow">What is unknown</p>
              <ul className="mt-1 space-y-1">
                {lane.ruling.unknowns.map((u, i) => (
                  <li key={i} className="text-[12.5px] leading-snug text-clay-light">
                    {u}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {isExpanded && lane.ruling.rejected_counter_argument && (
            <div className="mt-2.5">
              <p className="eyebrow">Rejected counter-argument</p>
              <p className="mt-1 text-[12.5px] leading-snug text-bone-dim">
                {lane.ruling.rejected_counter_argument}
              </p>
            </div>
          )}

          {isExpanded && lane.ruling.invalidation_condition && (
            <div className="mt-2.5">
              <p className="eyebrow">Reopens if</p>
              <p className="mt-1 text-[12.5px] leading-snug text-moisture">
                {lane.ruling.invalidation_condition.plain_english}
              </p>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
