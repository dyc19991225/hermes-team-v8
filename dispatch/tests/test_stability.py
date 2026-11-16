"""V7.1 focused regression tests. Only local Python fixtures and temporary DBs.

No model API calls, business project commands, builds, formatters or deployment.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import dispatcher as d
import policy
from process_runtime import process_identity, OwnedProcess, execute_streaming
HOOK=ROOT/'team-policy.py'
sp=importlib.util.spec_from_file_location('v71_hook',HOOK)
h=importlib.util.module_from_spec(sp); sp.loader.exec_module(h)

def until(predicate, timeout=8):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=predicate()
        if value: return value
        time.sleep(.03)
    raise AssertionError('Timed out waiting for test condition')

class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.db=self.root/'state.sqlite3'; self.results=self.root/'results'
        self.results.mkdir(); self.prompts=self.root/'prompts'; self.threads=[]
        self.agents=policy.load_agents(); self.routing=policy.load_routing(); self.routing.pop('project_registry',None)
        self.agents['agents']['codex']['executable']=sys.executable
        self.routing['project_whitelist']=[str(self.root)]
        self.patches=[mock.patch.object(d,'load_agents',return_value=self.agents),
            mock.patch.object(d,'load_routing',return_value=self.routing)]
        for p in self.patches: p.start()
        d.init_db(self.db)

    def tearDown(self):
        for t in self.threads:
            if t.is_alive(): t.join(12)
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def task(self, **extra):
        spec=dict(idempotency_key=str(uuid.uuid4()),agent='codex',project_root=str(self.root),
            task_type='code_write',risk_level='medium',execution_mode='workspace_write',prompt='fixture',timeout=10)
        spec.update(extra)
        return d.create_task(spec,db_path=self.db,prompts_dir=self.prompts)['task_id']

    def conn(self): return d.connect_db(self.db)
    def sql(self, sql, args=()):
        c=self.conn()
        try: return [dict(x) for x in c.execute(sql,args)]
        finally: c.close()
    def invocation(self, code, timeout=10, stdin_text=None):
        return dict(argv=[sys.executable,'-u','-c',code],cwd=str(self.root),stdin_text=stdin_text,timeout=timeout,json_mode='jsonl')
    def run_async(self, tid, inv):
        result={}
        def run():
            try:
                with mock.patch.object(d,'build_cli_invocation',return_value=inv):
                    result.update(d.run_task(tid,db_path=self.db,results_dir=self.results))
            except BaseException as exc: result['exception']=repr(exc)
        t=threading.Thread(target=run); t.start(); self.threads.append(t)
        until(lambda:d.get_task(tid,db_path=self.db)['status']=='RUNNING')
        return t,result
    def lock(self,tid): return self.sql('SELECT * FROM project_locks WHERE task_id=?',(tid,))
    def seed_lock(self,tid,*,pid=99999999,start='known-dead',token=None,lease=0):
        token=token or str(uuid.uuid4())
        self.sql("UPDATE tasks SET status='RUNNING',run_token=?,owner_pid=?,owner_start=?,lease_until=? WHERE task_id=?",(token,pid,start,lease,tid))
        self.sql("INSERT INTO project_locks(project_root,task_id,acquired_at,lock_token,owner_pid,owner_start,lease_until) VALUES (?,?,'old',?,?,?,?)",
            (str(self.root),tid,token,pid,start,lease))
        self.sql('UPDATE tasks SET child_pid=99999998,child_start=? WHERE task_id=?',('known-dead-child',tid))
        self.sql('UPDATE project_locks SET child_pid=99999998,child_start=? WHERE task_id=?',('known-dead-child',tid))
        return token
    def recover(self,token):
        c=self.conn()
        try:
            c.execute('BEGIN IMMEDIATE'); ok=d._recover_lock(c,str(self.root),token); c.execute('COMMIT'); return ok
        finally: c.close()

    def test_partial_bytes_stderr_backpressure_and_heartbeat_before_exit(self):
        tid=self.task()
        inv=self.invocation("import sys,time; sys.stdout.write('{\"value\":'); sys.stdout.flush(); sys.stderr.write('x'*200000); sys.stderr.flush(); time.sleep(2.3); print('42}')")
        t,result=self.run_async(tid,inv)
        out=self.results/f'{tid}.stdout.log'; err=self.results/f'{tid}.stderr.log'
        until(lambda:out.exists() and out.stat().st_size>0 and err.exists() and err.stat().st_size==200000)
        self.assertTrue(t.is_alive()); self.assertEqual(out.read_bytes(),b'{"value":')
        first=d.get_task(tid,db_path=self.db)['updated_at']
        until(lambda:d.get_task(tid,db_path=self.db)['updated_at']!=first)
        self.assertGreaterEqual(len(self.sql("SELECT * FROM task_events WHERE detail LIKE '%heartbeat%'")),2)
        self.assertEqual(self.lock(tid)[0]['owner_pid'],os.getpid())
        t.join(8); self.assertEqual(result['status'],'SUCCEEDED'); self.assertFalse(self.lock(tid))

    def test_silent_process_still_has_heartbeat_and_status_poll(self):
        tid=self.task(); t,res=self.run_async(tid,self.invocation("import time; time.sleep(2.2); print('{}')"))
        until(lambda:len(self.sql("SELECT * FROM task_events WHERE detail LIKE '%heartbeat%'"))>=2)
        status=d.inspect_task(tid,db_path=self.db)
        self.assertEqual(status['status'],'RUNNING'); self.assertEqual(status['streams']['stdout']['bytes'],0)
        t.join(8); self.assertEqual(res['status'],'SUCCEEDED')

    def tree_code(self):
        pidfile=self.root/'grandchild.pid'
        code="import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path("+repr(str(pidfile))+").write_text(str(p.pid)); print('{}',flush=True); time.sleep(60)"
        return code,pidfile

    def test_timeout_terminates_process_tree_and_releases_lock(self):
        tid=self.task(); code,pidfile=self.tree_code()
        with mock.patch.object(d,'build_cli_invocation',return_value=self.invocation(code,2)):
            result=d.run_task(tid,db_path=self.db,results_dir=self.results)
        self.assertTrue(result['timed_out']); self.assertEqual(result['status'],'FAILED')
        self.assertEqual(process_identity(int(pidfile.read_text()))[0],'gone'); self.assertFalse(self.lock(tid))

    def test_cancel_is_request_then_tree_stops_then_lock_releases(self):
        tid=self.task(); code,pidfile=self.tree_code(); t,res=self.run_async(tid,self.invocation(code))
        until(pidfile.exists)
        row=d.cancel_task(tid,db_path=self.db)
        self.assertEqual(row['status'],'RUNNING'); self.assertEqual(row['cancel_requested'],1)
        t.join(8); self.assertEqual(res['status'],'CANCELLED')
        self.assertEqual(process_identity(int(pidfile.read_text()))[0],'gone'); self.assertFalse(self.lock(tid))

    def test_blocked_stdin_does_not_block_timeout(self):
        tid=self.task()
        with mock.patch.object(d,'build_cli_invocation',return_value=self.invocation('import time; time.sleep(60)',1,'x'*1000000)):
            result=d.run_task(tid,db_path=self.db,results_dir=self.results)
        self.assertTrue(result['timed_out']); self.assertLess(result['duration_seconds'],8)

    def test_spawn_failure_finalizes_and_releases_only_owned_lock(self):
        tid=self.task(); inv=self.invocation(''); inv['argv']=[str(self.root/'missing.exe')]
        with mock.patch.object(d,'build_cli_invocation',return_value=inv):
            result=d.run_task(tid,db_path=self.db,results_dir=self.results)
        self.assertEqual(result['status'],'FAILED'); self.assertFalse(self.lock(tid))

    def test_result_write_failure_does_not_leave_running_or_lock(self):
        tid=self.task()
        with mock.patch.object(d,'build_cli_invocation',return_value=self.invocation("print('{}')")), mock.patch.object(d.os,'replace',side_effect=OSError('fixture disk failure')):
            result=d.run_task(tid,db_path=self.db,results_dir=self.results)
        self.assertEqual(result['status'],'FAILED'); self.assertFalse(self.lock(tid))
        self.assertIn('Result write failed',d.get_task(tid,db_path=self.db)['error'])

    def test_unverified_tree_cleanup_retains_lock(self):
        tid=self.task()
        with mock.patch.object(d,'build_cli_invocation',return_value=self.invocation('')), mock.patch.object(d,'execute_streaming',side_effect=d.UnsafeCleanup('fixture cleanup failure')):
            result=d.run_task(tid,db_path=self.db,results_dir=self.results)
        self.assertEqual(result['status'],'FAILED'); self.assertTrue(self.lock(tid))
        self.assertIn('lock retained',result['error'])

    def test_heartbeat_failure_stops_owned_child_and_finalizes(self):
        tid=self.task(); original=d._record_event
        def record(*args,**kwargs):
            if str(args[-1]).startswith('{"heartbeat"'): raise sqlite3.OperationalError('fixture heartbeat failure')
            return original(*args,**kwargs)
        with mock.patch.object(d,'build_cli_invocation',return_value=self.invocation('import time; time.sleep(60)')), mock.patch.object(d,'_record_event',side_effect=record):
            result=d.run_task(tid,db_path=self.db,results_dir=self.results)
        self.assertEqual(result['status'],'FAILED'); self.assertFalse(self.lock(tid))

    def test_late_cancel_consistent_in_result_and_database(self):
        tid=self.task(); original=d.execute_streaming
        def execute(*args,**kwargs):
            outcome=original(*args,**kwargs)
            d.cancel_task(tid,db_path=self.db)
            return outcome
        with mock.patch.object(d,'build_cli_invocation',return_value=self.invocation("print('{}')")), mock.patch.object(d,'execute_streaming',side_effect=execute):
            result=d.run_task(tid,db_path=self.db,results_dir=self.results)
        self.assertEqual(result['status'],'CANCELLED')
        self.assertEqual(json.loads((self.results/f'{tid}.json').read_text())['status'],'CANCELLED')
        self.assertEqual(d.get_task(tid,db_path=self.db)['status'],'CANCELLED')

    def test_large_output_keeps_full_log_but_bounds_json_parse(self):
        tid=self.task()
        with mock.patch.object(d,'build_cli_invocation',return_value=self.invocation("print('x'*5000000)")):
            result=d.run_task(tid,db_path=self.db,results_dir=self.results)
        self.assertEqual(result['status'],'FAILED'); self.assertIn('parse limit',result['error'])
        self.assertGreater((self.results/f'{tid}.stdout.log').stat().st_size,5000000)

    def test_lock_token_changed_while_running_never_deleted(self):
        tid=self.task(); t,res=self.run_async(tid,self.invocation('import time; time.sleep(30)'))
        self.sql("UPDATE project_locks SET lock_token='other-owner' WHERE task_id=?",(tid,))
        t.join(8); self.assertEqual(res['status'],'FAILED')
        self.assertEqual(self.lock(tid)[0]['lock_token'],'other-owner')

    def test_stale_lock_requires_expiry_dead_identities_and_matching_token(self):
        tid=self.task(); token=self.seed_lock(tid)
        self.assertFalse(self.recover('mismatched-token')); self.assertTrue(self.lock(tid))
        self.assertTrue(self.recover(token)); self.assertFalse(self.lock(tid))
        self.assertEqual(d.get_task(tid,db_path=self.db)['status'],'FAILED')

    def test_live_reused_or_unknown_owner_never_reclaimed(self):
        tid=self.task(); token=self.seed_lock(tid,pid=os.getpid(),start='different-start')
        self.assertFalse(self.recover(token))
        with self.assertRaises(policy.PolicyError): d.cancel_task(tid,db_path=self.db)
        with mock.patch.object(d,'process_identity',return_value=('unknown',None)):
            self.assertFalse(self.recover(token))
        self.assertTrue(self.lock(tid))

    def test_live_child_prevents_stale_lock_recovery(self):
        tid=self.task(); token=self.seed_lock(tid)
        self.sql('UPDATE project_locks SET child_pid=?,child_start=?',(os.getpid(),process_identity(os.getpid())[1]))
        self.assertFalse(self.recover(token)); self.assertTrue(self.lock(tid))

    def test_unexpired_lease_never_reclaimed(self):
        tid=self.task(); token=self.seed_lock(tid,lease=time.time()+100)
        self.assertFalse(self.recover(token))

    def test_unregistered_child_is_not_assumed_dead(self):
        tid=self.task(); token=self.seed_lock(tid)
        self.sql('UPDATE project_locks SET child_pid=NULL,child_start=NULL')
        self.assertFalse(self.recover(token))

    def test_dry_run_cannot_overwrite_background_reservation(self):
        tid=self.task()
        self.sql("UPDATE tasks SET status='QUEUED',run_token='reservation' WHERE task_id=?",(tid,))
        with self.assertRaises(policy.PolicyError): d.dry_run_task(tid,db_path=self.db)
        self.assertEqual(d.get_task(tid,db_path=self.db)['status'],'QUEUED')

    def test_legacy_lock_and_cancel_fail_closed(self):
        tid=self.task(); self.seed_lock(tid)
        self.sql('UPDATE project_locks SET lock_token=NULL,owner_pid=NULL,owner_start=NULL')
        self.sql('UPDATE tasks SET run_token=NULL,owner_pid=NULL,owner_start=NULL')
        self.assertFalse(self.recover('anything'))
        with self.assertRaises(policy.PolicyError): d.cancel_task(tid,db_path=self.db)
        self.assertTrue(self.lock(tid))

    def test_cancel_before_start_and_cancel_twice(self):
        tid=self.task(); self.assertEqual(d.cancel_task(tid,db_path=self.db)['status'],'CANCELLED')
        with self.assertRaises(policy.PolicyError): d.cancel_task(tid,db_path=self.db)

    def test_sqlite_migration_preserves_legacy_rows_and_is_repeatable(self):
        tid=self.task(); self.seed_lock(tid)
        # Downgrade only this fixture to the pre-V7.1 schema, then migrate twice.
        new={ 'tasks':['run_token','owner_pid','owner_start','child_pid','child_start','heartbeat_at','lease_until','cancel_requested','timeout_seconds'],
            'project_locks':['lock_token','owner_pid','owner_start','child_pid','child_start','heartbeat_at','lease_until'] }
        c=self.conn()
        try:
            for table,cols in new.items():
                for col in cols: c.execute('ALTER TABLE '+table+' DROP COLUMN '+col)
        finally: c.close()
        d.init_db(self.db); d.init_db(self.db)
        row=d.get_task(tid,db_path=self.db)
        self.assertEqual(row['status'],'RUNNING'); self.assertIsNone(row['run_token']); self.assertEqual(len(self.lock(tid)),1)
        self.assertEqual(self.sql('PRAGMA integrity_check')[0]['integrity_check'],'ok')

    def hook(self,command):
        (self.root/'agents.json').write_text(json.dumps(self.agents)); (self.root/'routing.json').write_text(json.dumps(self.routing))
        return h.evaluate_hook(dict(hook_event_name='pre_tool_call',tool_name='terminal',tool_input={'command':command},cwd=str(self.root)),self.root)

    def test_hook_only_narrow_cancel_addition(self):
        tid=self.task(risk_level='high',approval_state='pending')
        cmd=f'python "{self.root / "dispatcher.py"}" cancel --task-id {tid}'
        self.assertEqual(self.hook(cmd),{})
        for suffix in (' --timeout 5',' --force',' --task-id '+tid,' --db other.sqlite3','; dir'):
            self.assertEqual(self.hook(cmd+suffix).get('action'),'block')
        for cmd in ('python -c "print(1)"',f'python "{self.root / "dispatcher.py"}" _worker --task-id {tid} --token abc'):
            self.assertEqual(self.hook(cmd).get('action'),'block')
        for tool in ('process_manage','execute_code','patch'):
            self.assertEqual(h.evaluate_hook(dict(hook_event_name='pre_tool_call',tool_name=tool,tool_input={}),self.root).get('action'),'block')

    def test_cli_permission_modes_timeout_bounds_and_streaming_flags(self):
        agents=policy.load_agents()
        base=dict(project_root=str(self.root),task_type='code_write',execution_mode='default',agent='claude')
        inv=policy.build_cli_invocation(base,'fixture',agents)
        self.assertIn('manual',inv['argv']); self.assertIn('none',inv['argv']); self.assertIn('stream-json',inv['argv'])
        for timeout in (-1,0,901,True):
            with self.assertRaises(policy.PolicyError): policy.build_cli_invocation(dict(base,timeout=timeout),'fixture',agents)
        agy=policy.build_cli_invocation(dict(base,agent='agy',execution_mode='plan',task_type='code_review'),'fixture',agents)
        self.assertIn('--sandbox',agy['argv']); self.assertIn('stream-json',agy['argv']); self.assertIn('gemini-3.8-flash-low',agy['argv'])

    def test_maintenance_gate_blocks_mutations_and_keeps_status(self):
        tid=self.task()
        (self.root/'.v71-maintenance').write_text('fixture')
        prefix=f'python "{self.root / "dispatcher.py"}" '
        self.assertEqual(self.hook(prefix+'run --task-id '+tid).get('action'),'block')
        self.assertEqual(self.hook(prefix+'cancel --task-id '+tid).get('action'),'block')
        self.assertEqual(self.hook(prefix+'status --task-id '+tid),{})

    def test_agent_structured_error_not_success(self):
        tid=self.task()
        with mock.patch.object(d,'build_cli_invocation',return_value=self.invocation("print('{\"type\":\"result\",\"is_error\":true}')")):
            result=d.run_task(tid,db_path=self.db,results_dir=self.results)
        self.assertEqual(result['status'],'FAILED')

    def setup_background_fixture(self):
        for name in ('dispatcher.py','policy.py','process_runtime.py','project_manager.py','workflow.py'): shutil.copy2(ROOT/name,self.root/name)
        (self.root/'agents.json').write_text(json.dumps(self.agents)); (self.root/'routing.json').write_text(json.dumps(self.routing))
        code,pidfile=self.tree_code()
        (self.root/'fixture.py').write_text(code)
        # Test-only shim in a temporary copy. Production never accepts arbitrary Python.
        p=self.root/'dispatcher.py'; source=p.read_text()
        shim="\ndef load_routing():\n    return json.loads((DISPATCH_ROOT/'routing.json').read_text(encoding='utf-8'))\n\ndef build_cli_invocation(task, prompt, agents=None):\n    return {'argv':[sys.executable,'-u',str(DISPATCH_ROOT/'fixture.py')], 'cwd':str(DISPATCH_ROOT), 'stdin_text':None,'timeout':60,'json_mode':'jsonl'}\n\n"
        source=source.replace('if __name__ == "__main__":',shim+'if __name__ == "__main__":')
        p.write_text(source)
        return pidfile

    def test_background_returns_while_worker_runs_then_cancel(self):
        pidfile=self.setup_background_fixture(); tid=self.task()
        before=time.monotonic()
        proc=subprocess.run([sys.executable,str(self.root/'dispatcher.py'),'--db',str(self.db),'run','--task-id',tid],capture_output=True,text=True,timeout=15)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr); self.assertLess(time.monotonic()-before,8)
        row=d.get_task(tid,db_path=self.db); self.assertEqual(row['status'],'RUNNING')
        until(pidfile.exists); self.assertEqual(process_identity(int(pidfile.read_text()))[0],'alive')
        d.cancel_task(tid,db_path=self.db)
        until(lambda:d.get_task(tid,db_path=self.db)['status']=='CANCELLED')
        self.assertEqual(process_identity(int(pidfile.read_text()))[0],'gone'); self.assertFalse(self.lock(tid))
        until(lambda:process_identity(row['owner_pid'])[0]=='gone')

    @unittest.skipUnless(os.name=='nt','Windows Job Object forced dispatcher exit')
    def test_forced_worker_death_kills_descendants_then_safe_recovery(self):
        pidfile=self.setup_background_fixture(); tid=self.task()
        proc=subprocess.run([sys.executable,str(self.root/'dispatcher.py'),'--db',str(self.db),'run','--task-id',tid],capture_output=True,text=True,timeout=15)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr); until(pidfile.exists)
        row=until(lambda:(r if (r:=d.get_task(tid,db_path=self.db)).get('child_pid') else None))
        from process_runtime import OpenProcess,CloseHandle,K
        import ctypes
        handle=OpenProcess(1,False,row['owner_pid']); self.assertTrue(handle)
        try:
            K.TerminateProcess.argtypes=[ctypes.c_void_p,ctypes.c_uint]; K.TerminateProcess.restype=ctypes.c_int
            self.assertTrue(K.TerminateProcess(handle,137))
        finally: CloseHandle(handle)
        until(lambda:process_identity(row['owner_pid'])[0]=='gone')
        until(lambda:process_identity(row['child_pid'])[0]=='gone')
        until(lambda:process_identity(int(pidfile.read_text()))[0]=='gone')
        self.assertTrue(self.lock(tid))
        self.sql('UPDATE tasks SET lease_until=0'); self.sql('UPDATE project_locks SET lease_until=0')
        self.assertEqual(d.cancel_task(tid,db_path=self.db)['status'],'CANCELLED'); self.assertFalse(self.lock(tid))

if __name__=='__main__': unittest.main(verbosity=2)
