"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AgentSide, CitedClaim, VerdictDetail } from "@/lib/types";
import { SectionCut } from "@/components/SectionCut";

const STAGE_MS = 1500;

const DISPOSITION: Record<string, { label: string; action: string; cls: string; dot: string }> = {
  dispatch: { label: "Dispatch", action: "Send a crew today", cls: "text-oxide-bright border-oxide/50 bg-oxide/10", dot: "bg-oxide" },
  inspect: { label: "Inspect", action: "Camera or manual check this week", cls: "text-ochre border-ochre/50 bg-ochre/10", dot: "bg-ochre" },
  monitor: { label: "Monitor", action: "No crew, but keep watching", cls: "text-moisture border-moisture/50 bg-moisture/10", dot: "bg-moisture" },
  close: { label: "Closed", action: "Isolated incident, no action", cls: "text-bone-dim border-ground-700 bg-ground-800", dot: "bg-bone-faint" },
};

function ClaimRow({ claim, muted }: { claim: CitedClaim; muted?: boolean }) {
  return (
    <li className={`border-l border-ground-700 pl-3.5 py-1.5 ${muted ? "opacity-55" : ""}`}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <code className="data text-[11.5px] text-clay-light">{claim.field}</code>
        <span className="data text-[11.5px] text-bone">= {String(claim.value)}</span>
        {claim.source && (
          <span className="data rounded-sm bg-ground-750 px-1.5 py-px text-[9.5px] uppercase tracking-wider text-bone-faint">
            {claim.source}
          </span>
        )}
      </div>
      <p className="mt-1 text-[13px] leading-relaxed text-bone-dim">{claim.interpretation}</p>
    </li>
  );
}

function Stage({
  n,
  name,
  role,
  shown,
  active,
  accent,
  children,
}: {
  n: string;
  name: string;
  role: string;
  shown: boolean;
  active: boolean;
  accent?: "moisture" | "oxide";
  children: React.ReactNode;
}) {
  const border =
    accent === "moisture" ? "border-moisture/45" : accent === "oxide" ? "border-oxide/45" : "border-ground-700";
  return (
    <li
      className={`relative rounded-lg border bg-ground-800 transition-all duration-500 ${
        shown ? `opacity-100 translate-y-0 ${border}` : "opacity-30 translate-y-1 border-ground-700"
      } ${active ? "ring-1 ring-bone/20" : ""}`}
      aria-hidden={!shown}
    >
      <div className="flex items-baseline gap-3 px-5 pt-4">
        <span className="data text-xs text-bone-faint">{n}</span>
        <h3 className="display text-lg text-bone">{name}</h3>
        <span className="ml-auto data text-[10px] uppercase tracking-wider text-bone-faint">{role}</span>
      </div>
      {/* inert, not just aria-hidden: a collapsed stage still contains links,
          and focusable content inside an aria-hidden region traps keyboard users. */}
      <div
        inert={!shown}
        className={`grid transition-all duration-500 ${
          shown ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"
        }`}
      >
        <div className="overflow-hidden">
          <div className="px-5 pb-5 pt-3">{children}</div>
        </div>
      </div>
    </li>
  );
}

