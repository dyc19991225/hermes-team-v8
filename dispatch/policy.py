#!/usr/bin/env python3
"""Policy validation & safe CLI command builder for Hermes Team V8.1 Dispatcher.
Python standard library only.
"""

from __future__ import annotations

import json
import os
import uuid
import project_manager as pm
from pathlib import Path
from typing import Any

DISPATCH_ROOT = Path(__file__).resolve().parent
AGENTS_PATH = DISPATCH_ROOT / "agents.json"
ROUTING_PATH = DISPATCH_ROOT / "routing.json"

VALID_STATUSES = (
    "CREATED",
    "DRY_RUN",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "BLOCKED",
    "AWAITING_APPROVAL",
    "CANCELLED",
)

FORBIDDEN_SUBSTRINGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--yolo",
    "bypassPermissions",
)


class PolicyError(ValueError):
    """Raised when a task or execution request violates dispatch policy."""


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise PolicyError(f"Expected JSON object in {path}")
    return data


def load_agents(path: Path = AGENTS_PATH) -> dict[str, Any]:
    return load_json(path)


def load_routing(path: Path = ROUTING_PATH) -> dict[str, Any]:
    cfg=load_json(path)
    if not cfg.get('project_registry'):
        raise PolicyError('V8 production registry configuration missing')
    return cfg


def normalize_path(raw: str | os.PathLike[str]) -> Path:
    if not isinstance(raw, (str, os.PathLike)):
        raise PolicyError("Path must be string or PathLike")
    text = str(raw).strip()
    if not text:
        raise PolicyError("Empty path is not allowed")
    # Resolve symlinks and relative segments without requiring target to exist
    p = Path(text).expanduser()
    if not p.is_absolute():
        raise PolicyError(f"Relative path rejected by fail-closed policy: {text}")
    return Path(os.path.normcase(str(p.resolve(strict=False))))


