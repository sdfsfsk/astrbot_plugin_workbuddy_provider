# WorkBuddy2API 上游协议研究报告

> 被研究对象：`workbuddy2api`（Go 1.22.5，模块名 `workbuddy2api`，开源网关项目）
> 目的：用纯 Python（httpx）直接调用上游 CodeBuddy / WorkBuddy API，不依赖该 Go 项目。
> 所有结论均给出 `路径:行号`；不确定处显式标注「不确定」。文中的账号标识与密钥均已脱敏。

---

## 0. 结论速览（先说最重要的）

| 问题 | 结论 |
|------|------|
| **图片生成** | ❌ **本项目完全不支持，也没有任何图片生成端点**。上游模型目录里带 `tags:["text-to-image"]` 的图片生成模型被**主动过滤掉**（`internal/upstream/client.go:1240-1256`、`internal/upstream/client.go:1399`）。项目里唯一与"图片"相关的能力是**图片输入（视觉理解）**，不是图片输出。 |
| 对外图像端点 | ❌ 无 `/v1/images/generations`、无 `/v1/images/edits` |
| 上游图像端点 | ❌ 代码中不存在任何图像生成上游路径（全仓库搜索 `images/generations` / `image_generation` / `text-to-image` 仅命中"过滤"逻辑） |
| 联网搜索 | ❌ 无任何内置搜索工具/开关 |
| 对话端点 | ✅ `POST {base}/v2/chat/completions`（强制 `stream:true`） |
| 图片**输入** | ✅ 通过 `messages[].content[].type="image_url"`（仅视觉模型，`supportsImages=true`） |

---

## 1. 本项目对外暴露的 HTTP 端点

### 1.1 路由注册表

注册位置：`internal/server/handler.go:105-121`

| 方法 | 路径 | 鉴权 | 处理函数 |
|------|------|------|----------|
| POST | `/v1/chat/completions` | Bearer | `handler.go:472 chatCompletions` |
| GET | `/v1/models` | Bearer | `handler.go:232 models` |
| GET | `/status` | Bearer | `handler.go:170 status` |
| GET | `/v1/stats` | Bearer | `internal/server/metrics.go:350 stats` |
| POST | `/v1/stats/reset` | Bearer | `internal/server/metrics.go:357 statsReset` |
| POST | `/admin/accounts/{uid}/disable` | Bearer | `handler.go:117`（仅 `admin.enabled=true` 时注册） |
| POST | `/admin/accounts/{uid}/enable` | Bearer | `handler.go:118`（同上） |
| POST | `/admin/accounts/{uid}/revive` | Bearer | `handler.go:119`（同上） |
| GET | `/healthz` | **无鉴权** | `handler.go:146 healthz` |

**明确不存在的端点**（不要臆想）：
`/v1/images/generations`、`/v1/images/edits`、`/v1/embeddings`、`/v1/completions`、`/v1/responses`、`/v1/audio/*`。

### 1.2 端点请求 / 响应结构

#### ① `GET /v1/models`
无请求参数。响应（`handler.go:232-237` + `handler.go:315-391`）：

```json
{
  "object": "list",
  "data": [
    {
      "id": "cn:deepseek-v4.1-flash",
      "object": "model",
      "created": 1753600000,
      "owned_by": "workbuddy",
      "context_length": 1000000,
      "max_output_tokens": 384000,
      "name": "DeepSeek V4.1 Flash",
      "description": "[x0.05 credit] 混元思考模型，具有增强的推理能力",
      "credits": "x0.05",
      "tags": ["craft", "badge:限时免费:#FF0000"],
      "vendor": "j",
      "is_default": true,
      "supports_images": true,
      "supports_reasoning": true,
      "supports_tool_call": true,
      "only_reasoning": false,
      "max_allowed_size": 192000,
      "reasoning_effort": "high",
      "reasoning_summary": "auto",
      "reasoning_supported_efforts": ["low", "high", "max"],
      "reasoning_default_effort": "high"
    }
  ]
}
```

字段来源：`handler.go:262-309 applyModelInfoFields`（**空值一律省略**，不编造）。
- `id` 前强制加 `cn:` 或 `global:` 前缀（`handler.go:319`、`handler.go:363`）——这是**本网关的路由协议**，上游只认裸名。
- `created` 硬编码 `1753600000`（`handler.go:321`、`handler.go:365`）。
- `context_length` / `max_output_tokens` 四级查找（`internal/upstream/model_catalog.go:240`、`:267`）。
- `reasoning_supported_efforts` / `reasoning_default_effort` 三级查找（`internal/upstream/effort_catalog.go:88 EffortListing`）；查不到则**整个字段省略**（不是空数组）。

#### ② `POST /v1/chat/completions`
请求体即 **OpenAI Chat Completions 格式**（透传，网关只做改写）：

```json
{
  "model": "cn:deepseek-v4.1-flash",
  "messages": [{"role": "user", "content": "hi"}],
  "stream": true,
  "reasoning_effort": "high",
  "max_tokens": 4096,
  "temperature": 0.7,
  "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
  "tool_choice": "auto"
}
```

- `model` 允许 `cn:` / `global:` 前缀（`internal/server/resolve_model.go:13`，剥前缀后才发给上游；`handler.go:597-599 rewriteModel`）。
- 请求体**无大小上限**（`handler.go:472-477`）。
- `stream=false` 时网关本地把上游 SSE 聚合成单响应（`handler.go:879-896`，聚合器 `internal/upstream/sse.go:30 Aggregate`）。

