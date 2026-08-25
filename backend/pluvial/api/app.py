"""FastAPI surface for Pluvial-AI.

Two products share one app. Address mode — the live, national, map-anchored
product — lives in `analyze_routes.py` and is mounted here. Everything
defined in this file is the Houston evaluation surface (design spec §7):

GET /queue        — today's Triage-promoted cases with their verdicts, for
                     the dispatcher board's three columns.
GET /segments/{id} — a segment's physical profile + complaint history, for
                     both the dispatcher card expansion and the public view.
POST /reprofile/{id} — force a fresh Mireye fetch for a segment (admin use;
                     still goes through the wrapper's quote-then-fetch path).

`POST /cascade/run` used to live here and is deliberately gone. It pinned
the Mireye wrapper at ceiling=0, so the flagship "prove it's real" path
provably never called Mireye — it re-reasoned over a profile fetched months
earlier at build time. `POST /analyze/plan` + `GET /analyze/run` replace it
and fetch live ground for whatever address is typed.
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

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"

from pluvial.api.analyze_routes import router as analyze_router
from pluvial.api.chat_routes import router as chat_router

app = FastAPI(title="Pluvial-AI API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tightened before public deployment
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(analyze_router)
app.include_router(chat_router)


@app.on_event("startup")
def _ensure_schema() -> None:
    dal.init_db()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/queue")
def queue(limit: int = 100):
    with dal.connect() as con:
        rows = dal.queue_cards(con, limit)

    cards = []
    for r in rows:
        d = dict(r)
        d["reasoning"] = d.pop("reasoning_json")
        d["cited_evidence"] = d.pop("cited_evidence_json")
        d["invalidation_condition"] = d.pop("invalidation_condition_json")
        d["reawakened"] = d["reawakened_from"] is not None
        cards.append(d)
    return {"cards": cards}


@app.get("/segments/{segment_id}")
def segment_detail(segment_id: int):
    with dal.connect() as con:
        seg = dal.get_segment(con, segment_id)
        if seg is None:
            raise HTTPException(status_code=404, detail="segment not found")
        complaints = dal.segment_complaints(con, segment_id)
        verdicts = dal.segment_verdicts(con, segment_id)
    return {
        "segment": seg,
        "complaints": complaints,
        "verdicts": verdicts,
    }


@app.post("/reprofile/{segment_id}")
def reprofile(segment_id: int):
    key = os.environ.get("MIREYE_API_KEY_1")
    if not key:
        raise HTTPException(status_code=400, detail="MIREYE_API_KEY_1 not set")
    ceiling = int(os.environ.get("PLUVIAL_CREDIT_CEILING_PER_RUN", 500))

    with dal.connect() as con:
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

    with dal.connect() as con:
        # bounding-box pre-filter (~5.5km at Houston's latitude), then exact
        # haversine over the candidates — avoids scanning all 168k segments.
        candidates = dal.segments_near_bbox(con, lat - 0.05, lat + 0.05, lon - 0.05, lon + 0.05)
        if not candidates:
            raise HTTPException(status_code=404, detail="no known street segment near this address")
        nearest = min(candidates, key=lambda c: _haversine_m(lat, lon, c["centroid_lat"], c["centroid_lon"]))
        seg = dal.get_segment(con, nearest["segment_id"])
        verdicts = dal.segment_verdicts(con, nearest["segment_id"], limit=5)

    return {
        "matched_address": hits[0].get("display_name"),
        "geocoded": {"lat": lat, "lon": lon},
        "segment": seg,
        "distance_m": round(_haversine_m(lat, lon, seg["centroid_lat"], seg["centroid_lon"]), 1),
        "verdicts": verdicts,
        # HCAD's public search doesn't have a documented deep-link query
        # format; point at the search homepage rather than guess one.
        "assessor_link": "https://hcad.org/property-search",
    }


@app.get("/stats")
def stats():
    """Counters for the landing page. Every number here is read from the
    live database or a written eval report — nothing is hardcoded, so the
    page can never drift from what the system actually did."""
    with dal.connect() as con:
        counts = dal.stats_counts(con)

    segments_profiled = counts["segments_profiled"]
    soil_usable = counts["soil_usable"]
    return {
        "segments_profiled": segments_profiled,
        "soil_usable": soil_usable,
        "soil_usable_rate": round(soil_usable / segments_profiled, 4) if segments_profiled else None,
        "complaints": counts["complaints"],
        "verdicts": counts["verdicts"],
        "reawakened": counts["reawakened"],
        "dispositions": counts["dispositions"],
        "outcomes": counts["outcomes"],
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
        # Address mode over the same pinned cases, fed only ground physics.
        # The gap against `full` is what complaint evidence contributes.
        ("address", "backtest_rescore_address_2026-07-15.json"),
    ):
        path = DATA_DIR / filename
        if not path.exists():
            out[label] = None
            continue
        data = json.loads(path.read_text())
        out[label] = {k: data.get(k) for k in ("n", "precision", "recall", "true_positive", "false_positive")}
        for extra in ("severity_counts", "skipped_unprofiled", "threat", "by_soil_usable"):
            if extra in data:
                out[label][extra] = data[extra]

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
    with dal.connect() as con:
        rows = dal.list_verdicts_brief(con, limit)
    return {"verdicts": rows}


@app.get("/verdicts/{verdict_id}")
def verdict_detail(verdict_id: int):
    """Everything the case-file view needs to replay one decision: the full
    Investigator / Skeptic / Adjudicator record, the complaints it covers,
    the segment's physical profile, and the prior verdict if this one was
    reawakened."""
    with dal.connect() as con:
        v = dal.get_verdict(con, verdict_id)
        if v is None:
            raise HTTPException(status_code=404, detail="verdict not found")
        case_numbers = v.pop("case_numbers")
        v["reasoning"] = v.pop("reasoning_json")
        v["cited_evidence"] = v.pop("cited_evidence_json")
        v["invalidation_condition"] = v.pop("invalidation_condition_json")

        complaints = dal.complaints_by_case_numbers(con, case_numbers) if case_numbers else []

        segment = dal.get_segment(con, v["segment_id"])
        moisture = dal.current_trigger_state(con, as_of=v["decided_at"])

        prior = None
        if v.get("reawakened_from"):
            prior = dal.verdict_brief(con, v["reawakened_from"])

    return {
        "verdict": v,
        "case_numbers": case_numbers,
        "complaints": complaints,
        "segment": segment,
        "moisture": moisture,
        "prior_verdict": prior,
    }
