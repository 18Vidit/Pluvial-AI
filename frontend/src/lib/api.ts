import {
  LiveCascadeResult,
  LookupResponse,
  QueueResponse,
  Stats,
  VerdictDetail,
  VerdictListItem,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8811";

export async function fetchQueue(): Promise<QueueResponse> {
  const res = await fetch(`${API_BASE}/queue`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /queue failed: ${res.status}`);
  return res.json();
}

export async function fetchSegment(segmentId: number) {
  const res = await fetch(`${API_BASE}/segments/${segmentId}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /segments/${segmentId} failed: ${res.status}`);
  return res.json();
}

export async function fetchStats(): Promise<Stats> {
  const res = await fetch(`${API_BASE}/stats`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /stats failed: ${res.status}`);
  return res.json();
}

export async function fetchVerdicts(limit = 60): Promise<{ verdicts: VerdictListItem[] }> {
  const res = await fetch(`${API_BASE}/verdicts?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /verdicts failed: ${res.status}`);
  return res.json();
}

export async function fetchVerdict(id: number): Promise<VerdictDetail> {
  const res = await fetch(`${API_BASE}/verdicts/${id}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /verdicts/${id} failed: ${res.status}`);
  return res.json();
}

/** Runs the four agents live. Costs real model calls, so the UI only calls
 *  this from an explicit, labelled action — never on page load. */
export async function runCascadeLive(caseNumber: string): Promise<LiveCascadeResult> {
  const res = await fetch(`${API_BASE}/cascade/run?case_number=${encodeURIComponent(caseNumber)}`, {
    method: "POST",
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `Live run failed: ${res.status}`);
  }
  return res.json();
}

export class LookupError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export async function fetchLookup(address: string): Promise<LookupResponse> {
  const res = await fetch(`${API_BASE}/lookup?address=${encodeURIComponent(address)}`, { cache: "no-store" });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new LookupError(body?.detail ?? `GET /lookup failed: ${res.status}`, res.status);
  }
  return res.json();
}
