"""V8.1 workflow metadata, explicit queues, acceptance gates and diagnostics.

Only explicitly queued tasks can start automatically. Historical tasks are never
enqueued by migration. Execution remains globally serial and existing permission
and process-ownership checks remain authoritative.
"""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
import re

from policy import PolicyError, check_execution_authorization, is_valid_uuid
import project_manager as pm

VERSION = '8.1.1'
TERMINAL = ('SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED')


def migrate(conn):
    for sql in (
        "CREATE TABLE IF NOT EXISTS task_options(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), budget_json TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS task_dependencies(task_id TEXT NOT NULL REFERENCES tasks(task_id), depends_on TEXT NOT NULL REFERENCES tasks(task_id), PRIMARY KEY(task_id,depends_on), CHECK(task_id<>depends_on))",
        "CREATE TABLE IF NOT EXISTS task_reviews(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), decision TEXT NOT NULL CHECK(decision IN ('ACCEPTED','REJECTED')), evidence_json TEXT NOT NULL, reviewer_session TEXT NOT NULL, reviewed_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS task_approvals(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), fingerprint TEXT NOT NULL, approved_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS task_queue(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), state TEXT NOT NULL CHECK(state IN ('WAITING','DISPATCHING','RUNNING','DONE','BLOCKED','CANCELLED')), requested_at TEXT NOT NULL, reason TEXT, claim_token TEXT, claim_pid INTEGER, claim_start TEXT)",
        "CREATE TABLE IF NOT EXISTS task_usage(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), usage_json TEXT NOT NULL, duration_seconds REAL NOT NULL, finished_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS adapter_checks(agent TEXT PRIMARY KEY, result_json TEXT NOT NULL, checked_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS queue_watch(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL, owner_pid INTEGER NOT NULL, owner_start TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_queue_state_time ON task_queue(state,requested_at)",
    ):
        conn.execute(sql)


def parse_budget(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {'max_output_bytes','max_tokens','max_retries'}:
        raise PolicyError('Unknown or invalid task budget')
    result = {'max_output_bytes':16*1024*1024, 'max_tokens':None, 'max_retries':0}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, int):
            raise PolicyError('Budget values must be integers')
        low, high = {'max_output_bytes':(1024,64*1024*1024), 'max_tokens':(1,2000000), 'max_retries':(0,0)}[key]
        if not low <= item <= high:
            raise PolicyError('Budget out of range; automatic retries are disabled')
        result[key] = item
    return result


def attach_task(conn, task_id, spec):
    if any(key in spec for key in ('review_state','usage','queue_state')):
        raise PolicyError('Workflow state is server-owned')
    budget = parse_budget(spec.get('budget'))
    dependencies = spec.get('depends_on', [])
    if not isinstance(dependencies,list) or len(dependencies)>16 or len(set(map(str,dependencies)))!=len(dependencies):
        raise PolicyError('Invalid task dependencies')
    task = dict(conn.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone())
    for parent in dependencies:
        if not is_valid_uuid(parent) or parent == task_id:
            raise PolicyError('Invalid dependency UUID')
        row = conn.execute('SELECT * FROM tasks WHERE task_id=?',(parent,)).fetchone()
        if row is None:
            raise PolicyError('Dependency does not exist')
        other = dict(row)
        if other.get('project_id') != task.get('project_id') or other['project_root'] != task['project_root']:
            raise PolicyError('Cross-project dependencies are not permitted')
        conn.execute('INSERT INTO task_dependencies VALUES (?,?)',(task_id,parent))
    # Dependencies are immutable and only refer to existing tasks, so a newly
    # created task cannot introduce a cycle or a forward reference.
    conn.execute('INSERT INTO task_options VALUES (?,?)',(task_id,json.dumps(budget)))


def budget_for(conn, task_id):
    row = conn.execute('SELECT budget_json FROM task_options WHERE task_id=?',(task_id,)).fetchone()
    return json.loads(row[0]) if row else parse_budget(None)


