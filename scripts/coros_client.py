"""COROS 官方 MCP 的最小客户端。

只依赖标准库。实现三段公开标准：
  - OAuth 2.0 动态客户端注册（RFC 7591）      -> /connect/register
  - OAuth 2.0 设备授权流程（RFC 8628）        -> /oauth2/device_authorization
  - MCP streamable HTTP（JSON-RPC 2.0）      -> POST {issuer}/mcp

不落任何业务逻辑：拉数据、映射入库由调用方负责。

用法：
    python -m scripts.coros_client login      # 设备码登录，token 存 data/coros/token.json
    python -m scripts.coros_client status     # 看 token 是否有效
    python -m scripts.coros_client tools      # 列出服务端实际提供的工具
    python -m scripts.coros_client call querySportRecords '{"startDate":"20261111"}'
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# 账号在中国大陆；其它区域改成 https://mcp.coros.com
DEFAULT_ISSUER = "https://mcpcn.coros.com"
SCOPE = "openid offline_access mcp.tools"
REDIRECT_URI = "http://127.0.0.1:43123/callback"
CLIENT_NAME = "LeaderCoach COROS Sync"

_ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = _ROOT / "data" / "coros" / "token.json"

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class CorosError(RuntimeError):
    pass


def _opener():
    """绕过系统代理：COROS 大陆端点直连可达，而本机代理端口可能不通。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """授权码回调靠 302 的 Location 拿，不能自动跟随。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = _opener()
_NO_REDIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _request(method: str, url: str, *, form: dict | None = None, body: dict | None = None,
             headers: dict | None = None, timeout: int = 20, no_redirect: bool = False):
    hdrs = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    opener = _NO_REDIRECT if no_redirect else _OPENER
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc


def _read_json(resp) -> dict:
    raw = resp.read().decode("utf-8", "replace")
    if not raw:
        return {}
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        # streamable HTTP 可能把结果放进 SSE 的 data: 行
        chunks, cur = [], []
        for line in raw.splitlines():
            if not line:
                if cur:
                    chunks.append("\n".join(cur))
                    cur = []
            elif line.startswith("data:"):
                cur.append(line[5:].lstrip())
        if cur:
            chunks.append("\n".join(cur))
        raw = chunks[-1] if chunks else ""
        if not raw:
            return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise CorosError(f"响应不是 JSON：{raw[:300]}") from exc


# ── OAuth ──────────────────────────────────────────────────

def discover(issuer: str = DEFAULT_ISSUER) -> dict:
    resp = _request("GET", f"{issuer}/.well-known/openid-configuration")
    meta = _read_json(resp)
    if resp.status != 200 or "token_endpoint" not in meta:
        raise CorosError(f"OAuth 发现失败：{resp.status} {meta}")
    return meta


def register_client(meta: dict) -> str:
    resp = _request("POST", meta["registration_endpoint"], body={
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token",
                        "urn:ietf:params:oauth:grant-type:device_code"],
        "response_types": ["code"],
        "scope": SCOPE,
        "token_endpoint_auth_method": "none",
    })
    payload = _read_json(resp)
    if resp.status not in (200, 201) or not payload.get("client_id"):
        raise CorosError(f"客户端注册失败：{resp.status} {payload}")
    return payload["client_id"]


def device_start(meta: dict, client_id: str) -> dict:
    """开一个 CLI 登录会话，拿浏览器授权链接。

    COROS 虽然公布了 device_authorization_endpoint，但动态注册的客户端用不了（401），
    实际可用的是 /api/v1/cli/login-sessions + claim 拿 loginTicket 的路径。
    """
    resp = _request("POST", f"{meta['issuer']}/api/v1/cli/login-sessions",
                   body={"clientId": client_id})
    payload = _read_json(resp)
    for field in ("sessionId", "pollToken", "loginUrl"):
        if not payload.get(field):
            raise CorosError(f"CLI 会话创建失败（缺 {field}）：{resp.status} {payload}")
    if resp.status not in (200, 201):
        raise CorosError(f"CLI 会话创建失败：{resp.status} {payload}")
    return payload


def _pkce() -> tuple[str, str]:
    import base64
    import hashlib
    import secrets
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _authorize_url(meta: dict, client_id: str, challenge: str, state: str,
                   login_ticket: str | None = None) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": f"{meta['issuer']}/mcp",
        "state": state,
    }
    if login_ticket:
        query["login_ticket"] = login_ticket
    return f"{meta['issuer']}/oauth2/authorize?{urllib.parse.urlencode(query)}"


def device_poll(meta: dict, session_id: str, poll_token: str, *,
                interval: int = 3, timeout: int = 600) -> str:
    """轮询到用户完成授权，返回 loginTicket。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _request("POST",
                        f"{meta['issuer']}/api/v1/cli/login-sessions/{session_id}/claim",
                        headers={"X-Poll-Token": poll_token})
        payload = _read_json(resp)
        if resp.status != 200:
            raise CorosError(f"poll 失败：{resp.status} {payload}")
        status = str(payload.get("status") or "").lower()
        if status == "authorized":
            ticket = payload.get("loginTicket")
            if not ticket:
                raise CorosError("已授权但没拿到 loginTicket」")
            return ticket
        if status == "pending":
            time.sleep(interval)
            continue
        if status in ("expired", "failed", "claimed"):
            raise CorosError(f"会话结束：{status} {payload.get('errorCode') or ''}")
        raise CorosError(f"未预期的会话状态：{status or payload}")
    raise CorosError("授权超时")


