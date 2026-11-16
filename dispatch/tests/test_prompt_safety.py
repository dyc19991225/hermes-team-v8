from _support import FIXTURE_PROJECT, FIXTURE_SANDBOX
import hashlib, sqlite3, tempfile, unittest, uuid, threading
from pathlib import Path
from unittest import mock
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dispatcher as d

class PromptSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=self.root/'state.sqlite3';self.prompts=self.root/'prompts'
        cfg=d.load_routing();cfg.pop('project_registry',None)
        patch=mock.patch.object(d,'load_routing',return_value=cfg);patch.start();self.addCleanup(patch.stop)
    def tearDown(self):self.tmp.cleanup()
    def spec(self):return dict(task_id=str(uuid.uuid4()),idempotency_key=str(uuid.uuid4()),agent='codex',project_root=str(FIXTURE_PROJECT),task_type='code_review',risk_level='low',execution_mode='read_only',prompt='original')
    def test_rejected_duplicates_preserve_task_content_events_locks_all_states(self):
        for state in ('CREATED','QUEUED','RUNNING'):
            for same_key in (True,False):
                with self.subTest(state=state,same_key=same_key):
                    spec=self.spec();row=d.create_task(spec,db_path=self.db,prompts_dir=self.prompts)
                    c=d.connect_db(self.db)
                    try:
                        c.execute('UPDATE tasks SET status=? WHERE task_id=?',(state,row['task_id']))
                        before=[list(r) for r in c.execute('SELECT * FROM task_events')]
                    finally:c.close()
                    dup=dict(spec,prompt='overwrite')
                    if not same_key:dup['idempotency_key']=str(uuid.uuid4())
                    with self.assertRaises(d.PolicyError):d.create_task(dup,db_path=self.db,prompts_dir=self.prompts)
                    self.assertEqual(Path(row['prompt_path']).read_text(),'original')
                    self.assertEqual(d.get_task(row['task_id'],db_path=self.db)['status'],state)
                    c=d.connect_db(self.db)
                    try:self.assertEqual(before,[list(r) for r in c.execute('SELECT * FROM task_events')])
                    finally:c.close()
    def test_existing_orphan_file_is_not_overwritten(self):
        spec=self.spec();self.prompts.mkdir();p=self.prompts/(spec['task_id']+'.txt');p.write_text('orphan')
        with self.assertRaises(FileExistsError):d.create_task(spec,db_path=self.db,prompts_dir=self.prompts)
        self.assertEqual(p.read_text(),'orphan')
    def test_transaction_failure_removes_only_new_snapshot(self):
        spec=self.spec()
        with mock.patch.object(d,'_record_event',side_effect=sqlite3.OperationalError('fixture failure')):
            with self.assertRaises(sqlite3.OperationalError):d.create_task(spec,db_path=self.db,prompts_dir=self.prompts)
        self.assertFalse((self.prompts/(spec['task_id']+'.txt')).exists())
    def test_changed_prompt_is_rejected_before_execution(self):
        row=d.create_task(self.spec(),db_path=self.db,prompts_dir=self.prompts)
        Path(row['prompt_path']).write_text('changed')
        with self.assertRaises(d.PolicyError):d.dry_run_task(row['task_id'],db_path=self.db,results_dir=self.root/'results')
    def test_concurrent_create_one_winner(self):
        spec=self.spec();d.init_db(self.db);out=[]
        def create():
            try:d.create_task(spec,db_path=self.db,prompts_dir=self.prompts);out.append('ok')
            except d.PolicyError:out.append('duplicate')
        threads=[threading.Thread(target=create) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertCountEqual(out,['ok','duplicate'])