def dependency_state(conn, task_id):
    rows = conn.execute('''SELECT t.status,r.decision FROM task_dependencies d
        JOIN tasks t ON t.task_id=d.depends_on LEFT JOIN task_reviews r ON r.task_id=t.task_id
        WHERE d.task_id=?''',(task_id,)).fetchall()
    if any(row['status'] in ('FAILED','CANCELLED','BLOCKED') or row['decision']=='REJECTED' for row in rows):
        return 'blocked', 'dependency_failed_or_rejected'
    if any(row['status']!='SUCCEEDED' for row in rows):
        return 'waiting', 'dependency_execution_pending'
    if any(row['decision']!='ACCEPTED' for row in rows):
        return 'waiting', 'dependency_acceptance_pending'
    return 'ready', None


def require_ready(conn, task_id):
    state, reason = dependency_state(conn, task_id)
    if state != 'ready':
        raise PolicyError(reason)


def scoped_task(conn, task_id, session_id, cfg):
    pid = pm.bound_project(session_id, Path(conn.execute('PRAGMA database_list').fetchone()[2]))
    pm.project(pid,cfg)
    row = conn.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
    if row is None or dict(row).get('project_id')!=pid:
        raise PolicyError('Task is outside the bound project')
    return dict(row)


def approval_fingerprint(task):
    fields = ('task_id', 'project_id', 'project_root', 'agent', 'task_type',
              'risk_level', 'execution_mode', 'prompt_sha256', 'timeout_seconds')
    snapshot = json.dumps([task.get(key) for key in fields], ensure_ascii=True)
    return hashlib.sha256(snapshot.encode('ascii')).hexdigest()


def authorize(conn, task, agents, cfg, timeout_override=None):
    requires_approval = cfg['risk_levels'].get(task['risk_level'], {}).get('requires_approval', True)
    verified = False
    if requires_approval:
        if timeout_override is not None and timeout_override != task['timeout_seconds']:
            return False, 'Approved task timeout cannot be changed'
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_approvals' AND type='table'").fetchone()
        if table:
            row = conn.execute('SELECT fingerprint FROM task_approvals WHERE task_id=?', (task['task_id'],)).fetchone()
            verified = bool(task['prompt_sha256'] and row and row[0] == approval_fingerprint(task))
    return check_execution_authorization(task, agents, cfg, approval_verified=verified)


