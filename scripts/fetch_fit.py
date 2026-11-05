"""从 Garmin 插件下载指定活动的 FIT（或最近 N 天活动的 FIT 缺口）。

用法：
  python scripts/fetch_fit.py 658766818 658322812 ...
  python scripts/fetch_fit.py --days 7        # 近 7 天活动的 FIT 缺口
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config                      # noqa: E402
from ingest import plugin_full                           # noqa: E402


def _env(plugin_cfg: dict) -> dict:
    env = dict(os.environ)
    env.update({
        "GARMIN_USERNAME": plugin_full.DEFAULTS["username"],
        "GARMIN_REGION": plugin_full.DEFAULTS["region"],
        "GARMIN_ACCOUNT": plugin_full.DEFAULTS["account"],
        "GARMIN_SESSION_TOKEN_FILE": plugin_full.DEFAULTS["session"],
        "GARMIN_FIT_DOWNLOAD_DIR": os.path.abspath(plugin_full.DEFAULTS["fit_dir"]),
        "GARMIN_ACTIVITY_DETAIL": "full",
    })
    return env


async def _run(ids: list[str], days: int) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=plugin_full.DEFAULTS["node"],
        args=[plugin_full.DEFAULTS["mcp_js"]],
        env=_env({}))
    out = {"requested": ids, "downloaded": [], "already": [], "failed": []}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as cs:
            await cs.initialize()
            listed = {t.name for t in (await cs.list_tools()).tools}
            if days > 0:
                end = date.today()
                start = end - timedelta(days=days - 1)
                res = await cs.call_tool("get_garmin_activities",
                                         {"limit": 100, "startDate": start.isoformat(),
                                          "endDate": end.isoformat(), "detail": "full"})
                data = _payload(res)
                for a in data if isinstance(data, list) else []:
                    if isinstance(a, dict) and a.get("activityId"):
                        ids.append(str(a["activityId"]))
            if "download_garmin_activity_fit" not in listed:
                out["error"] = "插件未注册 download_garmin_activity_fit"
                return out
            fit_root = Path(os.path.abspath(plugin_full.DEFAULTS["fit_dir"]))
            for aid in dict.fromkeys(ids):
                exist = list(fit_root.rglob(f"{aid}.fit")) if fit_root.exists() else []
                if exist:
                    out["already"].append(aid)
                    continue
                try:
                    res = await cs.call_tool("download_garmin_activity_fit",
                                             {"activityId": int(aid)})
                    ok = not getattr(res, "isError", False)
                    payload = _payload(res)
                    if isinstance(payload, dict) and payload.get("error"):
                        ok = False
                    out["downloaded" if ok else "failed"].append(
                        aid if ok else {"id": aid, "resp": str(payload)[:200]})
                except Exception as e:  # noqa: BLE001
                    out["failed"].append({"id": aid, "error": f"{type(e).__name__}: {e}"})
    return out


def _payload(res):
    content = getattr(res, "content", None) or []
    for item in content:
        txt = getattr(item, "text", None)
        if txt:
            try:
                return json.loads(txt)
            except Exception:  # noqa: BLE001
                return txt
    return getattr(res, "structuredContent", None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="下载活动 FIT")
    ap.add_argument("ids", nargs="*", help="Garmin activityId")
    ap.add_argument("--days", type=int, default=0, help="改为拉近 N 天活动")
    a = ap.parse_args(argv)
    if not a.ids and a.days <= 0:
        ap.error("请给 activityId 或 --days")
    cfg = load_config()
    plugin_full._require_plugin_config()
    rep = asyncio.run(_run(list(a.ids), a.days))
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if not rep.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
