# Neko AI Search

Neko AI Search 是一个 AI 搜索答案引擎示例项目。它会先通过 Tavily 获取实时
网页搜索结果，再把检索到的来源交给 DeepSeek 兼容 OpenAI 的聊天模型生成带引用
的 Markdown 答案，最后以前端 SSE 流式输出的方式展示答案、引用来源、完整搜索
结果和相关追问。

项目整体体验接近 Perplexity 一类的答案搜索产品：用户输入问题后，可以看到 AI
逐字生成回答，同时保留可核查的原始网页结果。

## 功能特性

- AI 搜索问答：基于网页搜索结果生成综合回答。
- 来源引用：要求模型使用 `[1]`、`[2]` 等编号标注事实来源。
- SSE 流式输出：后端持续推送搜索进度、答案 token 和相关问题。
- Markdown 渲染：前端支持 Markdown 内容与代码高亮。
- 完整结果列表：除 AI 回答外，也展示全部搜索来源，便于继续核验。
- 本地搜索历史：使用 `localStorage` 保存最近搜索记录。
- 搜索缓存：相同问题命中缓存时直接返回已有答案，减少重复搜索和 AI 调用。
- 多模态结果列表：搜索结果支持网页、图片、视频和文件四种类型展示。
- 结果排序：根据相关度、来源权威性、内容时效性和结果类型重排搜索结果。
- 快速/深度模式：前端可切换回答模式，分别平衡速度成本和完整性。
- 搜索可观测性：每次搜索生成 `search_id`，实时返回步骤状态、耗时和错误位置。
- 成本保护：提供按 IP 限流、每日外部调用配额和流式并发限制。
- 快速回答模式：压缩模型上下文并默认用规则生成相关问题，降低回答耗时。
- 安全防护：拦截提示注入输入，并清洗搜索来源和模型输出中的风险内容。
- Mock 模式：缺少外部 API Key 时可使用本地模拟数据进行开发调试。

## 技术栈

### 后端

| 技术 | 作用 |
| --- | --- |
| Python 3.10+ | 后端运行环境 |
| FastAPI | HTTP API、CORS 和流式响应 |
| Uvicorn | ASGI 服务运行器 |
| Pydantic | 请求和响应数据结构校验 |
| LangChain | 统一调用搜索工具和聊天模型 |
| langchain-tavily | Tavily 搜索集成 |
| langchain-openai | 通过 OpenAI 兼容协议调用 DeepSeek |
| python-dotenv | 从 `.env` 加载本地环境变量 |
| pytest | 后端单元测试 |

### 前端

| 技术 | 作用 |
| --- | --- |
| Vue 3 | 单页应用与响应式状态管理 |
| TypeScript | 前端类型约束 |
| Vite | 前端开发服务器和构建工具 |
| marked | Markdown 转 HTML |
| marked-highlight | Markdown 代码块高亮适配 |
| highlight.js | 代码语法高亮 |
| lucide-vue | 图标组件 |

## 项目结构

```text
neko-ai-search/
    backend/
        app/
            main.py                 # FastAPI 入口、路由和 SSE 编排
            config.py               # 环境变量配置
            schemas.py              # API 请求/响应模型
            services/
                ai_service.py       # DeepSeek 提示词、流式生成和相关问题生成
                cache_service.py    # 搜索结果缓存和查询 key 规范化
                cost_guard_service.py # 限流、配额和并发保护
                metrics_service.py  # Prometheus 兼容文本指标
                observability_service.py # 搜索步骤计时、search_id 和 JSON 日志
                search_service.py   # Tavily 搜索、多模态结果标准化和排序
                security_service.py # 提示注入检测和敏感词过滤
                sse.py              # SSE 事件格式化工具
            security/
                blocked_terms.txt   # 敏感违规词词表
        tests/
            test_ai_service.py      # AI 提示词构造测试
            test_sse.py             # SSE 格式化测试
        requirements.txt
    frontend/
        src/
            App.vue                 # 搜索页面、SSE 消费和渲染逻辑
            main.ts                 # Vue 应用入口
            styles.css              # 页面样式
            types.ts                # 前端类型定义
        package.json
        vite.config.ts
```

## 核心实现逻辑

### 1. 后端搜索编排

入口位于 `backend/app/main.py`。后端提供两个主要接口：

- `POST /api/search`：非流式搜索，返回完整 JSON。
- `POST /api/search/stream`：流式搜索，通过 SSE 逐步返回结果。

流式搜索的生命周期如下：

