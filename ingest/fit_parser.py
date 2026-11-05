from __future__ import annotations

import json
from pathlib import Path

_SPORT_MAP = {
    "running": "run",
    "cycling": "cycling",
    "climbing": "climb",
    "table_tennis": "table_tennis",
    "strength_training": "strength",
    "trail_running": "trail_run",
    "walking": "other",
    "other": "other",
}

def _frame_fields(frame) -> dict:
    out = {}
    try:
        fields = frame.fields
        for f in fields:
            name = getattr(f, "name", None)
            if name is None:
                continue
            val = getattr(f, "value", None)
            if val is not None:
                out[name] = val
    except Exception:
        pass
    return out

def parse_fit(path) -> dict:
    try:
        import fitdecode
    except ImportError as e:
        raise RuntimeError("缺少 fitdecode 依赖，请执行：pip install fitdecode") from e

    meta: dict = {}
    records: list[dict] = []
    with fitdecode.FitReader(str(path)) as fit:
        for frame in fit:
            if getattr(frame, "frame_type", None) != fitdecode.FIT_FRAME_DATA:
                continue
            name = getattr(frame, "name", "")
            fields = _frame_fields(frame)
            if name == "session":
                meta.update(fields)
            elif name == "record":
                records.append(fields)
            elif name in ("sport", "activity"):
                meta.update(fields)

    def g(*names, default=None):
        for n in names:
            if n in meta and meta[n] is not None:
                return meta[n]
        return default

    sport_raw = g("sport", "sub_sport")
    sport_id = _SPORT_MAP.get(str(sport_raw).lower() if sport_raw else "", "other")

    duration_s = g("total_timer_time", "total_elapsed_time")
    duration_min = round(duration_s / 60.0, 2) if duration_s else None

    dist_m = g("total_distance")
    distance_km = round(dist_m / 1000.0, 3) if dist_m else None

    avg_speed = g("enhanced_avg_speed", "avg_speed")
    pace = None
    if distance_km and duration_min and distance_km > 0:
        pace = round(duration_min / distance_km, 2)

    dynamics = None
    if records:
        def _avg(key):
            vals = [r[key] for r in records if key in r and isinstance(r[key], (int, float))]
            return round(sum(vals) / len(vals), 2) if vals else None
        if any(k in records[0] for k in ("vertical_oscillation", "stance_time", "stride_length")):
            dynamics = {
                "vo_cm": _avg("vertical_oscillation"),
                "gct_ms": _avg("stance_time"),
                "stride_cm": _avg("stride_length"),
            }

    def _scaled(v, lo, hi, div):
        if v is None:
            return None
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return max(lo, min(hi, int(round(x / div))))

    rpe = _scaled(g("workout_rpe"), 1, 10, 10.0)
    feel = _scaled(g("workout_feel"), 0, 5, 20.0)

    st = g("start_time")
    if hasattr(st, "astimezone"):
        from datetime import timedelta, timezone
        st = st.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "garmin_activity_id": g("timestamp", default=None),
        "fit_path": str(path),
        "start_time": st,
        "rpe": rpe,
        "feel": feel,
        "sport_id": sport_id,
        "duration_min": duration_min,
        "distance_km": distance_km,
        "avg_hr": g("avg_heart_rate"),
        "max_hr": g("max_heart_rate"),
        "avg_pace_min_km": pace,
        "elevation_gain_m": g("total_ascent"),
        "power_avg_w": g("avg_power"),
        "power_max_w": g("max_power"),
        "dynamics": dynamics,
        "indoor": None,
        "annotation": None,
    }


# ── 运动分段：lap（手动/自动圈）与 1 km 自动分段 → activity_splits ─────────

