# 架构

把一池 Mistral 账号聚合成一个标准的 OpenAI 兼容端点。核心难点不是转发，而是三件事：
**上游限流的感知与规避**、**协议差异的抹平**、**并发下的资源不泄漏**。

## 分层

```
                         ┌─────────────────────────────────────┐
  OpenAI 兼容客户端  ──▶ │ api/          Web 层（FastAPI）      │
  (Cherry / ChatBox      │   chat.py         对话主路径          │
   / OpenAI SDK / …)     │   openai_api.py   models/embed/mod   │
                         │   admin.py        管理与统计          │
  浏览器（管理台）   ──▶ │   auth_routes.py  登录 / 登出         │
                         │   pages.py        页面与健康检查      │
                         │   deps.py         上下文与鉴权        │
                         └──────────────┬──────────────────────┘
                                        │
                         ┌──────────────▼──────────────────────┐
                         │ core/         领域层（无 Web 依赖）  │
                         │   openai_compat  请求归一化/错误包络 │
                         │   reasoning      思考格式转换         │
                         │   models         模型注册表与别名     │
                         │   clientkeys     下游令牌与配额       │
                         │   auth           管理密码与会话       │
                         │   pool           账号池与限流调度     │
                         │   upstream       故障转移执行器       │
                         │   store          SQLite 持久化        │
                         └──────────────┬──────────────────────┘
                                        │
                                   api.mistral.ai
```

`core/` 不 import 任何 FastAPI/Starlette 符号，所以领域逻辑可以脱离 HTTP 单测。
`api/` 只做协议适配和编排，不写业务规则。

## 请求主路径

以 `POST /v1/chat/completions` 为例：

1. **鉴权与配额**（`deps.client_auth` → `clientkeys.check`）
   校验下游密钥，检查启用状态、有效期、模型白名单、RPM、日 token 配额。
   一把密钥都没签发时放行并在启动日志里告警。
2. **模型解析**（`models.ModelRegistry.resolve`）
   把客户端给的名字（可能是自定义别名或上游别名）解析成真实模型 id，
   顺便拿到 `capabilities.reasoning`，决定能否转发 `reasoning_effort`。
3. **请求归一化**（`openai_compat.normalize_chat_request`）
   丢弃上游会 422 的标准 OpenAI 参数、改名、映射思考强度、清洗消息字段。
4. **故障转移**（`upstream.Upstream.open`）
   挑账号 → 拿并发闸门 → 发请求 → 看状态码。429/5xx 换账号重来，其他 4xx 直接回传。
5. **思考格式转换**（`reasoning`）
   非流式改写 message，流式用状态机逐条改写 delta。
6. **响应标准化**（`openai_compat.normalize_*_response`）
   去掉上游私有字段、补齐 OpenAI 期望的结构、合成 `reasoning_tokens`。
7. **记账**（`store.record`）
   非阻塞入队，后台线程批量落盘。

## 三个关键设计

### 1. 资源所有权：Lease

并发代理最容易泄漏的是「账号在途计数 / 并发信号量 / 上游连接」这三样，尤其在流式场景下
它们的生命周期比 HTTP handler 长。这里用一个显式的 `Lease` 把三者绑在一起：

```
Upstream.open() 成功  ──▶  Lease 持有 (response, account, semaphore)
                            调用方必须最终调用一次 lease.aclose()
Upstream.open() 抛异常 ──▶  三者已在内部释放干净，调用方什么都不用做
```

非流式在 `finally` 里关；流式把 Lease 交给异步生成器，由生成器的 `finally` 关，
客户端中途断连触发的 `GeneratorExit` 同样会走到那里。

### 2. 限流窗口的建模

实测结论（`core/pool.py` 顶部有完整记录）：

- 每个账号（org）独立 50 req/min，token 上限随模型不同（Mistral 系 50k，GLM 系 250k）
- 窗口固定 60 秒，429 响应通常不带 `Retry-After`
- 单个请求允许超出 token 窗口，但之后 `remaining` 会被截断为 0，该分钟内后续请求全 429

对应的调度：先 round-robin 找 `remaining_req - inflight > 0` 的账号（减去在途请求是为了
避免并发把同一个账号的配额超额认购），找不到再按剩余配额加权评分兜底。撞 429 的账号冷却到
当前窗口结束，5xx 按连续错误次数递增退避（上限 30 秒）。窗口过期时惰性恢复额度，不需要定时器。

### 3. 存储：一个连接 + 一条写队列

`record()` 在请求热路径上被调用，不能碰磁盘。做法是入内存队列后立刻返回，后台线程按最多
200 行一批 `executemany`。连接只有一条，WAL + `busy_timeout=5000`，读写都过同一把锁。
队列打满时丢弃并计数（`stats.dropped` 会显示），宁可少记一条也不阻塞代理。

## 协议兼容层：为什么需要它

下面每一条都是对上游实测出来的，不是猜的（探针输出在 `.private/probe*_out.json`）。

### 请求方向

