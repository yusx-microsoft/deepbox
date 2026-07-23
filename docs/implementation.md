# deepbox 实现说明（How it works）

> 本文解释 P0 骨架 + 真实 Claude CLI 接入是**如何实现**的，配合 `design.md`（设计）阅读。
> 目标读者：想读代码、想扩展或部署这套系统的人。

---

## 1. 全景：一条消息的完整旅程

Structured runtime 的主路径：

```text
Browser native chat
  -> input {data, generic options}
  -> FastAPI /ws/term（RBAC + keyboard lease + opaque relay）
  -> Connector StructuredAgentSession
  -> 本地 Claude Code/Copilot CLI
  -> canonical event
  -> connector spool + Server durable recording/ACK
  -> Browser reducer + semantic render
```

PTY runtime 的兼容路径：

```text
Browser xterm -> input bytes -> Server relay -> Connector PtySession -> 本地 CLI
Browser xterm <- output bytes <- Server relay/recording <- Connector PtySession
```

**一句话**：Server 是 runtime-agnostic durable switchboard。它不运行模型、不持有模型密钥，
也不解释 runtime/model/reasoning；它只执行身份与协作规则，并可靠转发/记录 terminal bytes 或
canonical event。智能（Claude/Copilot/Codex）100% 跑在用户机器上。

---

## 2. 服务端（`server/app/`）

### 2.1 `models.py` — 数据层
SQLAlchemy 2.0 声明式模型 + SQLite。核心身份/运行表为
`user / devbox / token / agent / session / message / bootstrap_state / invitation`；DVR 使用
`recording_frame / recording_checkpoint`；Cut 8 增加 `organization / workspace / membership /
session_participant / keyboard_lease`。
- 用 `mapped_column` 强类型；`init_db()` 建表并暴露 `SessionLocal` 工厂。
- 关系用 `cascade="all, delete-orphan"`：删 user 级联删它的 devbox/token/agent；每个 SQLite 连接都执行 `PRAGMA foreign_keys=ON`，数据库外键也会在直接删除 agent 时级联清理 session/message。
- **P1 Cut 1 附加列/表**（对既有 SQLite 库用 `_migrate()` 做**加列式**迁移，不改数据）：
  - `user.role`（默认 `member`，取值 `owner`/`member`）、`user.disabled_at`（可空）。
  - `bootstrap_state`：单例行 `id=1`，与首个 owner **同一事务**插入；主键唯一性构成
    持久、并发安全的原子闩锁——并发首启只有一方提交成功。
  - `invitation`：仅存 `token_hash`（SHA-256），带 `expires_at / redeemed_at /
    revoked_at`；兑换是单条条件 `UPDATE`，保证一次性、过期/吊销即失效。

### 2.1a P1 Cut 1 路由（onboarding）
详见 `onboarding.md`。摘要：
- `GET /api/auth/bootstrap-status` → 安全布尔；`POST /api/auth/bootstrap` → 一次性建首个 owner，
  凭据按 SHA-256 比对，任何非法/不可用一律通用 `404`，从不回显 token/hash。
- `POST/GET/DELETE /api/invitations`（owner）：铸造（有界 TTL、明文只回一次）、列出元数据、吊销。
  浏览器生成 `/#invite=...` fragment 链接（不进入 HTTP/access log），首次加载立即从地址栏移除并仅在内存保留；
  注入登录表单前做 HTML attribute escaping。
- `POST /api/auth/register` 支持 `invite_code`：原子兑换、创建 member。开发自注册仍受
  `DEEPBOX_REGISTRATION_ENABLED` 控制，生产须保持 false。
- `GET /api/users`、`POST /api/users/{id}/disable|enable`（owner）：禁用/恢复成员；禁用用户无法登录，
  现有浏览器会话与 connector bearer token 失效，活跃 WebSocket 立即关闭；绝不禁用最后一个启用的 owner（含自锁）。

### 2.2 `util.py` — 凭证与 id
- `new_id()`：`uuid4().hex`。
- `new_token()`：`hpc_box_` + 32 随机字节的 hex。返回 `(完整token, sha256, preview)`。
  **数据库只存 sha256**，完整 token 只在创建时返回一次。
- `hash_password/verify_password`：`salt$sha256(salt+pw)`（P0 够用；生产应换 bcrypt/argon2）。

### 2.3 `hub.py` — 实时路由核心
一个进程内单例 `Hub`，维护两类连接和几张路由表：
```python
DevboxConn { ws, devbox_id, agent_ids, outbound, sender_task, retired }
                                                  # 一个 connector 的 WS
HumanConn  { ws, user_id, sessions }              # 一个浏览器的 WS

devboxes:         devbox_id  -> DevboxConn
agent_to_devbox:  agent_id   -> devbox_id     # 找 agent 属于哪台 devbox
session_watchers: session_id -> {HumanConn}   # 谁在看这个会话
```
关键方法：
- `to_devbox(agent_id, frame)`：把帧非阻塞排入 host 该 agent 的 connector 的有界队列。
- `sync_agents(devbox_id, agent_ids, directory)`：在 Hub 锁内原子替换在线路由并排入权威 `agents` 目录。
- `to_session_humans(session_id, frame)`：广播给所有正在看该会话的浏览器。

每条 DevboxConn 只有一个 sender task 调 `ws.send_json()`，保持帧顺序并隔离慢连接；队列上限 256，单帧发送超时 5 秒，溢出/失败会 retire 并以 `1011` 关闭。重复 devbox 连接会先 retire 旧连接并以 `4002` 关闭；`remove_devbox(expected=...)` 防止旧 receive loop 的迟到 `finally` 误删新连接。
> dataclass 加了 `eq=False`，让连接对象按**身份**可哈希（否则含可变字段的 dataclass 不能进 set）。

### 2.4 `main.py` — FastAPI 应用
三块内容：

**(a) REST 管理面**（浏览器,cookie 认证）
`register/login/logout` → 用 `itsdangerous` 签名 cookie 存 `uid`。
`register` 受 `DEEPBOX_REGISTRATION_ENABLED` 控制：production 默认 false，
关闭时路由返回 403（fail-closed，避免公网开放注册）。
`/api/devboxes`（增删查、轮换 token）、`/api/.../agents`（增删）、
`/api/agents/{id}/sessions`（开会话）、`/api/sessions/{id}/messages`。
每个受保护路由都调 `current_user()` 校验 cookie。

