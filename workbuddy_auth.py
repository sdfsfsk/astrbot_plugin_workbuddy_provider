"""Self-contained WorkBuddy / CodeBuddy login and credential persistence.

The plugin performs the same headless device-authorization flow the official
CodeBuddy CLI uses, so it never depends on the ``workbuddy2api`` gateway or on
any external directory:

1. ``POST {base}/v2/plugin/auth/state?platform=CLI`` returns ``state`` + ``authUrl``.
2. The administrator opens ``authUrl`` in a browser and signs in.
3. ``GET {base}/v2/plugin/auth/token?state=...`` returns the token bundle.
4. ``GET {base}/v2/plugin/login/account?state=...`` returns uid / nickname.

Credentials are stored under AstrBot's plugin-data directory. Writes are
serialized with a file lock, flushed, and atomically replaced; on Windows the
payload is additionally encrypted for the current user with DPAPI when pywin32
is importable.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock

from astrbot import logger
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)

# Realm-specific upstream endpoints. CN login talks to copilot.tencent.com while
# the CodeBuddy web origin stays codebuddy.cn; the global realm is same-origin.
WORKBUDDY_REALM_CN = "cn"
WORKBUDDY_REALM_GLOBAL = "global"
WORKBUDDY_REALMS = (WORKBUDDY_REALM_CN, WORKBUDDY_REALM_GLOBAL)

# Login-time fingerprint, deliberately different from the runtime chat UA.
LOGIN_USER_AGENT = "CLI/2.63.2 CodeBuddy/2.63.2"

LOGIN_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

AUTH_STORE_VERSION = 1
LOGIN_POLL_INTERVAL_SECONDS = 3.0
LOGIN_TIMEOUT_SECONDS = 15 * 60
_PLUGIN_DATA_DIRNAME = "astrbot_plugin_workbuddy_provider"
_AUTH_STORE_FILENAME = "workbuddy_auth.json"


class WorkBuddyLoginPending(Exception):
    """Raised while the browser authorization has not completed yet."""


def token_fingerprint(token: str) -> str:
    """Return a non-secret stable fingerprint for OAuth-copy ownership."""
    return hashlib.sha256(token.encode()).hexdigest()


def realm_endpoints(realm: str) -> tuple[str, str]:
    """Return ``(api_base, web_origin)`` for one account realm.

    Args:
        realm: ``"cn"`` or ``"global"`` (case-insensitive); anything else falls
            back to ``"cn"``.

    Returns:
        The upstream API base URL and the Origin/Referer web origin.
    """
    if str(realm or "").strip().lower() == WORKBUDDY_REALM_GLOBAL:
        return "https://www.workbuddy.ai", "https://www.workbuddy.ai"
    return "https://copilot.tencent.com", "https://www.codebuddy.cn"


def create_client(proxy: str | None = None, timeout: float = 30) -> httpx.AsyncClient:
    """Create an httpx client that only honours the explicitly configured proxy.

    Both upstream realms are service-region locked, so ambient ``HTTP_PROXY`` /
    ``NO_PROXY`` environment variables must not silently reroute traffic; some
    NO_PROXY spellings (``::1`` / ``[::1]``) even make httpx raise while parsing
    its proxy map.

    Args:
        proxy: Optional proxy URL; empty or None means a direct connection.
        timeout: Request timeout in seconds.

    Returns:
        A configured ``httpx.AsyncClient``.
    """
    resolved = str(proxy or "").strip()
    return httpx.AsyncClient(
        proxy=resolved or None,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )


def normalize_realm(value: Any) -> str:
    """Normalize a realm setting, inferring global from workbuddy.ai domains.

    Args:
        value: Raw realm value, possibly empty or a domain string.

    Returns:
        ``"cn"`` or ``"global"``.
    """
    text = str(value or "").strip().lower()
    if text == WORKBUDDY_REALM_GLOBAL:
        return WORKBUDDY_REALM_GLOBAL
    if text.endswith("workbuddy.ai"):
        return WORKBUDDY_REALM_GLOBAL
    return WORKBUDDY_REALM_CN


def decode_jwt_payload(token: str) -> dict | None:
    """Decode a JWT payload without verifying its signature.

    Args:
        token: The raw token string.

    Returns:
        The decoded payload mapping, or None when the token is not a JWT.
    """
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return None
    return decoded if isinstance(decoded, dict) else None


def token_expiry(token: str) -> int | None:
    """Return the ``exp`` claim of a token in Unix seconds, if present."""
    payload = decode_jwt_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    return int(exp) if isinstance(exp, int | float) else None


def _plugin_data_dir() -> Path:
    """Return the plugin data directory and migrate a legacy location."""
    data_dir = Path(get_astrbot_plugin_data_path()) / _PLUGIN_DATA_DIRNAME
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        data_dir.chmod(0o700)
    legacy = Path(get_astrbot_data_path()) / _PLUGIN_DATA_DIRNAME / _AUTH_STORE_FILENAME
    target = data_dir / _AUTH_STORE_FILENAME
    if legacy.is_file() and target.exists():
        raise RuntimeError(
            "同时发现新旧两份 WorkBuddy 登录凭据，请备份后删除旧版凭据文件。"
        )
    if legacy.is_file():
        try:
            legacy.replace(target)
            if os.name != "nt":
                target.chmod(0o600)
        except FileNotFoundError:
            if not target.exists():
                raise
        except OSError as e:
            raise RuntimeError(
                "无法迁移旧版 WorkBuddy 登录凭据，请检查数据目录权限。"
            ) from e
    return data_dir


def _auth_store_path() -> Path:
    """Return the canonical credential document path."""
    return _plugin_data_dir() / _AUTH_STORE_FILENAME


def _auth_store_lock(path: Path) -> FileLock:
    """Return the cross-process lock associated with the credential document."""
    return FileLock(str(path.with_suffix(".lock")), timeout=10)


def _parse_auth_store(value: Any) -> dict:
    """Validate a credential document without echoing secret material."""
    if not isinstance(value, dict):
        raise ValueError("WorkBuddy 登录凭据文件必须包含 JSON 对象。")
    version = value.get("version", AUTH_STORE_VERSION)
    if version != AUTH_STORE_VERSION:
        raise ValueError(f"不支持的 WorkBuddy 登录凭据版本：{version}")
    allowed = {
        "version",
        "access_token",
        "refresh_token",
        "expires_at",
        "uid",
        "enterprise_id",
        "nickname",
        "domain",
        "realm",
        "managed_token_hashes",
    }
    unknown = [key for key in value if key not in allowed]
    if unknown:
        raise ValueError("WorkBuddy 登录凭据文件包含未知字段。")
    access_token = value.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("WorkBuddy 登录凭据缺少访问令牌。")
    refresh_token = value.get("refresh_token") or ""
    if not isinstance(refresh_token, str):
        raise ValueError("WorkBuddy 登录凭据包含无效的刷新令牌。")
    expires_at = value.get("expires_at")
    if expires_at is not None and (
        isinstance(expires_at, bool) or not isinstance(expires_at, int | float)
    ):
        raise ValueError("WorkBuddy 登录凭据包含无效的过期时间。")
    managed_hashes = value.get(
        "managed_token_hashes", [token_fingerprint(access_token)]
    )
    if not isinstance(managed_hashes, list) or any(
        not isinstance(item, str)
        or len(item) != 64
        or any(char not in "0123456789abcdef" for char in item)
        for item in managed_hashes
    ):
        raise ValueError("WorkBuddy 登录凭据包含无效的 OAuth token 指纹。")
    domain = value.get("domain") or ""
    return {
        "version": AUTH_STORE_VERSION,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(expires_at) if expires_at else 0,
        "uid": str(value.get("uid") or ""),
        "enterprise_id": str(value.get("enterprise_id") or ""),
        "nickname": str(value.get("nickname") or ""),
        "domain": str(domain),
        "realm": normalize_realm(value.get("realm") or domain),
        "managed_token_hashes": sorted(
            set(managed_hashes) | {token_fingerprint(access_token)}
        ),
    }


def _windows_dpapi_protect(data: bytes) -> bytes:
    """Encrypt credential bytes for the current Windows user via DPAPI."""
    try:
        import win32crypt
    except ImportError as e:
        raise RuntimeError("Windows 缺少 pywin32，无法加密 WorkBuddy 凭据。") from e
    try:
        return win32crypt.CryptProtectData(
            data, "AstrBot WorkBuddy OAuth", None, None, None, 0x01
        )
    except Exception as e:  # pragma: no cover - depends on host DPAPI state
        raise RuntimeError("Windows DPAPI 加密 WorkBuddy 凭据失败。") from e


def _windows_dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt credential bytes for the current Windows user via DPAPI."""
    try:
        import win32crypt
    except ImportError as e:
        raise RuntimeError("Windows 缺少 pywin32，无法读取 WorkBuddy 凭据。") from e
    try:
        return win32crypt.CryptUnprotectData(data, None, None, None, 0x01)[1]
    except Exception as e:  # pragma: no cover - depends on host DPAPI state
        raise RuntimeError(
            "Windows DPAPI 无法解密 WorkBuddy 凭据；凭据可能属于其他用户。"
        ) from e


