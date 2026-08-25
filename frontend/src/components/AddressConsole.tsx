"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { ChatComposer, ChatTurn, PendingQuote } from "@/components/ChatComposer";
import { GroundMap } from "@/components/GroundMap";
import { SampleInspector } from "@/components/SampleInspector";
import { THREAT_LABEL, ThreatLane } from "@/components/ThreatLane";
import { chatConfirmPath, chatPath, fetchAnalysis, PlanError, planAnalysis, runPath } from "@/lib/address-api";
import { THREATS } from "@/lib/address-types";
import { AnalysisState, initialState, reduce, StoredAnalysis } from "@/lib/analysis-state";
import { streamEvents } from "@/lib/stream";

/* Every one of these has actually been run, and the note says what came
   back — including the ones that come back `unresolved`. Offering only the
   addresses that resolve well would make the coverage boundary look like an
   accident when a reviewer's own address hits it.

   Brenham is small-town Texas on real SSURGO soil. Bowling Green sits on
   Kentucky karst: the soil gate fires at all nine points and the rulings
   still land, because karst mapping is not SSURGO and survives the veto.
   Golden and Kingwood are the honest failure mode. */
const EXAMPLES = [
  { label: "Brenham, TX", value: "100 W Alamo St, Brenham, TX", note: "soil resolves at all 9 points" },
  { label: "Bowling Green, KY", value: "1001 College St, Bowling Green, KY", note: "karst; soil gate fires, rulings still land" },
  { label: "Golden, CO", value: "1200 Washington Ave, Golden, CO", note: "urban core; unresolved" },
  { label: "Kingwood, TX", value: "1000 Kingwood Drive, Kingwood, TX", note: "suburban but mapped Urban land" },
];

function CreditCounter({ spent, quoted }: { spent: number; quoted: number | null }) {
  return (
    <div className="data flex items-baseline gap-1.5 text-[12px]">
      <span className="text-bone-faint">credits</span>
      <span className="text-[15px] text-moisture tabular-nums">{spent}</span>
      {quoted !== null && <span className="text-bone-faint">/ {quoted} quoted</span>}
    </div>
  );
}