非流式响应（`sse.go:247-266`）：

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1789000000,
  "model": "deepseek-v4.1-flash",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "你好",
      "reasoning_content": "思考过程……",
      "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 20,
    "total_tokens": 30,
    "prompt_cache_hit_tokens": 7808,
    "prompt_cache_miss_tokens": 0,
    "prompt_cache_write_tokens": 0,
    "credit": 0.02
  }
}
```

流式响应 = 逐帧 `data: {...}\n\n` + 末尾 `data: [DONE]\n\n`（`sse.go:480-610 StreamHint`）。

#### ③ 错误响应（所有端点统一）
`handler.go:1118-1151`：

```json
{
  "error": {
    "message": "上游原文或网关文案",
    "type": "api_error",
    "code": "image_invalid",
    "gateway_hint": "可选：面向客户端的修复建议"
  }
}
```

`code` 取值（`handler.go`）：`invalid_api_key` :138、`invalid_request` :479、`content_blocked` :769、`prompt_too_long` :784、`image_invalid` :798、`upstream_parse` :883、`rate_limit_exceeded` :924、`waf_ip_blocked` :931、`no_healthy_account` :912。

---

## 2. 上游真实 API

### 2.1 Base URL 与路径常量

| 用途 | Realm | Base | 路径 | 位置 |
|------|-------|------|------|------|
| Chat | CN | `https://copilot.tencent.com` | `/v2/chat/completions` | `client.go:729`、`client.go:1029` |
| Chat | global | `https://www.workbuddy.ai` | `/v2/chat/completions` | `client.go:743`、`client.go:1029` |
| Billing | CN | `https://www.codebuddy.cn` | — | `client.go:730` |
| Billing | global | `https://www.workbuddy.ai` | — | `client.go:754` |
| 模型目录 | CN | chat base | `/console/enterprises/personal/models` | `client.go:1220` |
| 模型目录 | global | chat base | `/v2/enterprises/personal/models` | `client.go:1221` |
| 模型目录（双域通用） | both | chat base | `/v3/config` | `client.go:1222` |
| Token 刷新 | both | chat base | `/v2/plugin/auth/token/refresh` | `client.go:957` |
| 登录：取授权 URL | both | chat base | `POST /v2/plugin/auth/state?platform=CLI` | `cmd/login/main.go:213` |
| 登录：轮询 token | both | chat base | `GET /v2/plugin/auth/token?state=<state>` | `cmd/login/main.go:250` |
| 登录：取账号 | both | chat base | `GET /v2/plugin/login/account?state=<state>` | `cmd/login/main.go:276` |
| 余额 | CN | billing base | `/v2/billing/meter/get-user-resource` | `client.go:874` |
| 余额 | global | billing base | `/billing/meter/get-user-resource` | `client.go:872` |
| 签到 | CN | billing base | `/v2/billing/meter/daily-checkin` | `client.go:875` |
| 签到 | global | billing base | `/billing/meter/daily-checkin` | `client.go:873` |
| 领取 trial | global | billing base | `/billing/ide/trial` | `scripts/global_region.py:143` |
| 地区查询 | global | billing base | `/billing/area/get-country-code`、`/billing/area/get-user-area-info` | `scripts/global_region.py:72,90` |
| 事件上报 | CN | billing base | `/v2/report` | `scripts/task_common.py:45` |

> **重要**：global 账号的 chat **也走 `/v2`**，不是 `/console`。原因见 `client.go:1038-1041`（`/console` 挂腾讯云 WAF body 内容规则，命中 `printf`/`whoami` 等特征会确定性 403）。

### 2.2 上游统一业务信封

`internal/upstream/client.go:628-633`：

```go
type apiEnvelope struct {
    Code int             `json:"code"`
    Msg  string          `json:"msg"`
    Data json.RawMessage `json:"data"`
}
```

即所有非 chat 上游接口返回 `{"code":0,"msg":"","data":...}`，`code != 0` 视为业务错误。

### 2.3 出站请求体改写管线（这是"能否被上游接受"的关键）

入口 `internal/upstream/payload.go:36 PrepareBodyOptWithEffortsAndDefault`，按顺序执行：

| 步骤 | 行为 | 位置 |
|------|------|------|
| 1 | **强制 `obj["stream"] = true`**（上游拒绝非流式） | `payload.go:44` |
| 2 | `max_completion_tokens` → `max_tokens`（仅当无显式 `max_tokens` 且值为正整数；别名一律删除） | `payload.go:106-131` |
| 3 | 无 `stream_options` 时补 `{"include_usage": true}` | `payload.go:59-61` |
| 4 | `tool_choice` 归一化 | `payload.go:304-347` |
| 5 | `role: "developer"` → `"system"` | `payload.go:210-229` |
| 6 | `image_url` 字符串 → `{"url": "..."}` 对象 | `payload.go:240-266` |
| 7 | tool_calls / tool 结果配对重排 + 孤儿清理 | `payload.go:70-77` |
| 8 | DeepSeek 思维链注入 `thinking` + 默认 effort | `thinking.go:156-204` |
| 9 | `reasoning_effort` 按模型能力降级 | `payload.go:141-197` |
| 10 | assistant 消息回填 `reasoning_content` | `thinking.go:70-144` |
| 11 | 指纹脱敏（可开关） | `payload.go:87-91` |
| 12 | 注入 `prompt_cache_key`（费用优化 ~17×） | `client.go:789`、`cache_key.go:35-79` |
| 13 | global 域：首条非 system 时前置 `{"role":"system","content":"You are a helpful assistant."}` | `payload.go:272-296`、`client.go:1065-1067` |

