# 脱敏配置示例

这里的项目名、磁盘路径、会话 ID 与任务内容均为虚构，只用于解释本地 V8.1 的数据结构。

- `registry.example.json`：展示项目状态、根目录和执行器白名单。`planned` 项目不能直接执行。
- `task.example.json`：展示任务归属、执行器、风险、模式、超时、幂等键、提示、预算和前置任务字段。
- `acceptance.example.json`：展示验收证据结构。示例输出文件没有随仓库提供，不能直接当成已验收结果。

核心 Dispatcher、Hook 和测试已在 [dispatch](../dispatch/) 提供，配套配置模板见 [config](../config/)；请先阅读 [运行说明](../docs/runtime-setup.md)。实际模型任务仍需本机 CLI、登录、项目目录和 Hook 信任配置。仅复制示例 JSON 不会自动完成运行配置。

在 Hermes 受控入口中，会话 ID 来自运行时 Hook；普通终端直接调用时需要显式会话 ID 和事先绑定。示例中的 `example-web-session` 只是占位值。幂等键用于拒绝重复创建，示例不是批量重试脚本。

新增项目须审核实际目录、链接与权限，同时更新注册中心和相关白名单，并完成边界验收。

`depends_on` 最多 16 项，须换成已存在的同项目真实任务 UUID；只有前置执行成功并验收 ACCEPTED，后续阶段才运行。空数组表示没有依赖。不要把验收、队列或用量状态写进创建 JSON。

`max_tokens` 是执行器上报量的停止阈值，延迟上报不能保证账单上限；不是预付额度。`max_retries` 当前只接受 0。所有 Worker 继续串行执行。
