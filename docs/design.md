# deepbox — 设计文档

> **一句话定位**：deepbox 是一个"agent 交换机 / 管理面"。用户把他们本地
> devbox 里的 agent CLI（Claude Code、GitHub Copilot CLI、Codex CLI …）连接到我们的
> server，然后**登录我们的平台，就能像在本地终端里一样**跟这些 agent 交互。
>
> **我们是平台，不是 AI 产品。** Server 永远不跑模型、不持有任何 API key、不安装任何
> CLI。智能与凭证 100% 留在用户的 devbox 上。我们只提供：身份、连接、频道/会话、
> 消息中继、presence、以及把用户输入和 agent 输出双向转发的"神经"。

---

## 0. 灵感来源

本设计直接借鉴姊妹项目 **deepradio** 的 **Computer Model**（`C:\Code\deepradio\docs`）。
核心思想完全一致：server 发出**不含内容的唤醒/中继信号**，真正的 agent 运行在用户机器上的
一个用户自启进程里。deepbox 把这套模型用 **Python** 重新实现，并针对
"HPC 式多 devbox / 多 agent 管理" + "把真实交互式 CLI 原样投射到 web" 做了强化。

deepradio 与 deepbox 的关键差异：

| 维度 | deepradio | deepbox |
|---|---|---|
| Agent 本体 | `link` 里的 LLM 循环 / 本地 CLI | **真实的交互式 CLI 进程**（claude/copilot/codex） |
| 中继粒度 | content-free wake + REST 拉取整条消息 | **持久 PTY 会话**，逐字节双向流式转发 |
| 用户体验目标 | 聊天室里多了个 AI 队友 | **与本地开终端用该 CLI 完全一致** |
| 技术栈 | TypeScript / Hono / SQLite | **Python / FastAPI / SQLite** |

---

## 1. 三个实体（Human / Devbox / Agent）

```
┌─────────────┐   owns   ┌──────────────────────────────┐
│   Human     │─────────▶│           Devbox             │  e.g. "Alex 的开发机 / 集群登录节点"
│ (user 登录) │          │  一台跑 connector 的机器     │
└─────────────┘          │  由一个 TOKEN 认证           │
                         └───────────────┬──────────────┘
                                         │ hosts (1:N)
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                     ▼
              ┌──────────┐        ┌──────────┐         ┌──────────┐
              │  Agent   │        │  Agent   │         │  Agent   │  每个 = 一个 agent principal
              │@claude   │        │@copilot  │         │@codex    │  绑定 devbox_id + runtime
              └──────────┘        └──────────┘         └──────────┘
```

| 实体 | 是什么 | 认证 |
|---|---|---|
| **Human** | 平台用户，浏览器登录 | session cookie（P0 用户名/密码；后续 OAuth） |
| **Devbox** | 一台跑 `connector` 的机器，认证单元，Human 拥有 | `hpc_box_<64hex>` bearer token，只存 hash |
| **Agent** | 一个可交互的 CLI 实例（runtime=claude-code/copilot-cli/codex-cli/mock），host 在某个 Devbox 上 | 继承其 Devbox 的 token，只能通过该 Devbox 说话 |

- **一个 Human 可拥有多个 Devbox**（多台开发机 / 多个集群节点）。
- **一个 Devbox 可 host 多个 Agent**（同一台机器上跑多个不同 CLI）。
- **Devbox 不是 principal**：它是基础设施，从不出现在消息作者里，只有它的 Agent 会。

---

## 2. 数据模型（SQLite / SQLAlchemy）

```
user           id, username, password_hash, display_name, created_at
devbox         id, owner_user_id, name, created_at, last_seen_at, capabilities(JSON)
token          id, devbox_id, hash, preview, created_at, last_used_at, revoked_at
agent          id, devbox_id, handle, display_name, runtime, cwd, launch_cmd,
               presence(offline|online|busy|error), created_at
session        id, user_id, agent_id, title, created_at            # 一次人-agent 的会话/频道
message        id, session_id, author_kind(user|agent|system),
               author_id, body, created_at
# 流式 CLI 输出不进 message 表逐条存；见 §5 的传输模型。
```