**`tool_choice` 归一化细节**（`payload.go:304-347`）——上游该字段是 Go `string` 类型，传对象会 400 `code=11101`：

```
"none"                                  → 删 tool_choice + 删 tools + 删 functions
{"type":"none"}                         → 同上
{"type":"auto"|"required"}              → 字符串 "auto" / "required"
{"type":"function","function":{"name":"x"}} → 字符串 "x"
其他对象 / 非标量                        → 删 tool_choice
```

**`prompt_cache_key` 格式**（`cache_key.go:68-79`）：

```
wb2a-<uid前8字符>-<sha256(uid + "|" + conversationID)[:16] 的 hex>
```

### 2.4 上游 SSE 流式事件格式

`internal/upstream/sse.go`。

**上游原始帧**（`sse.go:148-205` 解析逻辑）：

```
data: {"id":"...","object":"chat.completion.chunk","created":123,"model":"deepseek-v4.1-flash","choices":[{"index":0,"delta":{"role":"assistant"}}]}

data: {"id":"...","choices":[{"index":0,"delta":{"content":"你"}}]}

data: {"id":"...","choices":[{"index":0,"delta":{"reasoning_content":"思"}}]}

data: {"id":"...","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"f","arguments":"{\"a\""}}]}}]}

data: {"id":"...","choices":[{"index":0,"finish_reason":"stop"}]}

data: {"id":"...","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30,"credit":0.02}}

data: [DONE]
```

解析器逐行只认前缀 `"data: "`（`sse.go:148`、`sse.go:569`），`data: [DONE]` 结束（`sse.go:150`、`sse.go:565`）。
末帧 `usage` 字段实测键：`prompt_tokens` / `completion_tokens` / `total_tokens` / `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` / `prompt_cache_write_tokens` / `credit`（`metrics.go:330-337`）。

**网关照 OpenAI 规范白名单重建后的帧**（`sse.go:391-463 normalizeFrame`）：

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion.chunk",
  "created": 1789000000,
  "model": "deepseek-v4.1-flash",
  "choices": [
    {
      "index": 0,
      "delta": {"role": "assistant", "content": "你", "reasoning_content": "思"},
      "finish_reason": null
    }
  ],
  "usage": null
}
```

白名单顶层键：`id`、`object`、`created`、`model`、`system_fingerprint`、`service_tier`（`sse.go:393`）。
`delta` 白名单键：`role`、`content`、`reasoning_content`、`refusal`、`tool_calls`、`function_call`（`sse.go:417-445`）。
空串 content / 空 delta 键一律省略；`finish_reason` 空 → `null`；`usage` 缺失 → `null`。

**流式错误帧**：带 `error` 键的帧**原样透传**（不走白名单），可能被附加 `error.gateway_hint`（`sse.go:525-530`、`sse.go:625-639`）。

**`tool_calls` 特殊处理**：
- 首片保留 `function.name`，同 `index` 后续分片**删除 name 键**（`sse.go:352-385 stripToolCallNames`）。
- 缺 `index` 时按「id 优先 → 最近 index 兜底」归位（`sse.go:89-123`）。
- 截断流（`finish_reason=="length"` 或未收到 `[DONE]`）丢弃残缺 `tool_calls`（`sse.go:240-242`）。

### 2.5 Python (httpx) 最小可用调用示例

```python
import httpx, json, uuid, hashlib

ACCESS_TOKEN = "<从 auths/*.json 的 auth.accessToken 读取>"
UID          = "<auths/*.json 的 account.uid>"
REALM        = "cn"          # 或 "global"
DEPLOYER     = "WorkBuddy"   # global 用 "WorkBuddy AI"

if REALM == "cn":
    BASE, ORIGIN = "https://copilot.tencent.com", "https://www.codebuddy.cn"
else:
    BASE, ORIGIN = "https://www.workbuddy.ai", "https://www.workbuddy.ai"

def sid(purpose: str) -> str:
    return hashlib.sha256(f"wb2a:{purpose}:{UID}".encode()).hexdigest()[:36]

mid  = uuid.uuid4().hex                       # 32 hex
conv = uuid.uuid4().hex[:32]

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": ORIGIN,
    "Referer": ORIGIN + "/",
    "User-Agent": f"WorkBuddy/5.5.4 {DEPLOYER}/5.5.4 CLI/2.137.1",
    "X-CodeBuddy-Request": "1",
    "Accept-Language": "zh-CN" if REALM == "cn" else "en-US",
    "Authorization": "Bearer " + ACCESS_TOKEN,
    "X-User-Id": UID,
    "X-Machine-ID": sid("machine"),
    "X-Session-ID": sid("session"),
    "X-Conversation-Request-ID": conv,
    "X-Conversation-Message-ID": mid,
    "X-Request-ID": mid,
    "X-Root-Request-ID": conv,
    "X-Trace-ID": conv,
    "X-B3-TraceId": conv, "X-B3-SpanId": mid[:16], "X-B3-Sampled": "1",
    "X-Agent-Purpose": "conversation",
    "X-IDE-Name": DEPLOYER, "X-IDE-Type": DEPLOYER,
    "X-IDE-Version": "5.5.4", "X-Product": DEPLOYER,
    "X-No-Enterprise-Id": "1", "X-No-Department-Info": "1",
}
if REALM == "global":
    headers["X-Domain"] = "www.workbuddy.ai"

