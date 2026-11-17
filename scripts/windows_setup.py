"""Interactive Windows setup. All writes are planned, backed up and reversible."""
from collections.abc import Mapping
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid

from ruamel.yaml import YAML
from configure import REPO, configuration, existing_absolute
sys.path.insert(0, str(REPO/'dispatch'))
from process_runtime import OwnedProcess

TOOLS = ('hermes', 'codex', 'claude', 'agy')
MATCHER = 'write_file|patch|read_file|search_files|session_search|read_terminal|terminal|delegate_task|execute_code|skill_manage|memory|cronjob_manage|process_manage|desktop_project|todo_list|browser_exec|browser_cdp|browser_vault|manage_catalog|manage_connections|computer_use|image_generate|text_to_speech'
INSTALL_LINKS = {
    'hermes': 'https://hermes-agent.nousresearch.com/docs/getting-started/installation',
    'codex': 'https://developers.openai.com/codex/cli/',
    'claude': 'https://code.claude.com/docs/en/setup',
    'agy': 'https://antigravity.google/docs/cli/install',
}


class SetupError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode('utf-8')


def native_run(argv, *, env=None, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, input=None):
    """Timeouts stop the owned process tree, including CLI launcher children."""
    args = [str(x) for x in argv]
    owner = OwnedProcess(args, cwd=os.getcwd(), stdin=input is not None,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        payload = input.encode(encoding) if isinstance(input, str) else input
        stdout, stderr = owner.proc.communicate(input=payload, timeout=timeout)
        if text:
            stdout = stdout.decode(encoding, errors)
            stderr = stderr.decode(encoding, errors)
        return subprocess.CompletedProcess(args, owner.proc.returncode, stdout, stderr)
    finally:
        owner.stop()
        for stream in (owner.proc.stdin, owner.proc.stdout, owner.proc.stderr):
            if stream:
                stream.close()


def probe_cli(path, *, run=native_run, kind=None):
    path = Path(path).expanduser()
    if not path.is_absolute() or not path.is_file() or path.suffix.lower() != '.exe':
        return None
    try:
        # Hermes --version also checks remote updates. --help checks the actual
        # CLI without turning environment discovery into an update request.
        help_probe = kind == 'hermes' or path.stem.lower() == 'hermes'
        result = run([path, '--help' if help_probe else '--version'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=8)
        text = (result.stdout or result.stderr).strip().splitlines()
        if result.returncode or not text:
            return None
        return {'path': str(path.resolve()), 'version': 'Hermes CLI（帮助检查通过）' if help_probe else text[0][:160],
                'probe': 'help' if help_probe else 'version', 'login': 'not_checked'}
    except (OSError, subprocess.TimeoutExpired):
        return None


def detect_tools(repo=REPO, *, env=None, which=shutil.which, run=native_run):
    env = os.environ if env is None else env
    local = Path(env.get('LOCALAPPDATA', str(Path.home()/'AppData'/'Local')))
    roaming = Path(env.get('APPDATA', str(Path.home()/'AppData'/'Roaming')))
    home = Path(env.get('HERMES_HOME', str(local/'hermes')))
    user = Path(env.get('USERPROFILE', str(Path.home())))
    npm = roaming/'npm'/'node_modules'
    candidates = {
        'hermes': [home/'bin'/'hermes.exe', local/'hermes'/'bin'/'hermes.exe'],
        'codex': [local/'Programs'/'OpenAI'/'Codex'/'bin'/'codex.exe'],
        'claude': [user/'.local'/'bin'/'claude.exe', npm/'@anthropic-ai'/'claude-code'/'bin'/'claude.exe'],
        'agy': [local/'agy'/'bin'/'agy.exe'],
    }
    for base in (npm/'@openai'/'codex', npm/'@openai'/'codex-win32-x64', npm/'@openai'/'codex-win32-arm64'):
        if base.is_dir():
            candidates['codex'].extend(base.glob('vendor/*/codex/codex.exe'))
            candidates['codex'].extend(base.glob('node_modules/@openai/codex-*/vendor/*/codex/codex.exe'))
    config = Path(repo)/'dispatch'/'agents.json'
    if config.is_file():
        try:
            for name, item in json.loads(config.read_text(encoding='utf-8')).get('agents', {}).items():
                if name in candidates and isinstance(item, dict) and item.get('executable'):
                    candidates[name].insert(0, Path(item['executable']))
        except (ValueError, OSError, TypeError):
            pass
    found = {}
    for name in TOOLS:
        command = which(name+'.exe') or which(name)
        if command:
            candidates[name].insert(0, Path(command))
        seen = set()
        for path in candidates[name]:
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            result = probe_cli(path, run=run, kind=name)
            if result:
                found[name] = result
                break
    return found


def hermes_home_for(executable, *, env=None, run=native_run):
    env = os.environ if env is None else env
    if env.get('HERMES_HOME'):
        return Path(env['HERMES_HOME']).expanduser().resolve()
    if executable:
        try:
            result = run([executable, 'config', 'path'], capture_output=True, text=True,
                         encoding='utf-8', errors='replace', timeout=10)
            lines = result.stdout.strip().splitlines()
            if result.returncode == 0 and lines:
                path = Path(lines[-1].strip())
                if path.is_absolute() and path.name == 'config.yaml':
                    return path.parent.resolve()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return (Path(env.get('LOCALAPPDATA', str(Path.home()/'AppData'/'Local')))/'hermes').resolve()


def check_path(path):
    """Reject redirecting a setup write through a link, reparse point or hardlink."""
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise SetupError('配置路径不能经过链接：'+str(item))
        if item.exists() and getattr(item.stat(), 'st_file_attributes', 0) & 0x400:
            raise SetupError('配置路径不能经过 reparse point：'+str(item))
    if path.is_file() and path.stat().st_nlink > 1:
        raise SetupError('配置文件存在硬链接：'+str(path))
    return path.resolve()


def read_optional(path):
    check_path(path)
    return path.read_bytes() if path.exists() else None


def read_json(path, fallback):
    raw = read_optional(path)
    if raw is None:
        return deepcopy(fallback)
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise SetupError('配置 JSON 无效，未覆盖：'+str(path)) from exc
    if not isinstance(data, dict):
        raise SetupError('配置必须是 JSON 对象：'+str(path))
    return data


def team_command(command):
    return isinstance(command, str) and bool(re.search(r'(?:^|[/\\])team-policy\.py(?:["\s]|$)', command, re.I))


def merge_hooks(raw, command, *, replace_team=False):
    yaml = YAML(typ='rt')
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = False
    yaml.width = 4096
    try:
        data = yaml.load(raw.decode('utf-8-sig')) if raw else {}
    except Exception as exc:
        raise SetupError('Hermes YAML 无效或有重复键；未修改原文件') from exc
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise SetupError('Hermes 配置根节点必须是映射')
    hooks = data.setdefault('hooks', {})
    if not isinstance(hooks, Mapping):
        raise SetupError('hooks 必须是映射；未覆盖原配置')
    entries = hooks.setdefault('pre_tool_call', [])
    if not isinstance(entries, list) or any(not isinstance(item, Mapping) for item in entries):
        raise SetupError('pre_tool_call 必须是 Hook 列表')
    if any(getattr(node, 'anchor', None) and node.anchor.value for node in (hooks, entries)):
        raise SetupError('Hook 使用 YAML anchor；请先拆开共享节点，避免影响其他事件。')
    conflicts = [item.get('command') for item in entries if team_command(item.get('command')) and item.get('command') != command]
    if conflicts and not replace_team:
        raise SetupError('发现其他 Team Policy Hook。确认迁移后选择“替换旧 Team Hook”；其他 Hook 会保留。')
    desired = {'matcher': MATCHER, 'command': command, 'timeout': 10, 'fail_closed': True}
    owned = [item for item in entries if item.get('command') == command]
    if not conflicts and len(owned) == 1 and dict(owned[0]) == desired:
        return raw, 0
    kept = [item for item in entries if item.get('command') != command and not (replace_team and team_command(item.get('command')))]
    entries[:] = [*kept, desired]
    output = io.StringIO()
    yaml.dump(data, output)
    return output.getvalue().encode('utf-8'), len(conflicts)


def ensure_idle(repo):
    path = Path(repo)/'dispatch'/'state.sqlite3'
    if not path.exists():
        return
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'tasks' in names and conn.execute("SELECT 1 FROM tasks WHERE status IN ('RUNNING','QUEUED') LIMIT 1").fetchone():
                raise SetupError('存在运行或排队任务，请先完成或取消；向导未修改配置。')
            if 'project_locks' in names and conn.execute('SELECT 1 FROM project_locks LIMIT 1').fetchone():
                raise SetupError('存在保留锁，向导不会删锁或覆盖正在工作的配置。')
    except sqlite3.Error as exc:
        raise SetupError('无法确认数据库空闲；未修改配置。') from exc


@dataclass
class Change:
    path: Path
    before: bytes | None
    after: bytes


@dataclass
class Plan:
    repo: Path
    home: Path
    changes: list[Change]
    replaced_hooks: int


def plan_setup(repo, project, project_id, executables, home, hermes, *, replace_team=False):
    repo = check_path(repo)
    project = check_path(existing_absolute(str(project), directory=True))
    home = check_path(home)
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', project_id):
        raise SetupError('项目 ID 只允许小写字母、数字和连字符，最多 64 字符')
    if project == repo or project in repo.parents or repo in project.parents:
        raise SetupError('项目目录与控制仓库必须相互独立')
    if 'codex' not in executables:
        raise SetupError('当前受限执行适配需要 Codex')
    executables = {name: existing_absolute(str(path)) for name, path in executables.items()}
    hermes = existing_absolute(str(hermes))
    ensure_idle(repo)
    registry_path = repo/'local-config'/'registry.json'
    defaults = configuration(registry_path, [(project_id, project)], executables, template_root=repo)
    agents = read_json(repo/'dispatch'/'agents.json', defaults[0])
    routing = read_json(repo/'dispatch'/'routing.json', defaults[1])
    registry = read_json(registry_path, defaults[2])
    registry_value = routing.get('project_registry', str(registry_path))
    if not isinstance(registry_value, str) or not Path(registry_value).is_absolute():
        raise SetupError('现有注册中心路径必须是绝对路径')
    if Path(registry_value).resolve() != registry_path:
        raise SetupError('现有注册中心在其他位置。请先手动迁移到 local-config/registry.json，避免覆盖外部账本配置。')
    if registry.get('schema_version') != 1 or not isinstance(registry.get('projects'), list):
        raise SetupError('项目注册配置格式不受支持')
    selected = None
    for item in registry['projects']:
        if not isinstance(item, dict) or not isinstance(item.get('root'), str):
            raise SetupError('已有项目配置无效')
        if not Path(item['root']).is_absolute():
            raise SetupError('已有项目根目录必须是绝对路径')
        root = Path(item['root']).resolve()
        if item.get('id') == project_id:
            if root != project:
                raise SetupError('此项目 ID 已绑定另一个目录；请使用新 ID，避免改变历史任务归属。')
            selected = item
        elif root == project or root in project.parents or project in root.parents:
            raise SetupError('新项目与已有项目目录重叠')
    if selected is None:
        selected = deepcopy(defaults[2]['projects'][0])
        registry['projects'].append(selected)
    selected.update(status='active', agents=list(executables), default_agent='codex')
    if not isinstance(agents.get('agents'), dict) or any(not isinstance(entry, dict) for entry in agents['agents'].values()):
        raise SetupError('执行器配置必须是映射')
    for name, entry in agents['agents'].items():
        if name in defaults[0]['agents']:
            entry['enabled'] = name in executables
            if entry['enabled']:
                entry['executable'] = str(executables[name])
    if not isinstance(agents.get('agents'), dict) or not all(name in agents['agents'] for name in executables):
        raise SetupError('执行器配置不完整')
    routing['project_registry'] = str(registry_path)
    roots = routing.setdefault('project_whitelist', [])
    if not isinstance(roots, list):
        raise SetupError('project_whitelist 必须是列表')
    if not any(Path(value).resolve() == project for value in roots):
        roots.append(str(project))
    command = subprocess.list2cmdline([sys.executable, str(repo/'dispatch'/'team-policy.py')])
    config_path = home/'config.yaml'
    before_config = read_optional(config_path)
    config, replaced = merge_hooks(before_config, command, replace_team=replace_team)
    skill = (repo/'skills'/'team-project-manager'/'SKILL.md').read_text(encoding='utf-8')
    skill = skill.replace('C:\\path\\to\\hermes-team-v8', repo.as_posix())
    skill = skill.replace('D:\\\\example-workspace\\\\project-alpha', project.as_posix())
    skill = skill.replace('D:/example-workspace/project-alpha', project.as_posix())
    skill = '\n'.join(line for line in skill.splitlines() if '进入示例项目 B' not in line and not line.startswith('这是公开模板：'))
    skill = skill.replace('project-alpha', project_id)
    state = {'version': '8.1.0', 'project_id': project_id, 'project_root': str(project),
             'hermes_home': str(home), 'hermes_executable': str(hermes), 'python': sys.executable}
    targets = [(repo/'dispatch'/'agents.json', json_bytes(agents)),
               (repo/'dispatch'/'routing.json', json_bytes(routing)), (registry_path, json_bytes(registry)),
               (config_path, config), (home/'skills'/'team-project-manager'/'SKILL.md', (skill+'\n').encode('utf-8')),
               (repo/'local-config'/'setup-state.json', json_bytes(state))]
    changes = []
    for path, after in targets:
        path = check_path(path)
        before = read_optional(path)
        if before != after:
            changes.append(Change(path, before, after))
    return Plan(repo, home, changes, replaced)


def atomic_write(path, raw):
    check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.team-setup-'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def allowed_targets(repo, home):
    return {repo/'dispatch'/'agents.json', repo/'dispatch'/'routing.json',
            repo/'local-config'/'registry.json', repo/'local-config'/'setup-state.json',
            home/'config.yaml', home/'skills'/'team-project-manager'/'SKILL.md'}


def restore_backup(repo, backup):
    repo = check_path(repo)
    backup = check_path(backup)
    if backup.parent != repo/'local-config'/'backups':
        raise SetupError('只能恢复本仓库 local-config/backups 下的向导备份')
    data = json.loads((backup/'manifest.json').read_text(encoding='utf-8'))
    home = check_path(Path(data['hermes_home']))
    allowed = allowed_targets(repo, home)
    ensure_idle(repo)
    changes = []
    for item in data['files']:
        path = check_path(Path(item['path']))
        if path not in allowed:
            raise SetupError('备份包含未授权目标')
        current = read_optional(path)
        if digest(current) != item['after_sha256']:
            raise SetupError('安装后文件已被其他程序修改，停止恢复以保留新改动：'+str(path))
        original = None
        if item['backup_file'] is not None:
            saved = backup/item['backup_file']
            if saved.parent != backup:
                raise SetupError('无效备份文件名')
            original = saved.read_bytes()
            if digest(original) != item['before_sha256']:
                raise SetupError('备份校验失败')
        changes.append((path, original))
    for path, original in reversed(changes):
        if original is None:
            path.unlink()
        else:
            atomic_write(path, original)


def apply_plan(plan):
    ensure_idle(plan.repo)
    for change in plan.changes:
        if read_optional(change.path) != change.before:
            raise SetupError('预览后文件发生变化，请重新检测：'+str(change.path))
    if not plan.changes:
        return None
    backup = plan.repo/'local-config'/'backups'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    check_path(backup)
    backup.mkdir(parents=True)
    records = []
    for index, change in enumerate(plan.changes):
        saved = f'{index}.bak' if change.before is not None else None
        if saved:
            (backup/saved).write_bytes(change.before)
        records.append({'path': str(change.path), 'backup_file': saved,
                        'before_sha256': digest(change.before), 'after_sha256': digest(change.after)})
    (backup/'manifest.json').write_bytes(json_bytes({'hermes_home': str(plan.home), 'files': records}))
    written = []
    try:
        for change in plan.changes:
            if read_optional(change.path) != change.before:
                raise SetupError('文件在安装期间被修改：'+str(change.path))
            atomic_write(change.path, change.after)
            written.append(change)
    except BaseException:
        for change in reversed(written):
            if read_optional(change.path) == change.after:
                if change.before is None:
                    change.path.unlink()
                else:
                    atomic_write(change.path, change.before)
        raise
    return backup


@contextmanager
def maintenance(repo):
    flag = Path(repo)/'dispatch'/'.v71-maintenance'
    check_path(flag)
    marker = json_bytes({'owner': 'windows-setup', 'pid': os.getpid(), 'token': uuid.uuid4().hex})
    try:
        with flag.open('xb') as output:
            output.write(marker)
    except FileExistsError as exc:
        raise SetupError('已有维护或安装在进行；未覆盖别人的维护标志。') from exc
    try:
        ensure_idle(repo)
        yield
    finally:
        if flag.is_file() and flag.read_bytes() == marker:
            flag.unlink()


def perform_install(plan, project_id, hermes, *, verify=None):
    with maintenance(plan.repo):
        backup = apply_plan(plan)
        try:
            results = (verify or verify_installation)(plan.repo, plan.home, project_id, hermes)
        except BaseException as exc:
            if backup:
                restore_backup(plan.repo, backup)
            raise SetupError('安装自检失败，已恢复本次配置改动；数据库不覆盖旧快照。') from exc
        return backup, results


def hermes_env(home):
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONUTF8='1')
    env.pop('HERMES_CONFIG', None)
    env.pop('HERMES_PROFILE', None)
    return env


def validate_hermes_home(executable, home, *, run=native_run):
    outcome = run([executable, 'config', 'path'], env=hermes_env(home), capture_output=True,
                  text=True, encoding='utf-8', errors='replace', timeout=20)
    lines = (outcome.stdout or '').strip().splitlines()
    if outcome.returncode or not lines:
        raise SetupError('所选目录无法启动该 Hermes 运行环境；请使用已初始化的 Hermes 配置目录。未修改 Team 配置。')
    actual = Path(lines[-1].strip())
    if not actual.is_absolute() or actual.resolve() != (Path(home)/'config.yaml').resolve():
        raise SetupError('Hermes 实际配置位置与所选目录不一致；未写入配置。')


def diagnostic_status(label, outcome):
    result = {'exit_code': outcome.returncode,
              'status': 'passed' if outcome.returncode == 0 else 'needs_attention'}
    if label == 'hooks_doctor':
        # Some Hermes versions print an issue count but still exit zero. Only
        # its explicit healthy report confirms the newly configured Hook.
        output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', (outcome.stdout or '')+'\n'+(outcome.stderr or ''))
        healthy = 'All shell hooks look healthy.' in output
        if outcome.returncode != 0 or not healthy:
            result['status'] = 'needs_attention'
            result['reason'] = 'hook_trust_pending' if 'not allowlisted' in output else 'native_health_not_confirmed'
    return result


def verify_installation(repo, home, project_id, hermes, *, run=native_run):
    env = hermes_env(home)
    # The setup process owns the maintenance gate; regular Dispatcher mutations
    # stay blocked while this process applies the additive initialization.
    initialize = 'import sys;sys.path.insert(0,'+repr(str(repo/'dispatch'))+');import dispatcher;dispatcher.init_db();dispatcher.load_agents();dispatcher.load_routing()'
    completed = run([sys.executable, '-X', 'utf8', '-B', '-c', initialize],
                    env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    if completed.returncode:
        raise SetupError('Dispatcher 初始化失败；配置可从备份恢复。')
    results = {}
    for label, args in (('config_check', ['config', 'check']), ('hooks_doctor', ['hooks', 'doctor'])):
        outcome = run([hermes, *args], env=env, capture_output=True, text=True,
                      encoding='utf-8', errors='replace', timeout=30)
        results[label] = diagnostic_status(label, outcome)
    # Only this temporary binding is created; no model turns or business tasks.
    session = 'setup-probe-'+uuid.uuid4().hex
    db = repo/'dispatch'/'state.sqlite3'
    try:
        with closing(sqlite3.connect(db, isolation_level=None)) as conn:
            conn.execute('INSERT INTO session_projects VALUES (?,?,?)', (session, project_id, datetime.now(timezone.utc).isoformat()))
        command = 'python "'+str(repo/'dispatch'/'dispatcher.py')+'" queue'
        payload = dict(hook_event_name='pre_tool_call', tool_name='terminal', tool_input={'command': command}, session_id=session)
        outcome = run([sys.executable, '-X', 'utf8', '-B', repo/'dispatch'/'team-policy.py'], env=env,
                      input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8', timeout=15)
        decision = json.loads(outcome.stdout)
        results['hook_session_injection'] = decision.get('action') == 'modify' and session in decision.get('args', {}).get('command', '')
        if not results['hook_session_injection']:
            raise SetupError('Hook 会话注入自检失败')
        payload.update(tool_name='read_file', tool_input={'path': str(repo/'dispatch'/'dispatcher.py')})
        outcome = run([sys.executable, '-X', 'utf8', '-B', repo/'dispatch'/'team-policy.py'], env=env,
                      input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8', timeout=15)
        results['control_plane_read_blocked'] = json.loads(outcome.stdout).get('action') == 'block'
        if not results['control_plane_read_blocked']:
            raise SetupError('Hook 控制目录边界自检失败')
    finally:
        with closing(sqlite3.connect(db, isolation_level=None)) as conn:
            conn.execute('DELETE FROM session_projects WHERE session_id=?', (session,))
    return results


def ask(text, default=''):
    value = input(text+(f' [{default}]' if default else '')+'：').strip().strip('"')
    return value or default


def choose_tool(name, detected, required):
    current = detected.get(name)
    while True:
        if current:
            print(f"{name}: {current['version']}  {current['path']}")
            choice = ask('使用此执行器？y=使用，p=手动路径'+('，n=禁用' if not required else ''), 'y').lower()
            if choice == 'y':
                return Path(current['path'])
            if choice == 'n' and not required:
                return None
        else:
            print(f'{name} 未检测到可运行的原生 exe。官方安装说明：{INSTALL_LINKS[name]}')
        raw = ask('输入 exe 绝对路径，安装后可输入路径重试'+('，留空禁用' if not required else '（必需）'))
        if not raw and not required:
            return None
        current = probe_cli(raw, kind=name) if raw else None
        if not current:
            print('未找到或版本命令失败。不要选择 .cmd / .ps1 包装脚本。')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect', action='store_true', help='只读检测，不安装或修改配置')
    parser.add_argument('--restore', type=Path, help='恢复本仓库的指定向导备份；不覆盖安装后的新改动')
    parser.add_argument('--launch', action='store_true', help='按已保存的本机设置启动 Hermes，使用原生信任提示')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('此向导仅支持 Windows')
    installed = False
    try:
        if args.restore:
            with maintenance(REPO):
                restore_backup(REPO, args.restore)
            print('配置已恢复；账本和账号文件未回滚。')
            return 0
        if args.launch:
            state = json.loads((REPO/'local-config'/'setup-state.json').read_text(encoding='utf-8'))
            return subprocess.call([state['hermes_executable']], env=hermes_env(state['hermes_home']), shell=False)
        detected = detect_tools()
        if args.inspect:
            print(json.dumps({'python': sys.version.split()[0], 'tools': detected,
                              'hermes_home': str(hermes_home_for(detected.get('hermes', {}).get('path')))}, ensure_ascii=True, indent=2))
            return 0
        print('\nHermes Team V8.1 Windows 安装配置向导\n检测不会发起模型任务，也不会读取登录密钥。')
        print('Python: '+sys.version.split()[0])
        hermes = choose_tool('hermes', detected, True)
        executables = {'codex': choose_tool('codex', detected, True)}
        for name in ('claude', 'agy'):
            selected = choose_tool(name, detected, False)
            if selected:
                executables[name] = selected
        home = Path(ask('Hermes 配置目录', str(hermes_home_for(hermes))))
        if not home.is_absolute():
            raise SetupError('Hermes 配置目录必须是绝对路径')
        validate_hermes_home(hermes, home)
        print('请先关闭正在使用该配置的 Hermes 对话，避免写入竞争。向导不会结束其他进程。')
        project = Path(ask('已有业务项目的绝对目录（与此仓库分开）'))
        project_id = ask('项目 ID（小写字母、数字、连字符）', 'project-alpha')
        replace_team = False
        try:
            plan = plan_setup(REPO, project, project_id, executables, home, hermes)
        except SetupError as exc:
            if '发现其他 Team Policy Hook' not in str(exc):
                raise
            print(str(exc))
            replace_team = ask('迁移旧 Team Hook 到此仓库？只替换 Team Hook，保留其他规则 [y/N]', 'n').lower() == 'y'
            if not replace_team:
                print('已取消，配置未修改。')
                return 0
            plan = plan_setup(REPO, project, project_id, executables, home, hermes, replace_team=True)
        print('\n将更新以下文件（原内容先备份，不显示凭据）：')
        for change in plan.changes:
            print(('  新建 ' if change.before is None else '  更新 ')+str(change.path))
        if not plan.changes:
            print('配置已一致，无需重写文件或刷新 Hook 时间戳。')
        if plan.replaced_hooks:
            print('将迁移旧 Team Hook：'+str(plan.replaced_hooks)+' 条')
        if ask('确认备份并应用配置？[y/N]', 'n').lower() != 'y':
            print('已取消，配置未修改。')
            return 0
        backup, results = perform_install(plan, project_id, hermes)
        installed = True
        print('\n配置与本地 Hook 注入自检完成。')
        if backup:
            print('本机备份：'+str(backup))
        print('Hermes 检查：'+json.dumps(results, ensure_ascii=False))
        print('请在 Hermes 的原生提示中确认新 Hook，随后运行 hermes hooks doctor。向导不会写信任记录。')
        print('Codex / Claude / AGY 的账号登录及主控模型由各自官方界面配置；本次未发起模型请求。')
        print('以后可双击 start-hermes.cmd 使用当前配置。')
        if ask('现在启动 Hermes，完成原生 Hook 信任与主控设置？[y/N]', 'n').lower() == 'y':
            return subprocess.call([str(hermes)], env=hermes_env(plan.home), shell=False)
        return 0
    except (SetupError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print('未完成：'+str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\n配置已完成，已取消启动。' if installed else '\n已取消。')
        return 0 if installed else 130


if __name__ == '__main__':
    raise SystemExit(main())
