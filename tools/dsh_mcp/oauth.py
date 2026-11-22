# -*- coding: utf-8 -*-
r"""内置轻量 OAuth 2.1 授权层（给远程 MCP 客户端用，无需外部 IdP）。

实现：
  * RFC 9728 受保护资源元数据（resource metadata）
  * RFC 8414 授权服务器元数据（authorization server metadata）
  * RFC 7591 动态客户端注册（DCR）
  * RFC 7636 PKCE（仅 S256）+ 授权码 + 刷新令牌
  * 授权口令直接复用启动网关时的 DSH_MCP_TOKEN（常数时间比较）

公网 Origin 动态推导：
  X-Forwarded-Proto / X-Forwarded-Host / Host（兼容 trycloudflare 临时域名），
  可用环境变量 DSH_MCP_PUBLIC_URL 显式覆盖。

状态落盘：默认 runtime/dsh_mcp_oauth.json（可用 DSH_MCP_OAUTH_FILE 覆盖）；
文件里只存 token 的 SHA-256 摘要，不存明文。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

import common

DEFAULT_SCOPE = "mcp"
CODE_TTL = 300                 # 授权码 5 分钟
ACCESS_TTL = 24 * 3600         # access_token 24 小时
REFRESH_TTL = 30 * 24 * 3600   # refresh_token 30 天
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"

_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")


# --------------------------------------------------------------------------- #
# 基础工具                                                                      #
# --------------------------------------------------------------------------- #

def _now() -> float:
    return time.time()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _s256(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())


def _safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", errors="replace"),
                               b.encode("utf-8", errors="replace"))


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


def public_origin(request: Request) -> str:
    """从请求头推导公网 Origin；DSH_MCP_PUBLIC_URL 优先。"""
    override = (os.environ.get("DSH_MCP_PUBLIC_URL") or "").strip().rstrip("/")
    if override:
        return override

    headers = request.headers
    proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = request.url.scheme or "http"

    host = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not host:
        host = (headers.get("host") or "").strip()
    if not host or not _HOST_RE.match(host):
        host = request.url.netloc or "127.0.0.1:8795"
    return "%s://%s" % (proto, host)


def store_path() -> Path:
    override = (os.environ.get("DSH_MCP_OAUTH_FILE") or "").strip()
    return Path(override) if override else (common.RUNTIME_DIR / "dsh_mcp_oauth.json")


def resource_metadata_url(origin: str) -> str:
    return origin + RESOURCE_METADATA_PATH


# --------------------------------------------------------------------------- #
# 轻量 OAuth 存储                                                               #
# --------------------------------------------------------------------------- #

def _log(message: str) -> None:
    print("[oauth] " + message, file=sys.stderr, flush=True)


def _valid_redirect_uri(uri) -> bool:
    if not isinstance(uri, str) or not uri or len(uri) > 2000:
        return False
    try:
        parts = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment:
        return False
    if parts.scheme == "https":
        return bool(parts.netloc)
    if parts.scheme == "http" and (parts.hostname or "").lower() in ("127.0.0.1", "localhost", "::1"):
        return True
    # 部分 MCP 客户端用自定义 scheme 回调（如 com.example.app:/oauth2redirect）。
    # 授权码只有在用户输入本机口令后才会签发，因此这里放行非危险 scheme。
    if (parts.scheme and parts.scheme not in ("javascript", "data", "file", "vbscript", "about")
            and (parts.netloc or parts.path)):
        return True
    return False


class OAuthStore:
    """线程安全 + 原子落盘的 OAuth 状态（clients / codes / tokens）。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    # ---- 持久化 ----

    def _blank(self) -> dict:
        return {"version": 1, "clients": {}, "codes": {},
                "access_tokens": {}, "refresh_tokens": {}}

    def _load(self) -> dict:
        data = common.read_json(self.path, None)
        if not isinstance(data, dict):
            data = self._blank()
        for key in ("clients", "codes", "access_tokens", "refresh_tokens"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        data["version"] = 1
        return data

    def _prune_locked(self) -> None:
        now = _now()
        self._data["codes"] = {
            k: v for k, v in self._data["codes"].items()
            if v.get("expires_at", 0) > now
        }
        self._data["access_tokens"] = {
            k: v for k, v in self._data["access_tokens"].items()
            if v.get("expires_at", 0) > now
        }
        self._data["refresh_tokens"] = {
            k: v for k, v in self._data["refresh_tokens"].items()
            if v.get("expires_at", 0) > now
        }

    def _save_locked(self) -> None:
        self._prune_locked()
        common.write_json(self.path, self._data)

    # ---- 动态客户端注册（RFC 7591）----

    def register_client(self, payload: dict) -> dict:
        redirect_uris = payload.get("redirect_uris")
        if isinstance(redirect_uris, str):
            redirect_uris = [redirect_uris]
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise OAuthError("invalid_redirect_uri", "redirect_uris 必填且不能为空")
        if len(redirect_uris) > 10:
            raise OAuthError("invalid_redirect_uri", "redirect_uris 最多 10 个")
        for uri in redirect_uris:
            if not _valid_redirect_uri(uri):
                raise OAuthError(
                    "invalid_redirect_uri",
                    "redirect_uri 只接受 https:// 或 http://127.0.0.1|localhost 回环地址：%r" % (uri,),
                )

        client_id = "dsh_client_" + secrets.token_hex(16)
        record = {
            "client_id": client_id,
            "client_name": str(payload.get("client_name") or "MCP 客户端")[:120],
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "created_at": _now(),
        }
        with self._lock:
            self._data["clients"][client_id] = record
            self._save_locked()
        return {
            "client_id": client_id,
            "client_id_issued_at": int(record["created_at"]),
            "client_name": record["client_name"],
            "redirect_uris": record["redirect_uris"],
            "grant_types": record["grant_types"],
            "response_types": record["response_types"],
            "token_endpoint_auth_method": "none",
        }

    def get_client(self, client_id: str) -> dict | None:
        if not client_id:
            return None
        with self._lock:
            record = self._data["clients"].get(client_id)
            return dict(record) if record else None

    # ---- 授权码 ----

    def issue_code(self, client_id: str, redirect_uri: str, code_challenge: str,
                   scope: str | None = None) -> str:
        code = _new_token("dsh_code_")
        record = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": scope or DEFAULT_SCOPE,
            "created_at": _now(),
            "expires_at": _now() + CODE_TTL,
        }
        with self._lock:
            self._data["codes"][_digest(code)] = record
            self._save_locked()
        return code

    def exchange_code(self, code: str, client_id: str | None, redirect_uri: str | None,
                      code_verifier: str | None) -> dict:
        if not code or not code_verifier:
            raise OAuthError("invalid_request", "code 与 code_verifier 必填")
        with self._lock:
            key = _digest(code)
            record = self._data["codes"].get(key)
            if not record:
                raise OAuthError("invalid_grant", "授权码无效、已使用或已过期")
            if record.get("expires_at", 0) < _now():
                self._data["codes"].pop(key, None)
                self._save_locked()
                raise OAuthError("invalid_grant", "授权码已过期")
            if client_id and client_id != record["client_id"]:
                raise OAuthError("invalid_grant", "client_id 与授权码不匹配")
            if redirect_uri and redirect_uri != record["redirect_uri"]:
                raise OAuthError("invalid_grant", "redirect_uri 与授权码不匹配")
            if not _safe_eq(_s256(code_verifier), record["code_challenge"]):
                raise OAuthError("invalid_grant", "PKCE code_verifier 校验失败")
            self._data["codes"].pop(key, None)
            tokens = self._issue_tokens_locked(record["client_id"], record.get("scope"))
            self._save_locked()
            return tokens

    # ---- 令牌 ----

    def _issue_tokens_locked(self, client_id: str, scope: str | None) -> dict:
        access_token = _new_token("dsh_at_")
        refresh_token = _new_token("dsh_rt_")
        now = _now()
        scope = scope or DEFAULT_SCOPE
        self._data["access_tokens"][_digest(access_token)] = {
            "client_id": client_id,
            "scope": scope,
            "issued_at": now,
            "expires_at": now + ACCESS_TTL,
        }
        self._data["refresh_tokens"][_digest(refresh_token)] = {
            "client_id": client_id,
            "scope": scope,
            "issued_at": now,
            "expires_at": now + REFRESH_TTL,
        }
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh_token,
            "scope": scope,
        }

    def validate_access_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self._lock:
            record = self._data["access_tokens"].get(_digest(token))
            if not record or record.get("expires_at", 0) < _now():
                return None
            return dict(record)

    def refresh(self, refresh_token: str, client_id: str | None) -> dict:
        if not refresh_token:
            raise OAuthError("invalid_request", "refresh_token 必填")
        with self._lock:
            key = _digest(refresh_token)
            record = self._data["refresh_tokens"].get(key)
            if not record or record.get("expires_at", 0) < _now():
                raise OAuthError("invalid_grant", "refresh_token 无效或已过期")
            if client_id and client_id != record["client_id"]:
                raise OAuthError("invalid_grant", "client_id 与 refresh_token 不匹配")
            # 轮换 refresh_token
            self._data["refresh_tokens"].pop(key, None)
            tokens = self._issue_tokens_locked(record["client_id"], record.get("scope"))
            self._save_locked()
            return tokens


