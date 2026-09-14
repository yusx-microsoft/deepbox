# 维护性重构：review 已通过

本轮改动已通过用户 review，并获准提交、推送及部署既有 Azure App Service。
是否已上线应以实际部署状态和 `/api/version` 为准，不从 Git push 推断。
保留现有 FastAPI、原生 JavaScript、SQLAlchemy 和本机 connector 架构，没有引入
新的框架，也没有为了减少行数而删除持久化、权限校验、重连、录制或多 runtime 支持。

## 建议 review 顺序

| 入口 | 重点 |
| --- | --- |
| `web/app.js`、`web/ui.js` | Terminal/Chat 会话选择、异步返回失效处理、成员角色编辑、回放和弹窗生命周期 |
| `server/app/main.py`、`models.py` | 持久化 surface、输入权限、所有连接器消息的归属和实例校验 |
| `connector/supervisor.py`、`agent_session.py`、`pty_session.py` | 启动/退出/取消、并发启动、输入队列、Windows PTY 资源释放 |
| `connector/client.py`、`transport.py`、`diagnostics.py`、`runtime_probe.py` | 单一发送链路、URL 校验、诊断和能力探测的信息边界 |
| `tests/test_session_surfaces.py`、`tests/test_pty_session.py`、`web/app.test.js` | 本轮新增的核心回归覆盖 |

也审查了 identity/security、hub/live/recording、connector 的 IPC/spool/projects/skills、
安装脚本及运维配置。没有发现需要改动的部分保留原样；这不是“保证剩余代码没有任何 bug”。

## 1. Terminal / Chat

- `Session.surface` 是可空的新增列，值为 `terminal` 或 `structured`。迁移是增量、
  幂等的；不会通过 runtime 名称猜测历史会话的类型。
- 明确点击 **Terminal** 只复用同类型的 live session。不会再错误复用已有 Chat，
  也不会把未知类型的旧会话当成 Terminal。**New session** 保留当前选择。
- attach/reconnect 使用已知会话类型，不悄悄切换 surface。旧页面或旧 WebSocket
  的晚到响应不能覆盖新页面。
- Chat 及结构化回放不初始化 xterm。Terminal 渲染器缺失会明确报错，不创建一个
  用户看不到的会话。回放初始化不会再删除 retention/delete 等工具栏 DOM。
- Windows 默认 PTY 启动原本就传 argv；本轮不能把“默认启动双重引号解析”算作根因。
  实际改进包括显式命令的 Windows 路径/引号解析、真实退出状态、失败启动和退出后的
  资源释放。POSIX 工作目录切换失败也不能继续在错误目录启动。
- 未知 runtime 不再静默替换成 mock；显式合法 `launch_cmd` 仍受支持。

## 2. 共享 workspace

- **Operator/Admin/Owner 可以发送结构化消息、回复权限请求及中断当前 turn，
  不需要抢占终端键盘。Viewer 始终只读。**
- Terminal 输入和 resize 仍受独占键盘控制；结束进程仍要求键盘持有者或 Admin/Owner。
- 新邀请表单明确默认选择 Operator；API 的默认角色仍为 Viewer。已有邀请、成员
  不会被自动提权。
- 已有同事如果是 Viewer：在 **Members & invitations → 选择 Operator → Save**
  中显式修改。选择本身不发送请求；保存失败保留旧授权，错误和待重试选项留在界面。
  UI 不编辑 Owner、自身授权，Admin 也不通过此界面修改另一个 Admin。
- **New chat** 只开新会话，不再隐式结束其他人可能仍在用的会话。
  **End session** 是独立、有确认且按权限启用的操作。
- 浏览器回放的只读输入区真正隐藏；增加 `[hidden]` 的样式约束，避免 `display:flex`
  覆盖 HTML 的 hidden 语义。

## 3. 删除和收敛的代码

- 移除静态 helper 的动态加载 promise、single-flight chat mount gate、废弃回调和
  没有实际工作的 input flush/discard 方法；脚本按固定顺序加载，发送路径是同步的。
