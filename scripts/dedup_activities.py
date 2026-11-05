"""合并重复活动：CSV 导入与 Garmin 插件采集指向同一次运动时，只保留插件记录。

背景
----
早期用 CSV 导出导入过一批活动（`garmin_activity_id = 'csv:日期:运动:序号'`），
后来接通 Garmin 插件后又采集到同一次运动（`plugin:<activityId>`）。
两条记录会**重复计算训练负荷**，直接污染 CTL/ATL/ACWR 和准备度评分。

但 CSV 那条是当时用 `bridge_trimp` 算过负荷的，插件那条 `load_final` 为 NULL，
所以不能直接删——必须先把负荷字段迁移到插件记录，再删除 CSV 记录。

判重规则：同一天 + 同运动类型 + 距离差 < 0.15 km。
用户手工录入的 RPE 优先级更高：哪边有 rpe 就以哪边为准。

用法：
  python scripts/dedup_activities.py --dry-run
  python scripts/dedup_activities.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db as dbmod        # noqa: E402
from core.config import load_config  # noqa: E402

DIST_TOL_KM = 0.15
LOAD_FIELDS = ("rpe", "srpe_au", "hr_trimp", "load_final",
               "load_path_used", "intensity_domain", "mismatch_flag")


def find_duplicates(conn) -> list[dict]:
    """返回 [{csv, plugin, day}]，一对一贪心匹配：距离差最小的先配。

    同一天可能有多条同类运动（例如同日晚间两次步行），所以不能取"第一个在容差内的"，
    必须取距离最接近的一条，并保证每条插件记录只被认领一次。
    """
    sel = "id, start_time, distance_km, " + ", ".join(LOAD_FIELDS)
    csv_rows = [dict(r) for r in conn.execute(
        f"SELECT {sel} FROM activities WHERE garmin_activity_id LIKE 'csv:%' "
        "ORDER BY start_time")]
    plugin_rows = [dict(r) for r in conn.execute(
        f"SELECT {sel}, sport_id FROM activities WHERE garmin_activity_id LIKE 'plugin:%'")]

    # 列出所有候选对，按距离差升序
    cands = []
    for c in csv_rows:
        day = (c["start_time"] or "")[:10]
        if not c["distance_km"]:
            continue
        for p in plugin_rows:
            if (p["start_time"] or "")[:10] != day:
                continue
            if not p["distance_km"]:
                continue
            diff = abs(p["distance_km"] - c["distance_km"])
            if diff < DIST_TOL_KM:
                cands.append((diff, c, p))
    cands.sort(key=lambda x: x[0])

    used_csv, used_plugin, out = set(), set(), []
    for diff, c, p in cands:
        if c["id"] in used_csv or p["id"] in used_plugin:
            continue
        used_csv.add(c["id"])
        used_plugin.add(p["id"])
        out.append({"csv": c, "plugin": p, "day": (c["start_time"] or "")[:10],
                    "dist_diff_km": round(diff, 3)})
    return out


def merge(conn, pair: dict, dry: bool) -> dict:
    """把 CSV 行的负荷迁移到 plugin 行，然后删除 CSV 行。"""
    c, p = pair["csv"], pair["plugin"]
    # 只填补 plugin 侧为空的字段；plugin 侧已有值（用户后录）则以它为准
    updates = {}
    for f in LOAD_FIELDS:
        if p.get(f) is None and c.get(f) is not None:
            updates[f] = c[f]
    if not dry:
        if updates:
            cols = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE activities SET {cols} WHERE id=?",
                         (*updates.values(), p["id"]))
        conn.execute("DELETE FROM activity_splits WHERE activity_id=?", (c["id"],))
        conn.execute("DELETE FROM activity_split_detail WHERE activity_id=?", (c["id"],))
        conn.execute("DELETE FROM activity_labels WHERE start_time=?",
                     (c["start_time"],))
        conn.execute("DELETE FROM activities WHERE id=?", (c["id"],))
    return {"csv_id": c["id"], "plugin_id": p["id"], "day": pair["day"],
            "moved": updates, "deleted": c["id"]}


def recompute_days(conn, cfg, days: list[str], dry: bool) -> int:
    """去重会改变当日负荷，必须重算受影响日期的引擎输出（含其后 1 天传播）。"""
    if dry:
        return len(days)
    from core import daily as dailymod
    from datetime import timedelta
    touched = set()
    for d in days:
        try:
            base = date.fromisoformat(d[:10])
        except ValueError:
            continue
        for off in (0, 1):
            touched.add((base + timedelta(days=off)).isoformat())
    for d in sorted(touched):
        try:
            dailymod.compute_engine_daily(conn, cfg, d)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 重算 {d} 失败：{type(e).__name__}: {e}")
    conn.commit()
    return len(touched)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合并 CSV / 插件重复活动")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    cfg = load_config()
    conn = dbmod.get_conn(cfg)
    try:
        before = {
            "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
            "load_sum": conn.execute(
                "SELECT COALESCE(SUM(load_final),0) FROM activities").fetchone()[0],
        }
        pairs = find_duplicates(conn)
        print(f"发现重复：{len(pairs)} 组")
        removed_load = 0.0
        moved_load = 0
        days = []
        for pair in pairs:
            c, p = pair["csv"], pair["plugin"]
            if p["load_final"] is None and c["load_final"] is not None:
                moved_load += 1
            else:
                removed_load += c["load_final"] or 0
            res = merge(conn, pair, a.dry_run)
            days.append(pair["day"])
            if a.dry_run:
                print(f"  {pair['day']} csv#{res['csv_id']} → plugin#{res['plugin_id']} "
                      f"迁移字段 {list(res['moved'])}")
        n = recompute_days(conn, cfg, days, a.dry_run)
        after = {
            "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
            "load_sum": conn.execute(
                "SELECT COALESCE(SUM(load_final),0) FROM activities").fetchone()[0],
        }
        print()
        print(f"[活动] {before['activities']} → {after['activities']}")
        print(f"[负荷合计] {before['load_sum']:.1f} → {after['load_sum']:.1f} AU "
              f"(净减 {before['load_sum'] - after['load_sum']:.1f})")
        print(f"[负荷迁移] {moved_load} 组把负荷移到插件记录，{len(pairs) - moved_load} 组直接消除重复")
        print(f"[重算] {'将重算' if a.dry_run else '已重算'} {n} 天引擎输出")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
