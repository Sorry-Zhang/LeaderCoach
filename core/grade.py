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


# ── 分段地形分类 ─────────────────────────────────────────────
#
# 关键方法论：**整次活动的爬升密度不能用来判断分段坡度影响**。
# 5 m/km 的平均值完全可能是「3 段各爬 30 m、另 3 段各降 30 m」拼出来的，
# 而分段内的配速差异可以达到 40 s/km。必须逐段看地形。

# 判定为复合地形所需的段内最小上升/下降量（米）
MIXED_MIN_M = 8.0
# 净坡度判定阈值（小数）
GRADE_CLASS_TH = 0.005
# 段内起伏占比阈值（起伏量 / 距离）
ROLLING_TH = 0.010


def classify_terrain(gain: float | None, loss: float | None, net: float | None,
                     dist_m: float | None) -> str | None:
    """按段内爬升/下降/净变化给分段贴地形标签。

    - ``mixed``   复合地形：段内既有明显上升(≥8 m)又有明显下降(≥8 m)，
                  这种段用净坡度描述会自相抵消，必须单独标出
    - ``climb``   净上坡（平均坡度 ≥ 0.5%）
    - ``descent`` 净下坡（≤ −0.5%）
    - ``rolling`` 起伏但净变化小（段内起伏量/距离 ≥ 1%）
    - ``flat``    基本平路
    """
    if not dist_m or dist_m <= 0:
        return None
    g = gain or 0.0
    l = loss or 0.0
    n = net if net is not None else (g - l)
    if g >= MIXED_MIN_M and l >= MIXED_MIN_M:
        return "mixed"
    grade = n / dist_m
    if grade >= GRADE_CLASS_TH:
        return "climb"
    if grade <= -GRADE_CLASS_TH:
        return "descent"
    if (g + l) / dist_m >= ROLLING_TH:
        return "rolling"
    return "flat"


def asymmetry(splits: list[dict], min_dist_m: float = 300.0,
              min_grade_pct: float = 0.4) -> dict | None:
    """上坡惩罚 vs 下坡收益的不对称性。

    把上坡段和下坡段**分开回归**，而不是混在一起拟合一条直线。原因：
    跑步对坡度的响应高度不对称——上坡需要额外做功，下坡只能部分回收
    （离心制动 + 步频受限），合并拟合会把两者平均掉，R² 必然很低，
    并不代表「坡度没影响」。

    返回 uphill_penalty（每 +1% 慢多少秒/km）与 downhill_benefit
    （每 −1% 快多少秒/km），以及 asymmetry_ratio = 收益/惩罚（通常 < 1）。
    """
    up, down = [], []
    walk = 0
    for s in splits:
        g, pace = s.get("avg_grade_pct"), s.get("pace_min_km")
        if g is None or not pace or (s.get("distance_m") or 0) < min_dist_m:
            continue
        # 走路/补给段（>8:00/km）是极端离群点，一个点就能把斜率符号拉反
        if pace > MAX_RUN_PACE_MIN_KM:
            walk += 1
            continue
        if g >= min_grade_pct:
            up.append((g, pace * 60))
        elif g <= -min_grade_pct:
            down.append((g, pace * 60))

    out = {"climb_segments": len(up), "descent_segments": len(down)}
    if walk:
        out["excluded_walk_segments"] = walk
    # 单次活动样本太少时，拟合斜率符号都可能是反的（实测出现过 −50 s/1%），
    # 这类数值比不给更有害，所以直接不输出。
    if len(up) < 6 or len(down) < 6:
        out["insufficient_samples"] = (
            f"本次只有 {len(up)} 个上坡段、{len(down)} 个下坡段（各需 ≥6 才拟合）。"
            "单次的坡度信号会被训练意图、疲劳、补给停顿淹没，斜率不可信。"
            "评价爬坡能力请用 pooled_grade_response（跨活动池化）。")
        return out
    up_fit = _ols(up)
    down_fit = _ols(down)
    # 单次活动还要求拟合有最低解释力：跑山以外的活动坡度跨度小、配速波动也小，
    # 信噪比不足时斜率会是随机数（实测出现过 -3 s/1% 甚至 -50 s/1%）。
    def _ok(fit):
        return fit is not None and (fit["r2"] or 0) >= MIN_SINGLE_R2

    if up_fit and not _ok(up_fit):
        out["uphill_unreliable"] = (
            f"上坡拟合 r2 仅 {up_fit['r2']:.3f}（<{MIN_SINGLE_R2}），"
            "坡度跨度或配速波动太小，斜率不可信，已隐藏。")
        up_fit = None
    if down_fit and not _ok(down_fit):
        down_fit = None

    if up_fit:
        out["uphill_penalty_sec_per_1pct"] = round(up_fit["slope"], 2)
        out["uphill_r2"] = round(up_fit["r2"], 3) if up_fit["r2"] is not None else None
    if down_fit:
        # 下坡段的 x（坡度%）本身是负数。斜率 >0 表示 x 越接近 0（下坡越缓）配速越慢，
        # 等价于「下坡越陡配速越快」，所以收益直接取 +slope。
        out["downhill_benefit_sec_per_1pct"] = round(down_fit["slope"], 2)
        out["downhill_r2"] = round(down_fit["r2"], 3) if down_fit["r2"] is not None else None
    if (out.get("uphill_penalty_sec_per_1pct")
            and out.get("downhill_benefit_sec_per_1pct") is not None
            and out["uphill_penalty_sec_per_1pct"] > 0):
        out["asymmetry_ratio"] = round(
            out["downhill_benefit_sec_per_1pct"] / out["uphill_penalty_sec_per_1pct"], 2)
        out["note"] = ("asymmetry_ratio = 下坡收益 / 上坡惩罚。典型 0.4–0.7："
                       "下坡收不回上坡的损失。接近 1 说明上下坡效率对称；"
                       ">1 通常意味着下坡敢加速（技术好）或上坡偏保守。")
    return out