**(b) REST 运行时面**（connector,Bearer token 认证）
`GET /api/me`：connector 启动时拉取"我这台 devbox 要跑哪些 agent"。
`POST /api/devboxes/{id}/agents` / `DELETE /api/agents/{id}`：云端增删 agent 后调用 `_push_agent_directory()` → `Hub.sync_agents()`。同一 devbox 的 push 由 `_agent_directory_locks` 串行化；每次用短生命周期 `SessionLocal()` 读取已提交的新快照，避免 WS 请求 session 的旧 relationship/read transaction。若 devbox **在线**，Hub 原子更新 `DevboxConn.agent_ids` + `agent_to_devbox` 路由并非阻塞下推权威 `agents` 帧（**热注册/热删除**，无需重连）；离线则下一次 `/api/me` 和 WS 重连目录兜底。删除时先向相关浏览器 watcher 下发 `exit` 并清掉 Hub/live presence，再由 supervisor 终止该 agent 的 PTY、fence durable spool 并丢弃待发控制帧；若竞态中的旧 output 已先到 server，`unknown session` 会回 `fence` 而不是形成永久重连毒帧。
`POST /api/devboxes/{id}/runtimes`：connector 上报本机探测到的可用 CLI。
用 `devbox_from_bearer()` 校验：查 token 的 sha256 → 定位 Devbox。

**(c) 两个 WebSocket**
- `/ws/devbox`（connector 用）：从 `Authorization: Bearer` header 取 token → 校验 →
  把该 devbox 所有 agent 置 `online` → 注册 `DevboxConn` 到 Hub，并把 protocol v3 `hello` 作为首帧排队 → 从新 DB session 下发权威 `agents` 快照。
  之后循环收 connector 发来的 `output/ready/exit/presence` 帧，转发给对应会话的浏览器；所有反向帧通过唯一 sender task 排队发送。
  断开时仅在 `remove_devbox(expected=conn)` 成功时把 agent 置 `offline`，避免旧连接退出覆盖同 devbox 的新连接。
- `/ws/term`（浏览器用）：从 cookie 取登录态 → 注册 `HumanConn`。
  收到 `open{session_id}` → `Hub.watch()` 订阅该会话 + 通知 connector 开 PTY；
  收到 `input/resize/close` → 补上 `agent_id` → `Hub.to_devbox()` 转给 connector。

---

## 3. connector（`connector/`）—— 本设计的灵魂

用户在自己机器上自启的进程。**智能和 API key 都在这里，server 永远看不到。**

### 3.0 P2 Cut 4：Supervisor / Transport 拆分
connector 拆成两半，二者只经 IPC 抽象（`ipc.py`）通信：
- **`supervisor.py`（sessiond）——会话所有权**：拥有全部 `PtySession` / `StructuredAgentSession` 生命周期；`detach()` 只断开 transport，绝不 kill PTY；显式 `terminate`、权威 `agents` 目录删除对应 agent 或 `shutdown()` 才结束 PTY。`agents` 帧必须是元素完整合法的列表才会原子替换目录（空列表是合法的 clear-all）；任一畸形元素使整帧保持 no-op。每次新 PTY 启动生成一个 UUID `pty_instance_id`，同一且仍存活的 local session 的幂等 `open` 复用该值；如果本地子进程已被外部终止但 reader 尚未完成清理，`open` 会剔除 stale handle 并启动新实例。旧 reader 的迟到 `exit` 以对象身份校验隔离，不能误删或关闭替代它的新 PTY。
- **`transport.py`——WebSocket 传输**：不拥有 PTY。`TransportSession` 在 IPC 与 `/ws/devbox` 之间转发帧，心跳和网络重连都不能改变 PTY 生命周期。`run()` 同时监督四个子任务，任一完成即在 `finally` 取消并 `gather(..., return_exceptions=True)` 回收全部任务，再重抛首个非取消异常，避免 `_channel_to_ws()` 等后台异常变成 “Task exception was never retrieved”。
- **IPC**：split 模式使用 Windows named pipe / POSIX `0600` Unix socket；消息为限制 1 MiB 的 newline-JSON，并有 HMAC 握手。all-in-one 模式使用相同 `Channel` 接口的 `LoopbackChannel`。

### 3.0a P2 Cut 5：Protocol v3 durable spool、server ACK 与精确 resume

Protocol v3 的输出身份是 `(session_id, pty_instance_id, seq)`：

