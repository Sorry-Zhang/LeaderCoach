"""睡眠状态评分（SSHI）：把睡眠折算成「今天身体恢复得如何」，供训练安排使用。

设计原则
--------
1. 目标不是预测睡眠，而是**用昨晚的睡眠推断当天状态**，再决定当天练什么。
2. 全部以**个人基线**为准，不套人群阈值；个人睡眠需要量由运动员在初始化时设定
   （`athlete_profile.sleep_need_h`，默认 7.5 h）。AASM 的 7–9 h 只作为兜底下限。
3. 分期（深/浅/REM）误差大，只用于**夜间稳定与解释**，不单独作为扣分项，也不做
   「今晚 REM 少所以降强度」这类判断。
4. 主观感受保留独立权重；未填写时可由客观项推算默认值并标记 `derived`，
   用户提出异议再覆盖。

六个维度（0–100，按权重合成 SSHI）
--------------------------------
- duration   时长 vs 个人需要量（含 7 天睡眠债）
- regularity 规律性：优先用睡眠阶段算 SRI，缺阶段时退化为入睡/起床时刻的离散度
- timing     睡眠中点相对个人 28 天中点的偏移
- efficiency 睡眠效率 = 总睡眠 / 卧床
- stability  夜间稳定：WASO、觉醒次数、阶段切换频率
- subjective 主观睡眠感受（1–5），可缺省

合成结果写入 `garmin_metrics`（metric=`sleep_state_score` 等），便于算个人百分位。
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

W_DEFAULT = {
    "duration": 0.25,
    "regularity": 0.25,
    "timing": 0.10,
    "efficiency": 0.15,
    "stability": 0.15,
    "subjective": 0.10,
}

# 睡眠阶段时间线的时间分辨率（Garmin 为 5 分钟）
EPOCH_MIN = 5
EPOCHS_PER_DAY = 24 * 60 // EPOCH_MIN


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _interp(xs, ys, x):
    """线性内插；x 超出范围时取端点。"""
    if x is None:
        return None
    if x <= xs[0]:
        return float(ys[0])
    if x >= xs[-1]:
        return float(ys[-1])
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            span = xs[i + 1] - xs[i]
            if span == 0:
                return float(ys[i])
            return ys[i] + (ys[i + 1] - ys[i]) * (x - xs[i]) / span
    return None


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


# ── 个人需要量与基线 ─────────────────────────────────────────

def personal_need_h(conn, cfg: dict) -> tuple[float, str]:
    """个人每晚睡眠需要量：档案 > 配置 > 7.5 h 默认。"""
    row = conn.execute("SELECT sleep_need_h FROM athlete_profile WHERE id=1").fetchone()
    if row and _num(row["sleep_need_h"]):
        return float(row["sleep_need_h"]), "athlete_profile"
    cfg_need = _num((cfg.get("sleep") or {}).get("personal_need_h"))
    if cfg_need:
        return cfg_need, "config"
    return 7.5, "default"


def _midpoint(day: str, start: str, end: str) -> float | None:
    """睡眠中点，折算成「相对当天 18:00 的分钟数」，跨午夜也连续。"""
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    if e <= s:
        e += timedelta(days=1)
    mid = s + (e - s) / 2
    base = datetime.fromisoformat(day + "T18:00:00").replace(tzinfo=mid.tzinfo)
    return (mid - base).total_seconds() / 60.0


def _recent_midpoints(conn, day: str, days: int = 28) -> list[float]:
    start = (date.fromisoformat(day) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT date, sleep_start, sleep_end FROM daily_metrics "
        "WHERE date >= ? AND date <= ? AND sleep_start IS NOT NULL AND sleep_end IS NOT NULL "
        "ORDER BY date", (start, day)).fetchall()
    out = []
    for r in rows:
        m = _midpoint(r["date"], r["sleep_start"], r["sleep_end"])
        if m is not None:
            out.append(m)
    return out


def _recent_hours(conn, day: str, days: int = 28) -> list[float]:
    start = (date.fromisoformat(day) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT sleep_hours FROM daily_metrics WHERE date >= ? AND date <= ? "
        "AND sleep_hours IS NOT NULL ORDER BY date", (start, day)).fetchall()
    return [float(r["sleep_hours"]) for r in rows]


# ── 睡眠阶段 → 片段/时段 ─────────────────────────────────────

def _stage_rows(conn, day: str) -> list:
    try:
        return conn.execute(
            "SELECT stage, start_local, end_local, seconds FROM sleep_stages "
            "WHERE date=? ORDER BY seq", (day,)).fetchall()
    except Exception:  # noqa: BLE001
        return []


def _asleep_epochs(rows) -> set[int]:
    """把阶段时间线映射成 24 h 内的 5 分钟 epoch 集合（awake 之外算睡着）。"""
    epochs: set[int] = set()
    for r in rows:
        if (r["stage"] or "").lower() == "awake":
            continue
        try:
            s = datetime.fromisoformat(r["start_local"])
            e = datetime.fromisoformat(r["end_local"])
        except (TypeError, ValueError):
            continue
        cur = s
        while cur < e:
            idx = int((cur.hour * 60 + cur.minute) // EPOCH_MIN) % EPOCHS_PER_DAY
            epochs.add(idx)
            cur += timedelta(minutes=EPOCH_MIN)
    return epochs


def sleep_regularity_index(conn, day: str) -> float | None:
    """SRI：相邻两天在同一时刻的睡眠状态一致比例。

    每 5 分钟一个 epoch，一致记 +1、不一致记 −1，均值映射到 0–100
    （随机约 50，完全一致 100；文献中健康成人中位数约 80）。
    """
    cur = _asleep_epochs(_stage_rows(conn, day))
    prev_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    prev = _asleep_epochs(_stage_rows(conn, prev_day))
    if not cur or not prev:
        return None
    agree = sum(1 for i in range(EPOCHS_PER_DAY) if (i in cur) == (i in prev))
    mismatch = EPOCHS_PER_DAY - agree
    return round(50.0 * (1 + (agree - mismatch) / EPOCHS_PER_DAY), 1)


def _fragmentation(rows) -> float | None:
    """每小时阶段切换次数。"""
    if len(rows) < 2:
        return None
    total = sum(_num(r["seconds"]) or 0 for r in rows)
    if total <= 0:
        return None
    return round((len(rows) - 1) / (total / 3600.0), 2)


# ── 各维度打分 ──────────────────────────────────────────────

def score_duration(hours, need_h, debt_7d=None) -> float | None:
    """以个人需要量为中心；不足按时长扣分，超出 1.5 h 后轻微扣分。"""
    h = _num(hours)
    if h is None:
        return None
    deficit = need_h - h
    if deficit > 0:
        score = 100 - 40 * deficit
    else:
        surplus = -deficit
        score = 100 if surplus <= 1.5 else 100 - 10 * (surplus - 1.5)
    # 7 天累计睡眠债：超过 3 h 后额外扣分，最多扣 15
    d7 = _num(debt_7d)
    if d7 is not None and d7 > 3:
        score -= min(15.0, (d7 - 3) * 3)
    return _clamp(score)


def score_regularity(sri=None, midpoint_sd_h=None) -> float | None:
    if sri is not None:
        return _clamp(_interp([50, 70, 85, 95], [0, 50, 85, 100], sri))
    if midpoint_sd_h is not None:
        return _clamp(100 - 20 * midpoint_sd_h)
    return None


def score_timing(mid_dev_h) -> float | None:
    if mid_dev_h is None:
        return None
    return _clamp(_interp([0, 0.5, 1.0, 2.0], [100, 100, 80, 40], abs(mid_dev_h)))


def score_efficiency(eff_pct) -> float | None:
    if eff_pct is None:
        return None
    return _clamp(_interp([70, 80, 85, 90, 95, 100], [30, 50, 65, 80, 95, 100], eff_pct))


def score_stability(waso_min=None, awake_segments=None, frag=None) -> float | None:
    parts = []
    if waso_min is not None:
        parts.append(_interp([0, 10, 20, 40, 60], [100, 95, 80, 55, 35], waso_min))
    if awake_segments is not None:
        parts.append(_interp([0, 1, 2, 4], [100, 90, 75, 50], awake_segments))
    if frag is not None:
        parts.append(_interp([1, 2, 3, 5], [100, 85, 65, 35], frag))
    return _clamp(sum(parts) / len(parts)) if parts else None


def score_subjective(sleep_feel) -> float | None:
    v = _num(sleep_feel)
    if v is None:
        return None
    return _clamp(_interp([1, 2, 3, 4, 5], [40, 55, 70, 85, 100], v))


# ── 主入口 ─────────────────────────────────────────────────

def evaluate(conn, cfg: dict, day: str, sleep_feel=None) -> dict:
    """计算某天的睡眠状态分。缺数据时返回 score=None，并说明缺哪一维。"""
    s_cfg = cfg.get("sleep") or {}
    weights = {**W_DEFAULT, **(s_cfg.get("weights") or {})}
    need_h, need_source = personal_need_h(conn, cfg)

    dm = conn.execute(
        "SELECT sleep_hours, sleep_deep_h, sleep_light_h, sleep_rem_h, sleep_awake_h, "
        "sleep_start, sleep_end FROM daily_metrics WHERE date=?", (day,)).fetchone()
    if dm is None:
        return {"date": day, "score": None, "confidence": "low", "dimensions": {},
                "personal_need_h": need_h, "need_source": need_source,
                "notes": ["当天没有睡眠记录"]}

    hours = _num(dm["sleep_hours"])
    awake_h = _num(dm["sleep_awake_h"]) or 0.0
    bed_h = (hours + awake_h) if hours is not None else None
    eff = round(hours / bed_h * 100, 1) if hours and bed_h and bed_h > 0 else None

    rows = _stage_rows(conn, day)
    sri = sleep_regularity_index(conn, day)
    mids = _recent_midpoints(conn, day, int(s_cfg.get("baseline_days", 28) or 28))
    mid_dev_h = None
    mid_sd_h = None
    if len(mids) >= 5:
        med = statistics.median(mids)
        mid_sd_h = round(statistics.pstdev(mids) / 60.0, 2)
        today_mid = _midpoint(day, dm["sleep_start"], dm["sleep_end"]) if dm["sleep_start"] else None
        if today_mid is not None:
            mid_dev_h = round((today_mid - med) / 60.0, 2)

    # 7 天睡眠债（相对个人需要量）
    h7 = _recent_hours(conn, day, 7)
    debt_7d = round(max(0.0, need_h * len(h7) - sum(h7)), 2) if h7 else None

    waso_raw = _num(dm["sleep_awake_h"])
    waso_min = round(waso_raw * 60, 1) if waso_raw is not None else None
    awake_segments = sum(1 for r in rows if (r["stage"] or "").lower() == "awake") or None
    frag = _fragmentation(rows)

    subj = score_subjective(sleep_feel)
    derived_subjective = False
    dims = {
        "duration": score_duration(hours, need_h, debt_7d),
        "regularity": score_regularity(sri, mid_sd_h),
        "timing": score_timing(mid_dev_h),
        "efficiency": score_efficiency(eff),
        "stability": score_stability(waso_min, awake_segments, frag),
        "subjective": subj,
    }
    if subj is None:
        # 未填主观时按客观项推算默认值（README 的 derived 约定）
        base = [dims[k] for k in ("duration", "regularity", "efficiency") if dims[k] is not None]
        if base:
            dims["subjective"] = round(sum(base) / len(base), 1)
            derived_subjective = True

    usable = {k: v for k, v in dims.items() if v is not None}
    if not usable:
        return {"date": day, "score": None, "confidence": "low", "dimensions": {},
                "personal_need_h": need_h, "need_source": need_source,
                "notes": ["睡眠数据不足，无法评分"]}

    total_w = sum(weights.get(k, 0) for k in usable)
    score = sum(usable[k] * weights.get(k, 0) for k in usable) / total_w if total_w else None
    n = len(usable)
    confidence = "high" if n >= 6 else ("medium" if n >= 4 else "low")

    notes = []
    if hours is not None and hours < (s_cfg.get("aasm_floor_h") or 7.0):
        notes.append(f"低于 AASM 成人下限 7 h（你设定 {need_h} h）")
    if derived_subjective:
        notes.append("主观项由客观项推算（derived），可在提出异议时覆盖")
    if sri is None and rows:
        notes.append("阶段时间线不足两天，规律性改用睡眠中点离散度")

    return {
        "date": day,
        "score": round(score, 1) if score is not None else None,
        "confidence": confidence,
        "dimensions": {k: (round(v, 1) if v is not None else None) for k, v in dims.items()},
        "weights": {k: weights.get(k, 0) for k in dims},
        "personal_need_h": need_h,
        "need_source": need_source,
        "hours": hours,
        "efficiency_pct": eff,
        "debt_7d_h": debt_7d,
        "sri": sri,
        "midpoint_dev_h": mid_dev_h,
        "midpoint_sd_h": mid_sd_h,
        "waso_min": waso_min,
        "awake_segments": awake_segments,
        "fragmentation_per_h": frag,
        "subjective_derived": derived_subjective,
        "notes": notes,
    }


def percentile(conn, day: str, score: float | None, days: int = 28) -> float | None:
    """今天的睡眠分在过去 N 天里的百分位（越低越差）。"""
    if score is None:
        return None
    start = (date.fromisoformat(day) - timedelta(days=days)).isoformat()
    try:
        vals = [r[0] for r in conn.execute(
            "SELECT value FROM garmin_metrics WHERE metric='sleep_state_score' "
            "AND date >= ? AND date < ? ORDER BY date", (start, day)).fetchall()]
    except Exception:  # noqa: BLE001
        return None
    if len(vals) < 7:
        return None
    below = sum(1 for v in vals if v < score)
    return round(below / len(vals) * 100, 0)


def _quantile(vals, p):
    vs = sorted(vals)
    if not vs:
        return None
    k = (len(vs) - 1) * p
    f, c = int(k), min(int(k) + 1, len(vs) - 1)
    return vs[f] + (vs[c] - vs[f]) * (k - f)


def g8_status(conn, cfg: dict, day: str, score: float | None) -> dict:
    """G8（睡眠预测项）的观察/拦截状态。

    触发条件：score < min(个人 28 天 p10, 绝对下限 60)。
    - 个人 p10 表达“相对你自己的基线偏差”；
    - 绝对下限防止把长期睡不好的人“正常化”（p10 太低时仍要求 < 60）。
    默认 mode=observe：只记录，不取消质量课；改成 enforce 才真正拦截。
    """
    s_cfg = cfg.get("sleep") or {}
    mode = str(s_cfg.get("g8_mode", "observe")).lower()
    floor = _num(s_cfg.get("g8_absolute_floor"))
    floor = 60.0 if floor is None else floor
    window = int(s_cfg.get("g8_p10_window_days") or 28)
    min_days = int(s_cfg.get("g8_p10_min_days") or 7)
    use_p10 = bool(s_cfg.get("g8_use_personal_p10", True))

    start = (date.fromisoformat(day) - timedelta(days=window)).isoformat()
    try:
        hist = [r[0] for r in conn.execute(
            "SELECT value FROM garmin_metrics WHERE metric='sleep_state_score' "
            "AND date >= ? AND date < ? ORDER BY date", (start, day)).fetchall()]
    except Exception:  # noqa: BLE001
        hist = []
    p10 = _quantile(hist, 0.10) if (use_p10 and len(hist) >= min_days) else None
    threshold = min(x for x in (p10, floor) if x is not None) if (p10 is not None or floor is not None) else None
    triggered = bool(score is not None and threshold is not None and score < threshold)
    return {
        "mode": mode,
        "p10": round(p10, 1) if p10 is not None else None,
        "absolute_floor": floor,
        "threshold": round(threshold, 1) if threshold is not None else None,
        "history_days": len(hist),
        "score": score,
        "triggered": triggered,
        # observe 模式下即使条件满足也不改变训练安排
        "enforced": bool(triggered and mode == "enforce"),
    }