```text
search_start -> sources -> answer_start -> token... -> answer_done -> related -> done
```

对应逻辑：

```python
yield format_sse("search_start", {"query": request.query})
results = await search_service.search(request.query)
yield format_sse("sources", {"results": [...]})

async for token in ai_service.stream_answer(request.query, results):
    yield format_sse("token", {"text": token})

yield format_sse("related", {"questions": related})
yield format_sse("done", {})
```

这样设计可以让前端先拿到来源列表，再实时展示模型生成内容，用户不必等待完整答案
生成完毕。

### 2. 搜索缓存

`backend/app/services/cache_service.py` 提供进程内 LRU 缓存。缓存 key 会先进行标准化，
并区分 `fast` / `deep` 模式：

```python
def normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())
```

当 `/api/search` 或 `/api/search/stream` 收到重复问题时，后端会优先读取缓存中的完整
`SearchResponse`。如果命中缓存，后端直接返回已生成的答案、来源和相关问题，不再调用
Tavily 或 DeepSeek。缓存默认 TTL 为 `SEARCH_CACHE_TTL_SECONDS=1800` 秒。

SSE 接口在命中缓存时会额外发送 `cache_hit` 事件，随后继续发送前端已支持的
`sources`、`answer_done`、`related` 和 `done` 事件，保持渲染流程兼容。

### 3. Tavily 搜索服务

`backend/app/services/search_service.py` 负责调用 Tavily，并把不同返回结构统一转换为
项目内部的 `SearchResult`：

```python
SearchResult(
    id=index,
    title=str(raw.get("title") or f"Source {index}"),
    url=str(raw.get("url") or ""),
    content=str(raw.get("content") or raw.get("snippet") or ""),
    score=raw.get("score"),
    published_date=raw.get("published_date"),
)
```

MVP 阶段会把 Tavily 返回的网页结果和顶层图片结果统一成 `SearchResult`。后端会根据
URL 规则识别 `text`、`image`、`video`、`file` 四类结果，并为文件补充 `file_type`，
为图片补充 `thumbnail_url`。

标准化后，后端不会直接使用 Tavily 的原始顺序，而是通过 `rank_search_results` 综合
计算排序分：

- Tavily 相关度分：保留搜索引擎对查询匹配度的判断。
- 来源权威性分：优先提升政府、教育、组织机构和文档类页面。
- 内容时效性分：优先提升近期发布的内容，缺少发布时间时给予较低默认分。
- 结果类型分：当问题明显需要图片、视频或文件时，对匹配类型进行加权。

排序完成后会重新编号 `id`，保证 AI 引用编号和前端展示顺序一致。

如果启用 `USE_MOCK_AI=true`，或没有配置 `TAVILY_API_KEY`，服务会返回固定模拟结果。
这使前端开发、接口联调和单元测试不依赖真实网络搜索。

### 4. DeepSeek 生成服务

`backend/app/services/ai_service.py` 负责构造提示词并调用 DeepSeek。答案生成系统提示词
要求模型：

- 只使用传入的网页来源作为依据。
- 使用和用户问题一致的语言回答。
- 对依赖来源的事实声明添加 `[1]` 形式的引用。
- 不编造不存在的来源编号。
- 默认先给结论，并控制回答长度，减少 AI 生成耗时。

核心提示词构造流程：

```python
def build_answer_prompt(query: str, results: list[SearchResult]) -> str:
    return (
        f"User question:\n{query}\n\n"
        f"Web sources:\n{build_source_context(results)}\n\n"
        "Write a helpful Markdown answer with inline source citations."
    )
```

模型客户端通过 `langchain-openai` 的 `ChatOpenAI` 创建，并使用 DeepSeek 的
OpenAI-compatible API：

```python
ChatOpenAI(
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_base_url,
    model=settings.deepseek_model,
    temperature=settings.deepseek_temperature,
    streaming=streaming,
)
```

为了降低 `AI 综合回答` 阶段耗时，MVP 默认启用快速回答模式：

- 只把排序靠前的有限来源传给模型，默认最多 `6` 条。
- 每条来源内容会被截断，默认最多 `900` 字符。
- Prompt 要求先给结论、减少冗余背景。
- 相关问题默认用规则模板生成，不额外调用一次大模型。

如果需要更智能但更慢的相关问题，可以设置 `AI_GENERATE_RELATED_WITH_AI=true`。
前端搜索框提供 `快速` 和 `深度` 两种模式：快速模式优先降低延迟和成本；深度模式会
放宽上下文预算，更适合复杂问题。

