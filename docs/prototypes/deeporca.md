# DeepBox × DeepOrca v1 交互原型

## 打开实际页面

- 用现代浏览器直接打开 [`web/prototypes/deeporca.html`](../../web/prototypes/deeporca.html)。
- 本机路径：`C:/repos/deepbox/web/prototypes/deeporca.html`。
- 如果运行中的 DeepBox 服务挂载了源码中的静态目录，也可访问 `/static/prototypes/deeporca.html`。

这是**可实际操作的单文件离线原型**，不是截图，也不是生产工作台或真实运行时接入。CSS、JavaScript、SVG 全部内联；不需要构建、下载、CDN、网络连接或独立 DeepOrca WebServer。页面不会安装包、启动 Worker、读取文件、执行命令、调用模型或接触凭据。所有 provisioning、聊天、工具结果和事件都是内存中的模拟数据；刷新页面即重置。

本次对齐 [集成设计](../deeporca-integration-design.md) 第 4、8、11 节。**v1 不提供交互式工具审批**；旧版原型的审批卡片、操作、传输、等待状态和策略选择器均已移除。本页不修改生产 renderer 或其他运行时的行为。

## 建议体验顺序

1. 初始状态只有已登记、在线的演示机器 **WIN-DEV01**，没有 Agent，也没有预置聊天。
2. 在机器卡片点击 **+ Add agent**：
   - 填写 Agent 名称；
   - Runtime 为 **DeepOrca — Python library**；
   - 从机器已登记的项目 `deepbox` / `design-system` 中选择；
   - 创建新的 managed profile，并使用 **Connector-default configuration template**。
   - 不填写工作目录、包目录、解释器、API key，也没有隔离方式或审批策略切换。v1 固定为隔离 library worker。
3. 点击 **Create agent**。树中出现 provisioning 状态，约一秒后进入 ready，并自动打开**空会话**。Inspector 可展开 `agent.created`、`agent.provisioning`、`agent.ready` 等事件。
4. 点击空会话中的 **Try a permitted read + a blocked operation**，或者自己输入文本并 Send。两者都会播放同一段明确标注的脚本响应：
   - 可折叠的 thinking summary（预设摘要，不是真实模型的内部推理）；
   - `read_file` 的允许结果；
   - `shell.execute` / `git push` 因最终 verdict 为 `REVIEW`，立即显示 blocked / not executed 结果；
   - 逐段流式文字。展开工具卡可看参数、结果与诊断码。
5. 流式过程中点击 **Stop**。后续模拟输出被丢弃，未完成的工具卡显示 cancelled，不会保持悬空状态。
6. **+ New conversation** 在当前 Agent 下增加独立会话；不会再创建 profile。点击左侧历史可切换会话。**Agent settings** 是独立入口：可改显示名，查看固定的 profile / project / template / worker 绑定；改名不会改变 profile identity。
7. 生成过程中点击底栏 **Simulate offline**。会话事件显示暂停，但内存模拟继续；**Reconnect** 从已有 display log 重建画面，不重发输入或工具调用。Agent readiness 与机器连接状态分开显示。
8. 点击 **Replay**，拖动事件数量滑块，再点 **Exit replay** 返回实时画面。回放只归约已有事件，不执行工具、不产生新请求。发送、停止、创建 Agent、修改设置、新建会话和连接切换在回放中禁用；先前已开始的模拟生成可独立继续。切换会话会退出回放。
9. 点击 **Inspector** 查看或收起运行检查器；小屏可通过左上角菜单打开机器树。支持 390px / 320px 窄屏、原生模态对话框、键盘焦点和展开卡片。

## Provisioning 分支

Add agent 底部的 **Demo control · simulated outcome** 是原型测试控件，不是生产配置：

| 演示选择 | 状态与反馈 |
| --- | --- |
| Ready | `created → provisioning → ready`，打开空会话 |
| Needs configuration | Agent 已记录，但本机模板不可用；不能聊天，提示在 Connector 机器侧配置模型模板 |
| Error | 模拟 Worker 初始化失败；不能聊天，提示查看机器侧兼容性与诊断，不提供自动安装 |

在后两种状态下打开 **Agent settings**，可选择下一次演示结果并点击 **Simulate local fix & retry**。这只模拟机器管理员在本地修复后的重新检查；不读取或写入真实配置。成功后复用同一个 profile 并打开空会话。