- **先落盘再发送**：`SessionSupervisor.emit()` 对 `output` 调用 `enqueue_output()`。`connector/spool.py` 的真实运行时 `DiskSpool` 使用 stdlib `sqlite3`，启用 WAL 和 `synchronous=FULL`；`outbox` 以三元组唯一约束保存 payload，`ack_state` 保存各 PTY 实例最后连续 ACK，`input_receipts` 保存输入去重 ID。单测可注入 `InMemorySpool`。
- **序号域**：`seq` 对 `output`/`event` frame 在 `BEGIN IMMEDIATE` 事务中按 PTY 实例分配，取 `max(last_acked_seq, outbox max seq)+1`，从 1 开始且不复用。ready/presence/exit/input_ack 等控制帧只进进程内队列，既不占 durable output 序号，也不会在 sessiond 重启后陈旧重放。
- **本地有界流水线**：`drain_to()` 不再单-inflight 停等，而是维护一个按 `ord` 有序、受 `MAX_INFLIGHT_FRAMES` 帧数与 `MAX_INFLIGHT_BYTES` 字节数双重约束的 in-flight 窗口（`_inflight_ids: {delivery_id -> bytes}`）。它优先发送进程内控制帧，再按顺序发送尚未 in-flight 的 durable `ord`，在窗口未满且未到 backpressure 上限前**连续发送多帧**，无需等待前面帧的 ACK——这消除了「一帧一 Azure RTT」的吞吐上限。窗口满时停止扫描，任一 ACK/fence/新 output 都会 `set` `pending_event` 让发送环从同一队尾续发。`ord` 已在 `_inflight_ids` 中的帧永不重发；spool 仍是 durability 真源，attach 时清空 `_inflight_ids`/`_inflight_bytes`，新 transport 按序重放全部未 ACK 行。
- **精确、乱序安全的 ACK 推进**：`_apply_ack(delivery_id)` 只处理确实在 `_inflight_ids` 中的 id。控制帧按序从 `_controls` 队首 pop；durable output 交由 spool 强制 per-stream 连续性——`spool.ack(ord)` 仅当该 seq 同时是本流最小且等于 `last_acked_seq+1` 时才删行并返回 True，因此**陈旧或乱序的 ACK 绝不会删错行**（先到的中间 ACK 只留在窗口里，等队首 ACK 到达才真正推进）。释放后从窗口移除该 id 并回补字节额度、`set` `pending_event`。fence 走 `_reconcile_inflight_after_fence()`：按 fence 后的 `pending_records()` 与当前 `_controls` 重算，丢掉已被清走的 durable 或 control in-flight id，仍在队列中的控制 id 保留。
- **ACK 不是 `send()` 成功**：transport 把「发送」与「durable ACK 处理」解耦为两条独立任务。`_channel_to_ws` 对 output 帧**发送即返回**，只把身份 `(session_id, pty_instance_id, seq)→(delivery_id, frame)` 按序记入 `_outstanding`（`OrderedDict`），不阻塞、可连续发多帧。独立的 `_process_server_events` 消费 server 事件：`ack` 精确弹出对应 `_outstanding` 行并回本地 `ipc_delivery_ack`（陈旧/未知 ACK 释放不了任何行，忽略）；`resend(expected_seq)` 重发该 seq 及其后本流所有 outstanding 行（保序恢复连续尾），若请求 seq 不在 outstanding 则 fail closed；server `error` 抛 `ProtocolError`；**fence 恢复**：若 server 判定某 `(session_id, pty_instance_id)` durable 流已分叉（connector 重启 PTY 却仍在重发旧 spool 尾），它回 `{type:"fence", ...}` 而非 terminal error，`_handle_fence` 清掉该流全部 outstanding 行并把 `{type:"fence"}` 转给 supervisor（不 raise、不重连），后续更新的 local session 实例得以继续 drain。非 output 控制帧仍以 `ws.send()` 完成为本地发送边界（发送后立即回 `ipc_delivery_ack`）。
- **server 两阶段：先 fan-out 再持久化再 ACK（回显不等 fsync）**：`server/app/recording.py` 的 `RecordingStore` 把 `persist_output()` 拆成纯内存的 `classify_output()`（读 ledger 判定 NEW/DUPLICATE/GAP/CONFLICT/INVALID，NEW 时构造**未 commit** 的 `RecordingFrame`）和 durable 的 `commit_new()`（`db.add`+`db.commit`，即 ACK 边界）。`/ws/devbox` 热路径对 NEW 帧**先** `feed_live_output()` + `Hub.to_session_humans()` 把 frame 广播给浏览器，**再**用 `await asyncio.to_thread(recording_store.commit_new, ...)` 落盘，commit 成功后才回 ACK 并同样在线程池里 `maybe_checkpoint()`。这样 browser 端的按键回显只经过内存分类 + 非阻塞入队，**绝不等待 network-disk 的 fsync**；而同步 commit 移出 asyncio 事件循环后，落盘期间事件循环继续把已入队的广播真正发出、也不阻塞后续输入路由。durable ACK 仍只表示 server 已持久化（连接对同一 session 串行处理，`s` 不跨线程并发）。相同三元组+相同 payload 是幂等 duplicate（重新 ACK、不重写）；gap 返回 `resend(expected_seq)`，不推进 ACK。**分叉流走 fence 而非 error**：相同三元组但 payload 冲突（CONFLICT），或 seq 低于持久化前沿且查无此行的陈旧尾（INVALID「below persisted frontier」），都由纯函数 `recording.output_ack_response()` 映射成可恢复的 `fence`（帧已展示，`commit_new` 若丢 unique-key race 也会重分类为 DUPLICATE/CONFLICT/GAP 供 connector 恢复）；只有真正 malformed / 非本 devbox 拥有的 INVALID 才仍是 terminal `error`。server 按 devbox/agent ownership 校验，不能跨机器写历史。`Hub.to_session_humans()` 只把 frame 非阻塞地 `put_nowait` 进每个 watcher 独立的 128-frame 有界队列；per-watcher sender 保序发送，每次发送默认限时 1 秒。队列满、发送失败或超时的 stale watcher 会从全部索引移除并以 1011 关闭，因此 refresh/resume 遗留连接或慢浏览器不能阻塞其他 viewer，更不能卡住 connector 的严格 FIFO spool 或延迟 durable ACK。
- **server 侧 SQLite 调优（去掉每帧 fsync 的网络往返）**：production DB 在 Azure App Service 的 `/home`（网络盘）上，SQLite 默认 `journal_mode=DELETE`+`synchronous=FULL` 会让每次 commit 付多次 fsync＝多次网络往返。`server/app/models.py::init_db()` 注册 SQLite 连接 PRAGMA 监听器（仅 SQLite URL），设 `journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=5000`、`wal_autocheckpoint=1000`：WAL 把每帧 commit 收敛成一次顺序追加、把 sync 推迟到 checkpoint，仍然崩溃安全（WAL 下 NORMAL 只在掉电时可能丢最后几条已提交事务，而这些帧 connector 的 durable spool 会在重连时重发，故仍可恢复）。`tests/test_db_pragmas.py` 断言实际生效的 PRAGMA。

- **断线恢复**：transport 在 server ACK 前崩溃、WebSocket 在 persist 后 ACK 前断开、或整机重启，outbox 行都仍在。CLI 用 `sha256(server_url + "
" + token)[:16]` 选择用户私有 spool 路径，路径不包含 token；重连按 `ord` 重放，server 去重后精确 ACK。
- **输入幂等**：browser input 缺少 ID 时 server 生成 UUID `client_input_id`；supervisor 在写 PTY 前通过 `input_receipts` 原子登记，同一 ID 重放不会二次写入。首次和 duplicate 都返回 `input_ack(status="delivered")`。server 只在收到 delivery ACK 后把该输入写入 cast，并把 ACK 转发给 session browser。
- **可观测性**：`SessionSupervisor.status()` 返回 `pending_frames`、`pending_bytes`、各 PTY 实例 `last_acked_seq/next_seq` 以及当前 `pty_instance_id`。

`open_spool(server_url, token)` 只在真实 CLI 模式注入；普通 `Connector(...)` / `SessionSupervisor(...)` 构造默认使用内存 spool，不会在单测或库调用时创建用户文件。server 仍只持有终端记录和非 secret 元数据，绝不接触模型或本地 API key。

