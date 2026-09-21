"""Manual live smoke test against the real WorkBuddy upstream.

Deliberately named without a ``test_`` prefix so ``pytest tests/`` stays fully
offline; it needs a logged-in account and consumes credits. Run it directly
after ``/workbuddy_login`` has stored credentials:

    cd <AstrBot>
    set ASTRBOT_ROOT=<AstrBot>
    .\\venv\\Scripts\\python.exe <plugin>\\tests\\smoke_live.py

Or point it at an explicit token without any stored login:

    python tests/smoke_live.py --token eyJ...

Credential resolution order: ``--token`` flag, then the plugin's own
DPAPI-encrypted store written by ``/workbuddy_login``. No external service,
gateway or CLI installation is required.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_workbuddy_provider.workbuddy_auth import (  # noqa: E402
    load_auth_store,
    realm_endpoints,
)
from astrbot_plugin_workbuddy_provider.workbuddy_source import (  # noqa: E402
    ProviderWorkBuddy,
    format_workbuddy_usage,
)


def _resolve_credentials(token: str | None) -> tuple[str, str]:
    """Return ``(token, realm)`` from the flag or the plugin credential store."""
    if token:
        return token, "cn"
    store = load_auth_store()
    if not store.get("access_token"):
        raise SystemExit(
            "未找到登录凭据。请先在 AstrBot 中执行 /workbuddy_login，"
            "或用 --token 传入访问令牌。"
        )
    return store["access_token"], store.get("realm") or "cn"


async def _run(token: str, realm: str, skip_image: bool, skip_chat: bool) -> int:
    base, _origin = realm_endpoints(realm)
    provider = ProviderWorkBuddy(
        {
            "id": "workbuddy-smoke",
            "type": "workbuddy_chat_completion",
            "provider_type": "chat_completion",
            "enable": True,
            "key": [token],
            "api_base": base,
            "timeout": 300,
            "proxy": "",
            "model": "auto",
            "custom_headers": {},
            "custom_extra_body": {},
        },
        {},
    )
    print(f"realm={realm} base={base}")

    models = await provider.get_models()
    print(f"[catalog] {len(models)} chat models: {models[:5]}")
    gen_models = await provider.get_image_models()
    edit_models = await provider.get_image_models(editing=True)
    print(f"[catalog] text-to-image={gen_models} image-to-image={edit_models}")

    try:
        usage = await provider.fetch_usage()
        print(format_workbuddy_usage(load_auth_store(), usage))
    except Exception as e:  # noqa: BLE001
        print(f"[usage] 查询失败：{type(e).__name__}: {e}")

    if not skip_image:
        start = time.time()
        raw = await provider.generate_image(
            "一只戴着草帽的橘猫坐在窗台上，水彩插画风格",
            model=gen_models[0] if gen_models else None,
        )
        out = Path("workbuddy-smoke-image.png")
        out.write_bytes(raw)
        print(
            f"[image] {len(raw)} bytes in {time.time() - start:.1f}s "
            f"magic={raw[:4]!r} -> {out.resolve()}"
        )

    if not skip_chat:
        start = time.time()
        response = await provider.text_chat(
            prompt="用一句话回答：1+1 等于几？", session_id="workbuddy-smoke"
        )
        print(f"[chat] {time.time() - start:.1f}s -> {response.completion_text!r}")
    return 0


def test_live_smoke() -> None:
    """Pytest entry point that fails loudly instead of skipping silently."""
    token, realm = _resolve_credentials(None)
    assert asyncio.run(_run(token, realm, skip_image=True, skip_chat=False)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkBuddy live smoke test")
    parser.add_argument("--token", default=None, help="access token override")
    parser.add_argument("--realm", default=None, choices=["cn", "global"])
    parser.add_argument("--skip-image", action="store_true")
    parser.add_argument("--skip-chat", action="store_true")
    args = parser.parse_args()
    token, realm = _resolve_credentials(args.token)
    realm = args.realm or realm
    print(json.dumps({"realm": realm, "token": f"{token[:12]}…"}, ensure_ascii=False))
    return asyncio.run(_run(token, realm, args.skip_image, args.skip_chat))


if __name__ == "__main__":
    sys.exit(main())
