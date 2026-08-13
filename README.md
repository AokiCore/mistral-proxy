# Mistral Pool 2API

把多个 Mistral 账号聚合成一个标准的 OpenAI 兼容端点：自动感知每个账号的分钟级限流窗口、
负载均衡、429/5xx 自动换号重试，并抹平 Mistral 与 OpenAI 之间的协议差异。

任意 OpenAI 兼容客户端（Cherry Studio、ChatBox、OpenAI SDK、LangChain…）把 base_url
指过来即可。

## 快速开始

```bash
pip install -r requirements.txt
python app.py --init-config     # 生成 config.toml，改里面的 admin_password
python app.py
```

打开 <http://127.0.0.1:8787/> 登录，在「访问令牌」页签发一把令牌，然后：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-pool-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"mistral-small-latest","messages":[{"role":"user","content":"hi"}]}'
```

在签发第一把令牌之前 `/v1/*` 是开放的，任何能连到端口的人都能用你的账号池。

## 配置文件

`config.toml`（跟 `app.py` 同目录，自动加载；`--config` 指定别的路径，`--no-config` 忽略）。
用 `--init-config` 生成带注释的模板。所有配置项都是扁平的，跟命令行参数一一对应：

```toml
host = "127.0.0.1"
port = 8787

admin_password = "你自己定的密码"   # 留空 = 首次启动随机生成一个并打印在控制台
# auth_enabled = false             # 彻底关掉管理台登录，仅限本机调试

api_key = ""                       # 固定的下游密钥；留空则在管理台签发可撤销的令牌

max_concurrency = 32
max_retry_accounts = 4
reasoning_format = "reasoning_content"

db = "proxy_usage.db"
keys_file = "mistral_keys.json"    # 设为 "" 表示只用数据库里的账号
connect_timeout = 30
read_timeout = 900
```

优先级是 **命令行参数 > 环境变量 > 配置文件 > 默认值**。写错的配置项会在启动时报错并提示，
不会静默忽略。文件含明文密码，`.gitignore` 已排除。

### 管理密码的三种设法

| 方式 | 适合 | 能否在界面里改 |
| --- | --- | --- |
| `config.toml` 里写 `admin_password` | 想自己定、且固定下来 | 否（改文件后重启） |
| 留空自动生成 → 管理台「设置」页改 | 想在界面里管理 | 是 |
| `python app.py --set-password` | 不想开界面、也不想把密码留在文件里 | 是 |

自动生成的密码只在控制台打印一次，库里存 PBKDF2（20 万轮）散列。只要还在用它，
管理台顶部会挂一条提示，改掉后自动消失。命令行 `--admin-token`（等价 `--admin-password`）
和环境变量 `MISTRAL_POOL_ADMIN_TOKEN` 的优先级都高于配置文件。

## 端点

| 端点 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` | 对话，支持流式、工具调用、多模态、思考内容 |
| `POST /v1/embeddings` | 向量化，支持 `encoding_format=base64` 与 `dimensions` |
| `POST /v1/moderations` | 内容审核，补齐 OpenAI 的 `flagged` 与分类别名 |
| `GET /v1/models` `GET /v1/models/{id}` | 模型清单与详情，含能力位与自定义别名 |
| `GET /health` | 健康检查（未登录时只返回存活状态） |

## 管理台

侧边栏六个页面，全部需要登录：

| 页面 | 内容 |
| --- | --- |
| `/` 仪表盘 | 吞吐趋势图、延迟分位数（P50/P90/P95/P99）、流式首字节 TTFT、状态码与端点分布、按模型/按令牌的用量、思考 token 占比、缓存命中率、渠道实时状态 |
| `/logs` 调用日志 | 全量调用记录，可按时间窗口、状态、端点、模型、令牌、流式、渠道邮箱、错误关键字筛选，服务端分页，点行看详情 |
| `/channels` 上游渠道 | Mistral 账号池：导入/添加/启停/删除、配额余量条、按状态筛选。删除写墓碑，重启不复活 |
| `/tokens` 访问令牌 | 签发下游令牌，独立设置速率上限、日 token 配额、模型白名单、有效期 |
| `/models` 模型 | 上游模型清单与能力位（推理/视觉/工具/OCR/音频），自定义别名管理 |
| `/settings` 设置 | 热改思考格式与重试次数、修改管理密码、登出所有设备、清理历史日志、接入信息 |

所有列表都支持点表头排序、翻页和调整每页条数；日志走服务端分页，其余三个页面数据量有限，
在前端分页以获得即时的搜索与筛选响应。右下角可切换明暗主题，选择记在 localStorage。

### 导入账号

渠道页的「导入账号」直接吃 `mistral_register.py` 的产物：选（或拖入）`mistral_keys.json`
和 `mistral_keys.csv` 都行，可以一次选多个，导入前会显示每个文件解析出多少个账号。
`mistral_register.py` 跑完之后在这里导一下即可，不用重启网关。

文件是在浏览器里读取后把内容上传的，服务端不接受本地路径 —— 那等于开了个任意文件读取。
导入结果会区分「新增 / 更新 / 被墓碑挡住 / 无 api_key 已跳过」，删过的邮箱要先在弹窗里恢复。

### 登录

管理台用密码登录，登录态存在 HttpOnly + SameSite=Lax 的签名 Cookie 里，URL 里不出现任何凭据。
签名密钥由密码散列与服务端 salt 派生，所以改密码或在设置页点「登出所有设备」会让所有 Cookie
立刻失效；进程重启则不会踢人。登录接口对同一 IP 有 15 分钟 10 次失败的节流。

脚本调用管理 API 可以直接带 `X-Admin-Token: 你的密码` 请求头，不需要先登录。

## 思考内容（reasoning）

Mistral 的思考块只在 `reasoning_effort` 生效时出现，且格式是私有的 chunk 数组。
本网关默认转成客户端支持最广的 `reasoning_content`（同时输出 `reasoning` 别名兼容 OpenRouter 系）。

四种模式，用 `--reasoning-format` 设全局默认，或按请求用 `X-Reasoning-Format` 头覆盖：

| 模式 | 效果 |
| --- | --- |
| `reasoning_content`（默认） | `content` 只留正文，思考放进 `reasoning_content` 与 `reasoning` |
| `think_tags` | 思考内联成 `<think>…</think>` 拼在正文前 |
| `passthrough` | 上游原始 chunk 数组原样透传 |
| `strip` | 丢弃思考 |

思考强度的四种客户端写法都能识别，统一映射到上游支持的取值：

```jsonc
{"reasoning_effort": "high"}            // OpenAI
{"reasoning": {"effort": "high"}}       // OpenRouter，支持 exclude
{"thinking": {"type": "enabled"}}       // DeepSeek / Anthropic 风格
{"enable_thinking": true}               // Qwen
```

`usage.completion_tokens_details.reasoning_tokens` 会按字符占比估算后补上（上游不单独上报）。

## 命令行参数

命令行用于临时覆盖，持久化配置建议写在 `config.toml`。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--config` | `config.toml` | 配置文件路径 |
| `--init-config` | — | 生成带注释的配置模板后退出 |
| `--no-config` | 关 | 忽略配置文件 |
| `--set-password` | — | 交互式设置管理密码后退出 |
| `--host` / `--port` | `127.0.0.1` / `8787` | 监听地址 |
| `--admin-token` / `--admin-password` | 空 | 固定管理密码，也可用 `MISTRAL_POOL_ADMIN_TOKEN` |
| `--no-auth` | 关 | 完全关闭管理台登录（仅限本机调试） |
| `--api-key` | 空 | 固定的调用方密钥，也可用 `MISTRAL_POOL_API_KEY`；更推荐在管理台签发可撤销的令牌 |
| `--keys` | `mistral_keys.json` | 启动时导入的上游账号文件 |
| `--no-keys` | 关 | 只用数据库里的账号，不读文件 |
| `--db` | `proxy_usage.db` | SQLite 路径 |
| `--max-concurrency` | 32 | 同时发往上游的请求数上限 |
| `--max-retry-accounts` | 4 | 遇到 429/5xx 时最多换几个账号 |
| `--reasoning-format` | `reasoning_content` | 思考输出格式 |
| `--allow-insecure` | 关 | 允许绑定公网地址的同时用 `--no-auth` |

管理台默认强制登录，所以绑公网地址不需要额外开关；但 `--no-auth` + 非回环地址会被拒绝启动，
除非再显式加 `--allow-insecure`。

## 安全须知

- `mistral_keys.json` / `mistral_keys.csv` 含明文密码与 API key，`.gitignore` 已排除。
- 管理密码以 PBKDF2（20 万轮）散列存库，明文只在生成那一次打印到控制台。
- 上游账号 key 默认不出现在任何响应里，只给预览；完整值需登录 + `?reveal=1`。
- 下游访问令牌只在签发时返回一次明文，库里存 SHA-256。
- 不支持 `?token=` 传凭据 —— 查询串会进浏览器历史、Referer 和访问日志。
- 未登录时 `/health` 只返回 `{"status":"ok"}`，不暴露账号池规模。

## 限流行为

实测：每账号独立 50 req/min，token 上限随模型不同（Mistral 系 50k/min，GLM 系 250k/min），
固定 60 秒窗口，429 不带 `Retry-After`。调度先 round-robin 找配额充足的账号（扣除在途请求），
找不到再按剩余配额加权评分兜底；撞 429 的账号冷却到窗口结束，5xx 按连续错误递增退避。

## 项目结构

```
app.py                装配与启动入口
core/                 领域层（不依赖 Web 框架）
  config.py           配置与启动期安全校验
  auth.py             管理密码与签名会话 Cookie
  openai_compat.py    请求归一化 / 错误包络标准化
  reasoning.py        思考格式转换（含流式状态机）
  models.py           模型注册表与别名
  clientkeys.py       下游令牌、配额、白名单
  pool.py             账号池与限流调度
  upstream.py         故障转移执行器
  store.py            SQLite（WAL + 后台批量写）
api/                  Web 层
  chat.py             /v1/chat/completions
  openai_api.py       models / embeddings / moderations
  admin.py            管理 API
  auth_routes.py      登录 / 登出
  pages.py            页面与健康检查
templates/ static/    页面与前端资源
tests/                pytest，320+ 个用例
mistral_register.py   批量注册账号并签出 key
test_key.py           探测 key 可用性与剩余额度
```

设计取舍与实测依据见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 开发

```bash
python -m pytest tests -q
```

上游用 `httpx.MockTransport` 模拟，不产生真实 API 调用。
