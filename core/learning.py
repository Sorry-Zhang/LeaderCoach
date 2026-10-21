from __future__ import annotations

import json
from datetime import datetime, timezone

def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def regress_k_bridge(pairs: list[tuple[float, float]]) -> dict | None:
    n = len(pairs)
    if n < 2:
        return None
    xy = sum(t * s for t, s in pairs)
    xx = sum(t * t for t, s in pairs)
    if xx <= 0:
        return None
    slope = xy / xx
    mean_s = sum(s for _, s in pairs) / n
    sstot = sum((s - mean_s) ** 2 for _, s in pairs)
    ssres = sum((s - slope * t) ** 2 for t, s in pairs)
    r2 = (1 - ssres / sstot) if sstot > 0 else 0.0
    return {"n": n, "slope": round(slope, 4), "r2": round(r2, 4)}

def gather_pairs(conn, sport_id: str) -> list[tuple[float, float]]:
    rows = conn.execute(
        "SELECT hr_trimp, srpe_au FROM activities "
        "WHERE sport_id=? AND load_path_used='srpe' AND hr_trimp IS NOT NULL "
        "AND srpe_au IS NOT NULL AND hr_trimp > 0",
        (sport_id,),
    ).fetchall()
    return [(float(r["hr_trimp"]), float(r["srpe_au"])) for r in rows]

def k_bridge_for(conn, sport_id: str, cfg: dict) -> tuple[float, str]:
    row = conn.execute(
        "SELECT value FROM learning_state WHERE param_key=?",
        (f"k_bridge@{sport_id}",),
    ).fetchone()
    if row:
        return float(row["value"]), "calibration"
    return float(cfg["load"]["k_bridge_default"]), "default"

def update_k_bridge(conn, sport_id: str, cfg: dict) -> dict:
    pairs = gather_pairs(conn, sport_id)
    reg = regress_k_bridge(pairs)
    lc = cfg["learning"]
    if reg is None:
        return {"sport_id": sport_id, "updated": False, "reason": "配对点不足"}
    if reg["n"] < lc["k_bridge_n_min"] or reg["r2"] < lc["k_bridge_r2_min"]:
        return {
            "sport_id": sport_id, "updated": False,
            "reason": f"护栏拦截：n={reg['n']}<{lc['k_bridge_n_min']} 或 R²={reg['r2']}<{lc['k_bridge_r2_min']}",
            "regression": reg,
        }
    evidence = json.dumps({"n": reg["n"], "r2": reg["r2"], "moved_to": reg["slope"]}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO learning_state(param_key, scope, value, source, evidence, updated_at) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(param_key) DO UPDATE SET value=excluded.value, evidence=excluded.evidence, updated_at=excluded.updated_at",
        (f"k_bridge@{sport_id}", sport_id, str(reg["slope"]), "calibration", evidence, _now()),
    )
    conn.commit()
    return {"sport_id": sport_id, "updated": True, "regression": reg}

def learning_status(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM learning_state ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]