def terrain_breakdown(splits: list[dict]) -> dict:
    """按地形标签分组统计：段数、距离、爬升、实际配速、GAP。"""
    groups: dict = {}
    for s in splits:
        t = s.get("terrain")
        if not t:
            continue
        b = groups.setdefault(t, {"segments": 0, "distance_m": 0.0, "gain_m": 0.0,
                                  "loss_m": 0.0, "pace_num": 0.0, "gap_num": 0.0,
                                  "grades": []})
        b["segments"] += 1
        d = s.get("distance_m") or 0
        b["distance_m"] += d
        b["gain_m"] += s.get("elevation_gain_m") or 0
        b["loss_m"] += s.get("elevation_loss_m") or 0
        if s.get("pace_min_km"):
            b["pace_num"] += s["pace_min_km"] * d
        if s.get("gap_pace_min_km"):
            b["gap_num"] += s["gap_pace_min_km"] * d
        if s.get("avg_grade_pct") is not None:
            b["grades"].append(s["avg_grade_pct"])

    out = {}
    for t, b in groups.items():
        d = b["distance_m"]
        out[t] = {
            "segments": b["segments"],
            "distance_km": round(d / 1000, 2),
            "elevation_gain_m": round(b["gain_m"], 1),
            "elevation_loss_m": round(b["loss_m"], 1),
            "avg_grade_pct": round(sum(b["grades"]) / len(b["grades"]), 2)
                             if b["grades"] else None,
            "actual_pace": _fmt_pace(b["pace_num"] / d) if d and b["pace_num"] else None,
            "gap_pace": _fmt_pace(b["gap_num"] / d) if d and b["gap_num"] else None,
        }
    return out


