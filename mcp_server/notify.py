from __future__ import annotations

import datetime as dt

def _parse_t(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))

def _in_quiet_hours(cfg: dict) -> bool:
    start, end = cfg["notify"]["quiet_hours"]
    s, e = _parse_t(start), _parse_t(end)
    now = dt.datetime.now().time()
    if s <= e:
        return s <= now <= e
    return now >= s or now <= e

def _toast(title: str, msg: str) -> None:
    try:
        from winotify import Notification
        Notification(app_id="Garmin Coach", title=title, msg=msg, duration="short").show()
    except Exception:
        pass

def _ntfy(cfg: dict, msg: str) -> None:
    try:
        import requests
        topic = cfg["notify"]["ntfy_topic"]
        if not topic or "REPLACE" in topic:
            return
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"), timeout=10)
    except Exception:
        pass

def notify(cfg: dict, msg: str, kind: str = "info") -> None:
    if kind != "code" and _in_quiet_hours(cfg):
        return
    channel = cfg["notify"]["channel"]
    title = "佳明教练·确认码" if kind == "code" else "佳明教练"
    if channel in ("toast", "both"):
        _toast(title, msg)
    if channel in ("ntfy", "both"):
        _ntfy(cfg, msg)
