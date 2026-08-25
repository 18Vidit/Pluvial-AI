import Link from "next/link";
import { fetchStats, fetchVerdicts } from "@/lib/api";
import { SectionCut } from "@/components/SectionCut";
import { Stats, VerdictListItem } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Depth rail: sections are marked by how far down the cause sits, because
 *  the page itself descends from surface symptom to subsurface mechanism. */
function Depth({ cm, label }: { cm: string; label: string }) {
  return (
    <div className="flex items-baseline gap-3 mb-5">
      <span className="data text-xs text-oxide-bright tabular-nums">{cm}</span>
      <span className="h-px flex-1 max-w-[64px] bg-ground-700" aria-hidden="true" />
      <span className="eyebrow">{label}</span>
    </div>
  );
}

/* Explicit map — Tailwind extracts class names statically, so `text-${tone}`
   would silently produce no styles at all. */
const TONE: Record<string, string> = {
  bone: "text-bone",
  "clay-light": "text-clay-light",
  moisture: "text-moisture",
  "oxide-bright": "text-oxide-bright",
};

function Stat({ value, label, tone = "bone" }: { value: string; label: string; tone?: keyof typeof TONE }) {
  return (
    <div>
      <div className={`data text-2xl sm:text-3xl tabular-nums ${TONE[tone] ?? TONE.bone}`}>{value}</div>
      <div className="mt-1 text-xs text-bone-dim leading-snug">{label}</div>
    </div>
  );
}