### 5. SSE 数据格式

`backend/app/services/sse.py` 将事件名称和 JSON 数据序列化成标准 SSE 帧：

```python
event: token
data: {"text": "..."}
```

这个格式可以被浏览器端的 `ReadableStream` 增量读取和解析。

### 6. 前端流式消费

`frontend/src/App.vue` 使用 `fetch` 调用 `/api/search/stream`，然后读取响应体：

```ts
const reader = body.getReader();
const decoder = new TextDecoder("utf-8");
let buffer = "";
```

前端按 `\n\n` 拆分 SSE 帧，解析出 `event` 和 `data` 后更新页面状态：

- `sources`：展示搜索来源。
- `token`：追加 AI 答案片段。
- `answer_done`：同步最终答案。
- `related`：展示相关追问。
- `error`：展示错误信息。

### 7. Markdown 与引用渲染

前端使用 `marked` 渲染 Markdown，并把答案中的 `[1]` 这类来源编号转换为可点击链接：

```ts
const markdown = text.replace(/\[(\d+)]/g, (raw, idText: string) => {
    const url = sourceUrls.get(Number(idText));
    return url ? `[[${idText}]](${url})` : raw;
});
```

同时通过 `highlight.js` 注册常见语言，对代码块进行高亮展示。

### 8. 搜索可观测性

`backend/app/services/observability_service.py` 为每次搜索生成唯一 `search_id`，并记录
关键步骤的开始、结束、耗时和错误信息。后端会输出 JSON 日志，便于后续接入 Loki、
ELK 或云日志平台。

流式接口会额外返回可观测事件：

- `trace_start`：一次搜索追踪开始，包含 `search_id`。
- `step_start`：某个搜索步骤开始。
- `step_done`：某个搜索步骤完成，包含 `duration_ms`。
- `step_error`：某个步骤失败，包含错误类型和错误信息。
- `trace_done`：完整搜索流程成功结束。
- `trace_error`：完整搜索流程异常结束。

当前 MVP 追踪的步骤包括缓存检查、来源搜索与排序、AI 回答生成、相关问题生成和缓存
写入。前端会在搜索结果页展示这些步骤和耗时，出现错误时可直接定位失败阶段和
`search_id`。

`GET /metrics` 会返回 Prometheus 兼容文本指标，包含搜索请求数、缓存命中/未命中、
错误计数、搜索步骤耗时和完整链路耗时，便于后续接入 Prometheus/Grafana。

### 9. 限流与配额

`backend/app/services/cost_guard_service.py` 提供进程内成本保护。MVP 阶段按客户端 IP
控制短期频率、每日外部 API 调用配额和流式搜索并发数：

- `RATE_LIMIT_PER_MINUTE`：单 IP 每分钟搜索请求上限，默认 `10`。
- `IP_DAILY_EXTERNAL_QUOTA`：单 IP 每日真实外部调用上限，默认 `50`。
- `GLOBAL_DAILY_EXTERNAL_QUOTA`：平台每日真实外部调用上限，默认 `1000`。
- `IP_CONCURRENT_STREAMS`：单 IP 最大并发流式搜索数，默认 `2`。

请求进入时会先执行频率限制和流式并发检查；缓存命中会直接返回，不消耗外部调用
配额。只有缓存未命中、即将调用 Tavily 和 DeepSeek 前，才会扣减每日外部调用配额。
当触发限制时，SSE 会返回 `error` 事件，并携带 `code`、`message`、
`retry_after_seconds` 和 `search_id`。

### 10. 安全防护

`backend/app/services/security_service.py` 提供 MVP 安全防护。当前采用轻量规则检测，
避免在主搜索链路上引入额外外部调用：

- 用户 query 会在缓存检查和外部 API 调用前进行安全检查。
- 命中提示注入风险时返回 `security_prompt_injection`。
- 命中敏感或违规词时返回 `security_blocked_terms`。
- Tavily 返回的标题和内容会在进入 AI Prompt 前被清洗。
- AI 输出会在返回前进行一次清洗。
- Prompt 明确声明搜索来源是不可信内容，模型只能把来源当作事实证据。
- 敏感违规词从 `backend/app/security/blocked_terms.txt` 加载，支持注释和空行。

被安全策略拦截时，SSE 会返回 `error` 事件，并携带 `code`、`message`、`reason`
和 `search_id`。后续可以把规则词表迁移到配置文件、数据库或专门的审核服务。

词表文件格式：