### 3.1 `client.py` — 主循环（组合 supervisor + transport）
1. `GET /api/me`（带 Bearer token）→ 拿到要跑的 agent 名单（runtime/cwd/launch_cmd）。
2. `probe_runtimes()`：用 `shutil.which()` 探测本机装了哪些 CLI（claude/copilot/codex），
   `POST /runtimes` 上报（让 UI 显示 capabilities）。
3. （all-in-one）每个 WS 连接新建一对 `LoopbackChannel`，`supervisor.attach()`，开 drain / control 两个 task；双进程下改由 `SupervisorService.serve()` 接受 transport 连接，`run_transport()` 连本地 sessiond。
4. 开 `/ws/devbox` WS（header 带 Bearer token），收 `hello`，交给 `TransportSession.run(ws)`。
5. server 帧经 transport→channel→`supervisor.handle_control()`；PTY 输出经
   `supervisor.emit()`→`pending`→`drain_to()`→transport→WS。
6. 断线自动重连（外层 `while True` + 3s 退避）。WS 断开时 `supervisor.detach()`，
   PTY 继续跑、output 继续进 `pending`，重连后新 transport 按序补发并 resume 同一 PTY。

`connector/runtimes.py` 是 runtime 单一事实来源。`RuntimeAdapter` 描述稳定 id、label、
`base_argv`、model flag/allowlist、permission mode argv、非机密 environment 和探测提示；
注册表内置 `mock`、`claude-code`、`copilot-cli`、`codex-cli`。`client.probe_runtimes()`
遍历注册表并把 install/version/path/features 作为 opaque capability JSON 上报，Server/Web
不解析 runtime-specific 字段。

`resolve_cmd(runtime, launch_cmd, model, permission_mode)`：显式 `launch_cmd` 仍优先，但只用
`shlex.split` 拆成 argv；否则由共享 `build_command()` 构造 argv。两条路径都会拒绝空 token、
控制字符和 shell 元字符，并以 argv 直接 spawn（不经过 shell）。未知 runtime 为兼容旧数据
回退到 `mock`。新增 runtime 只需定义并 `register()` 一个 adapter，无需修改 supervisor、Server
或 Web。

### 3.2 `pty_session.py` — 跨平台伪终端
**为什么必须用 PTY**：Claude Code/Copilot/Codex 是**交互式 TUI**，会检测"是不是真终端"
来决定渲染彩色框、光标定位、快捷键。普通管道（subprocess.PIPE）会让它们退化或拒绝运行。
PTY（伪终端）让 CLI 以为自己连着真终端,于是输出完整的原生界面。

- **Windows**：`pywinpty`（封装 ConPTY）。`PtyProcess.spawn(cmd, cwd, dimensions=(rows,cols))`。仅 connector 需要，安装自 `requirements-connector.txt`（`sys_platform=="win32"` 门控）；根 `requirements.txt` 只含 server 依赖，保持 Linux/Oryx 可装。
  用后台线程 `run_in_executor` 阻塞读，读到就 `await on_output()`。
- **POSIX**：内置 `pty.fork()` + `os.execvp`，子进程跑 CLI；父进程 `os.read(fd)` 读输出，
  `ioctl(TIOCSWINSZ)` 设尺寸。
- **初始尺寸很关键**：Claude 的 TUI 需要合理的 cols/rows 才能正确布局，所以 `PtySession`
  构造时就带默认 `120x30`,浏览器连上后再用第一个 `resize` 帧校准。

### 3.3 `mockcli.py` — 测试替身
一个假 CLI：读 stdin 行，回 `you said: ...`。让整条链路（WS 协议、Hub 路由、PTY 转发）
不依赖任何真实 agent 就能端到端测试。

---

## 4. web（`web/`）—— Native chat + Terminal fallback SPA

### 4.1 `index.html`
挂载 xterm.js（CDN）、xterm-addon-fit，含最小内联 reset。**此文件保持不变**；
`app.js` 在运行时注入 `<link rel="stylesheet" href="/static/styles.css">`（在内联
reset 之后加载，故外部主题为唯一事实来源）。

### 4.2 `app.js`
- **认证**：登录/注册/首 owner bootstrap → 后端设 cookie → `boot()` 拉 `/api/me/user`。
- **主 shell**：克制品牌 topbar（搜索/⌘K 入口、owner 入口、用户、退出）+ 左侧
  **Fleet 面板**（标题、online/total 汇总、搜索、紧凑 devbox/agent 清单）+ 右侧
  **Terminal stage**；未选 agent 时显示空状态与快捷提示。devbox/agent 状态均为
  「圆点 + 文字」。
- **Command palette**（`Ctrl/Cmd+K`）：overlay（不引入路由），筛选打开 agent、打开
  history、创建 devbox、进入 owner（仅 owner）；`↑/↓` 导航、`Enter` 执行、`Esc` 关闭。
- **模态与 Fleet 生命周期**：createDevbox / createAgent / 删除确认 / 错误提示都用 app 内自定义 modal/form 取代浏览器 `prompt/alert/confirm`。createAgent 的 runtime 是当前 devbox capabilities 经 `runtimeOptions()` 清洗、去重后的 select；没有已上报 runtime 时拒绝提交并提示先启动/reconnect connector。每个 agent 行提供 Delete，经 URL 编码的 `/api/agents/{id}` 删除后重拉 Fleet；`loadDevboxes()` 用 request generation 丢弃较旧响应，防止并发增删的过期 GET 覆盖新状态。**一次性 token 只渲染进内存中的
  modal DOM，绝不写 storage/cookie/URL/日志。** modal 提供 Copy token 和 Copy command；完整 Windows
  命令由 `web/ui.js::windowsConnectorCommand()` 纯函数生成，剪贴板 API 不可用时回退到临时 textarea。
- **双 surface**：打开 agent 时先读 opaque capability。`features.structured: true` 立即进入 native chat；
  其他 runtime 由 `setupTerm()` 建 xterm + FitAddon。两者都优先 resume connector 仍存活的 live session，
  否则 `POST .../sessions` 新建，再连接 `/ws/term`。
- **Chat**：`kind: event` 的 live/restore frame 交给 `chat.js` reducer；composer control 来自 generic
  capability descriptors。发送时把 `data/options/client_input_id` 同帧提交，附件经 FileReader 编码；
  connector 的 `session.config` 是显示值的确认事实。
- **Terminal fallback**：`term.onData` 同步直发 input，不设 batching timer；`output/restore` 写入 xterm。
  keyboard lease 控制 stdin/focus，resize 发 `resize`。WS/session 被替换时关闭旧 input sender；断线指数退避重连。