# --------------------------------------------------------------------------- #
# 请求体解析                                                                    #
# --------------------------------------------------------------------------- #

async def _read_payload(request: Request) -> dict:
    body = await request.body()
    content_type = (request.headers.get("content-type") or "").lower()
    stripped = body.lstrip()
    if "application/json" in content_type or stripped.startswith(b"{"):
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as ex:  # noqa: BLE001
            raise OAuthError("invalid_request", "JSON 解析失败：%s" % ex)
        if not isinstance(data, dict):
            raise OAuthError("invalid_request", "请求体必须是 JSON 对象")
        return data
    try:
        parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except Exception as ex:  # noqa: BLE001
        raise OAuthError("invalid_request", "表单解析失败：%s" % ex)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def _client_credentials(request: Request, payload: dict) -> tuple[str | None, str | None]:
    client_id = payload.get("client_id")
    client_secret = payload.get("client_secret")
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("basic "):
        try:
            raw = base64.b64decode(auth[6:].strip()).decode("utf-8")
            basic_id, _, basic_secret = raw.partition(":")
            if not client_id:
                client_id = urllib.parse.unquote(basic_id)
            if not client_secret:
                client_secret = urllib.parse.unquote(basic_secret)
        except Exception:  # noqa: BLE001
            pass
    return (str(client_id) if client_id else None,
            str(client_secret) if client_secret else None)