```text
# 以 # 开头的是注释
违规词示例
forbidden-test-term
```

可以通过 `SECURITY_BLOCKED_TERMS_PATH` 指定自定义词表路径。服务启动时会读取词表并
预编译匹配规则，因此新增词后需要重启后端服务。

## 环境变量

后端会通过 `python-dotenv` 读取 `.env`。可以在 `backend/.env` 中配置：

```env
APP_NAME=neko-ai-search
APP_ENV=development
FRONTEND_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

TAVILY_API_KEY=your_tavily_key
TAVILY_MAX_RESULTS=8

DEEPSEEK_API_KEY=your_deepseek_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_TEMPERATURE=0.2
DEEPSEEK_REASONING_EFFORT=
DEEPSEEK_THINKING=true

USE_MOCK_AI=false

SEARCH_CACHE_TTL_SECONDS=1800

AI_MAX_CONTEXT_SOURCES=6
AI_MAX_SOURCE_CONTENT_CHARS=900
AI_FAST_ANSWER=true
AI_GENERATE_RELATED_WITH_AI=false

RATE_LIMIT_PER_MINUTE=10
IP_DAILY_EXTERNAL_QUOTA=50
GLOBAL_DAILY_EXTERNAL_QUOTA=1000
IP_CONCURRENT_STREAMS=2

SECURITY_BLOCKED_TERMS_PATH=backend/app/security/blocked_terms.txt
```

前端可通过 `frontend/.env` 覆盖 API 地址：

```env
VITE_API_BASE_URL=http://localhost:8000
```

本地只想查看页面效果时，可以把后端的 `USE_MOCK_AI` 设置为 `true`。

## 本地运行

### 1. 启动后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

健康检查：

```bash
curl http://localhost:8000/health
```

### 2. 启动前端

```bash
cd frontend
npm install
npm run dev
```

默认前端地址：

```text
http://localhost:5173
```

## API 说明

### `GET /health`

用于检查后端服务是否正常启动。

响应示例：

```json
{
    "status": "ok",
    "service": "neko-ai-search"
}
```

### `POST /api/search`

非流式搜索接口。

请求示例：

```json
{
    "query": "DeepSeek V4 有哪些能力？",
    "mode": "fast"
}
```

响应字段：

| 字段 | 说明 |
| --- | --- |
| `query` | 用户原始问题 |
| `mode` | 搜索模式，支持 `fast` 和 `deep` |
| `answer` | AI 生成的 Markdown 答案 |
| `results` | 标准化后的搜索结果列表 |
| `related_questions` | 相关追问列表 |

### `POST /api/search/stream`

SSE 流式搜索接口。前端主要使用该接口。

事件类型：

| 事件 | 说明 |
| --- | --- |
| `trace_start` | 搜索链路追踪开始，返回 `search_id` |
| `step_start` | 某个搜索步骤开始 |
| `step_done` | 某个搜索步骤完成，返回耗时 |
| `step_error` | 某个搜索步骤失败，返回错误位置和错误信息 |
| `trace_done` | 搜索链路追踪完成 |
| `trace_error` | 搜索链路追踪失败 |
| `search_start` | 搜索开始 |
| `cache_hit` | 命中缓存，后端会直接返回已有搜索内容 |
| `sources` | 返回搜索来源列表 |
| `answer_start` | AI 开始生成 |
| `token` | AI 答案增量片段 |
| `answer_done` | AI 答案生成完成 |
| `related` | 返回相关追问 |
| `done` | 完整流程结束 |
| `error` | 发生错误 |

## 测试与构建

后端测试：

```bash
cd backend
pytest
```

前端类型检查与构建：

```bash
cd frontend
npm run build
```

## 开发说明

- 后端配置集中在 `backend/app/config.py`，默认允许 `5173` 端口的前端访问。
- 搜索结果会在进入模型前被标准化，避免前端直接依赖 Tavily 原始响应结构。
- AI 回答提示词要求模型基于来源生成内容，适合做可追溯的搜索答案。
- 前端没有使用 `EventSource`，而是用 `fetch + ReadableStream`，便于发送 `POST` 请求体。
- 搜索历史只存储在浏览器本地，不会写入后端数据库。

## 后续可扩展方向

- 增加用户账号和云端搜索历史。
- 增加来源重排、去重和可信度评分。
- 支持多模型切换，例如 DeepSeek、OpenAI、Qwen 等兼容接口。
- 为前端补充组件级测试和端到端测试。
- 增加 Dockerfile 与一键部署配置。
