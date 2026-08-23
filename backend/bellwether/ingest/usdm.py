"""US Drought Monitor lookup — the coarse weekly corroborator for the
Movement Trigger State (design spec §4.2). Verified during design research:
the ArcGIS FeatureServer returns a DM class per point when a drought
polygon covers it, and nothing (not even D0) when it doesn't — which is
currently the case everywhere in Houston.
"""
from __future__ import annotations

import httpx

USDM_URL = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/USDM_current/FeatureServer/0/query"

HOUSTON_REPRESENTATIVE_POINT = (29.76, -95.41)  # metro centroid; polygons don't vary within the metro


def current_usdm_class(lat: float = HOUSTON_REPRESENTATIVE_POINT[0], lon: float = HOUSTON_REPRESENTATIVE_POINT[1]) -> str | None:
    with httpx.Client(timeout=30) as client:
        r = client.get(
            USDM_URL,
            params={
                "geometry": f"{lon},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "DM",
                "returnGeometry": "false",
                "f": "json",
            },
        )
        r.raise_for_status()
        features = r.json().get("features", [])
        if not features:
            return None  # no drought polygon: better than D0, per Mireye's own field notes
        dm = features[0]["attributes"].get("DM")
        return f"D{dm}" if dm is not None else None
