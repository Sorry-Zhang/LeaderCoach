from __future__ import annotations

import hashlib
import json
import secrets
import time

class PendingStore:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._pending: dict = {}

    def _business(self, tool: str, params: dict) -> str:
        clean = {k: v for k, v in (params or {}).items()
                 if k not in ("confirm", "confirm_code")}
        return json.dumps([tool, clean], sort_keys=True, ensure_ascii=False, default=str)

    def _hash(self, tool: str, params: dict) -> str:
        return hashlib.sha256(self._business(tool, params).encode("utf-8")).hexdigest()

    def open(self, tool: str, params: dict, needs_code: bool) -> tuple[str, str | None]:
        h = self._hash(tool, params)
        code = f"{secrets.randbelow(10000):04d}" if needs_code else None
        self._pending[h] = {
            "tool": tool, "params": params, "code": code,
            "expires_at": time.time() + self.cfg["confirm"]["ttl_seconds"],
            "attempts": 0, "needs_code": needs_code,
        }
        return h, code

    def consume(self, tool: str, params: dict, confirm_code: str | None = None) -> dict:
        h = self._hash(tool, params)
        p = self._pending.get(h)
        if not p or time.time() > p["expires_at"]:
            self._pending.pop(h, None)
            return {"ok": False, "error": "E_PENDING_EXPIRED",
                    "message": "确认已过期，请重新发起"}
        if p["needs_code"]:
            if not confirm_code:
                return {"ok": False, "error": "E_CONFIRM_REQUIRED",
                        "message": "请提供确认码"}
            p["attempts"] += 1
            if p["attempts"] > self.cfg["confirm"]["max_attempts"]:
                self._pending.pop(h, None)
                return {"ok": False, "error": "E_CONFIRM_INVALID",
                        "message": "尝试次数超限，已作废，请重新发起"}
            if confirm_code != p["code"]:
                return {"ok": False, "error": "E_CONFIRM_INVALID",
                        "message": "确认码错误"}
        self._pending.pop(h, None)
        return {"ok": True}