body = {
    "model": "deepseek-v4.1-flash",     # 裸名，无 cn:/global: 前缀
    "messages": [{"role": "user", "content": "你好"}],
    "stream": True,                     # 上游强制要求
    "stream_options": {"include_usage": True},
    "reasoning_effort": "high",         # 可选
    "prompt_cache_key": f"wb2a-{UID[:8]}-" + hashlib.sha256((UID + "|" + conv).encode()).hexdigest()[:32],
}

with httpx.Client(timeout=httpx.Timeout(120.0, read=None)) as c:
    with c.stream("POST", BASE + "/v2/chat/completions", headers=headers, json=body) as r:
        if r.status_code >= 400:
            print(r.status_code, r.read().decode())
        else:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    p = line[6:]
                    if p == "[DONE]":
                        break
                    print(json.loads(p))
```

> 注意：`max_tokens` 请直接使用（**不要**发 `max_completion_tokens`）；`tool_choice` 请直接发字符串。

---

## 3. 图片生成（最重要的一问）——**本项目不支持**

### 3.1 决定性证据

**证据 1：路由表里没有任何图像端点。**
`internal/server/handler.go:105-121` 完整列出全部 9 条路由，无 `/v1/images/*`。

**证据 2：图片生成模型被主动过滤掉（这是最关键的证据）。**
`internal/upstream/client.go:1236-1257`：

```go
// nonChatModel 判定是否非对话模型（应从模型列表过滤掉）。
// 来源：harness buddy.ts:547-555。三类规则：
//   - id 前缀 nes-/completion-/codewise-：嵌入/补全/代码专用模型，选了报 code=11102。
//   - maxOutputTokens ≤ 256：tiny 输出非对话模型。
//   - tags 含 text-to-image：图片生成模型，非本网关用途。
func nonChatModel(id string, maxOutputTokens int64, tags []string) bool {
	id = strings.ToLower(strings.TrimSpace(id))
	for _, p := range [...]string{"nes-", "completion-", "codewise-"} {
		if strings.HasPrefix(id, p) {
			return true
		}
	}
	if maxOutputTokens > 0 && maxOutputTokens <= 256 {
		return true
	}
	for _, t := range tags {
		if t == "text-to-image" {
			return true          // ← 第 1252 行：图片生成模型直接判为"非对话模型"
		}
	}
	return false
}
```

`internal/upstream/client.go:1399`（FetchModels 尾部再次确认）：

```go
// tags 含 text-to-image）根本不进返回列表（来源：harness buddy.ts:547-555）。
```

对应单元测试 `internal/upstream/models_fields_test.go:135-148`：

```go
// TestFetchModelsFiltersTextToImage P2：tags 含 text-to-image 过滤。
{"id":"img-gen","name":"IMG","maxInputTokens":8192,"maxOutputTokens":8192,"tags":["text-to-image","chat"]},
...
t.Fatalf("expected only glm-5.2 (no text-to-image), got %+v", infos)
```

**证据 3：全仓库穷举搜索结果。** 对全部 `.go/.md/.json/.sh/.ps1/.py/.yml/.cmd` 文件搜索
`images/generations|image_generation|text-to-image|generate_image|文生图|图片生成|画图|draw_image|t2i`，
**唯一命中就是上面那三处"过滤"逻辑**，无任何"生成"逻辑。

### 3.2 本项目唯一的"图片"能力 = 图片**输入**（视觉理解）

触发方式：在 `messages[].content[]` 里放 `{"type":"image_url"}` part。

```json
{
  "model": "cn:hy3",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "描述这张图"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    ]
  }]
}
```

- 上游**只认对象形态** `{"url": "..."}`；字符串形态会被网关自动包装（`payload.go:231-266 normalizeImageURL`），字符串形态直发上游会 400 `code=11101`（原文：`Parse message failed: invalid image_url content ... cannot unmarshal string into Go value of type v2.ImageContent`，见 `client_test.go:56`）。
- 对象内可带 `detail`、`mime_type` 字段（`payload_test.go:216-217`）。
- 仅 `supportsImages=true` 的模型可用；网关在 `/v1/models` 透出 `supports_images`（`handler.go:287-289`）。
- 模型不支持图片时报错 11133；图片数据非法报 11135（`hint.go:31-50`、`hint_test.go:44-48`）。

### 3.3 关于"上游是否有图片生成 API"

**不确定。** 本项目代码中**不存在**任何指向图片生成上游端点的证据：
- 已知的上游模型目录响应里存在 `tags` 字段，且**确实存在** `text-to-image` 这个 tag 值（否则 `client.go:1252` 的过滤分支和它的测试用例（`models_fields_test.go:139` 用 `"tags":["text-to-image","chat"]`）不会存在）——这说明**上游确实存在图片生成模型**。
- 但本项目**没有实现**调用它们，代码里也**没有**记录对应的端点路径或请求体格式。
- 因此：**图片生成的上游端点路径、请求体、响应格式在本仓库中无任何依据**，无法从本仓库推断。若需要，只能另行逆向（不在本报告范围内）。

---

## 4. 内置模型清单

### 4.1 `internal/upstream/model.json`（28 个条目，全部 `"source": "seed"`）

| 模型 ID | context_length | max_output_tokens | 行号 |
|---------|---------------|-------------------|------|
| `glm-5.2` | 1000000 | 131072 | :2-6 |
| `glm-5.1` | 200000 | 131072 | :7-11 |
| `glm-5.3` | 1000000 | 131072 | :12-16 |
| `glm-5.3-flash` | 1000000 | 131072 | :17-21 |
| `glm-5v-turbo` | 200000 | 131072 | :22-26 |
| `kimi-k2.7` | 256000 | 65536 | :27-31 |
| `kimi-k2.6` | 256000 | 262144 | :32-36 |
| `kimi-k2.5` | 164000 | 262144 | :37-41 |
| `kimi-k3` | 1048576 | 131072 | :42-46 |
| `kimi-k2.8-preview` | 1048576 | *(省略)* | :47-50 |
| `minimax-m3` | 512000 | 512000 | :51-55 |
| `hy3` | 192000 | 64000 | :56-60 |
| `hy3-preview` | 262144 | 64000 | :61-65 |
| `hy4-preview` | 1000000 | 64000 | :66-70 |
| `hy4-preview-x` | 1000000 | 64000 | :71-75 |
| `deepseek-v4-pro` | 1000000 | 384000 | :76-80 |
| `deepseek-v4-flash` | 1000000 | 384000 | :81-85 |
| `deepseek-v4.1-flash` | 1000000 | 384000 | :86-90 |
| `gpt-6-astra` | 1050000 | 128000 | :91-95 |
| `gpt-5.6-sol` | 1050000 | 128000 | :96-100 |
| `gpt-5.6-terra` | 1050000 | 128000 | :101-105 |
| `gpt-5.6-luna` | 1050000 | 128000 | :106-110 |
| `gpt-5.5` | 1050000 | 128000 | :111-115 |
| `gpt-5.4` | 1050000 | 128000 | :116-120 |
| `gpt-5.3-codex` | 400000 | 128000 | :121-125 |
| `gemini-3.5-flash` | 1048576 | 65536 | :126-130 |
| `auto` | 168000 | *(省略)* | :131-134 |

> **重要澄清**：`model.json` **不是**"可用模型白名单"，而是 `context_length` / `max_output_tokens` 的**本地缓存兜底表**（第 3 级查找，见 `model_catalog.go:1-17`）。真实的 `/v1/models` 列表是**纯动态**从上游拉的（`handler.go:231`、`handler.go:313`），**失败就返回空列表，无静态兜底**。

### 4.2 `internal/upstream/context_catalog.go:40-79`（第 2 级静态知识表）

与上面几乎一致的 27 条（不含 `kimi-k2.8-preview` 之外的差异），额外说明：`kimi-k2.8-preview` 在 context_catalog 中 `maxOutput: 0`（省略）；`hy4-preview-x` 输出按同族估算。兜底常量 `DefaultContextWindow = 1000000`（`context_catalog.go:27`）。

### 4.3 `internal/upstream/global_models.go:23-45`（global 历史静态名单，21 个）

> 注释明确：**"纯动态化后不再作为模型目录的基底/兜底…生产链路对本名单零引用。保留仅作历史对照"**（`global_models.go:20-22`）。仅列出，**不可依赖**：

`default-model`、`fast-model`、`balanced-model`、`primary-model`、`hy4-preview`、`gpt-5.6-sol`、`gpt-5.6-terra`、`deep-model`、`deepseek-v4.1-flash`、`gpt-6-astra`、`hy4-preview-f`、`hy3`、`glm-5.2`、`gpt-5.6-luna`、`gpt-5.5`、`gpt-5.4`、`gpt-5.3-codex`、`gemini-3.5-flash`、`glm-5.3`、`kimi-k3`、`kimi-k2.6`

### 4.4 实战观测到的模型名（`data/state.json` 的 `model_costs` / `model_cooldowns`）

`auto`、`balanced-model`、`deep-model`、`deepseek-v3-2-volc`、`deepseek-v4.1-flash`、`fast-model`、`glm-5.0-turbo`、`hunyuan-2.0-instruct`、`hy4-preview`、`hy4-preview-f`、`kimi-k2.5`、`minimax-m2.7`、`minimax-m3-pay`、`default-1.1`、`default-1.2`、`glm-4.6v`、`kimi-k2-thinking`、`minimax-m2.5`

（这些是真实账号跑出来的，说明上游模型池比静态表大得多。）

---

## 5. reasoning effort 合法取值与放置方式

### 5.1 合法取值全集（有序，从低到高）

`internal/upstream/payload.go:134`：

```go
var effortRank = map[string]int{"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}
```

**合法值 = `off`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`**（共 7 个）。
不在表内的值 → **原样透传**（不降级，`payload.go:166-169`）；上游可能因此 400。

### 5.2 各模型实际支持的档位

#### CN / CodeBuddy 面（`effort_catalog.go:25-42 cnEffortFallback`）

| 模型 | supported_efforts | default |
|------|------------------|---------|
| `deepseek-v4-flash` | low, high, max | *(未声明)* |
| `deepseek-v4.1-flash` | low, high, max | high |
| `deepseek-v4-pro` | low, high, **xhigh** | high |
| `hy4-preview` | high | high |
| `hy4-preview-x` | high | *(未声明)* |
| `hy3` | low, high | high |
| `hy3-x` | low, high | high |
| `glm-5.3` | low, high, max | high |
| `glm-5.3-flash` | low, high, max | high |
| `glm-5.2` | high, **xhigh** | high |
| `glm-5.1` | medium | *(未声明)* |
| `glm-5v-turbo` | medium | *(未声明)* |
| `kimi-k3-1` | medium | *(未声明)* |
| `kimi-k2.7` | medium | *(未声明)* |
| `kimi-k2.6` | medium | *(未声明)* |
| `minimax-m3` | medium | *(未声明)* |

#### global / WorkBuddy 面（`effort_catalog.go:47-66 globalEffortFallback`）

| 模型 | supported_efforts | default |
|------|------------------|---------|
| `fast-model` | medium | — |
| `balanced-model` | medium | — |
| `primary-model` | high | — |
| `hy4-preview-f` | high | high |
| `hy3` | low, high | high |
| **`deepseek-v4.1-flash`** | **仅 high** | — |
| `gpt-6-astra` | low, medium, high, xhigh, max | high |
| `gpt-5.6-sol` | low, medium, high, xhigh, max | high |
| `gpt-5.6-terra` | low, medium, high, xhigh, max | high |
| `gpt-5.6-luna` | low, medium, high, xhigh, max | high |
| `gpt-5.5` | low, medium, high, xhigh | high |
| `gpt-5.4` | low, medium, high, xhigh | high |
| `gpt-5.3-codex` | medium | — |
| `gemini-3.5-flash` | medium | — |
| `glm-5.3` | low, high, max | high |
| `glm-5.2` | high, xhigh | high |
| `kimi-k3` | medium | — |
| `kimi-k2.6` | medium | — |

> **关键差异（`effort_catalog.go:44-46` 明示）**：同一个 `deepseek-v4.1-flash`，CN 面三档 `['low','high','max']`，**global 面只有 `['high']`** —— 往 global 上游发 `low`/`max` 是**非法参数 400**。两个 realm 的档位表**绝不混用**。

### 5.3 怎么放进请求（三种合法形态）

**形态 A：只发 `reasoning_effort`（snake_case）** —— 最简单

```json
{"model": "deepseek-v4.1-flash", "messages": [...], "stream": true, "reasoning_effort": "high"}
```

**形态 B：`reasoningEffort`（camelCase）** —— 上游同样识别

```json
{"reasoningEffort": "high"}
```

> 两个字段名都被识别，snake 优先（`payload.go:153-160`）。只发其中一个即可。

**形态 C：`reasoning_effort` + `thinking`（DeepSeek 系推荐，官方客户端形态）**

```json
{
  "model": "deepseek-v4.1-flash",
  "messages": [...],
  "stream": true,
  "thinking": {"type": "enabled"},
  "reasoning_effort": "high"
}
```

`thinking.type` 取值：`"enabled"` / `"disabled"`（`thinking.go:156-186`）：
- `thinking.type="disabled"` → 网关会**删除** `reasoning_effort` / `reasoningEffort`（`thinking.go:169-173`）。
- `thinking.type="enabled"` 但缺 effort → 补默认档（模型声明的 `defaultEffort`，否则硬编码 `"high"`，`thinking.go:32`）。
- **对 `deepseek*` 前缀模型**：不给 `thinking` 时，网关会**自动注入** `{"type":"enabled"}` + 默认 effort（`thinking.go:177-185`）。**这条很关键**：裸请求不给 effort 时上游按"不思考"应答，`reasoning_content` 长度为 0（`thinking.go:9-16` 注释）。

### 5.4 网关的降级算法（Python 侧可复刻）

`payload.go:141-197 normalizeReasoningEffort`：
1. 模型在支持表里且请求档位**被支持** → 原样透传。
2. 请求档位**不被支持** → 改为「**≤ 请求档位的最高支持档**」。
3. 支持档**全部高于**请求档 → 取**最低支持档**。
4. 未知模型 / 未知档位 / 未携带该字段 → **一律透传**（不降级）。

---

## 6. 联网搜索等特殊能力

**❌ 本项目不支持，也没有任何开关。**

全仓库搜索 `web_search|websearch|联网|search_tool` 无命中（唯一相关命中是 `tool_choice` 归一化与"不联网"的测试注释）。

**理论上可以用 tools 让模型自己调**——上游支持 function calling（`supportsToolCall` 能力旗标，`client.go:1152`），`tools` / `functions` 字段原样透传（仅 `tool_choice` 会被归一化，`payload.go:298-347`）。但**模型不会自己联网**；`tools` 必须由你的 Python 客户端自己实现并回传结果（标准 OpenAI 多轮 tool 循环）：

```json
{
  "model": "deepseek-v4.1-flash",
  "messages": [{"role": "user", "content": "今天的新闻"}],
  "stream": true,
  "tools": [{
    "type": "function",
    "function": {
      "name": "web_search",
      "description": "搜索网页",
      "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    }
  }],
  "tool_choice": "auto"
}
```

> ⚠️ tool 配对是硬性约束：工具结果必须紧跟对应的 `tool_calls`，中间插入其他消息会导致上游对之后每条消息返 400。网关为此做了重排 + 孤儿清理（`payload.go:65-77`、`internal/upstream/tool_pairing.go`），Python 侧需自己保证配对正确。

**其他非对话能力**（签到/积分/旅行/开学季/猫猫任务）都在 `internal/scheduler/` 与 `scripts/task_runner.py`，属于账号运营而非模型能力，与"联网搜索"无关。

---

## 7. 认证头与 token 携带方式

### 7.1 对网关（本项目的入站鉴权）

`internal/server/handler.go:129-144`：

```
Authorization: Bearer <config.json 的 api_key>
```

- `api_key` 若为空串 → **完全不鉴权**。
- 用 `subtle.ConstantTimeCompare` 常量时间比较，必须严格 `Bearer ` 前缀。
- 失败响应：`401 {"error":{"message":"missing or invalid API key","type":"api_error","code":"invalid_api_key"}}`。
- `config.json` 中为形如 `sk-wb2api-<40 位 hex>` 的网关密钥（部署时自行生成，勿外泄）。

### 7.2 对上游（真正要复刻的部分）

构造函数：`internal/upstream/headers.go:153-174 CommonHeaders` + `:220-267 ChatHeaders`。

#### Chat 请求（`/v2/chat/completions`）完整头集

| 头名 | 值 | 位置 |
|------|-----|------|
| `Content-Type` | `application/json` | `headers.go:154` |
| `Accept` | `application/json, text/event-stream`（chat 覆盖） | `headers.go:223` |
| `X-Requested-With` | `XMLHttpRequest` | `headers.go:158` |
| `Origin` | CN `https://www.codebuddy.cn` / global `https://www.workbuddy.ai` | `headers.go:159`、`:28-29` |
| `Referer` | Origin + `/` | `headers.go:161` |
| `User-Agent` | `WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1`（CN）<br>`WorkBuddy/5.5.4 WorkBuddy AI/5.5.4 CLI/2.137.1`（global） | `headers.go:67-73`、`:21`、`:26` |
| `X-CodeBuddy-Request` | `1`（风控闸门头，**所有** API 必带） | `headers.go:165` |
| `Accept-Language` | CN `zh-CN` / global `en-US` | `headers.go:168`、`:177-182` |
| **`Authorization`** | **`Bearer <accessToken>`**；token 为空时改为 `X-No-Authorization: 1` | `headers.go:226-230` |
| **`X-User-Id`** | `<uid>`；为空时 `X-No-User-Id: 1` | `headers.go:231-235` |
| `X-Enterprise-Id` | 有则发；否则 `X-No-Enterprise-Id: 1` | `headers.go:242-246` |
| `X-Domain` | 有则发；否则 `X-No-Department-Info: 1` | `headers.go:247-251` |
| `X-Machine-ID` | `sha256("wb2a:machine:" + uid)` 前 **36 hex** | `headers.go:135-150` |
| `X-Session-ID` | `sha256("wb2a:session:" + uid)` 前 **36 hex** | `headers.go:135-150` |
| `X-Device-Token` | 可选（`auth.DeviceToken` > `config.device_token` > 文件） | `headers.go:104-122` |
| `X-Conversation-ID` | 会话级，body 的 `conversation_id` 提取；空则**不发** | `headers.go:291-293` |
| `X-Conversation-Request-ID` | **轮级聚合主键，必发** | `headers.go:294` |
| `X-Conversation-Message-ID` | 消息级 32 hex | `headers.go:295` |
| `X-Request-ID` | 与上同值 | `headers.go:296` |
| `X-Root-Request-ID` | = conversationRequestID | `headers.go:297` |
| `X-Trace-ID` | 入站透传值，空则回落 conversationRequestID | `headers.go:298-302` |
| `X-B3-TraceId` | conversationRequestID（非法则回落 messageID） | `headers.go:303-307` |
| `X-B3-SpanId` | `messageID[:16]` | `headers.go:308` |
| `X-B3-Sampled` | `1` | `headers.go:309` |
| `X-Agent-Purpose` | `conversation` | `headers.go:351` |
| `X-IDE-Name` / `X-IDE-Type` | `WorkBuddy`（默认） | `headers.go:352-353` |
| `X-IDE-Version` | `5.5.4` | `headers.go:354` |
| `X-Product` | `WorkBuddy` | `headers.go:355` |
| global 额外：`X-No-Enterprise-Id` | `1` | `headers.go:200` |
| global 额外：`X-Domain` | `www.workbuddy.ai` | `headers.go:201` |
| 可选 IP 透传：`X-Forwarded-For` / `X-Real-IP` / `X-Client-IP` | 仅 `upstream.passthrough_ip=true` | `headers.go:361-368` |

> **安全红线**（`headers.go:236`）：chat 请求中**绝不携带** `X-Refresh-Token`。

#### Refresh 请求（`/v2/plugin/auth/token/refresh`）

`headers.go:426-434 RefreshHeaders`：

```
POST {chat_base}/v2/plugin/auth/token/refresh
Content-Type: application/json
Accept: application/json
X-Requested-With: XMLHttpRequest
Origin / Referer: <realm 域>
User-Agent: WorkBuddy/5.5.4 <platform>/5.5.4 CLI/2.137.1
X-CodeBuddy-Request: 1
Accept-Language: zh-CN | en-US
X-Machine-ID / X-Session-ID: 同上派生
X-Refresh-Token: <refreshToken>          ← 只允许出现在这里
X-Enterprise-Id: <enterpriseId>（若有）
X-Auth-Refresh-Source: plugin
```

无请求体（`client.go:960` 传 `nil` body）。响应（`client.go:985-990`）：

```json
{"code": 0, "msg": "", "data": {"accessToken": "...", "refreshToken": "...", "expiresIn": 5184000, "domain": "www.codebuddy.cn"}}
```

> 实测 `expiresIn` 恒为 5184000（60 天），见 `client.go:1017`。响应中 `accessToken` / `refreshToken` **总是同时轮换**（`client.go:998-999`）。

#### Billing 请求头（`headers.go:398-423 BillingHeaders`）

与 chat 的差异：
- `User-Agent` 是**单段** `WorkBuddy/<clientVersion>`（不带 CLI 段，`headers.go:92-97`）——对齐官方 banner/check-in 白名单头组。
- 额外 `X-Tenant-Id: <enterpriseId>`（`headers.go:416`）。
- 无会话头族（无 X-Conversation-*、无 B3）。

### 7.3 凭据文件结构（`auths/workbuddy-<uid>.json`）

```json
{
  "account": {
    "uid": "<uuid>",
    "enterpriseId": "",
    "nickname": "<昵称>"
  },
  "auth": {
    "accessToken": "<RS256 JWT>",
    "refreshToken": "<RS256 JWT，typ=Offline>",
    "expiresAt": 1794713179,
    "domain": "www.codebuddy.cn",
    "realm": "cn"
  }
}
```

（实际文件为 `auths/workbuddy-<uid>.json`；`realm` 取值 `"cn"` / `"global"`，`internal/auth/auth.go` 的 `Realm()` 归一化，空/非法 → `cn`。）

### 7.4 登录（拿 token）流程——纯 Python 可复刻

`cmd/login/main.go`：

```
① POST {base}/v2/plugin/auth/state?platform=CLI     body: {}      → data: {state, authUrl}
② 浏览器打开 authUrl 完成登录
③ GET  {base}/v2/plugin/auth/token?state=<state>                  → data: {accessToken, refreshToken, expiresIn, domain}
④ GET  {base}/v2/plugin/login/account?state=<state>  + Bearer     → data: {uid, enterpriseId, nickname}
```

- 登录阶段 UA 用 `CLI/2.63.2 CodeBuddy/2.63.2`（`cmd/login/main.go:41`），**与运行期 UA 不同**。
- 登录阶段 `Accept: application/json, text/plain, */*`（`cmd/login/main.go:68`）。
- **无 PKCE**，`state` 由服务端签发（`cmd/login/main.go:17`）。
- `state` 与 `realm` 必须同域（`cmd/login/main.go:199-206`）。

---

## 8. 给纯 Python 实现的落地要点清单

| # | 要点 | 依据 |
|---|------|------|
| 1 | chat 端点 = `{base}/v2/chat/completions`，CN base=`https://copilot.tencent.com`，global base=`https://www.workbuddy.ai` | `client.go:729/743/1029` |
| 2 | **必须 `stream: true`**，上游拒绝非流式 | `payload.go:44` |
| 3 | 用 `max_tokens`，**不要**用 `max_completion_tokens` | `payload.go:106-131` |
| 4 | `tool_choice` 用**字符串**，对象形式上游 400 `11101` | `payload.go:3`、`:304-347` |
| 5 | `role` 不要用 `developer`，改写为 `system` | `payload.go:210-229` |
| 6 | `image_url` 必须是 `{"url": "..."}` 对象 | `payload.go:231-266` |
| 7 | `Authorization: Bearer <accessToken>` + `X-User-Id: <uid>` 必带 | `headers.go:226-235` |
| 8 | `X-CodeBuddy-Request: 1` 必带（风控闸门） | `headers.go:165` |
| 9 | UA 必须三段式 `WorkBuddy/<v> <platform>/<v> CLI/<v>`；global 平台段是 `WorkBuddy AI`（送错可能 403 `11140`） | `headers.go:58-73` |
| 10 | Origin/Referer 必须与 realm 同域 | `headers.go:159-161` |
| 11 | 建议带 `prompt_cache_key`，费用降约 17× | `cache_key.go:1-9` |
| 12 | DeepSeek 系必须带 `thinking.type=enabled` + `reasoning_effort`，否则无思维链 | `thinking.go:9-16` |
| 13 | effort 必须按 realm + 模型查表，global 的 `deepseek-v4.1-flash` **只认 high** | `effort_catalog.go:44-46` |
| 14 | 上游错误体格式 `{"code":<int>,"msg":"...","requestId":"..."}`，业务码见 `client.go:286`（11102）、`:259`（6004） | `client.go:628-633` |
| 15 | **图片生成无实现，无端点，无参考** | 见 §3 |
| 16 | 联网搜索无实现 | 见 §6 |

