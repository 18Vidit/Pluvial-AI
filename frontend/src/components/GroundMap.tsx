"use client";

import * as maplibregl from "maplibre-gl";
import type { GeoJSONSource, Map as MapLibreMap, MapMouseEvent } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useCallback, useEffect, useMemo, useRef } from "react";

import { SampleView } from "@/lib/address-types";

/* CARTO's dark basemap: free, keyless, and already the right value range for
   a palette built on soil-black. Attribution is required and is rendered by
   MapLibre's own control below. Nothing here can leak a credential because
   there is no credential. */
const RASTER_TILES = [
  "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
  "https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
  "https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
];
const ATTRIBUTION =
  '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap</a> contributors, <a href="https://carto.com/attributions">© CARTO</a>';

/* Served as a static asset by scripts/copy-maplibre-worker.mjs rather than
   bundled. Turbopack does not resolve maplibre's internal worker URL, and the
   failure is silent in the worst way: raster tiles still paint on the main
   thread, so the basemap looks fine while no GeoJSON layer ever renders. */
maplibregl.setWorkerUrl("/maplibre/maplibre-gl-worker.mjs");

const US_CENTER: [number, number] = [-98.5, 39.8];
const US_ZOOM = 3.4;
const SITE_ZOOM = 17.4;

/* Point states, in the order a sample moves through them. Read as a legend:
   unfetched ground is an outline, fetched ground is filled clay, cited ground
   is live teal, vetoed ground is struck out. */
const STATE_COLORS: Record<string, string> = {
  pending: "#6f6559",   // bone-faint
  fetched: "#b99a73",   // clay-light
  cited: "#4fa8a0",     // moisture
  vetoed: "#382c20",    // ground-700 — present, but with nothing to say
};

interface Props {
  center: { lat: number; lon: number } | null;
  samples: SampleView[];
  /** Distances, not coordinates — so these are drawn as rings around the
   *  property, never as pins. Mireye returns "how far to the nearest
   *  school", and inventing a position for it would be fabrication. */
  consequences?: { label: string; distance_m: number }[];
  selectedSampleId?: number | null;
  onSelectSample?: (sampleId: number | null) => void;
  /** Region-search cells, drawn as rectangles shaded by objective score. */
  cells?: { cell_id: number; bbox: BBox; score: number | null; subdivided: boolean }[];
  cellBounds?: BBox | null;
}

export interface BBox {
  min_lat: number;
  min_lon: number;
  max_lat: number;
  max_lon: number;
}

const EARTH_R = 6_371_000;

/** A circle on the ground as a GeoJSON ring. 64 segments is smooth at any
 *  zoom a property is viewed at, and the cos(lat) term keeps the ring round
 *  rather than an ellipse away from the equator. */
function ringGeometry(lat: number, lon: number, radiusM: number, steps = 64) {
  const coords: [number, number][] = [];
  for (let i = 0; i <= steps; i++) {
    const theta = (i / steps) * 2 * Math.PI;
    const dy = (radiusM * Math.sin(theta)) / EARTH_R;
    const dx = (radiusM * Math.cos(theta)) / (EARTH_R * Math.cos((lat * Math.PI) / 180));
    coords.push([lon + (dx * 180) / Math.PI, lat + (dy * 180) / Math.PI]);
  }
  return coords;
}

function samplesToGeoJSON(samples: SampleView[]) {
  return {
    type: "FeatureCollection" as const,
    features: samples.map((s) => ({
      type: "Feature" as const,
      id: s.sample_id,
      geometry: { type: "Point" as const, coordinates: [s.lon, s.lat] },
      properties: {
        sample_id: s.sample_id,
        role: s.role,
        state: s.state,
        color: STATE_COLORS[s.state] ?? STATE_COLORS.pending,
        radius: s.role === "property" ? 9 : s.role === "frontage" ? 6.5 : 5.5,
        soil_usable: s.soil_usable === true,
        label: s.bearing ?? "site",
      },
    })),
  };
}

function crossToGeoJSON(samples: SampleView[]) {
  const property = samples.find((s) => s.role === "property");
  if (!property) return { type: "FeatureCollection" as const, features: [] };
  return {
    type: "FeatureCollection" as const,
    features: samples
      .filter((s) => s.role !== "property")
      .map((s) => ({
        type: "Feature" as const,
        geometry: {
          type: "LineString" as const,
          coordinates: [
            [property.lon, property.lat],
            [s.lon, s.lat],
          ],
        },
        properties: { role: s.role },
      })),
  };
}

