# Pluvial-AI — Eval Report (2026-08-24)

Real numbers from the first live runs of the full agent cascade (Triage → Investigator → Skeptic → Adjudicator) against real Houston 311 complaints, real OpenAI models, and a real Mireye account. All runs are cache-only (`run_budget_ceiling=0`): every physical fact comes from the 990-segment bulk profile, never a live fetch mid-eval, so these numbers reflect the cascade's reasoning, not Mireye availability.

**Frozen at:** `2026-07-15`, label window 30 days (labels computed only from complaints strictly after `created_at`, never fed to the agents — see design spec §8 and `eval/backtest.py`).

## Headline backtest (n=50)

| | |
|---|---|
| Precision | **78.6%** (11/14 flagged cases were real escalations) |
| Recall | **30.6%** (11/36 real escalations were caught) |
| TP / FP / FN / TN | 11 / 3 / 25 / 11 |

Zero runtime errors across all 50 cases — the SQLite cross-thread bug (see below) was fixed before this run.

**Read on recall:** at 30.6%, the cascade is deliberately conservative — it flags a minority of eventual escalations. Given the disposition set (`dispatch`/`inspect` = flagged vs. `monitor`/`close`/`discard` = not), this reads as a system tuned toward precision over recall at its current guidance version, consistent with an early, uncalibrated pass (before any Calibrator run has adjusted thresholds).

## NYC negative control (n=10 profiled)

| | |
|---|---|
| Soil-usable rate | **0%** (Houston bulk sample: ~20.6%; design research predicted ~14–25%) |
| False soil claims | **0 / 10** |

The `soil_usable` honesty gate held completely — no case argued from soil dynamics on ground SSURGO can't actually speak to. This is the test the design spec cared about most: an agent that quietly ignores its own gate would make the physical signal theatre. It didn't.

## Ablations (n=30, same matched case subset as the first 30 of the headline backtest)

| Variant | Precision | Recall | FP |
|---|---|---|---|
| Full cascade (matched subset) | 77.8% | 31.8% | 2 |
| `no_moisture` | 87.5% | 31.8% | 1 |
| `no_memory` | **60.0%** | **13.6%** | 2 |

- **Moisture's contribution to precision is not clearly positive** on this sample — removing it didn't hurt precision (if anything, one fewer false positive). n=30 is too small to call this definitive either way; it's a real, honest result, not a flattering one.
- **Memory/precedent access matters a lot.** Recall roughly halves without it (31.8% → 13.6%), and precision drops too (77.8% → 60.0%). This is the strongest signal in this eval round: prior verdicts and precedent search are doing real work, not decoration.

## Caveats

- n=50 (headline) / n=30 (ablations) / n=10 (negative control) are small samples for a first live pass — read directional, not final. Confidence intervals aren't reported here; treat differences under ~10 percentage points as noise.
- All cases are drawn only from the 990 segments profiled in the bulk job (§9 budget), which are weighted toward high-complaint-count street segments — not a random sample of the study area.
- No Calibrator run has touched these verdicts yet, so this reflects the cascade's zero-shot guidance, not a calibrated system.

## Raw results

- `data/backtest_full_2026-07-15.json`
- `data/backtest_no_moisture_2026-07-15.json`
- `data/backtest_no_memory_2026-07-15.json`
- `data/negative_control_nyc.json`