def extract_splits(path) -> dict:
    """解析一个活动 FIT 的分段信息（含爬升与坡度调整配速）。

    返回 {"start_time": "YYYY-MM-DD HH:MM:SS",
          "laps": [{idx, distance_m, timer_s, avg_hr, max_hr, ascent_m, start_time, ...爬坡字段}],
          "km_splits": [{km, pace_min_km, avg_hr, max_hr, distance_m, duration_s,
                         start_time, end_time,
                         elevation_gain_m, elevation_loss_m, net_elevation_m,
                         avg_grade_pct, max_grade_pct, gap_pace_min_km}]}
    爬升/坡度由 record 层的 enhanced_altitude 逐点算出（见 core.grade）。
    """
    try:
        import fitdecode
    except ImportError as e:
        raise RuntimeError("缺少 fitdecode，请执行：pip install fitdecode") from e

    laps, records, session = [], [], {}
    with fitdecode.FitReader(str(path)) as fit:
        for frame in fit:
            if getattr(frame, "frame_type", None) != fitdecode.FIT_FRAME_DATA:
                continue
            name = getattr(frame, "name", "")
            fields = _frame_fields(frame)
            if name == "session":
                session.update(fields)
            elif name == "lap":
                laps.append(fields)
            elif name == "record":
                records.append(fields)

    st = session.get("start_time")
    if hasattr(st, "astimezone"):
        from datetime import timedelta, timezone
        st = st.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    lap_rows = []
    for i, l in enumerate(laps):
        ls = l.get("start_time")
        if hasattr(ls, "astimezone"):
            from datetime import timedelta, timezone
            ls = ls.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        lap_rows.append({
            "idx": i,
            "distance_m": l.get("total_distance"),
            "timer_s": l.get("total_timer_time"),
            "avg_hr": l.get("avg_heart_rate"),
            "max_hr": l.get("max_heart_rate"),
            "ascent_m": l.get("total_ascent"),
            "start_time": ls,
        })

    # ── 逐点序列（距离 + 海拔 + 心率）。海拔优先用 enhanced_altitude ──
    pts = []
    for r in sorted(records, key=lambda x: (x.get("timestamp") is None, x.get("timestamp"))):
        d = r.get("distance")
        t = r.get("timestamp")
        if d is None or t is None:
            continue
        alt = r.get("enhanced_altitude")
        if alt is None:
            alt = r.get("altitude")
        pts.append({"d": d, "t": t, "alt": alt, "hr": r.get("heart_rate")})

    km_rows = []
    if len(pts) >= 2:
        from core import grade as grademod
        km = 1
        seg = 0                      # 当前分段在 pts 里的起始下标
        for i in range(1, len(pts)):
            span = pts[i]["d"] - pts[seg]["d"]
            is_last = i == len(pts) - 1
            if span < 1000 and not is_last:
                continue
            chunk = pts[seg:i + 1]
            dspan = chunk[-1]["d"] - chunk[0]["d"]
            secs = (chunk[-1]["t"] - chunk[0]["t"]).total_seconds()
            # 末段不足 100 m 视为零头，丢弃
            if is_last and span < 1000 and dspan < 100:
                break
            hrs = [p["hr"] for p in chunk if p["hr"] is not None]
            elev = grademod.segment_stats([(q["d"], q["alt"]) for q in chunk])
            pace = round(secs / 60 / (dspan / 1000), 3) if secs > 0 and dspan > 0 else None
            km_rows.append({
                "km": km,
                "pace_min_km": pace,
                "avg_hr": round(sum(hrs) / len(hrs)) if hrs else None,
                "max_hr": max(hrs) if hrs else None,
                "distance_m": round(dspan, 1),
                "duration_s": round(secs, 1) if secs > 0 else None,
                "start_time": _local_str(chunk[0]["t"]),
                "end_time": _local_str(chunk[-1]["t"]),
                "elevation_gain_m": elev["gain_m"],
                "elevation_loss_m": elev["loss_m"],
                "net_elevation_m": elev["net_m"],
                "avg_grade_pct": elev["avg_grade_pct"],
                "max_grade_pct": elev["max_grade_pct"],
                "gap_pace_min_km": grademod.grade_adjusted_pace(pace, elev["cost_ratio"]),
            })
            km += 1
            if is_last:
                break
            seg = i                    # 边界点作为下一段的起点

    return {"start_time": st, "laps": lap_rows, "km_splits": km_rows}


def _local_str(ts) -> str | None:
    """FIT 时间戳 → 本地（+08:00）'YYYY-MM-DD HH:MM:SS' 字符串。"""
    if ts is None or not hasattr(ts, "astimezone"):
        return None
    from datetime import timedelta, timezone
    return ts.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _find_activity(conn, fit_file, info: dict):
    """把一份 FIT 归到 activities 的一行。

    优先级：
      1) FIT 文件名就是 Garmin activityId（插件下载命名 <activityId>.fit）
         → garmin_activity_id = 'plugin:<id>' / '<id>' / fit_path 结尾匹配；
      2) 开始时刻对齐（±90 分钟）+ 时长相近，并用距离作二次校验；
      3) 仅同一天且只有唯一候选时兜底。
    旧实现只靠 fit_path LIKE（本库全为 NULL）+ 开始时刻 1 分钟窗口，
    而活动时间戳又被截成日期，因此永远匹配不上——这是分段数卡死 232 的根因。
    """
    stem = Path(fit_file).stem
    if stem.isdigit():
        hit = conn.execute(
            "SELECT id FROM activities WHERE garmin_activity_id IN (?,?,?) "
            "OR fit_path LIKE ? ORDER BY id DESC LIMIT 1",
            (f"plugin:{stem}", stem, f"plugin:{stem}", f"%{stem}%")).fetchone()
        if hit:
            return hit

    st = info.get("start_time")
    if not st:
        return None
    day = st[:10]
    sql = ("SELECT id, start_time, duration_min, distance_km FROM activities "
           "WHERE substr(start_time,1,10)=?")
    cands = conn.execute(sql, (day,)).fetchall()
    if not cands:
        # 跨日：开始时刻接近零点时，FIT 本地日期可能比活动早/晚一天
        from datetime import date as _d, timedelta as _td
        try:
            alt = (_d.fromisoformat(day) - _td(days=1)).isoformat()
        except Exception:  # noqa: BLE001
            alt = None
        if alt:
            cands = conn.execute(sql, (alt,)).fetchall()
    if not cands:
        return None

    fit_dur_min = None
    try:
        fit_dur_min = sum((r.get("timer_s") or 0) for r in info.get("laps") or []) / 60.0 or None
    except Exception:  # noqa: BLE001
        fit_dur_min = None

    def _score(row):
        s = 0.0
        rs = row["start_time"] or ""
        if len(rs) >= 19:
            try:
                from datetime import datetime as _dt
                delta = abs((_dt.fromisoformat(rs[:19]) - _dt.fromisoformat(st[:19])).total_seconds())
                s += max(0.0, 1.0 - delta / 5400.0)  # 90 分钟窗口内线性衰减
            except Exception:  # noqa: BLE001
                pass
        else:
            s += 0.3  # 只有日期：无法用时刻定位，但同日候选仍可用
        if fit_dur_min and row["duration_min"]:
            if abs(row["duration_min"] - fit_dur_min) <= max(2.0, 0.05 * fit_dur_min):
                s += 0.8
        return s

    scored = sorted(((_score(r), r) for r in cands), key=lambda x: -x[0])
    if not scored or scored[0][0] <= 0:
        return None
    if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 0.05:
        return None  # 分数并列，不猜
    return scored[0][1]