# --------------------------------------------------------------------------- #
# 授权页                                                                        #
# --------------------------------------------------------------------------- #

_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
     font-family:-apple-system,"Segoe UI","Microsoft YaHei",Roboto,sans-serif;
     background:#f4f6fb;color:#1c2333;padding:24px}
@media (prefers-color-scheme:dark){body{background:#0f141d;color:#e8ecf5}}
.card{width:100%;max-width:460px;background:#fff;border-radius:16px;padding:28px 28px 24px;
      box-shadow:0 12px 40px rgba(20,30,60,.14)}
@media (prefers-color-scheme:dark){.card{background:#171e2b;box-shadow:0 12px 40px rgba(0,0,0,.5)}}
h1{font-size:19px;margin:0 0 14px}
p{font-size:14px;line-height:1.6;margin:10px 0}
code{font-size:12px;word-break:break-all;background:rgba(120,140,180,.16);
     padding:2px 6px;border-radius:6px}
label{display:block;font-size:13px;margin:18px 0 8px;font-weight:600}
input[type=password]{width:100%;padding:11px 12px;font-size:14px;border-radius:10px;
     border:1px solid rgba(120,140,180,.5);background:transparent;color:inherit;outline:none}
input[type=password]:focus{border-color:#4f7cff;box-shadow:0 0 0 3px rgba(79,124,255,.18)}
button{width:100%;margin-top:18px;padding:12px;font-size:15px;font-weight:600;color:#fff;
     background:linear-gradient(135deg,#4f7cff,#6f5cff);border:0;border-radius:10px;cursor:pointer}
button:hover{filter:brightness(1.06)}
.hint{font-size:12px;opacity:.72;margin-top:14px}
.err{color:#d83a3a;font-weight:600}
"""


def _render_authorize_page(client: dict, params: dict, error: str | None = None) -> str:
    name = html.escape(str(client.get("client_name") or "MCP 客户端"))
    redirect_uri = html.escape(str(params.get("redirect_uri") or ""), quote=True)
    order = ("response_type", "client_id", "redirect_uri", "state",
             "code_challenge", "code_challenge_method", "scope")
    hidden = "".join(
        '<input type="hidden" name="%s" value="%s">' % (
            html.escape(key, quote=True),
            html.escape(str(params.get(key) or ""), quote=True),
        )
        for key in order
    )
    err_block = '<p class="err">%s</p>' % html.escape(error) if error else ""
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DSH MCP 授权</title><style>%s</style></head>
<body><main class="card">
<h1>DSH MCP 授权</h1>
<p><b>%s</b> 请求访问本机 DSH 网关。</p>
<p>回调地址：<code>%s</code></p>
%s
<form method="post" action="/oauth/authorize">
%s
<label for="passphrase">本机授权口令（启动网关时设置的 DSH_MCP_TOKEN）</label>
<input id="passphrase" type="password" name="passphrase" autocomplete="current-password"
       placeholder="粘贴 DSH_MCP_TOKEN" required autofocus>
<button type="submit">允许授权</button>
</form>
<p class="hint">口令只在本机比对，不会通过网络回传。授权码 5 分钟内有效。</p>
</main></body></html>""" % (_CSS, name, redirect_uri, err_block, hidden)


def _html_error(message: str, status: int = 400) -> HTMLResponse:
    body = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DSH MCP 授权失败</title><style>%s</style></head>
<body><main class="card"><h1>授权请求无效</h1>
<p class="err">%s</p></main></body></html>""" % (_CSS, html.escape(message))
    return HTMLResponse(body, status_code=status, headers=NO_STORE)


def _oauth_json_error(ex: OAuthError) -> JSONResponse:
    return JSONResponse(
        {"error": ex.error, "error_description": ex.description},
        status_code=ex.status, headers=NO_STORE,
    )


def _authorize_params(source) -> dict:
    def get(key: str) -> str:
        value = source.get(key)
        return str(value) if value is not None else ""

    if get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "只支持 response_type=code")
    client_id = get("client_id")
    if not client_id:
        raise OAuthError("invalid_request", "client_id 必填")
    redirect_uri = get("redirect_uri")
    if not redirect_uri:
        raise OAuthError("invalid_request", "redirect_uri 必填")
    code_challenge = get("code_challenge")
    if not code_challenge:
        raise OAuthError("invalid_request", "code_challenge 必填（PKCE S256）")
    method = (get("code_challenge_method") or "S256").upper()
    if method != "S256":
        raise OAuthError("invalid_request", "只支持 code_challenge_method=S256")
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": get("state"),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": get("scope"),
    }


def _append_query(uri: str, **values) -> str:
    parts = urllib.parse.urlsplit(uri)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, v) for k, v in values.items() if v is not None]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(query), parts.fragment)
    )


# --------------------------------------------------------------------------- #
# 路由                                                                          #
# --------------------------------------------------------------------------- #

def make_routes(store: OAuthStore, passphrase: str) -> list[Route]:
    """返回发现 / DCR / 授权 / 令牌端点。"""

    async def protected_resource(request: Request):
        origin = public_origin(request)
        return JSONResponse({
            "resource": origin + "/mcp",
            "authorization_servers": [origin],
            "bearer_methods_supported": ["header"],
        }, headers=NO_STORE)

    async def auth_server_metadata(request: Request):
        origin = public_origin(request)
        return JSONResponse({
            "issuer": origin,
            "authorization_endpoint": origin + "/oauth/authorize",
            "token_endpoint": origin + "/oauth/token",
            "registration_endpoint": origin + "/oauth/register",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported":
                ["none", "client_secret_post", "client_secret_basic"],
            "scopes_supported": [DEFAULT_SCOPE],
        }, headers=NO_STORE)

    async def register(request: Request):
        try:
            payload = await _read_payload(request)
            result = store.register_client(payload)
        except OAuthError as ex:
            _log("register rejected: %s (%s)" % (ex.error, ex.description))
            return _oauth_json_error(ex)
        _log("register ok: client_id=%s name=%r redirect_uris=%r"
             % (result["client_id"], result.get("client_name"),
                result.get("redirect_uris")))
        return JSONResponse(result, status_code=201, headers=NO_STORE)

    async def authorize_get(request: Request):
        try:
            params = _authorize_params(request.query_params)
            client = store.get_client(params["client_id"])
            if not client:
                raise OAuthError("invalid_client", "未知 client_id")
            if params["redirect_uri"] not in client["redirect_uris"]:
                raise OAuthError("invalid_request", "redirect_uri 未注册")
        except OAuthError as ex:
            _log("authorize GET rejected: %s (%s)" % (ex.error, ex.description))
            return _html_error(ex.description, ex.status if ex.status != 400 else 400)
        return HTMLResponse(_render_authorize_page(client, params), headers=NO_STORE)

    async def authorize_post(request: Request):
        try:
            payload = await _read_payload(request)
            params = _authorize_params(payload)
            client = store.get_client(params["client_id"])
            if not client:
                raise OAuthError("invalid_client", "未知 client_id")
            if params["redirect_uri"] not in client["redirect_uris"]:
                raise OAuthError("invalid_request", "redirect_uri 未注册")
        except OAuthError as ex:
            _log("authorize POST rejected: %s (%s)" % (ex.error, ex.description))
            return _html_error(ex.description, 400)

        supplied = str(payload.get("passphrase") or "")
        if not _safe_eq(supplied, passphrase):
            _log("authorize POST wrong passphrase for client %s" % params["client_id"])
            return HTMLResponse(
                _render_authorize_page(client, params, error="授权口令不正确，请重试。"),
                status_code=401, headers=NO_STORE,
            )

        code = store.issue_code(params["client_id"], params["redirect_uri"],
                               params["code_challenge"], params.get("scope"))
        _log("authorize ok: client_id=%s，已签发授权码" % params["client_id"])
        location = _append_query(params["redirect_uri"], code=code,
                                 state=params.get("state"))
        return RedirectResponse(location, status_code=302, headers=NO_STORE)

    async def token(request: Request):
        try:
            payload = await _read_payload(request)
        except OAuthError as ex:
            return _oauth_json_error(ex)
        grant_type = str(payload.get("grant_type") or "")
        client_id, _client_secret = _client_credentials(request, payload)
        try:
            if grant_type == "authorization_code":
                tokens = store.exchange_code(
                    str(payload.get("code") or ""),
                    client_id,
                    str(payload.get("redirect_uri") or "") or None,
                    str(payload.get("code_verifier") or "") or None,
                )
                _log("token ok: authorization_code -> client %s" % client_id)
                return JSONResponse(tokens, headers=NO_STORE)
            if grant_type == "refresh_token":
                tokens = store.refresh(
                    str(payload.get("refresh_token") or ""), client_id)
                _log("token ok: refresh_token -> client %s" % client_id)
                return JSONResponse(tokens, headers=NO_STORE)
        except OAuthError as ex:
            _log("token rejected: %s (%s)" % (ex.error, ex.description))
            return _oauth_json_error(ex)
        return _oauth_json_error(OAuthError(
            "unsupported_grant_type", "只支持 authorization_code / refresh_token"))

    routes = [
        Route(RESOURCE_METADATA_PATH, protected_resource),
        Route(RESOURCE_METADATA_PATH + "/mcp", protected_resource),
        Route(AS_METADATA_PATH, auth_server_metadata),
        Route(AS_METADATA_PATH + "/mcp", auth_server_metadata),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authorize_get, methods=["GET"]),
        Route("/oauth/authorize", authorize_post, methods=["POST"]),
        Route("/oauth/token", token, methods=["POST"]),
    ]
    return routes
