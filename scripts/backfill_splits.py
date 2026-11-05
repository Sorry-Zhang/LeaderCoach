"""一次性回填：历史活动的分段数据 + 修复被截断的 start_time。

背景：活动时间戳曾被截成日期（'YYYY-MM-DD'），而分段回填靠开始时刻对齐活动，
结果所有新活动的 FIT 分段都匹配不上，activity_splits 长期卡在同一批 232 条。

本脚本做两件事（可重复执行，幂等）：
  1. 用 archive/plugin_raw 里的 Garmin 活动存档，把 activities.start_time 补成完整时间戳；
  2. 扫描 data/garmin_fit 下的全部 FIT，按 <activityId>.fit 归到对应活动，回填分段。

用法：
  python scripts/backfill_splits.py --dry-run   # 只看会改什么
  python scripts/backfill_splits.py             # 真正写入
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db as dbmod          # noqa: E402
from core.config import load_config   # noqa: E402


def _activity_archive_starts(cfg: dict) -> dict:
    """{garminActivityId(str): 'YYYY-MM-DD HH:MM:SS'}：来自 plugin_raw 活动存档。"""
    out: dict[str, str] = {}
    root = Path(cfg["_root"]) / (cfg["garmin_plugin"].get("raw_dir") or "archive/plugin_raw")
    folder = root / "get_garmin_activities"
    if not folder.exists():
        return out
    for f in sorted(folder.glob("*.json*")):
        try:
            raw = gzip.open(f, "rt", encoding="utf-8") if f.suffix == ".gz" else open(f, encoding="utf-8")
            with raw as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        for a in data if isinstance(data, list) else []:
            if not isinstance(a, dict):
                continue
            aid = a.get("activityId")
            st = a.get("startTimeLocal") or a.get("beginTimestamp") or a.get("startTime")
            if aid and st:
                out[str(aid)] = str(st)[:19]
    return out


def _fit_starts(cfg: dict) -> dict:
    """{activityId(str): 'YYYY-MM-DD HH:MM:SS'}：直接读每个 FIT 的 session 开始时刻。

    FIT 文件名就是 Garmin activityId，因此这是覆盖最全、最可靠的时刻来源。
    """
    from ingest import fit_parser
    out: dict[str, str] = {}
    for f in sorted(_fit_root(cfg).rglob("*.fit")):
        try:
            info = fit_parser.extract_splits(f)
        except Exception:  # noqa: BLE001
            continue
        st = info.get("start_time")
        if st and len(st) >= 19:
            out[f.stem] = st[:19]
    return out


def _db_starts(conn) -> dict:
    """{activityId(str): start_local}：来自 garmin_activities 表（插件活动名映射）。"""
    out = {}
    try:
        for r in conn.execute(
                "SELECT activity_id, start_local FROM garmin_activities "
                "WHERE start_local IS NOT NULL AND length(start_local) >= 19"):
            out[str(r[0]).replace("plugin:", "").split("@")[0]] = r[1][:19]
    except Exception:  # noqa: BLE001
        pass
    return out


def fix_start_times(conn, starts: dict, dry: bool) -> tuple[int, int]:
    """把只有日期的 start_time 补成完整时间戳。返回 (检查数, 修复数)。"""
    rows = conn.execute(
        "SELECT id, garmin_activity_id, start_time FROM activities "
        "WHERE length(start_time) < 19").fetchall()
    fixed = 0
    for r in rows:
        gid = (r["garmin_activity_id"] or "").replace("plugin:", "").strip()
        full = starts.get(gid)
        if not full or len(full) < 19:
            continue
        if dry:
            fixed += 1
            continue
        conn.execute("UPDATE activities SET start_time=? WHERE id=?", (full, r["id"]))
        fixed += 1
    if not dry and fixed:
        conn.commit()
    return len(rows), fixed


def _fit_root(cfg: dict) -> Path:
    p = Path(cfg["garmin_plugin"].get("fit_dir") or "data/garmin_fit")
    return p if p.is_absolute() else Path(cfg["_root"]) / p


def backfill(cfg: dict, conn, dry: bool, infos: dict | None = None) -> dict:
    from ingest import fit_parser
    files = sorted(_fit_root(cfg).rglob("*.fit"))
    if dry:
        # 只统计可匹配数，不写库
        matched, skipped = 0, 0
        for f in files:
            info = (infos or {}).get(str(f))
            if info is None:
                try:
                    info = fit_parser.extract_splits(f)
                except Exception:  # noqa: BLE001
                    skipped += 1
                    continue
            if not info["start_time"]:
                skipped += 1
                continue
            hit = fit_parser._find_activity(conn, f, info)
            matched += 1 if hit else 0
        return {"fit_files": len(files), "matched": matched, "skipped": skipped, "rows": 0}
    n = fit_parser.rebuild_splits(cfg, files, conn=conn, infos=infos)
    have = conn.execute("SELECT COUNT(DISTINCT activity_id) FROM activity_splits").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    return {"fit_files": len(files), "rows": n, "activities_with_splits": have,
            "activities_total": total,
            "coverage_pct": round(100.0 * have / total, 1) if total else 0.0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回填历史分段 + 修复开始时间")
    ap.add_argument("--dry-run", action="store_true", help="只检查，不写库")
    a = ap.parse_args(argv)

    cfg = load_config()
    conn = dbmod.get_conn(cfg)
    try:
        before = {
            "splits": conn.execute("SELECT COUNT(*) FROM activity_splits").fetchone()[0],
            "acts_with_splits": conn.execute(
                "SELECT COUNT(DISTINCT activity_id) FROM activity_splits").fetchone()[0],
            "acts_total": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
        }
        print(f"[before] 分段 {before['splits']} 条，"
              f"覆盖 {before['acts_with_splits']}/{before['acts_total']} 个活动")

        # 时刻来源优先级：FIT 头部 > garmin_activities 表 > 活动列表存档
        from ingest import fit_parser
        fits = sorted(_fit_root(cfg).rglob("*.fit"))
        infos: dict[str, dict] = {}
        for f in fits:
            try:
                infos[str(f)] = fit_parser.extract_splits(f)
            except Exception:  # noqa: BLE001
                continue
        starts = _activity_archive_starts(cfg)
        starts.update(_db_starts(conn))
        starts.update({Path(k).stem: v["start_time"][:19]
                       for k, v in infos.items()
                       if v.get("start_time") and len(v["start_time"]) >= 19})

        checked, fixed = fix_start_times(conn, starts, a.dry_run)
        print(f"[start_time] 待补 {checked} 条，"
              f"{'可修复' if a.dry_run else '已修复'} {fixed} 条"
              f"（时刻来源 {len(starts)} 个）")

        rep = backfill(cfg, conn, a.dry_run, infos)
        print(f"[fit] 扫描 {rep['fit_files']} 个 FIT：{rep}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
