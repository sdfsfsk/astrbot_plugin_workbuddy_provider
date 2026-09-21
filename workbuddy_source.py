"""WorkBuddy / Tencent CodeBuddy provider adapter for AstrBot.

Talks to the CodeBuddy upstream (``copilot.tencent.com`` for the CN realm,
``www.workbuddy.ai`` for the global realm) with the OAuth access token obtained
by the plugin's own login flow, instead of an API key.

Request handling reuses AstrBot's built-in OpenAI Chat Completions provider;
this class only adds the CodeBuddy-specific headers, the mandatory SSE
streaming behavior, reasoning-effort translation, model discovery and the
standalone image generation/editing endpoints.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from astrbot import logger
from astrbot.core.exceptions import EmptyModelOutputError
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.register import (
    provider_cls_map,
    provider_registry,
    register_provider_adapter,
)
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.utils.network_utils import create_proxy_client

from .workbuddy_auth import (
    WORKBUDDY_REALM_CN,
    WORKBUDDY_REALM_GLOBAL,
    add_managed_token_hashes,
    compare_and_save_auth_store,
    create_client,
    decode_jwt_payload,
    load_auth_store,
    normalize_realm,
    realm_endpoints,
    refresh_access_token,
    token_expiry,
    token_fingerprint,
    tokens_to_store,
)

# Client fingerprint advertised to the upstream. The platform segment differs
# per realm: sending "WorkBuddy" for a global account can trip risk control.
WORKBUDDY_CLIENT_VERSION = "5.5.4"
WORKBUDDY_CLI_VERSION = "2.137.1"
WORKBUDDY_DEFAULT_MODEL = "auto"
WORKBUDDY_DEFAULT_PROXY = ""

# Runtime chat settings owned by the plugin config, so they apply to every
# provider instance and can be changed from the settings page or chat commands.
WORKBUDDY_REASONING_EFFORTS = [
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]
WORKBUDDY_EFFORT_RANK = {name: index for index, name in enumerate(WORKBUDDY_REASONING_EFFORTS)}
WORKBUDDY_DEFAULT_EFFORT = "medium"
WORKBUDDY_IMAGE_SIZES = ["1024x1024", "1024x1536", "1536x1024"]
WORKBUDDY_TEXT_TO_IMAGE_TAG = "text-to-image"
WORKBUDDY_IMAGE_TO_IMAGE_TAG = "image-to-image"
# Mirrors the official CLI fallbacks, used only when the catalog has no match.
WORKBUDDY_FALLBACK_IMAGE_MODEL = "hunyuan-image-v3.0"
WORKBUDDY_FALLBACK_IMAGE_EDIT_MODEL = "hunyuan-image-v2.0-general-edit"

WORKBUDDY_IMAGE_MAX_REFERENCES = 3
WORKBUDDY_IMAGE_MAX_INPUT_BYTES = 10 * 1024 * 1024
WORKBUDDY_IMAGE_MAX_OUTPUT_BYTES = 25 * 1024 * 1024
WORKBUDDY_MAX_IMAGE_PROMPT_CHARS = 32_000

# Model prefixes and limits the upstream treats as non-chat entries.
WORKBUDDY_NON_CHAT_PREFIXES = ("nes-", "completion-", "codewise-")
WORKBUDDY_NON_CHAT_MAX_OUTPUT_TOKENS = 256

# Static fallback catalog; the live list is fetched from ``/v3/config``.
WORKBUDDY_MODEL_CATALOG = [
    "auto",
    "hy3",
    "hy4-preview",
    "hy4-preview-f",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "kimi-k3-2",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "minimax-m3-pay",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-v4.1-flash",
]

_TOKEN_REFRESH_LOCK = asyncio.Lock()
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_TERMINAL_QUOTA_PATTERN = re.compile(
    r"insufficient|out of credit|quota|balance|exceeded|6004|14017|14018",
    re.IGNORECASE,
)

_PLUGIN_SETTINGS: dict = {
    "reasoning_effort": WORKBUDDY_DEFAULT_EFFORT,
    "thinking_enabled": True,
    "image_model": "auto",
    "image_edit_model": "auto",
    "image_size": "1024x1024",
    "image_n": 1,
}


class WorkBuddyUsageLimitError(RuntimeError):
    """Raised when retrying cannot recover an exhausted credit balance."""


def update_workbuddy_settings(settings: dict) -> None:
    """Update runtime WorkBuddy request settings from the plugin config.

    Args:
        settings: Plugin config possibly carrying ``reasoning_effort``,
            ``thinking_enabled``, ``image_model``, ``image_edit_model``,
            ``image_size`` and ``image_n``. Missing or invalid keys keep their
            current values.
    """
    effort = settings.get("reasoning_effort")
    if effort in WORKBUDDY_EFFORT_RANK:
        _PLUGIN_SETTINGS["reasoning_effort"] = effort
    if "thinking_enabled" in settings:
        _PLUGIN_SETTINGS["thinking_enabled"] = bool(settings["thinking_enabled"])
    for key in ("image_model", "image_edit_model"):
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            _PLUGIN_SETTINGS[key] = value.strip()
    size = settings.get("image_size")
    if size in WORKBUDDY_IMAGE_SIZES:
        _PLUGIN_SETTINGS["image_size"] = size
    count = settings.get("image_n")
    if isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 4:
        _PLUGIN_SETTINGS["image_n"] = count


def get_workbuddy_settings() -> dict:
    """Return a copy of the current runtime WorkBuddy request settings."""
    return dict(_PLUGIN_SETTINGS)


def normalize_image_model(value: str, *, editing: bool = False) -> str:
    """Validate a manual image model ID or the ``auto`` selection setting.

    Args:
        value: The configured value; ``auto`` triggers catalog discovery.
        editing: Whether the value addresses the image-to-image model.

    Returns:
        The trimmed model ID or ``"auto"``.

    Raises:
        ValueError: If the value is malformed.
    """
    if not isinstance(value, str):
        raise ValueError("图片模型必须填写 auto 或模型 ID。")
    model = value.strip() or "auto"
    if model != "auto" and (
        len(model) > 100 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", model)
    ):
        raise ValueError("图片模型必须填写 auto 或有效的模型 ID。")
    return model


def _safe_detail(value: object, limit: int = 400) -> str:
    """Bound provider diagnostics and remove credential-shaped material."""
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            value = error.get("message") or error.get("code") or ""
        else:
            value = error or value.get("message") or value.get("msg") or ""
    text = " ".join(str(value).split())
    text = _JWT_PATTERN.sub("[REDACTED]", text)
    text = re.sub(r"(?i)Bearer\s+[^\s,}\"]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)([\"']?(?:accessToken|refreshToken|access_token|refresh_token)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s,}\"']+",
        r"\1[REDACTED]",
        text,
    )
    return text[:limit]


def format_workbuddy_usage(store: dict, usage: dict | None = None) -> str:
    """Render account, token state and credit balance as plain text.

    Args:
        store: The stored credential document.
        usage: Optional parsed billing payload from the balance endpoint.

    Returns:
        A human-readable multi-line summary.
    """
    lines = ["🐾 WorkBuddy 账号状态"]
    nickname = store.get("nickname") or ""
    uid = store.get("uid") or ""
    if nickname or uid:
        lines.append(f"账号: {nickname or '未知'}" + (f"（{uid}）" if uid else ""))
    lines.append(f"区域: {store.get('realm') or WORKBUDDY_REALM_CN}")
    if store.get("enterprise_id"):
        lines.append(f"企业 ID: {store['enterprise_id']}")

    token = store.get("access_token") or ""
    if token:
        exp = token_expiry(token) or int(store.get("expires_at") or 0)
        if exp:
            expire_at = (
                datetime.fromtimestamp(exp, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
            state = "已过期" if exp < time.time() else "有效"
            lines.append(f"访问令牌: {state}（到期 {expire_at}）")
        lines.append(
            "刷新令牌: " + ("已保存，到期自动续期" if store.get("refresh_token") else "缺失")
        )

    packages = _extract_credit_packages(usage)
    if packages:
        remaining = sum(item["remain"] for item in packages)
        total = sum(item["size"] for item in packages)
        lines.append(f"剩余额度: {remaining:g} / {total:g} credits")
        for item in packages[:4]:
            name = item["name"] or "额度包"
            expired = f"，到期 {item['expired']}" if item["expired"] else ""
            lines.append(f"  · {name}: 剩余 {item['remain']:g} / {item['size']:g}{expired}")
    elif usage is None:
        lines.append("剩余额度: 查询失败（可稍后重试）")
    else:
        lines.append("剩余额度: 未找到有效的额度包")
    return "\n".join(lines)


def _extract_credit_packages(usage: dict | None) -> list[dict]:
    """Flatten Tencent billing resource entries into credit packages.

    Args:
        usage: Raw billing payload, possibly None.

    Returns:
        A list of ``{name, remain, size, expired}`` dicts, skipping exhausted
        or zero-sized entries.
    """
    if not isinstance(usage, dict):
        return []
    data = usage.get("data") or usage
    if isinstance(data, dict):
        data = data.get("Response") or data
    if isinstance(data, dict):
        data = data.get("Data") or data
    accounts = data.get("Accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        return []
    packages: list[dict] = []
    for entry in accounts:
        if not isinstance(entry, dict):
            continue
        size = _as_float(entry.get("CapacitySizePrecise") or entry.get("CapacitySize"))
        remain = _as_float(
            entry.get("CycleCapacityRemainPrecise")
            if entry.get("CycleCapacityRemainPrecise") is not None
            else entry.get("CapacityRemain")
        )
        if size is None or remain is None or size <= 0:
            continue
        packages.append(
            {
                "name": str(entry.get("PackageName") or entry.get("SubProductName") or ""),
                "remain": remain,
                "size": size,
                "expired": str(entry.get("ExpiredTime") or ""),
            }
        )
    return packages


def _as_float(value: object) -> float | None:
    """Parse a numeric field that the billing API may return as a string."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _downgrade_effort(requested: str, supported: list[str]) -> str:
    """Pick the closest supported reasoning effort for one model.

    Args:
        requested: The configured effort level.
        supported: Effort levels the model advertises.

    Returns:
        The requested level when supported, otherwise the highest supported
        level at or below it, or the lowest supported level when every
        supported level is higher than requested.
    """
    if not supported:
        return requested
    if requested in supported:
        return requested
    rank = WORKBUDDY_EFFORT_RANK.get(requested)
    if rank is None:
        return requested
    ranked = sorted(
        (WORKBUDDY_EFFORT_RANK.get(name, 3), name)
        for name in supported
        if name in WORKBUDDY_EFFORT_RANK
    )
    if not ranked:
        return requested
    below = [item for item in ranked if item[0] <= rank]
    return below[-1][1] if below else ranked[0][1]


