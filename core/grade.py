"""坡度-配速分析：爬升分段、平均坡度、坡度调整配速（GAP）。

核心方法
--------
1. **爬升/下降统计**用阈值累计法（threshold accumulation）：把相邻海拔差累加，
   累计量超过 `MIN_CLIMB_M` 才确认并计入，否则可能是气压计/GPS 漂移噪声直接丢弃。
   这是 GPX/运动分析里的通行做法，可避免总爬升被噪声放大数倍。
2. **坡度调整配速（GAP）** 用 Minetti et al. (2002) 跑步代谢代价方程：
        Cr(i) = 155.4·i⁵ − 30.4·i⁴ − 43.3·i³ + 46.3·i² + 19.5·i + 3.6   [J·kg⁻¹·m⁻¹]
   i 为坡度小数（上坡为正），平地 Cr(0) = 3.6。
   等效平地配速的推导（能量速率守恒）：
        Cr(i)·v_actual = Cr(0)·v_flat   →   v_flat = v_actual · Cr(i)/Cr(0)
        配速是速度的倒数，因此   GAP = 实际配速 ÷ (Cr(i)/Cr(0))
   即 ratio = Cr(i)/Cr(0) 是「代价比」，GAP = 实际配速 / ratio。
   上坡 ratio>1 → GAP 比实际配速快（同样用力在平路能跑更快）；
   缓下坡 ratio<1 → GAP 比实际配速慢。
   含义：扣掉坡度影响后再比较，才能公平判断「同样用力跑得快不快」。

参考：Minetti AE, et al. Energy cost of walking and running at extreme uphill and
downhill slopes. J Appl Physiol. 2002;93(3):1039-46.
"""
from __future__ import annotations

from typing import Optional

# 计入爬升的最小累计高度（米）：低于此视为噪声。
# FIT 的 enhanced_altitude 是气压高度，分辨率 0.1 m、采样约 4 m，
# 实测 1–1.5 m 阈值与 Garmin 官方 total_ascent 最接近（3 m 会低估 20–40%）。
# 可在 config.toml 的 [grade] min_climb_m 覆盖。
MIN_CLIMB_M = 1.5
# 坡度截断（小数）：超出范围 Minetti 方程外推不可靠
GRADE_CLAMP = 0.45
# 计算坡度的滑动窗口长度（米）。FIT 采样约每 4 m 一个点、海拔分辨率 0.1 m，
# 直接逐点算坡度会被量化噪声放大（1 m / 20 m = 5% 的假坡），因此用窗口平均坡度。
GRADE_WINDOW_M = 50.0
# Minetti 方程在 0 坡度的代谢代价
CR_FLAT = 3.6

SPLIT_ELEV_COLUMNS = (
    ("elevation_loss_m", "REAL"),
    ("net_elevation_m", "REAL"),
    ("avg_grade_pct", "REAL"),
    ("max_grade_pct", "REAL"),
    ("gap_pace_min_km", "REAL"),
)


def configured_min_climb(cfg: dict | None = None) -> float:
    """从配置读取爬升阈值，缺省用 MIN_CLIMB_M。"""
    if not cfg:
        return MIN_CLIMB_M
    try:
        v = (cfg.get("grade") or {}).get("min_climb_m")
        return float(v) if v is not None else MIN_CLIMB_M
    except (TypeError, ValueError):
        return MIN_CLIMB_M


def ensure_columns(conn) -> None:
    """幂等追加坡度相关列到 activity_split_detail。"""
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(activity_split_detail)")}
    except Exception:  # noqa: BLE001
        return
    if not have:
        return
    for name, ddl in SPLIT_ELEV_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE activity_split_detail ADD COLUMN {name} {ddl}")


def minetti_cost(grade: float) -> float:
    """Minetti 跑步代谢代价 Cr(i)，单位 J/(kg·m)。grade 为小数坡度。"""
    i = max(-GRADE_CLAMP, min(GRADE_CLAMP, grade))
    return (155.4 * i ** 5 - 30.4 * i ** 4 - 43.3 * i ** 3
            + 46.3 * i ** 2 + 19.5 * i + 3.6)