def is_path_within(candidate: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    try:
        c = normalize_path(candidate)
        r = normalize_path(root)
    except PolicyError:
        return False
    return c == r or r in c.parents


def is_valid_uuid(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        return False
    return str(parsed) == value.strip().lower()


def validate_project_root(project_root: str, routing_cfg: dict[str, Any]) -> Path:
    norm_target = normalize_path(project_root)
    whitelist = routing_cfg.get("project_whitelist", [])
    for allowed in whitelist:
        try:
            norm_allowed = normalize_path(allowed)
        except PolicyError:
            continue
        if norm_target == norm_allowed:
            return norm_target
    raise PolicyError(f"Project root not in whitelist: {project_root}")


def validate_agent(agent: str, agents_cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(agent, str) or not agent.strip():
        raise PolicyError("Agent name is required")
    agent_id = agent.strip()
    agents = agents_cfg.get("agents", {})
    if agent_id not in agents:
        raise PolicyError(f"Agent not in whitelist: {agent_id}")
    spec = agents[agent_id]
    if not spec.get("enabled", False):
        raise PolicyError(f"Agent is disabled: {agent_id}")
    exe = spec.get("executable")
    if not isinstance(exe, str) or not Path(exe).is_file():
        raise PolicyError(f"Agent executable not found: {exe}")
    return spec


def validate_task_spec(
    spec: dict[str, Any],
    agents_cfg: dict[str, Any] | None = None,
    routing_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    agents_cfg = agents_cfg or load_agents()
    routing_cfg = routing_cfg or load_routing()

    if not isinstance(spec, dict):
        raise PolicyError("Task specification must be a JSON object")

    task_id = spec.get("task_id")
    if task_id is not None and not is_valid_uuid(str(task_id)):
        raise PolicyError(f"Invalid task_id UUID: {task_id}")

    idempotency_key = spec.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise PolicyError("idempotency_key is required and must be a non-empty string")

    agent_name = spec.get("agent")
    agent_spec = validate_agent(agent_name, agents_cfg)

    project_root = spec.get("project_root")
    if not isinstance(project_root, str):
        raise PolicyError("project_root must be a string")
    norm_root = validate_project_root(project_root, routing_cfg)

    if routing_cfg.get('project_registry'):
        try:pm.validate(spec,routing_cfg)
        except pm.ProjectError as exc:raise PolicyError(str(exc)) from exc
    task_types = routing_cfg.get("task_types", {})
    task_type = spec.get("task_type")
    if task_type not in task_types:
        raise PolicyError(f"Unsupported task_type: {task_type}")
    tt_meta = task_types[task_type]
    if routing_cfg.get('project_registry') and not tt_meta.get('write') and spec.get('execution_mode') not in ('read_only','plan'):
        raise PolicyError('Non-writing tasks require read_only/plan mode')

    risk_levels = routing_cfg.get("risk_levels", {})
    risk_level = spec.get("risk_level")
    if risk_level not in risk_levels:
        raise PolicyError(f"Unsupported risk_level: {risk_level}")
    rank={'low':0,'medium':1,'high':2}
    if rank.get(risk_level,-1)<rank.get(tt_meta.get('min_risk_level','low'),0):
        raise PolicyError('Task risk is below its type minimum')

    execution_mode = spec.get("execution_mode")
    agent_modes = agent_spec.get("execution_modes", {})
    if execution_mode not in agent_modes:
        raise PolicyError(
            f"execution_mode {execution_mode!r} not supported by agent {agent_name!r}"
        )
    if execution_mode not in tt_meta.get("allowed_modes", []):
        raise PolicyError(
            f"execution_mode {execution_mode!r} not allowed for task_type {task_type!r}"
        )

    approval_states = routing_cfg.get("approval_states", [])
    approval_state = spec.get("approval_state", "not_required")
    if approval_state not in approval_states:
        raise PolicyError(f"Invalid approval_state: {approval_state}")

    timeout = spec.get("timeout", agent_spec.get("default_timeout_seconds", 120))
    max_t = int(agent_spec.get("max_timeout_seconds", 600))
    if isinstance(timeout,bool) or not isinstance(timeout, (int, float)) or timeout < 1 or timeout > max_t:
        raise PolicyError(f"Timeout {timeout} out of range (1..{max_t})")

    prompt_text = spec.get("prompt")
    prompt_path = spec.get("prompt_path")
    if not (isinstance(prompt_text, str) and prompt_text.strip()) and not (
        isinstance(prompt_path, str) and prompt_path.strip()
    ):
        raise PolicyError("Either prompt or prompt_path must be provided")

    return {
        "project_id": spec.get("project_id"),
        "task_id": task_id.strip().lower() if isinstance(task_id, str) else None,
        "idempotency_key": idempotency_key.strip(),
        "agent": agent_name.strip(),
        "project_root": str(norm_root),
        "task_type": task_type,
        "risk_level": risk_level,
        "execution_mode": execution_mode,
        "approval_state": approval_state,
        "timeout": int(timeout),
        "is_write": bool(tt_meta.get("write", False)),
    }


def check_execution_authorization(
    task_row: dict[str, Any],
    agents_cfg: dict[str, Any] | None = None,
    routing_cfg: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    agents_cfg = agents_cfg or load_agents()
    routing_cfg = routing_cfg or load_routing()

    if not is_valid_uuid(str(task_row.get("task_id", ""))):
        return False, "Invalid task UUID"

    try:
        agent_spec=validate_agent(str(task_row.get("agent", "")), agents_cfg)
        validate_project_root(str(task_row.get("project_root", "")), routing_cfg)
        if routing_cfg.get('project_registry'):
            pm.validate(task_row,routing_cfg)
        tt=routing_cfg.get('task_types',{}).get(task_row.get('task_type'))
        mode=task_row.get('execution_mode')
        if not tt or mode not in tt.get('allowed_modes',[]) or mode not in agent_spec.get('execution_modes',{}):
            return False,'Invalid task type or execution mode'
        if routing_cfg.get('project_registry') and not tt.get('write') and mode not in ('read_only','plan'):
            return False,'Non-writing tasks require read_only/plan mode'
        rank={'low':0,'medium':1,'high':2}
        if rank.get(task_row.get('risk_level'),-1)<rank.get(tt.get('min_risk_level','low'),0):
            return False,'Task risk below type minimum'
    except (PolicyError,pm.ProjectError) as exc:
        return False, str(exc)

    risk_level = task_row.get("risk_level")
    risk_cfg = routing_cfg.get("risk_levels", {}).get(risk_level)
    if not isinstance(risk_cfg, dict):
        return False, f"Unknown risk_level: {risk_level}"

    approval_state = task_row.get("approval_state")
    if risk_cfg.get("requires_approval", True):
        if approval_state != "approved":
            return False, f"High-risk task requires approval_state='approved' (got {approval_state!r})"
    else:
        if approval_state in ("pending", "denied"):
            return False, f"Task approval_state is {approval_state!r}"

    return True, "ok"


def build_cli_invocation(
    task_row: dict[str, Any],
    prompt_text: str,
    agents_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return {argv, stdin_text, cwd, timeout, json_mode} without shell=True."""
    agents_cfg = agents_cfg or load_agents()
    agent_id = task_row["agent"]
    spec = validate_agent(agent_id, agents_cfg)
    exe = str(Path(spec["executable"]).resolve())
    mode_name = task_row["execution_mode"]
    mode_cfg = spec["execution_modes"][mode_name]
    cwd = str(normalize_path(task_row["project_root"]))
    raw_timeout=task_row.get('timeout')
    if raw_timeout is None: raw_timeout=spec['default_timeout_seconds']
    if isinstance(raw_timeout,bool) or not isinstance(raw_timeout,(int,float)) or raw_timeout <= 0 or raw_timeout > int(spec['max_timeout_seconds']):
        raise PolicyError('Timeout outside agent bounds')
    timeout=int(raw_timeout)
    if timeout<1: raise PolicyError('Timeout must be at least one second')

    stdin_text: str | None = None
    if agent_id == "codex":
        sandbox = mode_cfg["sandbox"]
        argv = [
            exe,
            "exec",
            "-C",
            cwd,
            "-s",
            sandbox,
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "-",
        ]
        stdin_text = prompt_text
        json_mode = "jsonl"
    elif agent_id == "claude":
        perm_mode = mode_cfg["permission_mode"]
        if perm_mode not in ('plan','manual','acceptEdits'):
            raise PolicyError('Unverified or unsafe Claude permission mode')
        argv = [
            exe,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            perm_mode,
            "--no-session-persistence",
            "--permission-prompts",
            "none",
        ]
        if "tools_restrict" in mode_cfg:
            argv.extend(["--tools", mode_cfg["tools_restrict"]])
        # Pass prompt via stdin so variadic --tools never consumes positional prompt
        # and Windows 32KB command line limits are avoided.
        stdin_text = prompt_text
        json_mode = "jsonl"
    elif agent_id == "agy":
        argv = [
            exe,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--print-timeout",
            f"{timeout}s",
        ]
        # stream-json selects headless mode and reads one complete user message.
        # AGY text print mode intentionally ignores stdin on current releases.
        stdin_text = json.dumps({'event':'user','message':{'content':prompt_text}},ensure_ascii=False)+'\n'
        if mode_cfg.get("sandbox"):
            argv.append("--sandbox")
        if mode_cfg.get("mode"):
            argv.extend(["--mode", mode_cfg["mode"]])
        model = task_row.get("model") or (
            spec.get("review_model")
            if task_row.get("task_type") in ("code_review", "security_audit")
            else spec.get("default_model")
        )
        if model:
            argv.extend(["--model", model])
        json_mode = "jsonl"
    else:
        raise PolicyError(f"Unsupported agent: {agent_id}")

    # Final safety guard: reject forbidden permission-bypass flags in argv
    for token in argv:
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad in token:
                raise PolicyError(f"Forbidden flag detected in argv: {bad}")

    if os.name == 'nt' and len(__import__('subprocess').list2cmdline(argv)) > 30000:
        raise PolicyError('Windows command line exceeds the safe argument budget')
    try:
        (stdin_text or '').encode('utf-8')
    except UnicodeError as exc:
        raise PolicyError('Prompt contains invalid Unicode') from exc

    return {
        "argv": argv,
        "stdin_text": stdin_text,
        "cwd": cwd,
        "timeout": timeout,
        "json_mode": json_mode,
    }
