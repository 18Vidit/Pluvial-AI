# Address-mode evaluation, 2026-08-25

Run: `pluvial backtest --frozen-at 2026-07-15 --mode address --rescore
data/backtest_full_2026-07-15.json`
Full per-case results: `data/backtest_rescore_address_2026-07-15.json`

## Why this number exists

The shipped product no longer sees 311 complaints, so the published
78.6% precision / 30.6% recall does not describe it. That number scores
whether the cascade correctly flags *311 complaints that later escalate*,
using complaint clustering as evidence — evidence address mode does not
have.

This run asks the address-mode question against the same ground and the
same labels: given only the physics under a segment, no complaint text and
no clustering, does the `service_lines` ruling come back `high` or
`elevated` for the segments where a complaint went on to escalate? It runs
on the identical 50 pinned cases via `--rescore`, so the two are measured
over the same corpus.

## Result

| | n | precision | recall |
|---|---|---|---|
| Triage mode (published) | 50 | 78.6% | 30.6% |
| **Address mode, pooled** | **50** | **44.4%** | **11.1%** |
| Address mode, soil-usable cases only | 12 | 44.4% | 66.7% |
| Address mode, Urban-land cases only | 38 | — | 0.0% |

Severity distribution: 0 `high`, 9 `elevated`, 3 `low`, **38 `unresolved`**.

## The pooled number is mostly a coverage measurement

76% of the pinned cases (38/50) sit on segments whose dominant SSURGO
component is `Urban land`. Every one of them returned `unresolved`, and 30
of those 38 were true failures. That single fact accounts for almost all of
the recall collapse: address mode is not getting these cases wrong, it is
correctly reporting that no soil answer exists at them, and the scoring
counts a refusal as a miss because the label says something failed.

So the pooled 11.1% is best read as **"SSURGO covers a quarter of the
Houston segments people complain about"**, not as "the agents reason
poorly". On the 12 cases where the ground was actually readable, recall is
66.7% — more than double the triage-mode figure — on 6 positives, which is
far too small a sample to claim anything from.

Reporting the pooled number alone would understate the reasoning; reporting
only the soil-usable stratum would hide a real product boundary. The
harness now emits both unconditionally (`by_soil_usable`), so neither can
be quoted without the other.

## What the gap does and does not show

The design predicted the address-mode number would be lower and treated the
gap as a finding: how much complaint evidence contributes on top of ground
physics. The gap is real, but this run shows it is **not primarily an
evidence gap** — it is a coverage gap. A 311 complaint exists at a
location whether or not SSURGO mapped the soil there, so the triage
cascade has something to argue from on all 50 cases and address mode has
something on 12.

The honest statement for the write-up is: *on Houston's 311 corpus,
address mode can only speak to about a quarter of the segments, and where
it can speak it finds more of the failures than the complaint-driven
cascade does — on a sample too small to lean on.*

## Caveats, carried forward

- **Sample size.** 50 cases, 36 positives pooled, 6 positives in the
  soil-usable stratum. No confidence interval on any of these would be
  narrow enough to act on.
- **Houston is the worst case for coverage, and this corpus is Houston.**
  The product's target user is suburban and rural, where the soil-usable
  rate is far higher than the 20.5% measured across profiled Houston
  segments. This evaluation therefore measures address mode on the terrain
  it is least suited to, because that is where labels exist.
- **The labels are still escalation/recurrence proxies**, not utility
  repair records, and they are about a buried-line failure — which is why
  `service_lines` is the scored threat and `foundation`/`subsidence` are
  not scored at all. There is no ground truth here for those two.
- **The unexplained triage-mode eval shift is still unresolved.** The
  re-score of the pinned 50 gave 75.0% / 16.7% against the published
  78.6% / 30.6%, with `inspect` 14→8; labels and Mireye profiles were
  verified byte-identical and the contamination hypothesis was tested and
  disproved. The comparison table above uses the published triage numbers
  and inherits that uncertainty.
- **Temporal isolation is better here than in triage mode.** Address mode
  has no `dossier_lookup`, so the known `frozen_at` leak in that tool does
  not apply, and `frozen_at` now also reaches `moisture_history` and
  `precedent_search`. The triage path still has the leak.
