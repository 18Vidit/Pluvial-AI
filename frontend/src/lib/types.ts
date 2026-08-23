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