def _is_non_chat_model(model_id: str, max_output_tokens: object, tags: list) -> bool:
    """Report whether a catalog entry is not a chat model."""
    lowered = str(model_id or "").lower()
    if lowered.startswith(WORKBUDDY_NON_CHAT_PREFIXES):
        return True
    if (
        isinstance(max_output_tokens, int | float)
        and not isinstance(max_output_tokens, bool)
        and 0 < max_output_tokens <= WORKBUDDY_NON_CHAT_MAX_OUTPUT_TOKENS
    ):
        return True
    return any(str(tag) in (WORKBUDDY_TEXT_TO_IMAGE_TAG, WORKBUDDY_IMAGE_TO_IMAGE_TAG) for tag in tags or [])


WORKBUDDY_PROVIDER_DESC = (
    "WorkBuddy（腾讯 CodeBuddy）订阅提供商适配器。推荐将 Key 留空，"
    "由管理员私聊发送 /workbuddy_login 完成浏览器授权登录并自动填入令牌、"
    "到期自动续期；也可手动粘贴已有的 accessToken。"
    "支持对话、推理深度调节、订阅额度查询与图片生成/改图。"
)

WORKBUDDY_CONFIG_TMPL = {
    "id": "workbuddy",
    "provider": "workbuddy",
    "type": "workbuddy_chat_completion",
    "provider_type": "chat_completion",
    "enable": True,
    "key": [],
    "api_base": "https://copilot.tencent.com",
    "timeout": 180,
    "proxy": WORKBUDDY_DEFAULT_PROXY,
    "model": WORKBUDDY_DEFAULT_MODEL,
    "custom_headers": {},
    "custom_extra_body": {},
}


