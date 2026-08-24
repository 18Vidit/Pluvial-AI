"""FastAPI surface for Pluvial-AI (design spec §7).

GET /queue        — today's Triage-promoted cases with their verdicts, for
                     the dispatcher board's three columns.
GET /segments/{id} — a segment's physical profile + complaint history, for
                     both the dispatcher card expansion and the public view.
POST /reprofile/{id} — force a fresh Mireye fetch for a segment (admin use;
                     still goes through the wrapper's quote-then-fetch path).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from pluvial.memory import dal
from pluvial.mireye.client import MireyeAccount, MireyeClient, QuoteExceedsCeilingError
from pluvial.mireye.wrapper import CreditCeilingExceeded, MireyeToolWrapper, RunBudget

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "pluvial.db"
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"

app = FastAPI(title="Pluvial-AI API")
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


@app.post("/reprofile/{segment_id}")
def reprofile(segment_id: int):
    key = os.environ.get("MIREYE_API_KEY_1")
    if not key:
        raise HTTPException(status_code=400, detail="MIREYE_API_KEY_1 not set")
    ceiling = int(os.environ.get("PLUVIAL_CREDIT_CEILING_PER_RUN", 500))

    with dal.connect(DB_PATH) as con:
        seg = dal.get_segment(con, segment_id)
        if seg is None:
            raise HTTPException(status_code=404, detail="segment not found")

        account = MireyeAccount(label="admin-reprofile", api_key=key)
        with MireyeClient(account) as client:
            wrapper = MireyeToolWrapper(con, client, RunBudget(ceiling=ceiling))
            try:
                result = wrapper.mireye_profile(
                    segment_id, seg["centroid_lat"], seg["centroid_lon"], force_refresh=True
                )
            except (CreditCeilingExceeded, QuoteExceedsCeilingError) as e:
                raise HTTPException(status_code=402, detail=str(e))
    return result


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@app.get("/lookup")
def lookup(address: str):
    """Public surface: geocode a free-text address, find the nearest known
    street segment, and return the same physical-profile + verdict-history
    view as /segments/{id}. Owner lookup is explicitly out of scope (design
    spec §10 budget) — this ends at the segment, not the parcel."""
    with httpx.Client(timeout=10, headers={"User-Agent": "pluvial-houston-311/1.0"}) as client:
        r = client.get(
            NOMINATIM_ENDPOINT,
            params={"q": f"{address}, Houston, TX", "format": "json", "limit": 1},
        )
        r.raise_for_status()
        hits = r.json()
    if not hits:
        raise HTTPException(status_code=404, detail="address not found")
    lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])

    with dal.connect(DB_PATH) as con:
        # bounding-box pre-filter (~5.5km at Houston's latitude), then exact
        # haversine over the candidates — avoids scanning all 168k segments.
        candidates = con.execute(
            """
            SELECT segment_id, name, centroid_lat, centroid_lon
            FROM segments
            WHERE centroid_lat BETWEEN ? AND ? AND centroid_lon BETWEEN ? AND ?
            """,
            (lat - 0.05, lat + 0.05, lon - 0.05, lon + 0.05),
        ).fetchall()
        if not candidates:
            raise HTTPException(status_code=404, detail="no known street segment near this address")
        nearest = min(candidates, key=lambda c: _haversine_m(lat, lon, c["centroid_lat"], c["centroid_lon"]))
        seg = dal.get_segment(con, nearest["segment_id"])
        verdicts = con.execute(
            "SELECT * FROM verdicts WHERE segment_id = ? ORDER BY decided_at DESC LIMIT 5",
            (nearest["segment_id"],),
        ).fetchall()

    return {
        "matched_address": hits[0].get("display_name"),
        "geocoded": {"lat": lat, "lon": lon},
        "segment": seg,
        "distance_m": round(_haversine_m(lat, lon, seg["centroid_lat"], seg["centroid_lon"]), 1),
        "verdicts": [dict(v) for v in verdicts],
        # HCAD's public search doesn't have a documented deep-link query
        # format; point at the search homepage rather than guess one.
        "assessor_link": "https://hcad.org/property-search",
    }


DATA_DIR = DB_PATH.parent


@app.get("/stats")
def stats():
    """Counters for the landing page. Every number here is read from the
    live database or a written eval report — nothing is hardcoded, so the
    page can never drift from what the system actually did."""
    with dal.connect(DB_PATH) as con:
        segments_profiled = con.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE profile_json IS NOT NULL AND segment_id > 0"
        ).fetchone()["n"]
        soil_usable = con.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE soil_usable = 1 AND segment_id > 0"
        ).fetchone()["n"]
        complaints = con.execute("SELECT COUNT(*) AS n FROM complaints").fetchone()["n"]
        verdicts_total = con.execute("SELECT COUNT(*) AS n FROM verdicts").fetchone()["n"]
        reawakened = con.execute(
            "SELECT COUNT(*) AS n FROM verdicts WHERE reawakened_from IS NOT NULL"
        ).fetchone()["n"]
        dispositions = {
            r["disposition"]: r["n"]
            for r in con.execute("SELECT disposition, COUNT(*) AS n FROM verdicts GROUP BY disposition")
        }
        outcomes = {
            r["label"]: r["n"]
            for r in con.execute("SELECT label, COUNT(*) AS n FROM outcomes GROUP BY label")
        }

    return {
        "segments_profiled": segments_profiled,
        "soil_usable": soil_usable,
        "soil_usable_rate": round(soil_usable / segments_profiled, 4) if segments_profiled else None,
        "complaints": complaints,
        "verdicts": verdicts_total,
        "reawakened": reawakened,
        "dispositions": dispositions,
        "outcomes": outcomes,
        "eval": _read_eval_summary(),
    }


def _read_eval_summary() -> dict:
    """Headline backtest + ablations, read from the report files the eval
    harness wrote. Missing files degrade to null rather than inventing
    numbers."""
    out: dict = {}
    for label, filename in (
        ("full", "backtest_full_2026-07-15.json"),
        ("no_moisture", "backtest_no_moisture_2026-07-15.json"),
        ("no_memory", "backtest_no_memory_2026-07-15.json"),
    ):
        path = DATA_DIR / filename
        if not path.exists():
            out[label] = None
            continue
        data = json.loads(path.read_text())
        out[label] = {k: data.get(k) for k in ("n", "precision", "recall", "true_positive", "false_positive")}

    nyc_path = DATA_DIR / "negative_control_nyc.json"
    if nyc_path.exists():
        nyc = json.loads(nyc_path.read_text())
        out["negative_control"] = {
            "n": nyc.get("n"),
            "n_soil_usable": nyc.get("n_soil_usable"),
            "n_false_soil_claims": nyc.get("n_false_soil_claims"),
        }
    else:
        out["negative_control"] = None
    return out


@app.get("/verdicts")
def list_verdicts(limit: int = 60):
    """Lightweight list for browsing into case files."""
    with dal.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT v.verdict_id, v.segment_id, v.disposition, v.priority, v.decided_at,
                   v.reawakened_from, s.name AS segment_name
            FROM verdicts v JOIN segments s ON s.segment_id = v.segment_id
            ORDER BY v.decided_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"verdicts": [dict(r) for r in rows]}


@app.get("/verdicts/{verdict_id}")
def verdict_detail(verdict_id: int):
    """Everything the case-file view needs to replay one decision: the full
    Investigator / Skeptic / Adjudicator record, the complaints it covers,
    the segment's physical profile, and the prior verdict if this one was
    reawakened."""
    with dal.connect(DB_PATH) as con:
        row = con.execute("SELECT * FROM verdicts WHERE verdict_id = ?", (verdict_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="verdict not found")
        v = dict(row)
        case_numbers = json.loads(v.pop("case_numbers"))
        v["reasoning"] = json.loads(v.pop("reasoning_json"))
        v["cited_evidence"] = json.loads(v.pop("cited_evidence_json"))
        cond = v.pop("invalidation_condition_json")
        v["invalidation_condition"] = json.loads(cond) if cond else None

        complaints = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM complaints WHERE case_number IN (%s)" % ",".join("?" for _ in case_numbers),
                case_numbers,
            ).fetchall()
        ] if case_numbers else []

        segment = dal.get_segment(con, v["segment_id"])
        moisture = dal.current_trigger_state(con, as_of=v["decided_at"])

        prior = None
        if v.get("reawakened_from"):
            p = con.execute(
                "SELECT verdict_id, disposition, priority, decided_at FROM verdicts WHERE verdict_id = ?",
                (v["reawakened_from"],),
            ).fetchone()
            prior = dict(p) if p else None

    return {
        "verdict": v,
        "case_numbers": case_numbers,
        "complaints": complaints,
        "segment": segment,
        "moisture": moisture,
        "prior_verdict": prior,
    }


@app.post("/cascade/run")
def cascade_run(case_number: str):
    """Run the four agents live against one complaint and return the result
    WITHOUT writing a verdict — this is the demo's 'prove it's real' path,
    so it must not pollute the recorded queue. Costs real model calls, which
    is why the UI makes it an explicit opt-in rather than the default view.
    Mireye stays cache-only (ceiling 0): a live run reasons over the profile
    already on file, it does not buy new ground truth."""
    import asyncio

    from pluvial.agents.cascade import run_cascade
    from pluvial.agents.context import CascadeContext

    key = os.environ.get("MIREYE_API_KEY_1")
    if not key:
        raise HTTPException(status_code=400, detail="MIREYE_API_KEY_1 not set")

    with dal.connect(DB_PATH) as con:
        complaint = con.execute("SELECT * FROM complaints WHERE case_number = ?", (case_number,)).fetchone()
        if complaint is None:
            raise HTTPException(status_code=404, detail="complaint not found")
        complaint = dict(complaint)
        segment = dal.get_segment(con, complaint["segment_id"])
        if segment is None or not segment.get("profile"):
            raise HTTPException(status_code=409, detail="segment has no cached Mireye profile; cannot run cache-only")

        guidance_version = dal.latest_guidance_version(con)
        account = MireyeAccount(label="live-demo", api_key=key)
        with MireyeClient(account) as client:
            ctx = CascadeContext(
                con=con,
                mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=0)),
                run_budget=RunBudget(ceiling=0),
                guidance_version=guidance_version,
            )
            triage, verdict, investigator, skeptic = asyncio.run(
                run_cascade(
                    con, ctx,
                    json.dumps(complaint, default=str),
                    json.dumps({"segment": segment}, default=str),
                )
            )

    return {
        "case_number": case_number,
        "triage": triage.model_dump() if triage else None,
        "investigator": investigator.model_dump() if investigator else None,
        "skeptic": skeptic.model_dump() if skeptic else None,
        "verdict": verdict.model_dump() if verdict else None,
        "persisted": False,
    }
