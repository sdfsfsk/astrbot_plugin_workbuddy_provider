"""AstrBot plugin: WorkBuddy (Tencent CodeBuddy) subscription provider.

Importing this package registers the ``workbuddy_chat_completion`` provider
adapter, after which a WorkBuddy provider can be added from the WebUI provider
page like any built-in provider type. The plugin owns the whole OAuth login
flow, so no external gateway or CLI installation is required.
"""

import asyncio
import re
import secrets
import time
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image, Reply
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.media_utils import resolve_media_ref_to_base64_data

from .workbuddy_auth import (
    WORKBUDDY_REALM_CN,
    WORKBUDDY_REALM_GLOBAL,
    WORKBUDDY_REALMS,
    clear_auth_store,
    load_auth_store,
    normalize_realm,
    poll_device_login,
    save_auth_store,
    start_device_login,
    token_fingerprint,
    tokens_to_store,
)
from .workbuddy_source import (
    WORKBUDDY_EFFORT_RANK,
    WORKBUDDY_IMAGE_SIZES,
    ProviderWorkBuddy,
    _register_workbuddy_provider,
    _unregister_workbuddy_provider,
    format_workbuddy_usage,
    get_workbuddy_settings,
    normalize_image_model,
    update_workbuddy_settings,
)


@register(
    "astrbot_plugin_workbuddy_provider",
    "Matsuko",
    "WorkBuddy（腾讯 CodeBuddy）模型服务提供商：内置浏览器授权登录、令牌自动填充与续期、订阅额度查询、图片生成与改图",
    "1.0.0",
    "https://github.com/sdfsfsk/astrbot_plugin_workbuddy_provider",
)
class WorkBuddyProviderPlugin(Star):
    """Registers the WorkBuddy provider adapter and helper commands."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._login_in_progress = False
        self._login_task: asyncio.Task | None = None
        update_workbuddy_settings(config)

    async def initialize(self):
        """Rebuild providers after a hot reload and auto-fill stored tokens.

        AstrBot re-executes plugin modules on hot reload but keeps running
        provider instances on the previously registered adapter class, so
        plugin code changes would not reach live providers until restart.
        """
        _register_workbuddy_provider()
        provider_manager = self.context.provider_manager
        pending_reload_ids = set(
            getattr(provider_manager, "_workbuddy_plugin_reload_ids", set())
        )
        if hasattr(provider_manager, "_workbuddy_plugin_reload_ids"):
            delattr(provider_manager, "_workbuddy_plugin_reload_ids")
        try:
            store = load_auth_store()
        except (RuntimeError, ValueError, OSError, PermissionError) as e:
            logger.warning("[WorkBuddy] 无法读取登录凭据用于 Key 自动填充: %s", e)
            store = {}
        if store:
            await self._reload_oauth_sources(
                set(store.get("managed_token_hashes", [])),
                oauth_access_token=store.get("access_token"),
                reload_models=False,
            )
        if provider_manager.provider_insts:
            stale_instances = [
                inst
                for inst in provider_manager.provider_insts
                if inst.provider_config.get("type") == "workbuddy_chat_completion"
                and not isinstance(inst, ProviderWorkBuddy)
            ]
            if stale_instances:
                model_entries = {
                    p.get("id"): p
                    for p in provider_manager.acm.default_conf.get("provider", [])
                    if isinstance(p, dict)
                }
                for inst in stale_instances:
                    entry = model_entries.get(inst.provider_config.get("id"))
                    if entry is None:
                        continue
                    logger.info(
                        "[WorkBuddy] 插件热重载后重建提供商实例: %s",
                        inst.provider_config.get("id"),
                    )
                    await provider_manager.reload(entry)
        if pending_reload_ids:
            for entry in provider_manager.acm.default_conf.get("provider", []):
                if (
                    isinstance(entry, dict)
                    and entry.get("id") in pending_reload_ids
                    and entry.get("enable", False)
                ):
                    logger.info(
                        "[WorkBuddy] 插件重新启用后恢复提供商实例: %s",
                        entry.get("id"),
                    )
                    await provider_manager.reload(entry)
        self._harden_tool_schemas()

    def _harden_tool_schemas(self) -> None:
        """Apply required/default constraints missing from AstrBot's decorator."""
        manager = self.context.get_llm_tool_manager()
        tool = manager.get_func("workbuddy_generate_image")
        if tool is None or not isinstance(tool.parameters, dict):
            return
        tool.parameters["required"] = ["prompt"]
        tool.parameters["additionalProperties"] = False
        properties = tool.parameters.get("properties")
        if isinstance(properties, dict):
            prop = properties.get("use_reference_images")
            if isinstance(prop, dict):
                prop["default"] = True

    async def terminate(self) -> None:
        """Cancel login polling and release the provider registration."""
        login_task = self._login_task
        if login_task and not login_task.done():
            login_task.cancel()
            if login_task is not asyncio.current_task():
                await asyncio.gather(login_task, return_exceptions=True)
        provider_manager = self.context.provider_manager
        provider_ids = [
            inst.provider_config.get("id")
            for inst in list(provider_manager.provider_insts)
            if isinstance(inst, ProviderWorkBuddy)
            and inst.provider_config.get("id")
        ]
        if provider_ids:
            pending = set(
                getattr(provider_manager, "_workbuddy_plugin_reload_ids", set())
            )
            pending.update(provider_ids)
            provider_manager._workbuddy_plugin_reload_ids = pending
        for provider_id in provider_ids:
            await provider_manager.terminate_provider(provider_id)
        _unregister_workbuddy_provider()

    def _get_workbuddy_provider(self) -> ProviderWorkBuddy | None:
        """Find the first instantiated WorkBuddy provider, if any."""
        for inst in self.context.provider_manager.provider_insts:
            if isinstance(inst, ProviderWorkBuddy):
                return inst
        return None

    async def _reload_oauth_sources(
        self,
        oauth_token_hashes: set[str],
        *,
        oauth_access_token: str | None = None,
        reload_models: bool = True,
    ) -> bool:
        """Fill the stored access token into WorkBuddy provider sources.

        Only copies previously written by this plugin are replaced; manually
        pasted keys are never touched.

        Args:
            oauth_token_hashes: Non-secret fingerprints of access-token copies
                previously managed by the plugin and safe to remove.
            oauth_access_token: Newly logged-in access token copied into
                empty/OAuth-managed sources for WebUI compatibility.
            reload_models: Reload affected model entries immediately. Startup
                migration disables this because core loads providers afterward.

        Returns:
            True when at least one WorkBuddy provider source exists.
        """
        provider_manager = self.context.provider_manager
        conf = provider_manager.acm.default_conf
        sources = [
            source
            for source in conf.get("provider_sources", [])
            if isinstance(source, dict)
            and source.get("type") == "workbuddy_chat_completion"
        ]
        if not sources:
            return False
        changed = False
        reload_source_ids: set[str] = set()
        for source in sources:
            raw_keys = source.get("key", [])
            if isinstance(raw_keys, str):
                keys = [raw_keys] if raw_keys else []
            elif isinstance(raw_keys, list):
                keys = [key for key in raw_keys if isinstance(key, str) and key]
            else:
                keys = []
            filtered = [
                key for key in keys if token_fingerprint(key) not in oauth_token_hashes
            ]
            configured = (
                [oauth_access_token]
                if oauth_access_token and not filtered
                else filtered
            )
            source_id = source.get("id")
            if configured != keys:
                source["key"] = configured
                changed = True
                if isinstance(source_id, str):
                    reload_source_ids.add(source_id)
            if not filtered and isinstance(source_id, str):
                reload_source_ids.add(source_id)
        if changed:
            conf.save_config()
        if reload_models:
            for entry in conf.get("provider", []):
                if (
                    isinstance(entry, dict)
                    and entry.get("provider_source_id") in reload_source_ids
                ):
                    await provider_manager.reload(entry)
        return True

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("workbuddy_login")
    async def workbuddy_login(self, event: AstrMessageEvent, realm: str = ""):
        """Run the administrator-only WorkBuddy browser authorization login."""
        if not event.is_private_chat():
            yield event.plain_result(
                "为防止授权链接被他人抢先绑定，请管理员私聊机器人发送 /workbuddy_login。"
            )
            return
        if self._login_in_progress:
            yield event.plain_result(
                "已有 WorkBuddy 登录流程进行中，请先完成授权或等待超时（15 分钟）。"
            )
            return
        selected = str(realm or "").strip().lower()
        if selected and selected not in WORKBUDDY_REALMS:
            yield event.plain_result(
                "用法：/workbuddy_login [cn|global]\n"
                "cn = 国内版（codebuddy.cn，默认），global = 国际版（workbuddy.ai）。"
            )
            return
        selected = selected or normalize_realm(self.config.get("realm"))

        self._login_in_progress = True
        self._login_task = asyncio.current_task()
        try:
            provider = self._get_workbuddy_provider()
            proxy = (
                provider.provider_config.get("proxy") or None
                if provider is not None
                else None
            )
            try:
                session = await start_device_login(selected, proxy)
            except (RuntimeError, httpx.HTTPError) as e:
                yield event.plain_result(f"❌ 登录失败：{e}")
                return
            yield event.plain_result(
                "🐾 WorkBuddy 授权登录\n"
                f"区域：{selected}\n"
                f"请在浏览器打开：{session['auth_url']}\n\n"
                "登录成功后松子会自动保存凭据并填入提供商，15 分钟内有效～"
            )
            try:
                result = await poll_device_login(session, proxy)
            except (TimeoutError, RuntimeError, httpx.HTTPError) as e:
                yield event.plain_result(f"❌ 登录失败：{e}")
                return

            try:
                previous_store = load_auth_store()
            except (RuntimeError, ValueError, OSError, PermissionError):
                previous_store = {}
            previous_access = previous_store.get("access_token", "")
            managed_hashes = set(previous_store.get("managed_token_hashes", []))
            if previous_access:
                managed_hashes.add(token_fingerprint(previous_access))
            try:
                store = tokens_to_store(
                    result["tokens"],
                    account=result.get("account"),
                    realm=selected,
                    previous=previous_store,
                    previous_refresh_token=previous_store.get("refresh_token", ""),
                    managed_token_hashes=managed_hashes,
                )
                save_auth_store(store)
            except (RuntimeError, ValueError, OSError) as e:
                yield event.plain_result(f"❌ 登录凭据保存失败：{e}")
                return

            expire_at = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(store.get("expires_at") or 0)
            )
            nickname = store.get("nickname") or "未知"
            activated = await self._reload_oauth_sources(
                set(store["managed_token_hashes"]),
                oauth_access_token=store["access_token"],
            )
            if activated:
                yield event.plain_result(
                    f"✅ WorkBuddy 登录成功！账号：{nickname}（{store.get('realm')}）\n"
                    f"令牌有效期至 {expire_at or '未知'}\n"
                    "访问令牌已自动填充到空 Key/旧 OAuth 来源；刷新令牌保存在受保护"
                    "凭据库，到期会自动续期～"
                )
            else:
                yield event.plain_result(
                    f"✅ WorkBuddy 登录成功！账号：{nickname}（{store.get('realm')}）\n"
                    f"令牌有效期至 {expire_at or '未知'}\n"
                    "⚠️ 但未找到 WorkBuddy 提供商。请在 WebUI 新增「WorkBuddy 订阅」"
                    "提供商（Key 可留空，将自动使用已保存的登录令牌）。"
                )
        finally:
            self._login_in_progress = False
            self._login_task = None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("workbuddy_logout")
    async def workbuddy_logout(self, event: AstrMessageEvent):
        """Remove stored credentials without deleting manually pasted keys."""
        login_task = self._login_task
        if login_task and not login_task.done():
            login_task.cancel()
            if login_task is not asyncio.current_task():
                await asyncio.gather(login_task, return_exceptions=True)
        try:
            stored = load_auth_store()
            managed_hashes = set(stored.get("managed_token_hashes", []))
            clear_auth_store()
            updated = await self._reload_oauth_sources(managed_hashes)
        except (RuntimeError, ValueError, OSError, PermissionError) as e:
            yield event.plain_result(f"❌ WorkBuddy 退出登录失败：{e}")
            return
        yield event.plain_result(
            "✅ WorkBuddy 登录凭据及其旧配置副本已删除；其他手工 Key 保持不变。"
            if updated
            else "✅ WorkBuddy 登录凭据已删除；当前没有 WorkBuddy 提供商。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("workbuddy_usage")
    async def workbuddy_usage(self, event: AstrMessageEvent):
        """查询 WorkBuddy 账号状态与订阅额度"""
        try:
            store = load_auth_store()
        except (RuntimeError, ValueError, OSError, PermissionError) as e:
            yield event.plain_result(f"❌ 读取登录凭据失败：{e}")
            return
        provider = self._get_workbuddy_provider()
        usage = None
        failure = ""
        if provider is None:
            failure = "未找到已启用的 WorkBuddy 提供商，仅显示登录状态。"
        elif not store:
            failure = "尚未登录，请先私聊发送 /workbuddy_login。"
        else:
            try:
                usage = await provider.fetch_usage()
            except (
                ValueError,
                PermissionError,
                RuntimeError,
                httpx.HTTPError,
            ) as e:
                failure = f"额度查询失败：{e}"
        lines = [format_workbuddy_usage(store, usage)]
        if failure:
            lines.append(f"⚠️ {failure}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("workbuddy_reasoning")
    async def workbuddy_reasoning(self, event: AstrMessageEvent, level: str = ""):
        """查看或设置 WorkBuddy 推理深度，并显示当前模型支持的档位"""
        level = (level or "").strip().lower()
        levels = " / ".join(WORKBUDDY_EFFORT_RANK)
        if level and level not in WORKBUDDY_EFFORT_RANK:
            yield event.plain_result(f"无效的推理深度：{level}\n可选值：{levels}")
            return
        lines: list[str] = []
        if level:
            previous = self.config.get("reasoning_effort")
            self.config["reasoning_effort"] = level
            try:
                self.config.save_config()
            except OSError:
                if previous is None:
                    self.config.pop("reasoning_effort", None)
                else:
                    self.config["reasoning_effort"] = previous
                yield event.plain_result("❌ 配置保存失败，请检查配置文件权限。")
                return
            update_workbuddy_settings(self.config)
            lines.append(f"✅ 推理深度已设为：{level}")

        configured = get_workbuddy_settings()["reasoning_effort"]
        lines.append(f"当前 WorkBuddy 推理深度：{configured}")

        provider = self._get_workbuddy_provider()
        if provider is None:
            lines.append("未找到已启用的 WorkBuddy 提供商，无法读取当前模型的档位。")
        else:
            try:
                info = await provider.get_reasoning_info()
            except (httpx.HTTPError, OSError) as e:
                info = None
                lines.append(f"⚠️ 读取模型档位失败：{e}")
            if info is not None:
                model = info["model"] or "未设置"
                lines.append(f"当前模型：{model}")
                if info["supported"]:
                    lines.append("该模型支持：" + " / ".join(info["supported"]))
                    if info["default"]:
                        lines.append(f"模型默认档位：{info['default']}")
                    if info["effective"] != configured:
                        lines.append(
                            f"实际发送：{info['effective']}"
                            f"（{configured} 不被支持，已自动降级）"
                        )
                    else:
                        lines.append(f"实际发送：{info['effective']}")
                elif model.lower().startswith("deepseek"):
                    lines.append("该模型未声明支持档位，将按 DeepSeek 方式透传设置值。")
                else:
                    lines.append("该模型未声明支持档位，插件不会向其发送该参数。")

        lines.append(f"全部可选值：{levels}")
        lines.append("用法：/workbuddy_reasoning <级别>")
        lines.append("（也可在插件配置页修改）")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("workbuddy_image_model")
    async def workbuddy_image_model(self, event: AstrMessageEvent, model: str = ""):
        """查看或切换图片生成/改图模型（auto / list / refresh / <模型ID>）"""
        arg = (model or "").strip()
        listing = arg.lower() in ("", "list", "refresh")
        edited = False
        if not listing:
            try:
                selection = normalize_image_model(arg)
            except ValueError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            provider = self._get_workbuddy_provider()
            if provider is None:
                yield event.plain_result(
                    "未找到已启用的 WorkBuddy 提供商，请先配置或发送 /workbuddy_login。"
                )
                return
            known = await provider.get_image_models()
            known += await provider.get_image_models(editing=True)
            if selection != "auto" and selection not in known:
                yield event.plain_result(
                    f"❌ 未知的图片模型：{selection}\n可用模型：\n"
                    + ("\n".join(known) if known else "（在线目录暂不可用）")
                )
                return
            previous = self.config.get("image_model")
            self.config["image_model"] = selection
            try:
                self.config.save_config()
            except OSError:
                if previous is None:
                    self.config.pop("image_model", None)
                else:
                    self.config["image_model"] = previous
                yield event.plain_result("❌ 配置保存失败，请检查配置文件权限。")
                return
            update_workbuddy_settings(self.config)
            edited = True

        settings = get_workbuddy_settings()
        lines = [
            ("✅ 已保存图片模型设置：" if edited else "当前图片模型设置：")
            + settings["image_model"]
        ]
        if listing:
            provider = self._get_workbuddy_provider()
            if provider is None:
                lines.append("未找到已启用的 WorkBuddy 提供商，无法读取在线图片模型。")
            else:
                generate_models = await provider.get_image_models()
                edit_models = await provider.get_image_models(editing=True)
                if arg.lower() == "refresh":
                    lines.insert(
                        0,
                        "✅ 已重新读取在线图片模型目录。"
                        if (generate_models or edit_models)
                        else "⚠️ 在线图片模型目录获取失败，稍后会自动重试。",
                    )
                lines.append(
                    "文生图模型：\n"
                    + ("\n".join(generate_models) if generate_models else "（未获取到）")
                )
                lines.append(
                    "改图模型：\n"
                    + ("\n".join(edit_models) if edit_models else "（未获取到）")
                )
                lines.append(
                    "自动模式下会选取目录中的第一个模型；当前文生图="
                    f"{await provider.resolve_image_model(settings['image_model'])}，"
                    f"改图={await provider.resolve_image_model(settings['image_edit_model'], editing=True)}"
                )
        lines.append("用法：/workbuddy_image_model auto / <模型ID> / list / refresh")
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _save_generated_image(data: bytes) -> Path:
        """Save generated image bytes under AstrBot's managed temp directory.

        The upstream returns JSON/PNG/WebP bytes depending on the image model,
        so the file extension is sniffed from the magic number: platforms and
        image hosts otherwise treat a mislabelled file as broken.

        Args:
            data: The raw image bytes returned by the provider.

        Returns:
            The written file path.
        """
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif data.startswith(b"GIF8"):
            suffix = ".gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            suffix = ".png"
        images_dir = Path(get_astrbot_temp_path()) / "astrbot_plugin_workbuddy_provider"
        images_dir.mkdir(parents=True, exist_ok=True)
        path = (
            images_dir
            / f"workbuddy-{int(time.time())}-{secrets.token_hex(4)}{suffix}"
        )
        path.write_bytes(data)
        return path

    @staticmethod
    async def _collect_message_images(event: AstrMessageEvent) -> list[str]:
        """Collect valid, unique images from the current or quoted message.

        Args:
            event: Message event whose current and quoted chains may contain images.

        Returns:
            Up to three image data URLs in message order.

        Raises:
            ValueError: If the message contains image references but none can be read.
        """
        refs: list[str] = []
        seen_refs: set[str] = set()

        def add_image(comp: Image) -> None:
            ref = comp.url or comp.file or comp.path or ""
            if ref and ref not in seen_refs:
                seen_refs.add(ref)
                refs.append(ref)

        def collect_quoted(components) -> None:
            for comp in components or []:
                if isinstance(comp, Image):
                    add_image(comp)
                elif isinstance(comp, Reply) and comp.chain:
                    collect_quoted(comp.chain)

        components = event.get_messages()
        # The current message is the primary canvas; quoted images are fallback
        # references even when the platform places the Reply segment first.
        for comp in components:
            if isinstance(comp, Image):
                add_image(comp)
        for comp in components:
            if isinstance(comp, Reply) and comp.chain:
                collect_quoted(comp.chain)
        data_urls: list[str] = []
        for ref in refs:
            if len(data_urls) >= 3:
                break
            try:
                resolved = await resolve_media_ref_to_base64_data(
                    ref, media_type="image", strict=True
                )
            except (httpx.HTTPError, ValueError, OSError) as e:
                logger.warning("[WorkBuddy] 读取参考图片失败: %s", e)
                continue
            if resolved:
                data_url = resolved.to_data_url()
                if data_url not in data_urls:
                    data_urls.append(data_url)
        if refs and not data_urls:
            raise ValueError(
                "检测到参考图片，但图片读取失败，已取消改图以避免误生成新图。"
            )
        return data_urls

    @filter.command("workbuddy_image")
    async def workbuddy_image(self, event: AstrMessageEvent, prompt: str = ""):
        """用插件配置的 WorkBuddy 订阅模型生成图片；附加或引用图片时为改图模式"""
        prompt = (prompt or "").strip()
        if not prompt:
            yield event.plain_result(
                "用法：/workbuddy_image <画面描述>\n"
                "消息中附加图片（或引用带图消息）即为改图模式，最多 3 张参考图。"
            )
            return
        provider = self._get_workbuddy_provider()
        if provider is None:
            yield event.plain_result(
                "未找到已启用的 WorkBuddy 提供商，请先在 WebUI 配置或发送 /workbuddy_login。"
            )
            return
        try:
            references = await self._collect_message_images(event)
            model = await provider.resolve_image_model(
                get_workbuddy_settings()[
                    "image_edit_model" if references else "image_model"
                ],
                editing=bool(references),
            )
        except (ValueError, httpx.HTTPError) as e:
            yield event.plain_result(f"❌ {e}")
            return
        action = "编辑" if references else "生成"
        yield event.plain_result(
            f"🎨 图片{action}中（{model}），一般需要 10 秒到 1 分钟，请稍等喵～"
        )
        try:
            data = await provider.generate_image(prompt, references, model=model)
        except (ValueError, PermissionError, RuntimeError, httpx.HTTPError) as e:
            yield event.plain_result(f"❌ {e}")
            return
        path = self._save_generated_image(data)
        yield event.image_result(str(path))

    @filter.llm_tool(name="workbuddy_generate_image")
    async def workbuddy_generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        use_reference_images: bool = True,
    ) -> str:
        """使用 WorkBuddy 订阅模型生成或编辑图片并直接发送给用户。当前消息或引用消息带图时，默认将图片作为编辑输入；只有用户明确要求忽略附图并从零生成时，才把 use_reference_images 设为 false。用户要求继续编辑旧图但本轮没有当前或引用图片时，先请用户引用或重发图片，不要把描述静默当成全新生成。

        Args:
            prompt(string): 完整的生成或编辑指令；编辑时必须明确只改什么、其余内容保持不变
            use_reference_images(bool): 是否使用当前消息和引用消息中的图片作为编辑输入，默认 true
        """
        provider = self._get_workbuddy_provider()
        if provider is None:
            return (
                "错误：未配置 WorkBuddy 提供商，无法生成图片。"
                "请提示主人先配置或发送 /workbuddy_login。"
            )
        try:
            references = (
                await self._collect_message_images(event)
                if use_reference_images
                else []
            )
        except (ValueError, httpx.HTTPError) as e:
            return f"图片编辑失败：{e}"
        action = "编辑" if references else "生成"
        try:
            model = await provider.resolve_image_model(
                get_workbuddy_settings()[
                    "image_edit_model" if references else "image_model"
                ],
                editing=bool(references),
            )
            await event.send(
                event.plain_result(
                    f"🎨 图片{action}中（{model}），一般需要 10 秒到 1 分钟，请稍等喵～"
                )
            )
            data = await provider.generate_image(prompt, references, model=model)
        except (ValueError, PermissionError, RuntimeError, httpx.HTTPError) as e:
            return f"图片{action}失败：{e}"
        path = self._save_generated_image(data)
        await event.send(event.image_result(str(path)))
        return (
            f"图片已{action}并直接发送给用户，无需在回复中描述图片内容或声称无法发送。"
        )

    @filter.llm_tool(name="workbuddy_image_models")
    async def workbuddy_image_models(self, event: AstrMessageEvent) -> str:
        """查询当前 WorkBuddy 账号可用的图片生成与改图模型清单。当用户询问能画什么图、有哪些绘图模型，或需要挑选图片模型时调用本工具。"""
        provider = self._get_workbuddy_provider()
        if provider is None:
            return "错误：未配置 WorkBuddy 提供商，无法查询图片模型。"
        try:
            generate_models = await provider.get_image_models()
            edit_models = await provider.get_image_models(editing=True)
        except httpx.HTTPError as e:
            return f"查询图片模型失败：{e}"
        settings = get_workbuddy_settings()
        lines = [
            f"当前文生图设置：{settings['image_model']}",
            "可用文生图模型：" + ("、".join(generate_models) or "（未获取到）"),
            f"当前改图设置：{settings['image_edit_model']}",
            "可用改图模型：" + ("、".join(edit_models) or "（未获取到）"),
            f"图片尺寸：{'、'.join(WORKBUDDY_IMAGE_SIZES)}，当前 {settings['image_size']}",
        ]
        return "\n".join(lines)
