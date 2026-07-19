# deepbox 产品设计

> 状态：Draft v2 — 已落地首个 UI foundation（Terminal-first Switchboard），
> 见 §7.5。
>
> deepbox 是一个面向 AI coding agent 的**持久会话控制平面**。用户把自己 Devbox 上的
> Claude Code、Codex CLI、GitHub Copilot CLI 等 agent 连接到平台，然后可以从浏览器查看、
> 操作、恢复和回放这些会话。

---

## 1. 产品定义

### 1.1 一句话定位

> 在任何设备上，安全地继续运行在自己 Devbox 上的 AI coding agent，会话不会因为浏览器
> 关闭或 Server 重启而消失。

### 1.2 我们提供什么

- 一个 Server 控制面：身份、Devbox、Agent、Session、presence、路由、录制和权限。
- 一个用户自启的 connector：连接本地 CLI/PTY 与 Server。
- 一个 Web 工作台：查看 Agent、恢复 Session、操作真实 TUI、回放历史。

### 1.3 我们不提供什么

- Server 不运行模型。
- Server 不持有 Claude/OpenAI/GitHub API key。
- Server 不代替用户安装或登录 Agent CLI。
- Server 不读取本地文件系统；只有用户 Devbox 上的 Agent 能访问其工作目录。
- 第一阶段不试图做完整 Cloud IDE、代码编辑器或 SSH 替代品。

### 1.4 核心原则

> **Server 是平台，不是 AI 产品。智能、凭证和工作区全部留在用户 Devbox 上。**

---

## 2. 为什么不是普通 Web Terminal

远程终端本身是成熟能力。deepbox 的价值不能停留在“浏览器里显示一个 shell”。

| 普通远程终端 | deepbox |
|---|---|
| 管理连接 | 管理 Agent Session 生命周期 |
| 关闭窗口后依赖 shell/tmux | Session 默认独立于 Viewer 存活 |
| 不理解 Agent 的存在 | 显式管理 Runtime、Agent、状态和能力 |
| scrollback 通常是本地临时状态 | Server 保存屏幕状态与 DVR |
| 单用户终端 | 可扩展到多 Viewer、控制权和审计 |
| 无任务语义 | 后续提供 waiting/completed/approval/job 语义 |

因此底层是 PTY，但产品核心对象是 **Session**。

---

## 3. 目标用户

### 3.1 首要用户

- 同时使用多台开发机、工作站或集群登录节点的开发者。
- 长时间运行 Claude Code/Codex 等 Agent 的开发者。
- 需要离开电脑后继续查看 Agent 进度的人。
- 在 Windows、Linux、远程 GPU/HPC 节点之间切换工作的用户。

### 3.2 后续用户

- 管理多台 Devbox 的小团队。
- 需要 Agent 工作记录、审计或可复现过程的组织。
- 需要把 Agent 长任务提升为 Job 的工程团队。

### 3.3 Jobs To Be Done

1. 当 Agent 在办公室 Devbox 上运行时，我想从另一台电脑查看并继续操作。
2. 当浏览器关闭或网络抖动时，我不想丢掉 Agent 会话和输出。
3. 当我有多个 Agent/Devbox 时，我想知道哪个在线、哪个正在工作、哪个等待输入。
4. 当任务结束后，我想回放 Agent 做了什么，而不是只看到最终结果。
5. 当团队协作时，我想让其他人查看会话，但不能让多人同时无序输入。

---

## 4. 产品对象模型

```text
User / Organization
  └── Workspace
       ├── Membership / Role
       └── Devbox
            ├── Connector
            └── Agent
                 └── Session
                      ├── Live terminal
                      ├── Current screen + scrollback
                      ├── DVR recording
                      ├── Status / presence
                      ├── Events / approvals
                      └── Artifacts
```

P0 当前实现了简化版：

```text
User → Devbox → Agent → Session
```

### 4.1 User

登录平台的人类用户。当前使用 username/password + signed cookie；生产版将支持 Organization、
Workspace 和更严格的认证。

### 4.2 Devbox

运行 connector 的用户机器，是基础设施和认证单元：

- 一名用户可以拥有多台 Devbox。
- 一台 Devbox 可以承载多个 Agent。
- Devbox 使用 `hpc_box_...` bearer token 认证。
- Devbox 不作为消息作者出现，也不拥有模型凭证的 Server 副本。

