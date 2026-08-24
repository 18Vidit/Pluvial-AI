"""FastAPI surface for Bellwether (design spec §7).

GET /queue        — today's Triage-promoted cases with their verdicts, for
                     the dispatcher board's three columns.
GET /segments/{id} — a segment's physical profile + complaint history, for
                     both the dispatcher card expansion and the public view.
POST /reprofile/{id} — force a fresh Mireye fetch for a segment (admin use;
                     still goes through the wrapper's quote-then-fetch path).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from bellwether.memory import dal

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "bellwether.db"

app = FastAPI(title="Bellwether API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tightened before public deployment
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _ensure_schema() -> None:
    dal.init_db(DB_PATH)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/queue")
def queue(limit: int = 100):
    with dal.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT v.verdict_id, v.segment_id, v.disposition, v.priority, v.decided_at,
                   v.reasoning_json, v.cited_evidence_json, v.rejected_counter_argument,
                   v.invalidation_condition_json, v.reawakened_from,
                   s.name AS segment_name, s.centroid_lat, s.centroid_lon
            FROM verdicts v
            JOIN segments s ON s.segment_id = v.segment_id
            WHERE v.verdict_id IN (
                SELECT MAX(verdict_id) FROM verdicts GROUP BY segment_id
            )
            ORDER BY
                CASE v.disposition WHEN 'dispatch' THEN 0 WHEN 'inspect' THEN 1 WHEN 'monitor' THEN 2 ELSE 3 END,
                v.decided_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    cards = []
    for r in rows:
        d = dict(r)
        d["reasoning"] = json.loads(d.pop("reasoning_json"))
        d["cited_evidence"] = json.loads(d.pop("cited_evidence_json"))
        cond = d.pop("invalidation_condition_json")
        d["invalidation_condition"] = json.loads(cond) if cond else None
        d["reawakened"] = d["reawakened_from"] is not None
        cards.append(d)
    return {"cards": cards}


@app.get("/segments/{segment_id}")
def segment_detail(segment_id: int):
    with dal.connect(DB_PATH) as con:
        seg = dal.get_segment(con, segment_id)
        if seg is None:
            raise HTTPException(status_code=404, detail="segment not found")
        complaints = con.execute(
            "SELECT * FROM complaints WHERE segment_id = ? ORDER BY created_at DESC", (segment_id,)
        ).fetchall()
        verdicts = con.execute(
            "SELECT * FROM verdicts WHERE segment_id = ? ORDER BY decided_at DESC", (segment_id,)
        ).fetchall()
    return {
        "segment": seg,
        "complaints": [dict(c) for c in complaints],
        "verdicts": [dict(v) for v in verdicts],
    }
