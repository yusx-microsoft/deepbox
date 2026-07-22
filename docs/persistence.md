# 会话持久化设计（Session Persistence）— 平台的立身之本

> 本文档专门设计 deepbox 的**会话持久化 / 重连 / 回放**能力。
>
> **为什么这一步决定成败**：用户在本地开终端用 agent，关掉窗口，一切归零。
> 如果我们的平台只是"网页版终端"，那和本地没区别。**平台的全部价值在于：
> 会话活在 devbox 上、被服务器忠实记录、随时随地无损重连、还能回放历史。**
> 这份设计就是把这句话变成机制。

---

## 1. 本地 vs 平台：我们要兑现的 5 个差异

| 能力 | 本地终端 | deepbox 平台（本设计交付） |
|---|---|---|
| **会话生命周期** | 绑定终端窗口，关了就没 | 会话活在 devbox 的 PTY 里，**独立于任何观看者** |
| **断线恢复** | 网络抖动可能丢工作 | **零丢失重连**：自动重连 + 立即还原当前画面 |
| **跨设备** | 不可能 | 关笔记本 → 手机/另一台机器打开，**同一个 live 会话** |
| **历史** | 只有内存 scrollback，关了即失 | **完整 DVR 录制**，可回放、审计、（未来）搜索 |
| **协作** | 单人 | **多观看者**同时看同一 live 会话（天然支持） |

---

## 2. 核心难点：终端是"屏幕"不是"日志"

终端输出流里混着光标移动、清屏、颜色、以及 **alt-screen**（Claude/vim 这类全屏 TUI
会切到备用屏并不断重绘）。因此：

- ❌ **不能**"从会话开始重播所有字节"——长会话里 99% 是 TUI 的中间重绘帧，又慢又大。
- ✅ 必须区分两个需求，用两种机制：
  1. **"现在屏幕长什么样"**（重连时要立刻看到）→ 用**服务端无头终端模拟器**维护当前屏幕状态，
     序列化成一小段"重绘字节"发过去。**有界**（只和屏幕尺寸有关），对 TUI 完美。
  2. **"这个会话从头到尾发生了什么"**（回放/审计）→ 用 server SQLite 中的 Protocol v3
     **durable `RecordingFrame`** 记录；API 可导出 asciicast v2，也可返回 events + checkpoints 做随机 seek。

---

## 3. 三层架构：Local Agent / Recorder / Viewer 解耦

本地 session 的活进程始终在 connector：它可以是 `PtySession`，也可以是
`StructuredAgentSession`。Server 的 `LiveSession`/durable recording 负责可靠接收、持久化和广播；
多个浏览器只负责渲染，可随时 attach/detach。

**三条铁律：**

1. **本地 agent process 是 live execution 的唯一源头**：住在 connector，不随浏览器关闭；只在
   connector/session supervisor 退出或用户显式 terminate 时结束。
2. **Server 是 opaque 持久化与广播层**：`kind: output` 的 terminal bytes 进入 pyte screen；
   `kind: event` 的 canonical JSON 保持 opaque。两者都先 durable commit 再 ACK/广播。
3. **Viewer 可随意来去**：terminal attach 收 screen restore；structured attach 收 bounded event JSONL
   restore。之后两者都继续接 live frame。

---

## 4. 生命周期语义：detach != terminate

| 动作 | 触发 | 对本地 agent process 的影响 | 用途 |
|---|---|---|---|
| **attach** | 浏览器打开会话 | 幂等确保 PTY/structured session 存在 | 开始观看 |
| **detach** | 浏览器关闭/离开 | **无**，本地 process 继续活 | 换 tab/设备或暂时离开 |
| **terminate** | 用户显式结束 | 结束 CLI process | 真正关闭会话 |
| connector/supervisor 退出 | devbox 重启等 | 托管 process 消失，会话离线/结束 | 当前进程边界 |

会话状态仍是 `starting -> live -> (detach/attach 任意多次) -> ended`。

---

## 5. Server 端：LiveSession、Recorder 与两种 restore

每个 `session_id` 对应内存 `LiveSession`，持久事实源是 SQLite `RecordingFrame`。connector frame
先按 `(session_id, pty_instance_id, seq)` durable commit，Server 才发送 ACK；相同 seq + payload hash
可安全 re-ACK，不同 payload fail closed。

- **Terminal restore**：`kind: output` 更新 pyte；attach 时 `serialize_screen()` 返回清屏、SGR 重绘和
  光标定位 bytes。Server 重启后从 durable output 重建 screen。
- **Structured restore**：`kind: event` 不进入 pyte。`LiveRegistry.event_restore()` 从 durable rows
  反向选择最新的、最多 4 MiB 的完整 JSON-object event，恢复原顺序后组成 JSONL；浏览器把该有界 replay
  window 当作权威快照，先 reset state 再逐行 fold 进 canonical reducer。坏 durable row 被隔离，不影响后续有效事件。
- **可靠续传**：connector 本地 spool 精确补发 Server 尚未 ACK 的 seq；transport 重连不结束已托管
  process。`pty_instance_id` 在 structured path 中是沿用的协议 identity，不表示存在真实 PTY。

DVR API 继续从 durable rows 导出 recording/replay；terminal checkpoint/seek 只消费 `kind: output`。
Retention/secure erase 对两种 frame payload 使用同一策略。