def approve(task_id, session_id, db_path):
    """Only the local operator CLI exposes this operation; the Hermes Hook denies it."""
    import dispatcher as d
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PolicyError('Approval requires an interactive local terminal')
    if not is_valid_uuid(task_id):
        raise PolicyError('Invalid task UUID')
    d.init_db(db_path)
    cfg = d.load_routing()

    def eligible(task):
        if not cfg['risk_levels'].get(task['risk_level'], {}).get('requires_approval'):
            raise PolicyError('This task does not require approval')
        if task['run_token'] or task['status'] not in ('AWAITING_APPROVAL', 'CREATED', 'DRY_RUN'):
            raise PolicyError('Only unreserved tasks can be approved')
        if task['approval_state'] == 'denied' or not task['prompt_sha256']:
            raise PolicyError('Task is denied or has no verified prompt snapshot')

    with closing(d.connect_db(db_path)) as conn:
        task = scoped_task(conn, task_id, session_id, cfg)
        eligible(task)
    fingerprint = approval_fingerprint(task)
    prompt = d.read_task_prompt(task)
    # Escape unprintable characters before displaying the task in the terminal.
    display = ''.join(ch if ch == '\n' or ch.isprintable() else ascii(ch)[1:-1] for ch in prompt)
    print(f"Task {task_id} in {task['project_root']}\n"
          f"Agent {task['agent']} / {task['task_type']} / {task['execution_mode']}\n"
          f"Timeout {task['timeout_seconds']} seconds\n\n{display}\n")
    if input(f'To approve, type the task ID {task_id}: ').strip() != task_id:
        raise PolicyError('Approval cancelled')
    with closing(d.connect_db(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = scoped_task(conn, task_id, session_id, cfg)
        eligible(current)
        if approval_fingerprint(current) != fingerprint or current['status'] != task['status']:
            raise PolicyError('Task changed while awaiting approval')
        d.read_task_prompt(current)
        at = d.utc_now()
        conn.execute('INSERT INTO task_approvals VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET fingerprint=excluded.fingerprint, approved_at=excluded.approved_at',
                     (task_id, fingerprint, at))
        conn.execute("UPDATE tasks SET approval_state='approved',status='CREATED',updated_at=? WHERE task_id=?", (at, task_id))
        d._record_event(conn, task_id, current['status'], 'CREATED', 'local operator approved task snapshot')
        conn.commit()
    return {'task_id': task_id, 'approval_state': 'approved', 'status': 'CREATED'}


def describe(conn, task_id):
    queue = conn.execute('SELECT state,reason FROM task_queue WHERE task_id=?',(task_id,)).fetchone()
    review = conn.execute('SELECT decision,reviewed_at FROM task_reviews WHERE task_id=?',(task_id,)).fetchone()
    usage = conn.execute('SELECT usage_json FROM task_usage WHERE task_id=?',(task_id,)).fetchone()
    return {'version':VERSION,'queue':dict(queue) if queue else None,
            'acceptance':dict(review) if review else {'decision':'PENDING'},
            'budget':budget_for(conn,task_id),'usage':json.loads(usage[0]) if usage else None}


def optional_describe(db_path, task_id):
    path=Path(db_path).resolve()
    if not path.is_file():return None
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as conn:
        conn.row_factory=sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_queue'").fetchone():return None
        return describe(conn,task_id)


def enqueue(task_id, session_id, db_path, timeout_override=None):
    import dispatcher as d
    d.init_db(db_path)
    if not is_valid_uuid(task_id):raise PolicyError('Invalid task UUID')
    cfg=d.load_routing()
    with closing(d.connect_db(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        task=scoped_task(conn,task_id,session_id,cfg)
        if task['status'] not in ('CREATED','DRY_RUN','QUEUED') or task['run_token']:
            raise PolicyError('Task already started, reserved or terminal')
        ok,reason=authorize(conn,task,d.load_agents(),cfg,timeout_override)
        if not ok:raise PolicyError(reason)
        if timeout_override is not None:
            maximum=d.load_agents()['agents'][task['agent']]['max_timeout_seconds']
            if isinstance(timeout_override,bool) or not isinstance(timeout_override,int) or not 1<=timeout_override<=maximum:
                raise PolicyError('Timeout outside agent bounds')
            conn.execute('UPDATE tasks SET timeout_seconds=? WHERE task_id=?',(timeout_override,task_id))
        conn.execute("INSERT OR IGNORE INTO task_queue(task_id,state,requested_at,reason) VALUES (?,'WAITING',?,NULL)",(task_id,d.utc_now()))
        conn.execute("UPDATE tasks SET status='QUEUED',updated_at=? WHERE task_id=?",(d.utc_now(),task_id))
        d._record_event(conn,task_id,task['status'],'QUEUED','explicit V8.1 queue request')
        conn.commit()
    return d.get_task(task_id,db_path=db_path)


def mark_running(conn, task_id):
    conn.execute("""UPDATE task_queue SET state='RUNNING',reason=NULL,
        claim_token=NULL,claim_pid=NULL,claim_start=NULL WHERE task_id=?""", (task_id,))


def finish(conn, task_id, report):
    state='CANCELLED' if report['status']=='CANCELLED' else 'DONE'
    conn.execute('UPDATE task_queue SET state=?,reason=? WHERE task_id=?',(state,report.get('error'),task_id))
    usage=dict(report.get('usage') or {'available':False})
    usage['budget_exceeded']=report.get('budget_exceeded')
    conn.execute('INSERT OR REPLACE INTO task_usage VALUES (?,?,?,?)',
                 (task_id,json.dumps(usage,ensure_ascii=True),report['duration_seconds'],report['finished_at']))


def release_reservation(task_id, token, reason, db_path):
    import dispatcher as d
    with closing(d.connect_db(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        queued=conn.execute('SELECT 1 FROM task_queue WHERE task_id=?',(task_id,)).fetchone()
        changed=False
        if queued:
            changed=conn.execute("""UPDATE tasks SET run_token=NULL,owner_pid=NULL,owner_start=NULL,error=NULL,updated_at=?
                WHERE task_id=? AND status='QUEUED' AND run_token=?""", (d.utc_now(),task_id,token)).rowcount==1
        if changed:
            reset_claim(conn, task_id, 'WAITING', reason)
        conn.commit()
    return changed


def reset_claim(conn, task_id, state, reason):
    conn.execute("""UPDATE task_queue SET state=?,reason=?,claim_token=NULL,claim_pid=NULL,claim_start=NULL
        WHERE task_id=?""", (state, reason, task_id))


def prestart_owner_gone(pid, start, identity):
    if not pid or not start:
        return False
    state, current = identity(pid)
    return state == 'gone' or (state == 'alive' and current is not None and current != start)


def recover_dispatches(conn, dispatcher):
    conn.execute("""UPDATE task_queue
        SET state=CASE (SELECT status FROM tasks WHERE tasks.task_id=task_queue.task_id)
            WHEN 'CANCELLED' THEN 'CANCELLED' WHEN 'BLOCKED' THEN 'BLOCKED' ELSE 'DONE' END,
            claim_token=NULL,claim_pid=NULL,claim_start=NULL
        WHERE state IN ('DISPATCHING','RUNNING')
        AND task_id IN (SELECT task_id FROM tasks WHERE status IN ('SUCCEEDED','FAILED','CANCELLED','BLOCKED'))""")
    reservations = conn.execute("""SELECT q.*,t.status,t.run_token,t.owner_pid,t.owner_start
        FROM task_queue q JOIN tasks t USING(task_id) WHERE q.state='DISPATCHING'""").fetchall()
    for row in reservations:
        if not prestart_owner_gone(row['claim_pid'], row['claim_start'], dispatcher.process_identity):
            continue
        if row['owner_pid'] and not prestart_owner_gone(row['owner_pid'], row['owner_start'], dispatcher.process_identity):
            continue
        if conn.execute('SELECT 1 FROM project_locks WHERE task_id=?', (row['task_id'],)).fetchone():
            continue
        # A late worker must still prove this token in the transaction that enters RUNNING.
        changed = conn.execute("""UPDATE tasks SET status='QUEUED',run_token=NULL,owner_pid=NULL,owner_start=NULL,updated_at=?
            WHERE task_id=? AND status IN ('QUEUED','DRY_RUN') AND run_token IS ?""",
            (dispatcher.utc_now(), row['task_id'], row['run_token'])).rowcount
        if changed:
            reset_claim(conn, row['task_id'], 'WAITING', 'recovered_abandoned_dispatch')
            dispatcher._record_event(conn, row['task_id'], row['status'], 'QUEUED', 'recovered pre-start reservation')


def finish_dispatch_failure(db_path, task_id, token, exc):
    import dispatcher as d
    with closing(d.connect_db(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        task = conn.execute("""SELECT t.status,t.run_token FROM tasks t JOIN task_queue q USING(task_id)
            WHERE t.task_id=? AND q.state='DISPATCHING' AND q.claim_token=?""", (task_id, token)).fetchone()
        handled = False
        if task and not task['run_token'] and task['status'] in ('QUEUED', 'DRY_RUN'):
            busy = isinstance(exc, d.ResourceBusy)
            status = 'QUEUED' if busy else 'FAILED'
            conn.execute('UPDATE tasks SET status=?,error=?,updated_at=? WHERE task_id=?',
                         (status, None if busy else str(exc), d.utc_now(), task_id))
            reset_claim(conn, task_id, 'WAITING' if busy else 'DONE', 'worker_busy' if busy else type(exc).__name__)
            d._record_event(conn, task_id, task['status'], status, 'worker busy' if busy else str(exc))
            handled = True
        conn.commit()
    return handled


def pump(db_path, project_id=None):
    """Claim at most one explicit request, never run arbitrary or historical tasks."""
    import dispatcher as d
    d.init_db(db_path)
    token=str(uuid.uuid4())
    with closing(d.connect_db(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        for lock in conn.execute('SELECT * FROM project_locks').fetchall():
            d._recover_lock(conn,lock['project_root'],lock['lock_token'])
        recover_dispatches(conn, d)
        occupied=conn.execute('SELECT 1 FROM project_locks LIMIT 1').fetchone() or conn.execute("SELECT 1 FROM tasks WHERE status='RUNNING' LIMIT 1").fetchone()
        claimed=conn.execute("SELECT 1 FROM task_queue WHERE state IN ('DISPATCHING','RUNNING') LIMIT 1").fetchone()
        if occupied or claimed:
            conn.execute("UPDATE task_queue SET reason='worker_busy' WHERE state='WAITING'")
            conn.commit()
            return {'started':False,'reason':'worker_busy'}
        sql="SELECT t.* FROM task_queue q JOIN tasks t USING(task_id) WHERE q.state='WAITING'"
        args=()
        if project_id is not None:sql+=' AND t.project_id=?';args=(project_id,)
        candidate=None
        for row in conn.execute(sql+' ORDER BY q.requested_at,t.task_id',args).fetchall():
            task=dict(row)
            if task['status'] in TERMINAL:
                conn.execute("UPDATE task_queue SET state='CANCELLED' WHERE task_id=?",(task['task_id'],));continue
            state,reason=dependency_state(conn,task['task_id'])
            if state=='blocked':
                conn.execute("UPDATE task_queue SET state='BLOCKED',reason=? WHERE task_id=?",(reason,task['task_id']))
                conn.execute("UPDATE tasks SET status='BLOCKED',error=?,updated_at=? WHERE task_id=?",(reason,d.utc_now(),task['task_id']))
                d._record_event(conn,task['task_id'],task['status'],'BLOCKED',reason)
            elif state=='waiting':
                conn.execute('UPDATE task_queue SET reason=? WHERE task_id=?',(reason,task['task_id']))
            else:
                candidate=task;break
        if candidate:
            state, identity = d.process_identity(os.getpid())
            if state != 'alive' or not identity:
                raise PolicyError('Cannot establish queue dispatcher identity')
            conn.execute("UPDATE task_queue SET state='DISPATCHING',reason=NULL,claim_token=?,claim_pid=?,claim_start=? WHERE task_id=?",
                         (token,os.getpid(),identity,candidate['task_id']))
        conn.commit()
    if not candidate:return {'started':False,'reason':'dependencies_or_empty'}
    try:
        task=d.launch_background(candidate['task_id'],db_path=db_path,claim_token=token)
        if task['status'] == 'CANCELLED':
            return {'started':False, 'reason':'cancelled_before_start'}
        if task['status'] == 'QUEUED' and not task.get('run_token'):
            return {'started':False, 'reason':'worker_busy'}
        with closing(d.connect_db(db_path)) as conn:
            if task['status'] == 'FAILED':
                conn.execute("""UPDATE task_queue SET state='DONE',reason=?,claim_token=NULL,claim_pid=NULL,claim_start=NULL
                    WHERE task_id=? AND state='DISPATCHING' AND claim_token=?""", (task.get('error'),task['task_id'],token))
        return {'started':True,'task_id':task['task_id'],'status':task['status']}
    except d.ResourceBusy as exc:
        finish_dispatch_failure(db_path, candidate['task_id'], token, exc)
        if d.get_task(candidate['task_id'], db_path=db_path)['status'] == 'CANCELLED':
            return {'started':False, 'reason':'cancelled_before_start'}
        return {'started':False,'reason':'worker_busy'}
    except Exception as exc:
        handled = finish_dispatch_failure(db_path, candidate['task_id'], token, exc)
        status = d.get_task(candidate['task_id'], db_path=db_path)['status']
        if status == 'CANCELLED':
            return {'started':False, 'reason':'cancelled_before_start'}
        if handled or status == 'FAILED':
            return {'started':False, 'reason':'dispatch_failed', 'task_id':candidate['task_id']}
        raise


def wake(db_path):
    import dispatcher as d
    with closing(d.connect_db(db_path)) as conn:
        if not conn.execute("SELECT 1 FROM task_queue WHERE state='WAITING' LIMIT 1").fetchone():return
        owner=conn.execute('SELECT * FROM queue_watch WHERE id=1').fetchone()
        if owner and not prestart_owner_gone(owner['owner_pid'], owner['owner_start'], d.process_identity):return
    d.RESULTS_DIR.mkdir(parents=True,exist_ok=True)
    with (d.RESULTS_DIR/'queue-dispatch.log').open('ab') as log:
        subprocess.Popen([sys.executable,str(Path(d.__file__).resolve()),'--db',str(Path(db_path).resolve()),'_queue_tick'],
                         stdin=subprocess.DEVNULL,stdout=log,stderr=log,close_fds=True,
                         creationflags=0x08000000 if os.name=='nt' else 0)


def watch(db_path, max_seconds=3600):
    """One local observer; waits on resources, never spends a model turn polling."""
    import dispatcher as d
    d.init_db(db_path)
    token=str(uuid.uuid4());state,identity=d.process_identity(os.getpid())
    if state!='alive' or not identity:raise PolicyError('Cannot establish queue observer identity')
    with closing(d.connect_db(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        owner=conn.execute('SELECT * FROM queue_watch WHERE id=1').fetchone()
        if owner and not prestart_owner_gone(owner['owner_pid'], owner['owner_start'], d.process_identity):return {'observer':'already_running'}
        conn.execute('INSERT OR REPLACE INTO queue_watch VALUES (1,?,?,?)',(token,os.getpid(),identity))
        conn.commit()
    deadline=time.monotonic()+max_seconds
    try:
        while time.monotonic()<deadline:
            result=pump(db_path)
            with closing(d.connect_db(db_path)) as conn:
                pending=conn.execute("SELECT 1 FROM task_queue WHERE state IN ('WAITING','DISPATCHING') LIMIT 1").fetchone()
            if not pending or (not result['started'] and result['reason']=='dependencies_or_empty'):return result
            time.sleep(1)
        return {'observer':'time_limit','reason':'explicit queued work remains; use run-next to resume'}
    finally:
        with closing(d.connect_db(db_path)) as conn:conn.execute('DELETE FROM queue_watch WHERE id=1 AND token=?',(token,))


def review(task_id, session_id, decision, evidence_path, db_path):
    import dispatcher as d
    cfg=d.load_routing()
    with closing(d.connect_db(db_path)) as conn:task=scoped_task(conn,task_id,session_id,cfg)
    if task['status']!='SUCCEEDED':raise PolicyError('Only successful execution can be accepted or rejected')
    if decision not in ('accept','reject'):raise PolicyError('Invalid acceptance decision')
    path=pm.inside(evidence_path,task['project_root'])
    if not path.is_file():raise PolicyError('Evidence must be a project file of at most 64 KiB')
    with path.open('rb') as evidence_file:raw=evidence_file.read(65537)
    if len(raw)>65536:raise PolicyError('Evidence exceeds 64 KiB')
    data=json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data,dict) or not isinstance(data.get('summary'),str) or not data['summary'].strip():raise PolicyError('Evidence summary is required')
    checks=data.get('checks')
    if not isinstance(checks,list) or not checks or len(checks)>32:raise PolicyError('Evidence checks are required')
    for check in checks:
        if not isinstance(check,dict) or not isinstance(check.get('name'),str) or not check['name'].strip() or check.get('status') not in ('passed','failed'):
            raise PolicyError('Invalid evidence check')
    if decision=='accept' and any(check['status']!='passed' for check in checks):raise PolicyError('Cannot accept failed checks')
    outputs=data.get('outputs',[])
    if not isinstance(outputs,list) or len(outputs)>64:raise PolicyError('Invalid evidence outputs')
    for output in outputs:
        if not isinstance(output,str) or not pm.inside(output,task['project_root']).is_file():raise PolicyError('Evidence output is missing or outside this project')
    if cfg['task_types'][task['task_type']].get('write') and not outputs:
        raise PolicyError('Write-task acceptance requires at least one existing project output')
    import hashlib
    evidence={'summary':data['summary'],'checks':checks,'outputs':outputs,'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()}
    with closing(d.connect_db(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        current=scoped_task(conn,task_id,session_id,cfg)
        if current['status']!='SUCCEEDED':raise PolicyError('Execution state changed')
        if conn.execute('SELECT 1 FROM task_reviews WHERE task_id=?',(task_id,)).fetchone():raise PolicyError('Acceptance decision is immutable; create a new task for rework')
        conn.execute('INSERT INTO task_reviews VALUES (?,?,?,?,?)',(task_id,decision.upper()+'ED' if decision=='reject' else 'ACCEPTED',json.dumps(evidence,ensure_ascii=True),session_id,d.utc_now()))
        d._record_event(conn,task_id,'SUCCEEDED','SUCCEEDED','acceptance: '+decision)
        conn.commit()
    wake(db_path)
    return {'task_id':task_id,'decision':'ACCEPTED' if decision=='accept' else 'REJECTED','evidence':evidence}


def queue_status(session_id, db_path):
    import dispatcher as d
    pid=pm.bound_project(session_id,db_path);pm.project(pid,d.load_routing())
    with closing(d.connect_db(db_path)) as conn:
        return [dict(row) for row in conn.execute('''SELECT q.task_id,q.state,q.reason,q.requested_at,t.status
            FROM task_queue q JOIN tasks t USING(task_id) WHERE t.project_id=?
            ORDER BY q.state IN ('DONE','CANCELLED','BLOCKED'),q.requested_at DESC LIMIT 100''',(pid,))]


def guard_command(command, args, session, db_path, cfg, dispatch_root):
    """Strict allow-list for coordinator commands, before the legacy Hook parser."""
    def blocked(reason):return {'action':'block','message':'V8.1 workflow policy: '+reason}
    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',session):return blocked('Missing or invalid runtime session')
    if (dispatch_root/'.v71-maintenance').exists() and command not in ('queue','usage'):
        return blocked('Dispatcher maintenance in progress')
    allowed={'run':{'--task-id','--timeout','--session-id'},
             'enqueue':{'--task-id','--session-id'},'queue':{'--session-id'},
             'run-next':{'--session-id'},'review':{'--task-id','--decision','--evidence','--session-id'},
             'doctor':{'--session-id','--probe'},'usage':{'--task-id','--session-id'}}[command]
    options={};index=0
    while index<len(args):
        flag=args[index]
        if flag not in allowed or flag in options:return blocked('Unknown or duplicate flag')
        if flag=='--probe':options[flag]=True;index+=1;continue
        if index+1>=len(args):return blocked('Missing flag value')
        options[flag]=args[index+1];index+=2
    if options.get('--session-id',session)!=session:return blocked('Forged session ID')
    pid=pm.bound_project(session,db_path);pm.project(pid,cfg)
    task=None
    if command in ('run','enqueue','review','usage'):
        tid=options.get('--task-id','')
        if not is_valid_uuid(tid):return blocked('Invalid task UUID')
        with closing(sqlite3.connect(Path(db_path).as_uri()+'?mode=ro',uri=True)) as conn:
            conn.row_factory=sqlite3.Row
            row=conn.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone()
        if row is None or dict(row).get('project_id')!=pid:return blocked('Task is outside the bound project')
        task=dict(row)
    if command in ('run','enqueue'):
        from policy import load_agents
        agents=load_agents(Path(dispatch_root)/'agents.json')
        with closing(sqlite3.connect(Path(db_path).as_uri()+'?mode=ro',uri=True)) as conn:
            ok,reason=authorize(conn,task,agents,cfg)
        if not ok:return blocked(reason)
        if task['status'] not in ('CREATED','DRY_RUN','QUEUED') or task['run_token']:
            return blocked('Task already reserved or terminal')
        if '--timeout' in options:
            value=options['--timeout']
            if not value.isdigit() or not 1<=int(value)<=agents['agents'][task['agent']]['max_timeout_seconds']:
                return blocked('Timeout outside agent bounds')
    if command=='review':
        if task['status']!='SUCCEEDED':return blocked('Execution is not successful')
        if options.get('--decision') not in ('accept','reject') or '--evidence' not in options:
            return blocked('Review decision and evidence are required')
        if not pm.inside(options['--evidence'],task['project_root']).is_file():
            return blocked('Evidence must be a current-project file')
    if '--session-id' not in options:
        parts=[sys.executable,str(dispatch_root/'dispatcher.py'),command,'--session-id',session,*args]
        return {'action':'modify','args':{'command':subprocess.list2cmdline(parts)}}
    return {}


def doctor(session_id, db_path, probe=False):
    import dispatcher as d
    pid=pm.bound_project(session_id,db_path);cfg=d.load_routing();item=pm.project(pid,cfg);agents=d.load_agents()
    results={}
    for agent,entry in agents['agents'].items():
        if not entry.get('enabled'):continue
        try:
            completed=subprocess.run([entry['executable'],'--version'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10)
            version=(completed.stdout or completed.stderr).strip()[:200]
            results[agent]={'version':version,'exit_code':completed.returncode,'profile_probe':'not_requested'}
        except Exception as exc:results[agent]={'error':type(exc).__name__}
    if probe:
        task={'task_id':str(uuid.uuid4()),'project_id':pid,'project_root':item['root'],'agent':'codex','task_type':'smoke_test','execution_mode':'read_only','timeout':15}
        inv=d.build_cli_invocation(task,'Permission profile handshake only; no model turn.',agents)
        inv=pm.worker_invocation(inv,task,cfg,agents)
        runtime=Path(json.loads(inv['stdin_text'])['runtime'])
        inv['argv']=[*inv['argv'],'--probe']
        inv['budget']={'max_output_bytes':1024*1024}
        out=runtime/'doctor.stdout.log';err=runtime/'doctor.stderr.log'
        outcome=d.execute_streaming(inv,out,err,lambda *args:False)
        stdout=out.read_text(encoding='utf-8',errors='replace')
        stderr=err.read_text(encoding='utf-8',errors='replace')
        try:events=[json.loads(line) for line in stdout.splitlines() if line.strip()]
        except ValueError:events=[]
        valid=outcome['exit_code']==0 and any(e.get('probe_only') and e.get('model_turns')==0 for e in events)
        results['codex']['profile_probe']='passed' if valid else 'failed'
        results['codex']['probe_model_turns']=0
        if not valid:results['codex']['probe_error']=(stderr or stdout)[-600:]
    with closing(d.connect_db(db_path)) as conn:
        for agent,result in results.items():
            old=conn.execute('SELECT result_json FROM adapter_checks WHERE agent=?',(agent,)).fetchone()
            result['version_changed']=bool(old and json.loads(old[0]).get('version')!=result.get('version'))
            conn.execute('INSERT OR REPLACE INTO adapter_checks VALUES (?,?,?)',(agent,json.dumps(result,ensure_ascii=True),d.utc_now()))
    return {'version':VERSION,'execution_policy':'serial_all_workers','agents':results,'automatic_retries':False,
            'token_budget_semantics':'Stop when reported usage exceeds the limit; providers may report only at completion. This is not a guaranteed billing cap.'}