def segment_deviation(splits: list[dict], top_n: int = 3) -> dict | None:
    """用 GAP 归一化后找「相对最费力 / 最轻松」的分段。

    GAP 已扣掉坡度，因此段与段之间的 GAP 差异反映的是**真实用力变化或疲劳**，
    而不是地形。这是分段分析的正确用法——也回答了「哪一段真的掉速了」。
    """
    # 配速超过 8:00/km 的段通常是走路、补给或等红灯，不是"更费力"，
    # 混进来会把第 20 段（11:07/km）误判成最费力段。
    excluded = 0
    rows = []
    for s in splits:
        if not s.get("gap_pace_min_km") or (s.get("distance_m") or 0) < 300:
            continue
        if (s.get("pace_min_km") or 0) > MAX_RUN_PACE_MIN_KM:
            excluded += 1
            continue
        rows.append((s.get("seq"), s["gap_pace_min_km"], s.get("pace_min_km")))
    if len(rows) < 4:
        return None
    paces = sorted(r[1] for r in rows)
    mid = paces[len(paces) // 2]
    devs = [{"seq": r[0], "gap_pace": _fmt_pace(r[1]),
             "deviation_sec": round((r[1] - mid) * 60, 1),
             "actual_pace": _fmt_pace(r[2])} for r in rows]
    slow = sorted(devs, key=lambda x: -x["deviation_sec"])[:top_n]
    fast = sorted(devs, key=lambda x: x["deviation_sec"])[:top_n]
    return {
        "median_gap": _fmt_pace(mid),
        "most_effortful_segments": [d for d in slow if d["deviation_sec"] > 3],
        "easiest_segments": [d for d in fast if d["deviation_sec"] < -3],
        "excluded_walk_segments": excluded,
        "note": "GAP 已扣掉坡度，因此这里的偏离是真实用力/疲劳变化，不是地形。"
                "deviation_sec 为正 = 该段相对更慢（更费力或掉速）。"
                f"配速慢于 {MAX_RUN_PACE_MIN_KM:.0f}:00/km 的段（走路/补给/等灯）已排除。",
    }


# ── 跨活动池化回归 ───────────────────────────────────────────
#
# 单次活动的上坡段往往只有 7–10 个，坡度信号会被训练意图（间歇、后程加速、
# 补给停顿）淹没，拟合斜率符号都可能是反的。要得到可信的坡度-配速响应，
# 必须**跨多次活动池化**，并且用「相对本次活动均配速的偏移」作为因变量，
# 消除活动之间的整体配速差异。

# 视为走路/停顿的配速下限（超过即排除，避免补给段污染回归）
MAX_RUN_PACE_MIN_KM = 8.0
# 单次活动拟合的最低 R²：低于此认为信噪比不足，斜率不可信
MIN_SINGLE_R2 = 0.20
# 相对偏离上限（秒/km）：超出视为训练意图或停顿，非坡度效应
MAX_DEV_SEC = 180.0


def _activity_ref_pace(rows: list[dict]) -> float | None:
    """活动内距离加权平均配速（min/km），作为池化时的基准。"""
    d = sum(r.get("distance_m") or 0 for r in rows)
    if not d:
        return None
    t = sum((r.get("pace_min_km") or 0) * (r.get("distance_m") or 0) for r in rows)
    return (t / d) if t else None


def _fit_pooled(up: list, down: list, allp: list, min_n: int = 10) -> dict:
    """对池化样本做上坡/下坡/合并回归，返回统一结构。"""
    out: dict = {}
    up_fit = _ols(up) if len(up) >= min_n else None
    down_fit = _ols(down) if len(down) >= min_n else None
    all_fit = _ols(allp) if len(allp) >= min_n * 2 else None

    if up_fit:
        out["uphill_penalty_sec_per_1pct"] = round(up_fit["slope"], 2)
        out["uphill_r2"] = round(up_fit["r2"], 3) if up_fit["r2"] is not None else None
        out["uphill_segments"] = up_fit["n"]
    if down_fit:
        # 下坡段的 x（坡度%）本身是负数。斜率 >0 表示下坡越缓配速越慢，
        # 等价于「下坡越陡配速越快」，所以收益直接取 +slope。
        out["downhill_benefit_sec_per_1pct"] = round(down_fit["slope"], 2)
        out["downhill_r2"] = round(down_fit["r2"], 3) if down_fit["r2"] is not None else None
        out["downhill_segments"] = down_fit["n"]
    if all_fit:
        out["combined_slope_sec_per_1pct"] = round(all_fit["slope"], 2)
        out["combined_r2"] = round(all_fit["r2"], 3) if all_fit["r2"] is not None else None
    if (out.get("uphill_penalty_sec_per_1pct") is not None
            and out.get("downhill_benefit_sec_per_1pct") is not None
            and out["uphill_penalty_sec_per_1pct"] > 0):
        out["asymmetry_ratio"] = round(
            out["downhill_benefit_sec_per_1pct"] / out["uphill_penalty_sec_per_1pct"], 2)
    return out


POOLED_NOTE = (
    "因变量是「该段配速 − 本次活动平均配速」，已消除活动间配速差异。"
    "上坡/下坡分开回归：上坡惩罚通常大于下坡收益（asymmetry_ratio 典型 0.4–0.8），"
    "合并成一条直线的 r2 天然偏低——这不代表坡度没影响，而是响应不对称（两者互相抵消）。"
    "r2 只有 0.1 左右是正常的：坡度能解释的分段配速方差本就有限，"
    "其余来自训练意图、疲劳、风、路面。看斜率方向与量级，不要看 r2 大小。")


def _collect_pooled(conn, range_days: int, min_dist_m: float, min_grade_pct: float,
                    by_week: bool = False) -> dict:
    """扫描活动分段，按活动内相对配速归一，可选按周分组。"""
    from datetime import date as _date, timedelta as _td
    start = (_date.today() - _td(days=max(30, range_days))).isoformat()
    acts = [dict(r) for r in conn.execute(
        "SELECT id, start_time FROM activities WHERE sport_id='run' "
        "AND substr(start_time,1,10) >= ? ORDER BY start_time", (start,)).fetchall()]

    buckets: dict = {}
    n_acts = n_segs = n_skipped = 0
    for a in acts:
        rows = [dict(r) for r in conn.execute(
            "SELECT distance_m, pace_min_km, avg_grade_pct FROM activity_split_detail "
            "WHERE activity_id=? AND split_type='km' AND avg_grade_pct IS NOT NULL",
            (a["id"],)).fetchall()]
        ref = _activity_ref_pace(rows)
        if not ref:
            continue
        n_acts += 1
        wk = _iso_week(a["start_time"][:10]) if by_week else "all"
        b = buckets.setdefault(wk, {"up": [], "down": [], "all": [],
                                    "activities": 0, "segments": 0, "skipped": 0})
        b["activities"] += 1
        for r in rows:
            g, pace, d = r.get("avg_grade_pct"), r.get("pace_min_km"), r.get("distance_m") or 0
            if g is None or not pace or d < min_dist_m:
                continue
            if pace > MAX_RUN_PACE_MIN_KM:
                n_skipped += 1
                b["skipped"] += 1
                continue
            dev = (pace - ref) * 60
            if abs(dev) > MAX_DEV_SEC:
                n_skipped += 1
                b["skipped"] += 1
                continue
            n_segs += 1
            b["segments"] += 1
            b["all"].append((g, dev))
            if g >= min_grade_pct:
                b["up"].append((g, dev))
            elif g <= -min_grade_pct:
                b["down"].append((g, dev))
    return {"buckets": buckets, "activities": n_acts, "segments": n_segs,
            "skipped_segments": n_skipped}


def pooled_grade_response(conn, range_days: int = 365, min_dist_m: float = 300.0,
                          min_grade_pct: float = 0.4, by_week: bool = False) -> dict:
    """跨活动池化回归：用全部有坡度数据的分段估计坡度-配速响应。

    单次活动的上坡段往往只有 7–10 个，坡度信号会被训练意图淹没，拟合斜率
    符号都可能是反的。池化后样本量足够，才能得到可信的响应。

    by_week=True 时按 ISO 周分组，返回每周各自的池化结果（用于看爬坡能力趋势）。
    """
    col = _collect_pooled(conn, range_days, min_dist_m, min_grade_pct, by_week)
    if not by_week:
        b = col["buckets"].get("all", {"up": [], "down": [], "all": []})
        out = {"activities": col["activities"], "segments": col["segments"],
               "skipped_segments": col["skipped_segments"], "range_days": range_days}
        out.update(_fit_pooled(b["up"], b["down"], b["all"]))
        out["note"] = POOLED_NOTE
        return out

    weeks = []
    for wk in sorted(col["buckets"]):
        b = col["buckets"][wk]
        item = {"week": wk, "activities": b["activities"], "segments": b["segments"]}
        item.update(_fit_pooled(b["up"], b["down"], b["all"]))
        weeks.append(item)
    return {"range_days": range_days, "weeks": weeks,
            "activities": col["activities"], "segments": col["segments"],
            "note": POOLED_NOTE}


# 一个阶段至少要有这么多上坡段，才认为斜率可信（周粒度通常达不到）
MIN_SEGMENTS_FOR_TREND = 40


def period_compare(conn, recent_weeks: int = 8, min_dist_m: float = 300.0,
                   min_grade_pct: float = 0.4) -> dict:
    """把时间切成「最近 N 周」与「之前 N 周」两段，各自池化后对比坡度响应。

    周粒度的上坡段只有 5–15 个，斜率会在 −8 到 +51 之间乱跳，完全不可用。
    把两个阶段各自的样本合起来（通常各 300+ 段）才有统计意义。
    """
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    cut = today - _td(weeks=recent_weeks)
    old_start = cut - _td(weeks=recent_weeks)

    def _run(start: str, end: str) -> dict:
        acts = [dict(r) for r in conn.execute(
            "SELECT id, start_time FROM activities WHERE sport_id='run' "
            "AND substr(start_time,1,10) >= ? AND substr(start_time,1,10) < ? "
            "ORDER BY start_time", (start, end)).fetchall()]
        up, down, allp = [], [], []
        n_acts = 0
        for a in acts:
            rows = [dict(r) for r in conn.execute(
                "SELECT distance_m, pace_min_km, avg_grade_pct FROM activity_split_detail "
                "WHERE activity_id=? AND split_type='km' AND avg_grade_pct IS NOT NULL",
                (a["id"],)).fetchall()]
            ref = _activity_ref_pace(rows)
            if not ref:
                continue
            n_acts += 1
            for r in rows:
                g, pace, d = (r.get("avg_grade_pct"), r.get("pace_min_km"),
                              r.get("distance_m") or 0)
                if g is None or not pace or d < min_dist_m or pace > MAX_RUN_PACE_MIN_KM:
                    continue
                dev = (pace - ref) * 60
                if abs(dev) > MAX_DEV_SEC:
                    continue
                allp.append((g, dev))
                if g >= min_grade_pct:
                    up.append((g, dev))
                elif g <= -min_grade_pct:
                    down.append((g, dev))
        res = {"activities": n_acts, "segments": len(allp), "climb_segments": len(up)}
        res.update(_fit_pooled(up, down, allp))
        return res

    recent = _run(cut.isoformat(), (today + _td(days=1)).isoformat())
    before = _run(old_start.isoformat(), cut.isoformat())

    out = {"recent_weeks": recent_weeks,
           "recent": dict(recent, start=cut.isoformat(), end=today.isoformat()),
           "before": dict(before, start=old_start.isoformat(), end=cut.isoformat())}
    rp = recent.get("uphill_penalty_sec_per_1pct")
    bp = before.get("uphill_penalty_sec_per_1pct")
    if (rp is not None and bp is not None
            and recent["climb_segments"] >= MIN_SEGMENTS_FOR_TREND
            and before["climb_segments"] >= MIN_SEGMENTS_FOR_TREND):
        rr2 = recent.get("uphill_r2") or 0
        br2 = before.get("uphill_r2") or 0
        weak = rr2 < 0.05 or br2 < 0.05
        out["change"] = {
            "uphill_penalty_delta": round(rp - bp, 2),
            "recent_r2": rr2, "before_r2": br2,
            "confidence": "low" if weak else "moderate",
            "interpretation": (
                f"{'上坡惩罚变小 = 爬坡能力进步' if rp < bp else '上坡惩罚变大 = 爬坡能力退步或近期疲劳'}"
                + (f"；但其中一侧 r2 仅 {min(rr2, br2):.3f}，拟合很弱，"
                   "该对比只能当线索，不能当结论。" if weak else "")),
        }
    else:
        out["change"] = None
        out["insufficient"] = (
            f"两阶段都需至少 {MIN_SEGMENTS_FOR_TREND} 个上坡段才能比较；"
            f"近期 {recent['climb_segments']} 个、之前 {before['climb_segments']} 个。"
            "样本不足时请勿解读为爬坡能力变化。")
    out["note"] = POOLED_NOTE
    return out


def weekly_terrain(conn, range_days: int = 90, min_dist_m: float = 300.0) -> list:
    """按周汇总**分段级地形构成**（不涉及单周斜率，避免样本不足的噪声）。

    这回答的是「这周跑了多少上坡段/下坡段/混合段，各自配速如何」，
    而不是用整次活动的爬升密度去代表地形。
    """
    from datetime import date as _date, timedelta as _td
    start = (_date.today() - _td(days=max(7, range_days))).isoformat()
    acts = [dict(r) for r in conn.execute(
        "SELECT id, start_time FROM activities WHERE sport_id='run' "
        "AND substr(start_time,1,10) >= ? ORDER BY start_time", (start,)).fetchall()]
    buckets: dict = {}
    for a in acts:
        rep = analyze_activity(conn, a["id"], min_split_m=min_dist_m)
        if not rep.get("splits"):
            continue
        wk = _iso_week(a["start_time"][:10])
        b = buckets.setdefault(wk, {"week": wk, "runs": 0, "distance_km": 0.0,
                                    "gain_m": 0.0, "terrain_km": {}, "pace_num": 0.0,
                                    "gap_num": 0.0, "d_m": 0.0, "max_delta": 0.0})
        b["runs"] += 1
        sm = rep["summary"]
        b["distance_km"] += sm.get("distance_km") or 0
        for sp in rep["splits"]:
            d = sp.get("distance_m") or 0
            b["gain_m"] += sp.get("elevation_gain_m") or 0
            b["d_m"] += d
            if sp.get("pace_min_km"):
                b["pace_num"] += sp["pace_min_km"] * d
            if sp.get("gap_pace_min_km"):
                b["gap_num"] += sp["gap_pace_min_km"] * d
            if sp.get("terrain"):
                b["terrain_km"][sp["terrain"]] = round(
                    b["terrain_km"].get(sp["terrain"], 0.0) + d / 1000, 2)
        md = sm.get("max_segment_gap_delta_sec") or 0
        b["max_delta"] = max(b["max_delta"], md)

    out = []
    for wk in sorted(buckets):
        b = buckets[wk]
        d = b["d_m"]
        item = {
            "week": wk,
            "runs": b["runs"],
            "distance_km": round(b["distance_km"], 1),
            "elevation_gain_m": round(b["gain_m"], 1),
            "terrain_km": b["terrain_km"],
            "actual_pace": _fmt_pace(b["pace_num"] / d) if d and b["pace_num"] else None,
            "gap_pace": _fmt_pace(b["gap_num"] / d) if d and b["gap_num"] else None,
            "max_segment_gap_delta_sec": round(b["max_delta"], 1) if b["max_delta"] else None,
        }
        out.append(item)
    return out


def _iso_week(day: str) -> str:
    from datetime import date as _date
    iso = _date.fromisoformat(day[:10]).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


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
        r["terrain"] = classify_terrain(r.get("elevation_gain_m"),
                                        r.get("elevation_loss_m"),
                                        r.get("net_elevation_m"),
                                        r.get("distance_m"))
        r["roughness_m"] = round((r.get("elevation_gain_m") or 0)
                                 + (r.get("elevation_loss_m") or 0), 1)

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

    seg_dev = segment_deviation(rows)
    max_seg_gap_delta = None
    deltas = [abs((r["pace_min_km"] - r["gap_pace_min_km"]) * 60) for r in rows
              if r.get("pace_min_km") and r.get("gap_pace_min_km")]
    if deltas:
        max_seg_gap_delta = round(max(deltas), 1)

    return {
        "splits": rows,
        "grade_pace_response": response,
        "asymmetry": asymmetry(rows),
        "terrain_breakdown": terrain_breakdown(rows),
        "segment_deviation": seg_dev,
        "summary": {
            "distance_km": round(d_sum / 1000, 3) if d_sum else None,
            "elevation_gain_m": round(total_gain, 1),
            "elevation_loss_m": round(total_loss, 1),
            "net_elevation_m": round(total_gain - total_loss, 1),
            "avg_grade_pct": round((total_gain - total_loss) / d_sum * 100, 2)
                             if d_sum else None,
            "climb_density_m_per_km": round(total_gain / (d_sum / 1000), 1)
                                      if d_sum else None,
            "actual_pace": _fmt_pace(actual),
            "gap_pace": _fmt_pace(gap),
            "gap_vs_actual_sec": round((actual - gap) * 60, 1)
                                 if actual and gap else None,
            # 分段级的真实起伏幅度：活动级平均会把局部陡坡完全抹平
            "max_segment_gap_delta_sec": max_seg_gap_delta,
            "terrain_scope_note": (
                "climb_density_m_per_km 只是**路线画像**，不能用来判断分段坡度影响："
                "同样的平均值既可以来自缓坡连绵，也可以来自陡上陡下交替。"
                "要看分段级差异请用 terrain_breakdown / asymmetry / "
                "max_segment_gap_delta_sec。"),
        },
    }


def _fmt_pace(pace_min_km: Optional[float]) -> Optional[str]:
    if not pace_min_km or pace_min_km <= 0:
        return None
    sec = int(round(float(pace_min_km) * 60))
    return f"{sec // 60}:{sec % 60:02d}"