export default async function Landing() {
  let stats: Stats | null = null;
  let featured: VerdictListItem | null = null;
  let error: string | null = null;

  try {
    const [s, v] = await Promise.all([fetchStats(), fetchVerdicts(60)]);
    stats = s;
    featured =
      v.verdicts.find((x) => x.disposition === "inspect" && x.segment_name) ??
      v.verdicts[0] ??
      null;
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  }

  const pct = (x: number | null | undefined) =>
    x == null ? "—" : `${Math.round(x * 100)}%`;

  return (
    <main className="w-full">
      {/* ── 0cm · surface ───────────────────────────────────────────── */}
      <section className="mx-auto max-w-7xl px-5 sm:px-8 pt-12 pb-16 sm:pt-20">
        <div className="grid lg:grid-cols-[1.05fr_1.35fr] gap-10 lg:gap-16 items-center">
          <div>
            <p className="eyebrow mb-5">Ground risk · any US address</p>
            <h1 className="display text-[2.6rem] sm:text-6xl xl:text-[4.4rem] text-bone">
              The clay moves.
              <br />
              <span className="text-clay-light">The pipes don&apos;t.</span>
            </h1>
            <p className="mt-6 text-[15px] sm:text-base leading-relaxed text-bone-dim max-w-lg">
              Expansive soil swells and shrinks through every wet–dry cycle — inches, not
              millimetres — and takes foundations and buried service lines with it. It is
              not a Texas problem: it runs through Colorado&apos;s Front Range, the
              Midwest and the Mississippi embayment.
            </p>
            <p className="mt-4 text-[15px] sm:text-base leading-relaxed text-bone-dim max-w-lg">
              Type an address. Pluvial-AI fetches the ground under it from Mireye at the
              moment you ask, then three pairs of agents argue over what it means — with
              every claim anchored to a specific sampled point on the map. Where there is
              no soil answer, it says so rather than calling it safe.
            </p>

            <div className="mt-8 flex flex-wrap gap-3">
              <Link
                href="/address"
                className="inline-flex items-center gap-2 rounded bg-bone px-5 py-3 text-sm font-medium text-ground-900 transition-colors duration-200 hover:bg-clay-light"
              >
                Analyse an address
                <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden="true">
                  <path d="M4 2.5v10l8-5-8-5z" fill="currentColor" />
                </svg>
              </Link>
              {featured && (
                <Link
                  href={`/case/${featured.verdict_id}`}
                  className="inline-flex items-center rounded border border-ground-700 px-5 py-3 text-sm text-bone transition-colors duration-200 hover:bg-ground-800"
                >
                  Watch a recorded decision
                </Link>
              )}
            </div>

            <p className="mt-5 text-xs leading-relaxed text-bone-faint max-w-lg">
              The Houston 311 corpus below is how the reasoning is measured, not what the
              product does — 395,783 real complaints with recorded verdicts and a
              backtest against escalation labels.
            </p>
          </div>

          {/* The thesis, drawn. Very High shrink-swell on a rewetting cycle:
              the exact condition the system is built to catch. */}
          <div className="rounded-lg border border-ground-700 bg-ground-850 p-5 sm:p-7">
            <div className="flex items-baseline justify-between mb-4 gap-4">
              <span className="eyebrow">Section cut · typical Houston block</span>
              <span className="data text-[11px] text-bone-faint">NRCS gNATSGO</span>
            </div>
            <SectionCut
              shrinkSwell="Very High"
              drainage="Moderately well drained"
              mapUnit="Lake Charles clay, 0 to 1 percent slopes"
              bedrockCm={null}
              soilUsable
              triggerState="rewetting"
              complaintLabel="311: low pressure"
            />
            <p className="mt-4 text-xs leading-relaxed text-bone-dim border-t border-ground-700 pt-4">
              The complaint is a data point at the surface. The reason for it is 1.5 metres
              down, in clay that has been moving against the pipe all season.
            </p>
          </div>
        </div>
      </section>

      {error && (
        <div className="mx-auto max-w-7xl px-5 sm:px-8 pb-10">
          <div className="rounded border border-oxide/40 bg-oxide/10 px-4 py-3 text-sm text-oxide-bright">
            Can&apos;t reach the Pluvial-AI API ({error}). Start it with{" "}
            <code className="data">uv run python -m pluvial.cli serve</code>.
          </div>
        </div>
      )}

      {/* ── −45cm · what it has read ────────────────────────────────── */}
      {stats && (
        <section className="border-y border-ground-700 bg-ground-850">
          <div className="mx-auto max-w-7xl px-5 sm:px-8 py-12">
            <Depth cm="−45cm" label="What it has read" />
            <div className="grid grid-cols-2 md:grid-cols-4 gap-8 sm:gap-10">
              <Stat
                value={stats.complaints.toLocaleString()}
                label="311 water, sewer and drainage complaints ingested — Jan 2022 to now"
              />
              <Stat
                value={stats.segments_profiled.toLocaleString()}
                label="Street segments profiled through Mireye, cached once and kept"
              />
              <Stat
                value={pct(stats.soil_usable_rate)}
                label={`Only ${stats.soil_usable} of them return usable soil — the rest are Urban land`}
                tone="clay-light"
              />
              <Stat
                value={stats.verdicts.toLocaleString()}
                label={`Decisions on record, ${stats.reawakened} of them re-opened by the system itself`}
                tone="moisture"
              />
            </div>
          </div>
        </section>
      )}

      {/* ── −120cm · the cascade ─────────────────────────────────────── */}
      <section className="mx-auto max-w-7xl px-5 sm:px-8 py-16">
        <Depth cm="−120cm" label="How one complaint is judged" />
        <h2 className="display text-3xl sm:text-4xl text-bone max-w-2xl">
          Four agents, and two of them disagree on purpose.
        </h2>
        <p className="mt-4 max-w-2xl text-[15px] leading-relaxed text-bone-dim">
          No formula combines the signals into a score. The agents argue over thresholded
          facts, and every claim has to carry the field it came from and the survey that
          published it.
        </p>

        {/* Ordered because the order is real: each stage only sees what the
            one before it produced. */}
        <ol className="mt-10 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {[
            {
              n: "01",
              name: "Triage",
              model: "gpt-4o-mini",
              body: "Reads the complaint and the street's dossier. Drops the ones that are plainly nothing, fast-paths the emergencies. Never touches Mireye.",
            },
            {
              n: "02",
              name: "Investigator",
              model: "gpt-4o",
              body: "Builds the case that this is an early symptom — soil movement potential, the moisture trigger, complaint clustering, void formation.",
            },
            {
              n: "03",
              name: "Skeptic",
              model: "gpt-4o",
              body: "Argues the innocent explanation, and holds the honesty gate: on Urban land it vetoes any soil claim outright, however good the story is.",
              accent: true,
            },
            {
              n: "04",
              name: "Adjudicator",
              model: "gpt-4o",
              body: "Rules between them. Writes the disposition, the evidence that decided it, the counter-argument it rejected, and what would re-open the case.",
            },
          ].map((s) => (
            <li
              key={s.n}
              className={`rounded-lg border bg-ground-800 p-5 ${
                s.accent ? "border-moisture/45" : "border-ground-700"
              }`}
            >
              <div className="flex items-baseline gap-3">
                <span className="data text-xs text-bone-faint">{s.n}</span>
                <h3 className="display text-lg text-bone">{s.name}</h3>
              </div>
              <p className="data mt-1 text-[10.5px] text-bone-faint">{s.model}</p>
              <p className="mt-3 text-[13.5px] leading-relaxed text-bone-dim">{s.body}</p>
            </li>
          ))}
        </ol>
      </section>

      {/* ── −190cm · the numbers, honestly ──────────────────────────── */}
      {stats?.eval?.full && (
        <section className="border-t border-ground-700 bg-ground-850">
          <div className="mx-auto max-w-7xl px-5 sm:px-8 py-16">
            <Depth cm="−190cm" label="What it actually scored" />
            <div className="grid lg:grid-cols-[1fr_1.1fr] gap-10 lg:gap-16">
              <div>
                <h2 className="display text-3xl sm:text-4xl text-bone">
                  It is precise, and it misses a lot.
                </h2>
                <p className="mt-4 text-[15px] leading-relaxed text-bone-dim">
                  Backtested against complaints frozen at 15 July 2026, labelled only by
                  what happened in the 30 days after — never shown to the agents. When it
                  flags a street, it is usually right. It also stays quiet on most of the
                  failures it should have caught. Both numbers are below.
                </p>

                <div className="mt-8 grid grid-cols-2 gap-8 max-w-sm">
                  <Stat value={pct(stats.eval.full.precision)} label={`Precision over ${stats.eval.full.n} live cases`} tone="moisture" />
                  <Stat value={pct(stats.eval.full.recall)} label="Recall — the honest weak spot" tone="oxide-bright" />
                </div>

                {stats.eval.negative_control && (
                  <div className="mt-8 rounded-lg border border-ground-700 bg-ground-800 p-5">
                    <p className="eyebrow mb-2">Negative control · New York</p>
                    <p className="text-[13.5px] leading-relaxed text-bone-dim">
                      Run unchanged against {stats.eval.negative_control.n} NYC complaints, where{" "}
                      <span className="data text-bone">
                        {pct(stats.eval.negative_control.n_soil_usable / stats.eval.negative_control.n)}
                      </span>{" "}
                      of points have usable soil. It invented a soil argument{" "}
                      <span className="data text-moisture">
                        {stats.eval.negative_control.n_false_soil_claims} times
                      </span>
                      . The gate held.
                    </p>
                  </div>
                )}
              </div>

              {/* Ablations: the finding that actually surprised us. */}
              <div>
                <p className="eyebrow mb-4">Ablations · same 30 cases, one input removed</p>
                <div className="space-y-3">
                  {[
                    { label: "Full cascade", run: { precision: 0.7777, recall: 0.3181 }, note: "baseline" },
                    { label: "Without moisture", run: stats.eval.no_moisture, note: "no clear loss — an honest null result" },
                    { label: "Without memory", run: stats.eval.no_memory, note: "recall halves; precedent is doing real work", bad: true },
                  ].map((row) => (
                    <div
                      key={row.label}
                      className={`rounded-lg border p-4 ${
                        row.bad ? "border-oxide/45 bg-oxide/[0.07]" : "border-ground-700 bg-ground-800"
                      }`}
                    >
                      <div className="flex items-baseline justify-between gap-4">
                        <span className="text-sm text-bone">{row.label}</span>
                        <span className="data text-sm tabular-nums text-bone-dim">
                          P {pct(row.run?.precision)} · R{" "}
                          <span className={row.bad ? "text-oxide-bright" : "text-bone"}>
                            {pct(row.run?.recall)}
                          </span>
                        </span>
                      </div>
                      <p className="mt-1.5 text-xs text-bone-dim">{row.note}</p>
                    </div>
                  ))}
                </div>
                <p className="mt-4 text-xs leading-relaxed text-bone-faint">
                  Small samples — treat gaps under ten points as noise. Full method and
                  caveats in the eval report.
                </p>
              </div>
            </div>
          </div>
        </section>
      )}

      {/* ── CTA ──────────────────────────────────────────────────────── */}
      <section className="mx-auto max-w-7xl px-5 sm:px-8 py-16 sm:py-20">
        <div className="grid md:grid-cols-2 gap-4">
          <Link
            href="/board"
            className="group rounded-lg border border-ground-700 bg-ground-800 p-7 transition-colors duration-200 hover:bg-ground-750"
          >
            <p className="eyebrow mb-3">For dispatch</p>
            <h3 className="display text-2xl text-bone">Today&apos;s queue</h3>
            <p className="mt-2 text-sm leading-relaxed text-bone-dim">
              Every open case, sorted by what a crew should do about it. Open one to see
              the argument that produced it.
            </p>
          </Link>
          <Link
            href="/lookup"
            className="group rounded-lg border border-ground-700 bg-ground-800 p-7 transition-colors duration-200 hover:bg-ground-750"
          >
            <p className="eyebrow mb-3">For residents</p>
            <h3 className="display text-2xl text-bone">Look up your street</h3>
            <p className="mt-2 text-sm leading-relaxed text-bone-dim">
              Search an address to see the ground underneath it, and whether the city has
              anything on file nearby.
            </p>
          </Link>
        </div>
      </section>

      <footer className="border-t border-ground-700">
        <div className="mx-auto max-w-7xl px-5 sm:px-8 py-8 flex flex-wrap gap-x-8 gap-y-2 text-xs text-bone-faint">
          <span>Soil, drainage and consequence fields via Mireye.</span>
          <span>Complaints via Houston 311 CRIS. Rainfall via NOAA NCEI.</span>
          <span className="data">Built for the Mireye × Delhi University build brief.</span>
        </div>
      </footer>
    </main>
  );
}
