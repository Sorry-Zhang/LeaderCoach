"""Garmin 插件的**全量只读采集**（v1.8）。

与 ingest/plugin_bridge.py 的分工：
  plugin_bridge  旧路径：只取 8 个日指标 + 活动摘要（保留兼容，配置 full_sync=false 时使用）
  plugin_full    全量路径：调用插件全部只读工具，原始返回 gzip 存档，并拆成结构化表

覆盖的插件工具（23 个中除 create_garmin_workout 外的全部只读工具）：
  区间：get_garmin_activities / get_garmin_sleep / get_garmin_steps / get_garmin_heart_rate
        / get_garmin_weight / get_garmin_hrv_summaries / get_garmin_stress_summaries
        / get_garmin_body_battery / get_garmin_max_metrics_range / get_garmin_race_predictions_range
  逐日：get_garmin_hrv / get_garmin_stress / get_garmin_training_readiness
        / get_garmin_daily_training_status / get_garmin_training_status
        / get_garmin_training_load_balance / get_garmin_max_metrics
  其它：get_garmin_workouts / get_garmin_profile / get_garmin_race_predictions
        / download_garmin_activity_fit（可选）

注意：内置 SDK 的 getSleep 不返回睡眠阶段时间线。如果账号 session 是 DI v2 格式，
本模块会自动尝试 SDK 直读 dailySleepDTO 的 sleepLevels（可选增强，实测为空即回退）。

    python -m ingest.plugin_full --start 2026-10-20 --end 2026-10-27
    python -m ingest.plugin_full --days 30 --download-fit
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core import db as dbmod
from core import store_ext
from core.config import load_config, resolve, get as cfg_get

PLUGIN_CFG = load_config().get("garmin_plugin", {})

DEFAULTS = {
    "node": os.environ.get("GARMIN_PLUGIN_NODE") or PLUGIN_CFG.get("node", "node"),
    "mcp_js": os.environ.get("GARMIN_PLUGIN_MCP_JS") or PLUGIN_CFG.get("mcp_js", ""),
    "session": os.environ.get("GARMIN_PLUGIN_SESSION") or PLUGIN_CFG.get("session", ""),
    "username": os.environ.get("GARMIN_USERNAME") or PLUGIN_CFG.get("username", ""),
    "region": os.environ.get("GARMIN_REGION") or PLUGIN_CFG.get("region", "cn"),
    "account": os.environ.get("GARMIN_ACCOUNT") or PLUGIN_CFG.get("account", "personal-cn"),
    "fit_dir": os.environ.get("GARMIN_FIT_DIR") or PLUGIN_CFG.get("fit_dir", "data/garmin_fit"),
}

# 插件里真正只读、可以全量拉取的工具清单（create_garmin_workout 除外）
DAILY_TOOLS = (
    "get_garmin_hrv",
    "get_garmin_stress",
    "get_garmin_training_readiness",
    "get_garmin_daily_training_status",
    "get_garmin_training_status",
    "get_garmin_training_load_balance",
    "get_garmin_max_metrics",
)
LATEST_TOOLS = ("get_garmin_race_predictions",)
SERIES_RANGE_TOOLS = (
    "get_garmin_hrv_summaries",
    "get_garmin_stress_summaries",
    "get_garmin_body_battery",
    "get_garmin_max_metrics_range",
    "get_garmin_race_predictions_range",
)

# 插件最长 30 天的区间限制（dateRangeSchema: maximum 30 days）
_CHUNK = 30


def _require_plugin_config() -> None:
    missing = [k for k in ("mcp_js", "session") if not DEFAULTS[k]]
    if missing:
        raise SystemExit(
            "缺少 Garmin 插件配置。请在 config.toml 的 [garmin_plugin] 填 mcp_js / session，"
            "或设置环境变量 GARMIN_PLUGIN_MCP_JS / GARMIN_PLUGIN_SESSION。")


def _chunks(start: str, end: str, days: int = _CHUNK):
    cur = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    while cur <= end_d:
        nxt = min(cur + timedelta(days=days - 1), end_d)
        yield cur.isoformat(), nxt.isoformat()
        cur = nxt + timedelta(days=1)


def _dates(start: str, end: str):
    cur = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    while cur <= end_d:
        yield cur.isoformat()
        cur += timedelta(days=1)


async def _call(session, name: str, args: dict):
    res = await session.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in (res.content or []))
    if getattr(res, "isError", False):
        raise RuntimeError(f"{name}: {text[:400]}")
    return json.loads(text) if text.strip() else None


async def _safe_call(session, name: str, args: dict, retries: int = 1):
    last = None
    for i in range(retries + 1):
        try:
            await asyncio.sleep(0.25)
            return await _call(session, name, args)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                await asyncio.sleep(1.5)
    print(f"[warn] {name} {args} -> {last}", file=sys.stderr)
    return None


# ── 字段提取：插件返回 → 结构化指标 ─────────────────────────

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN 过滤


def _dto(payload):
    """SDK 健康指标多数包在 {startDate, endDate, ..., <dtoList>} 信封里。"""
    if not isinstance(payload, dict):
        return payload
    if "dailySleepDTO" in payload:
        return payload["dailySleepDTO"]
    if "stressValuesArray" in payload or "bodyBatteryValuesArray" in payload:
        return payload
    for key in ("dtoList", "trainingReadiness", "trainingStatusList", "loadBalanceList",
                "maxMetricsList", "records", "data"):
        if isinstance(payload.get(key), list) and payload[key]:
            return payload[key][0]
    return payload


def _wall_local(ms) -> str | None:
    """把 Garmin 的 *TimestampLocal（墙上时间以 UTC 毫秒存储）还原成带本地时区标注的 ISO 字符串。"""
    n = _num(ms)
    if n is None:
        return None
    wall = datetime.fromtimestamp(n / 1000, timezone.utc)
    return wall.replace(tzinfo=datetime.now().astimezone().tzinfo).isoformat(timespec="seconds")


def _metric_rows_from_sleep(dto: dict) -> dict:
    """日均值列：接口偶发返回分钟而非秒，这里做单位归一并换算成小时。"""
    def hours(sec):
        s = _num(sec)
        if s is None:
            return None
        if s > 48 * 3600:   # 明显是分钟
            s = s * 60
        return round(s / 3600, 2)

    def minutes(sec):
        s = _num(sec)
        if s is None:
            return None
        if s > 48 * 3600:
            return s
        return round(s / 60, 1)

    return {
        "sleep_hours": hours(dto.get("sleepTimeSeconds")),
        "sleep_deep_h": hours(dto.get("deepSleepSeconds")),
        "sleep_light_h": hours(dto.get("lightSleepSeconds")),
        "sleep_rem_h": hours(dto.get("remSleepSeconds")),
        "sleep_awake_h": hours(dto.get("awakeSleepSeconds")),
        "sleep_awake_min": minutes(dto.get("awakeSleepSeconds")),
        "sleep_start": _wall_local(dto.get("sleepStartTimestampLocal")),
        "sleep_end": _wall_local(dto.get("sleepEndTimestampLocal")),
        "sleep_validation": dto.get("sleepValidation"),
        "nap_h": hours(dto.get("napTimeSeconds") or dto.get("napTimeSecs")),
    }


def _readiness_metrics(dto: dict) -> dict:
    return {
        "readiness_score": _num(dto.get("score")),
        "readiness_level": dto.get("level"),
        "recovery_time_h": _num(dto.get("recoveryTime")),
        "hrv_factor_pct": _num(dto.get("hrvFactorPercent")),
        "sleep_score_factor_pct": _num(dto.get("sleepScoreFactorPercent")),
        "recovery_time_factor_pct": _num(dto.get("recoveryTimeFactorPercent")),
        "acwr_factor_pct": _num(dto.get("acwrFactorPercent")),
        "stress_history_factor_pct": _num(dto.get("stressHistoryFactorPercent")),
        "acute_load": _num(dto.get("acuteLoad")),
        "sleep_history_factor_pct": _num(dto.get("sleepHistoryFactorPercent")),
        "sleep_awake_min_dto": _num(dto.get("sleepAwakeTime")),
    }


def _training_status_metrics(payload) -> dict:
    """get_garmin_daily_training_status / get_garmin_training_status 的真实结构：
    {latestTrainingStatusData: {deviceId: {...}}, recordedDevices: [...]}"""
    if not isinstance(payload, dict):
        return {}
    latest = payload.get("latestTrainingStatusData")
    dto = None
    if isinstance(latest, dict) and latest:
        dto = next((v for v in latest.values() if isinstance(v, dict)), None)
    if dto is None:
        dto = _dto(payload)
    if not isinstance(dto, dict):
        return {}
    acute = dto.get("acuteTrainingLoadDTO") or {}
    chronic = _num(acute.get("dailyTrainingLoadChronic"))
    coef = (_num(acute.get("maxTrainingLoadChronic")) or 0) - (_num(acute.get("minTrainingLoadChronic")) or 0)
    coef = coef / 2 / chronic if chronic else None
    out = {
        "garmin_training_status": _num(dto.get("trainingStatus")),
        "garmin_training_status_phrase": dto.get("trainingStatusFeedbackPhrase"),
        "garmin_fitness_trend": _num(dto.get("fitnessTrend")),
        "garmin_sport": dto.get("sport"),
        "garmin_weekly_load": _num(dto.get("weeklyTrainingLoad")),
        "garmin_load_acute_7d": _num(acute.get("dailyTrainingLoadAcute")),
        "garmin_load_chronic": chronic,
        "garmin_acwr": _num(acute.get("dailyAcuteChronicWorkloadRatio")),
        "garmin_acwr_status": acute.get("acwrStatus"),
        "garmin_acwr_percent": _num(acute.get("acwrPercent")),
        "garmin_load_tunnel_min": _num(dto.get("loadTunnelMin")),
        "garmin_load_tunnel_max": _num(dto.get("loadTunnelMax")),
    }
    if coef:
        out["garmin_load_chronic_44d"] = round(chronic * coef, 1)
    return {k: v for k, v in out.items() if v is not None}


def _load_balance_metrics(payload) -> dict:
    """{metricsTrainingLoadBalanceDTOMap: {deviceId: {monthlyLoad*}}}"""
    if not isinstance(payload, dict):
        return {}
    m = payload.get("metricsTrainingLoadBalanceDTOMap")
    dto = next((v for v in m.values() if isinstance(v, dict)), None) if isinstance(m, dict) else None
    if dto is None:
        dto = _dto(payload)
    if not isinstance(dto, dict):
        return {}
    out = {
        "load_balance_aerobic_low": _num(dto.get("monthlyLoadAerobicLow")),
        "load_balance_aerobic_high": _num(dto.get("monthlyLoadAerobicHigh")),
        "load_balance_anaerobic": _num(dto.get("monthlyLoadAnaerobic")),
        "load_balance_target_aerobic_low_min": _num(dto.get("monthlyLoadAerobicLowTargetMin")),
        "load_balance_target_aerobic_low_max": _num(dto.get("monthlyLoadAerobicLowTargetMax")),
        "load_balance_target_aerobic_high_min": _num(dto.get("monthlyLoadAerobicHighTargetMin")),
        "load_balance_target_aerobic_high_max": _num(dto.get("monthlyLoadAerobicHighTargetMax")),
        "load_balance_target_anaerobic_min": _num(dto.get("monthlyLoadAnaerobicTargetMin")),
        "load_balance_target_anaerobic_max": _num(dto.get("monthlyLoadAnaerobicTargetMax")),
    }
    return {k: v for k, v in out.items() if v is not None}


def _max_metrics(payload) -> dict:
    dto = _dto(payload)
    if not isinstance(dto, dict):
        return {}
    return {k: _num(dto.get(k)) for k in ("vo2MaxPreciseValue", "vo2MaxValue", "fitnessAge")
            if _num(dto.get(k)) is not None}


def _battery_metrics(payload) -> dict:
    """get_garmin_body_battery 是每日对象列表：charged/drained + 时间序列（与 get_garmin_stress 同格式）。"""
    rows = payload if isinstance(payload, list) else [payload]
    out = {}
    lows, highs = [], []
    for x in rows:
        if not isinstance(x, dict):
            continue
        levels = [_num(r[1]) for r in (x.get("bodyBatteryValuesArray") or [])
                  if isinstance(r, list) and len(r) > 1 and _num(r[1]) is not None]
        if levels:
            lows.append(min(levels))
            highs.append(max(levels))
        out["body_battery_charged"] = out.get("body_battery_charged", []) + [_num(x.get("charged"))]
        out["body_battery_drained"] = out.get("body_battery_drained", []) + [_num(x.get("drained"))]
    clean = {}
    if lows:
        clean["body_battery_min"] = min(lows)
        clean["body_battery_max"] = max(highs)
    ch = [v for v in out.get("body_battery_charged", []) if v is not None]
    dr = [v for v in out.get("body_battery_drained", []) if v is not None]
    if ch:
        clean["body_battery_charged_sum"] = sum(ch)
    if dr:
        clean["body_battery_drained_sum"] = sum(dr)
    return clean


def _filled_sleep(raw: list) -> set:
    return {r["date"] for r in raw if r.get("tool") == "sdk_getSleep" and r.get("written")}


def _race_predictions(payload) -> dict:
    dto = _dto(payload)
    if not isinstance(dto, dict):
        return {}
    out = {}
    for key, label in (("time5K", "5k"), ("time10K", "10k"), ("timeHalfMarathon", "half"),
                       ("timeMarathon", "marathon")):
        v = _num(dto.get(key))
        if v:
            out[f"race_pred_{label}_min"] = round(v / 60, 1)
    return out


# ── 写库 ────────────────────────────────────────────────────

def _upsert_daily(conn, day: str, values: dict) -> None:
    """只把非空字段写入 daily_metrics（与 health_fit_parser 的 write_health_rows 同规则）。"""
    cols = ("sleep_hours", "sleep_deep_h", "sleep_light_h", "sleep_rem_h", "sleep_awake_h",
            "sleep_start", "sleep_end", "hrv_last_night", "hrv_baseline", "resting_hr",
            "stress_daily_avg")
    row = {k: values.get(k) for k in cols if values.get(k) is not None}
    if not row:
        return
    existing = conn.execute("SELECT * FROM daily_metrics WHERE date=?", (day,)).fetchone()
    merged = {k: existing[k] for k in existing.keys()} if existing else {}
    merged.pop("date", None)
    merged.update(row)
    keys = list(merged)
    conn.execute(
        f"INSERT INTO daily_metrics(date,{','.join(keys)}) "
        f"VALUES({','.join('?' for _ in range(len(keys) + 1))}) "
        "ON CONFLICT(date) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in keys),
        [day] + [merged[k] for k in keys])


def _extract_activity(act: dict) -> dict:
    """插件活动（full） → activities 表字段（只映射 v1.7 schema 里已有的列）。"""
    zones = {}
    for i in range(1, 6):
        v = _num(act.get(f"hrTimeInZone_{i}"))
        if v is not None:
            zones[f"b{i}"] = v
    start = act.get("startTimeLocal") or act.get("startTime")
    dur_s = _num(act.get("duration"))
    dist_m = _num(act.get("distance"))
    return {
        "garmin_activity_id": f"plugin:{act.get('activityId')}" if act.get("activityId") else None,
        "start_time": start,
        "sport_key": (act.get("activityType") or {}).get("typeKey") if isinstance(act.get("activityType"), dict) else act.get("type"),
        "duration_min": round(dur_s / 60.0, 2) if dur_s else None,
        "distance_km": round(dist_m / 1000.0, 3) if dist_m else None,
        "avg_hr": _num(act.get("averageHR")),
        "max_hr": _num(act.get("maxHR")),
        "hr_zones_pct": zones or None,
        "avg_pace_min_km": _num(act.get("averagePaceMinPerKm")),
        "elevation_gain_m": _num(act.get("elevationGain")),
        "power_avg_w": _num(act.get("avgPower")),
        "power_max_w": _num(act.get("maxPower")),
        "te_aerobic": _num(act.get("aerobicTrainingEffect")),
        "te_anaerobic": _num(act.get("anaerobicTrainingEffect")),
        "official_load": _num(act.get("activityTrainingLoad") or act.get("trainingStressScore")),
        "indoor": bool(act.get("indoor")) if act.get("indoor") is not None else None,
        "dynamics": {k: act.get(k) for k in (
            "avgGroundContactTime", "avgVerticalOscillation", "avgStrideLength",
            "averageRunningCadenceInStepsPerMinute") if act.get(k) is not None} or None,
        "name": act.get("activityName"),
        "lap_count": act.get("lapCount"),
        "has_splits": act.get("hasSplits"),
    }

_SPORT_MAP = {
    "running": "run", "trail_running": "other", "walking": "other", "hiking": "other",
    "cycling": "other", "road_biking": "other",
    "climbing": "climb", "bouldering": "climb", "indoor_climbing": "climb",
    "table_tennis": "table_tennis",
    "strength_training": "strength",
}


def _upsert_activities(conn, cfg: dict, acts: list) -> int:
    valid = {r[0] for r in conn.execute("SELECT sport_id FROM sport_registry")}
    n = 0
    touched_days = set()
    for raw in acts:
        a = _extract_activity(raw)
        if not a["start_time"] or not a["duration_min"] or not a["garmin_activity_id"]:
            continue
        touched_days.add(str(a["start_time"])[:10])
        sport = _SPORT_MAP.get((a["sport_key"] or "").lower(), "other")
        if sport not in valid:
            sport = "other"
        hit = conn.execute(
            "SELECT id FROM activities WHERE garmin_activity_id=?",
            (a["garmin_activity_id"],)).fetchone()
        if hit:
            # 只补齐插件才有的字段（COALESCE），不覆盖用户已录的 RPE / 负荷
            conn.execute(
                "UPDATE activities SET hr_zones_pct=COALESCE(hr_zones_pct, ?), "
                "te_aerobic=COALESCE(te_aerobic, ?), te_anaerobic=COALESCE(te_anaerobic, ?), "
                "official_load=COALESCE(official_load, ?), dynamics=COALESCE(dynamics, ?), "
                "updated_at=? WHERE id=?",
                (json.dumps(a["hr_zones_pct"]) if a["hr_zones_pct"] else None,
                 a["te_aerobic"], a["te_anaerobic"], a["official_load"],
                 json.dumps(a["dynamics"], ensure_ascii=False) if a["dynamics"] else None,
                 _nowlike(), hit["id"]))
        else:
            # 插件版活动不做 sRPE 负荷（RPE 由用户后续录入）；先落基础事实
            _insert_activity_base(conn, a, sport)
            n += 1
        store_ext.upsert_garmin_activity(conn, raw.get("activityId"), a["name"],
                                         a["sport_key"], a["start_time"])
    conn.commit()
    if touched_days:
        try:
            from core import daily as dailymod
            for d in sorted(touched_days)[-7:]:
                dailymod.compute_engine_daily(conn, cfg, d)
        except Exception:  # noqa: BLE001
            pass
    return n


def _nowlike() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _insert_activity_base(conn, a: dict, sport: str) -> None:
    from core.engine import intensity_domain
    domain = intensity_domain(hr_zones_pct=a["hr_zones_pct"])
    conn.execute(
        "INSERT INTO activities(garmin_activity_id, start_time, sport_id, duration_min, distance_km, "
        "avg_hr, max_hr, hr_zones_pct, avg_pace_min_km, elevation_gain_m, power_avg_w, power_max_w, "
        "dynamics, te_aerobic, te_anaerobic, official_load, indoor, intensity_domain, "
        "calc_version, param_version, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(garmin_activity_id) DO NOTHING",
        (a["garmin_activity_id"], a["start_time"], sport, a["duration_min"], a["distance_km"],
         a["avg_hr"], a["max_hr"],
         json.dumps(a["hr_zones_pct"]) if a["hr_zones_pct"] else None,
         a["avg_pace_min_km"], a["elevation_gain_m"], a["power_avg_w"], a["power_max_w"],
         json.dumps(a["dynamics"], ensure_ascii=False) if a["dynamics"] else None,
         a["te_aerobic"], a["te_anaerobic"], a["official_load"], a["indoor"],
         domain, "1.3", "p2026-10", _nowlike()))


# ── 可选：SDK 直读睡眠原始 DTO（含 sleepLevels） ─────────────

def _probe_sdk_sleep(dates: list) -> dict:
    """仅当 session 是 DI v2 时才尝试；返回 {date: raw_payload}。失败即返回空。"""
    node = DEFAULTS["node"]
    js = PLUGIN_CFG.get("probe_js") or str(resolve(load_config(), "ingest/_sdk_sleep_probe.js"))
    if not Path(js).exists():
        return {}
    env = dict(os.environ)
    env.update({
        "GARMIN_USERNAME": DEFAULTS["username"], "GARMIN_REGION": DEFAULTS["region"],
        "GARMIN_ACCOUNT": DEFAULTS["account"], "GARMIN_SESSION_TOKEN_FILE": DEFAULTS["session"],
        "GARMIN_FIT_DOWNLOAD_DIR": os.path.abspath(DEFAULTS["fit_dir"]),
        "PLUGIN_LIB": str(Path(DEFAULTS["mcp_js"]).parent),
    })
    args = [js] + [str(Path(DEFAULTS["mcp_js"]).parent)] + dates
    try:
        import subprocess
        out = subprocess.run([node] + args, env=env, capture_output=True, timeout=180, text=True,
                             encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] SDK 睡眠直读不可用：{type(e).__name__}: {e}", file=sys.stderr)
        return {}
    if out.returncode != 0:
        print(f"[warn] SDK 睡眠直读失败：{(out.stderr or '')[-400:]}", file=sys.stderr)
        return {}
    try:
        data = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict) or data.get("error"):
        print(f"[warn] SDK 睡眠直读：{data}", file=sys.stderr)
        return {}
    return data.get("sleep") or {}


# ── 主流程 ──────────────────────────────────────────────────

def _raw_file(cfg, r) -> Path:
    scope, day = r["scope"], r["date"]
    return store_ext.raw_root(cfg) / r["tool"] / (
        f"{scope}.json.gz" if scope else f"{day}.json.gz")


def _night_summary(raw: dict) -> dict:
    """SDK 睡眠原始载荷里的夜间汇总（顶层字段）。"""
    dto = raw.get("dailySleepDTO") or {}
    scores = dto.get("sleepScores") or {}
    out = {
        "avg_overnight_hrv": _num(raw.get("avgOvernightHrv")),
        "body_battery_change": _num(raw.get("bodyBatteryChange")),
        "avg_sleep_stress": _num(dto.get("avgSleepStress")),
        "avg_sleep_hr": _num(dto.get("avgHeartRate")),
        "awake_count": _num(dto.get("awakeCount")),
        "restless_moments": _num(raw.get("restlessMomentsCount")),
        "unmeasurable_sleep_min": (_num(dto.get("unmeasurableSleepSeconds")) or 0) / 60 or None,
        "respiration_avg": _num(dto.get("averageRespirationValue")),
        "respiration_min": _num(dto.get("lowestRespirationValue")),
        "respiration_max": _num(dto.get("highestRespirationValue")),
        "sleep_score": _num((scores.get("overall") or {}).get("value")),
        "sleep_deep_pct": _num((scores.get("deepPercentage") or {}).get("value")),
        "sleep_light_pct": _num((scores.get("lightPercentage") or {}).get("value")),
        "sleep_rem_pct": _num((scores.get("remPercentage") or {}).get("value")),
        "sleep_need_min": (_num((dto.get("sleepNeed") or {}).get("actual"))),
    }
    hr = raw.get("sleepHeartRate")
    if isinstance(hr, list):
        vals = [_num(x.get("value")) for x in hr if isinstance(x, dict)]
        vals = [v for v in vals if v is not None]
        if vals:
            out["sleep_hr_min"] = min(vals)
            out["sleep_hr_max"] = max(vals)
    return {k: v for k, v in out.items() if v is not None}


def _upsert_official(conn, day: str, m: dict) -> None:
    """同时镜像到 v1.7 的 official_snapshot（Garmin 官方负荷，隔离层，仅列出不参与自算）。"""
    if not m:
        return
    conn.execute(
        "INSERT INTO official_snapshot(date, training_readiness, recovery_time_h, load_7d, "
        "load_ratio_4w1w, training_status, raw) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET load_7d=COALESCE(excluded.load_7d, official_snapshot.load_7d), "
        "load_ratio_4w1w=COALESCE(excluded.load_ratio_4w1w, official_snapshot.load_ratio_4w1w), "
        "training_status=COALESCE(excluded.training_status, official_snapshot.training_status), "
        "raw=COALESCE(excluded.raw, official_snapshot.raw)",
        (day,
         m.get("readiness_score"),
         m.get("recovery_time_h") / 60 if m.get("recovery_time_h") else None,
         m.get("garmin_load_acute_7d"),
         m.get("garmin_acwr"),
         m.get("garmin_training_status_phrase"),
         json.dumps(m, ensure_ascii=False)))


def ingest_payload(conn, cfg: dict, report: dict, tool: str, d: str | None, payload) -> None:
    """把插件原始返回拆进结构化表（实时调用与历史重放共用）。"""
    if tool == "sdk_getSleep" and isinstance(payload, dict):
        dto = payload.get("dailySleepDTO") or payload
        day = dto.get("calendarDate") or d
        _upsert_daily(conn, day, _metric_rows_from_sleep(dto))
        _upsert_daily(conn, day, _night_summary(payload))
        n = store_ext.replace_sleep_stages(conn, day, payload.get("sleepLevels") or dto.get("sleepLevels"),
                                           dto.get("sleepStartTimestampLocal"))
        if n:
            report["sleep_stage_days"] = report.get("sleep_stage_days", 0) + 1
        series = store_ext.write_night_series(conn, day, payload)
        if series:
            report["sleep_series_days"] = report.get("sleep_series_days", 0) + 1
    if tool == "get_garmin_sleep":
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict) or not item.get("date"):
                continue
            _upsert_daily(conn, item["date"], _metric_rows_from_sleep(item))
            store_ext.upsert_metrics(conn, item["date"], {"sleep_score": _num(item.get("sleepScore"))})
            report["days"] = report.get("days", 0) + 1
    elif tool == "get_garmin_hrv" and isinstance(payload, dict):
        s = store_ext.write_series(conn, d, hrv_readings=payload.get("hrvReadings"))
        if s["hrv"]:
            report["hrv_reading_days"] = report.get("hrv_reading_days", 0) + 1
    elif tool == "get_garmin_stress" and isinstance(payload, dict):
        s = store_ext.write_series(conn, d, stress_values=payload.get("stressValuesArray"),
                                   battery_values=payload.get("bodyBatteryValuesArray"))
        if s["stress"]:
            report["stress_series_days"] = report.get("stress_series_days", 0) + 1
        if s["battery"]:
            report["battery_series_days"] = report.get("battery_series_days", 0) + 1
    elif tool == "get_garmin_hrv_summaries" and isinstance(payload, dict):
        for x in payload.get("hrvSummaries") or []:
            if not isinstance(x, dict) or not x.get("calendarDate"):
                continue
            day, b = x["calendarDate"], (x.get("baseline") or {})
            low, high = _num(b.get("balancedLow")), _num(b.get("balancedUpper"))
            center = round((low + high) / 2, 1) if low is not None and high is not None else None
            _upsert_daily(conn, day, {"hrv_last_night": _num(x.get("lastNightAvg")),
                                      "hrv_baseline": center})
            store_ext.upsert_metrics(conn, day, {
                "hrv_last_night_5min_high": _num(x.get("lastNight5MinHigh")),
                "hrv_weekly_avg": _num(x.get("weeklyAvg")),
                "hrv_balanced_center": center})
    elif tool == "get_garmin_stress_summaries" and isinstance(payload, list):
        for x in payload:
            if not isinstance(x, dict) or not x.get("calendarDate"):
                continue
            v = (x.get("values") or {}).get("overallStressLevel")
            if v is not None:
                _upsert_daily(conn, x["calendarDate"], {"stress_daily_avg": _num(v)})
    elif tool == "get_garmin_heart_rate" and isinstance(payload, list):
        for x in payload:
            if not isinstance(x, dict) or not x.get("date"):
                continue
            _upsert_daily(conn, x["date"], {"resting_hr": _num(x.get("restingHR"))})
            store_ext.upsert_metrics(conn, x["date"], {
                "resting_hr": _num(x.get("restingHR")),
                "hr_max_day": _num(x.get("maxHR")),
                "hr_min_day": _num(x.get("minHR"))})
    elif tool == "get_garmin_steps" and isinstance(payload, list):
        for x in payload:
            if isinstance(x, dict) and x.get("date"):
                store_ext.upsert_metrics(conn, x["date"], {
                    "steps": _num(x.get("totalSteps")),
                    "steps_goal": _num(x.get("goal")),
                    "highly_active_s": _num(x.get("highlyActiveSeconds"))})
    elif tool == "get_garmin_weight" and isinstance(payload, list):
        for x in payload:
            if isinstance(x, dict) and x.get("date"):
                store_ext.upsert_metrics(conn, x["date"], {
                    "weight_kg": _num(x.get("weightKg")),
                    "bmi": _num(x.get("bmi")),
                    "body_fat_pct": _num(x.get("bodyFatPercentage")),
                    "muscle_mass_kg": _num(x.get("muscleMassKg")),
                    "water_pct": _num(x.get("waterPercentage"))})
    elif tool == "get_garmin_training_readiness":
        store_ext.upsert_metrics(conn, d, _readiness_metrics(_dto(payload) or {}))
    elif tool == "get_garmin_daily_training_status":
        store_ext.upsert_metrics(conn, d, _training_status_metrics(payload))
        _upsert_official(conn, d, _training_status_metrics(payload))
    elif tool == "get_garmin_training_status":
        store_ext.upsert_metrics(conn, d, _training_status_metrics(payload))
        _upsert_official(conn, d, _training_status_metrics(payload))
    elif tool == "get_garmin_training_load_balance":
        store_ext.upsert_metrics(conn, d, _load_balance_metrics(payload))
    elif tool == "get_garmin_body_battery":
        rows = payload if isinstance(payload, list) else [payload]
        for x in rows:
            if not isinstance(x, dict):
                continue
            day = x.get("date") or d
            store_ext.write_series(conn, day, battery_values=[
                (r[0], "MEASURED", r[1]) for r in (x.get("bodyBatteryValuesArray") or [])
                if isinstance(r, list) and len(r) > 1])
            # 每日汇总要落到当天，不能落到整段区间的起始日
            store_ext.upsert_metrics(conn, day, _battery_metrics([x]))
    elif tool == "get_garmin_max_metrics":
        store_ext.upsert_metrics(conn, d, _max_metrics(payload))
    elif tool in ("get_garmin_race_predictions_range", "get_garmin_max_metrics_range"):
        rows = payload if isinstance(payload, list) else [payload]
        for x in rows:
            if isinstance(x, dict) and x.get("calendarDate"):
                fn = _race_predictions if tool.startswith("get_garmin_race") else _max_metrics
                store_ext.upsert_metrics(conn, x["calendarDate"], fn(x))
    elif tool == "get_garmin_race_predictions":
        day = (d if d and d != "latest" else None) or _prediction_date(payload) or date.today().isoformat()
        store_ext.upsert_metrics(conn, day, _race_predictions(payload))


def _prediction_date(payload):
    dto = _dto(payload)
    return dto.get("calendarDate") if isinstance(dto, dict) else None


def replay_raw(cfg: dict, report: dict, since: str | None = None) -> int:
    """重放 archive/plugin_raw 里的历史存档（新增解析逻辑后可回填）。"""
    conn = dbmod.get_conn(cfg)
    n = 0
    try:
        q = "SELECT tool, target_date AS date, scope FROM plugin_raw"
        args = ()
        if since:
            q += " WHERE target_date >= ? OR target_date IS NULL"
            args = (since,)
        for r in conn.execute(q, args).fetchall():
            path = _raw_file(cfg, r)
            if not path.exists():
                print(f"[replay] missing {path}", file=sys.stderr)
                continue
            try:
                payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                print(f"[replay] bad payload {path}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            try:
                ingest_payload(conn, cfg, report, r["tool"], r["date"], payload)
            except Exception as e:  # noqa: BLE001
                print(f"[replay] ingest failed {r['tool']} {r['date']}: {type(e).__name__}: {e}",
                      file=sys.stderr)
                continue
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def run(start: str | None, end: str | None, activities: int, download_fit: bool,
        node: str, mcp_js: str, session_file: str, username: str, region: str,
        account: str, fit_dir: str, with_fit_splits: bool = True):
    _require_plugin_config()
    cfg = load_config()
    end = end or date.today().isoformat()
    start = start or (date.fromisoformat(end) - timedelta(days=13)).isoformat()
    env = dict(os.environ)
    env.update({
        "GARMIN_USERNAME": username, "GARMIN_REGION": region, "GARMIN_ACCOUNT": account,
        "GARMIN_SESSION_TOKEN_FILE": session_file, "GARMIN_FIT_DOWNLOAD_DIR": fit_dir,
        "GARMIN_ACTIVITY_DETAIL": "full",
    })
    os.makedirs(fit_dir, exist_ok=True)
    params = StdioServerParameters(command=node, args=[mcp_js], env=env)
    report = {"start": start, "end": end, "tools_ok": [], "tools_failed": [], "raw": [],
              "days": 0, "activities": 0, "inserted": 0, "splits": 0, "fit_files": 0,
              "sleep_stage_days": 0, "hrv_reading_days": 0, "stress_series_days": 0,
              "battery_series_days": 0, "metric_rows": 0}

    async def _main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as cs:
                await cs.initialize()
                listed = {t.name for t in (await cs.list_tools()).tools}

                async def grab(name, args, target=None, range_end=None, latest=False):
                    if name not in listed:
                        report["tools_failed"].append({"tool": name, "error": "not registered"})
                        return None
                    data = await _safe_call(cs, name, args)
                    if data is None:
                        report["tools_failed"].append({"tool": name, "error": "call failed"})
                        return None
                    report["tools_ok"].append(name)
                    day = "latest" if latest else target
                    conn = dbmod.get_conn(cfg)
                    try:
                        def _ingest_old(old, _name=name, _day=day):
                            # 解析被本轮覆盖的上一批数据（共享同一个 report，统计累加）
                            ingest_payload(conn, cfg, report, _name, _day, old)

                        conn.execute("BEGIN")
                        raw = store_ext.save_raw(cfg, conn, name, day, data,
                                                 range_end=range_end, ingest_old=_ingest_old)
                        report["raw"].append(raw)
                        ingest_payload(conn, cfg, report, name, day, data)
                        conn.commit()
                    except Exception as e:  # noqa: BLE001
                        conn.rollback()
                        print(f"[warn] {name} 解析失败：{type(e).__name__}: {e}", file=sys.stderr)
                    finally:
                        conn.close()
                    return data

                acts = []
                for s, e in _chunks(start, end):
                    if activities:
                        got = await grab("get_garmin_activities", {"limit": 100, "detail": "full"},
                                         target=s, range_end=e)
                        if isinstance(got, list):
                            acts.extend(g for g in got if isinstance(g, dict))
                    for tool in ("get_garmin_sleep", "get_garmin_heart_rate", "get_garmin_steps",
                                 "get_garmin_weight"):
                        await grab(tool, {"startDate": s, "endDate": e}, target=s, range_end=e)
                    for tool in SERIES_RANGE_TOOLS:
                        args = {"startDate": s, "endDate": e}
                        if tool == "get_garmin_race_predictions_range":
                            args["aggregation"] = "daily"
                        await grab(tool, args, target=s, range_end=e)

                for d in _dates(start, end):
                    for tool in DAILY_TOOLS:
                        await grab(tool, {"date": d}, target=d)
                await grab("get_garmin_race_predictions", {}, latest=True)
                await grab("get_garmin_workouts", {"limit": 100, "offset": 0}, latest=True)
                await grab("get_garmin_profile", {}, latest=True)

                if download_fit:
                    for a in acts:
                        aid = a.get("activityId")
                        if not aid:
                            continue
                        target = Path(fit_dir) / f"GARMIN_FIT_cn_{username}" / f"{aid}.fit"
                        if target.exists():
                            continue
                        if await _safe_call(cs, "download_garmin_activity_fit", {"activityId": aid}):
                            report["fit_files"] += 1
                return acts, [p for p in Path(fit_dir).rglob("*.fit")]

    acts, fits = asyncio.run(_main())

    # SDK 直读睡眠阶段（可选增强）：插件 get_garmin_sleep 不含阶段时间线
    target_days = list(_dates(start, end))
    sdk_sleep = _probe_sdk_sleep(target_days)
    if sdk_sleep:
        conn = dbmod.get_conn(cfg)
        try:
            for d, payload in sdk_sleep.items():
                if isinstance(payload, dict):
                    raw = store_ext.save_raw(cfg, conn, "sdk_getSleep", d, payload)
                    report["raw"].append(raw)
                    ingest_payload(conn, cfg, report, "sdk_getSleep", d, payload)
            conn.commit()
        finally:
            conn.close()

    conn = dbmod.get_conn(cfg)
    try:
        report["metric_rows"] = conn.execute("SELECT COUNT(*) FROM garmin_metrics").fetchone()[0]
        report["activities"] = len(acts)
        report["inserted"] = _upsert_activities(conn, cfg, acts)
    finally:
        conn.close()

    # 解析器/衍生表有变更时，可用 replay_raw() 从 archive/plugin_raw 重放旧存档回填
    if cfg_get(cfg, "garmin_plugin.replay_raw_each_sync", False):
        report["replayed"] = replay_raw(cfg, report)

    # 运动分段：本地 FIT 的 lap / split / 1km 自动分段
    if with_fit_splits and fits:
        try:
            from ingest import fit_parser
            report["splits"] = fit_parser.rebuild_splits(cfg, fits)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] FIT 分段回填失败：{type(e).__name__}: {e}", file=sys.stderr)

    report["raw_written"] = sum(1 for r in report["raw"] if r.get("written"))
    report["raw_bytes"] = sum(r.get("bytes", 0) for r in report["raw"])
    report["tools_ok"] = sorted(set(report["tools_ok"]))
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description="Garmin 插件全量只读采集")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--days", type=int, default=0, help="不给 start 时，回溯的天数（含今天）")
    p.add_argument("--activities", type=int, default=1, help="是否拉活动（1/0）")
    p.add_argument("--download-fit", action="store_true", help="缺 FIT 时从插件下载")
    p.add_argument("--no-splits", action="store_true", help="不回填 FIT 分段")
    p.add_argument("--replay", action="store_true",
                   help="不联网：只把 archive/plugin_raw 的历史存档重新解析进库（解析器变更后回填）")
    p.add_argument("--node", default=DEFAULTS["node"])
    p.add_argument("--mcp-js", default=DEFAULTS["mcp_js"])
    p.add_argument("--session", default=DEFAULTS["session"])
    p.add_argument("--username", default=DEFAULTS["username"])
    p.add_argument("--region", default=DEFAULTS["region"])
    p.add_argument("--account", default=DEFAULTS["account"])
    p.add_argument("--fit-dir", default=DEFAULTS["fit_dir"])
    a = p.parse_args(argv)
    if a.replay:
        cfg = load_config()
        report = {"days": 0, "activities": 0, "inserted": 0, "splits": 0,
                  "sleep_stage_days": 0, "hrv_reading_days": 0, "stress_series_days": 0,
                  "battery_series_days": 0, "sleep_series_days": 0, "metric_rows": 0}
        n = replay_raw(cfg, report, since=a.start)
        report["replayed_files"] = n
        conn = dbmod.get_conn(cfg)
        try:
            report["coverage"] = store_ext.stats(conn)
        finally:
            conn.close()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    end = a.end or date.today().isoformat()
    start = a.start or (date.fromisoformat(end) - timedelta(days=max(1, a.days) - 1)).isoformat()
    rep = run(start, end, a.activities, a.download_fit, a.node, a.mcp_js, a.session,
              a.username, a.region, a.account, a.fit_dir, with_fit_splits=not a.no_splits)
    rep["tools_ok"] = sorted(set(rep["tools_ok"]))
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return rep


if __name__ == "__main__":
    main()