def PENDING_PATH():
    return _ROOT / "data" / "coros" / "pending.json"


def login_start(issuer: str = DEFAULT_ISSUER) -> dict:
    """第一步：注册客户端 + 开 CLI 会话，打印授权链接。"""
    meta = discover(issuer)
    client_id = register_client(meta)
    verifier, challenge = _pkce()
    state = base64_urlsafe(24)
    dev = device_start(meta, client_id)

    p = PENDING_PATH()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "issuer": issuer, "client_id": client_id, "verifier": verifier,
        "state": state, "session_id": dev["sessionId"],
        "poll_token": dev["pollToken"], "interval": int(dev.get("intervalSeconds") or 3),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n请用浏览器打开下面的链接，登录 COROS 并点“同意”：\n")
    print(f"  {dev['loginUrl']}\n")
    return dev


def base64_urlsafe(n: int) -> str:
    import base64
    import secrets
    return base64.urlsafe_b64encode(secrets.token_bytes(n)).decode().rstrip("=")


def login_finish(timeout: int = 600) -> dict:
    """第二步：轮询 → 拿 ticket → 换 authorization code → 换 token。"""
    p = PENDING_PATH()
    if not p.exists():
        raise CorosError("没有待完成的授权，先运行 login-start")
    pending = json.loads(p.read_text(encoding="utf-8"))
    meta = discover(pending["issuer"])

    ticket = device_poll(meta, pending["session_id"], pending["poll_token"],
                         interval=pending["interval"], timeout=timeout)

    url = _authorize_url(meta, pending["client_id"], _challenge_from(pending["verifier"]),
                         pending["state"], login_ticket=ticket)
    resp = _request("GET", url, no_redirect=True)
    location = resp.headers.get("Location") or ""
    if resp.status not in (301, 302, 303, 307) or not location:
        raise CorosError(f"授权未返回回调：{resp.status} {location[:200]}")

    q = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    code = (q.get("code") or [""])[0]
    got_state = (q.get("state") or [""])[0]
    if not code:
        raise CorosError(f"回调里没有 code：{location[:200]}")
    if got_state != pending["state"]:
        raise CorosError("state 不匹配，放弃本次授权")

    tok = _request("POST", meta["token_endpoint"], form={
        "grant_type": "authorization_code",
        "client_id": pending["client_id"],
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": pending["verifier"],
    })
    payload = _read_json(tok)
    if tok.status != 200 or not payload.get("access_token"):
        raise CorosError(f"换 token 失败：{tok.status} {payload}")

    payload["client_id"] = pending["client_id"]
    payload["issuer"] = pending["issuer"]
    payload["obtained_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_token(payload)
    p.unlink(missing_ok=True)
    print(f"授权成功，token 已保存到 {TOKEN_PATH}")
    return payload


def _challenge_from(verifier: str) -> str:
    import base64
    import hashlib
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def login(issuer: str = DEFAULT_ISSUER, timeout: int = 600) -> dict:
    login_start(issuer)
    return login_finish(timeout=timeout)


def save_token(token: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")
    try:  # 尽量收紧权限（Windows 上 chmod 只是尽力而为）
        TOKEN_PATH.chmod(0o600)
    except OSError:
        pass


def load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _expired(token: dict, skew: int = 60) -> bool:
    exp = token.get("expires_in")
    got = token.get("obtained_at")
    if not exp or not got:
        return False
    try:
        start = time.mktime(time.strptime(got, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False
    return time.time() > start + int(exp) - skew


def ensure_token(issuer: str = DEFAULT_ISSUER) -> dict:
    token = load_token()
    if token is None:
        raise CorosError("还没有 token，先运行：python -m scripts.coros_client login")
    if not _expired(token):
        return token
    if not token.get("refresh_token"):
        raise CorosError("token 已过期且没有 refresh_token，请重新 login")
    meta = discover(token.get("issuer") or issuer)
    resp = _request("POST", meta["token_endpoint"], form={
        "grant_type": "refresh_token",
        "client_id": token.get("client_id", ""),
        "refresh_token": token["refresh_token"],
    })
    payload = _read_json(resp)
    if resp.status != 200 or not payload.get("access_token"):
        raise CorosError(f"刷新 token 失败，请重新 login：{resp.status} {payload}")
    payload["client_id"] = token.get("client_id", "")
    payload["issuer"] = token.get("issuer") or issuer
    payload["obtained_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_token(payload)
    return payload


# ── MCP ───────────────────────────────────────────────────

def _mcp_post(token: dict, body: dict) -> dict:
    issuer = token.get("issuer") or DEFAULT_ISSUER
    resp = _request("POST", f"{issuer}/mcp", body=body, headers={
        "Authorization": f"{token.get('token_type', 'Bearer')} {token['access_token']}",
        "Accept": "application/json, text/event-stream",
    })
    payload = _read_json(resp)
    if resp.status != 200:
        raise CorosError(f"MCP 请求失败：{resp.status} {payload}")
    if "error" in payload:
        raise CorosError(f"MCP 返回错误：{payload['error']}")
    return payload


def _mcp_call(token: dict, request_id: int, method: str, params: dict | None = None) -> dict:
    return _mcp_post(token, {"jsonrpc": "2.0", "id": request_id, "method": method,
                             "params": params or {}})


def _init(token: dict) -> None:
    _mcp_call(token, 1, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": CLIENT_NAME, "version": "1.0.0"},
    })


def list_tools(token: dict | None = None) -> list[dict]:
    token = token or ensure_token()
    _init(token)
    out, cursor, rid = [], None, 2
    while True:
        params = {"cursor": cursor} if cursor else {}
        result = _mcp_call(token, rid, "tools/list", params).get("result") or {}
        out.extend(result.get("tools") or [])
        cursor = result.get("nextCursor")
        if not cursor:
            return out
        rid += 1


def call_tool(name: str, arguments: dict | None = None, token: dict | None = None) -> dict:
    token = token or ensure_token()
    _init(token)
    result = _mcp_call(token, 2, "tools/call",
                       {"name": name, "arguments": arguments or {}}).get("result") or {}
    return result


def structured(result: dict) -> Any:
    """把 MCP 工具结果里的内容尽量还原成 Python 对象。"""
    if "structuredContent" in result:
        return result["structuredContent"]
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text") or ""
            try:
                parts.append(json.loads(text))
            except ValueError:
                parts.append(text)
        else:
            parts.append(item)
    if len(parts) == 1:
        return parts[0]
    return parts


# ── CLI ───────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"

    if cmd == "login-start":
        issuer = args[1] if len(args) > 1 else DEFAULT_ISSUER
        login_start(issuer)
        return 0

    if cmd == "login-finish":
        timeout = int(args[1]) if len(args) > 1 else 600
        login_finish(timeout=timeout)
        return 0

    if cmd == "login":
        issuer = args[1] if len(args) > 1 else DEFAULT_ISSUER
        login(issuer)
        return 0

    if cmd == "status":
        token = load_token()
        if not token:
            print("没有 token")
            return 1
        print(json.dumps({k: token.get(k) for k in
                          ("issuer", "scope", "token_type", "obtained_at", "expires_in")},
                         ensure_ascii=False, indent=2))
        print(f"过期：{_expired(token)}")
        return 0

    if cmd == "tools":
        tools = list_tools()
        for t in tools:
            desc = (t.get("description") or "").splitlines()[0][:70]
            print(f"{t.get('name'):38s} {desc}")
        print(f"\n共 {len(tools)} 个工具")
        return 0

    if cmd == "call":
        if len(args) < 2:
            print("用法：call <tool> [json-args]")
            return 2
        argv_json = json.loads(args[2]) if len(args) > 2 else {}
        print(json.dumps(structured(call_tool(args[1], argv_json)),
                         ensure_ascii=False, indent=2))
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
