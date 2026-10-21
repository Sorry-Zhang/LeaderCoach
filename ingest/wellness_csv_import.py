from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path

_SENTINEL = {"", "--", "-", "none", "nan", "null"}

def _col(header, *keys) -> str | None:
    for h in header:
        hl = (h or "").lower()
        if all(k in hl for k in keys):
            return h
    return None

def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s.lower() in _SENTINEL:
        return None
    try:
        return float(s)
    except ValueError:
        return None

def _hours(v):
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in _SENTINEL:
        return None
    low = s.lower()
    if "h" in low or re.search(r"\d+\s*m", low):
        h = re.search(r"(\d+)\s*h", low)
        m = re.search(r"(\d+)\s*m", low)
        hh = int(h.group(1)) if h else 0
        mm = int(m.group(1)) if m else 0
        return round(hh + mm / 60, 2) if (hh or mm) else None
    x = _num(s)
    if x is None:
        return None
    if x > 48:
        return round(x / 60, 2)
    return round(x, 2)

def _date(v):
    s = str(v or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None

def _parse_csv(path: Path) -> dict:
    out: dict[str, dict] = {}
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        c_date = _col(header, "date") or _col(header, "day") or _col(header, "time")
        if not c_date:
            return out
        c_sleep = _col(header, "sleep", "hour") or _col(header, "total", "sleep")
        c_deep = _col(header, "deep")
        c_light = _col(header, "light")
        c_rem = _col(header, "rem")
        c_awake = _col(header, "awake")
        c_hrv = _col(header, "hrv") or _col(header, "last night", "avg")
        c_rhr = _col(header, "resting")
        c_stress = _col(header, "stress")

        for row in reader:
            d = _date(row.get(c_date))
            if not d:
                continue
            r = out.setdefault(d, {"date": d})
            if c_sleep and r.get("sleep_hours") is None:
                r["sleep_hours"] = _hours(row.get(c_sleep))
            if c_deep and r.get("sleep_deep_h") is None:
                r["sleep_deep_h"] = _hours(row.get(c_deep))
            if c_light and r.get("sleep_light_h") is None:
                r["sleep_light_h"] = _hours(row.get(c_light))
            if c_rem and r.get("sleep_rem_h") is None:
                r["sleep_rem_h"] = _hours(row.get(c_rem))
            if c_awake and r.get("sleep_awake_h") is None:
                r["sleep_awake_h"] = _hours(row.get(c_awake))
            if c_hrv and r.get("hrv_last_night") is None:
                r["hrv_last_night"] = _num(row.get(c_hrv))
            if c_rhr and r.get("resting_hr") is None:
                r["resting_hr"] = _num(row.get(c_rhr))
            if c_stress and r.get("stress_daily_avg") is None:
                r["stress_daily_avg"] = _num(row.get(c_stress))
    return out

def _cn_md(v):
    m = re.match(r"\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", str(v or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None

def _parse_range_from_name(name: str):
    m = re.search(
        r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})\s*-\s*(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})", name)
    if not m:
        return None
    y1, m1, d1, y2, m2, d2 = (int(x) for x in m.groups())
    try:
        return date(y1, m1, d1), date(y2, m2, d2)
    except ValueError:
        return None

def _resolve_year(md, start: date, end: date) -> str | None:
    mo, dy = md
    for y in (start.year - 1, start.year, start.year + 1):
        try:
            d = date(y, mo, dy)
        except ValueError:
            continue
        if start <= d <= end:
            return d.isoformat()
    try:
        return date(start.year, mo, dy).isoformat()
    except ValueError:
        return None

def _ms(v):
    s = str(v or "").strip()
    if s in ("--", "-", ""):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None

def _parse_hrv_cn(path: Path, start: date, end: date) -> dict:
    out: dict[str, dict] = {}
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        c_date = _col(header, "日期") or _col(header, "date")
        c_hrv = _col(header, "夜间", "hrv") or _col(header, "hrv")
        c_base = _col(header, "基准线") or _col(header, "baseline")
        c_avg = _col(header, "7", "天") or _col(header, "平均")
        if not (c_date and c_hrv):
            return out
        for row in reader:
            md = _cn_md(row.get(c_date))
            if not md:
                continue
            d = _resolve_year(md, start, end)
            if not d:
                continue
            r = out.setdefault(d, {"date": d})
            hrv = _ms(row.get(c_hrv))
            if hrv is not None:
                r["hrv_last_night"] = hrv
            if c_avg:
                avg = _ms(row.get(c_avg))
                if avg is not None:
                    r["hrv_7d_avg"] = avg
            if c_base:
                nums = re.findall(r"(\d+(?:\.\d+)?)", str(row.get(c_base) or ""))
                if len(nums) >= 2:
                    r["hrv_baseline"] = round((float(nums[0]) + float(nums[1])) / 2, 1)
    return out

def import_wellness(folder, conn=None) -> dict:
    folder = Path(folder)
    files = sorted(folder.rglob("*.csv")) if folder.is_dir() else [folder]
    merged: dict[str, dict] = {}
    for f in files:
        try:
            parsed: dict = {}
            rng = _parse_range_from_name(f.name)
            if rng:
                parsed = _parse_hrv_cn(f, *rng)
            if not parsed:
                parsed = _parse_csv(f)
        except Exception:
            continue
        for d, row in parsed.items():
            cur = merged.setdefault(d, {})
            for k, v in row.items():
                if v is not None:
                    cur[k] = v
    if conn is not None:
        from ingest.health_fit_parser import write_health_rows
        write_health_rows(conn, merged.values())
    return merged

def _cn_duration_hours(v):
    s = str(v or "").strip()
    if s in ("--", "-", ""):
        return None
    h = re.search(r"(\d+)\s*时", s)
    m = re.search(r"(\d+)\s*分", s)
    hh = int(h.group(1)) if h else 0
    mm = int(m.group(1)) if m else 0
    return round(hh + mm / 60, 2) if (hh or mm) else None

def _week_range(v, year: int):
    s = str(v or "").strip()
    m = re.match(r"\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*-\s*"
                 r"(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日", s)
    if not m:
        return None
    m1, d1, m2, d2 = (int(x) if x else None for x in m.groups())
    m2 = m2 or m1
    try:
        start = date(year, m1, d1)
        end = date(year, m2, d2)
    except ValueError:
        return None
    if end < start:
        try:
            end = date(year + 1, m2, d2)
        except ValueError:
            return None
    return start.isoformat(), end.isoformat()

def _parse_sleep_weekly(path: Path, year: int) -> list:
    out = []
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return out

    header = rows[0]

    def col(*keys):
        for i, h in enumerate(header):
            hl = (h or "").lower()
            if all(k in hl for k in keys):
                return i
        return None

    i_date = col("日期") if col("日期") is not None else col("date")
    i_dur = col("持续时间") if col("持续时间") is not None else col("duration")
    i_score = col("得分") if col("得分") is not None else col("score")
    i_qual = col("质量") if col("质量") is not None else col("quality")
    if i_date is None or i_dur is None:
        return out

    for r in rows[1:]:
        if not r:
            continue
        extra = len(r) - len(header)

        def val(i):
            if i is None:
                return None
            j = i + (1 if (extra > 0 and i > i_date) else 0)
            return r[j] if 0 <= j < len(r) else None

        rng = _week_range(val(i_date), year)
        if not rng:
            continue
        hours = _cn_duration_hours(val(i_dur))
        if hours is None:
            continue
        out.append({
            "date_start": rng[0], "date_end": rng[1], "avg_hours": hours,
            "score": _num(val(i_score)) if i_score is not None else None,
            "quality": (val(i_qual) or "").strip() or None if i_qual is not None else None,
        })
    return out

def import_sleep_weekly(folder, conn=None) -> int:
    folder = Path(folder)
    files = sorted(folder.rglob("*.csv")) if folder.is_dir() else [folder]
    if conn is not None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sleep_weekly (date_start TEXT PRIMARY KEY, "
            "date_end TEXT, avg_hours REAL, score REAL, quality TEXT, source TEXT)")
    n = 0
    for f in files:
        m = re.search(r"(\d{4})\s*年", f.name)
        if not m:
            continue
        try:
            rows = _parse_sleep_weekly(f, int(m.group(1)))
        except Exception:
            continue
        if conn is None or not rows:
            continue
        for r in rows:
            conn.execute(
                "INSERT INTO sleep_weekly(date_start, date_end, avg_hours, score, quality, source) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(date_start) DO UPDATE SET "
                "date_end=excluded.date_end, avg_hours=excluded.avg_hours, "
                "score=excluded.score, quality=excluded.quality, source=excluded.source",
                (r["date_start"], r["date_end"], r["avg_hours"], r["score"],
                 r["quality"], f.name))
            n += 1
        conn.commit()
    return n