- 合并普通打开、恢复和新建会话的页面流程；modal/form 使用统一的关闭生命周期。
- 移除 `Connector._sender`、委托属性/方法 facade，以及 supervisor 的 `_PendingView`
  队列伪装。真正发送和确认仍只有 `TransportSession` 一条路径。
- 移除没有消费者的 `LiveSession.subscribers`，保留 Hub 的真实订阅关系；移除未调用的
  legacy input recorder、无效 `_retried` 参数、`DEFAULT_CMDS` 兼容视图和未使用的
  installer URL 常量。
- 结构化写入统一经过有界队列，per-turn 子进程依次运行。队列满/关闭明确拒绝；
  kill 后的待执行任务不能重新启动进程。并发启动按 session 串行，退休/关闭会使
  待启动请求失效。

新增的生命周期防护、归属校验和测试会增加行数；这里不把“总代码行数更少”等同于
“更好维护”，也没有为了凑删除量去掉有用的协议兼容或安全功能。

## 4. 安全与可靠性

- ready、exit、旧格式 output、process/session snapshot、input ACK、runtime 错误
  都校验连接器是否拥有该 session，以及当前连接/实例是否仍有效；不只校验 v3 输出。
- 浏览器输入重新检查当前身份、成员资格和 attachment。角色撤销、禁用用户不能继续
  通过旧连接发消息；格式错误的 frame 得到明确错误而不是打断服务。
- 拒绝的 input ACK 清除 pending 项，但不被记为已交付的录制输入。
- 非 loopback connector URL 必须 HTTPS，拒绝携带用户名/密码、query 或 fragment
  的 base URL。HTTP 和 WebSocket 使用同一套校验。
- 未知诊断错误只输出异常类型，不回显可能含凭据的异常文本。能力报告只提取版本号；
  probe 输出暂存在临时文件，只读取 64 KiB 前缀，避免先把全部输出装入内存。

## 5. 验证

- Python 全套：**627 passed，4 skipped**。跳过的是 POSIX socket 权限、POSIX spool
  权限、POSIX fork/exec 握手和当前 Windows 权限不允许的 symlink 创建，不能计作已验证。
- Node 全套：**84 passed**，包括实际 `app.js` 的页面调度、成员保存、角色、WebSocket、
  dialog 与 replay 回归，而不只是测试纯 helper。
- 独立 Windows Python 子进程 PTY：带空格路径、引号/特殊字符参数、输入、resize、
  正常和非零退出、kill/句柄释放。没有调用真实 Claude/Copilot/Codex。
- 独立 Chrome profile + 临时 FastAPI/数据库 + fake connector：Operator 聊天、Viewer
  只读、显式角色提升后发送、新聊天保留旧共享会话、确认结束、结构化回放和缺少 xterm
  资源时的可见错误。外部资源请求全部拦截，没有访问真实 workspace。
- UTF-8/BOM/原有每文件行尾、Python 编译、CR-aware `git diff --check` 已通过。
  review 阶段保留了全部本地改动，取得批准后才进行提交和发布。
- 重跑捕获了安装器旧测试的 PID 文件握手竞态（文件已存在但内容还未写完）。测试
  fixture 改为写完后原子 rename；该测试连续独立执行三次通过，没有用 sleep 加时、
  关闭检查或跳过用例来掩盖失败。

```bat
cd /d C:\Code\deepbox
.venv\Scripts\python.exe -m pytest -q
node --test web/ui.test.js web/chat.test.js web/collaboration.test.js web/replay.test.js web/app.test.js
```

## 尚未做的验证 / 上线注意

- 没有连接你的机器、启动真实 agent、使用真实 CLI 登录态或修改线上 workspace。
  真实 Claude 的账号、安装、鉴权和你网络里的 xterm CDN 可达性仍需你亲自验收。
- Windows 下未执行原生 POSIX fork/exec 路径。现有依赖弃用告警没有通过关闭 warnings
  或升级整套依赖来掩盖。
- 本地测试不代替 Azure 发布验证。UI/server 与 connector 修复需要各自更新后
  才会在线生效；部署服务端不自动升级或启动任何本机 connector。
- Web 资源没有 hash 文件名；以后正式发布后请 Ctrl+F5，避免使用缓存的旧页面。
