# LeaderCoach

[![English](https://img.shields.io/badge/English-current-blue)](README.en.md)
[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-switch-red)](README.md)

A local-first multi-sport training engine. Garmin data is imported into a local
database; deterministic code computes load, recovery and today's training forecast;
the engine returns today's session. The LLM only performs adjustment and explanation —
it does not take part in runtime computation.

> Terminology: **training forecast** (`training_forecast`, items G1-G10) is the set of
> situations that may call for a change today, not a safety review; **logging** covers daily
> subjective state and how consistently it is recorded; **training plan** replaces the earlier
> "prescription" wording. The database column `gates_triggered` is a legacy name whose meaning is
> the training forecast; it is kept only for compatibility with older rows.

![Training load vs HRV deviation](assets/load_vs_hrv.png)

> Single-subject real data. The x-axis is a relative day index and the y-axis is
> normalized; dates and absolute values are hidden.

## How the data capability evolved

Collection was opened in stages, not all at once:

- The first version read only activity summaries and a few daily metrics (sleep duration, HRV,
  resting HR, average stress) so the data -> engine -> session loop could be validated with a
  small task load.
- As the storage layer, parsers and validation grew, collection expanded to every read-only plugin
  tool and added the sleep-stage timeline, overnight series, activity splits and official training
  metrics.
- Early notes in `docs/` therefore say "only activities" or "0 days of sleep"; that was true then.
  The data table in this README and `store_ext.stats()` are the current source of truth.

## Architecture

| Layer | Location | Role |
|---|---|---|
| Acquisition | `ingest/` | Garmin plugin MCP, FIT/CSV parsing, weather |
| Observation | `ml/features.py`, `core/daily.py` | Daily panel, HRV deviation, CTL/ATL/TSB, ACWR |
| Models | `ml/` | Ridge / GBM, walk-forward; research prototype |
| Control | `core/engine.py`, `core/templates.py` | G1–G10 gates, T1–T6 templates |
| Explanation | `coach/` | LLM performs adjustment and explanation only |

## Data

- Activities: Garmin FIT / CSV;
- Daily physiology: HRV, sleep, resting heart rate, stress;
- Labels: daily subjective state + post-session RPE/Feel;
- Weather: Open-Meteo (optional).

### Full Garmin plugin collection (v1.8)

`python main.py sync` and the MCP `sync_garmin` tool now default to
**`ingest/plugin_full.py`**, which calls every read-only plugin tool and archives the
raw responses (gzip) under `archive/plugin_raw/`:

| Category | Tool | Stored in |
| --- | --- | --- |
| Activities | `get_garmin_activities` (full detail) | `activities` (HR zones, training effect, official load, gait) |
| Sleep | `get_garmin_sleep` | `daily_metrics` (duration, stages, sleep start/end) |
| Sleep stages | SDK `getSleepData` via `ingest/_sdk_sleep_probe.js` | `sleep_stages` timeline + `sleep_series` (night HR/stress/body battery/HRV/respiration) |
| HRV | `get_garmin_hrv` / `_summaries` | `hrv_readings` (5-min readings) + `garmin_metrics` |
| Stress / body battery | `get_garmin_stress` / `_summaries` / `get_garmin_body_battery` | `stress_series`, `body_battery_series` |
| Training | readiness / training status / load balance / max metrics / race predictions | `garmin_metrics` + `official_snapshot` |
| Splits | local FIT laps / 1-km splits | `activity_splits` |

- Raw payloads are stored per `tool/date.json.gz` (the same key is refreshed on later runs);
  replay them with `python -m ingest.plugin_full --replay` after parser changes.
- Backfill historically in month-sized chunks (one call may span at most 31 days):
  `python -m ingest.plugin_full --start 2026-06-01 --end 2026-06-30`.
- Garmin official metrics are an isolated reference layer and never feed the computed readiness.
- The previous minimal path remains available: `python main.py sync --legacy`
  or `sync_garmin(full=False)`.

### Availability depends on device settings (measured on this account)

| Data | Measured coverage | Note |
| --- | --- | --- |
| Sleep summary + sleep window | from 2026-03-11 | earlier responses carry no stage timeline |
| Sleep-stage timeline + night series | **from 2026-06-01** | SDK returns `sleepLevels` / HR / stress series only for recent dates |
| HRV 5-min readings | from ~June, 125/149 days | some nights return an empty array |
| Stress / body-battery series | **from 2026-06-01** | earlier days carry daily averages only |
| Training status / load balance / race predictions | from 2026-03-11 | |
| Full activity detail | latest ~100 activities | the plugin does not filter activities by date, so backfill cannot reach older ones |

### Subjective input and the "default fill" convention

- `log_subjective`'s `sleep_feel` (1-5) keeps its own weight as the subjective term.
- **A derived default may be stored** when the user does not dispute the predicted
  awake state: fill it from the sleep engine's prediction (duration, regularity,
  efficiency vs. the athlete's own reference) and mark `note` with `source: derived`.
  When the user objects, overwrite it and keep the predicted value for later comparison.
- Personal sleep need defaults to **7.5 h** in the athlete profile and is meant to be set
  per athlete at onboarding; see the sleep-state section above.
- This prevents the subjective pillar from silently disappearing on unfilled days, but the
  subjective term remains a correction term: it never replaces objective sleep metrics and is
  never the sole basis for a training gate.

### Sleep-state score (v1.9)

The sleep module does not predict sleep. It turns last night into **how recovered you are today**,
which then feeds the day's plan. Implementation: `core/sleep_health.py`; results are stored in
`garmin_metrics` (`sleep_state_score` plus per-dimension values).

Six dimensions (0-100, weighted):

| Dimension | Basis | Weight |
| --- | --- | --- |
| duration | deviation from the athlete's **personal sleep need** + 7-day sleep debt | 0.25 |
| regularity | Sleep Regularity Index across adjacent nights (5-min epochs); midpoint SD fallback | 0.25 |
| timing | sleep midpoint vs the personal 28-day median | 0.10 |
| efficiency | total sleep / time in bed | 0.15 |
| stability | WASO, awakenings, stage transitions per hour | 0.15 |
| subjective | `sleep_feel` 1-5; derived from objective terms when missing (marked `derived`) | 0.10 |

**Personal sleep need is a fixed quantity set at onboarding**: `athlete_profile.sleep_need_h`
(currently **7.5 h** for this athlete). Every athlete sets their own value; the
`sleep.personal_need_h` config entry is only a fallback. The AASM 7-9 h range is used as a lower
reference bound and never overrides the personal value.

Wiring:

- The sleep pillar uses `sleep_state_score` instead of the old absolute-hours interpolation
  (falls back automatically when the score is unavailable).
- Observation result (149 days, June-October): **11 days** met the `min(p10, 60)` condition,
  0 days were enforced (`sleep_g8_enforced` stayed 0). Those days become the candidate sample for
  later validation.
- Training forecast item G8 defaults to **observation mode** (`sleep.g8_mode = "observe"`):
  it triggers when `sleep score < min(personal 28-day p10, absolute floor 60)`, and only records
  `sleep_g8_triggered` plus the threshold in `garmin_metrics` and the brief. It does **not**
  change the day's plan. Set `g8_mode = "enforce"` to actually remove the quality session; without
  a score it falls back to the absolute `sleep_hours_floor`. Observation is the default because
  the score shows almost no predictive power for same-day performance here (see below), so the
  project first collects evidence on which days enforcement would have hit.
- Stage breakdown (deep/light/REM) only informs night stability and explanations. It never
  triggers an adjustment on its own, because wrist-worn staging error is substantial.

### Current validation (descriptive, not causal)

149 days between June and October have a sleep score (mean 79.1, SD 10.7):

| Comparison | r | n |
| --- | --- | --- |
| Same-day activity RPE | 0.09 | 130 |
| Same-day Feel | -0.12 | 128 |
| Run speed per heart beat | -0.09 | 175 |
| Next-day HRV | 0.05 | 124 |

**In this dataset the sleep score has essentially no linear predictive power for same-day
perceived exertion or performance.** This matches the project's earlier negative results (training
load adds ~0 incremental R^2 for sleep) and the literature showing weak sleep-HRV coupling. The
score is therefore used only as a conservative exclusion rule (no quality work after a very poor
night), never as a positive reason to add load. Validating it properly needs performance anchors
such as heart-rate drift at fixed pace or time trials.