def _dpapi_available() -> bool:
    """Report whether current-user DPAPI encryption can be used."""
    if os.name != "nt":
        return False
    try:
        import win32crypt  # noqa: F401
    except ImportError:
        return False
    return True


def _serialize_auth_store(store: dict) -> str:
    """Serialize plaintext on POSIX and DPAPI ciphertext on Windows."""
    if not _dpapi_available():
        return json.dumps(store, ensure_ascii=False, indent=2) + "\n"
    inner = json.dumps(store, ensure_ascii=False, separators=(",", ":")).encode()
    document = {
        "version": AUTH_STORE_VERSION,
        "protection": "windows-dpapi-current-user",
        "payload": base64.b64encode(_windows_dpapi_protect(inner)).decode("ascii"),
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _decode_auth_document(value: Any) -> dict:
    """Decode one stored document, transparently accepting plaintext JSON."""
    if isinstance(value, dict) and value.get("protection") is not None:
        if os.name != "nt":
            raise RuntimeError("当前系统无法解密 Windows DPAPI WorkBuddy 凭据。")
        if (
            value.get("version") != AUTH_STORE_VERSION
            or value.get("protection") != "windows-dpapi-current-user"
            or not isinstance(value.get("payload"), str)
        ):
            raise ValueError("WorkBuddy DPAPI 凭据文件格式异常。")
        try:
            protected = base64.b64decode(value["payload"], validate=True)
            inner = json.loads(_windows_dpapi_unprotect(protected))
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError("WorkBuddy DPAPI 凭据内容格式异常。") from e
        return _parse_auth_store(inner)
    return _parse_auth_store(value)


def _read_auth_store_unlocked(path: Path) -> dict:
    """Read one credential document while the caller owns its file lock."""
    if not path.exists():
        return {}
    if not path.is_file():
        raise RuntimeError("WorkBuddy 登录凭据路径不是普通文件。")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise PermissionError(
            "WorkBuddy 登录凭据权限过宽，请将文件权限设置为仅当前用户可读写。"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError("WorkBuddy 登录凭据文件不是有效 JSON。") from e
    except OSError as e:
        raise RuntimeError("无法读取 WorkBuddy 登录凭据文件。") from e
    return _decode_auth_document(value)


def _write_auth_store_unlocked(path: Path, store: dict) -> None:
    """Atomically write one validated store while the caller owns its lock."""
    payload = _serialize_auth_store(store)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def load_auth_store() -> dict:
    """Load and validate the persisted WorkBuddy credential ({} when absent)."""
    path = _auth_store_path()
    with _auth_store_lock(path):
        return _read_auth_store_unlocked(path)


def save_auth_store(store: dict) -> None:
    """Validate and atomically persist the WorkBuddy credential."""
    normalized = _parse_auth_store(store)
    path = _auth_store_path()
    with _auth_store_lock(path):
        _write_auth_store_unlocked(path, normalized)


def compare_and_save_auth_store(
    expected_access_token: str,
    expected_refresh_token: str,
    store: dict,
) -> bool:
    """Commit refreshed credentials only if the source generation is unchanged."""
    normalized = _parse_auth_store(store)
    path = _auth_store_path()
    with _auth_store_lock(path):
        current = _read_auth_store_unlocked(path)
        if (
            current.get("access_token") != expected_access_token
            or current.get("refresh_token") != expected_refresh_token
        ):
            return False
        _write_auth_store_unlocked(path, normalized)
        return True


def add_managed_token_hashes(expected_access_token: str, token_hashes: set) -> bool:
    """Remember historical OAuth config copies for selective future cleanup."""
    path = _auth_store_path()
    with _auth_store_lock(path):
        current = _read_auth_store_unlocked(path)
        if current.get("access_token") != expected_access_token:
            return False
        current["managed_token_hashes"] = sorted(
            set(current.get("managed_token_hashes", [])) | token_hashes
        )
        _write_auth_store_unlocked(path, _parse_auth_store(current))
        return True


def clear_auth_store() -> None:
    """Remove the persisted WorkBuddy credential under the store lock."""
    path = _auth_store_path()
    with _auth_store_lock(path):
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as e:
            raise RuntimeError("无法删除 WorkBuddy 登录凭据文件。") from e


def tokens_to_store(
    tokens: dict,
    *,
    account: dict | None = None,
    realm: str = WORKBUDDY_REALM_CN,
    previous_refresh_token: str = "",
    managed_token_hashes: set | None = None,
    previous: dict | None = None,
) -> dict:
    """Normalize a login/refresh response into a persistable credential.

    Args:
        tokens: Parsed token payload with accessToken/refreshToken/expiresIn.
        account: Optional uid/enterpriseId/nickname payload from the account API.
        realm: Account realm used when the response carries no domain.
        previous_refresh_token: Existing refresh token kept when not rotated.
        managed_token_hashes: Historical fingerprints retained for cleanup.
        previous: Previous store, used to keep known account fields.

    Returns:
        A validated versioned credential document.

    Raises:
        ValueError: If required token fields are absent or malformed.
    """
    if not isinstance(tokens, dict):
        raise ValueError("WorkBuddy 令牌响应格式异常。")
    access_token = tokens.get("accessToken") or tokens.get("access_token")
    refresh_token = (
        tokens.get("refreshToken") or tokens.get("refresh_token") or ""
    ) or previous_refresh_token
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("WorkBuddy 令牌响应缺少访问令牌。")
    if not isinstance(refresh_token, str):
        raise ValueError("WorkBuddy 令牌响应包含无效的刷新令牌。")
    domain = str(tokens.get("domain") or (previous or {}).get("domain") or "")
    expires_in = tokens.get("expiresIn")
    expires_at = 0
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool):
        if 0 < float(expires_in) < 10 * 365 * 86400:
            expires_at = int(time.time() + float(expires_in))
    if not expires_at:
        expires_at = token_expiry(access_token) or int(
            (previous or {}).get("expires_at") or 0
        )
    account = account if isinstance(account, dict) else {}
    previous = previous or {}
    return _parse_auth_store(
        {
            "version": AUTH_STORE_VERSION,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "uid": account.get("uid") or previous.get("uid") or "",
            "enterprise_id": (
                account.get("enterpriseId")
                if account.get("enterpriseId") is not None
                else previous.get("enterprise_id") or ""
            ),
            "nickname": account.get("nickname") or previous.get("nickname") or "",
            "domain": domain,
            "realm": normalize_realm(tokens.get("realm") or realm or domain),
            "managed_token_hashes": sorted(
                set(managed_token_hashes or set())
                | {token_fingerprint(access_token)}
            ),
        }
    )


def _login_request_headers(origin: str) -> dict:
    """Build the six headers the login endpoints accept."""
    return {
        **LOGIN_HEADERS,
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": LOGIN_USER_AGENT,
    }


def _unwrap_envelope(resp: httpx.Response) -> dict:
    """Validate the ``{code,msg,data}`` envelope and return ``data``.

    Args:
        resp: The upstream HTTP response.

    Returns:
        The envelope ``data`` object (empty dict when absent).

    Raises:
        WorkBuddyLoginPending: While the browser authorization is incomplete.
        RuntimeError: If the transport failed or the payload is malformed.
    """
    if resp.status_code >= 500:
        raise RuntimeError(f"WorkBuddy 服务端错误（HTTP {resp.status_code}）。")
    if resp.status_code >= 400:
        raise WorkBuddyLoginPending(f"登录未完成（HTTP {resp.status_code}）")
    try:
        envelope = resp.json()
    except ValueError as e:
        raise RuntimeError("WorkBuddy 接口返回了无效 JSON。") from e
    if not isinstance(envelope, dict):
        raise RuntimeError("WorkBuddy 接口响应格式异常。")
    if envelope.get("code") != 0:
        message = str(envelope.get("msg") or "")[:200]
        raise WorkBuddyLoginPending(
            f"登录未完成（code={envelope.get('code')} {message}）"
        )
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


async def start_device_login(
    realm: str = WORKBUDDY_REALM_CN,
    proxy: str | None = None,
) -> dict:
    """Request a login state and the browser authorization URL.

    Args:
        realm: ``"cn"`` or ``"global"``.
        proxy: Optional HTTP/SOCKS proxy for the request.

    Returns:
        Dict with ``state``, ``auth_url``, ``realm``, ``base`` and ``origin``.

    Raises:
        RuntimeError: If the service rejects or malforms the response.
    """
    realm = normalize_realm(realm)
    base, origin = realm_endpoints(realm)
    async with create_client(proxy, 30) as client:
        resp = await client.post(
            f"{base}/v2/plugin/auth/state?platform=CLI",
            headers=_login_request_headers(origin),
            content=b"{}",
        )
    data = _unwrap_envelope(resp)
    state = data.get("state")
    auth_url = data.get("authUrl")
    if not state or not auth_url:
        raise RuntimeError("WorkBuddy 登录响应缺少 state 或 authUrl。")
    return {
        "state": str(state),
        "auth_url": str(auth_url),
        "realm": realm,
        "base": base,
        "origin": origin,
    }


async def fetch_login_account(
    session: dict,
    access_token: str,
    proxy: str | None = None,
) -> dict:
    """Fetch uid / enterprise / nickname for a completed login."""
    base = session["base"]
    headers = {
        **_login_request_headers(session["origin"]),
        "Authorization": f"Bearer {access_token}",
    }
    async with create_client(proxy, 30) as client:
        resp = await client.get(
            f"{base}/v2/plugin/login/account?state={session['state']}",
            headers=headers,
        )
    try:
        return _unwrap_envelope(resp)
    except (RuntimeError, WorkBuddyLoginPending) as e:
        logger.warning("[WorkBuddy] 读取账号信息失败: %s", e)
        return {}


async def poll_device_login(
    session: dict,
    proxy: str | None = None,
    *,
    interval: float = LOGIN_POLL_INTERVAL_SECONDS,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> dict:
    """Poll the token endpoint until the browser authorization completes.

    The upstream endpoint is a pure query: it returns a non-zero business code
    while the user has not finished signing in, and the token bundle once done.

    Args:
        session: The dict returned by :func:`start_device_login`.
        proxy: Optional HTTP/SOCKS proxy.
        interval: Seconds between polls (minimum 1s to stay polite).
        timeout: Overall deadline in seconds.

    Returns:
        Dict with ``tokens`` (raw token payload) and ``account``.

    Raises:
        TimeoutError: If authorization does not complete before the deadline.
        RuntimeError: On transport or service failures.
    """
    base = session["base"]
    headers = _login_request_headers(session["origin"])
    deadline = time.monotonic() + max(timeout, 1)
    interval = max(float(interval), 1.0)
    async with create_client(proxy, 30) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                resp = await client.get(
                    f"{base}/v2/plugin/auth/token?state={session['state']}",
                    headers=headers,
                )
                data = _unwrap_envelope(resp)
            except WorkBuddyLoginPending:
                continue
            access_token = data.get("accessToken") or data.get("access_token")
            if isinstance(access_token, str) and access_token:
                account = await fetch_login_account(session, access_token, proxy)
                return {"tokens": data, "account": account}
    raise TimeoutError("等待 WorkBuddy 授权超时，请重新发起登录。")


async def refresh_access_token(
    refresh_token: str,
    realm: str = WORKBUDDY_REALM_CN,
    proxy: str | None = None,
) -> dict:
    """Refresh an access token using a stored refresh token.

    Args:
        refresh_token: The stored refresh token.
        realm: Account realm selecting the upstream base URL.
        proxy: Optional HTTP/SOCKS proxy.

    Returns:
        The raw token payload from the refresh endpoint.

    Raises:
        ValueError: If the refresh token is empty.
        RuntimeError: If the refresh is rejected or malformed.
    """
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("WorkBuddy 刷新令牌为空。")
    base, _origin = realm_endpoints(realm)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Refresh-Token": refresh_token,
        "X-Auth-Refresh-Source": "plugin",
    }
    async with create_client(proxy, 30) as client:
        resp = await client.post(
            f"{base}/v2/plugin/auth/token/refresh", headers=headers, content=b""
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"WorkBuddy 令牌刷新失败（HTTP {resp.status_code}），"
            "请重新 /workbuddy_login。"
        )
    try:
        envelope = resp.json()
        envelope = envelope.get("data") if isinstance(envelope, dict) else None
    except ValueError as e:
        raise RuntimeError("WorkBuddy 令牌刷新响应格式异常。") from e
    if not isinstance(envelope, dict) or not (
        envelope.get("accessToken") or envelope.get("access_token")
    ):
        raise RuntimeError("WorkBuddy 令牌刷新失败，请重新 /workbuddy_login。")
    return envelope
