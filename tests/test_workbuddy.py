"""Offline unit tests for the WorkBuddy plugin (no network, no credentials)."""

import asyncio
import base64
import json
import time

import pytest

from astrbot_plugin_workbuddy_provider import workbuddy_auth as auth
from astrbot_plugin_workbuddy_provider import workbuddy_source as source


def _fake_jwt(payload: dict) -> str:
    """Build an unsigned JWT-shaped token carrying ``payload``."""

    def segment(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment(payload)}.signature"


# --- realms -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("realm", "expected"),
    [
        ("cn", ("https://copilot.tencent.com", "https://www.codebuddy.cn")),
        ("global", ("https://www.workbuddy.ai", "https://www.workbuddy.ai")),
        ("", ("https://copilot.tencent.com", "https://www.codebuddy.cn")),
        ("GLOBAL", ("https://www.workbuddy.ai", "https://www.workbuddy.ai")),
    ],
)
def test_realm_endpoints(realm, expected):
    assert auth.realm_endpoints(realm) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "cn"),
        ("cn", "cn"),
        ("global", "global"),
        ("www.workbuddy.ai", "global"),
        ("codebuddy.cn", "cn"),
        (None, "cn"),
    ],
)
def test_normalize_realm(value, expected):
    assert auth.normalize_realm(value) == expected


# --- HTTP client ------------------------------------------------------------