def _median_filter(vals: list[float], k: int = 5) -> list[float]:
    """k 点中值滤波：去掉海拔序列里的单点尖峰（GPS/气压计跳变）。

    窗口在首尾用**边界值复制**补齐，否则端点会被窗口内的邻点取代，
    使线性爬升的净爬升被系统性低估（真实数据里每段都会吃这个误差）。
    """
    if len(vals) < 3:
        return list(vals)
    half = k // 2
    n = len(vals)
    out = []
    for i in range(n):
        lo, hi = i - half, i + half + 1
        win = ([vals[0]] * max(0, -lo) + vals[max(0, lo):min(n, hi)]
               + [vals[-1]] * max(0, hi - n))
        win.sort()
        out.append(win[len(win) // 2])
    return out


def _accumulate(alts: list[float], sign: float,
                min_climb: float = MIN_CLIMB_M) -> float:
    """阈值累计法算爬升（sign=+1）或下降（sign=-1）的总量。"""
    total = 0.0
    accum = 0.0
    prev = alts[0]
    for a in alts[1:]:
        accum += (a - prev) * sign
        prev = a
        if accum >= min_climb:
            total += accum
            accum = 0.0
        elif accum <= -min_climb:
            accum = 0.0
    return total


def segment_stats(pts: list[tuple[float, float]],
                  min_climb: float = MIN_CLIMB_M) -> dict:
    """由 [(累计距离m, 海拔m)] 序列算一段的爬升/下降/坡度与代价比。

    返回的 cost_ratio 就是「距离加权平均 Cr / Cr(0)」，直接乘实际配速即得 GAP。
    """
    clean = [(d, a) for d, a in pts if d is not None and a is not None]
    if len(clean) < 2:
        return {"gain_m": None, "loss_m": None, "net_m": None,
                "avg_grade_pct": None, "max_grade_pct": None, "cost_ratio": None}
    dist = clean[-1][0] - clean[0][0]
    alts = _median_filter([a for _, a in clean], 5)
    clean = [(d, a) for (d, _), a in zip(clean, alts)]
    net = alts[-1] - alts[0]

    gain = _accumulate(alts, 1.0, min_climb)
    loss = _accumulate(alts, -1.0, min_climb)

    # 滑动窗口坡度：对每个点找前方约 GRADE_WINDOW_M 处的点，用两者的高度差算坡度
    grades = []   # [(覆盖距离m, 坡度)]
    j = 0
    n = len(clean)
    for i in range(n - 1):
        if j < i:
            j = i
        while j + 1 < n and clean[j][0] - clean[i][0] < GRADE_WINDOW_M:
            j += 1
        dd = clean[j][0] - clean[i][0]
        if dd >= GRADE_WINDOW_M * 0.5:
            w = min(dd, GRADE_WINDOW_M)
            grades.append((w, (clean[j][1] - clean[i][1]) / dd))
    max_grade = max((abs(g) for _, g in grades), default=None)

    cost = 0.0
    dsum = 0.0
    for w, g in grades:
        cost += minetti_cost(g) * w
        dsum += w
    cost_ratio = (cost / dsum / CR_FLAT) if dsum > 0 else None

    return {
        "gain_m": round(gain, 1),
        "loss_m": round(loss, 1),
        "net_m": round(net, 1),
        "avg_grade_pct": round(net / dist * 100, 2) if dist and dist > 0 else None,
        "max_grade_pct": round(max_grade * 100, 2) if max_grade is not None else None,
        "cost_ratio": round(cost_ratio, 4) if cost_ratio else None,
    }


def grade_adjusted_pace(pace_min_km: Optional[float],
                        cost_ratio: Optional[float]) -> Optional[float]:
    """等效平地配速（min/km）= 实际配速 ÷ 代价比。

    cost_ratio = Cr(坡度)/Cr(平地)，上坡 >1。由能量速率守恒推出（见模块 docstring）。
    """
    if not pace_min_km or pace_min_km <= 0 or not cost_ratio:
        return None
    return round(pace_min_km / cost_ratio, 3)


def grade_adjusted_pace_by_grade(pace_min_km: Optional[float],
                                 avg_grade: Optional[float]) -> Optional[float]:
    """只有平均坡度、没有逐点数据时的近似 GAP。"""
    if not pace_min_km or pace_min_km <= 0 or avg_grade is None:
        return None
    return round(pace_min_km / (minetti_cost(avg_grade) / CR_FLAT), 3)


# ── 活动级坡度-配速分析 ────────────────────────────────────────

def _ols(points: list[tuple[float, float]]) -> Optional[dict]:
    """一元线性回归 y = a + b·x，返回斜率与 R²。"""
    n = len(points)
    if n < 3:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    if sxx <= 1e-9:
        return None
    slope = sxy / sxx
    r2 = (sxy ** 2) / (sxx * syy) if syy > 1e-9 else None
    return {"slope": slope, "intercept": my - slope * mx,
            "r2": r2, "n": n}


def analyze_activity(conn, activity_id: int, min_split_m: float = 400.0) -> dict:
    """对单个活动做坡度-配速分析。

    输出：
      - splits：每段 距离/爬升/下降/净爬升/平均坡度/最大坡度/实际配速/GAP
      - grade_pace_response：坡度-配速响应（每 +1% 坡度配速变化多少秒/km）
      - summary：总爬升/下降/爬升密度/GAP 与实际配速差
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT seq, distance_m, duration_s, pace_min_km, avg_hr, max_hr, "
        "elevation_gain_m, elevation_loss_m, net_elevation_m, avg_grade_pct, "
        "max_grade_pct, gap_pace_min_km, start_time, end_time "
        "FROM activity_split_detail WHERE activity_id=? AND split_type='km' ORDER BY seq",
        (activity_id,)).fetchall()]
    if not rows:
        return {"splits": [], "message": "该活动没有每公里分段，无法做坡度分析"}

    for r in rows:
        if r.get("gap_pace_min_km") is None:
            r["gap_pace_min_km"] = grade_adjusted_pace_by_grade(
                r.get("pace_min_km"), (r["avg_grade_pct"] / 100.0)
                if r.get("avg_grade_pct") is not None else None)

    # 坡度-配速响应：只用足够长的段，避免短尾段噪声
    pts = [(r["avg_grade_pct"], r["pace_min_km"] * 60) for r in rows
           if r.get("avg_grade_pct") is not None and r.get("pace_min_km")
           and (r.get("distance_m") or 0) >= min_split_m]
    fit = _ols(pts)
    response = None
    if fit:
        grades_seen = [p[0] for p in pts]
        response = {
            "samples": fit["n"],
            "sec_per_1pct_grade": round(fit["slope"], 2),
            "r2": round(fit["r2"], 3) if fit["r2"] is not None else None,
            "grade_span_pct": round(max(grades_seen) - min(grades_seen), 2),
            "flat_pace_est": _fmt_pace(fit["intercept"] / 60),
            "note": "sec_per_1pct_grade：每 +1% 坡度配速慢多少秒/km。正常为正（上坡更慢），"
                    "典型 8–15 s；越小说明爬坡能力越强。负值通常意味着该次跑有间歇/"
                    "后程加速等非坡度因素。r2 是拟合优度：低于 0.15 说明本次配速变化"
                    "主要不是坡度造成的，不要用它评价爬坡能力。grade_span_pct 是本次"
                    "各段平均坡度的跨度，跨度过小（<1.5%）时斜率不可信。",
        }

    total_gain = sum(r.get("elevation_gain_m") or 0 for r in rows)
    total_loss = sum(r.get("elevation_loss_m") or 0 for r in rows)
    d_sum = sum(r.get("distance_m") or 0 for r in rows)
    t_sum = sum(r.get("duration_s") or 0 for r in rows)
    actual = (t_sum / 60) / (d_sum / 1000) if d_sum and t_sum else None
    gap_num = sum((r.get("gap_pace_min_km") or 0) * (r.get("distance_m") or 0) for r in rows)
    gap = gap_num / d_sum if d_sum and gap_num else None

    return {
        "splits": rows,
        "grade_pace_response": response,
        "summary": {
            "distance_km": round(d_sum / 1000, 3) if d_sum else None,
            "elevation_gain_m": round(total_gain, 1),
            "elevation_loss_m": round(total_loss, 1),
            "net_elevation_m": round(total_gain - total_loss, 1),
            "avg_grade_pct": round((total_gain - total_loss) / d_sum * 100, 2)
                             if d_sum else None,
            "climb_density_m_per_km": round(total_gain / (d_sum / 1000), 1) if d_sum else None,
            "actual_pace": _fmt_pace(actual),
            "gap_pace": _fmt_pace(gap),
            "gap_vs_actual_sec": round((actual - gap) * 60, 1)
                                 if actual and gap else None,
        },
    }


def _fmt_pace(pace_min_km: Optional[float]) -> Optional[str]:
    if not pace_min_km or pace_min_km <= 0:
        return None
    sec = int(round(float(pace_min_km) * 60))
    return f"{sec // 60}:{sec % 60:02d}"
