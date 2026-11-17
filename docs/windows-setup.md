# Windows 一键安装配置向导

下载并解压仓库后，双击根目录的 **`install.cmd`**。目录可以包含空格或中文。向导以当前用户运行，不改变系统 PowerShell 策略，不修改账号密钥，也不代写 Hook 信任记录。

## 首次运行

1. **准备 Python**：检测 Python 3.13+。未找到时，可以确认后通过 `winget` 安装 Python 3.13；没有 winget 则显示官方安装页面。创建仓库专用 `.setup-venv`，按固定版本和 SHA-256 安装 YAML 编辑依赖。
2. **检测 CLI**：查找 PATH、常见安装位置及已有本机配置中的 Hermes、Codex、Claude Code、AGY。Hermes 用帮助命令检查，其余用版本命令，不发起模型任务。当前需要 Hermes 和 Codex；Claude / AGY 可选择禁用。
3. **选择项目**：先核对选定 Hermes 目录确实能启动其运行环境、配置位置相符，再填写项目绝对目录与项目 ID。项目不能放在控制仓库内，不能与其他项目重叠；已有 ID 不能悄悄改绑到另一个目录。
4. **预览改动**：显示将新增或更新的文件路径。确认后先备份，再更新本机 agents / routing / registry，合并 Hermes Hook，生成实际路径的管理 Skill。不会输出配置内的密钥。
5. **执行自检**：短暂持有维护标志，确认没有运行 / 排队任务和保留锁，初始化本仓库账本，检查本地 Hook 注入和控制目录拒绝，再运行 Hermes config check / hooks doctor。不会结束其他进程或删锁。
6. **完成信任和登录**：可当场启动 Hermes，由 Hermes 原生提示确认新 Hook；主控模型和执行器账号仍在各自官方界面完成。检查显示 needs_attention 时，配置已写入但相关检查尚未通过，不能当作全部就绪。

安装后可双击 **`start-hermes.cmd`**。它按本机 `local-config/setup-state.json` 中保存的 Hermes 路径和配置目录启动，避免自定义配置目录丢失。

CLI 尚未安装时，向导会提供官方安装说明，并允许填写安装后的原生 `.exe` 路径。它不自动下载这些第三方 CLI、不选择模型订阅、不处理扫码或密钥；`.cmd` / `.ps1` 包装脚本不能直接作为 Worker 可执行文件。

## 既有配置如何处理

- YAML 采用 round-trip 编辑，保留模型、渠道、无关 Hook 和注释。无效 YAML、重复键、共享 Hook anchor 或无法识别的结构会拒绝覆盖。
- 如果发现其他位置的 `team-policy.py`，先询问是否迁移，只有明确选择后才替换这些旧 Team Hook；其他 Hook 保留。
- 旧 Team Hook 的迁移只切换 Hook 入口，不自动搬移另一套 Dispatcher 的注册中心、历史账本或会话绑定。需要保留的项目应先登记到当前仓库，随后新开 Hermes 对话重新绑定；旧数据文件保留原位。
- 修改前在 `local-config/backups/<时间-随机值>/` 保存原文件与校验清单。备份可能含用户私有配置，该目录已被 Git 忽略。
- 重复运行相同设置不会重复添加 Hook，也不会重写一致文件。预览后文件变化、存在其他维护标志或数据库不空闲时停止操作。
- 写入或自检失败时恢复本次配置改动。不会用旧数据库覆盖账本；初始化后的空库或加法迁移可以保留。自检用的临时会话绑定会清理。
- 回退会检查安装后文件是否又被用户修改，发现新改动就停止，避免覆盖。

手动恢复指定备份：

```powershell
.\.setup-venv\Scripts\python.exe -X utf8 -B scripts\windows_setup.py --restore "C:\path\to\hermes-team-v8\local-config\backups\实际备份目录"
```

仅检测，不写配置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -CheckOnly
.\.setup-venv\Scripts\python.exe -X utf8 -B scripts\windows_setup.py --inspect
```

第一条仅检查 Python / winget，不创建虚拟环境；第二条需要已有 setup 环境，读取 CLI 位置和版本，不验证登录或调用模型。移动仓库或升级 Hook 后，重新运行向导并按 Hermes 提示复核信任。

## 测试与验证范围

运行时仍可独立使用 Python 标准库运行 105 项回归。安装向导另有 28 项测试，需要 setup 环境中的 YAML 依赖：

```powershell
python -X utf8 -B scripts/run_tests.py
.\.setup-venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests/setup -v
```

开发者尚未运行向导时，可以先执行 `python -m venv .setup-venv`，再用其中的 Python 执行 `-m pip install --only-binary=:all: --require-hashes -r requirements-setup.txt`。

测试使用临时项目、临时 Hermes 目录和假 CLI，覆盖真实 Dispatcher / Hook 本地进程、配置合并、备份恢复、并发改动、中断、中文与空格路径及 PowerShell 检测入口。另在已有 CLI 的 Windows 环境只读检查了检测结果和真实 PowerShell 入口的取消行为。

从 GitHub ZIP 的完整安装测试确认：部分 Hermes 版本的 `hooks doctor` 会在有问题时仍返回 0。向导现在要求其明确报告 Hook 健康才标记 passed；尚未信任、没有 Hook 或无法确认时标记 needs_attention，避免将配置写入误认为已获信任。

部分 Hermes 原生安装还把依赖环境绑定到其数据目录。不要把任意空目录当成已经初始化的 Hermes home；向导会在写入配置前验证该目录可启动，并拒绝写到与原生命令不一致的位置。它不会执行 `hermes pm repair` 或搬移现有依赖环境。

未在空白机器实际安装 Python、未执行真实账号登录或新 Hook 授权，也未进行模型计费调用。Windows 以外的平台尚未验收。详见 [向导验证摘要](../evidence/windows-setup-test-summary.json)。

官方参考：[Python Windows 下载](https://www.python.org/downloads/windows/)、[winget install](https://learn.microsoft.com/en-us/windows/package-manager/winget/install)、[Hermes 安装](https://hermes-agent.nousresearch.com/docs/getting-started/installation)、[Codex CLI](https://developers.openai.com/codex/cli/)、[Claude Code](https://code.claude.com/docs/en/setup)、[AGY](https://antigravity.google/docs/cli/install)、[ruamel.yaml](https://pypi.org/project/ruamel.yaml/)。