class ProviderWorkBuddy(ProviderOpenAIOfficial):
    """CodeBuddy upstream provider driven by a stored OAuth access token."""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        """Initialize the WorkBuddy provider with CodeBuddy-specific defaults.

        Args:
            provider_config: Provider source and model configuration.
            provider_settings: Global provider settings.
        """
        merged_config = dict(provider_config)
        self.realm = normalize_realm(
            merged_config.get("realm") or merged_config.get("api_base")
        )
        default_base, self.origin = realm_endpoints(self.realm)
        merged_config["api_base"] = str(
            merged_config.get("api_base") or default_base
        ).rstrip("/")
        merged_config.setdefault("model", WORKBUDDY_DEFAULT_MODEL)
        self.api_base = merged_config["api_base"]
        # The SDK appends "/chat/completions", so keep the "/v2" prefix here.
        merged_config["api_base"] = (
            self.api_base
            if self.api_base.endswith("/v2")
            else f"{self.api_base}/v2"
        )
        self._model_cache: list[dict] = []
        self._model_cache_at = 0.0
        store = self._load_store()
        self.client_default_headers = self._build_session_headers(store, "")
        super().__init__(merged_config, provider_settings)
        # AstrBot owns transport retries; disable the SDK's hidden retry layer.
        self.client.max_retries = 0

        stored_token = store.get("access_token") if store else None
        configured_token = self.api_keys[0] if len(self.api_keys) == 1 else None
        if stored_token and not any(self.api_keys):
            logger.info(
                "[WorkBuddy] 提供商未配置 Key，改用 /workbuddy_login 保存的登录令牌。"
            )
            self._set_runtime_token(stored_token)
        elif stored_token and configured_token:
            stored_exp = token_expiry(stored_token) or store.get("expires_at") or 0
            configured_exp = token_expiry(configured_token) or 0
            if stored_exp > configured_exp:
                logger.info(
                    "[WorkBuddy] 使用登录凭据库中更新的访问令牌，忽略配置里的旧副本。"
                )
                add_managed_token_hashes(
                    stored_token, {token_fingerprint(configured_token)}
                )
                self._set_runtime_token(stored_token, previous_token=configured_token)
        self.client_default_headers = self._build_session_headers(
            self._load_store(), self._active_token()
        )
        self._apply_static_headers()
        self._warn_token_state()

    @staticmethod
    def _load_store() -> dict:
        """Load the credential store, returning {} when unreadable."""
        try:
            return load_auth_store()
        except (RuntimeError, ValueError, OSError, PermissionError) as e:
            logger.warning("[WorkBuddy] 无法读取登录凭据: %s", e)
            return {}

    def _create_http_client(self, provider_config: dict) -> httpx.AsyncClient:
        """Create the SDK transport without inheriting ambient proxy settings.

        Both upstream realms are service-region locked, so a machine-wide
        ``HTTP_PROXY``/``NO_PROXY`` would silently reroute or break requests
        (malformed NO_PROXY entries such as ``::1`` even raise inside httpx).
        WorkBuddy traffic therefore only uses the provider's own ``proxy``
        setting.

        Args:
            provider_config: Provider configuration carrying the proxy field.

        Returns:
            The httpx client handed to the OpenAI SDK.
        """
        proxy = str(provider_config.get("proxy") or "").strip()
        httpx_module: Any = httpx
        try:
            from openai import _base_client as openai_base_client

            httpx_module = getattr(openai_base_client, "httpx", httpx)
        except ImportError:
            pass
        if proxy:
            return create_proxy_client(
                "WorkBuddy", proxy, httpx_module=httpx_module
            )
        return httpx_module.AsyncClient(trust_env=False)

    def _build_session_headers(self, store: dict, token: str) -> dict:
        """Build per-account device and conversation headers.

        Args:
            store: The credential document, used for uid/domain when present.
            token: The active access token; its ``sub`` claim is the uid, so a
                manually pasted token still produces a complete header set.

        Returns:
            A header mapping merged into every upstream request.
        """
        payload = decode_jwt_payload(token) if token else None
        uid = str(store.get("uid") or (payload or {}).get("sub") or "")
        is_global = self.realm == WORKBUDDY_REALM_GLOBAL
        platform = "WorkBuddy AI" if is_global else "WorkBuddy"
        message_id = secrets.token_hex(16)
        conversation_request_id = secrets.token_hex(16)
        headers = {
            "User-Agent": (
                f"WorkBuddy/{WORKBUDDY_CLIENT_VERSION} {platform}/"
                f"{WORKBUDDY_CLIENT_VERSION} CLI/{WORKBUDDY_CLI_VERSION}"
            ),
            "Origin": self.origin,
            "Referer": self.origin + "/",
            "X-Requested-With": "XMLHttpRequest",
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "en-US" if is_global else "zh-CN",
            "X-Agent-Purpose": "conversation",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": WORKBUDDY_CLIENT_VERSION,
            "X-Product": "WorkBuddy",
            "X-Conversation-Request-ID": conversation_request_id,
            "X-Conversation-Message-ID": message_id,
            "X-Request-ID": message_id,
            "X-Root-Request-ID": conversation_request_id,
            "X-Trace-ID": conversation_request_id,
            "X-B3-TraceId": conversation_request_id,
            "X-B3-SpanId": message_id[:16],
            "X-B3-Sampled": "1",
        }
        if uid:
            headers["X-Machine-ID"] = hashlib.sha256(
                f"wb2a:machine:{uid}".encode()
            ).hexdigest()[:36]
            headers["X-Session-ID"] = hashlib.sha256(
                f"wb2a:session:{uid}".encode()
            ).hexdigest()[:36]
            headers["X-User-Id"] = uid
            headers["X-No-Enterprise-Id"] = "1"
            headers["X-Domain"] = (
                "www.workbuddy.ai"
                if is_global
                else str(store.get("domain") or "www.codebuddy.cn")
            )
        return headers

    def _apply_static_headers(self) -> None:
        """Push the current device/session headers onto the SDK client.

        ``AsyncOpenAI.default_headers`` is read-only, so the client is replaced
        by a derived copy. ``with_options`` shares the underlying httpx client,
        transport and API key, so this neither reconnects nor drops the token.
        """
        headers = dict(self.client_default_headers)
        headers.update(
            {
                str(key): str(value)
                for key, value in (
                    self.provider_config.get("custom_headers") or {}
                ).items()
            }
        )
        self.client = self.client.with_options(default_headers=headers)

    def _active_token(self) -> str:
        """Return the token selected for the current request."""
        return (
            self.client.api_key
            or self.chosen_api_key
            or (self.api_keys[0] if self.api_keys else "")
        )

    def _set_runtime_token(self, token: str, previous_token: str | None = None) -> None:
        """Adopt one refreshed token without discarding unrelated account keys."""
        updated: list[str] = []
        replaced = False
        for key in self.api_keys:
            if not key:
                continue
            if key == previous_token or not replaced:
                if not replaced:
                    updated.append(token)
                    replaced = True
                continue
            updated.append(key)
        if not replaced:
            updated.insert(0, token)
        self.api_keys = updated
        self.chosen_api_key = token
        self.client.api_key = token
        self.client_default_headers = self._build_session_headers(
            self._load_store(), token
        )
        self._apply_static_headers()

    def _warn_token_state(self) -> None:
        """Log a warning for malformed or expired tokens at startup."""
        for key in self.api_keys:
            if not key:
                continue
            exp = token_expiry(key)
            if exp is None:
                logger.warning(
                    "[WorkBuddy] 配置的 Key 不是有效的 JWT 访问令牌，请使用 "
                    "/workbuddy_login 登录或粘贴 accessToken 本体。"
                )
                continue
            expire_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp))
            if exp < time.time():
                logger.warning(
                    "[WorkBuddy] 访问令牌已于 %s 过期，请重新 /workbuddy_login。",
                    expire_at,
                )
            else:
                logger.info("[WorkBuddy] 访问令牌有效期至 %s", expire_at)

    async def _maybe_refresh_token(self) -> None:
        """Refresh the stored credential when it is close to expiry."""
        active_token = self._active_token()
        store = self._load_store()
        active_exp = token_expiry(active_token)
        if store.get("access_token") == active_token and store.get("expires_at"):
            active_exp = min(active_exp or store["expires_at"], store["expires_at"])
        if active_exp is None or active_exp - time.time() > 600:
            return

        async with _TOKEN_REFRESH_LOCK:
            active_token = self._active_token()
            store = self._load_store()
            stored_token = store.get("access_token", "")
            if stored_token != active_token:
                stored_exp = token_expiry(stored_token) or store.get("expires_at") or 0
                if stored_exp - time.time() > 600:
                    self._set_runtime_token(stored_token, previous_token=active_token)
                else:
                    logger.warning(
                        "[WorkBuddy] 登录凭据已被其他流程替换，请重新 /workbuddy_login。"
                    )
                return

            active_exp = token_expiry(active_token) or store.get("expires_at")
            if active_exp is None or active_exp - time.time() > 600:
                return
            refresh_token = store.get("refresh_token")
            if not refresh_token:
                logger.warning(
                    "[WorkBuddy] 访问令牌即将过期且没有刷新令牌，请发送 /workbuddy_login。"
                )
                return
            try:
                tokens = await refresh_access_token(
                    refresh_token,
                    store.get("realm") or self.realm,
                    self.provider_config.get("proxy") or None,
                )
                new_store = tokens_to_store(
                    tokens,
                    previous=store,
                    previous_refresh_token=refresh_token,
                    managed_token_hashes=set(store.get("managed_token_hashes", []))
                    | {token_fingerprint(active_token)},
                )
                committed = compare_and_save_auth_store(
                    active_token, refresh_token, new_store
                )
            except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
                logger.warning("[WorkBuddy] 访问令牌自动刷新失败: %s", _safe_detail(e))
                return

            latest = self._load_store() if not committed else new_store
            new_token = latest.get("access_token") or active_token
            if new_token != active_token:
                self._set_runtime_token(new_token, previous_token=active_token)
            new_exp = token_expiry(new_token) or latest.get("expires_at")
            logger.info(
                "[WorkBuddy] 访问令牌已自动刷新，新令牌有效期至 %s",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(new_exp))
                if new_exp
                else "未知",
            )

    def _account_headers(self) -> dict:
        """Return the headers required by every upstream account request."""
        return dict(self.client_default_headers)

    async def _request_model_catalog(
        self, *, force_refresh: bool = False
    ) -> list[dict]:
        """Fetch and cache the upstream model catalog from ``/v3/config``.

        Args:
            force_refresh: Bypass the 10-minute cache.

        Returns:
            The raw ``data.models`` entries; an empty list on failure.
        """
        if not force_refresh and self._model_cache and time.monotonic() - self._model_cache_at < 600:
            return self._model_cache
        token = self._active_token()
        if not token:
            return []
        try:
            async with create_client(
                self.provider_config.get("proxy"), 20
            ) as client:
                resp = await client.get(
                    f"{self.api_base}/v3/config",
                    headers={
                        **self._account_headers(),
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                )
            if resp.status_code != 200:
                logger.warning(
                    "[WorkBuddy] 模型目录请求返回 HTTP %s，使用内置目录。",
                    resp.status_code,
                )
                return self._model_cache
            payload = resp.json()
            models = (payload.get("data") or {}).get("models")
            if not isinstance(models, list):
                logger.warning("[WorkBuddy] 模型目录响应缺少 models 数组。")
                return self._model_cache
        except (httpx.HTTPError, ValueError, OSError) as e:
            logger.warning("[WorkBuddy] 模型目录获取失败：%s", _safe_detail(e))
            return self._model_cache
        self._model_cache = [m for m in models if isinstance(m, dict)]
        self._model_cache_at = time.monotonic()
        return self._model_cache

    def _catalog_chat_models(self) -> list[str]:
        """Return chat-capable model IDs from the cached catalog."""
        models: list[str] = []
        for entry in self._model_cache:
            if entry.get("disabled"):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            if _is_non_chat_model(
                model_id, entry.get("maxOutputTokens"), entry.get("tags") or []
            ):
                continue
            if model_id not in models:
                models.append(model_id)
        return models

    def _catalog_image_model(self, editing: bool) -> str:
        """Return the first catalog model carrying the requested image tag."""
        tag = (
            WORKBUDDY_IMAGE_TO_IMAGE_TAG if editing else WORKBUDDY_TEXT_TO_IMAGE_TAG
        )
        for entry in self._model_cache:
            tags = entry.get("tags") or []
            if tag in tags and isinstance(entry.get("id"), str) and entry["id"]:
                return str(entry["id"])
        return (
            WORKBUDDY_FALLBACK_IMAGE_EDIT_MODEL
            if editing
            else WORKBUDDY_FALLBACK_IMAGE_MODEL
        )

    async def resolve_image_model(self, setting: str, *, editing: bool = False) -> str:
        """Resolve ``auto`` to a catalog model, keeping manual IDs untouched.

        Args:
            setting: The configured image model (``auto`` or an explicit ID).
            editing: Whether the image-to-image catalog should be consulted.

        Returns:
            The model ID to send upstream.

        Raises:
            ValueError: If the configured value is malformed.
        """
        model = normalize_image_model(setting, editing=editing)
        if model != "auto":
            return model
        await self._request_model_catalog()
        return self._catalog_image_model(editing)

    async def get_models(self) -> list[str]:
        """Return the upstream model catalog for the WebUI model picker.

        Returns:
            Chat-capable model IDs, falling back to the built-in catalog when
            discovery fails.
        """
        await self._request_model_catalog(force_refresh=not self._model_cache)
        models = self._catalog_chat_models()
        return models or list(WORKBUDDY_MODEL_CATALOG)

    async def get_image_models(self, *, editing: bool = False) -> list[str]:
        """Return the catalog model IDs usable for image generation or editing."""
        await self._request_model_catalog()
        tag = (
            WORKBUDDY_IMAGE_TO_IMAGE_TAG if editing else WORKBUDDY_TEXT_TO_IMAGE_TAG
        )
        return [
            str(entry["id"])
            for entry in self._model_cache
            if tag in (entry.get("tags") or []) and entry.get("id")
        ]

    def _apply_provider_specific_request_overrides(
        self, payloads: dict, extra_body: dict
    ) -> None:
        """Inject CodeBuddy reasoning controls and a cache key.

        AstrBot calls this hook for both streaming and non-streaming requests
        right before the SDK call, which makes it the single place where the
        upstream-specific request fields can be added.

        Args:
            payloads: Named parameters forwarded to the OpenAI SDK.
            extra_body: Raw JSON fields merged into the request body.
        """
        super()._apply_provider_specific_request_overrides(payloads, extra_body)
        settings = get_workbuddy_settings()
        model = str(payloads.get("model") or "").lower()

        # Reasoning effort is only sent when the catalog advertises levels for
        # this model, or for the DeepSeek family which always accepts one;
        # other models reject the field with HTTP 400.
        effort = extra_body.pop("reasoning_effort", None)
        extra_body.pop("reasoningEffort", None)
        is_deepseek = model.startswith("deepseek")
        supported = self._supported_efforts(model)
        if supported:
            extra_body["reasoning_effort"] = _downgrade_effort(
                str(effort or settings["reasoning_effort"]), supported
            )
        elif is_deepseek:
            extra_body["reasoning_effort"] = str(
                effort or settings["reasoning_effort"]
            )

        # DeepSeek-family models answer without reasoning unless thinking is
        # explicitly enabled, and the toggle must be paired with an effort.
        if is_deepseek and settings["thinking_enabled"]:
            extra_body.setdefault("thinking", {"type": "enabled"})

        # A stable cache key cuts upstream cost substantially.
        cache_key = extra_body.pop("prompt_cache_key", None)
        if not cache_key:
            uid = (self._load_store().get("uid") or "anonymous")[:8]
            cache_key = (
                f"astrbot-{uid}-"
                f"{hashlib.sha256(str(payloads.get('model')).encode()).hexdigest()[:16]}"
            )
        extra_body["prompt_cache_key"] = cache_key

    def _supported_efforts(self, model: str) -> list[str]:
        """Return the reasoning efforts the catalog advertises for a model."""
        for entry in self._model_cache:
            if str(entry.get("id") or "").lower() == model:
                reasoning = entry.get("reasoning") or {}
                efforts = reasoning.get("supportedEfforts")
                if isinstance(efforts, list):
                    return [str(name) for name in efforts]
                break
        return []

    async def text_chat(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ) -> LLMResponse:
        """Aggregate a streaming turn into one response.

        The CodeBuddy upstream only accepts ``stream: true``, so the
        non-streaming entry point delegates to the streaming one.

        Raises:
            EmptyModelOutputError: If the stream yields no complete response.
        """
        final_response = None
        async for response in self.text_chat_stream(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            extra_user_content_parts=extra_user_content_parts,
            **kwargs,
        ):
            if not response.is_chunk:
                final_response = response
        if final_response is None:
            raise EmptyModelOutputError("WorkBuddy 未返回完整响应。")
        return final_response

    async def text_chat_stream(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ):
        """Stream one turn, refreshing the access token first when needed."""
        await self._maybe_refresh_token()
        if not self._model_cache:
            await self._request_model_catalog()
        self._apply_static_headers()
        async for response in super().text_chat_stream(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            extra_user_content_parts=extra_user_content_parts,
            **kwargs,
        ):
            yield response

    async def fetch_usage(self) -> dict:
        """Query the CodeBuddy credit balance for the logged-in account.

        Returns:
            The parsed billing payload.

        Raises:
            ValueError: If no access token is configured.
            PermissionError: If the token is rejected.
        """
        await self._maybe_refresh_token()
        token = self._active_token()
        if not token:
            raise ValueError("未配置 WorkBuddy 访问令牌，请先 /workbuddy_login。")
        store = self._load_store()
        billing_base = (
            "https://www.workbuddy.ai"
            if (store.get("realm") or self.realm) == WORKBUDDY_REALM_GLOBAL
            else "https://www.codebuddy.cn"
        )
        headers = {
            **self._account_headers(),
            # The billing endpoints expect the single-segment desktop UA.
            "User-Agent": f"WorkBuddy/{WORKBUDDY_CLIENT_VERSION}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        async with create_client(self.provider_config.get("proxy"), 30) as client:
            resp = await client.post(
                f"{billing_base}/v2/billing/meter/get-user-resource",
                headers=headers,
                json={},
            )
        if resp.status_code in (401, 403):
            raise PermissionError(
                f"访问令牌无效或已过期（HTTP {resp.status_code}），请重新 /workbuddy_login。"
            )
        if resp.status_code == 404:
            raise RuntimeError("当前账号不支持额度查询接口（HTTP 404）。")
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError("额度接口返回了无效 JSON。") from e
        if not isinstance(payload, dict):
            raise RuntimeError("额度接口响应格式异常。")
        return payload

    async def generate_image(
        self,
        prompt: str,
        reference_images: list[str] | None = None,
        *,
        model: str | None = None,
    ) -> bytes:
        """Generate or edit an image with the configured subscription model.

        Mirrors the official client: ``/v2/images/generations`` for pure
        generation and ``/v2/images/edits`` when reference images are supplied.

        Args:
            prompt: The generation/edit instruction.
            reference_images: Optional reference images as data URLs.
            model: Optional already-resolved model ID.

        Returns:
            The generated image bytes.

        Raises:
            ValueError: If no token is configured or the prompt is invalid.
            PermissionError: If the token is rejected.
            RuntimeError: For backend or payload failures.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("图片提示词不能为空。")
        if len(prompt) > WORKBUDDY_MAX_IMAGE_PROMPT_CHARS:
            raise ValueError(
                f"图片提示词不能超过 {WORKBUDDY_MAX_IMAGE_PROMPT_CHARS} 个字符。"
            )
        await self._maybe_refresh_token()
        token = self._active_token()
        if not token:
            raise ValueError("未配置 WorkBuddy 访问令牌，请先 /workbuddy_login。")

        refs = [ref for ref in (reference_images or []) if ref][
            :WORKBUDDY_IMAGE_MAX_REFERENCES
        ]
        total_input_bytes = 0
        for ref in refs:
            if not ref.startswith("data:image/") or "," not in ref:
                raise ValueError("参考图片必须是有效的 image data URL。")
            estimated = len(ref.split(",", 1)[1]) * 3 // 4
            if estimated > WORKBUDDY_IMAGE_MAX_INPUT_BYTES:
                raise ValueError("单张参考图片不能超过 10 MiB。")
            total_input_bytes += estimated
        if total_input_bytes > WORKBUDDY_IMAGE_MAX_INPUT_BYTES:
            raise ValueError("参考图片总大小不能超过 10 MiB。")

        settings = get_workbuddy_settings()
        if model is None:
            model = await self.resolve_image_model(
                settings["image_edit_model"] if refs else settings["image_model"],
                editing=bool(refs),
            )
        body: dict = {
            "model": model,
            "prompt": prompt,
            "size": settings["image_size"],
            "n": settings["image_n"],
        }
        if refs:
            body["image"] = refs
        endpoint = (
            f"{self.api_base}/v2/images/edits"
            if refs
            else f"{self.api_base}/v2/images/generations"
        )
        logger.info(
            "[WorkBuddy] 图片请求 model=%s mode=%s references=%d size=%s",
            model,
            "edit" if refs else "generate",
            len(refs),
            settings["image_size"],
        )
        async with create_client(self.provider_config.get("proxy"), 300) as client:
            resp = await client.post(
                endpoint,
                json=body,
                headers={
                    **self._account_headers(),
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
        if resp.status_code in (401, 403):
            raise PermissionError(
                f"访问令牌无效或已过期（HTTP {resp.status_code}），请重新 /workbuddy_login。"
            )
        if resp.status_code != 200:
            detail = _image_error_detail(resp)
            logger.warning(
                "[WorkBuddy] 图片请求失败 HTTP %d: %s", resp.status_code, detail
            )
            raise RuntimeError(f"上游图片接口返回 HTTP {resp.status_code}：{detail}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError("图片生成接口返回了无效 JSON。") from e
        if not isinstance(payload, dict):
            raise RuntimeError("图片生成接口响应格式异常。")
        if payload.get("code") != 0:
            raise RuntimeError(
                "上游图片接口错误："
                f"code={payload.get('code')} {_safe_detail(payload.get('msg'))}"
            )
        data = payload.get("data") or {}
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise RuntimeError("图片生成响应中没有图像数据。")
        credit = (data.get("usage") or {}).get("credit") if isinstance(data, dict) else None
        if isinstance(credit, int | float) and not isinstance(credit, bool):
            logger.info("[WorkBuddy] 本次图片消耗 %.2f credits", float(credit))
        return await self._download_image(items[0])

    async def _download_image(self, item: dict) -> bytes:
        """Return image bytes from one response item (base64 or URL).

        Args:
            item: One ``data.data`` entry carrying ``b64_json`` or ``url``.

        Returns:
            The decoded image bytes.

        Raises:
            RuntimeError: If the item carries no usable image data.
        """
        if not isinstance(item, dict):
            raise RuntimeError("图片生成响应条目格式异常。")
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded.strip():
            if len(encoded) * 3 // 4 > WORKBUDDY_IMAGE_MAX_OUTPUT_BYTES:
                raise RuntimeError("图片生成响应超过 25 MiB 安全上限。")
            try:
                raw = base64.b64decode(encoded.strip(), validate=True)
            except (ValueError, binascii.Error) as e:
                raise RuntimeError("图片生成响应包含无效的 Base64 数据。") from e
            return self._check_image_size(raw)
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise RuntimeError("图片生成响应既没有 base64 也没有有效 URL。")
        try:
            async with httpx.AsyncClient(
                proxy=str(self.provider_config.get("proxy") or "").strip() or None,
                timeout=120,
                trust_env=False,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"下载生成图片失败：{_safe_detail(e)}") from e
        return self._check_image_size(resp.content)

    @staticmethod
    def _check_image_size(raw: bytes) -> bytes:
        """Validate that downloaded image bytes are non-empty and bounded."""
        if not raw:
            raise RuntimeError("图片生成响应内容为空。")
        if len(raw) > WORKBUDDY_IMAGE_MAX_OUTPUT_BYTES:
            raise RuntimeError("图片生成响应超过 25 MiB 安全上限。")
        return raw


def _image_error_detail(resp: httpx.Response) -> str:
    """Extract a bounded, credential-free error detail from an image response."""
    if len(resp.content) > 64 * 1024:
        return "响应过大，未解析原因"
    try:
        payload = resp.json()
    except ValueError:
        return _safe_detail(resp.text)
    if not isinstance(payload, dict):
        return _safe_detail(payload)
    message = payload.get("msg") or payload.get("message")
    code = payload.get("code")
    detail = _safe_detail(message) if message is not None else ""
    if code is not None and str(code) not in detail:
        detail = f"{detail}（code={_safe_detail(code)}）" if detail else f"code={_safe_detail(code)}"
    return detail or "未提供可解析原因"


def _register_workbuddy_provider() -> None:
    """Register the provider adapter, replacing any stale registration.

    AstrBot re-executes plugin modules on hot reload while provider
    registrations live in process-global registries, so a plain decorator
    registration would raise a duplicate-type error on the second load.
    """
    current = provider_cls_map.get("workbuddy_chat_completion")
    if current is not None and current.cls_type is ProviderWorkBuddy:
        return
    stale = provider_cls_map.pop("workbuddy_chat_completion", None)
    if stale is not None and stale in provider_registry:
        provider_registry.remove(stale)
    register_provider_adapter(
        "workbuddy_chat_completion",
        WORKBUDDY_PROVIDER_DESC,
        default_config_tmpl=dict(WORKBUDDY_CONFIG_TMPL),
        provider_display_name="WorkBuddy 订阅",
    )(ProviderWorkBuddy)


def _unregister_workbuddy_provider() -> None:
    """Remove this module's provider registration without touching replacements."""
    current = provider_cls_map.get("workbuddy_chat_completion")
    if current is None or current.cls_type is not ProviderWorkBuddy:
        return
    provider_cls_map.pop("workbuddy_chat_completion", None)
    if current in provider_registry:
        provider_registry.remove(current)


_register_workbuddy_provider()
