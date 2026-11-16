"""V8.1 workflow metadata, explicit queues, acceptance gates and diagnostics.

Only explicitly queued tasks can start automatically. Historical tasks are never
enqueued by migration. Execution remains globally serial and existing permission
and process-ownership checks remain authoritative.
"""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
import re

from policy import PolicyError, is_valid_uuid
import project_manager as pm

VERSION = '8.1.0'
TERMINAL = ('SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED')


def migrate(conn):
    for sql in (
        "CREATE TABLE IF NOT EXISTS task_options(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), budget_json TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS task_dependencies(task_id TEXT NOT NULL REFERENCES tasks(task_id), depends_on TEXT NOT NULL REFERENCES tasks(task_id), PRIMARY KEY(task_id,depends_on), CHECK(task_id<>depends_on))",
        "CREATE TABLE IF NOT EXISTS task_reviews(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), decision TEXT NOT NULL CHECK(decision IN ('ACCEPTED','REJECTED')), evidence_json TEXT NOT NULL, reviewer_session TEXT NOT NULL, reviewed_at TEXT NOT NULL)",
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
        ok,reason=d.check_execution_authorization(task,d.load_agents(),cfg)
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
    conn.execute("UPDATE task_queue SET state='RUNNING',reason=NULL WHERE task_id=?",(task_id,))


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
            changed=conn.execute("UPDATE tasks SET status='QUEUED',run_token=NULL,error=NULL,updated_at=? WHERE task_id=? AND status='QUEUED' AND run_token=?",(d.utc_now(),task_id,token)).rowcount==1
        if changed:
            conn.execute("UPDATE task_queue SET state='WAITING',reason=?,claim_token=NULL WHERE task_id=?",(reason,task_id))
        conn.commit()
    return changed


def pump(db_path, project_id=None):
    """Claim at most one explicit request, never run arbitrary or historical tasks."""
    import dispatcher as d
    d.init_db(db_path)
    token=str(uuid.uuid4())
    with closing(d.connect_db(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        for lock in conn.execute('SELECT * FROM project_locks').fetchall():
            d._recover_lock(conn,lock['project_root'],lock['lock_token'])
        # Reconcile completed workers, including failures before RUNNING.
        conn.execute("UPDATE task_queue SET state='DONE' WHERE state IN ('DISPATCHING','RUNNING') AND task_id IN (SELECT task_id FROM tasks WHERE status IN ('SUCCEEDED','FAILED','CANCELLED','BLOCKED'))")
        for row in conn.execute("SELECT q.*,t.status,t.run_token FROM task_queue q JOIN tasks t USING(task_id) WHERE q.state='DISPATCHING'").fetchall():
            if row['run_token'] is None and row['claim_pid'] and d.process_identity(row['claim_pid'])[0]=='gone':
                conn.execute("UPDATE task_queue SET state='WAITING',claim_token=NULL WHERE task_id=?",(row['task_id'],))
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
            identity=d.process_identity(os.getpid())[1]
            conn.execute("UPDATE task_queue SET state='DISPATCHING',reason=NULL,claim_token=?,claim_pid=?,claim_start=? WHERE task_id=?",
                         (token,os.getpid(),identity,candidate['task_id']))
        conn.commit()
    if not candidate:return {'started':False,'reason':'dependencies_or_empty'}
    try:
        task=d.launch_background(candidate['task_id'],db_path=db_path)
        with closing(d.connect_db(db_path)) as conn:
            if task['status'] in TERMINAL:
                conn.execute("UPDATE task_queue SET state='DONE',reason=? WHERE task_id=? AND claim_token=?",(task.get('error'),task['task_id'],token))
        return {'started':True,'task_id':task['task_id'],'status':task['status']}
    except d.ResourceBusy:
        with closing(d.connect_db(db_path)) as conn:
            conn.execute("UPDATE task_queue SET state='WAITING',reason='worker_busy',claim_token=NULL WHERE task_id=? AND claim_token=?",(candidate['task_id'],token))
        return {'started':False,'reason':'worker_busy'}
    except Exception as exc:
        with closing(d.connect_db(db_path)) as conn:
            conn.execute("UPDATE task_queue SET state='DONE',reason=? WHERE task_id=? AND claim_token=?",(type(exc).__name__,candidate['task_id'],token))
            conn.execute("UPDATE tasks SET status='FAILED',error=?,updated_at=? WHERE task_id=? AND run_token IS NULL",(str(exc),d.utc_now(),candidate['task_id']))
        raise


def wake(db_path):
    import dispatcher as d
    with closing(d.connect_db(db_path)) as conn:
        if not conn.execute("SELECT 1 FROM task_queue WHERE state='WAITING' LIMIT 1").fetchone():return
        owner=conn.execute('SELECT * FROM queue_watch WHERE id=1').fetchone()
        if owner and d.process_identity(owner['owner_pid'])[0]!='gone':return
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
        if owner and d.process_identity(owner['owner_pid'])[0]!='gone':return {'observer':'already_running'}
        conn.execute('INSERT OR REPLACE INTO queue_watch VALUES (1,?,?,?)',(token,os.getpid(),identity))
        conn.commit()
    deadline=time.monotonic()+max_seconds
    try:
        while time.monotonic()<deadline:
            result=pump(db_path)
            with closing(d.connect_db(db_path)) as conn:
                waiting=conn.execute("SELECT 1 FROM task_queue WHERE state='WAITING' LIMIT 1").fetchone()
            if not waiting or (not result['started'] and result['reason']!='worker_busy'):return result
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
        from policy import check_execution_authorization,load_agents
        agents=load_agents(Path(dispatch_root)/'agents.json')
        ok,reason=check_execution_authorization(task,agents,cfg)
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
