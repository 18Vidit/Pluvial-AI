"use client";

import { SampleView } from "@/lib/address-types";

/* The brief's "an answer nobody can check is not a result", made literal:
   click any point and read the raw values with the source Mireye cited for
   each one. Fields are grouped the way the signals are derived, not
   alphabetically, so the panel reads as an argument rather than a dump. */
const GROUPS: { title: string; fields: string[] }[] = [
  {
    title: "Soil movement",
    fields: [
      "soil_map_unit_name",
      "soil_shrink_swell_class",
      "soil_available_water_capacity",
      "soil_drainage_class",
      "bedrock_depth_cm",
    ],
  },
  {
    title: "Void formation",
    fields: ["soil_erodibility_k_factor", "soil_hydrologic_group", "in_karst_area", "karst_exposure_class"],
  },
  { title: "Moisture corroborator", fields: ["drought_category"] },
  {
    title: "Consequence",
    fields: [
      "nearest_school_distance_m",
      "nearest_hospital_distance_m",
      "nearest_major_road_class",
      "housing_units_within_1km",
      "public_water_system_population_served",
      "tract_population",
      "county_median_household_income",
    ],
  },
  {
    title: "Context and gage",
    fields: [
      "elevation",
      "water_system_name",
      "within_water_service_area",
      "nearest_usgs_gage_name",
      "nearest_usgs_gage_distance_m",
      "nearest_usgs_gage_daily_discharge_cfs",
    ],
  },
];

const ROLE_NOTE: Record<string, string> = {
  property: "The geocoded coordinate itself.",
  frontage: "30m out on the cross — roughly where a service line leaves the structure toward the street.",
  neighbourhood: "150m out — about the scale at which SSURGO map units change.",
};

export function SampleInspector({
  sample,
  onClose,
}: {
  sample: SampleView;
  onClose: () => void;
}) {
  const profile = sample.profile;

  return (
    <aside className="flex h-full flex-col rounded-lg border border-ground-700 bg-ground-850">
      <header className="flex items-start justify-between gap-3 border-b border-ground-700 px-4 py-3">
        <div>
          <p className="eyebrow">
            point {sample.sample_id} · {sample.role}
            {sample.bearing ? ` ${sample.bearing}` : ""}
          </p>
          <p className="data mt-1 text-[12px] text-bone-dim">
            {sample.lat.toFixed(5)}, {sample.lon.toFixed(5)}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded px-2 py-1 text-[12px] text-bone-faint transition-colors hover:bg-ground-750 hover:text-bone"
        >
          close
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <p className="text-[12.5px] leading-snug text-bone-dim">{ROLE_NOTE[sample.role]}</p>

        <div
          className={`mt-3 rounded border px-3 py-2 text-[12.5px] leading-snug ${
            sample.soil_usable
              ? "border-moisture/40 bg-moisture/8 text-bone"
              : "border-clay/45 bg-clay/10 text-clay-light"
          }`}
        >
          {sample.soil_usable
            ? "SSURGO has a real soil component here, so shrink-swell, drainage and erodibility are answerable at this point."
            : "The dominant SSURGO component here is Urban land. No shrink-swell, drainage or hydrologic-group value exists at this point — this is not low risk, it is no soil answer."}
        </div>

        {sample.citedBy.length > 0 && (
          <p className="mt-2 text-[12px] text-moisture">
            Cited by: {sample.citedBy.join(", ")}
          </p>
        )}
        {sample.vetoedBy.length > 0 && (
          <p className="mt-1 text-[12px] text-oxide-bright">
            Soil claims vetoed in: {sample.vetoedBy.join(", ")}
          </p>
        )}

        {!profile && (
          <p className="mt-4 text-[12.5px] text-bone-faint">
            Not fetched yet — this point is still in the plan.
          </p>
        )}

        {profile &&
          GROUPS.map((group) => {
            const rows = group.fields.filter((f) => profile[f] !== undefined);
            if (rows.length === 0) return null;
            return (
              <div key={group.title} className="mt-4">
                <p className="eyebrow">{group.title}</p>
                <dl className="mt-1.5 space-y-1.5">
                  {rows.map((f) => {
                    const entry = profile[f];
                    const value = entry?.value;
                    return (
                      <div key={f} className="border-b border-ground-700/60 pb-1.5">
                        <dt className="data text-[11px] text-bone-faint">{f}</dt>
                        <dd className="data text-[13px] text-bone">
                          {value === null || value === undefined ? "—" : String(value)}
                        </dd>
                        {entry?.source && (
                          <dd className="data mt-0.5 text-[10.5px] text-bone-faint">
                            source: {entry.source}
                          </dd>
                        )}
                      </div>
                    );
                  })}
                </dl>
              </div>
            );
          })}
      </div>
    </aside>
  );
}
