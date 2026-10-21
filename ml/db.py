from __future__ import annotations

from pathlib import Path

def ensure_schema(conn) -> None:
    sql = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
    for stmt in sql.replace("\r\n", "\n").split(";\n"):
        s = stmt.strip()
        if s:
            conn.execute(s + ";")
    conn.commit()
