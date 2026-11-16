import importlib.util,json,os,shutil,sqlite3,sys,tempfile,threading,time,unittest,uuid,tomllib
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import dispatcher as d
import policy
import project_manager as pm
HOOK=ROOT/'team-policy.py'
sp=importlib.util.spec_from_file_location('v8_hook',HOOK);h=importlib.util.module_from_spec(sp);sp.loader.exec_module(h)

class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.control=self.root/'control';self.control.mkdir();self.db=self.control/'state.sqlite3'
        self.prompts=self.control/'prompts';self.prompts.mkdir();self.results=self.control/'results';self.results.mkdir()
        self.roots={pid:self.root/pid for pid in ('fixture-a','fixture-b')}
        for pid,p in self.roots.items():p.mkdir();(p/'AGENTS.md').write_text('ONLY_'+pid);(p/'local.txt').write_text(pid)
        self.cfg=policy.load_routing();self.cfg['project_registry']=str(self.control/'registry.json')
        self.cfg['project_whitelist']=[str(x) for x in self.roots.values()]
        self.entries=[dict(id=pid,name=pid,root=str(p),status='active',agents=['codex','claude','agy']) for pid,p in self.roots.items()]
        self.save_registry()
        (self.control/'routing.json').write_text(json.dumps(self.cfg));shutil.copy2(ROOT/'agents.json',self.control/'agents.json')
        self.patches=[mock.patch.object(d,'load_routing',return_value=self.cfg),mock.patch.object(policy,'load_routing',return_value=self.cfg),mock.patch.object(d,'PROMPTS_DIR',self.prompts)]
        for p in self.patches:p.start()
        d.init_db(self.db)
        pm.bind('session-a','fixture-a',self.db,self.cfg);pm.bind('session-b','fixture-b',self.db,self.cfg)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def save_registry(self): (self.control/'registry.json').write_text(json.dumps(dict(schema_version=1,projects=self.entries)))
    def task(self,pid='fixture-a',**extra):
        spec=dict(project_id=pid,project_root=str(self.roots[pid]),session_id='session-a' if pid=='fixture-a' else 'session-b',agent='codex',task_type='code_write',risk_level='medium',execution_mode='workspace_write',prompt='fixture request',idempotency_key=str(uuid.uuid4()))
        spec.update(extra);return d.create_task(spec,db_path=self.db,prompts_dir=self.prompts)
    def hook(self,tool,args,session='session-a'):
        return h.evaluate_hook(dict(hook_event_name='pre_tool_call',tool_name=tool,tool_input=args,session_id=session,cwd=str(self.roots['fixture-a'])),self.control)
    def test_required_project_binding_and_root_match(self):
        for extra in (dict(project_id='fixture-b'),dict(project_id=None),dict(session_id='session-b'),dict(project_root=str(self.root/'unknown'))):
            with self.subTest(extra=extra),self.assertRaises((policy.PolicyError,pm.ProjectError)):self.task(**extra)
    def test_context_and_snapshots_are_scoped(self):
        a=self.task();b=self.task('fixture-b')
        first=Path(a['prompt_path']).read_text();second=Path(b['prompt_path']).read_text()
        self.assertIn('ONLY_fixture-a',first);self.assertNotIn('ONLY_fixture-b',first)
        self.assertIn('ONLY_fixture-b',second);self.assertNotEqual(a['project_id'],b['project_id'])
    def test_sessions_remain_independent_on_switch(self):
        pm.bind('session-a','fixture-b',self.db,self.cfg)
        self.assertEqual(pm.bound_project('session-a',self.db),'fixture-b')
        self.assertEqual(pm.bound_project('session-b',self.db),'fixture-b')
        pm.bind('session-b','fixture-a',self.db,self.cfg)
        self.assertEqual(pm.bound_project('session-a',self.db),'fixture-b')
    def test_runtime_session_injection_is_validated_by_original_policy(self):
        prefix=f'python "{self.control / "dispatcher.py"}" '
        changed=self.hook('terminal',dict(command=prefix+'bind --project-id fixture-a'))
        self.assertEqual(changed['action'],'modify');self.assertIn('session-a',changed['args']['command'])
        content=dict(project_id='fixture-a',project_root=str(self.roots['fixture-a']),agent='codex',task_type='code_write',risk_level='medium',execution_mode='workspace_write',prompt='test',idempotency_key='injection-check')
        changed=self.hook('write_file',dict(path=str(self.control/'outside.json'),content=json.dumps(content)))
        self.assertEqual(changed['action'],'block')
        (self.control/'inbox').mkdir()
        changed=self.hook('write_file',dict(path=str(self.control/'inbox'/'new.json'),content=json.dumps(content)))
        self.assertEqual(changed['action'],'modify')
        self.assertEqual(json.loads(changed['args']['content'])['session_id'],'session-a')
    def test_migration_repeatable_preserves_rows_and_backfills_known_roots(self):
        row=self.task();c=d.connect_db(self.db)
        try:c.execute('UPDATE tasks SET project_id=NULL WHERE task_id=?',(row['task_id'],))
        finally:c.close()
        d.init_db(self.db);d.init_db(self.db)
        actual=d.get_task(row['task_id'],db_path=self.db)
        self.assertEqual(actual['project_id'],'fixture-a');self.assertEqual(actual['status'],row['status'])
    def test_hook_blocks_cross_project_reads_and_unbound_sessions(self):
        self.assertEqual(self.hook('read_file',dict(path=str(self.roots['fixture-a']/'local.txt'))),{})
        self.assertEqual(self.hook('read_file',dict(path=str(self.roots['fixture-b']/'local.txt')))['action'],'block')
        self.assertEqual(self.hook('read_file',dict(path=str(self.roots['fixture-a']/'local.txt')),'unknown')['action'],'block')
        self.assertEqual(self.hook('session_search',{})['action'],'block')
    def test_missing_production_registry_never_falls_back_to_legacy(self):
        broken=dict(self.cfg);broken.pop('project_registry')
        (self.control/'routing.json').write_text(json.dumps(broken))
        with mock.patch.object(pm,'PRODUCTION_DISPATCH',self.control):
            self.assertEqual(self.hook('read_file',dict(path=str(self.roots['fixture-a']/'local.txt')))['action'],'block')
    def test_hook_blocks_cross_task_and_forged_binding(self):
        a=self.task();b=self.task('fixture-b')
        prefix=f'python "{self.control / "dispatcher.py"}" '
        self.assertEqual(self.hook('terminal',dict(command=prefix+'status --task-id '+b['task_id']))['action'],'block')
        self.assertEqual(self.hook('terminal',dict(command=prefix+'bind --session-id session-b --project-id fixture-a'))['action'],'block')
        self.assertEqual(self.hook('terminal',dict(command=prefix+'bind --session-id session-a --project-id fixture-b')), {})
        self.assertEqual(self.hook('terminal',dict(command=prefix+'context --session-id session-a')), {})
    def test_traversal_hardlink_and_reparse_paths_rejected(self):
        with self.assertRaises(pm.ProjectError):pm.inside(str(self.roots['fixture-a']/'..'/'fixture-b'/'local.txt'),self.roots['fixture-a'])
        linked=self.roots['fixture-a']/'hard.txt';os.link(self.roots['fixture-b']/'local.txt',linked)
        with self.assertRaises(pm.ProjectError):pm.inside(linked,self.roots['fixture-a'])
        link=self.roots['fixture-a']/'escape'
        if os.name=='nt':
            import subprocess
            r=subprocess.run(['cmd','/c','mklink','/J',str(link),str(self.roots['fixture-b'])],capture_output=True)
            self.assertEqual(r.returncode,0)
        else:link.symlink_to(self.roots['fixture-b'],target_is_directory=True)
        with self.assertRaises(pm.ProjectError):pm.inside(link/'local.txt',self.roots['fixture-a'])
        self.assertEqual(self.hook('search_files',dict(path=str(self.roots['fixture-a']),pattern='x'))['action'],'block')
    def test_registry_overlap_and_root_redirect_fail_closed(self):
        self.entries[1]['root']=str(self.roots['fixture-a']/'nested');self.save_registry()
        with self.assertRaises(pm.ProjectError):pm.registry(self.cfg)
    def test_native_workers_receive_scoped_policies_without_global_mutation(self):
        agents=policy.load_agents()
        for agent,mode in (('codex','read_only'),('agy','read_only')):
            row=self.task(agent=agent,task_type='smoke_test',risk_level='low',execution_mode=mode)
            inv=policy.build_cli_invocation(row,'fixture',agents)
            actual=pm.worker_invocation(inv,row,self.cfg,agents,prepare=False)
            if agent=='codex':
                spec=json.loads(actual['stdin_text'])
                self.assertFalse(spec['write'])
                filesystem=tomllib.loads('policy='+spec['filesystem_toml'])['policy']
                self.assertTrue(any(pm.canonical(path)==pm.canonical(str(self.roots['fixture-b'])) and value=='deny' for path,value in filesystem.items()))
                self.assertTrue(actual['argv'][1].endswith('codex_worker.py'))
            else:
                settings=json.loads(actual['argv'][actual['argv'].index('--settings')+1])
                self.assertFalse(settings['allowNonWorkspaceAccess'])
                self.assertIn('command(*)',settings['permissions']['deny'])
                self.assertIn('mcp(*)',settings['permissions']['deny'])
                self.assertIn('write_file(*)',settings['permissions']['deny'])
                self.assertTrue(any(pm.canonical(rule[len('read_file('):-1])==pm.canonical(str(self.roots['fixture-b'])) for rule in settings['permissions']['deny'] if rule.startswith('read_file(')))
    def test_agy_requires_successful_final_envelope(self):
        for payload,expected in (({'event':'step_update'},'FAILED'),({'event':'result','result':{'status':'ERROR'}},'FAILED'),({'event':'result','result':{'status':'SUCCESS'}},'SUCCEEDED')):
            row=self.task(agent='agy',task_type='smoke_test',risk_level='low',execution_mode='read_only')
            def execute(inv,out,err,heartbeat):
                Path(out).write_text(json.dumps(payload));Path(err).write_text('')
                return dict(exit_code=0,timed_out=False,cancelled=False,tree_stopped=True)
            with mock.patch.object(pm,'worker_invocation',side_effect=lambda inv,*args:inv),mock.patch.object(d,'execute_streaming',side_effect=execute):
                result=d.run_task(row['task_id'],db_path=self.db,results_dir=self.results)
            self.assertEqual(result['status'],expected)
    def test_prompt_path_must_belong_to_selected_project(self):
        with self.assertRaises(pm.ProjectError):self.task(prompt='',prompt_path=str(self.roots['fixture-b']/'local.txt'))
    def test_parallel_projects_serialize_and_locks_carry_project_id(self):
        a=self.task();b=self.task('fixture-b');observed={};started=threading.Event();release=threading.Event()
        def execute(inv,out,err,heartbeat):
            Path(out).write_text('{}');Path(err).write_text('')
            heartbeat(os.getpid(),d.process_identity(os.getpid())[1]);started.set();release.wait(5)
            return dict(exit_code=0,timed_out=False,cancelled=False,tree_stopped=True)
        def run():observed.update(d.run_task(a['task_id'],db_path=self.db,results_dir=self.results))
        with mock.patch.object(pm,'worker_invocation',side_effect=lambda inv,*args:inv),mock.patch.object(d,'execute_streaming',side_effect=execute):
            t=threading.Thread(target=run);t.start();self.assertTrue(started.wait(5))
            c=d.connect_db(self.db)
            try:self.assertEqual(c.execute('SELECT project_id FROM project_locks').fetchone()[0],'fixture-a')
            finally:c.close()
            try:
                with self.assertRaises(policy.PolicyError):d.run_task(b['task_id'],db_path=self.db,results_dir=self.results)
            finally:release.set();t.join(8)
        self.assertEqual(observed['status'],'SUCCEEDED')
        with mock.patch.object(pm,'worker_invocation',side_effect=lambda inv,*args:inv),mock.patch.object(d,'execute_streaming',side_effect=execute):
            result=d.run_task(b['task_id'],db_path=self.db,results_dir=self.results)
        self.assertEqual(result['project_id'],'fixture-b');self.assertEqual(result['status'],'SUCCEEDED')