- **静态资源兼容性**：shell 与 `/static/*` 响应使用 `Cache-Control: no-cache`，每次页面加载都向 server 重新验证。`ui.js` 的 URL 带 terminal-input capability revision，且 loader 只有检测到 `createTerminalInputSender` 才接受已有的 `DeepboxUI`；因此新 `app.js` 不会再与浏览器缓存中的旧 batcher helper 混用并在连接终端时抛错。
- 所有渲染进 HTML 的服务端字符串（name/handle/runtime…）经 `esc()` 转义。server 持久化 capability blob 时保持 opaque；Web Add agent 表单读取 runtime capability facts，trim/去重并忽略畸形项。

### 4.3 `ui.js` + `ui.test.js`
DOM-free 的纯逻辑抽到 UMD 模块 `web/ui.js`：fleet 汇总、devbox/agent 过滤、command 生成/筛选、
runtime label/options、`supportsStructuredChat()`、opaque agent API path、initials、状态映射、
`escapeHtml`。`web/chat.js` 另外承载 canonical reducer、JSONL parser、generic controls/options 与
semantic render。`app.js` 用缓存 Promise 动态加载这些模块；`web/ui.test.js` 和 `web/chat.test.js`
（node:test）覆盖关键纯逻辑。

---

## 5. 真实 Claude PTY fallback 是怎么跑通的

这条历史 fallback 仍无需 Server 特殊代码。步骤是：
1. 建 agent 时 `runtime="claude-code"`,connector 的 `resolve_cmd` 把它解析成 `claude`。
2. connector 收到 `open` → `PtySession(['claude'], cwd, ...)` 起真实进程。
3. Claude 检测到 PTY → 渲染完整 TUI → 字节流经 `output` 帧 → server 透传 → xterm 渲染。
4. 用户输入 → `input` 帧 → 写进 Claude 的 stdin → Claude 正常响应。

验证：`tests_claude.py`（E2E PASS）、`snapshot.py`（用 pyte 把流还原成文本快照，
肉眼确认欢迎框 + `● Hello!` 回复都在）。

---

## 6. 端到端测试与工具

| 文件 | 作用 |
|---|---|
| `tests_e2e.py` | mock runtime 全链路（注册→建 agent→connector→WS→输入→回显）|
| `tests_claude.py` | 真实 Claude CLI 全链路 |
| `snapshot.py` | 用 pyte 终端模拟器把 PTY 流渲染成文本快照（= 浏览器所见）|
| `provision_demo.py` | **仅开发**：调用公开注册 API 建 demo/demo 账号 + Devbox + Claude agent,打印 token。非自动 seed；`DEEPBOX_ENV=production` 时拒绝运行 |
| `tests/test_persistence.py` | connector FIFO、scrollback restore、DVR 回归测试 |
| `tests/test_collaboration.py` | Cut 8 角色排序、workspace 隔离、lease 竞争/续租/释放/超时/CAS 移交 |
| `tests/test_collaboration_routes.py` | Cut 8 workspace REST 共享与 owner 不变量、Viewer opaque REST 拒绝、WS 只读拒绝 |
| `tests/test_models_migration.py` | 旧 SQLite schema 的 Cut 8 加列、建表与 personal workspace 回填 |
| `tests/test_connector_ipc.py` | **P2 Cut 4** IPC 帧编解码（仅 JSON object）+ `MAX_FRAME` 边界 + `LoopbackChannel` 有序/EOF/背压；真实本地 IPC（本机 Windows 命名管道）鉴权握手 + reconnect + 错误密钥拒绝 + POSIX 0600；自定义陈旧 Unix endpoint 清理 |
| `tests/test_connector_supervisor.py` | **P2 Cut 4** supervisor/transport 拆分：transport 重启不 kill PTY、detach 期间 output 缓冲、按序补发、close 只 kill 目标 PTY；覆盖外部终止后的 stale PTY 替换与迟到 exit 隔离；权威 agents 热添加、畸形整帧拒绝，以及删除 PTY/pty_instance/durable spool/待发控制帧清理；另含真实双进程 IPC 仿真：detach/reconnect 下 FakePty 存活、第二个 transport 被 `ipc_busy` 拒绝（用 FakePty，不起真实 agent/ConPTY）|
| `tests/test_connector_transport.py` | 四子任务 FIRST_COMPLETED 后全部 cancel/gather，后台异常被取回并重抛，不留下悬空 task |
| `tests/test_hub.py` | devbox 有界发送队列、hello 首帧顺序、重复连接 retire、权威目录路由增删、agent 删除时 watcher exit/presence 清理与离线 no-op |
| `tests/test_agent_lifecycle.py` | 同一在线 devbox 并发添加 claude-code + codex 后串行 fresh-snapshot 校准，并验证删除 agent 的 session 级联与实时 reconcile |
| `tests/test_db_pragmas.py` | SQLite 每连接 `PRAGMA foreign_keys=ON` |
| `web/ui.test.js` | runtime options 清洗/去重、opaque agent ID URL 编码及其他 DOM-free UI helpers |
| `tests/test_config.py` | production 配置和 Origin allowlist 测试 |
| `tests/test_connector_diagnostics.py` | connector URL/TLS/DNS 诊断消息测试 |

---

## 7. 远程部署配置

`server/app/config.py` 从环境变量/`.env` 加载 Server 配置。`python -m server` 根据配置启动
Uvicorn。production 模式会拒绝开发默认 secret、空 Origin allowlist 和非 Secure cookie。

推荐三机部署使用 Tailscale Serve：Uvicorn 只监听 `127.0.0.1:8077`，Tailscale 负责 Tailnet
内的 HTTPS/WSS。浏览器 `/ws/term` 校验 Origin；connector `/ws/devbox` 只接受 Authorization
header token，不接受 query-string token。详见 `remote-deployment.md`。

健康检查：

- `GET /api/health`：进程存活和协议版本。
- `GET /api/ready`：额外检查 DB 和 recording 数据目录。

---

## 7a. 最小生产运维（P1 Cut 3）

详见 [`docs/operations.md`](operations.md)。实现分布：

- `server/app/logging.py` — 结构化 JSON 日志（每行一个对象，`ts/level/logger/message` +
  `event` 字段）。`configure_logging()` 幂等安装 handler，`log_event()` 丢弃 `None`
  字段以避免泄露未设置的密钥。`main.py` 在导入时调用一次，`DEEPBOX_LOG_LEVEL` 控制级别。
