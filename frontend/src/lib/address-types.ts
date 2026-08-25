/** Address mode's wire contract. Mirrors backend/pluvial/api/events.py and
 *  the Pydantic models in backend/pluvial/agents/models.py — one envelope,
 *  whether the work was driven by the address box, the chat, or a region
 *  search. */

export type Threat = "foundation" | "service_lines" | "subsidence";
export type Severity = "high" | "elevated" | "low" | "unresolved";
export type SampleRole = "property" | "frontage" | "neighbourhood";

export const THREATS: Threat[] = ["foundation", "service_lines", "subsidence"];

export interface SampleProfileField {
  value: string | number | boolean | null;
  source?: string | null;
}

export type SampleProfile = Record<string, SampleProfileField>;

export interface AddressCitedClaim {
  field: string;
  value: string;
  source?: string | null;
  interpretation: string;
  /** Null only for evidence with genuinely no point — moisture history is
   *  regional by design. Everything else anchors to a sampled point. */
  sample_id: number | null;
}

export interface AddressInvalidationCondition {
  reopen_if_trigger_state_in?: string[];
  plain_english: string;
}

export interface ThreatRuling {
  threat: Threat;
  severity: Severity;
  decisive_evidence: AddressCitedClaim[];
  rejected_counter_argument: string;
  invalidation_condition: AddressInvalidationCondition | null;
  unknowns: string[];
  explanation: string;
}

export interface PlannedSample {
  sample_id: number;
  role: SampleRole;
  bearing: string | null;
  lat: number;
  lon: number;
}

export interface AnalysisPlan {
  location_id: number;
  query_text: string;
  label: string;
  lat: number;
  lon: number;
  region_key: string;
  station: { name: string; distance_m: number };
  samples: PlannedSample[];
  n_points: number;
  n_fields: number;
  quoted_credits: number;
  credits_spent: number;
}

export type StreamEventType =
  | "location"
  | "sample_planned"
  | "quote"
  | "point_profiled"
  | "triage"
  | "stage"
  | "tool_call"
  | "claim"
  | "veto"
  | "ruling"
  | "cell_scored"
  | "cell_subdivided"
  | "message"
  | "done"
  | "error";

/** `lane` is which threat argument an event belongs to, or "system" for
 *  anything shared. Three concurrent cascades share one channel, so without
 *  it the client could not tell whose claim just arrived. */
export interface StreamEvent<P = Record<string, unknown>> {
  type: StreamEventType;
  lane: Threat | "system" | string;
  payload: P;
  credits_spent: number;
  seq: number;
}

/** Four visual states, in the order a point moves through them. */
export type SampleState = "pending" | "fetched" | "cited" | "vetoed";

export interface SampleView extends PlannedSample {
  state: SampleState;
  soil_usable: boolean | null;
  profile: SampleProfile | null;
  /** Which lanes have cited this point, so a pulse can be attributed. */
  citedBy: Threat[];
  vetoedBy: Threat[];
}

export interface LaneEntry {
  seq: number;
  kind: "stage" | "tool_call" | "claim" | "veto" | "message" | "ruling";
  side?: "investigator" | "skeptic";
  text: string;
  detail?: string;
  sample_id?: number | null;
  sample_ids?: number[];
  field?: string;
  source?: string | null;
}

export interface LaneState {
  threat: Threat;
  stage: string | null;
  entries: LaneEntry[];
  ruling: ThreatRuling | null;
}
