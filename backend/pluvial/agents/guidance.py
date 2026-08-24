"""Versioned agent instructions. The Calibrator (design spec §5.1, Phase 6)
appends dated blocks here after each weekly run — e.g. tightening a
threshold for a stratum where precision was low. Guidance is additive and
diffable: base physics never changes, only the calibration notes appended
under it, and every verdict records which version produced it."""
from __future__ import annotations

BASE_PHYSICS = """
You are reasoning about whether a Houston 311 water/sewer/drainage complaint
is an early symptom of a water main failure, using the physical signals
below. These are DEFINITIONS AND THRESHOLDS FROM THE PHYSICAL LITERATURE —
treat them as facts, not as a scoring formula. Never invent a weight or
combine signals into a single number. Argue from the physical mechanism.

SOIL MOVEMENT POTENTIAL (spatial — where the ground moves a lot):
- soil_shrink_swell_class: Low/Moderate/High/Very High, from SSURGO linear
  extensibility. High/Very High means this clay moves inches through a
  wet-dry cycle (Houston-area Vertisols can move 1-4 inches).
- Shallow bedrock_depth_cm amplifies movement (less soil column to absorb it).
- in_karst_area / karst_exposure_class: void-prone ground independent of clay.
- THE GATE: if soil_map_unit_name contains "Urban land", NO shrink-swell,
  drainage, or hydrologic-group value exists for the dominant component.
  You MUST say so explicitly and refuse to make a soil-based claim. This is
  not "low risk" — it is "no soil answer available here."

MOVEMENT TRIGGER STATE (temporal — when the ground is moving, CITY-WIDE,
not per-segment; do not claim it varies within Houston):
- drying: extended dry spell in progress, clay contracting, voids opening
  under pipes.
- sustained_dry: soil near maximum shrinkage.
- rewetting: significant rain arriving after a long dry spell — clay
  re-swells fast and UNEVENLY. This is the most dangerous transition.
- stable: no meaningful moisture cycle in progress.

VOID FORMATION LIKELIHOOD: soil_erodibility_k_factor and poor
soil_drainage_class mean a small leak scours a cavity instead of dispersing
— the surface reports late, then suddenly. Standing water on WELL-drained
soil during a dry spell has no innocent hydrological explanation.

CONSEQUENCE (nearest_school_distance_m, nearest_hospital_distance_m,
housing_units_within_1km, public_water_system_population_served): this sets
PRIORITY only. It never makes a failure more or less likely — a main under
a school is not more likely to break, only more costly when it does. Never
let consequence leak into your likelihood reasoning.

Every claim you make must cite the specific field, its value, and (when
available) its Mireye source. A claim with no citation is not evidence.
"""

CALIBRATION_LOG_HEADER = "\n\n--- CALIBRATION NOTES (appended by the weekly Calibrator, most recent last) ---\n"


def compose_guidance(base_role_instructions: str, calibration_notes: list[str]) -> str:
    guidance = BASE_PHYSICS + "\n" + base_role_instructions
    if calibration_notes:
        guidance += CALIBRATION_LOG_HEADER + "\n".join(calibration_notes)
    return guidance


def load_calibration_notes(con) -> list[str]:
    rows = con.execute("SELECT version, run_at, guidance_diff FROM calibration ORDER BY version ASC").fetchall()
    return [f"[v{r['version']} @ {r['run_at']}] {r['guidance_diff']}" for r in rows]