### 4.3 Agent

一个可启动的本地 Agent 配置：

```text
handle
runtime
working directory
launch command
capabilities
host devbox
```

Agent 是模板/入口，Session 才是实际运行实例。

### 4.4 Session

Session 是产品的一等对象，代表“一次持续存在的 Agent 工作上下文”。

它必须拥有：

```text
id
agent_id
owner/workspace
state
started_at
last_activity_at
ended_at
exit_code
terminal dimensions
recording metadata
connector instance
PTY instance
```

Session 不应因为 Viewer 离开而结束。

### 4.5 Viewer

正在观看 Session 的浏览器连接：

- Viewer 可以随时 attach/detach。
- Viewer 不是 Session 的生命周期拥有者。
- 多 Viewer 可以同时观看。
- 后续只有持有 keyboard control 的 Viewer 可以输入。

---

## 5. Session 生命周期

### 5.1 状态机目标

```text
created
  → starting
  → live
  ↔ disconnected
  → terminating
  → ended

starting/live/disconnected
  → failed
```

状态定义：

| 状态 | 含义 |
|---|---|
| `created` | DB 已创建，尚未要求 connector 启动 |
| `starting` | Server 已发 open，等待 PTY ready |
| `live` | PTY 存活，connector 可达 |
| `disconnected` | PTY 可能仍活着，但 connector/Server 暂时失联 |
| `terminating` | 用户显式要求结束，等待进程退出 |
| `ended` | 进程已退出，有明确 exit code 或正常终止 |
| `failed` | 启动失败或状态不可恢复 |

### 5.2 生命周期语义

| 用户动作 | 语义 |
|---|---|
| New Session | 显式创建新的 PTY/Agent 上下文 |
| Attach/Resume | 打开现有 Session，不创建新进程 |
| Detach | Viewer 离开；PTY 继续运行 |
| Terminate | 显式结束 PTY，Session 转 ended |
| Archive | 从默认列表隐藏，但不删除 recording |
| Delete | 删除元数据和 recording，需要二次确认 |

### 5.3 必须避免的行为

- 点击 Agent 时静默创建 Session。
- Viewer WS 断开时杀掉 PTY。
- ended Session 被重新 attach 时悄悄启动一个新的 Agent。
- connector 未确认存活时把 Session 永久标成 ended。
- 多个 Viewer 同时向同一 PTY 无序输入。

---

## 6. 核心用户流程

### 6.1 首次接入 Devbox

```text
注册/登录
→ 创建 Devbox
→ 平台显示一次性 token 和安装命令
→ 用户在自己的机器运行 connector
→ connector 探测 runtime/version/capabilities
→ Devbox 和 Agent 显示 online
```

原则：平台不能替用户预先运行 connector、创建本地进程或接触本地凭证。

### 6.2 创建 Agent

```text
选择 Devbox
→ 选择探测到的 Runtime
→ 设置 handle、cwd、可选 launch command
→ 保存 Agent 配置
```

任何 secret 只来自 connector 本地环境，不存 Server。

### 6.3 开始 Session

```text
打开 Agent
→ 查看已有 Sessions
→ 选择 New Session
→ Server 创建 Session(starting)
→ connector 启动 PTY
→ ready
→ Session(live)
→ 浏览器显示真实 TUI
```

### 6.4 恢复 Session

```text
打开 live/disconnected Session
→ Viewer attach
→ Server 发送 restore（scrollback + viewport + cursor）
→ connector 确认 PTY 存活
→ 衔接后续 live output
```

### 6.5 Server 重启

目标流程：

```text
Server 停止
→ connector PTY 继续运行
→ output 写入本地 durable spool
→ Server 恢复
→ connector 重连并上报 surviving sessions
→ 从 last ACK 补发 output
→ Viewer 自动重连
→ restore + live output
```

当前实现已具备内存 FIFO 和 surviving session 上报，但尚未具备磁盘 spool + ACK，因此仍属于
“可恢复”而非严格可证明的 durable delivery。

### 6.6 回放历史

```text
打开 ended/archived Session
→ 加载 recording 元数据和 checkpoints
→ 播放/暂停/调速/seek
→ 可下载或按权限分享
```

---

## 7. 产品界面结构

