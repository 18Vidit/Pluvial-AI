/** Folds the SSE envelope into the state the map and the lanes render from.
 *
 *  Kept out of the components on purpose: the binding between a claim and a
 *  point on the map is the load-bearing idea of this whole interface, and it
 *  is one function here rather than scattered across three components that
 *  each have a slightly different idea of when a point counts as "cited".
 */
import {
  AddressCitedClaim,
  AnalysisPlan,
  CellBBox,
  CellView,
  LaneEntry,
  LaneState,
  SampleProfile,
  SampleView,
  StreamEvent,
  THREATS,
  Threat,
  ThreatRuling,
} from "./address-types";

export interface AnalysisState {
  plan: AnalysisPlan | null;
  /** True when this view was rebuilt from memory rather than streamed. The
   *  credit counter must not claim a run that did not happen in this tab. */
  restored: boolean;
  /** Region-search cells. Separate from `samples` because they answer a
   *  different question — where to look next, not what is under one address. */
  cells: CellView[];
  cellBounds: CellBBox | null;
  center: { lat: number; lon: number } | null;
  samples: SampleView[];
  lanes: Record<Threat, LaneState>;
  systemLog: LaneEntry[];
  triage: { decision: string; reason: string } | null;
  creditsSpent: number;
  running: boolean;
  finished: boolean;
  error: string | null;
}

/** A React key for LaneEntry, generated independently of the backend's
 *  `event.seq`. The backend's seq only counts events within ONE SSE
 *  request — every `/analyze/run`, `/chat` and `/chat/confirm` call starts
 *  a fresh EventStream at seq=1 — but a session's lane entries accumulate
 *  across many such requests (a chat turn appends to lanes a prior run
 *  already populated). Keying React's list on the raw backend seq meant
 *  two entries from different requests could carry the same small integer
 *  and collide as React keys — observed live as a "two children with the
 *  same key" warning once a chat-driven regional search's own EventStream
 *  restarted numbering from 1. This counter is unique for the page's
 *  entire lifetime, independent of how many requests contributed entries. */
let nextEntryKey = 0;
function entryKey(): number {
  nextEntryKey += 1;
  return nextEntryKey;
}

export function emptyLanes(): Record<Threat, LaneState> {
  const lanes = {} as Record<Threat, LaneState>;
  for (const threat of THREATS) {
    lanes[threat] = { threat, stage: null, entries: [], ruling: null };
  }
  return lanes;
}

export function initialState(): AnalysisState {
  return {
    plan: null,
    restored: false,
    cells: [],
    cellBounds: null,
    center: null,
    samples: [],
    lanes: emptyLanes(),
    systemLog: [],
    triage: null,
    creditsSpent: 0,
    running: false,
    finished: false,
    error: null,
  };
}

export function stateFromPlan(plan: AnalysisPlan): AnalysisState {
  return {
    ...initialState(),
    plan,
    center: { lat: plan.lat, lon: plan.lon },
    samples: plan.samples.map((s) => ({
      ...s,
      state: "pending",
      soil_usable: null,
      profile: null,
      citedBy: [],
      vetoedBy: [],
    })),
  };
}

export interface StoredAnalysis {
  location: {
    location_id: number;
    query_text: string;
    label: string;
    lat: number;
    lon: number;
    region_key: string;
  };
  samples: {
    sample_id: number;
    role: string;
    lat: number;
    lon: number;
    soil_usable: boolean | null;
    profile: SampleProfile | null;
  }[];
  rulings: {
    threat: Threat;
    severity: ThreatRuling["severity"];
    cited_evidence: AddressCitedClaim[];
    rejected_counter_argument: string | null;
    invalidation_condition: ThreatRuling["invalidation_condition"];
    reasoning: {
      investigator?: { argument?: string; claims?: AddressCitedClaim[] } | null;
      skeptic?: {
        argument?: string;
        claims?: AddressCitedClaim[];
        veto_reason?: string | null;
        vetoed_sample_ids?: number[];
      } | null;
      adjudicator_explanation?: string;
      unknowns?: string[];
    } | null;
  }[];
}

type Action =
  | { kind: "plan"; plan: AnalysisPlan }
  | { kind: "hydrate"; stored: StoredAnalysis }
  | { kind: "event"; event: StreamEvent }
  | { kind: "running"; running: boolean }
  | { kind: "error"; message: string }
  | { kind: "reset" };

