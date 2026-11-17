#!/usr/bin/env python3
"""Hermes Team V8.1 Task Dispatcher (Python stdlib only).

Manages task lifecycle in SQLite (WAL, foreign keys, busy_timeout, atomic transactions,
project write locks) and executes approved CLI agents with shell=False.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import project_manager as pm
from process_runtime import execute_streaming, process_identity, UnsafeCleanup

from policy import (
    DISPATCH_ROOT,
    PolicyError,
    build_cli_invocation,
    is_valid_uuid,
    load_agents,
    load_routing,
    validate_task_spec,
)
import workflow as wf

DEFAULT_DB_PATH = DISPATCH_ROOT / "state.sqlite3"
PROMPTS_DIR = DISPATCH_ROOT / "prompts"
RESULTS_DIR = DISPATCH_ROOT / "results"
LEASE_SECONDS = 30

class ResourceBusy(PolicyError):
    """Valid work is waiting for an owned worker slot, not an execution failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = connect_db(db_path)
    ddl_statements = (
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            project_root TEXT NOT NULL,
            agent TEXT NOT NULL,
            task_type TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'CREATED', 'DRY_RUN', 'QUEUED', 'RUNNING',
                'SUCCEEDED', 'FAILED', 'BLOCKED', 'AWAITING_APPROVAL', 'CANCELLED'
            )),
            prompt_path TEXT NOT NULL,
            result_path TEXT,
            exit_code INTEGER,
            duration REAL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            approval_state TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS task_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            at TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            detail TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_locks (
            project_root TEXT PRIMARY KEY,
            task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
            lock_mode TEXT NOT NULL DEFAULT 'write',
            acquired_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_root)",
        "CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id)",
    )
    try:
        conn.execute("BEGIN IMMEDIATE;")
        for stmt in ddl_statements:
            conn.execute(stmt)
        # Additive, transactional, repeatable migration; legacy ownership stays unknown.
        for table, columns in {
            'tasks': {'run_token':'TEXT', 'owner_pid':'INTEGER', 'owner_start':'TEXT',
                'child_pid':'INTEGER', 'child_start':'TEXT', 'heartbeat_at':'TEXT',
                'lease_until':'REAL', 'cancel_requested':'INTEGER NOT NULL DEFAULT 0',
                'timeout_seconds':'INTEGER', 'prompt_sha256':'TEXT'},
            'project_locks': {'lock_token':'TEXT', 'owner_pid':'INTEGER', 'owner_start':'TEXT',
                'child_pid':'INTEGER', 'child_start':'TEXT', 'heartbeat_at':'TEXT', 'lease_until':'REAL'},
        }.items():
            existing={r['name'] for r in conn.execute('PRAGMA table_info('+table+')')}
            for name, kind in columns.items():
                if name not in existing: conn.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+kind)
        cfg = load_routing()
        if cfg.get('project_registry'):
            pm.migrate(conn,cfg)
            for row in conn.execute("SELECT task_id,prompt_path FROM tasks WHERE prompt_sha256 IS NULL").fetchall():
                try:
                    path=pm.inside(row['prompt_path'],PROMPTS_DIR)
                    if path.name==row['task_id']+'.txt' and path.is_file():
                        conn.execute('UPDATE tasks SET prompt_sha256=? WHERE task_id=?',(hashlib.sha256(path.read_bytes()).hexdigest(),row['task_id']))
                except (pm.ProjectError,OSError):pass
        wf.migrate(conn)
        conn.execute("COMMIT;")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def _record_event(
    conn: sqlite3.Connection,
    task_id: str,
    from_status: str | None,
    to_status: str,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO task_events(task_id, at, from_status, to_status, detail) VALUES (?, ?, ?, ?, ?)",
        (task_id, utc_now(), from_status, to_status, detail),
    )


