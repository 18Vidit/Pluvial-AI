"""The one event envelope every streaming surface speaks.

The primary address flow, a chat-driven fetch and a regional search all emit
the same shape, so the map reacts identically no matter which surface asked
for the work. That is the reason this is a module and not three ad-hoc
dictionaries.

    {"type": ..., "lane": ..., "payload": {...}, "credits_spent": N, "seq": N}

`lane` is which of the three threat arguments an event belongs to, or
"system" for anything shared (the plan, the quote, the fetch, the final
done). Three concurrent cascades are merged into one channel, so without a
lane tag the client could not tell whose claim it just received.

`credits_spent` is the running total for the whole run and is attached to
every event rather than only to spending ones. The counter on screen is then
a property of the last event received, which cannot drift out of step with
the map the way a separately-tracked number would.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Literal

EventType = Literal[
    # shared / system
    "location",          # geocode resolved
    "sample_planned",    # one planned point, before anything is spent
    "quote",             # a proposed purchase awaiting confirmation
    "point_profiled",    # one point came back from Mireye
    "triage",            # the shared triage decision
    "stage",             # a lane moved to investigator/skeptic/adjudicator
    # per-lane cascade
    "tool_call",
    "claim",
    "veto",
    "ruling",
    # regional search
    "cell_scored",
    "cell_subdivided",
    # chat
    "message",
    # terminal
    "done",
    "error",
]

SYSTEM_LANE = "system"


@dataclass
class Event:
    type: EventType
    payload: dict[str, Any]
    lane: str = SYSTEM_LANE
    credits_spent: int = 0
    seq: int = 0

    def to_sse(self) -> str:
        """One SSE frame. The event name is duplicated into the `event:` line
        as well as the JSON body so a client can either addEventListener by
        type or read everything off one onmessage handler."""
        body = json.dumps(
            {
                "type": self.type,
                "lane": self.lane,
                "payload": self.payload,
                "credits_spent": self.credits_spent,
                "seq": self.seq,
            },
            default=str,
        )
        return f"event: {self.type}\ndata: {body}\n\n"


@dataclass
class EventStream:
    """Stamps sequence numbers and the running credit total onto events.

    Credits are held here rather than in each producer because three lanes
    plus the fetch loop all contribute to one number, and a total assembled
    from four independent counters is a total that will eventually be wrong.
    """

    credits_spent: int = 0
    _seq: Any = field(default_factory=lambda: count(1))

    def spend(self, credits: int) -> int:
        self.credits_spent += credits
        return self.credits_spent

    def make(self, type: EventType, payload: dict[str, Any], lane: str = SYSTEM_LANE) -> Event:
        return Event(
            type=type, payload=payload, lane=lane,
            credits_spent=self.credits_spent, seq=next(self._seq),
        )