/** A vetoed point stays vetoed even if another lane cites it. The veto is a
 *  statement that no soil answer exists there, and that does not stop being
 *  true because a different threat found something else to say about the
 *  same coordinate — so `vetoed` wins over `cited` in the display. */
function nextState(sample: SampleView): SampleView["state"] {
  if (sample.vetoedBy.length > 0) return "vetoed";
  if (sample.citedBy.length > 0) return "cited";
  if (sample.profile) return "fetched";
  return "pending";
}

function withSample(
  samples: SampleView[],
  sampleId: number,
  update: (s: SampleView) => SampleView,
): SampleView[] {
  return samples.map((s) => (s.sample_id === sampleId ? { ...update(s) } : s));
}

function claimText(claim: AddressCitedClaim): string {
  return `${claim.field} = ${claim.value}`;
}

export function reduce(state: AnalysisState, action: Action): AnalysisState {
  switch (action.kind) {
    case "reset":
      return initialState();
    case "plan":
      return stateFromPlan(action.plan);
    case "hydrate":
      return hydrate(action.stored);
    case "running":
      return { ...state, running: action.running };
    case "error":
      return { ...state, error: action.message, running: false };
    case "event":
      return applyEvent(state, action.event);
  }
}

function pushLane(state: AnalysisState, lane: string, entry: LaneEntry): AnalysisState {
  if (!(THREATS as string[]).includes(lane)) {
    return { ...state, systemLog: [...state.systemLog, entry] };
  }
  const threat = lane as Threat;
  return {
    ...state,
    lanes: {
      ...state.lanes,
      [threat]: { ...state.lanes[threat], entries: [...state.lanes[threat].entries, entry] },
    },
  };
}

function sameBox(a: CellBBox, b: CellBBox): boolean {
  return (
    Math.abs(a.min_lat - b.min_lat) < 1e-9 &&
    Math.abs(a.min_lon - b.min_lon) < 1e-9 &&
    Math.abs(a.max_lat - b.max_lat) < 1e-9 &&
    Math.abs(a.max_lon - b.max_lon) < 1e-9
  );
}

/** Fit the map to the whole search extent once, on the first cell — the
 *  region is fixed for the run, and refitting per cell would make the map
 *  twitch through the entire traversal. */
function boundsOf(cells: CellView[]): CellBBox | null {
  if (cells.length === 0) return null;
  return {
    min_lat: Math.min(...cells.map((c) => c.bbox.min_lat)),
    min_lon: Math.min(...cells.map((c) => c.bbox.min_lon)),
    max_lat: Math.max(...cells.map((c) => c.bbox.max_lat)),
    max_lon: Math.max(...cells.map((c) => c.bbox.max_lon)),
  };
}

function bearingFrom(
  property: { lat: number; lon: number } | undefined,
  point: { lat: number; lon: number; role: string },
): string | null {
  if (!property || point.role === "property") return null;
  const dLat = point.lat - property.lat;
  const dLon = point.lon - property.lon;
  // A tolerance relative to the offset itself: the frontage cross is exactly
  // N/S/E/W, the neighbourhood ring exactly diagonal, and anything a chat
  // turn added is somewhere in between and gets the compound label.
  const scale = Math.max(Math.abs(dLat), Math.abs(dLon)) * 0.35;
  const ns = dLat > scale ? "N" : dLat < -scale ? "S" : "";
  const ew = dLon > scale ? "E" : dLon < -scale ? "W" : "";
  return `${ns}${ew}` || null;
}

/** Rebuild the whole view from what was persisted, so a finished analysis
 *  survives a refresh and can be linked to. Replaying the recorded claims
 *  through the same cited/vetoed logic is what keeps the map's point states
 *  identical to what they were when the run happened — deriving them a
 *  second way here is how the two would drift apart. */