function ringsToGeoJSON(
  center: { lat: number; lon: number } | null,
  consequences: { label: string; distance_m: number }[],
) {
  if (!center) return { type: "FeatureCollection" as const, features: [] };
  return {
    type: "FeatureCollection" as const,
    features: consequences
      .filter((c) => Number.isFinite(c.distance_m) && c.distance_m > 0)
      .map((c) => ({
        type: "Feature" as const,
        geometry: {
          type: "LineString" as const,
          coordinates: ringGeometry(center.lat, center.lon, c.distance_m),
        },
        properties: { label: c.label, distance_m: Math.round(c.distance_m) },
      })),
  };
}

function cellsToGeoJSON(cells: NonNullable<Props["cells"]>) {
  return {
    type: "FeatureCollection" as const,
    features: cells.map((c) => ({
      type: "Feature" as const,
      id: c.cell_id,
      geometry: {
        type: "Polygon" as const,
        coordinates: [
          [
            [c.bbox.min_lon, c.bbox.min_lat],
            [c.bbox.max_lon, c.bbox.min_lat],
            [c.bbox.max_lon, c.bbox.max_lat],
            [c.bbox.min_lon, c.bbox.max_lat],
            [c.bbox.min_lon, c.bbox.min_lat],
          ],
        ],
      },
      properties: { score: c.score ?? -1, subdivided: c.subdivided },
    })),
  };
}

