"""The fixed field selection Pluvial-AI fetches per segment.

This list is deliberately small and named, not "everything Mireye has" —
credits are metered per field per location (see design spec §9) and every
field here maps to one of the four derived signals or the consequence
surface. No field is fetched speculatively.
"""

# Soil Movement Potential (design spec §4.1) — spatial discriminator.
SOIL_MOVEMENT_FIELDS = [
    "soil_shrink_swell_class",
    "soil_available_water_capacity",
    "soil_drainage_class",
    "bedrock_depth_cm",
    "in_karst_area",
    "karst_exposure_class",
    "soil_map_unit_name",  # the gate: 'Urban land' -> soil_usable = False
]

# Void Formation Likelihood (§4.2)
VOID_FORMATION_FIELDS = [
    "soil_erodibility_k_factor",
    "soil_hydrologic_group",
]

# Corroborating temporal signal (§4.2) — coarse, frequently null; NOAA NCEI
# daily precipitation is the primary trigger, this is the cited backstop.
DROUGHT_CORROBORATOR_FIELDS = [
    "drought_category",
]

# Consequence surface (§4.4) — priority, never probability.
CONSEQUENCE_FIELDS = [
    "nearest_school_distance_m",
    "nearest_hospital_distance_m",
    "nearest_major_road_class",
    "housing_units_within_1km",
    "public_water_system_population_served",
    "tract_population",
    "county_median_household_income",
]

# Context, cheap, used for the reporting-bias correction (§4.6) and general
# framing in agent explanations.
CONTEXT_FIELDS = [
    "elevation",
    "water_system_name",
    "within_water_service_area",
]

# Used by the Skeptic to rule out natural surface water as the source of a
# "standing water" complaint (design spec §5.2, usgs_gage tool).
GAGE_FIELDS = [
    "nearest_usgs_gage_daily_discharge_cfs",
    "nearest_usgs_gage_distance_m",
    "nearest_usgs_gage_name",
]

ALL_FIELDS = (
    SOIL_MOVEMENT_FIELDS
    + VOID_FORMATION_FIELDS
    + DROUGHT_CORROBORATOR_FIELDS
    + CONSEQUENCE_FIELDS
    + CONTEXT_FIELDS
    + GAGE_FIELDS
)

# de-dupe while preserving order (soil_map_unit_name etc. only listed once)
ALL_FIELDS = list(dict.fromkeys(ALL_FIELDS))

URBAN_LAND_MARKERS = ("urban land",)


def is_soil_usable(field_values: dict[str, object]) -> bool:
    """False when the dominant SSURGO component is Urban land — no
    shrink-swell, drainage or hydrologic-group data exists at that point.
    The agents must refuse to make a soil claim rather than guess.

    Accepts either a bare value or the {"value": ..., "source": ...} shape
    Mireye responses are normalized into (see mireye/wrapper.py)."""
    raw = field_values.get("soil_map_unit_name")
    if isinstance(raw, dict):
        raw = raw.get("value")
    if not raw:
        return False
    unit_name = str(raw).lower()
    return not any(marker in unit_name for marker in URBAN_LAND_MARKERS)