export function hydrate(stored: StoredAnalysis): AnalysisState {
  const state: AnalysisState = {
    ...initialState(),
    center: { lat: stored.location.lat, lon: stored.location.lon },
    samples: stored.samples.map((s) => ({
      sample_id: s.sample_id,
      role: s.role as SampleView["role"],
      // Bearing is not stored — it is a property of the plan, not of the
      // point — so it is re-derived from the offset to the property point.
      // Labelling every restored point "site" would make the frontage cross
      // unreadable, which is most of what the map is for.
      bearing: bearingFrom(stored.samples.find((o) => o.role === "property"), s),
      lat: s.lat,
      lon: s.lon,
      state: s.profile ? "fetched" : "pending",
      soil_usable: s.soil_usable,
      profile: s.profile,
      citedBy: [],
      vetoedBy: [],
    })),
    plan: {
      location_id: stored.location.location_id,
      query_text: stored.location.query_text,
      label: stored.location.label,
      lat: stored.location.lat,
      lon: stored.location.lon,
      region_key: stored.location.region_key,
      station: { name: "", distance_m: 0 },
      samples: [],
      n_points: stored.samples.length,
      n_fields: 0,
      // Zero, and read as "this was paid for in an earlier session" by the
      // counter. Inventing a number from the field count would put a
      // plausible, unverified figure on screen next to real ones.
      quoted_credits: 0,
      credits_spent: 0,
    },
    restored: true,
    finished: true,
  };

  let next = state;
  let seq = 0;
  for (const ruling of stored.rulings) {
    const reasoning = ruling.reasoning ?? {};
    for (const side of ["investigator", "skeptic"] as const) {
      for (const claim of reasoning[side]?.claims ?? []) {
        seq += 1;
        next = applyEvent(next, {
          type: "claim",
          lane: ruling.threat,
          payload: { side, ...claim } as unknown as Record<string, unknown>,
          credits_spent: 0,
          seq,
        });
      }
      const argument = reasoning[side]?.argument;
      if (argument) {
        seq += 1;
        next = applyEvent(next, {
          type: "message",
          lane: ruling.threat,
          payload: { side, text: argument },
          credits_spent: 0,
          seq,
        });
      }
    }
    const vetoed = reasoning.skeptic?.vetoed_sample_ids ?? [];
    if (vetoed.length > 0) {
      seq += 1;
      next = applyEvent(next, {
        type: "veto",
        lane: ruling.threat,
        payload: { reason: reasoning.skeptic?.veto_reason, sample_ids: vetoed },
        credits_spent: 0,
        seq,
      });
    }
    seq += 1;
    next = applyEvent(next, {
      type: "ruling",
      lane: ruling.threat,
      payload: {
        threat: ruling.threat,
        severity: ruling.severity,
        decisive_evidence: ruling.cited_evidence ?? [],
        rejected_counter_argument: ruling.rejected_counter_argument ?? "",
        invalidation_condition: ruling.invalidation_condition ?? null,
        unknowns: reasoning.unknowns ?? [],
        explanation: reasoning.adjudicator_explanation ?? "",
      } as unknown as Record<string, unknown>,
      credits_spent: 0,
      seq,
    });
  }
  return { ...next, finished: true, running: false };
}