def create_task(
    spec: dict[str, Any],
    *,
    db_path: Path = DEFAULT_DB_PATH,
    prompts_dir: Path = PROMPTS_DIR,
) -> dict[str, Any]:
    init_db(db_path)
    agents_cfg = load_agents()
    routing_cfg = load_routing()
    validated = validate_task_spec(spec, agents_cfg, routing_cfg)

    if routing_cfg.get('project_registry'):
        if pm.bound_project(spec.get('session_id'),db_path)!=validated['project_id']:
            raise PolicyError('Task differs from session binding')
        if spec.get('prompt_path'):
            pm.inside(spec['prompt_path'],validated['project_root'])
    task_id = validated["task_id"] or str(uuid.uuid4())
    now = utc_now()

    prompts_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = prompts_dir.resolve() / f"{task_id}.txt"
    prompt_path = str(prompt_file)
    prompt_text = spec.get("prompt")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        prompt_text = Path(spec["prompt_path"]).read_text(encoding="utf-8")
    if routing_cfg.get('project_registry'):
        prompt_text=pm.context(validated['project_id'],routing_cfg)+'\n\n--- Task request ---\n'+prompt_text
    prompt_created = False

    risk_cfg = routing_cfg["risk_levels"][validated["risk_level"]]
    initial_status = (
        "AWAITING_APPROVAL"
        if risk_cfg.get("requires_approval") and validated["approval_state"] != "approved"
        else "CREATED"
    )

    conn = connect_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        existing = conn.execute(
            "SELECT task_id, status FROM tasks WHERE idempotency_key = ? OR task_id = ?",
            (validated["idempotency_key"], task_id),
        ).fetchone()
        if existing:
            conn.execute("ROLLBACK;")
            raise PolicyError(
                f"Duplicate idempotency_key={validated['idempotency_key']!r} "
                f"(existing task_id={existing['task_id']}, status={existing['status']})"
            )

        # Hold the reservation transaction while exclusively creating a new snapshot.
        # Never resolve/follow an existing task filename, and never truncate it.
        with prompt_file.open("x", encoding="utf-8", newline="") as f:
            prompt_created = True
            f.write(prompt_text)
            f.flush()
            os.fsync(f.fileno())
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, idempotency_key, created_at, updated_at, project_root,
                agent, task_type, risk_level, execution_mode, status,
                prompt_path, result_path, exit_code, duration, retry_count,
                error, approval_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, NULL, ?)
            """,
            (
                task_id,
                validated["idempotency_key"],
                now,
                now,
                validated["project_root"],
                validated["agent"],
                validated["task_type"],
                validated["risk_level"],
                validated["execution_mode"],
                initial_status,
                prompt_path,
                validated["approval_state"],
            ),
        )
        _record_event(
            conn,
            task_id,
            None,
            initial_status,
            f"created agent={validated['agent']} type={validated['task_type']} risk={validated['risk_level']}",
        )
        conn.execute('UPDATE tasks SET timeout_seconds=?,prompt_sha256=? WHERE task_id=?', (validated['timeout'],hashlib.sha256(prompt_file.read_bytes()).hexdigest(),task_id))
        if routing_cfg.get('project_registry'):
            conn.execute('UPDATE tasks SET project_id=? WHERE task_id=?',(validated['project_id'],task_id))
        wf.attach_task(conn, task_id, spec)
        conn.execute("COMMIT;")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        if prompt_created:
            prompt_file.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    return get_task(task_id, db_path=db_path)


def read_task_prompt(task):
    cfg=load_routing()
    if cfg.get('project_registry'):
        return pm.read_prompt(task,PROMPTS_DIR)
    path = Path(task["prompt_path"])
    raw = path.read_bytes()
    if task.get("prompt_sha256") and hashlib.sha256(raw).hexdigest() != task["prompt_sha256"]:
        raise PolicyError("Task prompt snapshot hash mismatch")
    return raw.decode("utf-8")


def get_task(task_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    if not is_valid_uuid(task_id):
        raise PolicyError(f"Invalid UUID: {task_id}")
    conn = connect_db(db_path)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            raise PolicyError(f"Task not found: {task_id}")
        return dict(row)
    finally:
        conn.close()


def inspect_task(task_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    task=get_task(task_id,db_path=db_path)
    result=Path(task['result_path']) if task.get('result_path') else RESULTS_DIR/f'{task_id}.json'
    task['streams']={}
    for stream in ('stdout','stderr'):
        path=result.parent/f'{task_id}.{stream}.log'
        info={'path':str(path),'bytes':0,'tail':''}
        if path.exists():
            with path.open('rb') as f:
                f.seek(0,2); size=f.tell(); f.seek(max(0,size-2048))
                info.update(bytes=size,tail=f.read(2048).decode('utf-8',errors='replace'))
        task['streams'][stream]=info
    if task.get('owner_pid'): task['owner_process_state']=process_identity(task['owner_pid'])[0]
    task['poll_after_seconds']=30 if task['status'] in ('QUEUED','RUNNING') else None
    return task


def task_status_summary(
    task_id: str, *, db_path: Path = DEFAULT_DB_PATH, wait_seconds: float = 30,
) -> dict[str, Any]:
    """Wait locally for a status change and return bounded, read-only evidence.

    Waiting here avoids spending a model turn for each short polling interval.
    Full evidence stays in inspect_task() and the existing result/log files.
    No task, approval, lease or lock is changed by this observer.
    """
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 30:
        raise PolicyError('Status wait must be between 0 and 30 seconds')
    started = time.monotonic()
    initial = get_task(task_id, db_path=db_path)['status']
    if initial in ('QUEUED', 'RUNNING'):
        deadline = started + wait_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
            if get_task(task_id, db_path=db_path)['status'] != initial:
                break
    task = inspect_task(task_id, db_path=db_path)
    result = {key: task.get(key) for key in (
        'task_id', 'project_id', 'agent', 'status', 'duration', 'timeout_seconds',
        'result_path', 'cancel_requested', 'poll_after_seconds',
    )}
    result['error'] = str(task['error'])[:800] if task.get('error') else None
    result['waited_seconds'] = round(time.monotonic() - started, 3)
    result['execution_status_only'] = True
    result['workflow'] = wf.optional_describe(db_path,task_id)
    pending = task['status'] in ('QUEUED', 'RUNNING')
    result['streams'] = {}
    for name, info in task['streams'].items():
        stream = {'path': info['path'], 'bytes': info['bytes']}
        if not pending:
            stream['tail'] = info['tail'][-512:]
        result['streams'][name] = stream
    return result


def _prepared_identity(task):
    """Fields that must not change while slow preparation happens outside SQLite."""
    return tuple(task.get(key) for key in (
        'status','run_token','project_id','project_root','agent','task_type',
        'risk_level','approval_state','execution_mode','prompt_path','prompt_sha256','timeout_seconds',
    ))


def _prepare_invocation(task, agents_cfg, routing_cfg, timeout_override, *, prepare):
    task = dict(task)
    prompt_text = read_task_prompt(task)
    task['timeout'] = timeout_override if timeout_override is not None else task.get('timeout_seconds')
    invocation = build_cli_invocation(task, prompt_text, agents_cfg)
    if routing_cfg.get('project_registry'):
        invocation = (pm.worker_invocation(invocation, task, routing_cfg, agents_cfg) if prepare else
                      pm.worker_invocation(invocation, task, routing_cfg, agents_cfg, prepare=False))
    if os.name == 'nt' and len(subprocess.list2cmdline(invocation['argv'])) > 30000:
        raise PolicyError('Windows command line exceeds the safe argument budget')
    return invocation


def dry_run_task(
    task_id: str,
    *,
    timeout_override: int | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    results_dir: Path = RESULTS_DIR,
) -> dict[str, Any]:
    init_db(db_path)
    agents_cfg = load_agents()
    routing_cfg = load_routing()

    prepared = get_task(task_id, db_path=db_path)
    if prepared.get('run_token') or prepared['status'] not in ('CREATED','DRY_RUN','QUEUED'):
        raise PolicyError('Task already reserved, started or terminal; dry-run refused')
    with closing(connect_db(db_path)) as conn:
        ok, reason = wf.authorize(conn, prepared, agents_cfg, routing_cfg, timeout_override)
    if not ok:
        raise PolicyError(f'Authorization check failed: {reason}')
    invocation = _prepare_invocation(prepared, agents_cfg, routing_cfg, timeout_override, prepare=False)

    conn = connect_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            conn.execute("ROLLBACK;")
            raise PolicyError(f"Task not found: {task_id}")
        task = dict(row)
        if _prepared_identity(task) != _prepared_identity(prepared):
            raise PolicyError('Task changed during preparation; retry validation')
        if task.get('run_token'):
            raise PolicyError('Task already reserved or started; dry-run refused')
        if task["status"] not in ("CREATED", "DRY_RUN", "QUEUED"):
            conn.execute("ROLLBACK;")
            raise PolicyError(f"Task status {task['status']} cannot enter DRY_RUN")

        ok, reason = wf.authorize(conn, task, agents_cfg, routing_cfg, timeout_override)
        if not ok:
            conn.execute("ROLLBACK;")
            raise PolicyError(f"Authorization check failed: {reason}")

        is_write = bool(routing_cfg["task_types"][task["task_type"]].get("write", False))
        lock_row = conn.execute(
            "SELECT task_id FROM project_locks WHERE project_root = ?",
            (task["project_root"],),
        ).fetchone()
        if is_write and lock_row and lock_row["task_id"] != task_id:
            conn.execute("ROLLBACK;")
            raise ResourceBusy(
                f"Project write lock already held for {task['project_root']} by task {lock_row['task_id']}"
            )

        expected_result = str((results_dir / f"{task_id}.json").resolve())

        now = utc_now()
        conn.execute(
            "UPDATE tasks SET status = 'DRY_RUN', result_path = ?, updated_at = ? WHERE task_id = ?",
            (expected_result, now, task_id),
        )
        _record_event(conn, task_id, task["status"], "DRY_RUN", "dry-run validated")
        conn.execute("COMMIT;")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()

    return {
        "ok": True,
        "task_id": task_id,
        "status": "DRY_RUN",
        "project_id": task.get('project_id'),
        "execution_boundary": invocation.get('boundary'),
        "argv": invocation["argv"],
        "cwd": invocation["cwd"],
        "timeout": invocation["timeout"],
        "json_mode": invocation["json_mode"],
        "result_path": expected_result,
    }


INLINE_RESULT_BYTES = 4 * 1024 * 1024
RESULT_TAIL_BYTES = 8192


def output_text(path):
    size = path.stat().st_size
    truncated = size > INLINE_RESULT_BYTES
    with path.open('rb') as stream:
        if truncated:
            stream.seek(size - RESULT_TAIL_BYTES)
        return stream.read().decode('utf-8', errors='replace'), truncated


def parse_result_stream(path, json_mode, agent, stdout_text, truncated):
    """Validate all recorded output while keeping large reports bounded."""
    events = []
    terminal = None
    agy_status = None
    reported_error = False

    def observe(event, size):
        nonlocal reported_error, agy_status, terminal
        if not isinstance(event, dict):
            return
        result = event.get('result')
        agy_final = event.get('event') == 'result' and isinstance(result, dict)
        typed_final = event.get('type') == 'result'
        error = event.get('is_error') is True or (typed_final and str(event.get('subtype', '')).startswith('error'))
        reported_error = reported_error or error
        if agy_final:
            agy_status = result.get('status')
        if truncated and (typed_final or agy_final):
            terminal = event if size <= INLINE_RESULT_BYTES else {
                'type': 'result', 'is_error': error, 'content_in_stdout_log': True}

    if json_mode == 'jsonl':
        seen = False
        with path.open('rb') as stream:
            for index, raw in enumerate(stream, 1):
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw.decode('utf-8', errors='replace'))
                except json.JSONDecodeError as exc:
                    return False, None, f'Invalid JSONL at line {index}: {exc}', reported_error
                observe(event, len(raw))
                if not truncated:
                    events.append(event)
                seen = True
        if not seen:
            return False, None, 'Empty stdout', reported_error
        parsed = ([terminal] if terminal is not None else []) if truncated else events
    else:
        # Single-object adapters are bounded by the configured output byte budget.
        raw = path.read_text(encoding='utf-8', errors='replace') if truncated else stdout_text
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            return False, None, f'Invalid JSON output: {exc}', reported_error
        for item in event if isinstance(event, list) else [event]:
            observe(item, len(raw))
        parsed = terminal if truncated else event
    return True, parsed, '', reported_error or (agent == 'agy' and agy_status != 'SUCCESS')


def _owner_gone(pid, start) -> bool:
    # A live PID (even reused with a different start identity) is NEVER reclaimed.
    return bool(pid and start and process_identity(pid)[0] == 'gone')


def _recover_lock(conn, project_root: str, expected_token: str) -> bool:
    """Caller holds BEGIN IMMEDIATE; lease alone is never evidence of death."""
    row=conn.execute('SELECT * FROM project_locks WHERE project_root=?',(project_root,)).fetchone()
    if not row or not expected_token or row['lock_token'] != expected_token:
        return False
    if row['lease_until'] is None or row['lease_until'] >= time.time(): return False
    if not _owner_gone(row['owner_pid'],row['owner_start']): return False
    # Death during the create/assign/register window cannot prove child ownership.
    # Keep such locks for inspection instead of guessing that no child exists.
    if not _owner_gone(row['child_pid'],row['child_start']): return False
    task=conn.execute('SELECT * FROM tasks WHERE task_id=?',(row['task_id'],)).fetchone()
    if task is None or task['run_token'] != expected_token: return False
    if task['status']=='RUNNING':
        conn.execute("UPDATE tasks SET status='FAILED', error=?, updated_at=? WHERE task_id=? AND run_token=?",
            ('Recovered expired lock after confirmed owner/child death',utc_now(),row['task_id'],expected_token))
        _record_event(conn,row['task_id'],'RUNNING','FAILED','stale owner/child confirmed gone; token matched')
    return conn.execute('DELETE FROM project_locks WHERE project_root=? AND lock_token=?',
        (project_root,expected_token)).rowcount==1


def run_task(task_id: str, *, timeout_override: int | None = None,
             db_path: Path = DEFAULT_DB_PATH, results_dir: Path = RESULTS_DIR,
             launch_token: str | None = None) -> dict[str, Any]:
    if not is_valid_uuid(task_id): raise PolicyError('Invalid task UUID')
    init_db(db_path)
    agents_cfg=load_agents(); routing_cfg=load_routing()
    results_dir.mkdir(parents=True,exist_ok=True)
    result_file=(results_dir/f'{task_id}.json').resolve()
    stdout_path=results_dir/f'{task_id}.stdout.log'
    stderr_path=results_dir/f'{task_id}.stderr.log'
    token=launch_token or str(uuid.uuid4())
    owner_pid = os.getpid()
    state, owner_start = process_identity(owner_pid)
    if state != 'alive' or not owner_start:
        raise PolicyError('Cannot establish dispatcher identity')
    if launch_token:
        # Acknowledge this reservation before doing slow preparation.
        with closing(connect_db(db_path)) as conn:
            changed = conn.execute("""UPDATE tasks SET owner_pid=?,owner_start=?
                WHERE task_id=? AND status='QUEUED' AND run_token=? AND owner_pid IS NULL""",
                (owner_pid, owner_start, task_id, token)).rowcount
        if not changed:
            raise PolicyError('Stale or already acknowledged launch token')
    prepared=get_task(task_id,db_path=db_path)
    if launch_token:
        if prepared['run_token']!=token or prepared['status']!='QUEUED':
            raise PolicyError('Stale launch token or task not queued')
    elif prepared['status'] not in ('CREATED','DRY_RUN','QUEUED') or prepared['run_token']:
        raise PolicyError('Task already started or terminal')
    with closing(connect_db(db_path)) as preflight:
        ok,reason=wf.authorize(preflight,prepared,agents_cfg,routing_cfg,timeout_override)
        if not ok:raise PolicyError(reason)
        wf.require_ready(preflight,task_id)
        budget=wf.budget_for(preflight,task_id)
    invocation=_prepare_invocation(prepared,agents_cfg,routing_cfg,timeout_override,prepare=True)
    invocation['budget']=budget
    conn=connect_db(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
        if not row: raise PolicyError('Task not found')
        task=dict(row)
        if _prepared_identity(task)!=_prepared_identity(prepared):
            raise PolicyError('Task changed during preparation; retry validation')
        if launch_token:
            if task['run_token']!=token or task['status']!='QUEUED': raise PolicyError('Stale launch token or task not queued')
        elif task['status'] not in ('CREATED','DRY_RUN','QUEUED') or task['run_token']:
            raise PolicyError('Task already started or terminal')
        ok,reason=wf.authorize(conn,task,agents_cfg,routing_cfg,timeout_override)
        if not ok: raise PolicyError(reason)
        wf.require_ready(conn,task_id)
        is_write=bool(routing_cfg['task_types'][task['task_type']].get('write',False)) or bool(routing_cfg.get('project_registry'))
        if routing_cfg.get('project_registry'):
            for other in conn.execute('SELECT * FROM project_locks WHERE project_root<>?',(task['project_root'],)).fetchall():
                if not _recover_lock(conn,other['project_root'],other['lock_token']):
                    raise ResourceBusy('V8 serial worker lock held by another project')
        if is_write:
            lock=conn.execute('SELECT * FROM project_locks WHERE project_root=?',(task['project_root'],)).fetchone()
            if lock and not _recover_lock(conn,task['project_root'],lock['lock_token']):
                raise ResourceBusy('Project write lock retained: live, legacy, unknown or token mismatch')
            conn.execute('''INSERT INTO project_locks(project_root,task_id,lock_mode,acquired_at,
                lock_token,owner_pid,owner_start,heartbeat_at,lease_until) VALUES (?,?,'write',?,?,?,?,?,?)''',
                (task['project_root'],task_id,utc_now(),token,owner_pid,owner_start,utc_now(),time.time()+LEASE_SECONDS))
        if routing_cfg.get('project_registry'):
            conn.execute('UPDATE project_locks SET project_id=? WHERE task_id=?',(task['project_id'],task_id))
        conn.execute("""UPDATE tasks SET status='RUNNING',run_token=?,owner_pid=?,owner_start=?,
            child_pid=NULL,child_start=NULL,heartbeat_at=?,lease_until=?,updated_at=?,result_path=? WHERE task_id=?""",
            (token,owner_pid,owner_start,utc_now(),time.time()+LEASE_SECONDS,utc_now(),str(result_file),task_id))
        _record_event(conn,task_id,task['status'],'RUNNING','owned worker started')
        wf.mark_running(conn,task_id)
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction: conn.execute('ROLLBACK')
        raise
    finally: conn.close()

    def heartbeat(child_pid,child_start):
        conn=connect_db(db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT status,run_token,cancel_requested FROM tasks WHERE task_id=?',(task_id,)).fetchone()
            if not row or row['run_token']!=token or row['status']!='RUNNING':
                raise PolicyError('Run ownership changed; stopping owned process')
            if is_write:
                lock=conn.execute('SELECT lock_token FROM project_locks WHERE project_root=?',(task['project_root'],)).fetchone()
                if not lock or lock['lock_token']!=token: raise PolicyError('Write lock ownership changed')
                conn.execute('''UPDATE project_locks SET child_pid=?,child_start=?,heartbeat_at=?,lease_until=?
                    WHERE project_root=? AND lock_token=?''',
                    (child_pid,child_start,utc_now(),time.time()+LEASE_SECONDS,task['project_root'],token))
            conn.execute('''UPDATE tasks SET child_pid=?,child_start=?,heartbeat_at=?,lease_until=?,updated_at=?
                WHERE task_id=? AND run_token=?''',(child_pid,child_start,utc_now(),time.time()+LEASE_SECONDS,utc_now(),task_id,token))
            sizes={p.name:p.stat().st_size if p.exists() else 0 for p in (stdout_path,stderr_path)}
            _record_event(conn,task_id,'RUNNING','RUNNING',json.dumps({'heartbeat':True,'bytes':sizes}))
            conn.execute('COMMIT')
            return bool(row['cancel_requested'])
        except BaseException:
            if conn.in_transaction: conn.execute('ROLLBACK')
            raise
        finally: conn.close()

    started=time.monotonic(); exit_code=-1; timed_out=False; cancelled=False
    json_ok=False; parsed=None; json_error=''; error=None; safe_cleanup=True
    stdout_text=''; stderr_text=''; final_status='FAILED'
    stdout_truncated=False; stderr_truncated=False
    report={}
    usage={'available':False};budget_exceeded=None
    try:
        outcome=execute_streaming(invocation,stdout_path,stderr_path,heartbeat)
        exit_code=outcome['exit_code']; timed_out=outcome['timed_out']; cancelled=outcome['cancelled']
        usage=outcome.get('usage',{'available':False});budget_exceeded=outcome.get('budget_exceeded')
        stdout_text,stdout_truncated=output_text(stdout_path)
        stderr_text,stderr_truncated=output_text(stderr_path)
        json_ok,parsed,json_error,reported_error=parse_result_stream(
            stdout_path,invocation['json_mode'],task['agent'],stdout_text,stdout_truncated)
        if cancelled: error='Cancelled by request; process job stopped'
        elif timed_out: error=f"Timed out after {invocation['timeout']}s; process job stopped"
        elif budget_exceeded: error='Task budget exceeded: '+budget_exceeded
        elif exit_code!=0: error=f'Process exited with {exit_code}: {stderr_text[:400]}'
        elif reported_error: error='Agent reported an error in structured output'
        elif not json_ok: error='JSON validation failed: '+json_error
    except UnsafeCleanup as exc:
        safe_cleanup=False; error='Process tree unverified; lock retained: '+str(exc)
    except BaseException as exc:
        error=f'{type(exc).__name__}: {exc}'
    finally:
        # Result I/O must never skip terminal-state / token-scoped lock cleanup.
        duration=round(time.monotonic()-started,3)
        final_status='CANCELLED' if cancelled else ('SUCCEEDED' if exit_code==0 and json_ok and not error else 'FAILED')
        report={'task_id':task_id,'project_id':task.get('project_id'),'agent':task['agent'],'project_root':invocation['cwd'],
            'execution_mode':task['execution_mode'],'status':final_status,'exit_code':exit_code,
            'duration_seconds':duration,'timed_out':timed_out,'json_parse_ok':json_ok,
            'json_parse_error':json_error or None,'error':error,'stdout':stdout_text,'stderr':stderr_text,
            'stdout_truncated':stdout_truncated,'stderr_truncated':stderr_truncated,
            'stdout_log':str(stdout_path),'stderr_log':str(stderr_path),'parsed_output':parsed,'finished_at':utc_now()}
        report.update(usage=usage,budget_exceeded=budget_exceeded)
        conn=connect_db(db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
            owns_task=bool(row and row['run_token']==token and row['status']=='RUNNING')
            if owns_task:
                if row['cancel_requested'] and safe_cleanup:
                    final_status='CANCELLED'; error='Cancelled by request; process job stopped'
                    report.update(status=final_status,error=error)
                try:
                    tmp=result_file.with_suffix('.json.tmp')
                    with tmp.open('w',encoding='utf-8') as f:
                        json.dump(report,f,ensure_ascii=True,indent=2); f.flush(); os.fsync(f.fileno())
                    os.replace(tmp,result_file)
                except Exception as exc:
                    final_status='FAILED'; error=f'Result write failed: {exc}'
                    report.update(status=final_status,error=error)
                finally:
                    conn.execute('''UPDATE tasks SET status=?,exit_code=?,duration=?,error=?,updated_at=?
                        WHERE task_id=? AND run_token=?''',(final_status,exit_code,duration,error,utc_now(),task_id,token))
                    _record_event(conn,task_id,'RUNNING',final_status,error or 'completed; owned tree stopped')
                    if safe_cleanup:
                        conn.execute('DELETE FROM project_locks WHERE task_id=? AND lock_token=?',(task_id,token))
                    wf.finish(conn,task_id,report)
            else:
                report.update(status='FAILED',error='Run ownership changed; existing task/result/lock preserved')
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction: conn.execute('ROLLBACK')
            raise
        finally: conn.close()
    try:wf.wake(db_path)
    except Exception as exc:print('[queue] wake failed: '+type(exc).__name__,file=sys.stderr)
    return report


def cancel_task(task_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    if not is_valid_uuid(task_id): raise PolicyError('Invalid task UUID')
    init_db(db_path); conn=connect_db(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
        if not row: raise PolicyError('Task not found')
        if row['status'] in ('SUCCEEDED','FAILED','CANCELLED'): raise PolicyError('Task already terminal')
        if row['status']=='RUNNING':
            if not row['run_token'] or not row['owner_pid'] or not row['owner_start']:
                raise PolicyError('Legacy/unknown process ownership; retain lock and inspect manually')
            state,identity=process_identity(row['owner_pid'])
            if state=='alive' and identity==row['owner_start']:
                conn.execute('UPDATE tasks SET cancel_requested=1,updated_at=? WHERE task_id=? AND run_token=?',
                    (utc_now(),task_id,row['run_token']))
                _record_event(conn,task_id,'RUNNING','RUNNING','cancel requested; owner must stop job before releasing lock')
            elif state=='gone' and (row['lease_until'] or 0)<time.time():
                lock=conn.execute('SELECT * FROM project_locks WHERE task_id=?',(task_id,)).fetchone()
                if lock:
                    if not _recover_lock(conn,row['project_root'],row['run_token']):
                        raise PolicyError('Cannot safely recover lock; retained')
                elif not _owner_gone(row['child_pid'],row['child_start']):
                    raise PolicyError('Child process ownership unresolved; retained')
                conn.execute("UPDATE tasks SET status='CANCELLED',cancel_requested=1,updated_at=? WHERE task_id=? AND run_token=?",
                    (utc_now(),task_id,row['run_token']))
                _record_event(conn,task_id,row['status'],'CANCELLED','confirmed owner/child gone after lease expiry')
            else: raise PolicyError('Owner identity unknown/reused or lease unexpired; retain lock')
        else:
            # QUEUED cancellation wins under the same transaction as worker claim.
            conn.execute("UPDATE tasks SET status='CANCELLED',cancel_requested=1,updated_at=? WHERE task_id=?",(utc_now(),task_id))
            _record_event(conn,task_id,row['status'],'CANCELLED','cancelled before execution')
            wf.reset_claim(conn, task_id, 'CANCELLED', 'cancelled_by_request')
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction: conn.execute('ROLLBACK')
        raise
    finally: conn.close()
    wf.wake(db_path)
    return get_task(task_id,db_path=db_path)


def launch_background(task_id: str, *, timeout_override=None, db_path=DEFAULT_DB_PATH, claim_token=None) -> dict[str,Any]:
    """Reserve once and detach a fixed dispatcher worker. No shell or caller-supplied code."""
    init_db(db_path)
    # Validate before spawning or changing status. Includes timeout bounds.
    dry_run_task(task_id,timeout_override=timeout_override,db_path=db_path)
    token=str(uuid.uuid4()); conn=connect_db(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
        if row['status']!='DRY_RUN' or row['run_token']: raise PolicyError('Task already reserved')
        if claim_token is not None and not conn.execute("""SELECT 1 FROM task_queue
                WHERE task_id=? AND state='DISPATCHING' AND claim_token=?""", (task_id, claim_token)).fetchone():
            raise PolicyError('Queue claim is no longer active')
        conn.execute("""UPDATE tasks SET status='QUEUED',run_token=?,owner_pid=NULL,owner_start=NULL,updated_at=?
            WHERE task_id=?""", (token,utc_now(),task_id))
        _record_event(conn,task_id,'DRY_RUN','QUEUED','background reservation')
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction: conn.execute('ROLLBACK')
        raise
    finally: conn.close()
    argv=[sys.executable,str(Path(__file__).resolve()),'--db',str(Path(db_path).resolve()),'_worker','--task-id',task_id,'--token',token]
    if timeout_override is not None: argv.extend(['--timeout',str(timeout_override)])
    try:
        RESULTS_DIR.mkdir(parents=True,exist_ok=True)
        with (RESULTS_DIR/f'{task_id}.worker.log').open('ab',buffering=0) as log:
            proc=subprocess.Popen(argv,cwd=str(DISPATCH_ROOT),stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                close_fds=True,shell=False,creationflags=(0x8|0x200|0x01000000) if os.name=='nt' else 0,
                start_new_session=os.name!='nt')
        # Bounded startup acknowledgment. Agent execution never blocks this command.
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            task=get_task(task_id,db_path=db_path)
            if task['status']!='QUEUED' or task['run_token']!=token: return task
            if proc.poll() is not None: raise PolicyError('Background worker exited before claim; inspect worker log')
            time.sleep(.05)
        return get_task(task_id,db_path=db_path)
    except BaseException as exc:
        conn=connect_db(db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            changed=conn.execute("UPDATE tasks SET status='FAILED',error=?,updated_at=? WHERE task_id=? AND run_token=? AND status='QUEUED'",
                (str(exc),utc_now(),task_id,token)).rowcount
            if changed:
                _record_event(conn,task_id,'QUEUED','FAILED',str(exc))
                wf.reset_claim(conn, task_id, 'DONE', type(exc).__name__)
            conn.execute('COMMIT')
        finally: conn.close()
        raise


def list_tasks(*, db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path)
    conn = connect_db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    # Hermes and the task logs exchange UTF-8 even on a GBK Windows console.
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    parser = argparse.ArgumentParser(description=f"Hermes Team V{wf.VERSION} Dispatcher")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize SQLite database and verify policy files")

    p_create = sub.add_parser("create", help="Create a task from validated inbox JSON file")
    p_create.add_argument("--file", required=True, help="Path to task JSON file")
    p_approve = sub.add_parser('approve', help='Approve a high-risk task in a local interactive terminal')
    p_approve.add_argument('--task-id', required=True)
    p_approve.add_argument('--session-id', required=True)

    p_dry = sub.add_parser("dry-run", help="Validate and prepare task without running CLI")
    p_dry.add_argument("--task-id", required=True, help="Task UUID")
    p_dry.add_argument("--timeout", type=int, default=None, help="Optional timeout override")

    p_run = sub.add_parser("run", help="Execute a prepared task via CLI subprocess")
    p_run.add_argument("--task-id", required=True, help="Task UUID")
    p_run.add_argument("--timeout", type=int, default=None, help="Optional timeout override")
    p_run.add_argument('--session-id',default=None)

    p_worker = sub.add_parser('_worker', help=argparse.SUPPRESS)
    p_worker.add_argument('--task-id',required=True)
    p_worker.add_argument('--token',required=True)
    p_worker.add_argument('--timeout',type=int,default=None)

    p_status = sub.add_parser("status", help="Inspect one task")
    p_status.add_argument("--task-id", required=True, help="Task UUID")

    sub.add_parser("list", help="List recent tasks")

    p_cancel = sub.add_parser("cancel", help="Cancel a task")
    p_cancel.add_argument("--task-id", required=True, help="Task UUID")

    for name in ('enqueue','queue','run-next','review','doctor','usage'):
        command=sub.add_parser(name)
        command.add_argument('--session-id',required=True)
        if name in ('enqueue','review','usage'):command.add_argument('--task-id',required=True)
        if name=='review':
            command.add_argument('--decision',choices=('accept','reject'),required=True)
            command.add_argument('--evidence',required=True)
        if name=='doctor':command.add_argument('--probe',action='store_true')
    sub.add_parser('_queue_tick',help=argparse.SUPPRESS)

    sub.add_parser('projects',help='List registered projects')
    for name in ('bind','context','project-status'):
        command=sub.add_parser(name)
        command.add_argument('--session-id',required=True)
        if name=='bind':command.add_argument('--project-id',required=True)
    args = parser.parse_args(argv)
    db_path = Path(args.db).resolve()

    try:
        if (DISPATCH_ROOT / '.v71-maintenance').exists() and args.command not in ('status','list','queue','usage'):
            raise PolicyError('Dispatcher maintenance in progress')
        if args.command in ('enqueue','queue','run-next','review','doctor','usage','_queue_tick'):
            init_db(db_path)
            if args.command=='_queue_tick':result=wf.watch(db_path)
            elif args.command=='enqueue':result=wf.enqueue(args.task_id,args.session_id,db_path)
            elif args.command=='queue':result=wf.queue_status(args.session_id,db_path)
            elif args.command=='run-next':
                pid=pm.bound_project(args.session_id,db_path);pm.project(pid,load_routing())
                result=wf.pump(db_path,pid)
            elif args.command=='review':result=wf.review(args.task_id,args.session_id,args.decision,args.evidence,db_path)
            elif args.command=='doctor':result=wf.doctor(args.session_id,db_path,args.probe)
            else:
                with __import__('contextlib').closing(connect_db(db_path)) as conn:
                    wf.scoped_task(conn,args.task_id,args.session_id,load_routing());result=wf.describe(conn,args.task_id)
            print(json.dumps(result,ensure_ascii=True,indent=2));return 0
        if args.command in ('projects','bind','context','project-status'):
            cfg=load_routing();init_db(db_path)
            if args.command=='projects':
                result=[{k:x.get(k) for k in ('id','name','status')} for x in pm.registry(cfg).values()]
            elif args.command=='bind':result=pm.bind(args.session_id,args.project_id,db_path,cfg)
            else:
                pid=pm.bound_project(args.session_id,db_path)
                if args.command=='context':result={'project_id':pid,'context':pm.context(pid,cfg)}
                else:
                    conn=connect_db(db_path)
                    try:result=[dict(x) for x in conn.execute('SELECT task_id,project_id,agent,status,task_type,updated_at FROM tasks WHERE project_id=? ORDER BY updated_at DESC LIMIT 30',(pid,))]
                    finally:conn.close()
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        if args.command == "init":
            init_db(db_path)
            agents = load_agents()
            routing = load_routing()
            print(json.dumps({
                "ok": True,
                "db": str(db_path),
                "agents": sorted(agents.get("agents", {}).keys()),
                "project_whitelist": routing.get("project_whitelist", []),
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "create":
            spec = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
            task = create_task(spec, db_path=db_path)
            print(json.dumps(task, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'approve':
            try:
                result = wf.approve(args.task_id, args.session_id, db_path)
            except (EOFError, KeyboardInterrupt) as exc:
                raise PolicyError('Approval cancelled') from exc
            print(json.dumps(result, ensure_ascii=True))
            return 0
        if args.command == "dry-run":
            res = dry_run_task(args.task_id, timeout_override=args.timeout, db_path=db_path)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            if load_routing().get('project_registry'):
                wf.enqueue(args.task_id,args.session_id,db_path,args.timeout)
                wf.pump(db_path,pm.bound_project(args.session_id,db_path))
                wf.wake(db_path)
                res=get_task(args.task_id,db_path=db_path)
            else:res=launch_background(args.task_id,timeout_override=args.timeout,db_path=db_path)
            print(json.dumps({'execution':'waiting' if res['status']=='QUEUED' and not res.get('run_token') else 'background','startup_wait_max_seconds':5,
                'terminal_timeout_recommendation':30,'task':res},ensure_ascii=False,indent=2))
            return 1 if res['status']=='FAILED' else 0
        if args.command == '_worker':
            try:
                res=run_task(args.task_id,timeout_override=args.timeout,db_path=db_path,launch_token=args.token)
                print(json.dumps({k:v for k,v in res.items() if k not in ('stdout','stderr','parsed_output')},ensure_ascii=False))
                return 0 if res['status']=='SUCCEEDED' else 1
            except BaseException as exc:
                if isinstance(exc,ResourceBusy) and wf.release_reservation(args.task_id,args.token,str(exc),db_path):
                    print(json.dumps({'execution':'waiting','task_id':args.task_id}));return 0
                conn=connect_db(db_path)
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    changed=conn.execute("""UPDATE tasks SET status='FAILED',error=?,updated_at=?
                        WHERE task_id=? AND run_token=? AND status='QUEUED' AND (owner_pid IS NULL OR owner_pid=?)""",
                        (str(exc),utc_now(),args.task_id,args.token,os.getpid())).rowcount
                    if changed: _record_event(conn,args.task_id,'QUEUED','FAILED',str(exc))
                    if changed:
                        wf.reset_claim(conn, args.task_id, 'DONE', type(exc).__name__)
                    conn.execute('COMMIT')
                finally: conn.close()
                wf.wake(db_path)
                raise
        if args.command == "status":
            print(json.dumps(task_status_summary(args.task_id, db_path=db_path), ensure_ascii=False, indent=2))
            return 0
        if args.command == "list":
            print(json.dumps(list_tasks(db_path=db_path), ensure_ascii=False, indent=2))
            return 0
        if args.command == "cancel":
            print(json.dumps(cancel_task(args.task_id, db_path=db_path), ensure_ascii=False, indent=2))
            return 0
    except (PolicyError, pm.ProjectError, OSError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
