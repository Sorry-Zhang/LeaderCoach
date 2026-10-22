from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core import db as dbmod
from core.config import load_config

_plugin_cfg = load_config().get("garmin_plugin", {})

DEFAULTS = {
    "node": os.environ.get("GARMIN_PLUGIN_NODE") or _plugin_cfg.get("node", "node"),
    "mcp_js": os.environ.get("GARMIN_PLUGIN_MCP_JS") or _plugin_cfg.get("mcp_js", ""),
    "session": os.environ.get("GARMIN_PLUGIN_SESSION") or _plugin_cfg.get("session", ""),
    "username": os.environ.get("GARMIN_USERNAME") or _plugin_cfg.get("username", ""),
    "region": os.environ.get("GARMIN_REGION") or _plugin_cfg.get("region", "cn"),
    "account": os.environ.get("GARMIN_ACCOUNT") or _plugin_cfg.get("account", "personal-cn"),
    "fit_dir": os.environ.get("GARMIN_FIT_DIR") or _plugin_cfg.get("fit_dir", "data/garmin_fit"),
}

def _require_plugin_config() -> None:
    missing = [k for k in ("mcp_js", "session") if not DEFAULTS[k]]
    if missing:
        raise SystemExit(
            "缺少 Garmin 插件配置。请设置环境变量：\n"
            "  GARMIN_PLUGIN_MCP_JS   插件 mcp.js 的绝对路径\n"
            "  GARMIN_PLUGIN_SESSION  插件会话文件路径\n"
            "插件安装：npm i -g dsh-plugin-garmin-connect（或见 README）"
        )

def _chunks(start: str, end: str, days: int = 30):
    cur = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    while cur <= end_d:
        nxt = min(cur + timedelta(days=days - 1), end_d)
        yield cur.isoformat(), nxt.isoformat()
        cur = nxt + timedelta(days=1)

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
            await asyncio.sleep(0.4)
            return await _call(session, name, args)
        except Exception as e:
            last = e
            if i < retries:
                await asyncio.sleep(2.0)
    print(f"[warn] {name} {args} -> {last}", file=sys.stderr)
    return None

async def _pull_daily(session, start: str, end: str) -> dict:
    rows: dict[str, dict] = {}

    def row(d: str) -> dict:
        return rows.setdefault(d, {"date": d})

    for s, e in _chunks(start, end, 30):
        hrv = await _safe_call(session, "get_garmin_hrv_summaries",
                               {"startDate": s, "endDate": e}) or {}
        for x in (hrv.get("hrvSummaries") or []):
            if not isinstance(x, dict):
                continue
            d = x.get("calendarDate")
            if d:
                r = row(d)
                r["hrv_last_night"] = x.get("lastNightAvg")
                b = x.get("baseline") or {}
                if b.get("balancedLow") is not None and b.get("balancedUpper") is not None:
                    r["hrv_baseline"] = round((b["balancedLow"] + b["balancedUpper"]) / 2, 1)

        sleep = await _safe_call(session, "get_garmin_sleep",
                                 {"startDate": s, "endDate": e}) or []
        if not isinstance(sleep, list):
            sleep = []
        for x in sleep:
            if not isinstance(x, dict):
                continue
            d = x.get("date")
            if d:
                r = row(d)
                r["sleep_hours"] = x.get("sleepDurationHours")
                r["sleep_deep_h"] = x.get("deepSleepHours")
                r["sleep_light_h"] = x.get("lightSleepHours")
                r["sleep_rem_h"] = x.get("remSleepHours")
                r["sleep_awake_h"] = x.get("awakeDurationHours")

        hr = await _safe_call(session, "get_garmin_heart_rate",
                              {"startDate": s, "endDate": e}) or []
        if not isinstance(hr, list):
            hr = []
        for x in hr:
            if not isinstance(x, dict):
                continue
            d = x.get("date")
            if d:
                row(d)["resting_hr"] = x.get("restingHR")

    for s, e in _chunks(start, end, 7):
        stress = await _safe_call(session, "get_garmin_stress_summaries",
                                  {"startDate": s, "endDate": e}) or []
        if not isinstance(stress, list):
            stress = []
        for x in stress:
            if not isinstance(x, dict):
                continue
            d = x.get("calendarDate")
            v = (x.get("values") or {}).get("overallStressLevel")
            if d and v is not None:
                row(d)["stress_daily_avg"] = v

    return rows