export function applyEvent(state: AnalysisState, event: StreamEvent): AnalysisState {
  const credits = { ...state, creditsSpent: event.credits_spent };
  const p = event.payload as Record<string, never>;

  switch (event.type) {
    case "location": {
      const lat = Number(p.lat);
      const lon = Number(p.lon);
      return { ...credits, center: { lat, lon } };
    }

    case "sample_planned": {
      const sampleId = Number(p.sample_id);
      if (credits.samples.some((s) => s.sample_id === sampleId)) return credits;
      return {
        ...credits,
        samples: [
          ...credits.samples,
          {
            sample_id: sampleId,
            role: p.role as never,
            bearing: (p.bearing as string) ?? null,
            lat: Number(p.lat),
            lon: Number(p.lon),
            state: "pending",
            soil_usable: null,
            profile: null,
            citedBy: [],
            vetoedBy: [],
          },
        ],
      };
    }

    case "point_profiled": {
      const sampleId = Number(p.sample_id);
      return {
        ...credits,
        samples: withSample(credits.samples, sampleId, (s) => {
          const updated = {
            ...s,
            profile: p.profile as unknown as SampleProfile,
            soil_usable: Boolean(p.soil_usable),
          };
          return { ...updated, state: nextState(updated) };
        }),
      };
    }

    case "triage":
      // Defense in depth: the backend now scopes every non-primary caller
      // (region search's per-survivor adjudication) to a prefixed lane like
      // "region-1-system" rather than bare "system", specifically so this
      // never fires for anything but the address actually being viewed. A
      // live run demonstrated the failure mode before that existed — a
      // chat-driven regional search overwrote the triage line of the
      // address on screen — so this stays lane-gated even though the
      // backend should never send anything else here.
      if (event.lane !== "system") return credits;
      return {
        ...credits,
        triage: { decision: String(p.decision), reason: String(p.reason ?? "") },
      };

    case "stage": {
      const threat = (THREATS as string[]).includes(event.lane) ? (event.lane as Threat) : null;
      // Lane stages carry no label — the stage name is the whole message and
      // ThreatLane maps it to readable text. System stages (the moisture sync,
      // the fetch) do carry one, because "moisture started" means nothing.
      const entry: LaneEntry = {
        seq: entryKey(),
        kind: "stage",
        text: (p.label as string) ?? String(p.stage),
      };
      const withEntry =
        p.status === "started" ? pushLane(credits, event.lane, entry) : credits;
      if (!threat) return withEntry;
      return {
        ...withEntry,
        lanes: {
          ...withEntry.lanes,
          [threat]: {
            ...withEntry.lanes[threat],
            stage: p.status === "finished" ? withEntry.lanes[threat].stage : (p.stage as string),
          },
        },
      };
    }

    case "tool_call": {
      if (p.status !== "called") return credits;
      return pushLane(credits, event.lane, {
        seq: entryKey(),
        kind: "tool_call",
        text: (p.label as string) ?? (p.tool as string),
        sample_id: p.sample_id != null ? Number(p.sample_id) : null,
      });
    }

    case "claim": {
      const claim = p as unknown as AddressCitedClaim & { side: "investigator" | "skeptic" };
      const entry: LaneEntry = {
        seq: entryKey(),
        kind: "claim",
        side: claim.side,
        text: claimText(claim),
        detail: claim.interpretation,
        sample_id: claim.sample_id,
        field: claim.field,
        source: claim.source ?? null,
      };
      let next = pushLane(credits, event.lane, entry);
      // The binding: a claim naming a point marks that point cited, which is
      // what makes it pulse on the map.
      if (claim.sample_id != null && (THREATS as string[]).includes(event.lane)) {
        const threat = event.lane as Threat;
        next = {
          ...next,
          samples: withSample(next.samples, claim.sample_id, (s) => {
            const updated = {
              ...s,
              citedBy: s.citedBy.includes(threat) ? s.citedBy : [...s.citedBy, threat],
            };
            return { ...updated, state: nextState(updated) };
          }),
        };
      }
      return next;
    }

    case "veto": {
      const sampleIds = ((p.sample_ids as unknown as number[]) ?? []).map(Number);
      const threat = event.lane as Threat;
      let next = pushLane(credits, event.lane, {
        seq: entryKey(),
        kind: "veto",
        text: "Honesty Gate: soil claim vetoed",
        detail: (p.reason as string) ?? undefined,
        sample_ids: sampleIds,
      });
      for (const id of sampleIds) {
        next = {
          ...next,
          samples: withSample(next.samples, id, (s) => {
            const updated = {
              ...s,
              vetoedBy: s.vetoedBy.includes(threat) ? s.vetoedBy : [...s.vetoedBy, threat],
            };
            return { ...updated, state: nextState(updated) };
          }),
        };
      }
      return next;
    }

    case "message":
      return pushLane(credits, event.lane, {
        seq: entryKey(),
        kind: "message",
        side: p.side as "investigator" | "skeptic",
        text: String(p.text ?? ""),
      });

    case "ruling": {
      const ruling = p as unknown as ThreatRuling;
      const threat = event.lane as Threat;
      if (!(THREATS as string[]).includes(threat)) return credits;
      return {
        ...credits,
        lanes: {
          ...credits.lanes,
          [threat]: {
            ...credits.lanes[threat],
            stage: null,
            ruling,
            entries: [
              ...credits.lanes[threat].entries,
              { seq: entryKey(), kind: "ruling", text: ruling.severity, detail: ruling.explanation },
            ],
          },
        },
      };
    }

    case "cell_scored": {
      const cell: CellView = {
        cell_id: Number(p.cell_id),
        bbox: p.bbox as unknown as CellBBox,
        score: p.score == null ? null : Number(p.score),
        subdivided: false,
      };
      const cells = [...credits.cells.filter((c) => c.cell_id !== cell.cell_id), cell];
      return { ...credits, cells, cellBounds: credits.cellBounds ?? boundsOf(cells) };
    }

    case "cell_subdivided": {
      // The parent stays on the map, faded: it is what justified refining
      // here, and dropping it would make the traversal look like it went
      // straight to the fine grid.
      const bbox = p.bbox as unknown as CellBBox;
      return {
        ...credits,
        cells: credits.cells.map((c) =>
          sameBox(c.bbox, bbox) ? { ...c, subdivided: true } : c,
        ),
      };
    }

    case "done":
      return { ...credits, running: false, finished: true };

    case "error":
      return { ...credits, running: false, error: String(p.message ?? "stream failed") };

    default:
      return credits;
  }
}

export type { Action };