### 7.1 推荐信息架构

```text
Global rail
  ├── Workspace
  ├── Sessions
  ├── Devboxes
  └── Settings

Session workspace
  ├── 左：Devbox / Agent / Session 导航
  ├── 中：Terminal / Replay 主区域
  └── 右：Session 状态、元数据、参与者、Artifacts
```

### 7.2 Session Control Center

每个 Session 显示：

- 标题
- Agent / Runtime / Devbox
- live/disconnected/ended/failed
- 开始时间、最后活动时间、持续时间
- working directory
- recording 大小
- Viewer 数量
- Resume / Replay / Terminate / Archive

### 7.3 Terminal 体验

必须保持：

- 原生 ANSI/truecolor
- 光标与 resize
- 鼠标和快捷键（Runtime 支持时）
- 有限 scrollback 恢复
- reconnect 状态可见但不遮挡 Agent TUI
- 多 Session tab
- 明确的 live/recording 指示

### 7.4 多 Viewer 控制权

后续协作模式：

```text
多人可以观看
→ 只有一个 Viewer 持有 keyboard lease
→ 其他人可请求控制权
→ 当前控制者可移交
→ 超时/断开后 lease 自动释放
```

### 7.5 已实现的 UI foundation（Terminal-first Switchboard）

> 本节描述当前 `web/` 中**已经实现**的界面，而非 §7.1–7.4 的长期目标。
> 未实现的多 Session tab、URL routing、command blocks 等仍属规划，不在此列。

**设计方向。** 借鉴 Tailscale 的机器清单、Linear 的密度与键盘手感、
Vercel/Geist 的克制暗色层级，但不复制其品牌。刻意避免紫蓝发光渐变、玻璃拟态、
AI 星光/机器人意象与营销式 hero。UI 用 sans，终端用 mono；终端永远是视觉主角。

**视觉系统。** `web/styles.css` 是 token 驱动的深色主题（surface/border/text/
status/accent 全部走 CSS 变量），细边框、单一低饱和青绿 accent、语义状态色。
它在 `index.html` 内联 reset 之后加载，成为样式的唯一事实来源。所有状态
（devbox online/offline、agent online/busy/offline、keyboard lease）都是
「圆点 + 文字」，从不只靠颜色表达。提供清晰的 `:focus-visible`、
`prefers-reduced-motion` 降级，以及窄屏（≤820px）下 fleet 面板与终端纵向堆叠的
响应式布局。

**主 shell 布局。**

```text
topbar（克制品牌 + 搜索/⌘K 入口 + owner 入口 + 用户 + 退出）
├── 左：Fleet 面板（标题、online/total 汇总、搜索框、紧凑 devbox/agent 清单）
└── 右：Terminal stage（会话 header + xterm 主区域；未选 agent 时显示空状态与快捷提示）
```

Devbox 为紧凑 panel，展示名称、状态文字、opaque capability 概览与「+ Agent /
Rotate token / Delete」操作；agent 行显示 monogram、handle、runtime label 与
状态文字，hover/选中时露出 History 操作。

**Command palette（⌘K / Ctrl+K）。** 一个 overlay（不引入路由），可筛选并打开
agent、打开某 agent 的 history、创建 devbox、进入 owner 控制台（仅 owner）。
Escape 关闭，ArrowUp/ArrowDown 移动选择，Enter 执行。

**模态与一次性 token。** createDevbox / createAgent、删除确认与错误提示都用
app 内自定义 modal/form，取代浏览器 `prompt/alert/confirm`。一次性 devbox token
只在内存中渲染进 modal DOM，绝不写入 storage、cookie、URL 或日志；用户可一键复制 raw token
或完整 Windows connector 命令，避免从换行文本中手工抄录。

**xterm 主题。** 终端配色与 UI token 对齐（青绿光标、语义 ANSI 调色板），
resize、reconnect、replay/DVR、collaboration lease 行为保持不变。

**纯函数层。** DOM-free 的可测逻辑集中在 UMD 模块 `web/ui.js`（fleet 汇总、
devbox/agent 过滤、command 生成与筛选、runtime label / initials、状态映射、
HTML escape），由 `app.js` 用与 replay/collaboration 相同的缓存 Promise 方式
动态加载，并由 `web/ui.test.js`（node:test）覆盖。