export function CaseFile({ detail }: { detail: VerdictDetail }) {
  const { verdict, complaints, segment, moisture, prior_verdict, case_numbers } = detail;

  const [revealed, setRevealed] = useState(0);
  const [playing, setPlaying] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  /* This page is a replay of a recorded decision, and only that. It used to
     carry a "run the agents live" button backed by POST /cascade/run, which
     pinned the Mireye wrapper at ceiling=0 — so the one control advertised as
     proof the system was live provably never called Mireye. It reasoned again
     over a profile fetched months earlier. The live path is /address, where
     the ground is bought at the moment you ask for it. */
  const investigator: AgentSide | null = verdict.reasoning.investigator;
  const skeptic: AgentSide | null = verdict.reasoning.skeptic;
  const explanation = verdict.reasoning.adjudicator_explanation;
  const evidence = verdict.cited_evidence;
  const rejected = verdict.rejected_counter_argument;
  const invalidation = verdict.invalidation_condition;
  const disposition = verdict.disposition ?? "close";
  const priority = verdict.priority;

  const d = DISPOSITION[disposition] ?? DISPOSITION.close;
  const soilUsable = segment?.soil_usable === 1;

  // The argument plays itself on arrival — the page should be alive before
  // anyone reaches for a control. Anyone who has asked for reduced motion gets
  // the whole record at once instead of a timed reveal.
  useEffect(() => {
    const reduced =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) setRevealed(4);
    else setPlaying(true);
  }, []);

  useEffect(() => {
    if (!playing) return;
    if (revealed >= 4) {
      setPlaying(false);
      return;
    }
    timer.current = setTimeout(() => setRevealed((r) => r + 1), revealed === 0 ? 350 : STAGE_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [playing, revealed]);

  const play = useCallback(() => {
    setRevealed(0);
    setPlaying(true);
  }, []);

  const profile = segment?.profile;
  const field = (k: string) => {
    const v = profile?.[k]?.value;
    return v === null || v === undefined ? null : String(v);
  };

  /* A street name alone geocodes to the middle of the street, and Houston
     streets split into dozens of OSM segments — so "Wilcrest Drive" can land
     on a different segment than this case, one that was never profiled. The
     311 title carries the actual address ("… - 9006 WILCREST DR"); use it so
     the lookup resolves to this segment. */
  const streetQuery = (() => {
    const title = complaints[0]?.title ?? "";
    const tail = title.split(" - ").pop()?.trim();
    if (tail && /\d/.test(tail)) return tail;
    return segment?.name ?? "";
  })();

  return (
    <div className="grid lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)] gap-8 xl:gap-12">
      {/* ── the ground ─────────────────────────────────────────────── */}
      <div className="lg:sticky lg:top-20 lg:self-start space-y-5">
        <div className="rounded-lg border border-ground-700 bg-ground-850 p-5">
          <div className="flex items-baseline justify-between gap-3 mb-4">
            <span className="eyebrow">Section cut</span>
            {streetQuery && (
              <Link
                href={`/lookup?q=${encodeURIComponent(streetQuery)}`}
                className="data text-[10.5px] text-bone-faint underline underline-offset-2 hover:text-bone"
              >
                public view →
              </Link>
            )}
          </div>
          <SectionCut
            shrinkSwell={field("soil_shrink_swell_class")}
            drainage={field("soil_drainage_class")}
            mapUnit={field("soil_map_unit_name")}
            bedrockCm={field("bedrock_depth_cm") ? Number(field("bedrock_depth_cm")) : null}
            soilUsable={soilUsable}
            triggerState={moisture?.trigger_state ?? null}
            complaintLabel={complaints[0]?.incident_case_type ?? "311 complaint"}
          />
        </div>

        <div className="rounded-lg border border-ground-700 bg-ground-850 p-5">
          <p className="eyebrow mb-3">The complaint</p>
          <dl className="space-y-2.5">
            {complaints.map((c) => (
              <div key={c.case_number} className="space-y-1.5">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <dt className="text-sm text-bone">{c.incident_case_type}</dt>
                  <dd className="data text-[10.5px] text-bone-faint">{c.case_number}</dd>
                </div>
                {c.title && <p className="text-[13px] leading-relaxed text-bone-dim">{c.title}</p>}
                <p className="data text-[10.5px] text-bone-faint">
                  filed {new Date(c.created_at).toLocaleDateString("en-US", { timeZone: "America/Chicago", dateStyle: "medium" })}
                  {c.status ? ` · ${c.status}` : ""}
                </p>
              </div>
            ))}
            {complaints.length === 0 && (
              <p className="text-sm text-bone-dim">Complaint record not on file.</p>
            )}
          </dl>
        </div>

        {profile && (
          <div className="rounded-lg border border-ground-700 bg-ground-850 p-5">
            <p className="eyebrow mb-3">What the ground reads</p>
            <dl className="space-y-2">
              {[
                ["soil_map_unit_name", "Map unit"],
                ["soil_shrink_swell_class", "Shrink–swell"],
                ["soil_drainage_class", "Drainage"],
                ["soil_hydrologic_group", "Hydrologic group"],
              ].map(([k, label]) => {
                const v = field(k);
                if (!v) return null;
                return (
                  <div key={k} className="flex items-baseline justify-between gap-4 border-b border-ground-700 pb-1.5">
                    <dt className="text-xs text-bone-dim shrink-0">{label}</dt>
                    <dd className="data text-[11.5px] text-bone text-right">{v}</dd>
                  </div>
                );
              })}
            </dl>
            {!soilUsable && (
              <p className="mt-3 rounded border border-ochre/40 bg-ochre/[0.08] px-3 py-2 text-xs leading-relaxed text-ochre">
                Dominant component is Urban land. No soil claim is admissible here — the
                Skeptic vetoes any argument built on one.
              </p>
            )}
          </div>
        )}
      </div>

      {/* ── the argument ───────────────────────────────────────────── */}
      <div>
        <div className="mb-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={play}
            disabled={playing}
            className="inline-flex items-center gap-2 rounded bg-bone px-4 py-2.5 text-sm font-medium text-ground-900 transition-colors duration-200 hover:bg-clay-light disabled:opacity-50"
          >
            <svg width="13" height="13" viewBox="0 0 15 15" fill="none" aria-hidden="true">
              <path d="M4 2.5v10l8-5-8-5z" fill="currentColor" />
            </svg>
            {revealed === 0 ? "Replay the argument" : playing ? "Replaying…" : "Replay again"}
          </button>

          <button
            type="button"
            onClick={() => {
              setPlaying(false);
              setRevealed(4);
            }}
            className="rounded border border-ground-700 px-4 py-2.5 text-sm text-bone-dim transition-colors duration-200 hover:bg-ground-800 hover:text-bone"
          >
            Show all
          </button>

          <Link
            href="/address"
            title="Fetches ground data from Mireye at the moment you ask, for any US address"
            className="ml-auto inline-flex items-center gap-2 rounded border border-moisture/50 bg-moisture/10 px-4 py-2.5 text-sm text-moisture transition-colors duration-200 hover:bg-moisture/20"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-moisture" aria-hidden="true" />
            Run it live on any address
          </Link>
        </div>

        <p className="mb-5 text-xs leading-relaxed text-bone-faint">
          Replay of the decision actually recorded on {new Date(verdict.decided_at).toLocaleDateString("en-US", { timeZone: "America/Chicago", dateStyle: "medium" })}. The reveal is paced for reading; the reasoning is verbatim.
        </p>

        <ol className="space-y-3">
          <Stage n="01" name="Triage" role="gpt-4o-mini" shown={revealed >= 1} active={playing && revealed === 1}>
            <p className="text-[13.5px] leading-relaxed text-bone-dim">
              Promoted. The complaint type and the street&apos;s dossier were enough to warrant a full investigation rather than a discard.
            </p>
          </Stage>

          <Stage n="02" name="Investigator" role="builds the case" shown={revealed >= 2} active={playing && revealed === 2}>
            {investigator ? (
              <>
                <p className="text-[13.5px] leading-relaxed text-bone">{investigator.argument}</p>
                {investigator.claims?.length > 0 && (
                  <ul className="mt-4 space-y-1">
                    {investigator.claims.map((c, i) => (
                      <ClaimRow key={i} claim={c} muted={!soilUsable && c.field.startsWith("soil_") && c.field !== "soil_usable"} />
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <p className="text-sm text-bone-faint">No investigator record.</p>
            )}
          </Stage>

          <Stage
            n="03"
            name="Skeptic"
            role="argues the innocent case"
            shown={revealed >= 3}
            active={playing && revealed === 3}
            accent={skeptic?.soil_claim_vetoed ? "moisture" : undefined}
          >
            {skeptic ? (
              <>
                {skeptic.soil_claim_vetoed && (
                  <p className="mb-3 rounded border border-moisture/45 bg-moisture/[0.09] px-3 py-2 text-xs leading-relaxed text-moisture">
                    <span className="data uppercase tracking-wider">Soil claim vetoed</span>
                    {skeptic.veto_reason ? ` — ${skeptic.veto_reason}` : ""}
                  </p>
                )}
                <p className="text-[13.5px] leading-relaxed text-bone">{skeptic.argument}</p>
                {skeptic.claims?.length > 0 && (
                  <ul className="mt-4 space-y-1">
                    {skeptic.claims.map((c, i) => (
                      <ClaimRow key={i} claim={c} />
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <p className="text-sm text-bone-faint">No skeptic record.</p>
            )}
          </Stage>

          <Stage
            n="04"
            name="Adjudicator"
            role="rules between them"
            shown={revealed >= 4}
            active={playing && revealed === 4}
            accent={disposition === "dispatch" ? "oxide" : undefined}
          >
            <div className={`mb-4 inline-flex items-baseline gap-2.5 rounded border px-3 py-2 ${d.cls}`}>
              <span className={`h-2 w-2 self-center rounded-sm ${d.dot}`} aria-hidden="true" />
              <span className="display text-base">{d.label}</span>
              <span className="text-xs opacity-80">{d.action}</span>
              {priority && (
                <span className="data ml-1 text-[10px] uppercase tracking-wider opacity-70">
                  {priority} priority
                </span>
              )}
            </div>

            <p className="text-[13.5px] leading-relaxed text-bone">{explanation}</p>

            {evidence.length > 0 && (
              <>
                <p className="eyebrow mt-5 mb-2">Evidence that decided it</p>
                <ul className="space-y-1">
                  {evidence.map((c, i) => (
                    <ClaimRow key={i} claim={c} />
                  ))}
                </ul>
              </>
            )}

            {rejected && (
              <>
                <p className="eyebrow mt-5 mb-2">Counter-argument it rejected</p>
                <p className="text-[13px] leading-relaxed text-bone-dim border-l border-ground-700 pl-3.5">
                  {rejected}
                </p>
              </>
            )}

            {invalidation && (
              <>
                <p className="eyebrow mt-5 mb-2">This re-opens if</p>
                <p className="rounded border border-ground-700 bg-ground-850 px-3.5 py-2.5 text-[13px] leading-relaxed text-bone-dim">
                  {invalidation.plain_english}
                </p>
              </>
            )}

            {prior_verdict && (
              <p className="mt-5 rounded border border-moisture/40 bg-moisture/[0.08] px-3.5 py-2.5 text-xs leading-relaxed text-moisture">
                Re-opened by the system on its own. It previously ruled{" "}
                <Link href={`/case/${prior_verdict.verdict_id}`} className="underline underline-offset-2">
                  {DISPOSITION[prior_verdict.disposition]?.label ?? prior_verdict.disposition}
                </Link>{" "}
                on this street, then the ground state changed and the condition above was met.
              </p>
            )}
          </Stage>
        </ol>
      </div>
    </div>
  );
}