export function GroundMap({
  center,
  samples,
  consequences = [],
  selectedSampleId = null,
  onSelectSample,
  cells = [],
  cellBounds = null,
}: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<MapLibreMap | null>(null);
  const flownTo = useRef<string | null>(null);

  const sampleData = useMemo(() => samplesToGeoJSON(samples), [samples]);
  const crossData = useMemo(() => crossToGeoJSON(samples), [samples]);
  const ringData = useMemo(() => ringsToGeoJSON(center, consequences), [center, consequences]);
  const cellData = useMemo(() => cellsToGeoJSON(cells), [cells]);

  /* Every layer's data is mirrored here so the load handler can seed all
     four sources from whatever the current props are. Without it there is a
     race with real consequences: the sample plan usually arrives before the
     style finishes loading, the setData call lands on a source that does not
     exist yet, and the points silently never appear. */
  const latest = useRef({ samples: sampleData, cross: crossData, rings: ringData, cells: cellData });
  latest.current = { samples: sampleData, cross: crossData, rings: ringData, cells: cellData };

  /* A no-op before the source exists, by design — the load handler seeds
     from `latest` the moment it does. */
  const setData = useCallback((id: string, data: unknown) => {
    const source = map.current?.getSource(id) as GeoJSONSource | undefined;
    source?.setData(data as never);
  }, []);

  useEffect(() => {
    if (map.current || !container.current) return;

    const m = new maplibregl.Map({
      container: container.current,
      style: {
        version: 8,
        sources: {
          basemap: { type: "raster", tiles: RASTER_TILES, tileSize: 256, attribution: ATTRIBUTION },
        },
        layers: [{ id: "basemap", type: "raster", source: "basemap" }],
      },
      center: US_CENTER,
      zoom: US_ZOOM,
      attributionControl: false,
    });
    m.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.current = m;
    if (process.env.NODE_ENV !== "production") {
      (window as unknown as { __groundMap?: MapLibreMap }).__groundMap = m;
    }

    m.on("load", () => {
      const empty = { type: "FeatureCollection", features: [] } as const;
      m.addSource("cells", { type: "geojson", data: empty as never });
      m.addSource("rings", { type: "geojson", data: empty as never });
      m.addSource("cross", { type: "geojson", data: empty as never });
      m.addSource("samples", { type: "geojson", data: empty as never });

      m.addLayer({
        id: "cells-fill",
        type: "fill",
        source: "cells",
        paint: {
          // Score runs 0 (bad ground for the objective) to 1 (good). Oxide
          // through clay to moisture: the same three-signal vocabulary the
          // rest of the interface uses, so a reviewer does not have to learn
          // a second colour language for the region layer.
          "fill-color": [
            "interpolate", ["linear"], ["get", "score"],
            -1, "#221b15", 0, "#c4552e", 0.5, "#8a6f52", 1, "#4fa8a0",
          ],
          "fill-opacity": ["case", ["get", "subdivided"], 0.06, 0.42],
        },
      });
      m.addLayer({
        id: "cells-outline",
        type: "line",
        source: "cells",
        paint: { "line-color": "#382c20", "line-width": 0.75 },
      });

      m.addLayer({
        id: "rings",
        type: "line",
        source: "rings",
        paint: {
          "line-color": "#6f6559",
          "line-width": 1,
          "line-dasharray": [3, 3],
          "line-opacity": 0.65,
        },
      });
      m.addLayer({
        id: "cross",
        type: "line",
        source: "cross",
        paint: {
          "line-color": ["case", ["==", ["get", "role"], "frontage"], "#b99a73", "#5c4a37"],
          "line-width": ["case", ["==", ["get", "role"], "frontage"], 1.4, 0.8],
          "line-opacity": 0.55,
        },
      });

      m.addLayer({
        id: "samples-halo",
        type: "circle",
        source: "samples",
        paint: {
          "circle-radius": ["+", ["get", "radius"], 7],
          "circle-color": ["get", "color"],
          "circle-opacity": ["case", ["==", ["get", "state"], "cited"], 0.22, 0],
        },
      });
      m.addLayer({
        id: "samples",
        type: "circle",
        source: "samples",
        paint: {
          "circle-radius": ["get", "radius"],
          "circle-color": ["case", ["==", ["get", "state"], "pending"], "#14100d", ["get", "color"]],
          "circle-stroke-width": 1.8,
          "circle-stroke-color": ["get", "color"],
          "circle-opacity": ["case", ["==", ["get", "state"], "vetoed"], 0.45, 0.92],
        },
      });
      m.addLayer({
        id: "samples-label",
        type: "symbol",
        source: "samples",
        layout: {
          "text-field": ["get", "label"],
          "text-size": 10,
          "text-offset": [0, 1.5],
          "text-anchor": "top",
          "text-allow-overlap": false,
        },
        paint: { "text-color": "#a99c8c", "text-halo-color": "#14100d", "text-halo-width": 1.2 },
      });

      m.on("click", "samples", (e: MapMouseEvent & { features?: maplibregl.MapGeoJSONFeature[] }) => {
        const id = e.features?.[0]?.properties?.sample_id;
        if (id != null) onSelectSample?.(Number(id));
      });
      m.on("click", (e: MapMouseEvent) => {
        const hits = m.queryRenderedFeatures(e.point, { layers: ["samples"] });
        if (hits.length === 0) onSelectSample?.(null);
      });
      m.on("mouseenter", "samples", () => (m.getCanvas().style.cursor = "pointer"));
      m.on("mouseleave", "samples", () => (m.getCanvas().style.cursor = ""));

      setData("samples", latest.current.samples);
      setData("cross", latest.current.cross);
      setData("rings", latest.current.rings);
      setData("cells", latest.current.cells);
    });

    return () => {
      m.remove();
      map.current = null;
    };
    // Layers are created once; data updates flow through the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => setData("samples", sampleData), [sampleData, setData]);
  useEffect(() => setData("cross", crossData), [crossData, setData]);
  useEffect(() => setData("rings", ringData), [ringData, setData]);
  useEffect(() => setData("cells", cellData), [cellData, setData]);

  /* Fly once per location. Re-flying on every state change would yank the
     map out from under anyone who panned to look at a point. */
  useEffect(() => {
    if (!map.current || !center) return;
    const key = `${center.lat.toFixed(5)},${center.lon.toFixed(5)}`;
    if (flownTo.current === key) return;
    flownTo.current = key;
    map.current.flyTo({ center: [center.lon, center.lat], zoom: SITE_ZOOM, duration: 2200, essential: true });
  }, [center]);

  useEffect(() => {
    if (!map.current || !cellBounds) return;
    map.current.fitBounds(
      [
        [cellBounds.min_lon, cellBounds.min_lat],
        [cellBounds.max_lon, cellBounds.max_lat],
      ],
      { padding: 48, duration: 1600 },
    );
    flownTo.current = null;
  }, [cellBounds]);

  useEffect(() => {
    if (!map.current || selectedSampleId == null) return;
    const sample = samples.find((s) => s.sample_id === selectedSampleId);
    if (sample) map.current.easeTo({ center: [sample.lon, sample.lat], duration: 600 });
  }, [selectedSampleId, samples]);

  /* Positioned inline rather than with utility classes on purpose:
     maplibre-gl.css declares `.maplibregl-map { position: relative }` and is
     injected after Tailwind's layer, so a `.absolute` class on this element
     loses the cascade and the map collapses to zero height. An inline style
     outranks both. */
  return (
    <div
      ref={container}
      style={{ position: "absolute", inset: 0 }}
      aria-label="Sampled ground around this address"
    />
  );
}
