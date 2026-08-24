"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { fetchLookup, LookupError } from "@/lib/api";
import { LookupResponse } from "@/lib/types";
import { SectionCut } from "@/components/SectionCut";

const DISPOSITION_LABEL: Record<string, string> = {
  dispatch: "Crew dispatched",
  inspect: "Inspection queued",
  monitor: "Being monitored",
  close: "Closed — no action",
};

const DISPOSITION_DOT: Record<string, string> = {
  dispatch: "bg-oxide",
  inspect: "bg-ochre",
  monitor: "bg-moisture",
  close: "bg-bone-faint",
};

/** A few real Houston streets that are in the profiled sample, so a first-time
 *  visitor can get a meaningful result without knowing an address to try. */
const EXAMPLES = ["Vinkins Road", "Wilcrest Drive", "901 Bagby St", "Westheimer Road"];

export function AddressLookup({ initialQuery = "" }: { initialQuery?: string }) {
  const [address, setAddress] = useState(initialQuery);
  const [result, setResult] = useState<LookupResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const search = useCallback(async (value: string) => {
    const q = value.trim();
    if (!q) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      setResult(await fetchLookup(q));
    } catch (err) {
      if (err instanceof LookupError && err.status === 404) {
        setError("No street on file near that address. Try a Houston street name.");
      } else {
        setError(err instanceof Error ? err.message : "Something went wrong.");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  // Deep link: /lookup?q=Vinkins Road runs the search on arrival.
  useEffect(() => {
    if (initialQuery) search(initialQuery);
  }, [initialQuery, search]);

  const seg = result?.segment;
  const profile = seg?.profile;
  const field = (k: string) => {
    const v = profile?.[k]?.value;
    return v === null || v === undefined ? null : String(v);
  };
  const soilUsable = seg?.soil_usable === 1;

  return (
    <div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          search(address);
        }}
        className="flex flex-col sm:flex-row gap-2.5"
      >
        <div className="flex-1">
          <label htmlFor="address" className="sr-only">
            Houston street or address
          </label>
          <input
            id="address"
            type="text"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="Street or address in Houston"
            autoComplete="street-address"
            className="w-full rounded border border-ground-700 bg-ground-850 px-4 py-3 text-[15px] text-bone placeholder:text-bone-faint transition-colors duration-200 focus:border-moisture focus:outline-none"
          />
        </div>
        <button
          type="submit"
          disabled={loading || !address.trim()}
          className="rounded bg-bone px-6 py-3 text-sm font-medium text-ground-900 transition-colors duration-200 hover:bg-clay-light disabled:opacity-50"
        >
          {loading ? "Searching…" : "Search"}
        </button>
      </form>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="data text-[10.5px] uppercase tracking-wider text-bone-faint">Try</span>
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            type="button"
            onClick={() => {
              setAddress(ex);
              search(ex);
            }}
            className="rounded-full border border-ground-700 px-3 py-1 text-xs text-bone-dim transition-colors duration-200 hover:border-bone-faint hover:text-bone"
          >
            {ex}
          </button>
        ))}
      </div>

      {error && (
        <div
          role="alert"
          className="mt-6 rounded border border-oxide/40 bg-oxide/10 px-4 py-3 text-sm text-oxide-bright"
        >
          {error}
        </div>
      )}

      {loading && (
        <div className="mt-8 space-y-3" aria-hidden="true">
          <div className="h-4 w-2/3 rounded bg-ground-800 animate-pulse" />
          <div className="h-56 rounded-lg bg-ground-800 animate-pulse" />
        </div>
      )}

      {result && seg && (
        <div className="mt-8 space-y-5 animate-rise">
          <div className="rounded-lg border border-ground-700 bg-ground-850 p-5 sm:p-6">
            <p className="eyebrow mb-2">Matched</p>
            <p className="text-[15px] leading-snug text-bone">{result.matched_address}</p>
            <p className="mt-2 text-xs text-bone-dim">
              Nearest street on file:{" "}
              <span className="text-bone">{seg.name ?? `Segment ${seg.segment_id}`}</span>
              <span className="data text-bone-faint"> · {result.distance_m.toLocaleString()}m away</span>
            </p>
          </div>

          <div className="rounded-lg border border-ground-700 bg-ground-850 p-5 sm:p-6">
            <div className="flex flex-wrap items-baseline justify-between gap-3 mb-4">
              <span className="eyebrow">The ground under it</span>
              {seg.profiled_at ? (
                <span
                  className={`data rounded-sm px-2 py-0.5 text-[10px] uppercase tracking-wider ${
                    soilUsable ? "bg-moisture/15 text-moisture" : "bg-ochre/15 text-ochre"
                  }`}
                >
                  {soilUsable ? "Soil readable" : "Urban land — no reading"}
                </span>
              ) : (
                <span className="data rounded-sm bg-ground-750 px-2 py-0.5 text-[10px] uppercase tracking-wider text-bone-faint">
                  Not yet profiled
                </span>
              )}
            </div>

            {seg.profiled_at ? (
              <>
                <SectionCut
                  shrinkSwell={field("soil_shrink_swell_class")}
                  drainage={field("soil_drainage_class")}
                  mapUnit={field("soil_map_unit_name")}
                  bedrockCm={field("bedrock_depth_cm") ? Number(field("bedrock_depth_cm")) : null}
                  soilUsable={soilUsable}
                />
                <dl className="mt-5 grid gap-x-8 gap-y-2 sm:grid-cols-2">
                  {[
                    ["soil_map_unit_name", "Map unit"],
                    ["soil_shrink_swell_class", "Shrink–swell"],
                    ["soil_drainage_class", "Drainage"],
                    ["bedrock_depth_cm", "Bedrock depth"],
                    ["nearest_school_distance_m", "Nearest school"],
                    ["nearest_hospital_distance_m", "Nearest hospital"],
                  ].map(([k, label]) => {
                    const v = field(k);
                    if (!v) return null;
                    const num = Number(v);
                    const display =
                      k.endsWith("_m") || k.endsWith("_cm")
                        ? `${Math.round(num).toLocaleString()}${k.endsWith("_cm") ? "cm" : "m"}`
                        : v;
                    return (
                      <div
                        key={k}
                        className="flex items-baseline justify-between gap-4 border-b border-ground-700 pb-1.5"
                      >
                        <dt className="text-xs text-bone-dim shrink-0">{label}</dt>
                        <dd className="data text-[11.5px] text-bone text-right">{display}</dd>
                      </div>
                    );
                  })}
                </dl>
              </>
            ) : (
              <p className="text-sm leading-relaxed text-bone-dim">
                This street hasn&apos;t been profiled through Mireye yet, so there&apos;s no
                subsurface reading on file. Profiling is done in bulk against streets with
                complaint history.
              </p>
            )}
          </div>

          <div className="rounded-lg border border-ground-700 bg-ground-850 p-5 sm:p-6">
            <p className="eyebrow mb-3">City record for this street</p>
            {result.verdicts.length === 0 ? (
              <p className="text-sm text-bone-dim">
                No triaged water complaints on file here.
              </p>
            ) : (
              <ul className="space-y-2">
                {result.verdicts.map((v) => (
                  <li key={v.verdict_id}>
                    <Link
                      href={`/case/${v.verdict_id}`}
                      className="flex items-center gap-3 rounded border border-ground-700 bg-ground-800 px-4 py-3 transition-colors duration-200 hover:bg-ground-750"
                    >
                      <span
                        className={`h-2 w-2 shrink-0 rounded-sm ${DISPOSITION_DOT[v.disposition] ?? "bg-bone-faint"}`}
                        aria-hidden="true"
                      />
                      <span className="text-sm text-bone">
                        {DISPOSITION_LABEL[v.disposition] ?? v.disposition}
                      </span>
                      <span className="data ml-auto text-[10.5px] text-bone-faint">
                        {new Date(v.decided_at).toLocaleDateString("en-US", {
                          timeZone: "America/Chicago",
                          dateStyle: "medium",
                        })}
                      </span>
                      <span className="data text-[10.5px] text-bone-faint" aria-hidden="true">→</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <p className="text-xs leading-relaxed text-bone-faint">
            Owner and parcel records aren&apos;t part of this tool.{" "}
            <a
              href={result.assessor_link}
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-2 hover:text-bone-dim"
            >
              Look up the county assessor record
            </a>
            .
          </p>
        </div>
      )}
    </div>
  );
}
