from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "paths": {
        "db": "db/coach.db",
        "fit_archive": "archive/fit",
        "backup_dir": "backup/",
        "fit_import_dir": "",
        "csv_import_dir": "",
    },
    "weather": {
        "provider": "open-meteo",
        "enabled": False,
        "lat": 39.90,
        "lon": 116.40,
        "cache_hours": 3,
        "timeout_s": 10,
    },
    "notify": {
        "channel": "toast",
        "ntfy_topic": "REPLACE_WITH_RANDOM_24CHAR",
        "quiet_hours": ["22:30", "07:00"],
    },
    "feeding": {
        "l1_subjective_days": 2,
        "l2_subjective_days": 4,
        "l3_subjective_days": 7,
        "l1_sync_fail_days": 1,
        "l2_sync_fail_days": 2,
        "weight_gap_days": 10,
    },
    "confirm": {"code_length": 4, "ttl_seconds": 300, "max_attempts": 3},
    "llm": {"model_path": "local://", "context_window": 32768, "temperature": 0.4},
    "mcp": {"transport": "stdio"},
    "load": {
        "k_bridge_default": 2.6,
        "mismatch_tol": 0.40,
        "luteal_mismatch_tol": 0.50,
    },
    "fitness": {
        "tau_ctl": 42,
        "tau_atl": 7,
        "tsb_soft_gate": -100,
        "tsb_hard_gate": -170,
        "chronic_floor_acwr": 200,
        "r1_daily_cap_ratio": 1.3,
    },
    "acwr": {"green": [0.8, 1.3], "freeze_threshold": 1.5},
    "readiness": {
        "weights": {"hrv": 0.25, "tsb": 0.25, "sleep": 0.20, "rhr": 0.15, "subjective": 0.15}
    },
    "gates": {
        "hrv_dev_gate": -15,
        "hrv_gate_persist_days": 2,
        "luteal_gate_persist_days": 3,
        "rhr_dev_gate": 10,
        "rhr_gate_persist_days": 2,
        "sleep_min_for_quality": 5,
        "readiness_easy_floor": 40,
    },
    "cycle": {"enabled": True, "amenorrhea_days": 45, "cycle_anomaly_days": 7},
    "reds": {
        "weight_drop_pct": 5,
        "weight_baseline_window_d": 30,
        "hrv_dev_persist_days": 7,
        "rhr_dev_persist_pct": 8,
        "protein_g_per_kg": [1.8, 2.2],
        "load_reduction_pct": [20, 30],
    },
    "learning": {
        "calibration_n_min": 60,
        "calibration_move_cap_pct": 20,
        "calibration_confirm_months": 2,
        "k_bridge_n_min": 40,
        "k_bridge_r2_min": 0.5,
        "phase_effect_min_cycles": 2,
        "weather_sensitivity_enabled": False,
    },
    "templates": {
        "version": "1.0",
        "block_length_weeks": 4,
        "progression_step": [1.05, 1.08],
        "deload_reduction": [0.30, 0.40],
        "taper_window_days": [7, 10],
        "completion_t2_threshold": 0.90,
        "completion_t1_threshold": 0.75,
        "slot_au_band_pct": 15,
        "slot_rpe_band": 1,
        "t4_soft_budget": [0.70, 0.80],
        "t4_hard_budget": [0.50, 0.60],
        "t6_budget": [0.70, 0.80],
    },
}

def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base

def load_config(path: str = "config.toml") -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path)
    if not p.is_absolute():
        cwd_candidate = (Path.cwd() / p).resolve()
        root_candidate = (Path(__file__).resolve().parent.parent / p).resolve()
        p = cwd_candidate if cwd_candidate.exists() else root_candidate
    if p.exists():
        with p.open("rb") as f:
            data = tomllib.load(f)
        _deep_merge(cfg, data)
    cfg["_root"] = str(p.resolve().parent)
    return cfg

def get(cfg: dict, dotted: str, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node

def resolve(cfg: dict, rel: str) -> Path:
    p = Path(rel)
    if not p.is_absolute():
        p = Path(cfg["_root"]) / p
    return p