> 命名保持"无聊耐用"：代码里就叫 `devbox/agent/session/message`。产品/营销层面可以再包装
> HPC 术语（"接入节点 / 作业 / 会话"）。

Token 规则（照搬 deepradio，已验证过的做法）：
- 格式 `hpc_box_` + 64 hex（32 随机字节）。
- 只存 `sha256(token)`，按 hash 查。
- `preview` = `hpc_box_` + 随机部分前 6 位 + `…`，供 UI 显示"是哪一个 token"。
- 创建时**完整 token 只返回一次**，之后不可再取。
- 可轮换（发新的）、可吊销（`revoked_at`，hub 立即断开在用的连接）。

---

## 3. 连接模型（两种 WebSocket）

Server 维护一个 **Hub**，管理两类连接：

```python
Conn =
  | HumanConn   { ws, user_id }                       # 浏览器
  | DevboxConn  { ws, devbox_id, agent_ids: set,      # connector
                  outbound: Queue[dict], sender_task, retired }
```

### Human 连接（浏览器）
`GET /ws?session=<cookie>` → 校验登录 → 订阅该用户可见的 session 事件。

### Devbox 连接（connector）
```
connector ──WS upgrade, header: Authorization: Bearer hpc_box_...──▶ server
server: 校验 token
        ├─ 无效/吊销 → close(4001)
        └─ 有效 → 解析 Devbox D
                   载入 D 的 agents (agent WHERE devbox_id = D.id)
                   把所有 host 的 agent presence 置为 online
                   touch devbox.last_seen_at
                   conn = DevboxConn(..., outbound=Queue(maxsize=256))
                   注册路由；同 Devbox 的旧连接 retire + close(4002)
                   先排 hello，再从 fresh DB snapshot 排权威 agents 目录
```
断开时：仅当该连接仍是 Hub 当前映射才清路由并把 agent 置为 offline；被替换旧连接的迟到 `finally` 不会覆盖新连接状态。

所有 server → connector 帧只做非阻塞入队，由每条连接唯一的 sender task 保序写 WebSocket。单帧发送超时（5 秒）、失败或 256 帧队列溢出会 retire 并 close(1011)，所以并发增删 agent 的 HTTP 请求不会卡在慢连接网络 I/O 上。`hello {devbox_id, agent_ids, protocol_version: 3}` 在连接可接收并发目录更新前入队，严格保持首帧语义。

> **为什么用 header 而不是 `?token=`**：connector 是本地进程，能设置 WS upgrade header，
> 把密钥挡在 URL / 访问日志之外。浏览器不能设 WS header —— 没关系，人类不用 token。

---

## 4. 认证与写入规则

| 请求来源 | 判定 | 允许的作者 |
|---|---|---|
| 带 `Authorization: Bearer hpc_box_...` | 校验 → 解析 Devbox D | author 必须是 `devbox_id == D.id` 的 agent（否则 403） |
| 无 token（浏览器 session） | 视为已登录 Human | author 必须是该 Human 自己（否则 403） |

要点：**无 token 的请求不能以 agent 身份说话**；**一个 token 只能扮演它那台 Devbox
上的 agent** —— 杜绝跨 Devbox 冒充。

---

## 5. 核心架构：Structured-first，PTY fallback

### 5.1 两条本地执行路径

支持 structured 的 adapter 使用原生 chat 路径：

```text
Browser composer
  -> generic input + options
  -> Server opaque relay
  -> Connector RuntimeAdapter / StructuredAgentSession
  -> 本地 Claude Code 或 Copilot CLI
  -> canonical events
  -> Server durable relay
  -> Browser semantic reducer/render
```

其他 adapter 保留 terminal fallback：

```text
Browser xterm <-> Server byte relay/recording <-> Connector PTY <-> 本地 CLI
```

