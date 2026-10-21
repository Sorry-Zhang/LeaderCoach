from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as dbmod

def test_init_21_tables():
    root = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp(prefix="coach_test_"))
    try:
        (tmp / "db").mkdir()
        shutil.copy(root / "db" / "schema.sql", tmp / "db" / "schema.sql")
        shutil.copy(root / "db" / "seed.sql", tmp / "db" / "seed.sql")
        cfg = {"_root": str(tmp), "paths": {"db": "db/coach.db"}}
        res = dbmod.init_db(cfg)
        assert res == "ok"

        conn = dbmod.get_conn(cfg)
        tables = conn.execute(
            "SELECT count(*) c FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchone()["c"]
        conn.close()
        assert tables == 21, f"预期 21 表，实际 {tables}"

        assert dbmod.check_version(cfg) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def test_audit_append_only():
    root = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp(prefix="coach_test_"))
    try:
        (tmp / "db").mkdir()
        shutil.copy(root / "db" / "schema.sql", tmp / "db" / "schema.sql")
        shutil.copy(root / "db" / "seed.sql", tmp / "db" / "seed.sql")
        cfg = {"_root": str(tmp), "paths": {"db": "db/coach.db"}}
        dbmod.init_db(cfg)
        conn = dbmod.get_conn(cfg)
        dbmod.audit(conn, "test", "cli", {}, "ok", "ok")
        try:
            conn.execute("UPDATE audit_log SET result_status='x'")
            conn.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "audit_log UPDATE 应被 ABORT"
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 项通过")