| 客户端会发的 | 上游反应 | 本层处理 |
| --- | --- | --- |
| `logit_bias` `seed` `user` `store` | 422 `extra_forbidden` | 丢弃 |
| `logprobs` `top_logprobs` | 400 not enabled | 丢弃 |
| `max_completion_tokens` | 422 `extra_forbidden` | 改名成 `max_tokens` |
| `reasoning_effort: low/medium` | 400，只接受 `none`/`high` | 映射 |
| assistant 消息里的 `reasoning_content` | 422 `extra_forbidden` | 从消息里剥掉 |
| `role: developer` | 422 role 不认 | 降级成 `system` |
| embeddings 的 `dimensions` | 422 `extra_forbidden` | 本层在响应里截断 |
| embeddings 的 `encoding_format: base64` | 接受但忽略，仍返回 float | 本层自己编码 |

`reasoning_content` 那条最要命：DeepSeek 风格的客户端在多轮对话里会把上一轮的思考原样回传，
不清理的话第二轮必然 422。消息字段用白名单过滤，因为上游是 pydantic `extra_forbidden`，
任何没见过的键都会炸。

思考强度支持四种写法的输入，统一映射到上游的 `none`/`high`：

```
OpenAI      reasoning_effort: "minimal"|"low"|"medium"|"high"
OpenRouter  reasoning: {effort, exclude, max_tokens}
DeepSeek    thinking: {type: "enabled"|"disabled"}
Qwen        enable_thinking: bool
```

### 响应方向

上游的错误体有三种互不相同的结构，而 OpenAI 客户端只认 `{"error": {...}}`：

```
{"object":"error","message":"Invalid model: x","type":"invalid_model","code":"1500"}
{"object":"error","message":{"detail":[…pydantic…]},"type":"invalid_request_error"}
{"detail":[…pydantic…]}   或   {"detail":"Invalid API Key"}
```

`normalize_error()` 把这三种全部压成标准包络，pydantic 的校验数组会被压成一句人话并提取
出错字段填进 `param`。

其他清理：去掉流式 chunk 里上游私有的 `p` 字段；`tool_calls: null` 直接删键；有 tool_calls
时把 `content: ""` 改成 `null`；usage 去掉一堆 null 噪声字段（含上游拼错的
`prompt_token_details`）并补上 `completion_tokens_details.reasoning_tokens`。

## 思考内容的输出格式

上游实测格式（只有 `reasoning_effort=high` 时才出现，且 `mistral-small` 这类模型也会有）：

```jsonc
// 非流式
"content": [
  {"type":"thinking","thinking":[{"type":"text","text":"…"}],"closed":true},
  {"type":"text","text":"最终答案"}
]
// 流式：思考增量是 list，正文增量是 str，类型切换点就是思考结束点
delta.content = [{"type":"thinking","thinking":[{"type":"text","text":"片段"}],"closed":true}]
delta.content = "片段"
```

注意 `closed` 恒为 `true`，不能拿它当结束标记。

对外没有官方标准，现状三派：DeepSeek 系（Qwen/Zhipu/Moonshot/火山）用 `reasoning_content`，
OpenRouter 用 `reasoning` + `reasoning_details`，MiniMax 等直接在 content 里塞 `<think>`。
本层默认同时输出 `reasoning_content` 和 `reasoning`（同一个字符串，多几个字节换兼容性），
只认标签的客户端可以切 `think_tags`。

四种模式，可全局配置、也可按请求用 `X-Reasoning-Format` 头覆盖，
或用 OpenRouter 的 `reasoning: {exclude: true}` 单次关闭：

| 模式 | 输出 |
| --- | --- |
| `reasoning_content`（默认） | `content` 只留正文，思考进 `reasoning_content` + `reasoning` |
| `think_tags` | 思考内联成 `<think>…</think>` 拼在 content 前面 |
| `passthrough` | 上游 chunk 数组原样透传 |
| `strip` | 丢弃思考 |

`think_tags` 在流式下需要状态机：`<think>` 只在第一段思考前发一次，`</think>` 在增量
从 list 切回 str 时补上；如果流因为 `max_tokens` 截断而始终没切回来，`finalize()` 负责补闭合。

上游不单独上报 reasoning token 数，本层按思考/正文的字符占比从 `completion_tokens` 里切一份
填进 `completion_tokens_details.reasoning_tokens`——是估算，但比没有强。

## 配置来源

`命令行 > 环境变量 > config.toml > 默认值`。实现上让 argparse 的所有默认值都是 `None`，
这样"用户显式传了一个恰好等于默认值的参数"和"用户没传"就能区分开，配置文件才有机会生效。
配置文件用 TOML（Python 3.11+ 自带 `tomllib`，不引依赖，且支持注释），扁平结构，
键名对用户友好（`admin_password` / `db` / `keys` 等）再映射到内部字段名。

未知键会直接报错而不是静默忽略 —— 配置项写错却毫无反应是最难排查的一类问题。
分节写法（`[server]`）也会被专门识别并给出"本项目用扁平结构"的提示。

`keys_file` 是三态的：`None`（没配过，自动找默认文件）、`""`（显式关闭）、路径（用它）。

## 三套凭据

不要混淆：

