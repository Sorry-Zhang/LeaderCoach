from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from core import db as dbmod
from core import store
from core import daily as dailymod
from core.config import load_config, resolve

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

def cmd_init(cfg, args=None):
    res = dbmod.init_db(cfg)
    print(f"integrity_check {res}, schema_version={dbmod.SCHEMA_VERSION}")
    print("下一步：python main.py profile init 注册运动档案；然后 python main.py sync ...")

def cmd_sync(cfg, args):
    conn = dbmod.get_conn(cfg)
    try:
        imported = 0
        if args.csv:
            from ingest import csv_import
            p = csv_import.find_activities_csv(args.csv)
            if not p:
                print(f"未在 {args.csv} 找到 activities.csv")
            else:
                acts = csv_import.import_activities_csv(p)
                for a in acts:
                    store.insert_activity(conn, cfg, a)
                    imported += 1
                print(f"CSV 导入 {imported} 条活动（{p}）")
        elif args.fit:
            from ingest import fit_parser
            folder = Path(args.fit)
            files = sorted(folder.rglob("*.fit")) if folder.is_dir() else [folder]
            for f in files:
                try:
                    arch = resolve(cfg, cfg["paths"]["fit_archive"])
                    dest = arch / datetime.now().strftime("%Y/%m") / f.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        shutil.copy2(f, dest)
                    act = fit_parser.parse_fit(dest)
                    if act.get("start_time"):
                        store.insert_activity(conn, cfg, act)
                        imported += 1
                except Exception as e:
                    print(f"跳过 {f.name}: {e}")
            print(f"FIT 导入 {imported} 条活动")
        else:
            try:
                if getattr(args, "full", True):
                    # 全量：插件全部只读工具 + 原始存档 + FIT 分段
                    from ingest import plugin_full
                    end = datetime.now().date()
                    start = end - timedelta(days=max(1, args.days) - 1)
                    rep = plugin_full.run(
                        start.isoformat(), end.isoformat(), 1,
                        bool(getattr(args, "download_fit", False)),
                        plugin_full.DEFAULTS["node"], plugin_full.DEFAULTS["mcp_js"],
                        plugin_full.DEFAULTS["session"], plugin_full.DEFAULTS["username"],
                        plugin_full.DEFAULTS["region"], plugin_full.DEFAULTS["account"],
                        plugin_full.DEFAULTS["fit_dir"])
                    print(json.dumps({k: rep[k] for k in (
                        "start", "end", "days", "activities", "inserted", "splits",
                        "sleep_stage_days", "hrv_reading_days", "stress_series_days",
                        "battery_series_days", "metric_rows", "raw_written", "raw_bytes")},
                        ensure_ascii=False, indent=2))
                    if rep["tools_failed"]:
                        print(f"未成功的工具调用：{rep['tools_failed'][:5]}")
                else:
                    from ingest import plugin_bridge
                    end = datetime.now().date()
                    start = end - timedelta(days=args.days)
                    plugin_bridge.main([
                        "--start", start.isoformat(), "--end", end.isoformat(),
                        "--activities", str(args.days), "--insert-activities",
                    ])
            except Exception as e:
                print(f"插件同步失败（{e}）。请用 --fit 或 --csv 导入。")
                sys.exit(1)
        conn.execute("INSERT INTO meta(key,value) VALUES('last_sync_at',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_now(),))
        conn.commit()
        print("同步完成。可运行：python main.py prompt 查看教练提示词；python main.py serve 启动 MCP。")
    finally:
        conn.close()

def cmd_health(cfg, args=None):
    from ingest import health_fit_parser, wellness_csv_import
    from ingest.zip_util import materialize
    conn = dbmod.get_conn(cfg)
    try:
        with materialize(args.fit) as folder:
            n_fit = len(list(folder.rglob("*.fit"))) if folder.is_dir() else 0
            n_csv = len(list(folder.rglob("*.csv"))) if folder.is_dir() else 0
            rows = {}
            n_week = 0
            if n_fit:
                rows.update(health_fit_parser.import_health_dir(str(folder), conn))
            if n_csv:
                rows.update(wellness_csv_import.import_wellness(str(folder), conn))
                n_week = wellness_csv_import.import_sleep_weekly(str(folder), conn)
        print(f"健康数据导入 {len(rows)} 天 → daily_metrics（FIT {n_fit} 个 / CSV {n_csv} 个）")
        if n_week:
            print(f"周聚合睡眠导入 {n_week} 周 → sleep_weekly")
        if not rows:
            print("未解析到数据：请先用 python -m ingest.health_fit_parser <某FIT> 查看字段结构")
    finally:
        conn.close()

def cmd_today(cfg, args=None):
    from datetime import date as _date

    from ml.today import today as _today

    day = getattr(args, "date", None) or _date.today().isoformat()
    conn = dbmod.get_conn(cfg)
    try:
        r = _today(conn, cfg, day)
    finally:
        conn.close()

    st = r["状态"]
    print(f"{r['日期']} {r['星期']}  |  诊断 {r['诊断']}  |  模板 {r['模板']}")
    print(f"CTL {st['CTL']}  ATL {st['ATL']}  TSB {st['TSB']}  "
          f"ACWR {st['ACWR']}  Readiness {st['Readiness']}")
    print(f"训练预测 {r['训练预测'] or '无'}  |  调整 {r['调整'] or '无'}")
    print("-" * 56)
    for s in r["今日安排"]:
        if "说明" in s and "项目" not in s:
            print(s["说明"])
            continue
        print(f"{s['项目']:<8} {s['时长_min']} min  RPE {s['RPE']}  "
              f"AU {s['AU']}  结构 {s['结构'] or '-'}")
        if s["说明"]:
            print(f"         {s['说明']}")

def _ensure_subjective_cols(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(subjective)")}
    for col, ddl in (("alcohol_units", "REAL"), ("caffeine_mg", "REAL"),
                     ("stress", "INTEGER")):
        if col not in have:
            conn.execute(f"ALTER TABLE subjective ADD COLUMN {col} {ddl}")
    conn.commit()

def cmd_subjective(cfg, args=None):
    from datetime import date as _date
    day = args.date or _date.today().isoformat()
    if not (0 <= args.soreness <= 10):
        print("soreness 必须在 0-10"); return
    if not (1 <= args.sleep_feel <= 5):
        print("sleep_feel 必须在 1-5"); return
    if args.stress is not None and not (1 <= args.stress <= 5):
        print("stress 必须在 1-5"); return
    conn = dbmod.get_conn(cfg)
    try:
        _ensure_subjective_cols(conn)
        store.upsert_subjective(conn, day, args.soreness, args.sleep_feel,
                                args.note, args.injury,
                                alcohol_units=args.alcohol,
                                caffeine_mg=args.caffeine, stress=args.stress)
        try:
            dailymod.compute_engine_daily(conn, cfg, day)
        except Exception as e:
            print(f"引擎日算失败（不影响标签）：{e}")
        n = conn.execute("SELECT COUNT(*) FROM subjective").fetchone()[0]
        extra = []
        if args.alcohol is not None:
            extra.append(f"酒精 {args.alcohol} 杯")
        if args.caffeine is not None:
            extra.append(f"咖啡因 {args.caffeine:.0f} mg")
        if args.stress is not None:
            extra.append(f"压力 {args.stress}/5")
        print(f"已记录 {day}：酸痛 {args.soreness}/10，睡眠感受 {args.sleep_feel}/5，"
              f"伤病 {bool(args.injury)}" + ("，" + "，".join(extra) if extra else ""))
        print(f"累计主观标签 {n} 天（≥120 天可训基线）")
    finally:
        conn.close()

def cmd_labels(cfg, args=None):
    from ingest import fit_labels
    conn = dbmod.get_conn(cfg)
    try:
        n = fit_labels.import_fit_labels(args.fit, conn)
        days = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM activity_labels").fetchone()[0]
        print(f"从 FIT 回填活动标签 {n} 条 → activity_labels，覆盖 {days} 个活动日")
        if days < 120:
            print(f"提示：{days} 天 < 120，暂不足以训基线；继续用 subjective 每日记录")
    finally:
        conn.close()

def _upsert_daily(conn, s: dict):
    conn.execute(
        "INSERT INTO daily_metrics(date, resting_hr, stress_daily_avg, hrv_last_night, hrv_baseline, "
        "sleep_hours, sleep_deep_h, sleep_light_h, sleep_rem_h, sleep_awake_h) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
        "resting_hr=excluded.resting_hr, stress_daily_avg=excluded.stress_daily_avg, "
        "hrv_last_night=excluded.hrv_last_night, hrv_baseline=excluded.hrv_baseline, "
        "sleep_hours=excluded.sleep_hours, sleep_deep_h=excluded.sleep_deep_h, "
        "sleep_light_h=excluded.sleep_light_h, sleep_rem_h=excluded.sleep_rem_h, "
        "sleep_awake_h=excluded.sleep_awake_h",
        (s.get("date"), s.get("resting_hr"), s.get("stress_daily_avg"),
         s.get("hrv_last_night"), s.get("hrv_baseline"),
         s.get("sleep_hours"), s.get("sleep_deep_h"), s.get("sleep_light_h"),
         s.get("sleep_rem_h"), s.get("sleep_awake_h")))
    conn.commit()

def cmd_serve(cfg, args=None):
    from mcp_server.tools import mcp
    mcp.run()

def cmd_task(cfg, args=None):
    if args is not None and getattr(args, "only", None) == "backup":
        print(f"备份完成：{dbmod.backup(cfg)}")
        return
    conn = dbmod.get_conn(cfg)
    try:
        today = datetime.now().date()
        for i in range(30):
            d = (today - timedelta(days=i)).isoformat()
            try:
                dailymod.compute_engine_daily(conn, cfg, d)
            except Exception:
                continue
        try:
            dest = dbmod.backup(cfg)
            print(f"备份完成：{dest}")
        except Exception as e:
            print(f"备份失败（阻断性告警）：{e}")
            sys.exit(1)
        print("夜间任务完成")
    finally:
        conn.close()

def cmd_backup(cfg, args=None):
    print(f"备份完成：{dbmod.backup(cfg)}")

def cmd_verify(cfg, args):
    r = dbmod.verify(cfg, args.db)
    print(json.dumps(r, ensure_ascii=False, indent=2))

def cmd_profile(cfg, args):
    conn = dbmod.get_conn(cfg)
    try:
        if args.interactive:
            print("=== 运动档案初始化（回车用默认值）===")
            sex = input("生理性别 (male/female，默认 female)：").strip() or "female"
            hr_max = _int(input("最大心率 hr_max（无则回车，稍后做递升测试）：")) or 0
            hr_rest = _int(input("静息心率 hr_rest（无则回车）：")) or 0
            sleep = _float(input("睡眠需求小时数（默认 7.5）：")) or 7.5
            contra = input("避孕方式 none/hormonal/hormonal_continuous/nonhormonal（默认 none）：").strip() or "none"
            zones = input("心率区间 JSON（默认留空用公式近似）：").strip()
        else:
            sex, hr_max, hr_rest, sleep, contra, zones = "female", 0, 0, 7.5, "none", ""
        hr_zones = zones or _default_zones(hr_max if hr_max else 190)
        conn.execute(
            "INSERT INTO athlete_profile(id, sex, hr_max, hr_max_source, hr_rest, hr_rest_source, "
            "sleep_need_h, hr_zones, contraception, updated_at) VALUES(1,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET sex=excluded.sex, hr_max=excluded.hr_max, "
            "hr_max_source=excluded.hr_max_source, hr_rest=excluded.hr_rest, "
            "hr_rest_source=excluded.hr_rest_source, sleep_need_h=excluded.sleep_need_h, "
            "hr_zones=excluded.hr_zones, contraception=excluded.contraception, updated_at=excluded.updated_at",
            (sex, hr_max or None, "field_test" if hr_max else None,
             hr_rest or None, "field_test" if hr_rest else None, sleep, hr_zones, contra, _now()))
        conn.commit()
        print("档案已保存")
    finally:
        conn.close()

def _default_zones(hr_max: int) -> str:
    z = [round(hr_max * p) for p in (0.50, 0.60, 0.70, 0.80, 0.90)]
    return json.dumps({"b1_low": z[0], "b2_low": z[1], "b3_low": z[2], "b4_low": z[3], "b5_low": z[4]})

def _int(s):
    try:
        return int(s)
    except ValueError:
        return 0

def _float(s):
    try:
        return float(s)
    except ValueError:
        return 0.0

def cmd_prompt(cfg, args=None):
    conn = dbmod.get_conn(cfg)
    try:
        from coach import context_builder
        print(context_builder.build_system_prompt(cfg, conn))
    finally:
        conn.close()

def cmd_notify(cfg, args):
    from mcp_server import notify
    notify.notify(cfg, "这是一条测试通知", kind="code")
    print("已发送测试通知")

def cmd_tool(cfg, args):
    conn = dbmod.get_conn(cfg)
    try:
        name = args.name
        if name == "get_daily_brief":
            result = dailymod.build_daily_brief(conn, cfg, args.date)
        elif name == "get_sync_status":
            brief = dailymod.build_daily_brief(conn, cfg)
            row = conn.execute("SELECT value FROM meta WHERE key='last_sync_at'").fetchone()
            result = {
                "last_sync_at": row["value"] if row else None,
                "feeding": brief["data_quality"]["feeding"],
                "activity_count": conn.execute("SELECT COUNT(*) c FROM activities").fetchone()["c"],
                "strength_count": conn.execute("SELECT COUNT(*) c FROM strength_sessions").fetchone()["c"],
            }
        elif name == "get_activities":
            start = (datetime.now().date() - timedelta(days=14)).isoformat()
            rows = conn.execute(
                "SELECT id, substr(start_time,1,10) AS date, sport_id, duration_min, distance_km, "
                "avg_hr, rpe, srpe_au, hr_trimp, load_final, load_path_used, intensity_domain, "
                "mismatch_flag FROM activities WHERE start_time>=? ORDER BY start_time", (start,)).fetchall()
            result = [dict(r) for r in rows]
        elif name == "get_audit_log":
            start = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE ts>=? ORDER BY ts DESC LIMIT 500", (start,)).fetchall()
            result = [dict(r) for r in rows]
        elif name == "get_learning_status":
            result = {"entries": learning.learning_status(conn)}
        elif name == "get_cycle_status":
            periods = [dict(r) for r in conn.execute(
                "SELECT start_date, end_date FROM cycle_periods ORDER BY start_date").fetchall()]
            from core import cycle as cyclemod
            result = {"prediction": cyclemod.predict(datetime.now().date(), periods),
                      "flags": cyclemod.evaluate_flags(periods, datetime.now().date(), cfg)}
        else:
            print("CLI 降级支持：get_daily_brief / get_sync_status / get_activities / "
                  "get_audit_log / get_learning_status / get_cycle_status")
            sys.exit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        conn.close()

def main(argv=None):
    p = argparse.ArgumentParser(prog="coach", description="佳明运动手表 AI 教练")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库")

    sp = sub.add_parser("sync", help="同步数据")
    sp.add_argument("--csv", help="Garmin 导出 CSV 目录（含 activities.csv）")
    sp.add_argument("--fit", help=".fit 文件目录（Garmin Express）")
    sp.add_argument("--days", type=int, default=60)
    sp.add_argument("--full", dest="full", action="store_true", default=True,
                    help="全量采集：插件全部只读工具 + 原始存档 + FIT 分段（默认开启）")
    sp.add_argument("--legacy", dest="full", action="store_false",
                    help="旧的最小采集路径（只有日指标 + 活动摘要）")
    sp.add_argument("--download-fit", action="store_true", help="缺 FIT 时从插件下载")

    sub.add_parser("serve", help="启动 MCP stdio 服务")

    tk = sub.add_parser("task", help="夜间任务管线")
    tk.add_argument("--only", choices=["backup"], help="只执行备份步骤")

    sub.add_parser("backup", help="备份数据库")

    vp = sub.add_parser("verify", help="校验数据库")
    vp.add_argument("--db", help="指定 db 文件路径")

    pp = sub.add_parser("profile", help="初始化/更新运动档案")
    pp.add_argument("action", nargs="?", default="init", help="init / update（可省略）")
    pp.add_argument("--interactive", action="store_true")

    sub.add_parser("prompt", help="打印编译后的教练系统提示词")

    np = sub.add_parser("notify", help="测试通知")
    np.add_argument("--test", action="store_true")

    tp = sub.add_parser("tool", help="CLI 降级调用只读工具")
    tp.add_argument("name")
    tp.add_argument("--date")
    tp.add_argument("--sport")
    tp.add_argument("--json-out", action="store_true")

    hp = sub.add_parser("health", help="导入健康数据 FIT（睡眠/HRV/RHR/压力）")
    hp.add_argument("--fit", required=True, help="健康 FIT 目录（导出健康数据）")

    tp = sub.add_parser("today", help="今日训练安排（按当前状态 + 训练预测）")
    tp.add_argument("--date", default=None, help="默认今天")
    tp.set_defaults(func=cmd_today)

    sb = sub.add_parser("subjective", help="记录当日主观状态（酸痛 0-10 / 睡眠感受 1-5）")
    sb.add_argument("--date", default=None, help="默认今天")
    sb.add_argument("--soreness", type=int, required=True, help="酸痛 0-10（0 无，5 明显，10 极限）")
    sb.add_argument("--sleep-feel", type=int, required=True, dest="sleep_feel",
                    help="睡眠感受 1-5（1 很差，3 一般，5 很好）")
    sb.add_argument("--injury", action="store_true", help="今日有疼痛/伤病")
    sb.add_argument("--note", default=None)
    sb.add_argument("--alcohol", type=float, default=None,
                    help="酒精标准杯数（1 杯≈14g 纯酒精）")
    sb.add_argument("--caffeine", type=float, default=None, help="咖啡因毫克")
    sb.add_argument("--stress", type=int, default=None, help="主观压力 1-5（5 最高）")

    lb = sub.add_parser("labels", help="从活动 FIT 回填佳明手动评价（RPE/Feel）")
    lb.add_argument("--fit", required=True, help="活动 FIT 目录（解压后的 .fit）")

    args = p.parse_args(argv)
    cfg = load_config()

    dispatch = {
        "init": cmd_init, "sync": cmd_sync, "serve": cmd_serve, "task": cmd_task,
        "backup": cmd_backup, "verify": cmd_verify, "profile": cmd_profile,
        "prompt": cmd_prompt, "notify": cmd_notify, "tool": cmd_tool,
        "health": cmd_health, "subjective": cmd_subjective, "labels": cmd_labels,
        "today": cmd_today,
    }
    dispatch[args.cmd](cfg, args)

if __name__ == "__main__":
    main()
