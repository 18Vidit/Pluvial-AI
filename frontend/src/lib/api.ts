import { QueueResponse } from "./types";

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
