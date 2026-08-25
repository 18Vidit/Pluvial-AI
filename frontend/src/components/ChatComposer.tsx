"use client";

import { useEffect, useRef } from "react";

export interface ChatTurn {
  id: number;
  side: "user" | "assistant" | "system" | "tool";
  text: string;
}

export interface PendingQuote {
  pending_id: string;
  kind: "sample_point" | "analyze_location";
  quoted_credits: number;
  label: string;
  location_id?: number;
  sample_id?: number;
}

/* The chat can propose a purchase; it can never make one. A `quote` event
   renders this, and the credits leave the account when — and only when —
   someone presses the button. Identical to the address box's gate, because
   it is the same gate: an agent may ask, a person authorises. */
function QuoteGate({
  quote,
  onConfirm,
  onDismiss,
  busy,
}: {
  quote: PendingQuote;
  onConfirm: () => void;
  onDismiss: () => void;
  busy: boolean;
}) {
  return (
    <div className="rounded border border-moisture/45 bg-moisture/8 px-3 py-2.5">
      <p className="eyebrow">quote · nothing spent yet</p>
      <p className="mt-1 text-[13px] leading-snug text-bone">{quote.label}</p>
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          onClick={onConfirm}
          disabled={busy}
          className="rounded bg-moisture px-3 py-1.5 text-[12.5px] font-medium text-ground-900 transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          {busy ? "Fetching…" : `Confirm ${quote.quoted_credits} credits`}
        </button>
        <button
          type="button"
          onClick={onDismiss}
          disabled={busy}
          className="rounded px-2.5 py-1.5 text-[12.5px] text-bone-faint transition-colors hover:text-bone disabled:opacity-40"
        >
          Not now
        </button>
      </div>
    </div>
  );
}

export function ChatComposer({
  turns,
  quote,
  busy,
  disabled,
  onSend,
  onConfirm,
  onDismissQuote,
}: {
  turns: ChatTurn[];
  quote: PendingQuote | null;
  busy: boolean;
  disabled: boolean;
  onSend: (message: string) => void;
  onConfirm: () => void;
  onDismissQuote: () => void;
}) {
  const input = useRef<HTMLInputElement | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns.length, quote]);

  return (
    <section className="flex min-h-0 flex-col rounded-lg border border-ground-700 bg-ground-850">
      <header className="border-b border-ground-700 px-3.5 py-2.5">
        <h3 className="display text-[14px] text-bone">Ask about this ground</h3>
        <p className="mt-0.5 text-[11.5px] leading-snug text-bone-faint">
          {disabled
            ? "Opens once the three rulings land."
            : "Answers from evidence already fetched, or proposes fetching more."}
        </p>
      </header>

      {turns.length > 0 && (
        <div ref={scroller} className="max-h-56 min-h-0 flex-1 space-y-2 overflow-y-auto px-3.5 py-2.5">
          {turns.map((turn) => (
            <div
              key={turn.id}
              className={
                turn.side === "user"
                  ? "border-l-2 border-bone-faint pl-2.5 text-[13px] text-bone"
                  : turn.side === "tool"
                    ? "data pl-2.5 text-[12px] text-bone-faint"
                    : turn.side === "system"
                      ? "pl-2.5 text-[12.5px] text-moisture"
                      : "border-l-2 border-moisture/50 pl-2.5 text-[13px] leading-snug text-bone-dim"
              }
            >
              {turn.side === "tool" && <span aria-hidden>▸ </span>}
              {turn.text}
            </div>
          ))}
          {quote && (
            <QuoteGate quote={quote} onConfirm={onConfirm} onDismiss={onDismissQuote} busy={busy} />
          )}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          const value = input.current?.value.trim();
          if (!value) return;
          onSend(value);
          if (input.current) input.current.value = "";
        }}
        className="flex gap-2 border-t border-ground-700 p-2.5"
      >
        <label htmlFor="chat" className="sr-only">
          Ask about this ground
        </label>
        <input
          id="chat"
          ref={input}
          disabled={disabled || busy}
          placeholder={disabled ? "Waiting for the rulings…" : "Why did you veto that point?"}
          className="flex-1 rounded border border-ground-700 bg-ground-900 px-3 py-2 text-[13px] text-bone placeholder:text-bone-faint focus:border-moisture focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={disabled || busy}
          className="rounded border border-ground-700 px-3 py-2 text-[13px] text-bone-dim transition-colors hover:bg-ground-800 hover:text-bone disabled:opacity-40"
        >
          {busy ? "…" : "Ask"}
        </button>
      </form>
    </section>
  );
}