当前演示 inventory 只支持创建托管 profile。绑定已有 profile 明确标为不可用，直到机器支持可靠的 exclusive profile ownership 协议；不提供一个无效的绑定按钮。

## 工具策略语义

| 本机最终判定 | v1 行为 | 此原型 |
| --- | --- | --- |
| `ALLOW` | 保留参数、路径等检查，通过后执行 | 显示允许读取的 fixture；没有真正读取文件 |
| `DENY` | 保留既有拒绝，不执行 | 通过说明展示该边界 |
| `REVIEW` | 交互分支之前转换为立即拒绝，不执行、不等待 | 同步生成 blocked 工具结果，`review_not_supported` 诊断 |

没有“允许一次”“始终允许”“确认命令”按钮或选择器，也没有审批请求/响应消息或等待决定的状态。blocked 卡片是**工具结果**，不是用户待办。

“不需要浏览器确认”不等于工具均可运行：项目选择本身不授权任意文件访问，也不是沙箱；实际允许范围仍由机器本地策略决定，不能为了消除提示而放宽安全配置。原型里所有工具结果均为模拟，`executed: false`；允许结果另外标注 `simulated_execution: true`，避免声称真实执行。

## 实现边界

- Agent binding、机器连通性和 session 是独立概念。profile ID 来自内部 Agent identity，不从可编辑名称派生。
- 每个 session 有独立的事件数组、草稿和取消 token，绑定相同 Agent 的 profile / project。
- `reduce(events)` 是纯 display reducer，实时显示、断线恢复、回放共用它。回放不调用模拟 send 或 provisioning。
- 断线控件模拟的是会话事件交付中断，不是真实网络或进程崩溃；机器及 provisioning inventory 仍为本页模拟状态。
- 回放进入时固定事件上限；退出后再显示新收到的事件。回放过程中后台模拟可继续，所以后台事件增加不代表回放重执行。
- 消息和用户可编辑名称都经过 HTML 转义。折叠卡片在流式更新时保留展开状态与 summary 焦点。
- `deepbox.deeporca.prototype.v1`、事件名称、profile 引用和诊断码服务于这个原型，不声明为生产协议。
- 原型不创建原生 SessionManager，不持久化模型上下文；display log 不等于原生模型上下文。
- 旧版的附件、主题切换、Viewer、Worker 分屏及导出等附件功能已移除，以聚焦 Agent-first v1 流程；没有留下无作用的操作入口。
- 实际 DeepBox 工作台及其生产 renderer 在其他文件中实现，本文件不替代该接入。

## 本地验证

可选测试文件：[`tests/test_deeporca_prototype.py`](../../tests/test_deeporca_prototype.py)。仅使用**本机已经安装**的 Python Playwright 与 Chromium，不安装或下载任何内容；依赖缺失时跳过。

```text
python -m unittest discover -s tests -p test_deeporca_prototype.py -v
```

覆盖 7 组浏览器检查：

1. 初始空 Agent 树、表单字段、名称校验、provisioning 与 ready 空会话；
2. needs-configuration / 初始化错误及本机修复模拟；
3. 流式、可折叠 thinking、允许工具、立即 blocked 结果，无审批操作或传输；
4. Stop 丢弃延迟输出、取消未完成工具、HTML 输入转义；
5. 离线显示冻结、重连不重复输入/工具、无副作用回放；
6. 新会话不创建新 profile、改名不改变绑定、多 Agent 项目隔离；
7. 390px / 320px 侧栏、对话框、检查器、无水平溢出和唯一 DOM ID。

测试监听 JavaScript 异常和网络/WebSocket 尝试，并阻止 HTTP/HTTPS 请求。它们不是生产后端或真实 DeepOrca SDK 的集成测试。

## v1 原型截图

以下是当前**离线原型**的截图，不是实际 Connector 或模型运行的证明：

- [聊天、允许工具和立即拒绝](deeporca-v1-workspace.png)
- [Add agent](deeporca-v1-add-agent.png)
- [移动端空会话](deeporca-v1-mobile.png)

## 旧截图

目录中的 `deeporca-workspace.png`、`deeporca-connect.png`、`deeporca-mobile.png` 是**旧版审批原型的历史截图，未更新且不代表 v1**。已移除旧预览嵌入链接。请以可打开、可操作的 HTML 页面为当前交互依据。