模型计算、provider 登录态和模型凭证始终留在用户机器。Server 不启动 CLI，也不解释 runtime ID、
model、effort 或 attachment；它只做身份/RBAC/keyboard lease、opaque frame 转发、可靠记录和协作广播。

### 5.2 RuntimeAdapter 与 capability facts

Connector 的 registry 是扩展边界。每个 adapter 描述：

- runtime ID/label、probe 与本地 command；
- PTY 或 structured 模式，以及 persistent/per-turn 进程策略；
- model、permission 和 CLI argv 映射；
- generic `select` / `file` controls 的 scope、choices、default 与 bounds。

Probe 后 connector 上报 display-safe capability object。Server 将其作为 opaque JSON 保存；浏览器仅根据
`features.structured` 选择 chat surface，并从 `features.controls` 生成 model/reasoning/file widgets。
connector-local executable path 不上报，浏览器也没有 runtime-ID 特判。

扩展原则仍是：**新增 runtime = 一个 connector adapter；Server 和 Browser 不改。**

### 5.3 Canonical event contract

Structured adapter 只向上游发送统一事件：

- `status`
- `session.config`
- `user.echo`
- `message.delta` / `message`
- `tool.call` / `tool.result`
- `permission.ask`
- `turn.end`
- `error`

事件只包含 UI 和恢复所需的 display-safe 字段，不包含 raw provider payload、chain-of-thought、token、
模型凭证或工作站路径。live frame 与 restore JSONL 使用同一个 reducer。

### 5.4 Frame protocol v3

| 方向 | Frame | 语义 |
|---|---|---|
| Browser -> Server -> Connector | `input {data, options, client_input_id}` | PTY bytes 或 structured turn；options 对 Server opaque |
| Browser -> Server -> Connector | `resize` / `terminate` | terminal resize 或显式结束本地 session |
| Server -> Connector | `open` | 幂等确保本地 PTY/structured process 存在 |
| Connector -> Server | `output {seq, pty_instance_id, kind, data}` | `kind` 为 `output` 或 `event`；durable commit 后 ACK |
| Server -> Browser | `restore {kind?, data}` | terminal screen bytes，或 `kind:event` 的 canonical-event JSONL |
| Server -> Browser | `output {kind, data}` | live terminal bytes 或单个 canonical event |

Structured options 和附件在 connector 按 adapter descriptor 二次验证；Server 不把 capability blob 变成
业务 schema。输出可靠性仍由 connector spool、单调 seq、Server ACK、带 `expected_seq` 的 `resend`、旧 instance 的
`fence` 和 payload hash 冲突 fail-closed 提供。

### 5.5 恢复与重连

- Terminal attach：Server 从 pyte/recording 恢复当前 screen，再接 live bytes。
- Structured attach：Browser 先由 capability 进入 chat；Server 返回最新的、最多 4 MiB 的完整 durable event JSONL
  tail，再接 live events。该 tail 是当前有界 replay window 的权威快照：browser 先 reset 再逐行 fold；单个坏行不会破坏
  后续 timeline。lazy chat mount 以 view epoch 隔离旧视图，并以 single-flight 合并 cold-start event burst。
- Connector WebSocket 使用 30s open timeout、20s ping、60s pong tolerance、5s close timeout 和
  16 MiB frame bound；异常断线后外层 loop 继续退避重连及 spool 续传。

## 6. REST 面（管理 + 运行时）

