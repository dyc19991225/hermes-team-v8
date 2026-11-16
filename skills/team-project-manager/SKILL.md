---
name: team-project-manager
description: One Hermes with isolated projects and shared Codex/Claude/AGY workers. Use for entering projects, dispatching tasks, queues, dependencies, acceptance, budgets and V8.1 diagnostics.
---

# Hermes Team V8.1 项目管理

一个 Hermes 主控、一份注册中心、一套共享 Dispatcher。默认中文。使用已登记项目，不新建 Hermes 实例，不直接调用 Agent CLI 修改业务文件。

这是公开模板：安装时将下文 `C:\path\to\hermes-team-v8` 改为实际仓库绝对路径，并核对项目 ID 与根目录。未登记的示例项目不能直接执行。

## 会话与项目

入口：`python "C:\path\to\hermes-team-v8\dispatch\dispatcher.py" projects`。

进入示例项目 A：`python "C:\path\to\hermes-team-v8\dispatch\dispatcher.py" bind --project-id project-alpha`。
进入示例项目 B：`python "C:\path\to\hermes-team-v8\dispatch\dispatcher.py" bind --project-id project-beta`。

Hook 注入真实 session_id。不要猜测、伪造或复用别的会话身份。绑定只影响当前会话，不自动执行旧任务。项目进度以当前项目规则和进度文件为准；planned / archived 项目不能执行。

`context` 重读当前项目上下文；`project-status` 查看当前项目任务。项目记忆分开保存，不通过 session_search 或其他项目日志导入历史。

## 创建和执行

只在用户要求实际工作时创建 inbox JSON。只能写 `C:\path\to\hermes-team-v8\dispatch\inbox\<unique>.json`；业务文件交给受控 Worker。

```json
{
  "project_id": "project-alpha",
  "project_root": "D:\\example-workspace\\project-alpha",
  "agent": "codex",
  "task_type": "code_write",
  "risk_level": "medium",
  "execution_mode": "workspace_write",
  "approval_state": "not_required",
  "idempotency_key": "unique-current-user-request",
  "prompt": "本次目标、允许修改范围、保留已有改动、验证方法和交付格式",
  "budget": {"max_output_bytes": 16777216, "max_retries": 0},
  "depends_on": []
}
```

省略 task_id，由服务生成。session_id 由真实运行时 Hook 补全。`depends_on` 可省略；如使用，必须引用已存在的同项目任务 UUID，最多 16 个，不能交叉项目。不要在任务 JSON 中写验收、队列或用量状态。

依次执行：

1. `create --file "C:\path\to\hermes-team-v8\dispatch\inbox\<unique>.json"`
2. `dry-run --task-id <uuid>`
3. `run --task-id <uuid>`
4. `status --task-id <uuid>`

命令前缀均为 `python "C:\path\to\hermes-team-v8\dispatch\dispatcher.py"`。run 在空闲时后台执行，忙时进入等待队列。所有 Worker 仍串行执行；不删锁、不绕过审批。不重复创建或运行同一任务。

status 本地等待状态变化，最长 30 秒。它返回精简执行状态及 workflow 信息，完整日志留在文件。不要每两秒用模型查询，也不要反复读取同一日志。

## 队列与阶段依赖

- `enqueue --task-id <uuid>`：明确加入等待队列；表示该任务可在后续队列运行、资源空闲及前置验收通过后执行。
- `queue`：只看当前项目队列和等待原因。
- `run-next`：启动当前项目可运行任务。普通任务完成后，已明确排入队列的任务由单一后台观察进程接续，不需要主控做模型轮询。
- `cancel --task-id <uuid>`：取消请求。运行中的任务必须等进程树停止后才能释放锁。

历史 CREATED / DRY_RUN 任务不会被迁移自动排队。前置任务失败、取消或验收拒绝时，依赖任务阻塞；前置执行成功但未验收时继续等待。后台队列观察进程单次最长运行一小时，超时后可用 run-next 恢复。崩溃恢复保留 token、进程身份和租约校验，不按时间盲目删锁。

## 独立验收

SUCCEEDED 只代表执行结束。主控依据验收标准独立核对测试、范围和交付文件后，才记录验收。需要文件型验收证据时，让 Worker 在当前项目生成一份 JSON，主控检查内容，不把执行者自报等同于独立验证。

```json
{
  "summary": "独立核对的结果与适用范围",
  "checks": [{"name": "相关回归测试", "status": "passed"}],
  "outputs": ["D:/example-workspace/project-alpha/实际交付文件"]
}
```

证据文件须在当前项目内，最大 64 KiB。checks 至少一项，只能 passed / failed；接受时全部 passed。写任务至少一个实际存在的本项目输出文件。不要自动执行示例路径。

`review --task-id <uuid> --decision accept --evidence "<本项目证据文件>"`

拒绝使用 `--decision reject`。验收决定不可改写；返工须创建新任务。依赖阶段要求前置执行成功且独立验收 ACCEPTED，才能继续。程序校验证据完整性与归属，不能替主控判断审美或证明测试真的执行。

## 预算、诊断与边界

- `usage --task-id <uuid>` 查看任务预算和执行器报告的用量。可选 `max_tokens` 为正整数；当执行器上报超限时终止所属进程树。有些执行器只在完成时上报，因此 Token 预算不是保证不超额的账单上限；未上报用量明确显示 unavailable，不算作零费用。
- 默认日志总预算 16 MiB；可配置 1 KiB 到 64 MiB。单流结构化结果解析仍保留 4 MiB 上限。每个任务只执行一次，默认不自动重试；失败先核对证据，不盲目循环。
- `doctor` 查看 CLI 版本与版本变化；`doctor --probe` 做 Codex 原生命名权限握手，不创建模型 turn。CLI 更新后先检查，再用一个小任务复测真实文件边界。
- Codex：常规开发、构建、测试和浏览器脚本验证。写任务 workspace_write，只读 read_only。
- Claude：保留受限执行层、明确工具列表及原登录。AGY：每次调用原生文件权限，提示以单条 stream-json user 消息经标准输入传递；终端、MCP 和浏览器仍禁止。browser_verify 默认交给 Codex。
- 高风险任务不能自行写 approved。保留 fail-closed Hook、审批、全局权限和共享账号配置。只读任务也只能访问当前项目范围。

派发前简明说明结果、范围、验收和下一检查点。通常每阶段 120–180 秒；视觉任务先检查可见小样本。长任务约每 60–90 秒简短更新，失败或不合格及时汇报并停止依赖阶段。接近主控轮数上限时保存检查点，用短交接继续；不载入其他项目历史。

普通终端调用需显式 `--session-id` 并先绑定；它不经过 Hermes Hook，不能代替 Hook 边界验收。维护说明：`docs/runtime-setup.md`。