def rebuild_splits(cfg, files, conn=None, infos: dict | None = None) -> int:
    """把 FIT 分段写进 activity_splits，并把 FIT 路径回写到 activities.fit_path。

    有 laps 时写入圈分段（split_type=lap），否则用 1 km 自动分段（split_type=km）。
    返回写入的分段条数。
    """
    from core import db as dbmod
    own = conn is None
    try:
        conn = conn or dbmod.get_conn(cfg)
        n = 0
        for f in files:
            if infos is not None and str(f) in infos:
                info = infos[str(f)]
            else:
                try:
                    info = extract_splits(f)
                except Exception:
                    continue
            if not info["start_time"]:
                continue
            act = _find_activity(conn, f, info)
            if not act:
                continue
            conn.execute("DELETE FROM activity_splits WHERE activity_id=?", (act["id"],))
            conn.execute("DELETE FROM activity_split_detail WHERE activity_id=?", (act["id"],))
            lap_rows, km_rows = [], []
            if info["laps"]:
                for l in info["laps"]:
                    secs, dist = l["timer_s"], l["distance_m"]
                    pace = round((secs / 60) / (dist / 1000), 3) if secs and dist else None
                    laps_st = l.get("start_time")
                    laps_end = None
                    if laps_st and secs:
                        try:
                            from datetime import datetime as _dt, timedelta as _td
                            laps_end = (_dt.fromisoformat(laps_st) + _td(seconds=float(secs))
                                        ).strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:  # noqa: BLE001
                            laps_end = None
                    lap_rows.append((act["id"], "lap", l["idx"] + 1, dist, secs, pace,
                                     l["avg_hr"], l.get("max_hr"), l["ascent_m"],
                                     laps_st, laps_end, "fit",
                                     None, None, None, None, None))
            for k in info["km_splits"]:
                km_rows.append((act["id"], "km", k["km"], k.get("distance_m"), k.get("duration_s"),
                                k["pace_min_km"], k["avg_hr"], k.get("max_hr"),
                                k.get("elevation_gain_m"), k.get("start_time"),
                                k.get("end_time"), "fit",
                                k.get("elevation_loss_m"), k.get("net_elevation_m"),
                                k.get("avg_grade_pct"), k.get("max_grade_pct"),
                                k.get("gap_pace_min_km")))
            detail_rows = km_rows + lap_rows
            if detail_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO activity_split_detail(activity_id, split_type, seq, "
                    "distance_m, duration_s, pace_min_km, avg_hr, max_hr, elevation_gain_m, "
                    "start_time, end_time, source, elevation_loss_m, net_elevation_m, "
                    "avg_grade_pct, max_grade_pct, gap_pace_min_km) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", detail_rows)
            # 旧表 activity_splits 保留一份兼容副本：有圈存圈，否则存公里
            legacy = []
            if lap_rows:
                legacy = [(r[0], r[2], r[5], r[6], r[8]) for r in lap_rows]
            else:
                legacy = [(r[0], r[2], r[5], r[6], r[8]) for r in km_rows]
            if legacy:
                conn.executemany(
                    "INSERT OR REPLACE INTO activity_splits(activity_id, km, pace_min_km, avg_hr, "
                    "elevation_gain_m) VALUES(?,?,?,?,?)", legacy)
            # 圈级明细（含每圈起止时刻/计时时长）存进 activities.annotation 的 splits 字段
            detail = json.dumps({"fit": str(f), "laps": info["laps"], "km": info["km_splits"]},
                                ensure_ascii=False)
            conn.execute("UPDATE activities SET annotation=?, fit_path=? WHERE id=?",
                         (detail, str(Path(f).resolve()), act["id"]))
            n += len(detail_rows)
        conn.commit()
        return n
    finally:
        if own and conn is not None:
            conn.close()