---

## 9. 明确标注「不确定」的事项

1. **上游图片生成 API 的端点与协议**：本仓库无任何依据。仅能确认上游模型目录里存在 `tags:["text-to-image"]` 的模型（由 `client.go:1252` 的过滤分支 + `models_fields_test.go:139` 的构造用例反推），但其调用方式未知。
2. **图片编辑 / 参考图 / 多图生成**：本仓库无任何依据，无法判断上游是否支持。
3. **`X-Device-Token` 的具体取值来源与格式**：`headers.go:104-115` 只说三级优先级，未给出 token 本身的获取方式（`config.json:53-54` 当前为空）。
4. **`/v3/config` 的完整响应结构**：只解析了 `data.models[].{id,name,disabled,...}`（`global_models.go:370-410`），其余字段未记录。
5. **上游 `usage.credit` 的确切计价单位**：`client.go` 按「每千 token 单价」折算（`state.json` 中是 `cost_per_1k`），但上游原始语义未在代码中说明。
6. **`GLM`/`Kimi` 等非 DeepSeek 模型的 thinking 字段格式**：`thinking.go:7` 提到「glm/kimi 走其他 thinkingFormat（qwen 系 enable_thinking 或默认开）」，但网关**未实现**这些格式的注入——即本仓库不知道 glm/kimi 的正确开关字段名。
