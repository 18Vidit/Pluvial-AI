/** SSE client for address mode.
 *
 *  Written against `fetch` + a ReadableStream rather than `EventSource`,
 *  because the chat endpoint needs to POST a body and `EventSource` cannot
 *  send one. One reader for every streaming surface beats two code paths
 *  that have to be kept in agreement about the same envelope.
 */
import { StreamEvent } from "./address-types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8811";

export interface StreamOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
}

/** Yields each event as it arrives. The caller drives it with `for await`,
 *  so backpressure and cancellation are the language's problem rather than
 *  a callback registry's. */
export async function* streamEvents(
  path: string,
  { method = "GET", body, signal }: StreamOptions = {},
): AsyncGenerator<StreamEvent> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => "");
    throw new Error(`stream ${path} failed: ${res.status} ${detail.slice(0, 200)}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line. Anything after the last
      // separator is a partial frame and has to stay in the buffer — a
      // chunk boundary lands mid-JSON often enough to matter.
      let split: number;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!dataLine) continue;
        try {
          yield JSON.parse(dataLine.slice(5).trim()) as StreamEvent;
        } catch {
          // A frame we cannot parse is a bug worth seeing, but dropping the
          // rest of a live run over it would be worse.
          console.warn("unparseable SSE frame", frame.slice(0, 200));
        }
      }
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
}

export { API_BASE };
