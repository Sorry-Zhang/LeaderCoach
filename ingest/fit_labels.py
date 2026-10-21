from __future__ import annotations

from ingest.fit_parser import parse_fit
from ingest.zip_util import materialize

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS activity_labels (start_time TEXT PRIMARY KEY, "
    "date TEXT, sport_id TEXT, rpe INTEGER, feel INTEGER, source TEXT)")

def import_fit_labels(folder, conn=None) -> int:
    if conn is not None:
        conn.execute(_SCHEMA)
    n = 0
    with materialize(folder) as root:
        files = sorted(root.rglob("*.fit")) if root.is_dir() else [root]
        for f in files:
            try:
                act = parse_fit(f)
            except Exception:
                continue
            if act.get("rpe") is None and act.get("feel") is None:
                continue
            st = act.get("start_time")
            day = str(st)[:10] if st else None
            if not (st and day and conn is not None):
                continue
            conn.execute(
                "INSERT INTO activity_labels(start_time, date, sport_id, rpe, feel, source) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(start_time) DO UPDATE SET "
                "date=excluded.date, sport_id=excluded.sport_id, rpe=excluded.rpe, "
                "feel=excluded.feel, source=excluded.source",
                (st, day, act.get("sport_id"), act.get("rpe"), act.get("feel"), f.name))
            n += 1
    if conn is not None:
        conn.commit()
    return n
