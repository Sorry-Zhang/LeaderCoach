"""coros-coach 入口：MCP 服务 + CLI。

    python -m coros_mcp.main serve      # 启动 MCP（stdio）
    python -m coros_mcp.main            # 不给子命令时默认 serve
    python -m coros_mcp.main sync --days 7
    python -m coros_mcp.main status
    python -m coros_mcp.main doctor     # 自检：token / 库 / 服务端工具
    python -m coros_mcp.main task       # 计划任务用：同步 + 重算

也支持直接跑文件（计划任务或 MCP 客户端的 cwd 不是项目目录时更方便）：
    D:\\LeaderCoach\\.venv\\Scripts\\python.exe D:\\LeaderCoach\\coros_mcp\\main.py task
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if __package__ in (None, ""):
    # 以文件方式直接运行时，先把自己注册成包，否则相对导入失败
    import importlib.util
    _pkg = importlib.util.spec_from_file_location(
        "coros_mcp", str(Path(__file__).resolve().parent / "__init__.py"),
        submodule_search_locations=[str(Path(__file__).resolve().parent)])
    _mod = importlib.util.module_from_spec(_pkg)
    sys.modules["coros_mcp"] = _mod
    _pkg.loader.exec_module(_mod)
    __package__ = "coros_mcp"

# MCP 走 stdio，协议要求 UTF-8。Windows 下管道默认是 GBK，
# 不显式改会在客户端侧变成乱码或解析失败。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

LOG_DIR = _ROOT / "data" / "coros"


def _log_crash() -> Path | None:
    """把启动/运行异常写到文件。

    MCP 客户端的 stderr 常被吞掉，只显示 "Connection closed"。
    没有这份日志就完全无法定位。故意不依赖 cfg（配置本身可能就是失败原因）。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "startup_error.log"
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 70}\n")
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] "
                    f"argv={sys.argv!r} cwd={Path.cwd()}\n")
            traceback.print_exc(file=f)
        return path
    except Exception:  # noqa: BLE001
        return None


def _p(o) -> None:
    print(json.dumps(o, ensure_ascii=False, indent=1, default=str))


def cmd_serve(args) -> int:
    from .tools import mcp
    mcp.run()
    return 0


def cmd_sync(args) -> int:
    from .tools import _run_sync
    _p(_run_sync(args.days))
    return 0


def cmd_status(args) -> int:
    from .tools import get_sync_status
    _p(get_sync_status())
    return 0


def cmd_profile(args) -> int:
    from .tools import get_coros_profile
    _p(get_coros_profile())
    return 0


def cmd_tools(args) -> int:
    from .tools import get_coros_server_tools
    _p(get_coros_server_tools())
    return 0


def cmd_doctor(args) -> int:
    """自检：把「客户端为什么连不上」的常见原因逐条验一遍。"""
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})

    add("Python 版本", sys.version_info >= (3, 11),
        f"{sys.version_info.major}.{sys.version_info.minor}")
    add("项目根目录", (_ROOT / "config.toml").exists(), str(_ROOT))

    try:
        from core.config import load_config
        cfg = load_config()
        add("读取 config.toml", True, f"_root={cfg.get('_root')}")
    except Exception as e:  # noqa: BLE001
        add("读取 config.toml", False, f"{type(e).__name__}: {e}")
        cfg = None

    try:
        from core import db as dbmod
        conn = dbmod.get_conn(cfg) if cfg else None
        n = conn.execute("SELECT COUNT(*) FROM activities WHERE source='coros'").fetchone()[0]
        conn.close()
        add("打开数据库", True, f"coros 活动 {n} 条")
    except Exception as e:  # noqa: BLE001
        add("打开数据库", False, f"{type(e).__name__}: {e}")

    try:
        from scripts import coros_client as cc
        tok = cc.load_token()
        if not tok:
            add("COROS 授权", False, "没有 token，先跑 login-start / login-finish")
        else:
            add("COROS 授权", not cc._expired(tok), f"issuer={tok.get('issuer')}")
    except Exception as e:  # noqa: BLE001
        add("COROS 授权", False, f"{type(e).__name__}: {e}")

    try:
        from scripts import coros_client as cc
        n = len(cc.list_tools())
        add("连接 COROS 服务端", n > 0, f"{n} 个工具")
    except Exception as e:  # noqa: BLE001
        add("连接 COROS 服务端", False, f"{type(e).__name__}: {e}")

    try:
        from .tools import mcp as m
        import asyncio
        n = len(asyncio.run(m.list_tools()))
        add("MCP 工具注册", n > 0, f"{n} 个工具")
    except Exception as e:  # noqa: BLE001
        add("MCP 工具注册", False, f"{type(e).__name__}: {e}")

    add("stdout 编码", (sys.stdout.encoding or "").lower().replace("-", "") in
        ("utf8", "utf_8"), sys.stdout.encoding)

    ok = all(c["ok"] for c in checks)
    _p({"all_ok": ok, "checks": checks,
        "hint": ("全部通过：客户端若仍报 Connection closed，检查 MCP 配置里的"
                 "『命令』是否为 python.exe 绝对路径、『参数』是否为两行"
                 "（脚本绝对路径 + serve），以及是否漏了 serve。")
        if ok else "有项目未通过，先修上面标 false 的项。"})
    return 0 if ok else 1


def cmd_task(args) -> int:
    """计划任务：同步近 N 天（默认 7）。失败不阻断，原因写进输出。"""
    from .tools import _run_sync
    try:
        res = _run_sync(args.days)
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] COROS 同步完成："
              f"{res.get('inserted', 0)} 个活动，"
              f"睡眠 {res['health'].get('sleep_days', 0)} 天")
        for n in res.get("notes") or []:
            print(f"  note: {n}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] COROS 同步失败："
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="coros-coach", description=__doc__)
    # nargs="?" + 默认 serve：漏写 serve 时不再 argparse 直接退出
    # （那种情况客户端只会看到 Connection closed，极难定位）
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="启动 MCP 服务（stdio，默认）")

    s = sub.add_parser("sync", help="同步最近 N 天")
    s.add_argument("--days", type=int, default=7)

    sub.add_parser("status", help="同步状态与覆盖度")
    sub.add_parser("profile", help="账号资料与绑定设备")
    sub.add_parser("tools", help="列出服务端工具")
    sub.add_parser("doctor", help="自检：token / 库 / 服务端 / 工具注册")

    t = sub.add_parser("task", help="计划任务：同步 + 重算")
    t.add_argument("--days", type=int, default=7)

    args = ap.parse_args(argv)
    cmd = args.cmd or "serve"
    if cmd in ("sync", "task") and not hasattr(args, "days"):
        args.days = 7
    return {"serve": cmd_serve, "sync": cmd_sync, "status": cmd_status,
            "profile": cmd_profile, "tools": cmd_tools,
            "doctor": cmd_doctor, "task": cmd_task}[cmd](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        path = _log_crash()
        print(f"启动失败，详情已写入 {path}", file=sys.stderr)
        raise
