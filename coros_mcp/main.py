"""coros-coach 入口：MCP 服务 + CLI。

    python -m coros_mcp.main serve      # 启动 MCP（stdio）
    python -m coros_mcp.main sync --days 7
    python -m coros_mcp.main status
    python -m coros_mcp.main profile
    python -m coros_mcp.main task       # 计划任务用：同步 + 重算

也支持直接跑文件（计划任务的 cwd 不是项目目录时方便）：
    D:\\LeaderCoach\\.venv\\Scripts\\python.exe D:\\LeaderCoach\\coros_mcp\\main.py task
"""
from __future__ import annotations

import argparse
import json
import sys
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


def _p(o) -> None:
    print(json.dumps(o, ensure_ascii=False, indent=1, default=str))


def cmd_serve(args) -> int:
    from .tools import mcp
    mcp.run()
    return 0


def cmd_sync(args) -> int:
    from .tools import _run_sync
    res = _run_sync(args.days)
    _p(res)
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


def cmd_task(args) -> int:
    """计划任务：同步近 N 天（默认 7）。失败不阻断，把原因写进输出。"""
    from .tools import _run_sync
    try:
        res = _run_sync(args.days)
        ok = res.get("inserted", 0)
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] COROS 同步完成："
              f"{ok} 个活动，睡眠 {res['health'].get('sleep_days', 0)} 天")
        for n in res.get("notes") or []:
            print(f"  note: {n}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] COROS 同步失败："
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="coros-coach", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="启动 MCP 服务（stdio）")

    s = sub.add_parser("sync", help="同步最近 N 天")
    s.add_argument("--days", type=int, default=7)

    sub.add_parser("status", help="同步状态与覆盖度")
    sub.add_parser("profile", help="账号资料与绑定设备")
    sub.add_parser("tools", help="列出服务端工具")

    t = sub.add_parser("task", help="计划任务：同步 + 重算")
    t.add_argument("--days", type=int, default=7)

    args = ap.parse_args(argv)
    return {"serve": cmd_serve, "sync": cmd_sync, "status": cmd_status,
            "profile": cmd_profile, "tools": cmd_tools,
            "task": cmd_task}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
