"""COROS 同步：拉活动 + 每日生理，写入本地库。

设计约束
--------
1. **不猜**。COROS 没给的一律留空并写进 notes，不写 0、不做插值。
2. **原始返回全量存档**（gzip），远端服务改版本时可复盘。
3. **按日期切片拉取**：多数 COROS 健康工具只有「最近 N 天」窗口，
   且 querySportRecords 的 limit 没有 offset，只能靠窗口切分做回填。
4. 健康工具在内置解析器里全部走 `parse_*`，解析失败时原文进入 raw 存档，
   不静默丢弃。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import coros_client as cc  # noqa: E402

from . import ingest_coros as ing  # noqa: E402

RAW_DIR = _ROOT / "data" / "coros" / "raw"

_NO_DATA = ("no data found", "not available", "haven't bound", "no devices found")


def _is_empty(text) -> bool:
    low = str(text).lower()
    return any(k in low for k in _NO_DATA)


def _stamp(day: str) -> str:
    """存入 CSV 用：只保留时间，不写完整内存地址。"""
    return f"{day[:10]}_{datetime.now().strftime('%H%M%S')}"


# ── 文本解析：健康类工具 ──────────────────────────────────

def parse_sleep_overview(text) -> dict:
    """querySleepOverview → {日期: {...}}。

    口径（重要）：用 `Main Sleep (asleep)` 而不是 `Daily Sleep`——
    后者含小睡，而引擎要的是夜间主睡眠。阶段只有百分比，按时长折算成小时，
    并标注 derived，避免被当成设备直读值。
    """
    out: dict = {}
    cur_day = None
    cur: dict = {}
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.fullmatch(r"(20\d{2}-\d{2}-\d{2})", line)
        if m:
            if cur_day and cur:
                out[cur_day] = cur
            cur_day, cur = m.group(1), {}
            continue
        if cur_day is None:
            continue
        if "not available" in line.lower():
            cur_day = None
            cur = {}
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key == "Main Sleep (asleep)":
            cur["sleep_hours"] = _hours(val)
        elif key == "Daily Sleep":
            cur["daily_sleep_h"] = _hours(val)
        elif key == "Deep Sleep Ratio":
            cur["_deep_pct"] = _pct(val)
        elif key == "Light Sleep Ratio":
            cur["_light_pct"] = _pct(val)
        elif key == "REM Ratio":
            cur["_rem_pct"] = _pct(val)
        elif key == "Awake Time":
            cur["sleep_awake_h"] = round((_minutes(val) or 0) / 60, 2)
        elif key == "Sleep Score":
            cur["_sleep_score"] = ing._num(val)
        elif key == "Main Sleep Window":
            parts = val.split(" - ")
            if len(parts) == 2:
                cur["sleep_start"] = _iso(parts[0])
                cur["sleep_end"] = _iso(parts[1])
        elif key == "Naps Total":
            cur["nap_min"] = _minutes(val)
    if cur_day and cur:
        out[cur_day] = cur

    for day, d in out.items():
        hours = d.get("sleep_hours")
        if hours and d.get("_deep_pct") is not None:
            # 阶段只有比例，按主睡眠时长折算；derived 标记不可省
            d["sleep_deep_h"] = round(hours * d["_deep_pct"] / 100, 2)
            d["sleep_light_h"] = round(hours * (d.get("_light_pct") or 0) / 100, 2)
            d["sleep_rem_h"] = round(hours * (d.get("_rem_pct") or 0) / 100, 2)
            d["stages_derived_from_pct"] = True
        for k in list(d):
            if k.startswith("_"):
                d.pop(k)
    return out


def parse_hrv(text) -> dict:
    """querySleepHrv → {日期: 日均 HRV}。"""
    out: dict = {}
    for raw in str(text).splitlines():
        line = raw.strip()
        m = re.match(r"^(20\d{2}-\d{2}-\d{2})\b.*?(\d+(?:\.\d+)?)\s*ms", line)
        if m:
            out[m.group(1)] = float(m.group(2))
            continue
        m = re.match(r"^(20\d{2}-\d{2}-\d{2})\D+HRV\D*(\d+(?:\.\d+)?)", line)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def parse_daily_number(text) -> dict:
    """queryRestingHeartRate / queryStressLevel / queryAvgHeartRate → {日期: 数值}。

    这些工具返回格式不固定（表格、列表、"No data found"），这里只做保守抽取：
    行内出现 yyyy-mm-dd 且有一个数字，就取该数字。
    """
    out: dict = {}
    for raw in str(text).splitlines():
        line = raw.strip()
        m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", line)
        if not m:
            continue
        day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        nums = re.findall(r"(\d+(?:\.\d+)?)", line[m.end():])
        if nums:
            out[day] = float(nums[0])
    return out


def _hours(val) -> float | None:
    """'7h 57min' -> 7.95"""
    if not val:
        return None
    h = re.search(r"(\d+)\s*h", val)
    m = re.search(r"(\d+)\s*min", val)
    if not h and not m:
        return ing._num(val)
    return round((int(h.group(1)) if h else 0) + (int(m.group(1)) / 60 if m else 0), 2)


def _minutes(val) -> float | None:
    if not val:
        return None
    h = re.search(r"(\d+)\s*h", val)
    m = re.search(r"(\d+)\s*min", val)
    if not h and not m:
        return ing._num(val)
    return round((int(h.group(1)) if h else 0) * 60 + (int(m.group(1)) if m else 0), 1)


def _pct(val) -> float | None:
    if not val:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", val)
    return float(m.group(1)) if m else ing._num(val)


def _iso(part: str) -> str | None:
    """'2026-10-15 00:28' -> '2026-10-15T00:28:00+08:00'"""
    try:
        dt = datetime.strptime(part.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return dt.astimezone().isoformat(timespec="seconds")


# ── 同步主流程 ────────────────────────────────────────────

def _windows(start: date, end: date, span: int) -> list[tuple[date, date]]:
    """把区间切成不超过 span 天的窗口（COROS 多数健康工具单次上限 7 天）。"""
    out, cur = [], start
    while cur <= end:
        stop = min(cur + timedelta(days=span - 1), end)
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


def sync_activities(conn, token, start: date, end: date, notes: list[str],
                    raw_dir: Path = RAW_DIR) -> dict:
    total = inserted = 0
    details = []
    for w_start, w_end in _windows(start, end, 7):
        raw = cc.structured(cc.call_tool(
            "querySportRecords",
            {"startDate": w_start.strftime("%Y%m%d"),
             "endDate": w_end.strftime("%Y%m%d"), "limit": 50}, token))
        ing._archive(raw_dir, "querySportRecords",
                     raw, f"{w_start:%Y%m%d}_{w_end:%Y%m%d}")
        records = ing.parse_record_text(raw)
        total += len(records)
        for rec in records:
            lid, st = rec["labelId"], rec.get("sportType")
            if st is None:
                notes.append(f"{lid}: 记录缺 sportType，跳过详情（COROS 要求二者成对使用）")
                continue
            detail_raw = cc.structured(cc.call_tool(
                "getActivityDetail", {"labelId": lid, "sportType": st}, token))
            laps_raw = cc.structured(cc.call_tool(
                "queryActivityLapData", {"labelId": lid, "sportType": st}, token))
            f1 = ing._archive(raw_dir, "getActivityDetail", detail_raw, lid)
            f2 = ing._archive(raw_dir, "queryActivityLapData", laps_raw, lid)
            detail = ing.parse_detail_text(detail_raw)
            aid = ing.upsert_activity(conn, rec, detail, laps_raw, [f1, f2])
            splits = ing.write_splits(conn, aid, laps_raw, rec.get("start_ts"))
            inserted += 1
            details.append({"id": aid, "external_id": lid, "splits": splits})
        conn.commit()
    return {"records": total, "inserted": inserted, "activities": details}


def sync_health(conn, token, end: date, notes: list[str],
                raw_dir: Path = RAW_DIR) -> dict:
    """每日生理。无设备时全部为空——此时只记 notes，不写任何行。"""
    result = {"sleep_days": 0, "hrv_days": 0, "rhr_days": 0,
              "stress_days": 0, "avg_hr_days": 0, "empty": []}
    days = 7  # COROS 多数窗口上限

    # 睡眠（可指定日期区间）
    start = (end - timedelta(days=days - 1)).isoformat()
    raw = cc.structured(cc.call_tool(
        "querySleepOverview",
        {"startDate": start.replace("-", ""), "endDate": end.strftime("%Y%m%d")}, token))
    ing._archive(raw_dir, "querySleepOverview", raw, _stamp(end.isoformat()))
    if _is_empty(raw):
        result["empty"].append("sleep")
        notes.append("睡眠：COROS 未返回数据（未绑定设备时正常；COROS 不支持外部导入睡眠）")
    else:
        for day, vals in parse_sleep_overview(raw).items():
            if ing.upsert_daily(conn, day, vals):
                result["sleep_days"] += 1
            stages_derived = vals.get("stages_derived_from_pct")
            if stages_derived:
                notes.append(f"{day}: 深/浅/REM 由 COROS 百分比 × 主睡眠时长折算"
                             "（COROS 不提供阶段时间线，sleep_stages 留空）")

    # HRV（可指定日期区间，上限 7 天）
    raw = cc.structured(cc.call_tool(
        "querySleepHrv",
        {"startDate": start.replace("-", ""), "endDate": end.strftime("%Y%m%d")}, token))
    ing._archive(raw_dir, "querySleepHrv", raw, _stamp(end.isoformat()))
    if _is_empty(raw):
        result["empty"].append("hrv")
        notes.append("HRV：无数据")
    else:
        for day, v in parse_hrv(raw).items():
            if ing.upsert_daily(conn, day, {"hrv_last_night": v}):
                result["hrv_days"] += 1

    # 其余只有「最近 N 天」窗口，不支持日期区间
    for tool, key, label in (("queryRestingHeartRate", "resting_hr", "rhr"),
                             ("queryStressLevel", "stress_daily_avg", "stress"),
                             ("queryAvgHeartRate", "_avg_hr", "avg_hr")):
        raw = cc.structured(cc.call_tool(tool, {"days": days}, token))
        ing._archive(raw_dir, tool, raw, _stamp(end.isoformat()))
        if _is_empty(raw):
            result["empty"].append(label)
            notes.append(f"{label}：无数据（{tool} 仅支持「最近 N 天」，不支持日期区间）")
            continue
        for day, v in parse_daily_number(raw).items():
            if key == "_avg_hr":
                continue  # 平均心率不进 daily_metrics（列不存在），保留在存档里
            if ing.upsert_daily(conn, day, {key: v}):
                result[f"{label}_days"] += 1
    conn.commit()
    return result


def recompute_engine(conn, cfg, days: list[str], notes: list[str]) -> dict:
    """对涉及到的日期重算 CTL/ACWR/准备度（失败不影响主流程）。"""
    from core import daily as dailymod
    ok, failed = 0, []
    for day in sorted(set(days)):
        try:
            dailymod.compute_engine_daily(conn, cfg, day)
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed.append(f"{day}: {type(e).__name__}: {e}")
    if failed:
        notes.append("引擎重算部分失败：" + "; ".join(failed[:3]))
    return {"recomputed": ok, "failed": len(failed)}