- connector 心跳：`connector/client.py` 每 20s 发送 `{"type":"heartbeat"}`，服务端刷新
  `last_seen_at` 并回 `heartbeat_ack`；`connect_count` 让重连可见。服务端把
  online/offline 记为结构化事件。
- `server/app/version.py` — 版本与 Git 构建来源。`/api/version` 仅公开
  `{version, commit}`（短哈希，公开安全）；`/api/admin/version`（owner）附完整
  commit 与 `dirty`。部署产物用 `DEEPBOX_GIT_COMMIT` 注入。
- `server/app/capacity.py` — 纯函数阈值判定（数据库越大越差、磁盘剩余越小越差），
  `/api/admin/capacity`（owner）返回 ok/warn/alert。阈值经 `config.py` 校验。
- `server/ops/backup.py` — SQLite 在线备份（`integrity_check` 校验）与恢复（校验 +
  live-server 守卫，`--force` 才能覆盖运行中的库；`.pre-restore` 侧车 + `os.replace`
  原子替换）。
- `server/ops/smoke.py` — 重启后冒烟：命中 `/api/health`、`/api/ready`、`/api/version`，
  失败非零退出，可作为部署门禁。

---

## 7.1 P2 Cut 6：Replay、Checkpoint 与 Retention

- `server/app/recording.py` 以 `RecordingFrame.id` 作为跨 PTY stream 的 durable cursor；
  `durable_events()` 生成按 cursor 排序的 replay event，`maybe_checkpoint()` 周期性保存完整终端屏幕，
  `metadata()` 只返回回放所需统计，不解析 runtime/model。
- `GET /api/sessions/{id}/recording` 输出兼容 asciicast v2；`GET /api/sessions/{id}/replay`
  返回 header、events、checkpoints、duration 与 metadata，且两者都执行 session owner 隔离。
- `PATCH /api/sessions/{id}/retention` 只接受 `none/7d/30d/permanent`，策略与执行在
  `RecordingStore.set_retention()` 同一操作完成。清理时 payload 清空、`redacted_at` 置位并删除可能含
  已过期内容的 checkpoint；seq/hash ledger 不删，因此 ACK 丢失后的 identical duplicate 仍可安全 re-ACK。
  `none` 对后续新 output 也在 `persist_output()` 返回（即 server ACK）前清空持久 payload。
- `web/replay.js` 是可独立测试的纯 replay helper；`web/app.js` 动态加载它，并提供 Session 历史列表、
  播放/暂停、0.5x/1x/2x/8x、timeline seek、首尾跳转、最终屏幕、asciicast 下载与 retention selector。
  seek 恢复 `time <= target` 的最近 checkpoint，再严格应用 `cursor > checkpoint.cursor` 的 output event；
  replay mode 禁用 xterm stdin，返回 live 时重建 terminal/WS 状态。
- 回归覆盖 retention 即时执行、未来 `none` 帧、redaction 后 duplicate ACK、checkpoint 清理、
  server 字段到 browser replay 字段的归一化，以及相同时间戳下的 cursor seek。

## 7.2 Cut 8：Workspace、角色与协作

- `models.py` 用 `Organization → Workspace → Membership` 表达共享边界；Membership 角色按
  `viewer < operator < admin < owner` 排序。`Devbox.workspace_id` 和 `Session.workspace_id` 把资源固定到
  workspace；新用户首次读取 workspace 时自动得到 personal organization/workspace，旧库中的 nullable
  Devbox 会事务性回填到 owner 的 personal workspace，Session 继承其 Devbox workspace。
- `/api/workspaces` 提供 workspace 创建、成员列表/新增/改角/移除。Admin 可管理低于 owner 的角色，
  只有 owner 可授予/撤销 owner；最后一个 owner 永远不能被移除或降级。成员被改角或移除时，Server 会关闭
  该用户在该 workspace Session 上的现有 socket，使权限撤销立即生效且不影响其他 workspace。Devbox、Token、Agent、Session、
  Recording/Replay/Retention 的 REST 授权统一经 `_devbox_role()` / `_session_role()`，越权目标继续返回 opaque 404。
- `Hub.session_watchers` 保留同一 Session 的多个 browser 连接。`SessionParticipant` 保存已加入的用户和
  last-seen；server 广播 `collaboration` frame（participants + keyboard state），浏览器显示只读/可请求/持有状态。
- `KeyboardLease` 以 `session_id` 为主键，60 秒 TTL，handoff 通过 `version` 做 compare-and-swap。Operator/Admin/Owner 可
  `keyboard_acquire/renew/release`；忙时请求广播给 holder，holder 以 `keyboard_handoff {target_user_id}` 原子移交。
  lease 到期比较先把 SQLite round-trip 后的 naive UTC 与 timezone-aware 时间统一归一化，避免 terminal WebSocket
  因 `can't compare offset-naive and offset-aware datetimes` 异常断开并持续 reconnect。
  只有有效 holder 可发送 `input`/`resize`/`terminate`；每次输入自动续租，浏览器持有期间每 20 秒续租，
  holder 断开最后一个该用户连接时释放。Viewer 的控制 frame 返回 `read_only`。
- `web/collaboration.js` 是可独立测试的权限/租约 UI 状态 helper：`deriveCollaborationState()` 归一化 keyboard state，`canSendInput()` 只让当前 holder 发送输入，`collabHeaderView(state, requester?)` 是纯视图模型——**`state` 为 null（attach 尚未完成/collaboration frame 未到）时返回显式 `connecting…` pending 徽标而非空徽标**，因此终端不会出现「聚焦却神秘不可输入」的黑屏；`app.js::renderCollab()` 据此在 attach 时先渲染 pending 态。`styles.css` 由 `app.js` 动态加载，无需更改 `index.html`。权限边界、共享资源、WebSocket 只读拒绝、租约竞争/移交、pending 态和迁移均有回归测试。

---

## 8. 现在的边界

- output 由 server Protocol v3 `RecordingFrame` durable store 记录，并可导出 asciicast v2 / JSON replay；
  connector 断线时使用**内存** FIFO 作前置去抖，持久性由 **P2 Cut 5 磁盘 spool**（seq/ACK/fsync/resume）保证：
  每帧落盘后才可发送、server durable commit 后才 ACK、connector 精确 seq ACK 后才移除。