**管理（浏览器 / 登录 Human）**
| 方法 · 路径 | 作用 |
|---|---|
| `POST /api/auth/register` `{username,password}` | 注册 |
| `POST /api/auth/login` | 登录，设 session cookie |
| `POST /api/devboxes` `{name}` | 创建 Devbox + 首个 token，**完整 token 返回一次** |
| `GET /api/devboxes` | 列出我的 Devbox（含 agents、在线状态） |
| `DELETE /api/devboxes/:id` | 删除（级联 token；其 agent 一并移除） |
| `POST /api/devboxes/:id/agents` `{handle,display_name,runtime,cwd?,launch_cmd?}` | 新建一个 host 的 agent |
| `DELETE /api/agents/:id` | 删除 agent；SQLite 外键级联 session/message，浏览器 watcher 收到 exit，在线 connector 立即收到新权威目录并终止本地 PTY/structured session 并丢弃该 agent 的待发帧 |
| `POST /api/devboxes/:id/tokens` | 轮换：发新 token（返回一次） |
| `DELETE /api/tokens/:id` | 吊销 token |
| `GET /api/agents/:id/sessions` | 列出该 agent 的会话及 live/inactive/ended 状态 |
| `POST /api/agents/:id/sessions` | 开一个新会话，返回 session_id |
| `GET /api/sessions/:id/messages` | 会话历史（结构化消息） |
| `GET /api/sessions/:id/recording` | 下载 asciicast v2 DVR 录制 |

**运行时（connector / Bearer token）**
| 方法 · 路径 | 作用 |
|---|---|
| `GET /api/me` | 返回本 Devbox + 它要跑的 agent 名单（handle/runtime/cwd/launch_cmd） |
| `POST /api/devboxes/:id/runtimes` `{capabilities}` | connector 在建 WS 前上报本机可用 CLI；Fleet Add agent 下拉框只列已上报 runtime |

> **配置切分（保护凭证）**：非机密的 per-agent 配置（handle、runtime、cwd、启动命令）存在
> server，`GET /api/me` 下发。**任何 API key / 登录态** 只存在 connector 本地环境，
> server 永不经手。这就是整个设计的意义所在。

---

## 7. connector 包（`connector/`，Python）

用户自启进程。启动：
```text
python -m connector --server-url http://localhost:8077 --token hpc_box_...
# 或环境变量 DEEPBOX_SERVER_URL / DEEPBOX_TOKEN
```

启动流程：
1. `GET /api/me` 获取权威 agent 目录。
2. registry probe 本机 runtime，以上报用的 display-safe capability object `POST /runtimes`。
3. 建立带 Bearer token 的 WS，收 `hello`/权威目录。
4. `open` 时按 adapter 创建 `StructuredAgentSession` 或 `PtySession`。
5. Structured input 进入 `write_turn(data, options)`；PTY input 写 stdin。两种 output 都先进入本地
   durable spool，再等 Server commit ACK；断线后精确补发。
6. 两个 WS entry point 共用 30s open、20s ping、60s pong、5s close 与 16 MiB frame 策略。

---

## 8. web 客户端（`web/`）

单页 switchboard 提供 Fleet、session/协作、native chat 与 terminal fallback：

- capability 报 `features.structured` 时，在第一帧前进入 chat；canonical event 驱动 reducer/render；
- generic `select`/`file` descriptors 生成 model、reasoning 和附件 widgets；
- tab re-attach fold durable event JSONL，再继续 live event；
- 非 structured runtime 继续由 xterm.js 渲染原始 PTY bytes。

---

## 9. Roadmap

- **已完成骨架与可靠性**：身份/devbox/agent/session、connector hot registration、Protocol v3 durable
  spool/ACK/resend/fence、DVR/retention、workspace/RBAC/keyboard lease 与 Azure 部署。
- **当前主线**：headless structured adapters + 自有聊天 UI；补齐 capability-driven controls、附件、
  session restore 和 connector transport 稳定性。PTY/xterm 只做兼容 fallback。
- **下一阶段**：真实多机 E2E、更多 adapter、可审计的 runtime permission、长任务/通知与生产容量治理。

---

## 10. 一图记住 —— 核心回路

```text
Human input/options
  -> Server（身份、协作、opaque relay）
  -> Connector（adapter validation + local model CLI）
  -> terminal bytes 或 canonical event
  -> Connector spool
  -> Server durable commit + ACK + broadcast
  -> xterm fallback 或 native chat
```

Server 永不运行模型、读取模型 key 或解析 capability 中的 model/reasoning 业务含义。