### 5.1 Cut 8 workspace 与协作状态

- `organization(id, name, is_personal, owner_user_id, created_at)`：workspace 的组织容器；personal organization
  通过 owner 关联用户。
- `workspace(id, org_id, name, is_personal, created_at)` 与
  `membership(id, workspace_id, user_id, role, created_at)`：Membership 对 `(workspace_id,user_id)` 唯一，角色为
  `viewer/operator/admin/owner`。Devbox 与 Session 各增加 nullable `workspace_id`；nullable 只用于无损迁移窗口，
  登录后的 workspace bootstrap 会把旧 Devbox 和其 Session 回填到 owner 的 personal workspace。
- `session_participant(id, session_id, user_id, role, joined_at, last_seen_at)`：对 `(session_id,user_id)` 唯一，
  attach 时 upsert，用于展示参与者；WebSocket 是否在线仍以进程内 Hub 为准。
- `keyboard_lease(session_id, holder_user_id, acquired_at, expires_at, version)`：每个 Session 至多一行。
  acquire/renew/release/handoff 在事务中检查 workspace role、TTL 和 version；过期行可被下一位 controller 原子接管。
  这是短期协作控制状态，不进入 recording 内容，也不改变 Server 不持有模型凭证的边界。

`_migrate()` 仅做 additive nullable columns/new tables，并调用幂等 personal workspace backfill；不重写
recording ledger 或 connector spool。SQLite 文件备份因此同时包含 workspace 元数据和 lease 状态。

---

## 6. 帧协议（browser attach + connector protocol v3）

浏览器 -> Server：
- `attach {session_id}`（原 `open`）
- `input {session_id, data, options?, client_input_id}`；`options` 对 Server opaque
- `resize {session_id, cols, rows}`
- `detach {session_id}`（原 `close`，不结束本地 process）
- `terminate {session_id}`（显式结束；要求有效 keyboard holder）
- `keyboard_acquire / keyboard_renew / keyboard_release / keyboard_handoff`

Server -> 浏览器：
- `restore {session_id, data}`：terminal screen bytes
- `restore {session_id, kind: "event", data}`：structured canonical-event JSONL tail
- `output {session_id, kind, data}`：live `output` bytes 或一个 `event`
- `status` / `exit` / `collaboration` / `keyboard_request`

Server <-> connector：
- `open`：幂等确保本地 PTY/structured session 存在
- `output {session_id, pty_instance_id, seq, kind, data}`：durable commit 后 `ack`
- gap -> `resend {expected_seq}`；旧 `pty_instance_id` -> `fence`；相同 seq/hash -> duplicate re-ACK；
  不同 payload -> fail closed
- `input {client_input_id, data, options?}`：connector 去重并返回 `input_ack`
- `resize` / `terminate`；detach 不下发给 connector

---

## 7. 浏览器端：自动重连与 surface-aware restore

- WS 断开后指数退避重连并自动 attach。
- Structured agent 在任何 output 到达前就根据 capability 进入 native chat，避免 tab 切回时停留在
  terminal 的 “resumed live session”；event restore 与 live event 走同一个 reducer。
- Terminal agent 继续使用 pyte screen restore、有限 scrollback 和 xterm live bytes。
- 打开 agent 时优先复用 connector 仍存活的最新 live session，不静默新建重复 session。
- Server 离线期间的两类 output 都由 connector spool 在重连后补发；ended session 仍可查看最终状态/DVR。

---

## 8. 有界性与成本

- **terminal restore 有界**：只和屏幕尺寸有关（~几十 KB），与会话时长无关。
- **structured restore 有界**：只返回最多 4 MiB 的完整 canonical-event JSONL tail。
- **pyte 内存有界**：只用于 terminal 的屏幕 + 有限 scrollback。
- **durable recording**：SQLite rows 随时长线性增长，且位于 ACK 路径以提供 delivery 语义；
  默认 30d retention，也可选 none/7d/permanent。清理 payload 不删除 dedup identity row。
- checkpoint interval 有界 seek 重放量；checkpoint 含完整屏幕，因此 retention 清理同步删除相关 checkpoint。
- Owner 可调用 `DELETE /api/sessions/{id}/recording` 执行 secure erase：每个 durable frame
  保留 `(session_id, pty_instance_id, seq)`、kind、原 `payload_hash` 与时间戳以维持 Protocol v3
  dedup/hash 账本，但 `data` 被固定 redaction marker 替换并写入 `redacted_at`；所有 checkpoint
  被物理删除。操作幂等，非 owner/跨租户目标返回 opaque 404。

---

## 9. 验收标准（这一步做没做好，就看这几条）

1. Structured agent 切到其他 tab 再回来 → 立即回到 native chat，durable timeline 恢复后继续 live，
   不停在 “resumed live session”。
2. Terminal agent 关闭 tab 再打开 → 立刻看到此前完整 screen，并可继续对话。
3. 拔网/刷新或 connector abnormal close → 自动重连，已 ACK output 不重复、未 ACK output 从 spool 补发。
4. 两个浏览器窗口同开一个会话 → 一个窗口输入，另一个实时看到；keyboard lease 仍只有一个 holder。
5. 结束会话后仍可读最终 recording/DVR；retention 与 secure erase 同时适用于 output/event frame。
6. 全程 Server 不运行模型、不持有 key、不解释 runtime options，只做规则、记录与广播。