- **P2 Cut 4 已拆分** supervisor（会话所有权）/ transport（WS）：transport 重启/断开不再 kill PTY。
  拆分既可跑在**进程内** `LoopbackChannel`（`python -m connector` 默认 all-in-one），
  也可跑真实**双进程**：`--mode supervisor` 长驻拥有 PTY 并经命名管道 / Unix socket（`0600`）
  serve IPC，`--mode transport` 拥有 WS 并可独立重连（本机 proactor 命名管道 reconnect 已单测实测通过）。
  依然：默认 all-in-one 进程整体退出仍 `shutdown()` kill 其托管 PTY；真实 Windows 服务形态
  下的 sessiond 长稳 + 真实 ConPTY 长稳验证尚未执行（见下方真机验收门）。
- **P2 Cut 5 已落地** `SessionSupervisor.pending` 由持久磁盘 spool 支撑（`connector/spool.py`，见 §3.0a）：
  emit 落盘 fsync、单调 seq、精确 seq ACK 持久化后移除、重启按序重放未 ACK 帧、尾部半条截断 / 内部损坏 fail closed。

### 8a. P2 Cut 4 真机验收门（尚未执行）

以下必须在真实机器上人工验证并记录后，才能声明 Cut 4“生产就绪”：
1. Windows：真实 ConPTY（pywinpty）起真实 CLI，命名管道 `\\.\pipe\deepbox-sessiond-<user>`
   实现为独立 sessiond 进程；杀掉 transport 进程后 PTY 存活、重连补发无丢字。
2. POSIX：Unix socket（`0600`）双进程；同样验证 transport 重启 PTY 存活。
3. Windows 服务形态下 sessiond 长稳（≥数小时、多次 transport 重连）。
本轮已落地：长度受限 JSON 帧与鉴权握手、真实本地 IPC（本机 Windows 命名管道 reconnect 单测）、
双进程拆分的 CLI 模式，以及“分离 transport detach/reconnect 下 FakePty 存活”仿真测试。
但上述 1、2、3 需要真实 ConPTY / agent / Windows 服务，仍需用户在真机人工验证；
在此之前，代码中的真实 ConPTY / Windows 服务路径不得当作已验证。

- 密码 hash 仍是 salted SHA-256，应在公开部署前升级 Argon2id。
- 单进程内存 Hub/LiveRegistry 不支持多实例横向扩展。
- Cut 8 已支持 Workspace、四级角色、多 Viewer 与单 holder keyboard lease；当前 Hub/lease 仍是单 Server
  实例语义，跨实例路由与共享 lease backend 属于 Cut 9。
- 浏览器把 xterm 输入按 24 ms 空闲窗口、80 ms 最大等待合并后再发往 WSS；Server 每帧仍会读取并校验
  60 秒 lease，但不再在输入热路径续租和提交 SQLite。持有者由独立的 20 秒 heartbeat 续租，失效或他人
  持有的 lease 仍会在任何输入转发前 fail closed。
- 应用本身不终止 TLS；Private Alpha 由 Tailscale Serve 提供 Tailnet 内 HTTPS/WSS，不能使用
  Funnel 或直接暴露 Uvicorn 到公网。

后续顺序见 `planning.md`。


## 9. 结构化 Agent 与原生聊天界面（Cut 10-11，2026-07-22）

### 9.1 方向与边界

对支持 headless/JSON 输出的 runtime，主路径不再是浏览器复刻全屏 TUI，而是 connector 在用户机器上
运行 CLI、翻译为统一事件，浏览器渲染自己的聊天界面。PTY/xterm 继续作为不支持 structured runtime
的兼容回退。Server 只转发、持久化 opaque frame；不运行模型、不读取模型凭证，也不解释 runtime、
model 或 reasoning 值。

### 9.2 Connector 事件协议与进程模型

- `connector/agent_session.py` 定义 display-safe canonical events：`status`、`session.config`、
  `user.echo`、`message.delta`、`message`、`tool.call`、`tool.result`、`permission.ask`、
  `turn.end`、`error`。
- Claude Code adapter 把 stream-json 翻译为上述事件，并在一个 live session 中保留长生命周期进程；
  Copilot CLI adapter 读取 JSONL，每个 turn 启动一个进程。
- `connector/supervisor.py` 对 structured session 调用 `write_turn(text, options)`，对 PTY session
  继续调用 `write(data)`；两条路径共用 session/open/terminate 与可靠 output transport。
- Structured output 使用 `kind: "event"`，仍带 `(session_id, pty_instance_id, seq)`，先进入 connector
  本地 spool，再由 Server durable commit/ACK。这里的 `pty_instance_id` 是协议兼容字段，不表示一定有 PTY。

### 9.3 Runtime family、surface negotiation 与 composer controls

- `connector/runtime_probe.py` 输出 capability schema v2。每个 family item 包含 `runtime`、`label`、
  `installation`、`compatibility`、`authentication`、`models`、`surfaces` 与排除 `probed_at`
  时间戳的稳定内容哈希 `revision`；未安装的 runtime 也会上报，以便 UI 给出 adapter 声明的安装命令/文档。
  Server 不读取 runtime/model 字符串，
  只把整个数组作为 opaque JSON 保存；executable path、probe 原始输出和 CLI 凭据都不会上传。
- 一个 family 可以注册多个 `RuntimeAdapter` surface。`surfaces[]` descriptor 携带内部 adapter ID、
  `terminal` / `structured` 名称、default 位与 generic features/controls。新 runtime family 仍只需 registry
  entry/adapter，不要求 Server 或 browser 添加 runtime-specific 分支。
- Browser 打开 agent 时按 family capability 选择 default surface（Claude/Copilot 当前为 `structured`），
  attach frame 显式发送 `surface`。Connector 用 `session.ready.surface` 确认；找不到或无法启动时返回
  `runtime.unavailable`（含 installation/compatibility/authentication 与 available surfaces），绝不静默
  回退到 terminal。
- Probe 可运行 adapter 声明的安全模型枚举 argv/parser；发现结果同时更新 family catalogue 与各 surface 的
  model choices。若 CLI 没有稳定枚举接口，`models.source` 为 `catalogue` 且 `complete:false`。浏览器把发现/目录
  模型渲染为建议，同时允许自定义 model；Connector 最终拒绝控制字符、shell metacharacter 和不允许 custom
  model 的 adapter 值。只有 adapter 提供可靠的非交互 status argv 时，authentication probe 才会参与 spawn gate；
  Copilot 仅提供交互式 `/login`，因此上报 `unknown` 而不是制造阻断启动的 false negative。