---

## 8. 数据与可靠性设计原则

### 8.1 两类数据

1. **控制面数据**：User、Devbox、Agent、Session、权限、状态，存关系数据库。
2. **终端事件流**：input/output/resize/exit，存 append-only recording + checkpoints。

### 8.2 当前屏幕与完整历史分离

- 当前屏幕：`pyte.HistoryScreen`，用于快速 restore，大小有界。
- 完整历史：asciicast v2 DVR，用于回放和审计，随时间增长。
- 后续 checkpoint：避免长 recording 每次从头重放。

### 8.3 可靠投递目标

协议 v3 应采用：

```text
connector local spool
→ frame(session_id, seq)
→ Server fsync/persist
→ ACK(session_id, seq)
→ connector 删除本地记录
```

Server 对 `(session_id, seq)` 去重，保证重试不会重复记录。

### 8.4 Session Source of Truth

- 活进程状态源头：Devbox session supervisor。
- 持久元数据源头：Server DB。
- 已持久化输出源头：recording store。
- 浏览器不是任何 Session 状态的源头。

---

## 9. Runtime 扩展设计

Runtime 应采用注册表/adapter：

```python
RuntimeAdapter:
    id
    label
    probe()
    launch_command()
    capabilities()
    normalize_environment()
```

目标：新增 Runtime 只需一个 adapter 文件和一个注册项，Server/Web 不做 runtime-specific 修改。

首批：

- `claude-code`
- `codex-cli`
- `copilot-cli`
- `mock`（测试）

capability blob 应保持 opaque，由 connector 探测和解释，Server 只存储/转发。

---

## 10. 安全与隐私

### 10.1 不可违反的边界

- API key、CLI 登录态永不进入 Server。
- Token 数据库只存 hash。
- Agent 只能由 host 它的 Devbox 身份发言/输出。
- Recording 必须经过 Session/Workspace 权限校验。
- 用户必须知道 Session 正在被录制。

### 10.2 上线前安全基线

- Argon2id 密码哈希
- 环境变量/secret manager 管理签名密钥
- HTTPS/WSS
- Secure/HttpOnly/SameSite cookie
- CSRF 和 WebSocket Origin 校验
- 登录、Token 和 API rate limit
- 审计日志
- Token 轮换、吊销和连接立即失效
- Recording retention、删除和加密

### 10.3 Recording 隐私

终端可能包含代码、内部路径、URL、环境信息甚至误打印的 secret。Workspace 必须可配置：

- 是否录制
- 保存期限
- 谁可以查看/下载
- 手动永久删除
- 后续敏感信息检测/redaction

---

## 11. 非目标与产品边界

短期不做：

- Server 侧模型调用或 API key 托管
- 完整 IDE/代码编辑器
- 任意远程桌面
- 自动读取用户代码仓库
- 在 connector 未授权时运行命令
- 依靠解析 ANSI 文本猜测所有 Agent 语义

Agent-native 状态（waiting/approval/completed）应优先来自 Runtime adapter 的结构化 sideband，
而不是让 Server 猜终端内容。

---

## 12. 成功指标

### 12.1 可靠性

- Session 非用户显式终止率
- reconnect 成功率
- 恢复到可交互状态的 P50/P95 时间
- output gap / duplicate rate
- connector crash-free session hours

### 12.2 使用价值

- 每用户连接 Devbox 数
- 每周 live Sessions 数
- Resume 而不是重建 Session 的比例
- 跨设备恢复次数
- Replay 使用率
- 长任务完成后通知打开率

### 12.3 产品北极星指标

> **每周被成功恢复并继续使用的 Agent Session 数。**

这个指标直接衡量平台是否提供了本地终端没有的价值。

---

## 13. 当前产品结论

当前 P0 已证明：真实 Claude Code 可以在用户 Devbox 上运行，通过 deepbox 原样交互，并在
Viewer detach 和 Server 重启后恢复同一 PTY。

下一阶段不应优先堆更多 Runtime 或做视觉包装，而应依次完成：

1. Session 一等对象和 Control Center。
2. seq/ACK/磁盘 spool 的严格可靠投递。
3. Replay 和历史体验。
4. connector/session supervisor 解耦。
5. 安全基线、权限和生产部署。

详见 [`planning.md`](planning.md)。