def test_create_client_ignores_ambient_proxy(monkeypatch):
    """Ambient proxy env vars must not leak into WorkBuddy traffic."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "::1,[::1]")
    client = auth.create_client(None, 5)
    try:
        # trust_env=False means httpx never builds a proxy map from the env.
        assert client._mounts == {}
    finally:
        import asyncio

        asyncio.run(client.aclose())


# --- tokens -----------------------------------------------------------------


def test_decode_jwt_payload_and_expiry():
    token = _fake_jwt({"sub": "user-1", "exp": 4102444800})
    assert auth.decode_jwt_payload(token) == {"sub": "user-1", "exp": 4102444800}
    assert auth.token_expiry(token) == 4102444800
    assert auth.decode_jwt_payload("not-a-jwt") is None
    assert auth.token_expiry("not-a-jwt") is None


def test_tokens_to_store_uses_expires_in_and_derives_expiry():
    store = auth.tokens_to_store(
        {"accessToken": "a.b.c", "refreshToken": "r", "expiresIn": 3600},
        account={"uid": "u1", "nickname": "松子"},
        realm="cn",
        previous={"domain": "www.codebuddy.cn"},
    )
    assert store["uid"] == "u1"
    assert store["nickname"] == "松子"
    assert store["realm"] == "cn"
    assert store["domain"] == "www.codebuddy.cn"
    assert 3500 < store["expires_at"] - time.time() <= 3600
    assert store["managed_token_hashes"] == [auth.token_fingerprint("a.b.c")]


def test_tokens_to_store_rejects_missing_access_token():
    with pytest.raises(ValueError):
        auth.tokens_to_store({"refreshToken": "r"})


def test_tokens_to_store_falls_back_to_jwt_expiry():
    token = _fake_jwt({"sub": "u", "exp": 4102444800})
    store = auth.tokens_to_store({"accessToken": token, "refreshToken": "r"})
    assert store["expires_at"] == 4102444800


def test_tokens_to_store_keeps_previous_refresh_token():
    store = auth.tokens_to_store(
        {"accessToken": "a.b.c", "expiresIn": 60},
        previous_refresh_token="old-refresh",
    )
    assert store["refresh_token"] == "old-refresh"


# --- credential store -------------------------------------------------------


def test_auth_store_roundtrip_and_clear():
    store = auth.tokens_to_store(
        {"accessToken": "a.b.c", "refreshToken": "r", "expiresIn": 7200},
        account={"uid": "u9", "nickname": "松子"},
    )
    auth.save_auth_store(store)
    loaded = auth.load_auth_store()
    assert loaded["access_token"] == "a.b.c"
    assert loaded["uid"] == "u9"
    auth.clear_auth_store()
    assert auth.load_auth_store() == {}


def test_auth_store_rejects_unknown_fields():
    with pytest.raises(ValueError):
        auth._parse_auth_store({"access_token": "a", "surprise": 1})


def test_compare_and_save_requires_matching_generation():
    auth.save_auth_store(
        auth.tokens_to_store(
            {"accessToken": "old", "refreshToken": "r", "expiresIn": 60}
        )
    )
    new_store = auth.tokens_to_store(
        {"accessToken": "new", "refreshToken": "r2", "expiresIn": 60}
    )
    assert auth.compare_and_save_auth_store("stale", "r", new_store) is False
    assert auth.compare_and_save_auth_store("old", "r", new_store) is True
    assert auth.load_auth_store()["access_token"] == "new"
    auth.clear_auth_store()


# --- reasoning efforts ------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "supported", "expected"),
    [
        ("medium", ["low", "medium", "high"], "medium"),
        ("max", ["low", "high"], "high"),
        ("minimal", ["low", "high"], "low"),
        ("off", ["high"], "high"),
        ("high", [], "high"),
        ("weird", ["low"], "weird"),
    ],
)
def test_downgrade_effort(requested, supported, expected):
    assert source._downgrade_effort(requested, supported) == expected


def test_settings_update_and_image_model_normalization():
    source.update_workbuddy_settings(
        {
            "reasoning_effort": "high",
            "image_model": " hunyuan-image-alpha ",
            "image_size": "1024x1536",
            "image_n": 2,
            "thinking_enabled": False,
        }
    )
    settings = source.get_workbuddy_settings()
    assert settings["reasoning_effort"] == "high"
    assert settings["image_model"] == "hunyuan-image-alpha"
    assert settings["image_size"] == "1024x1536"
    assert settings["image_n"] == 2
    assert settings["thinking_enabled"] is False
    # Invalid values must not clobber the current settings.
    source.update_workbuddy_settings(
        {"reasoning_effort": "bogus", "image_size": "1x1", "image_n": 99}
    )
    settings = source.get_workbuddy_settings()
    assert settings["reasoning_effort"] == "high"
    assert settings["image_size"] == "1024x1536"
    assert settings["image_n"] == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [("auto", "auto"), ("", "auto"), ("hunyuan-image-alpha", "hunyuan-image-alpha")],
)
def test_normalize_image_model_valid(value, expected):
    assert source.normalize_image_model(value) == expected


@pytest.mark.parametrize("value", ["a b", "x" * 200, "bad/../path"])
def test_normalize_image_model_invalid(value):
    with pytest.raises(ValueError):
        source.normalize_image_model(value)


# --- reasoning introspection ------------------------------------------------


def _provider_with_catalog(entries: list[dict], model: str) -> "source.ProviderWorkBuddy":
    """Build an offline provider whose model catalog is pre-seeded."""
    provider = source.ProviderWorkBuddy(
        {
            "id": "test",
            "type": "workbuddy_chat_completion",
            "provider_type": "chat_completion",
            "enable": True,
            "key": [],
            "api_base": "https://copilot.tencent.com",
            "timeout": 30,
            "proxy": "",
            "model": model,
            "custom_headers": {},
            "custom_extra_body": {},
        },
        {},
    )
    provider._model_cache = entries
    provider._model_cache_at = time.monotonic()
    return provider


def test_get_reasoning_info_downgrades_to_supported_level():
    """A configured level the model rejects must be reported as downgraded."""
    provider = _provider_with_catalog(
        [
            {
                "id": "glm-5.3",
                "reasoning": {
                    "supportedEfforts": ["low", "high", "max"],
                    "defaultEffort": "high",
                },
            }
        ],
        "glm-5.3",
    )
    source.update_workbuddy_settings({"reasoning_effort": "medium"})
    info = asyncio.run(provider.get_reasoning_info())
    assert info["model"] == "glm-5.3"
    assert info["supported"] == ["low", "high", "max"]
    assert info["default"] == "high"
    assert info["effective"] == "low"


def test_get_reasoning_info_keeps_supported_level():
    provider = _provider_with_catalog(
        [{"id": "glm-5.3", "reasoning": {"supportedEfforts": ["low", "high", "max"]}}],
        "glm-5.3",
    )
    source.update_workbuddy_settings({"reasoning_effort": "max"})
    info = asyncio.run(provider.get_reasoning_info())
    assert info["effective"] == "max"


def test_get_reasoning_info_omits_field_for_plain_model():
    """Models without a reasoning block must not receive the parameter."""
    provider = _provider_with_catalog([{"id": "glm-4.6", "reasoning": None}], "glm-4.6")
    source.update_workbuddy_settings({"reasoning_effort": "high"})
    info = asyncio.run(provider.get_reasoning_info())
    assert info["supported"] == []
    assert info["effective"] == ""


def test_get_reasoning_info_passes_through_for_deepseek():
    """DeepSeek models accept an effort even without an advertised list."""
    provider = _provider_with_catalog(
        [{"id": "deepseek-v4.1-flash", "reasoning": {"effort": "high"}}],
        "deepseek-v4.1-flash",
    )
    source.update_workbuddy_settings({"reasoning_effort": "medium"})
    info = asyncio.run(provider.get_reasoning_info())
    assert info["supported"] == []
    assert info["effective"] == "medium"


def test_get_reasoning_info_reports_empty_effective_for_unknown_model():
    provider = _provider_with_catalog([], "mystery-model")
    source.update_workbuddy_settings({"reasoning_effort": "high"})
    info = asyncio.run(provider.get_reasoning_info())
    assert info["model"] == "mystery-model"
    assert info["effective"] == ""


# --- catalog filtering ------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "max_out", "tags", "expected"),
    [
        ("hunyuan-image-alpha", None, ["text-to-image"], True),
        ("hunyuan-image-alpha-edit", None, ["image-to-image"], True),
        ("codewise-completions", 256, [], True),
        ("completion-gf", 8192, [], True),
        ("nes-embed", 8192, [], True),
        ("hunyuan-3b", 256, [], True),
        ("deepseek-v4.1-flash", 128000, [], False),
        ("glm-5.3", 64000, ["craft"], False),
    ],
)
def test_is_non_chat_model(model_id, max_out, tags, expected):
    assert source._is_non_chat_model(model_id, max_out, tags) is expected


# --- credit parsing ---------------------------------------------------------


def test_extract_credit_packages_and_formatting():
    usage = {
        "code": 0,
        "data": {
            "Response": {
                "Data": {
                    "Accounts": [
                        {
                            "PackageName": "礼包",
                            "CapacitySizePrecise": "3000",
                            "CycleCapacityRemainPrecise": "1200",
                            "ExpiredTime": "2027-04-22 18:26:55",
                        },
                        {
                            "PackageName": "空包",
                            "CapacitySizePrecise": "0",
                            "CycleCapacityRemainPrecise": "0",
                        },
                    ]
                }
            }
        },
    }
    packages = source._extract_credit_packages(usage)
    assert len(packages) == 1
    assert packages[0]["remain"] == 1200.0
    text = source.format_workbuddy_usage(
        {"nickname": "松子", "uid": "u1", "realm": "cn"}, usage
    )
    assert "剩余额度: 1200 / 3000 credits" in text
    assert "松子" in text


def test_extract_credit_packages_handles_missing_data():
    assert source._extract_credit_packages(None) == []
    assert source._extract_credit_packages({"data": {}}) == []


# --- diagnostics ------------------------------------------------------------


def test_safe_detail_redacts_credentials():
    token = _fake_jwt({"sub": "u", "exp": 4102444800})
    text = source._safe_detail(f"failed Bearer {token} accessToken={token}")
    assert token not in text
    assert "REDACTED" in text


def test_image_error_detail_reads_envelope():
    import httpx

    resp = httpx.Response(
        400,
        json={"code": 11103, "msg": "backend not supported"},
        request=httpx.Request("POST", "https://example.test/"),
    )
    detail = source._image_error_detail(resp)
    assert "backend not supported" in detail
    assert "11103" in detail