- Claude structured 暴露 model、effort 和 file controls；Copilot structured 暴露 model、reasoning
  effort（`low|medium|high|xhigh|max`）和 attachment controls。当前 generic control kind 只有 `select` 与 `file`；
  descriptor 携带 key、
  label、scope、choices/default 或文件数量/总字节上限，浏览器不按 runtime ID 分支。
- Connector 按 adapter allowlist 验证每个 option；session-scope select 在首个 turn 后锁定，per-turn
  select 每轮应用。agent `runtime_config.permission_mode` 同样先按 adapter allowlist 清洗，再进入每轮实际 argv。
  `session.config` 只回显 connector 已确认的 scalar 值，浏览器据此校正控件状态。

### 9.4 文件输入

- 浏览器依据 file descriptor 生成隐藏 file input、附件 chips 和错误提示，并在发送前检查数量与总大小。
- 文件通过 `FileReader` 转为 base64；Claude adapter 上限为最多 4 个、合计 1 MiB，Copilot adapter 为最多 4 个、合计 4 MiB。
- Connector 再次验证 count、声明/解码大小、base64、文件名与 adapter mode。Claude 只接受可解码的
  UTF-8 文本并嵌入 prompt；Copilot 写入 connector-owned 临时目录，以 `--attachment` 传给本地 CLI。per-turn
  child 完全 reap 且 stderr reader 退出后才删除目录；Windows 短暂文件锁会有界重试，不会让 turn task 异常退出。
- `user.echo` 和 durable history 只保留文件名/type/size，不保留 base64 正文或临时路径。

### 9.5 Browser chat、tab re-attach 与 restore

- `web/ui.js` 先用 family ID（并兼容旧 adapter ID）定位 capability，再由 default surface 决定 chat 或
  terminal；`web/chat.js` 独立负责事件解析、reducer、generic control normalization/options 和 semantic
  HTML；`web/app.js` 负责 DOM/WS/FileReader 接线。
- 打开 structured agent 时，在第一帧到达前就进入 chat surface，避免短暂落入 xterm 后停在
  “resumed live session”。lazy mount 使用 per-view epoch 丢弃旧 agent/view 的延迟结果，并用 single-flight 合并 cold-start
  event burst。live `event` 与 restore `event` 走同一个 reducer。
- `LiveRegistry.event_restore()` 从 durable recording 反向截取最新的、最多 4 MiB 的完整 `kind: event` 行，
  验证每行是 JSON object 后按原顺序组成 JSONL。浏览器把该有界 replay window 当作权威快照，先 reset state
  再逐行独立解析/fold；坏行不会吞掉后面的有效事件。
- reducer 合并 `session.config`、`user.echo`、assistant message、tool、permission、turn 和 error 状态；
  optimistic user turn 在收到 canonical `user.echo` 时不会重复显示。同一 `tool_id` 的完整 tool snapshot 更新已有
  streaming card；connector 对同一 turn 的重复 `turn.end` 去重，并在 Claude partial delta 后抑制重复完整文本，
  因此 live 与 restore 都不会出现重复气泡/工具卡。非 JSON native stdout 只产生通用 `error`，原文不会进入 relay/recording。

### 9.6 LocalProject 持久化与隐私边界

- `connector/local_store.py` 的 `LocalProjectStore` 使用 `~/.deepbox/state.db`，表为
  `local_project(id, name, path, created_at, updated_at)`。SQLite 开启 WAL、`synchronous=NORMAL`、
  `busy_timeout` 和 `foreign_keys`；跨进程 mutation 用相邻 `.lock` 文件串行化。新目录/数据库分别尝试
  `0700`/`0600` 权限。`add()` 只接受已存在的目录、存为绝对路径，展开 `~` 但不展开路径中合法的环境变量语法；
  canonical path 重复添加会复用原 ID，显式名称只更新 metadata。
- `python -m connector project add|remove|list|sync` 与常驻 connector 共用该 store。每次连接和 mutation 后，
  `Connector.report_projects()` 向 `/api/devboxes/{id}/projects` 只发送 `public_projects()` 的
  `{id,name}`（以及 legacy migration 的 `{agent_id,local_project_id}`）；绝不发送 `path`。
- Server 的 `DevboxProject` 只保存 path-free metadata。Agent 通过 `local_project_id` 外键引用 project，删除
  project 时外键置空；authoritative report 也会清理已消失 project 和悬空引用，再推送新的 agent directory。
  创建 agent 时 Server 校验 project 属于同一 devbox，`runtime_config` 必须是对象且不超过 16 KiB。
- `resolve_agents()` 在 connector 本机把 `local_project_id` 解析成 `cwd`。缺失 ID/目录产生 `project_error`，
  supervisor 的 attach 返回结构化 `runtime.unavailable(code=project_unavailable)`。旧 directory 中的 `cwd`
  会被导入为 LocalProject；首次成功 report 提交 migration 并清空 Server 上所有 legacy absolute cwd。

### 9.7 Connector WebSocket 稳定性

`Connector.run()` 与 `run_transport()` 统一使用：`open_timeout=30s`、`ping_interval=20s`、
`ping_timeout=60s`、`close_timeout=5s`、`max_size=16 MiB`。这降低短时调度/SNAT 抖动造成的误断，
但不会掩盖断线；外层 connector loop 仍在 abnormal close 后退避重连并从 durable spool 续传。
生产 B1 App Service 的 SNAT 上限仍是平台容量风险，参数硬化不是扩容的替代品。

### 9.8 当前限制与验证

- Claude structured 默认使用 `--permission-mode acceptEdits`，其他声明模式映射到对应 `--permission-mode`，仅
  `bypassPermissions` 使用 `--dangerously-skip-permissions`；Copilot structured 使用 `--allow-all-tools`。workspace
  role/keyboard lease 仍限制谁能提交输入，但这两个 adapter 当前不提供逐 tool 的 runtime approval。
- Copilot 每 turn 新进程，因此不保留跨 turn context；Claude 的 context 跟随 live structured process。
- Canonical event 不转发 raw provider payload、chain-of-thought、connector token、模型凭证或工作站路径。
- `tests/test_connector_runtimes.py`、`tests/test_copilot_session.py`、
  `tests/test_connector_transport.py`、`tests/test_server_recording.py` 覆盖 adapter/options/附件/restore/WS；
  `web/chat.test.js` 与 `web/ui.test.js` 覆盖 JSONL 容错、reducer、controls、render 与 surface selection。