async def _pull_activities(session, limit: int, download_fit: bool,
                           fit_dir: str | None = None,
                           username: str | None = None) -> list:
    raw = []
    offset = 0
    while len(raw) < limit:
        batch = min(100, limit - len(raw))
        got = await _call(session, "get_garmin_activities",
                          {"limit": batch, "offset": offset}) or []
        if not got:
            break
        raw.extend(got)
        if len(got) < batch:
            break
        offset += len(got)
    acts = raw
    out = []
    for a in acts:
        aid = a.get("activityId")
        item = {
            "activityId": aid,
            "name": a.get("activityName"),
            "start": a.get("startTimeLocal"),
            "type": (a.get("activityType") or {}).get("typeKey"),
            "duration_s": a.get("duration"),
            "distance_m": a.get("distance"),
            "avgHR": a.get("averageHR"),
            "maxHR": a.get("maxHR"),
            "pace": a.get("averagePaceMinPerKm"),
            "elev": a.get("elevationGainMeters"),
        }
        if download_fit and aid:
            target = None
            if fit_dir and username:
                target = (Path(fit_dir) / f"GARMIN_FIT_cn_{username}" / f"{aid}.fit")
            if target is not None and target.exists():
                item["fit"] = {"skipped": True, "file": str(target)}
            else:
                try:
                    item["fit"] = await _call(session, "download_garmin_activity_fit",
                                              {"activityId": aid})
                except Exception as e:
                    item["fit_error"] = str(e)[:200]
        out.append(item)
    return out

_SPORT_MAP = {
    "running": "run", "walking": "other", "hiking": "other",
    "cycling": "other",
    "climbing": "climb", "table_tennis": "table_tennis",
    "strength_training": "strength",
}

def _insert_activities(conn, acts: list) -> int:
    from core import store
    cfg = load_config()
    valid = {r[0] for r in conn.execute("SELECT sport_id FROM sport_registry")}
    n = 0
    for a in acts:
        start = a.get("start")
        dur_s = a.get("duration_s") or 0
        if not start or dur_s <= 0:
            continue
        day = start[:10]
        sport = _SPORT_MAP.get(a.get("type") or "", "other")
        if sport not in valid:
            sport = "other"
        dur_min = round(dur_s / 60.0, 2)
        hit = conn.execute(
            "SELECT id FROM activities WHERE substr(start_time,1,10)=? AND sport_id=? "
            "AND ABS(duration_min-?) < 1.0", (day, sport, dur_min)).fetchone()
        if hit:
            continue
        dist = a.get("distance_m")
        store.insert_activity(conn, cfg, {
            "garmin_activity_id": f"plugin:{a['activityId']}",
            "start_time": start,
            "sport_id": sport,
            "duration_min": dur_min,
            "distance_km": round(dist / 1000.0, 3) if dist else None,
            "avg_hr": a.get("avgHR"),
            "max_hr": a.get("maxHR"),
            "avg_pace_min_km": a.get("pace"),
            "elevation_gain_m": a.get("elev"),
        })
        n += 1
    conn.commit()
    return n

def run(start: str | None, end: str | None, activities: int,
        download_fit: bool, insert_activities: bool, node: str, mcp_js: str,
        session_file: str, username: str, region: str, account: str, fit_dir: str):
    _require_plugin_config()
    env = dict(os.environ)
    env.update({
        "GARMIN_USERNAME": username,
        "GARMIN_REGION": region,
        "GARMIN_ACCOUNT": account,
        "GARMIN_SESSION_TOKEN_FILE": session_file,
        "GARMIN_FIT_DOWNLOAD_DIR": fit_dir,
    })
    os.makedirs(fit_dir, exist_ok=True)
    params = StdioServerParameters(command=node, args=[mcp_js], env=env)

    async def _main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as cs:
                await cs.initialize()
                daily = await _pull_daily(cs, start, end) if (start and end) else {}
                acts = await _pull_activities(cs, activities, download_fit,
                                              fit_dir, username) if activities else []
                return daily, acts

    daily, acts = asyncio.run(_main())

    inserted = 0
    if daily or (insert_activities and acts):
        conn = dbmod.get_conn(load_config())
        try:
            if daily:
                from ingest.health_fit_parser import write_health_rows
                write_health_rows(conn, daily.values())
            if insert_activities and acts:
                inserted = _insert_activities(conn, acts)
        finally:
            conn.close()
    return daily, acts, inserted

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--activities", type=int, default=0)
    p.add_argument("--download-fit", action="store_true")
    p.add_argument("--insert-activities", action="store_true")
    p.add_argument("--node", default=DEFAULTS["node"])
    p.add_argument("--mcp-js", default=DEFAULTS["mcp_js"])
    p.add_argument("--session", default=DEFAULTS["session"])
    p.add_argument("--username", default=DEFAULTS["username"])
    p.add_argument("--region", default=DEFAULTS["region"])
    p.add_argument("--account", default=DEFAULTS["account"])
    p.add_argument("--fit-dir", default=DEFAULTS["fit_dir"])
    args = p.parse_args(argv)

    daily, acts, inserted = run(
        args.start, args.end, args.activities, args.download_fit,
        args.insert_activities, args.node, args.mcp_js, args.session,
        args.username, args.region, args.account, args.fit_dir)
    print(json.dumps({
        "daily_days": len(daily),
        "daily_first": min(daily) if daily else None,
        "daily_last": max(daily) if daily else None,
        "activities_listed": len(acts),
        "activities_inserted": inserted,
        "fits_downloaded": sum(1 for a in acts if a.get("fit")),
        "fit_dir": args.fit_dir,
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