- **管理密码**：登录管理台用。优先级同上，`config.toml` 里的 `admin_password` 最常用；
  都没配就在首次启动时随机生成一个，以 PBKDF2（20 万轮，随机 salt）散列存进 `meta` 表，
  明文只打印一次。库里额外记一个 `generated` 标记，还没被用户改过时界面顶部会挂提示。
  密码来自配置文件/命令行时，界面上的改密码入口会关掉并说明原因 —— 那些来源在重启时
  总会赢，允许在界面改只会造成"改了但没生效"的困惑。
- **上游账号 key**：池子里那 N 个 Mistral 账号的凭据。存在 `account_records` 表，
  API 默认只返回 `sk-abc…mnop` 形式的预览，取完整值要登录 + 显式 `?reveal=1`。
- **下游访问令牌**：签发给调用方的 `sk-pool-…`。库里只存 SHA-256，明文只在签发响应里出现
  一次。每把可独立设置 RPM、日 token 配额、模型白名单、有效期。

签发第一把令牌会自动开启 `/v1/*` 鉴权；在此之前是开放代理，启动日志会告警。

### 会话为什么用签名 Cookie 而不是会话表

登录后发一个 `payload.hmac` 形式的 Cookie，payload 只装过期时间，签名密钥是
`sha256(salt + 密码散列)`，salt 存在 `meta` 表。这样：

- 无状态，进程重启不踢人（密钥是从库里派生的，不在内存里）
- 改密码自动让所有旧 Cookie 失效，因为密钥的一半就是密码散列
- 「登出所有设备」= 轮换 salt，一行代码，不需要遍历会话表
- Cookie 是 HttpOnly + SameSite=Lax，前端 JS 拿不到，也不经过 URL

代价是没有单会话粒度的撤销。对单管理员的自建网关来说，「全部登出」已经覆盖了真实需求，
不值得为此引入一张会话表和它的清理逻辑。

管理接口同时接受 `X-Admin-Token` 请求头（值就是密码），方便脚本调用。刻意不支持 `?token=`：
查询串会进浏览器历史、`Referer` 和服务端访问日志。

## 数据表

| 表 | 用途 |
| --- | --- |
| `requests` | 每次调用一行，带 TTFT、重试次数、reasoning/cached tokens、客户端密钥 |
| `account_records` | 上游账号凭据（事实源） |
| `accounts` | 上游账号运行时状态（配额、冷却、错误计数） |
| `deleted_accounts` | 墓碑表，防止删掉的账号被 keys 文件重新导入时复活 |
| `client_keys` | 下游令牌（哈希、限额、白名单、累计用量） |
| `meta` | 模型注册表缓存、自定义别名、管理密码散列、会话 salt |

建表全部 `IF NOT EXISTS`，缺列用 `ALTER TABLE` 补，老库可以直接打开不用迁移脚本。

`requests` 表同时服务三类查询：仪表盘的聚合、日志页的分页筛选、CSV 导出。分位数用
`ORDER BY … LIMIT 1 OFFSET n` 算（SQLite 没有内置 percentile 函数），靠 `idx_req_ts` 撑。

## 前端

服务端只渲染骨架，数据全部由前端 fetch。模板在 `templates/`，脚本样式在 `static/`，
Python 里没有一行拼 HTML 或 JS 的代码。无构建步骤，也没有引任何第三方运行时。

`app.css` 是一套令牌化的设计系统：颜色/间距/圆角/字号全部走 CSS 自定义属性，
明暗两套主题只改 `<html data-theme>` 上的一个值。颜色刻意用实色而不是半透明叠加，
否则同一套边框在两种底色下会一个太重一个看不见。首屏渲染前有段内联脚本先把主题定下来，
避免暗色用户被闪一下白底。

`ui.js` 里的 `DataTable` 是四个列表页共用的：列定义 + 排序 + 分页 + 多选 + 空状态 +
加载骨架。日志页数据量无上限，走服务端分页（`server: true`，由页面驱动请求）；
渠道/令牌/模型三页数据量有界，一次拉全在前端分页，换来即时的搜索与筛选响应。
抽出这个组件之前，每个页面都在拼一遍几乎一样的 `<table>` 字符串。

表格里的按钮一律用事件委托 + `data-*` 属性绑定，不拼 `onclick="fn('<用户数据>')"` ——
那是个 XSS 入口。

前端有真实浏览器回归：Playwright 跑明暗两套主题，逐页截图、收集控制台报错、
并实际点击翻页/排序/筛选/弹窗验证交互（脚本在 `.private/browser_check.py`）。

## 测试

`python -m pytest tests -q`，320+ 个用例，上游一律用 `httpx.MockTransport`，不产生真实调用。

覆盖的重点是回归而不是行数：流式请求的账号故障转移、Lease 三件套的归还、
上游会拒绝的每一个 OpenAI 参数、错误包络的三种输入形态、思考格式四种模式的流式状态机、
usage 只出现一次、令牌配额的每条拒绝路径、登录与会话的每条失效路径（改密码 / 轮换 salt /
过期 / 篡改签名 / 开放重定向 / 暴力破解节流）、日志筛选的每个条件、并发下的 SQLite 写入、
老库 schema 升级。