## Privacy

This repository contains **framework code only**, with no personal data:

- Not included: FIT files, CSV exports, databases, backups, athlete profile,
  race results, dates, or physiological values;
- The figure uses single-subject real data, but dates and absolute values are hidden;
- Full data can be shared separately after contacting the author.

## Results

Data through **2026-10-20**, **n=175**.  
If numbers differ later, the latest `python -m ml.transition` output prevails.

| Model | MAE | R² |
|---|---|---|
| Ridge | 13.711 | −0.248 |
| GBM | 15.316 | −0.454 |

- Classification baseline: macro-F1 ≈ 0.29–0.31 (chance level);
- Incremental R² of long-window exposure for subsequent sleep ≈ 0;
- Conclusion: the current models **must not be used for decisions**. The
  bottleneck is causal identification, not model capacity.

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-ml.txt

# 1) Install the Garmin plugin (MIT, not bundled with this repo)
npm i -g dsh-plugin-garmin-connect

# 2) Point to the plugin via environment variables (never hard-code paths)
$env:GARMIN_PLUGIN_MCP_JS  = "<plugin-dir>\lib\mcp.js"
$env:GARMIN_PLUGIN_SESSION = "<plugin-session-file>"
$env:GARMIN_PLUGIN_NODE    = "node"

# 3) Initialize, sync, and get today's session
python main.py init
python main.py sync --days 60
python main.py today
```

## Layout

```
core/       Engine: load, recovery, gates, template selection
ingest/     Acquisition: plugin bridge, FIT/CSV, weather
ml/         Models: features, baselines, transition, exposure, anchors
templates/  T1–T6 templates
db/         schema.sql / seed.sql
mcp_server/ MCP tools
coach/      Prompt templates
tests/      Engine and schema tests
```

## Changelog

- 2026-10-27 (v1.10): G8 moved to observation mode - triggers when
  `sleep score < min(personal 28-day p10, 60)`, records only, never changes the plan;
  `sleep.g8_mode="enforce"` enables the actual block.
- 2026-10-27 (v1.9): sleep-state score (`core/sleep_health.py`), personal sleep need (7.5 h,
  set per athlete), sleep pillar and G8 driven by the score; terminology pass (training
  forecast / logging / training plan).
- 2026-10-27 (v1.8): full read-only plugin collection by default (raw gzip archive, sleep-stage timeline, HRV/stress/body-battery series, activity splits); `core/store_ext.py` extension tables; new `get_sleep_detail` tool.

- 2026-10-23: added `sync_garmin` (sync inside MCP); single-day HRV drop gates G4b/G4c with a user-chosen action; dual baseline (rolling-14 + Garmin reference).
- 2026-10-21: first public release.

## License

- Project code: MIT, see `LICENSE`;
- Third-party components and references: see `THIRD_PARTY_NOTICES.md`;
- Raw Garmin data is not distributed with this repository; users collect their own.
