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

type Action =
  | { kind: "plan"; plan: AnalysisPlan }
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
        seq: event.seq,
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
        seq: event.seq,
        kind: "tool_call",
        text: (p.label as string) ?? (p.tool as string),
        sample_id: p.sample_id != null ? Number(p.sample_id) : null,
      });
    }

    case "claim": {
      const claim = p as unknown as AddressCitedClaim & { side: "investigator" | "skeptic" };
      const entry: LaneEntry = {
        seq: event.seq,
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
        seq: event.seq,
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
        seq: event.seq,
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
              { seq: event.seq, kind: "ruling", text: ruling.severity, detail: ruling.explanation },
            ],
          },
        },
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
