import { AnalysisPlan } from "./address-types";
import { API_BASE } from "./stream";

export class PlanError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

/** Geocode, lay out the nine sample points, and quote them. Spends nothing —
 *  this is the half of the flow that is safe to run on every keystroke of
 *  intent, and the number it returns is Mireye's own quote, not an estimate
 *  computed here that could later diverge from the bill. */
export async function planAnalysis(address: string): Promise<AnalysisPlan> {
  const res = await fetch(`${API_BASE}/analyze/plan`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ address }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new PlanError(body?.detail ?? `plan failed: ${res.status}`, res.status);
  }
  return res.json();
}

export function runPath(locationId: number): string {
  return `/analyze/run?location_id=${locationId}`;
}

export async function fetchAnalysis(locationId: number) {
  const res = await fetch(`${API_BASE}/analyze/${locationId}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /analyze/${locationId} failed: ${res.status}`);
  return res.json();
}

export function chatPath(): string {
  return "/chat";
}

export function chatConfirmPath(sessionId: string, pendingId: string): string {
  return `/chat/confirm?session_id=${encodeURIComponent(sessionId)}&pending_id=${encodeURIComponent(pendingId)}`;
}
