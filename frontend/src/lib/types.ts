export type Disposition = "dispatch" | "inspect" | "monitor" | "close";
export type Priority = "critical" | "high" | "medium" | "low";

export interface CitedClaim {
  field: string;
  value: string;
  source?: string | null;
  interpretation: string;
}

export interface InvalidationCondition {
  reopen_if_trigger_state_in?: string[];
  reopen_if_new_complaints_within_days?: number | null;
  reopen_if_new_complaints_within_m?: number | null;
  reopen_if_new_complaint_count_at_least?: number | null;
  plain_english: string;
}

export interface QueueCard {
  verdict_id: number;
  segment_id: number;
  disposition: Disposition;
  priority: Priority | null;
  decided_at: string;
  segment_name: string | null;
  centroid_lat: number;
  centroid_lon: number;
  reasoning: {
    investigator?: { argument: string; claims: CitedClaim[] } | null;
    skeptic?: { argument: string; claims: CitedClaim[]; soil_claim_vetoed: boolean; veto_reason?: string | null } | null;
    adjudicator_explanation?: string;
  };
  cited_evidence: CitedClaim[];
  rejected_counter_argument: string;
  invalidation_condition: InvalidationCondition | null;
  reawakened: boolean;
}

export interface QueueResponse {
  cards: QueueCard[];
}

export interface SegmentProfileField {
  value: string | number | boolean | null;
  source?: string | null;
}

export interface LookupSegment {
  segment_id: number;
  name: string | null;
  highway_class: string | null;
  centroid_lat: number;
  centroid_lon: number;
  soil_usable: number | null;
  profiled_at: string | null;
  profile?: Record<string, SegmentProfileField>;
}

export interface LookupVerdict {
  verdict_id: number;
  disposition: Disposition;
  priority: Priority | null;
  decided_at: string;
  reasoning_json: string;
  cited_evidence_json: string;
}

export interface EvalRun {
  n: number;
  precision: number | null;
  recall: number | null;
  true_positive: number;
  false_positive: number;
}

export interface Stats {
  segments_profiled: number;
  soil_usable: number;
  soil_usable_rate: number | null;
  complaints: number;
  verdicts: number;
  reawakened: number;
  dispositions: Record<string, number>;
  outcomes: Record<string, number>;
  eval: {
    full: EvalRun | null;
    no_moisture: EvalRun | null;
    no_memory: EvalRun | null;
    negative_control: { n: number; n_soil_usable: number; n_false_soil_claims: number } | null;
  };
}

export interface VerdictListItem {
  verdict_id: number;
  segment_id: number;
  disposition: Disposition;
  priority: Priority | null;
  decided_at: string;
  reawakened_from: number | null;
  segment_name: string | null;
}

export interface AgentSide {
  claims: CitedClaim[];
  argument: string;
  soil_claim_vetoed?: boolean;
  veto_reason?: string | null;
  signals_referenced?: string[];
}

export interface VerdictDetail {
  verdict: {
    verdict_id: number;
    segment_id: number;
    disposition: Disposition;
    priority: Priority | null;
    decided_at: string;
    agent_version: string;
    reawakened_from: number | null;
    rejected_counter_argument: string | null;
    reasoning: {
      investigator: AgentSide | null;
      skeptic: AgentSide | null;
      adjudicator_explanation?: string;
    };
    cited_evidence: CitedClaim[];
    invalidation_condition: InvalidationCondition | null;
  };
  case_numbers: string[];
  complaints: {
    case_number: string;
    incident_case_type: string;
    title: string | null;
    status: string | null;
    created_at: string;
    closed_at: string | null;
  }[];
  segment: LookupSegment | null;
  moisture: { trigger_state: string | null; antecedent_30d_mm: number | null; date: string } | null;
  prior_verdict: { verdict_id: number; disposition: Disposition; priority: Priority | null; decided_at: string } | null;
}

export interface LookupResponse {
  matched_address: string;
  geocoded: { lat: number; lon: number };
  segment: LookupSegment;
  distance_m: number;
  verdicts: LookupVerdict[];
  assessor_link: string;
}
