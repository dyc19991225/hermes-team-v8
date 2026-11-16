#!/usr/bin/env python3
"""Hermes v0.23.0 pre_tool_call fail-closed policy hook for Team V7.

Python standard library only. Compatible with Windows paths.
Stdin JSON: {hook_event_name, tool_name, tool_input, cwd, session_id, extra, ...}
Stdout JSON:
  Allow: {}
  Block: {"action": "block", "message": "Policy violation: ..."}
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

DEFAULT_DISPATCH_ROOT = Path(__file__).resolve().parent

READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "search_files",
        "skills_list",
        "skill_view",
        "session_search",
        "read_terminal",
        "web_search",
        "web_extract",
        "vision_analyze",
        "clarify",
    }
)

FORBIDDEN_CLI_MARKERS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--yolo",
    "--accept-hooks",
    "hermes_accept_hooks",
    "bypasspermissions",
    "hermes -z",
)

SHELL_CHAIN_CHARS = ("\n", "\r", "\x00", ";", "&", "|", ">", "<", "`", "$(")


def allow() -> dict[str, Any]:
    return {}


def block(reason: str) -> dict[str, Any]:
    return {"action": "block", "message": f"Policy violation: {reason}"}


def _norm_case_real(p: Path) -> Path:
    resolved = Path(os.path.realpath(str(p.resolve(strict=False))))
    return Path(os.path.normcase(str(resolved)))


def _resolve_candidate_path(raw_path: Any, cwd: Any) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    text = raw_path.strip()
    p = Path(text).expanduser()
    if not p.is_absolute():
        if not isinstance(cwd, str) or not cwd.strip():
            return None
        base = Path(cwd.strip()).expanduser()
        if not base.is_absolute():
            return None
        p = base / p
    # Reject explicit symlink pointing outside when target or parent exists
    return _norm_case_real(p)


def _is_direct_child_of(candidate: Path, parent_dir: Path) -> bool:
    return _norm_case_real(candidate.parent) == _norm_case_real(parent_dir)


def _is_within_dir(candidate: Path, root_dir: Path) -> bool:
    c = _norm_case_real(candidate)
    r = _norm_case_real(root_dir)
    return c == r or r in c.parents


def _is_valid_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    try:
        parsed = uuid.UUID(s)
    except (ValueError, AttributeError):
        return False
    return str(parsed) == s.lower()


def _load_json_dict(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def _split_command(cmd: str) -> list[str]:
    out: list[str] = []
    for tok in shlex.split(cmd, posix=(os.name != "nt")):
        if os.name == "nt" and len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
            tok = tok[1:-1]
        out.append(tok)
    return out


def _validate_write_file(tool_input: dict[str, Any], cwd: Any, dispatch_root: Path) -> dict[str, Any]:
    raw_path = tool_input.get("path")
    candidate = _resolve_candidate_path(raw_path, cwd)
    if candidate is None:
        return block("write_file requires a valid path")

    inbox_dir = _norm_case_real(dispatch_root / "inbox")
    if not _is_direct_child_of(candidate, inbox_dir):
        return block("write_file is restricted to direct .json task files in dispatch/inbox")
    if candidate.suffix.lower() != ".json":
        return block("write_file in dispatch/inbox only permits .json task files")

    # Reject if target itself already exists as a symlink
    raw_obj = Path(str(raw_path)).expanduser()
    if not raw_obj.is_absolute() and isinstance(cwd, str) and cwd.strip():
        raw_obj = Path(cwd.strip()) / raw_obj
    if raw_obj.is_symlink():
        return block("symlink target rejected")

    content = tool_input.get("content")
    if not isinstance(content, str) or not content.strip():
        return block("write_file task JSON content must be a non-empty string")
    try:
        task_spec = json.loads(content)
    except json.JSONDecodeError as exc:
        return block(f"write_file content is not valid JSON: {exc}")
    if not isinstance(task_spec, dict):
        return block("write_file task JSON must be an object")

    agents_cfg = _load_json_dict(dispatch_root / "agents.json")
    routing_cfg = _load_json_dict(dispatch_root / "routing.json")

    agent = task_spec.get("agent")
    agents = agents_cfg.get("agents", {})
    if not isinstance(agent, str) or agent not in agents or not agents[agent].get("enabled", False):
        return block(f"task JSON specifies unknown or disabled agent: {agent!r}")

    proj = task_spec.get("project_root")
    proj_norm = _resolve_candidate_path(proj, cwd)
    allowed_roots = {
        _norm_case_real(Path(p))
        for p in routing_cfg.get("project_whitelist", [])
        if isinstance(p, str) and p.strip()
    }
    if proj_norm is None or proj_norm not in allowed_roots:
        return block(f"task JSON project_root not in whitelist: {proj!r}")

    task_types = routing_cfg.get("task_types", {})
    task_type = task_spec.get("task_type")
    if task_type not in task_types:
        return block(f"task JSON invalid task_type: {task_type!r}")

    risk_levels = routing_cfg.get("risk_levels", {})
    risk_level = task_spec.get("risk_level")
    if risk_level not in risk_levels:
        return block(f"task JSON invalid risk_level: {risk_level!r}")

    exec_mode = task_spec.get("execution_mode")
    if exec_mode not in agents[agent].get("execution_modes", {}):
        return block(f"task JSON execution_mode {exec_mode!r} invalid for agent {agent!r}")
    if exec_mode not in task_types[task_type].get("allowed_modes", []):
        return block(f"task JSON execution_mode {exec_mode!r} not allowed for {task_type!r}")

    approval_state = task_spec.get("approval_state", "not_required")
    if approval_state not in routing_cfg.get("approval_states", []):
        return block(f"task JSON invalid approval_state: {approval_state!r}")
    if risk_levels[risk_level].get("requires_approval") and approval_state == "approved":
        return block("task JSON cannot self-approve a high-risk task via write_file")

    idem = task_spec.get("idempotency_key")
    if not isinstance(idem, str) or not idem.strip():
        return block("task JSON requires non-empty idempotency_key")

    prompt = task_spec.get("prompt")
    prompt_path = task_spec.get("prompt_path")
    if not (isinstance(prompt, str) and prompt.strip()) and not (isinstance(prompt_path, str) and prompt_path.strip()):
        return block("task JSON requires prompt or prompt_path")

    return allow()


def _check_sqlite_task(
    task_id: str,
    subcommand: str,
    dispatch_root: Path,
) -> dict[str, Any] | None:
    if subcommand in ('run','dry-run') and (dispatch_root / '.v71-maintenance').exists():
        return block('dispatcher maintenance in progress')
    if not _is_valid_uuid(task_id):
        return block(f"invalid task UUID: {task_id!r}")

    db_path = _norm_case_real(dispatch_root / "state.sqlite3")
    if not db_path.is_file():
        return block("dispatcher state.sqlite3 does not exist")

    agents_cfg = _load_json_dict(dispatch_root / "agents.json")
    routing_cfg = _load_json_dict(dispatch_root / "routing.json")

    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id.strip().lower(),)).fetchone()
        if row is None:
            return block(f"task {task_id} not found in state.sqlite3")

        agent = row["agent"]
        agents = agents_cfg.get("agents", {})
        if agent not in agents or not agents[agent].get("enabled", False):
            return block(f"task agent {agent!r} not in whitelist")

        proj_norm = _resolve_candidate_path(row["project_root"], None)
        allowed_roots = {
            _norm_case_real(Path(p))
            for p in routing_cfg.get("project_whitelist", [])
            if isinstance(p, str) and p.strip()
        }
        if proj_norm is None or proj_norm not in allowed_roots:
            return block(f"task project_root {row['project_root']!r} not in whitelist")

        if subcommand == 'cancel':
            if row['status'] in ('SUCCEEDED','FAILED','CANCELLED'):
                return block('cannot cancel terminal task')
            # Cancellation grants no execution or writing authority. The dispatcher
            # verifies process identity/token and keeps the lock until the tree stops.
            return None

        risk_level = row["risk_level"]
        risk_cfg = routing_cfg.get("risk_levels", {}).get(risk_level)
        if not isinstance(risk_cfg, dict):
            return block(f"task risk_level {risk_level!r} invalid")

        approval_state = row["approval_state"]
        if risk_cfg.get("requires_approval", True):
            if approval_state != "approved":
                return block(f"high-risk task {task_id} is not approved (state={approval_state!r})")
        elif approval_state in ("pending", "denied"):
            return block(f"task {task_id} has non-runnable approval_state={approval_state!r}")

        if subcommand in ("dry-run", "run"):
            if row["status"] not in ("CREATED", "DRY_RUN", "QUEUED"):
                return block(f"task {task_id} status {row['status']!r} cannot enter {subcommand}")

            task_type = row["task_type"]
            tt_meta = routing_cfg.get("task_types", {}).get(task_type, {})
            if tt_meta.get("write"):
                lock_row = conn.execute(
                    "SELECT task_id FROM project_locks WHERE project_root = ?",
                    (row["project_root"],),
                ).fetchone()
                if lock_row and lock_row["task_id"] != task_id.strip().lower():
                    return block(f"project lock held by {lock_row['task_id']}")

        return None
    finally:
        conn.close()


def _validate_dispatcher_argv(tokens: list[str], cwd: Any, dispatch_root: Path) -> dict[str, Any]:
    if len(tokens) < 3:
        return block("incomplete dispatcher command")

    exe_name = Path(tokens[0]).name.lower()
    if exe_name not in ("python", "python.exe", "python3", "python3.exe"):
        return block(f"dispatcher must be invoked via python, got {tokens[0]!r}")

    script_path = _resolve_candidate_path(tokens[1], cwd)
    expected_script = _norm_case_real(dispatch_root / "dispatcher.py")
    if script_path != expected_script:
        return block("dispatcher script path mismatch")

    subcmd = tokens[2]
    rest = tokens[3:]
    if (dispatch_root / '.v71-maintenance').exists() and subcmd not in ('status','list'):
        return block('dispatcher maintenance in progress')

    if subcmd in ("init", "list"):
        if rest:
            return block(f"unexpected args for dispatcher {subcmd}")
        return allow()

    if subcmd in ("dry-run", "run", "status", "cancel"):
        task_id: str | None = None
        seen=set()
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok in seen: return block('duplicate dispatcher flag')
            seen.add(tok)
            if tok == "--task-id" and i + 1 < len(rest):
                task_id = rest[i + 1]
                i += 2
            elif tok == "--timeout" and subcmd in ("dry-run", "run") and i + 1 < len(rest):
                if not rest[i + 1].isdigit():
                    return block("invalid --timeout value")
                i += 2
            else:
                return block(f"unsupported or disallowed dispatcher flag: {tok!r}")
        if not task_id:
            return block(f"dispatcher {subcmd} requires --task-id <uuid>")
        err = _check_sqlite_task(task_id, subcmd, dispatch_root)
        return err if err is not None else allow()

    if subcmd == "create":
        if len(rest) != 2 or rest[0] != "--file":
            return block("dispatcher create only accepts --file <inbox_json>")
        candidate = _resolve_candidate_path(rest[1], cwd)
        inbox_dir = _norm_case_real(dispatch_root / "inbox")
        if candidate is None or not _is_direct_child_of(candidate, inbox_dir) or candidate.suffix.lower() != ".json":
            return block("dispatcher create --file must be a direct .json inside dispatch/inbox")
        return allow()

    return block(f"unsupported dispatcher subcommand: {subcmd!r}")


def _validate_readonly_argv(tokens: list[str], cwd: Any, dispatch_root: Path) -> dict[str, Any]:
    if not tokens:
        return block("empty command")
    cmd = Path(tokens[0]).name.lower()
    if cmd.endswith(".exe"):
        cmd = cmd[:-4]
    args = tokens[1:]

    if cmd == "pwd":
        return allow() if not args else block("pwd takes no arguments")

    if cmd == "hermes":
        if args in (["--version"], ["hooks", "list"], ["hooks", "ls"], ["hooks", "doctor"], ["config", "check"]):
            return allow()
        if len(args) == 3 and args[0] == "config" and args[1] == "get" and not args[2].startswith("-"):
            return allow()
        return block("hermes command restricted to read-only inspection")

    if cmd == "codex":
        if args in (["--version"], ["--help"], ["exec", "--help"], ["login", "status"]):
            return allow()
        return block("codex terminal invocation restricted to help/version/status")

    if cmd == "claude":
        if args in (["--version"], ["--help"], ["auth", "status"]):
            return allow()
        return block("claude terminal invocation restricted to help/version/status")

    if cmd == "agy":
        if args in (["--version"], ["--help"], ["models"]):
            return allow()
        return block("agy terminal invocation restricted to help/version/models")

    if cmd == "git":
        routing_cfg = _load_json_dict(dispatch_root / "routing.json")
        allowed_roots = {
            _norm_case_real(Path(p))
            for p in routing_cfg.get("project_whitelist", [])
            if isinstance(p, str) and p.strip()
        }
        idx = 0
        if len(args) >= 2 and args[0] == "-C":
            cpath = _resolve_candidate_path(args[1], cwd)
            if cpath is None or cpath not in allowed_roots:
                return block(f"git -C path not in whitelist: {args[1]!r}")
            idx = 2
        if idx >= len(args):
            return block("git requires subcommand")
        sub = args[idx]
        sub_args = args[idx + 1 :]
        if sub not in ("status", "branch", "diff", "log", "show", "rev-parse"):
            return block(f"git subcommand {sub!r} is not read-only")
        for tok in sub_args:
            low = tok.lower()
            if low in ("-d", "-f", "--force", "--delete", "-m", "-c", "--exec", "--ext-diff", "--textconv", "--output"):
                return block(f"unsafe git flag {tok!r}")
        if sub == "branch" and any(not t.startswith("-") for t in sub_args):
            return block("git branch mutation disallowed")
        return allow()

    if cmd in ("ls", "dir"):
        for tok in args:
            if tok.startswith("-"):
                continue
            p = _resolve_candidate_path(tok, cwd)
            if p is None:
                return block("invalid path for directory listing")
            routing_cfg = _load_json_dict(dispatch_root / "routing.json")
            allowed = [_norm_case_real(Path(x)) for x in routing_cfg.get("project_whitelist", [])]
            allowed.append(_norm_case_real(dispatch_root))
            if not any(_is_within_dir(p, root) for root in allowed):
                return block(f"listing outside allowed roots: {tok!r}")
        return allow()

    return block(f"terminal command not in read-only or dispatcher allowlist: {cmd!r}")


def _validate_terminal(tool_input: dict[str, Any], cwd: Any, dispatch_root: Path) -> dict[str, Any]:
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return block("terminal command must be a non-empty string")
    if any(ch in cmd for ch in SHELL_CHAIN_CHARS):
        return block("shell chaining, piping, redirection, or multiline commands are blocked")

    low_cmd = cmd.lower()
    for bad in FORBIDDEN_CLI_MARKERS:
        if bad in low_cmd:
            return block(f"forbidden CLI option detected: {bad}")

    try:
        tokens = _split_command(cmd)
    except ValueError as exc:
        return block(f"unparseable command line: {exc}")
    if not tokens:
        return block("empty parsed command")

    # Check if targeting dispatcher.py
    if len(tokens) >= 2:
        cand = _resolve_candidate_path(tokens[1], cwd)
        if cand is not None and cand == _norm_case_real(dispatch_root / "dispatcher.py"):
            return _validate_dispatcher_argv(tokens, cwd, dispatch_root)

    return _validate_readonly_argv(tokens, cwd, dispatch_root)


def _validate_delegate_task(tool_input: dict[str, Any], dispatch_root: Path) -> dict[str, Any]:
    action = tool_input.get("action", "spawn")
    if action == "list":
        return allow()
    if action == "spawn":
        tasks = tool_input.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return block("delegate_task spawn requires non-empty tasks linked to Dispatcher")
        for entry in tasks:
            if not isinstance(entry, dict):
                return block("invalid delegate_task item")
            ctx = str(entry.get("context", ""))
            # Must explicitly carry DISPATCH_TASK:<uuid> that exists & is authorized in SQLite
            marker = "DISPATCH_TASK:"
            if marker not in ctx:
                return block("delegate_task cannot bypass Dispatcher (missing DISPATCH_TASK:<uuid>)")
            after = ctx.split(marker, 1)[1].strip().split()[0]
            err = _check_sqlite_task(after, "run", dispatch_root)
            if err is not None:
                return err
        return allow()
    return block(f"delegate_task action {action!r} cannot bypass Dispatcher")


def evaluate_hook(payload: dict[str, Any], dispatch_root: Path = DEFAULT_DISPATCH_ROOT) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return block("payload must be a JSON object")
    if payload.get("hook_event_name") != "pre_tool_call":
        return block(f"unexpected hook_event_name: {payload.get('hook_event_name')!r}")

    sys.path.insert(0,str(dispatch_root))
    try:
        from project_manager import guard_hook
        decision=guard_hook(payload,dispatch_root)
        if decision is not None and decision.get('action')=='modify':
            checked=evaluate_hook(dict(payload,tool_input=decision['args']),dispatch_root)
            return decision if checked=={} else checked
        if decision is not None:return decision
    except Exception as exc:return block('V8 fail-closed: '+str(exc))
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return block("missing tool_name")

    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        return block("tool_input must be a JSON object")

    cwd = payload.get("cwd")

    if tool_name in READ_ONLY_TOOLS:
        return allow()
    if tool_name == "write_file":
        return _validate_write_file(tool_input, cwd, dispatch_root)
    if tool_name == "patch":
        return block("direct business or file modification via patch is blocked by V7 policy")
    if tool_name == "terminal":
        return _validate_terminal(tool_input, cwd, dispatch_root)
    if tool_name == "delegate_task":
        return _validate_delegate_task(tool_input, dispatch_root)

    # All other write/execution/mutation tools default to block (fail-closed)
    return block(f"tool {tool_name!r} is blocked by default fail-closed policy")


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        decision = evaluate_hook(payload, DEFAULT_DISPATCH_ROOT)
    except Exception as exc:
        decision = block(f"fail-closed hook exception: {exc}")
    sys.stdout.write(json.dumps(decision, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
