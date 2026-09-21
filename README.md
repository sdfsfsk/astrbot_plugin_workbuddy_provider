# astrbot_plugin_workbuddy_provider

[AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：将 **WorkBuddy（腾讯 CodeBuddy）订阅** 作为模型服务提供商接入 AstrBot，用订阅额度驱动对话与绘图，无需 API Key。

推荐使用插件内置的 **`/workbuddy_login` 登录**：管理员在私聊中发起浏览器授权，插件自动保存凭据、**自动填入提供商 Key** 并到期续期，**不依赖 workbuddy2api 网关、CodeBuddy CLI 或任何外部目录**。同时也保留手动粘贴 accessToken 的接入方式。

## 功能

- **内置浏览器授权登录**：管理员私聊执行 `/workbuddy_login`，插件向官方 OAuth 端点申请 `state` + 授权链接，浏览器登录完成后自动轮询取回令牌；凭据用受保护目录 + Windows DPAPI（当前用户）落盘，跨进程加锁、原子写入
- **Key 自动填充**：登录成功后把访问令牌自动写入「Key 为空的 WorkBuddy 提供商来源」，无需手工复制；`/workbuddy_logout` 只清理插件写入的副本，手工 Key 保持不变
- **令牌自动续期**：访问令牌临近到期（10 分钟内）时用刷新令牌自动换新并写回凭据库；刷新失败只记日志，不影响其它请求
- **国内版 / 国际版双区域**：`cn`（copilot.tencent.com + codebuddy.cn）与 `global`（workbuddy.ai）自动切换上游地址、Origin/Referer、User-Agent 平台段与 `Accept-Language`
- **模型目录在线拉取**：`get_models()` 读取上游 `/v3/config`，自动过滤补全类与绘图类条目；WebUI「获取模型列表」即时可用
- **推理深度可调**：插件配置下拉框或 `/workbuddy_reasoning` 指令（`off` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`），并按模型实际支持的档位自动降级，不支持该参数的模型不会发送
- **DeepSeek 思维链**：自动为 deepseek 系列注入 `thinking.type=enabled` + 推理档位，思维链正常回传
- **订阅额度查询**：`/workbuddy_usage` 显示账号、令牌有效期与各额度包剩余 credits
- **图片生成与改图**：`/workbuddy_image` 指令 + LLM 工具 `workbuddy_generate_image`，走官方图片端点（`/v2/images/generations` 与 `/v2/images/edits`）；当前消息或引用消息带图时自动改图，最多 3 张参考图
- **完整能力**：流式输出、工具调用（Function Calling）、多模态图片输入、多 Key 轮转、`prompt_cache_key` 费用优化

## 图片生成说明

WorkBuddy 的绘图能力**不在对话端点上**：用 `hunyuan-image-*` 模型调用 `/v2/chat/completions` 会被上游以 `code=11103 Backend [hunyuan-stream] is not supported` 拒绝。插件按官方客户端的实现改走独立图片端点：

| 能力 | 端点 | 关键请求字段 |
|------|------|--------------|
| 文生图 | `POST {base}/v2/images/generations` | `model` / `prompt` / `size` / `n` |
| 改图 | `POST {base}/v2/images/edits` | 上述字段 + `image`（data URL 数组）/ `input_fidelity` |

可选模型从模型目录中按标签自动发现：`text-to-image` → `hunyuan-image-alpha`，`image-to-image` → `hunyuan-image-alpha-edit`。响应形如 `{"code":0,"data":{"data":[{"url": "..."}],"usage":{"credit": 5.71}}}`，插件既支持 `url` 也会下载，也支持上游返回 `b64_json`。生成一张 1024x1024 约 9~15 秒、约 5.7 credits。

## 风险提示

使用个人订阅额度驱动聊天机器人**可能违反腾讯 CodeBuddy / WorkBuddy 的服务条款**，存在账号被风控或封禁的风险。**请自行评估风险，建议使用小号。**

## 安装

1. 将本插件目录放入 AstrBot 插件目录 `data/plugins/`，或在 WebUI 插件页通过仓库链接安装；
2. 重启 AstrBot（或热重载插件）；
3. 打开 WebUI → **服务提供商** → **新增提供商** → 选择 **WorkBuddy 订阅**，Key 栏留空并保存；
4. 用管理员账号**私聊**机器人发送 `/workbuddy_login`（国际版用 `/workbuddy_login global`）；
5. 打开回复中的授权链接完成浏览器登录，插件会自动保存凭据并把访问令牌填入提供商。

> 若第 3 步还没建提供商，登录也会成功，只是会提示你补建；建好后重启或热重载即可自动填入。

## 指令

| 指令 | 权限 | 说明 |
|------|------|------|
| `/workbuddy_login [cn\|global]` | 管理员（需私聊） | 发起浏览器授权登录；成功后自动填充 Key |
| `/workbuddy_logout` | 管理员 | 删除插件保存的凭据及其配置副本，保留手工 Key |
| `/workbuddy_usage` | 管理员 | 查询账号状态、令牌有效期与剩余 credits |
| `/workbuddy_reasoning [级别]` | 管理员 | 查看/设置推理深度 |
| `/workbuddy_image_model [auto\|list\|refresh\|模型ID]` | 管理员 | 查看、切换、刷新图片模型 |
| `/workbuddy_image <描述>` | 所有人 | 生成图片；附带或引用图片即为改图 |

## LLM 工具

| 工具名 | 功能 |
|--------|------|
| `workbuddy_generate_image` | 生成或编辑图片并直接发送给用户；默认把当前/引用图片作为编辑输入 |
| `workbuddy_image_models` | 查询当前账号可用的文生图 / 改图模型清单 |

## 配置项

| 配置 | 默认 | 说明 |
|------|------|------|
| `realm` | `cn` | 默认登录区域（`cn` 国内版 / `global` 国际版） |
| `reasoning_effort` | `medium` | 推理深度，按模型支持档位自动降级 |
| `thinking_enabled` | `true` | 为 DeepSeek 系列开启思维链 |
| `image_model` | `auto` | 文生图模型；`auto` 取目录中第一个 `text-to-image` 模型 |
| `image_edit_model` | `auto` | 改图模型；`auto` 取目录中第一个 `image-to-image` 模型 |
| `image_size` | `1024x1024` | 图片尺寸（另支持 `1024x1536` / `1536x1024`） |
| `image_n` | `1` | 单次生成张数（1-4，插件只发送第一张） |

提供商级别的配置：`api_base`（CN 默认 `https://copilot.tencent.com`，国际版 `https://www.workbuddy.ai`）、`proxy`（**留空即直连**）、`model`、`key`、`timeout`。

## 代理说明

插件的所有出站请求（登录、对话、模型目录、额度、图片）**只使用提供商配置里的 `proxy`**，不会继承系统的 `HTTP_PROXY` / `NO_PROXY` 环境变量：

- 上游按区域锁定，系统级代理会让国内版请求绕道甚至直接失败；
- 部分 `NO_PROXY` 写法（如 `::1` / `[::1]`）会让 httpx 在解析代理表时直接抛异常。

国内版建议留空直连；国际版若网络不可达，请在提供商配置的 `proxy` 中显式填写（如 `http://127.0.0.1:10808`）。

## 目录结构

```text
astrbot_plugin_workbuddy_provider/
├── main.py                # 插件入口：指令、LLM 工具、配置读写、Key 自动填充
├── workbuddy_source.py    # Provider 适配器：请求头、推理档位、模型目录、额度、图片端点
├── workbuddy_auth.py      # 内置登录流程与凭据持久化（DPAPI / 文件锁 / 原子写）
├── _conf_schema.json      # 插件配置 Schema
├── metadata.yaml
└── requirements.txt
```

## 已知限制

- 上游只接受 `stream: true`，插件的非流式入口内部走流式聚合；
- `X-Device-Token`（桌面端风控头）由官方客户端的 Turing SDK 产生，插件无法自行生成，缺失时上游目前不阻断请求；
- 自动登录依赖浏览器与官方 `state` 端点，`state` 有效期约 15 分钟，超时需重新发起。

## 许可证

AGPL-3.0