export function AddressConsole({ locationId }: { locationId?: number }) {
  const [state, dispatch] = useReducer(reduce, undefined, initialState);
  const [address, setAddress] = useState("");
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const abort = useRef<AbortController | null>(null);

  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [quote, setQuote] = useState<PendingQuote | null>(null);
  const [chatBusy, setChatBusy] = useState(false);
  /* One session per tab. Thread state lives in the server process keyed by
     this, and dies with it — nothing about an unconfirmed quote should
     outlive the conversation that produced it. */
  const sessionId = useRef<string>(
    typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()),
  );
  const turnSeq = useRef(0);
  const pushTurn = useCallback((side: ChatTurn["side"], text: string) => {
    turnSeq.current += 1;
    setTurns((t) => [...t, { id: turnSeq.current, side, text }]);
  }, []);

  const plan = state.plan;

  /* Step one. Geocodes, lays out nine points, and asks Mireye what they
     cost. Spends nothing — there is no code path from here to a fetch. */
  const doPlan = useCallback(async (value: string) => {
    const q = value.trim();
    if (!q) return;
    abort.current?.abort();
    setPlanning(true);
    setPlanError(null);
    setSelected(null);
    dispatch({ kind: "reset" });
    try {
      dispatch({ kind: "plan", plan: await planAnalysis(q) });
    } catch (e) {
      setPlanError(
        e instanceof PlanError && e.status === 404
          ? "No US address matched that. Try adding a city and state."
          : e instanceof Error
            ? e.message
            : "Planning failed.",
      );
    } finally {
      setPlanning(false);
    }
  }, []);

  /* Step two, and the only thing in this interface that spends money. It
     runs because someone clicked it. */
  const doRun = useCallback(async () => {
    if (!plan) return;
    const controller = new AbortController();
    abort.current = controller;
    dispatch({ kind: "running", running: true });
    try {
      for await (const event of streamEvents(runPath(plan.location_id), {
        signal: controller.signal,
      })) {
        dispatch({ kind: "event", event });
      }
      dispatch({ kind: "running", running: false });
    } catch (e) {
      if (controller.signal.aborted) return;
      dispatch({ kind: "error", message: e instanceof Error ? e.message : "Run failed." });
    }
  }, [plan]);

  /* Chat events flow through the SAME reducer as the primary run, which is
     the point: a point fetched because someone asked a question in prose
     paints on the map exactly as one fetched from the address box. */
  const consumeStream = useCallback(
    async (path: string, body?: unknown) => {
      for await (const event of streamEvents(path, { method: "POST", body })) {
        if (event.lane === "chat") {
          if (event.type === "message") {
            const side = (event.payload as { side?: string; text?: string }).side;
            pushTurn(side === "system" ? "system" : "assistant", String((event.payload as { text?: string }).text ?? ""));
          } else if (event.type === "tool_call") {
            pushTurn("tool", String((event.payload as { label?: string }).label ?? "working"));
          } else if (event.type === "error") {
            pushTurn("system", String((event.payload as { message?: string }).message ?? "failed"));
          }
          if (event.type === "done") {
            const handoff = event.payload as { handoff?: string; location_id?: number };
            if (handoff.handoff === "analyze_run" && handoff.location_id != null) {
              // A confirmed new address re-enters the primary flow rather
              // than getting a parallel implementation inside chat.
              for await (const runEvent of streamEvents(runPath(handoff.location_id))) {
                dispatch({ kind: "event", event: runEvent });
              }
            }
          }
          continue;
        }
        if (event.type === "quote") {
          setQuote(event.payload as unknown as PendingQuote);
          continue;
        }
        dispatch({ kind: "event", event });
      }
    },
    [pushTurn],
  );

  const sendChat = useCallback(
    async (message: string) => {
      if (!plan) return;
      pushTurn("user", message);
      setChatBusy(true);
      setQuote(null);
      try {
        await consumeStream(chatPath(), {
          session_id: sessionId.current,
          location_id: plan.location_id,
          message,
        });
      } catch (e) {
        pushTurn("system", e instanceof Error ? e.message : "Chat failed.");
      } finally {
        setChatBusy(false);
      }
    },
    [plan, consumeStream, pushTurn],
  );

  const confirmQuote = useCallback(async () => {
    if (!quote) return;
    setChatBusy(true);
    const path = chatConfirmPath(sessionId.current, quote.pending_id);
    setQuote(null);
    try {
      await consumeStream(path);
    } catch (e) {
      pushTurn("system", e instanceof Error ? e.message : "Confirm failed.");
    } finally {
      setChatBusy(false);
    }
  }, [quote, consumeStream, pushTurn]);

  /* ?location=N reopens a finished analysis. A result you cannot return to
     is not much of a result — and it is also what makes the rulings, the
     map states and the chat survive a refresh mid-demo. */
  useEffect(() => {
    if (locationId == null) return;
    let cancelled = false;
    (async () => {
      try {
        const stored = (await fetchAnalysis(locationId)) as StoredAnalysis;
        if (!cancelled) {
          dispatch({ kind: "hydrate", stored });
          setAddress(stored.location.query_text);
        }
      } catch (e) {
        if (!cancelled) setPlanError(e instanceof Error ? e.message : "Could not load that analysis.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [locationId]);

  const selectedSample = useMemo(
    () => state.samples.find((s) => s.sample_id === selected) ?? null,
    [state.samples, selected],
  );

  /* Distances, not coordinates. Mireye returns "how far to the nearest
     school", so a ring is the honest drawing and a pin would invent a
     position that was never in the data. */
  const consequences = useMemo(() => {
    const property = state.samples.find((s) => s.role === "property");
    const profile = property?.profile;
    if (!profile) return [];
    return [
      { label: "nearest school", key: "nearest_school_distance_m" },
      { label: "nearest hospital", key: "nearest_hospital_distance_m" },
    ]
      .map(({ label, key }) => ({ label, distance_m: Number(profile[key]?.value ?? NaN) }))
      .filter((c) => Number.isFinite(c.distance_m) && c.distance_m > 0);
  }, [state.samples]);

  const fetched = state.samples.filter((s) => s.profile).length;
  const usable = state.samples.filter((s) => s.soil_usable).length;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* ── the ask ──────────────────────────────────────────────────── */}
      <div className="border-b border-ground-700 bg-ground-900/95 px-5 py-4 sm:px-8">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            doPlan(address);
          }}
          className="mx-auto flex w-full max-w-7xl flex-col gap-2.5 sm:flex-row"
        >
          <label htmlFor="address" className="sr-only">
            Any US address
          </label>
          <input
            id="address"
            type="text"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="Any US address — street, city, state"
            autoComplete="street-address"
            className="flex-1 rounded border border-ground-700 bg-ground-850 px-4 py-3 text-[15px] text-bone placeholder:text-bone-faint transition-colors duration-200 focus:border-moisture focus:outline-none"
          />
          <button
            type="submit"
            disabled={planning || !address.trim()}
            className="rounded bg-bone px-5 py-3 text-[14px] font-medium text-ground-900 transition-opacity duration-200 disabled:opacity-40"
          >
            {planning ? "Planning…" : "Plan the sample"}
          </button>
        </form>

        <div className="mx-auto mt-2.5 flex w-full max-w-7xl flex-wrap items-center gap-x-3 gap-y-1.5">
          <span className="eyebrow">try</span>
          {EXAMPLES.map((ex) => (
            <button
              key={ex.value}
              type="button"
              title={ex.note}
              onClick={() => {
                setAddress(ex.value);
                doPlan(ex.value);
              }}
              className="text-[12.5px] text-bone-dim underline decoration-ground-700 underline-offset-4 transition-colors hover:text-bone"
            >
              {ex.label}
            </button>
          ))}
        </div>

        {planError && <p className="mx-auto mt-2 w-full max-w-7xl text-[13px] text-oxide-bright">{planError}</p>}
      </div>

      {/* ── the quote gate ───────────────────────────────────────────── */}
      {plan && !state.restored && !state.running && !state.finished && (
        <div className="border-b border-ground-700 bg-ground-850 px-5 py-3.5 sm:px-8">
          <div className="mx-auto flex w-full max-w-7xl flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-[13.5px] text-bone">{plan.label}</p>
              <p className="data mt-0.5 text-[12px] text-bone-faint">
                {plan.n_points} points × {plan.n_fields} fields ·{" "}
                <span className="text-clay-light">{plan.quoted_credits} credits</span> · moisture from{" "}
                {plan.station.name} ({(plan.station.distance_m / 1000).toFixed(1)} km away)
              </p>
            </div>
            <button
              type="button"
              onClick={doRun}
              className="rounded bg-moisture px-5 py-2.5 text-[14px] font-medium text-ground-900 transition-opacity duration-200 hover:opacity-90"
            >
              Fetch {plan.quoted_credits} credits & run
            </button>
          </div>
        </div>
      )}

      {/* ── map + lanes ──────────────────────────────────────────────── */}
      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
        <div className="relative min-h-[380px] lg:min-h-0">
          <GroundMap
            center={state.center}
            samples={state.samples}
            consequences={consequences}
            selectedSampleId={selected}
            onSelectSample={setSelected}
            cells={state.cells}
            cellBounds={state.cellBounds}
          />

          <div className="pointer-events-none absolute left-3 top-3 rounded border border-ground-700 bg-ground-900/88 px-3 py-2 backdrop-blur-sm">
            {state.restored ? (
              <p className="eyebrow">restored from memory</p>
            ) : (
              <CreditCounter spent={state.creditsSpent} quoted={plan?.quoted_credits ?? null} />
            )}
            {state.samples.length > 0 && (
              <p className="data mt-1 text-[11px] text-bone-faint">
                {fetched}/{state.samples.length} points fetched · {usable} with soil data
              </p>
            )}
          </div>

          {selectedSample && (
            <div className="absolute bottom-3 left-3 right-3 max-h-[62%] sm:right-auto sm:w-[360px]">
              <SampleInspector sample={selectedSample} onClose={() => setSelected(null)} />
            </div>
          )}

          {!plan && (
            <div className="pointer-events-none absolute inset-x-0 bottom-6 flex justify-center px-5">
              <p className="max-w-md rounded border border-ground-700 bg-ground-900/88 px-4 py-3 text-center text-[13px] leading-snug text-bone-dim backdrop-blur-sm">
                Type an address. Nine points get planned around it and quoted before anything is
                spent — the ground is fetched only when you say so.
              </p>
            </div>
          )}
        </div>

        <div className="flex min-h-0 flex-col gap-3 border-t border-ground-700 p-3 lg:border-l lg:border-t-0">
          {state.triage && (
            <div className="rounded-lg border border-ground-700 bg-ground-850 px-3.5 py-2.5">
              <p className="eyebrow">triage · {state.triage.decision}</p>
              <p className="mt-1 text-[12.5px] leading-snug text-bone-dim">{state.triage.reason}</p>
            </div>
          )}

          {state.systemLog.length > 0 && !state.triage && (
            <div className="rounded-lg border border-ground-700 bg-ground-850 px-3.5 py-2.5">
              <p className="data text-[12px] text-bone-dim">
                {state.systemLog[state.systemLog.length - 1].text}
              </p>
            </div>
          )}

          {state.error && (
            <div className="rounded-lg border border-oxide/50 bg-oxide/10 px-3.5 py-2.5 text-[13px] text-oxide-bright">
              {state.error}
            </div>
          )}

          <div className="grid min-h-0 flex-1 grid-rows-3 gap-3">
            {THREATS.map((threat) => (
              <ThreatLane
                key={threat}
                lane={state.lanes[threat]}
                onSelectSample={setSelected}
              />
            ))}
          </div>

          <ChatComposer
            turns={turns}
            quote={quote}
            busy={chatBusy}
            disabled={!state.finished}
            onSend={sendChat}
            onConfirm={confirmQuote}
            onDismissQuote={() => setQuote(null)}
          />

          {state.finished && (
            <p className="data text-center text-[11.5px] text-bone-faint">
              {THREATS.map((t) => `${THREAT_LABEL[t]}: ${state.lanes[t].ruling?.severity ?? "—"}`).join(
                " · ",
              )}{" "}
              · {state.creditsSpent} credits spent
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

export type { AnalysisState };
