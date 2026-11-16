# 运行源码与本地配置

本仓库提供 V8.1 的六个核心模块、Hermes Hook、管理 Skill 模板、八个原有测试模块及新增的配置入口回归。Windows、Python 3.13 是本次实际验收环境；这里不包含 Hermes 本体、模型 CLI、账号登录、业务项目或已有账本。

## 文件结构

```text
dispatch/
  dispatcher.py         任务、后台执行、状态与锁
  policy.py             校验与 CLI 参数
  project_manager.py    项目、会话、上下文和权限适配
  codex_worker.py       Codex app-server 协议
  process_runtime.py    Windows Job Objects、输出、停止和预算
  workflow.py           队列、依赖、验收、用量和诊断
  team-policy.py        Hermes pre_tool_call Hook
  tests/                回归与本地进程集成测试
config/                 无账号信息的 agents / routing 模板
skills/                 管理 Skill 模板
scripts/configure.py    生成本机忽略配置，拒绝覆盖已有文件
scripts/run_tests.py    临时目录、虚构项目和模拟 CLI 的离线测试
```

这些模块以本地已验收的 V8.1 为基础，去除了固定用户名、业务目录和本机安装位置。Hook 默认控制同目录的 Dispatcher，缺少项目注册配置时拒绝请求。代码中的进程 token、模型 Token 用量字段和测试假值不是登录密钥。

## 先运行离线测试

在仓库根目录执行：

```powershell
python -X utf8 -B scripts/run_tests.py
```

不需要安装 Codex、Claude Code、AGY，也不需要登录。测试入口把源码复制到临时目录，生成临时项目和配置，以当前 Python 充当假的执行器路径；用户目录和 Codex 配置也指向临时环境。真正的进程测试只启动本地 Python fixture，原生权限协议使用模拟响应，不发起模型请求。

测试结束清理临时环境，不在仓库或本机正式 Hermes 创建任务。可以用 `--pattern test_upgrade_v81.py` 只检查 V8.1 回归。直接在未配置的 `dispatch/` 运行 unittest 不会自动生成测试配置，请使用上面的入口。

## 配置实际运行

实际模型任务需要自行安装并登录相应 CLI。Codex 也用于 Claude 的 Windows 受限执行适配，因此配置入口要求提供 Codex 可执行文件。以下路径是占位值，先替换为本机真实绝对路径：

```powershell
python -X utf8 scripts/configure.py --project-id project-alpha --project-root "D:\work\project-alpha" --codex "C:\path\to\codex.exe" --claude "C:\path\to\claude.exe" --agy "C:\path\to\agy.exe"
python -X utf8 dispatch/dispatcher.py init
python -X utf8 dispatch/dispatcher.py bind --session-id my-chat --project-id project-alpha
python -X utf8 dispatch/dispatcher.py doctor --session-id my-chat
```

`--claude` 和 `--agy` 可省略；省略的执行器保持禁用。业务项目目录与控制仓库应相互独立。配置脚本生成被 Git 忽略的 `dispatch/agents.json`、`dispatch/routing.json` 和 `local-config/registry.json`，不读取或写入账号凭据，不覆盖已有配置，不安装 Hook。

`doctor --probe` 可在配置好之后额外检查 Codex 原生命名权限握手，不创建模型 turn。真实任务使用用户既有登录，可能消耗配额。当前适配协议验证过 Codex 0.169.0、Claude Code 2.1.322、AGY 1.5.5；其他版本需重新检查，普通终端成功不等于正式 Hermes Hook 隔离通过。

任务 JSON 可参照 [示例](../examples/task.example.json)，把路径和会话换成实际值。普通终端需要显式 `session_id`；Hermes 接入后由 Hook 注入真实会话。新任务应依次 create、dry-run、run、status；高风险审批和独立验收仍为两个状态。

## 接入 Hermes

将 [管理 Skill 模板](../skills/team-project-manager/SKILL.md) 中的控制仓库路径、项目 ID 和项目根目录换成本机配置后，放到自己的 Hermes Skill 目录。项目 B 仅为示例，必须先登记才能使用。

Hook 使用仓库内 `dispatch/team-policy.py`，默认指向它同目录的 Dispatcher；不要把这个文件单独复制到别处。将下面示例合并到自己的 Hermes Hook 配置，保留既有规则，并由 Hermes 本身完成首次信任确认：

```yaml
hooks:
  pre_tool_call:
    - matcher: write_file|patch|read_file|search_files|session_search|read_terminal|terminal|delegate_task|execute_code|skill_manage|memory|cronjob_manage|process_manage|desktop_project|todo_list|browser_exec|browser_cdp|browser_vault|manage_catalog|manage_connections|computer_use|image_generate|text_to_speech
      command: C:/path/to/hermes-team-v8/dispatch/team-policy.py
      timeout: 10
      fail_closed: true
```

然后运行 `hermes hooks doctor` 和 `hermes config check`，在新 Hermes 对话中绑定项目，验证真实会话注入、同项目访问和跨项目拒绝。模板不是本机信任记录，复制源码不会自动取得 Hook 审批。

## 公开与本地内容

仓库收录明确列出的程序、测试、虚构配置和验证摘要。`agents.json`、`routing.json`、注册中心和每次任务生成的提示、结果、数据库、进程日志、会话记录、备份均保留本地。不要把整个 Hermes / Codex / Claude / AGY 用户目录复制进仓库。

公开版本保持全局串行、无自动重试和现有权限策略。日志默认 16 MiB；Token 阈值依赖执行器上报，不能保证账单上限。验收程序只检查证据结构和归属，测试真实性与交付质量需要独立核对。
