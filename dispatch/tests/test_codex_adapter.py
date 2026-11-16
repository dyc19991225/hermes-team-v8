import io,json,sys,unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import codex_worker as worker

class Buffer(io.StringIO):
    def close(self):pass

class FakeProcess:
    def __init__(self,responses):
        self.stdin=Buffer();self.stdout=Buffer(''.join(json.dumps(x)+'\n' for x in responses));self.stopped=False
    def terminate(self):self.stopped=True
    def wait(self,timeout=None):return 0
    def kill(self):self.stopped=True

class CodexAdapterTests(unittest.TestCase):
    def execute(self,profile='hermes_v8',sandbox='workspaceWrite',completion='completed'):
        spec=dict(executable='codex.exe',root='E:/fixture-a',write=True,filesystem_toml='{}',runtime='E:/fixture-runtime',disabled_mcp=[],prompt='fixture')
        responses=[{'id':1,'result':{}},{'id':3,'result':{'activePermissionProfile':{'id':profile},'sandbox':{'type':sandbox},'thread':{'id':'thread-fixture'}}},{'id':4,'result':{'turn':{'id':'turn-fixture'}}},{'method':'turn/completed','params':{'turn':{'id':'turn-fixture','status':completion}}}]
        self.process=FakeProcess(responses);self.output=Buffer()
        with mock.patch.object(worker.subprocess,'Popen',return_value=self.process),mock.patch.object(worker.sys,'stdin',Buffer(json.dumps(spec))),mock.patch.object(worker.sys,'stdout',self.output):return worker.main()
    def test_required_profile_acknowledged_before_turn(self):
        self.assertEqual(self.execute(),0);self.assertTrue(self.process.stopped)
        messages=[json.loads(x) for x in self.process.stdin.getvalue().splitlines()]
        start=next(x for x in messages if x['method']=='thread/start')
        self.assertEqual(start['params']['permissions'],'hermes_v8');self.assertEqual(start['params']['approvalPolicy'],'never')
    def test_wrong_profile_stops_before_any_model_turn(self):
        with self.assertRaises(RuntimeError):self.execute(profile=':danger-full-access')
        self.assertNotIn('"turn/start"',self.process.stdin.getvalue());self.assertTrue(self.process.stopped)
    def test_dangerous_fallback_stops_before_any_model_turn(self):
        with self.assertRaises(RuntimeError):self.execute(sandbox='dangerFullAccess')
        self.assertNotIn('"turn/start"',self.process.stdin.getvalue())
    def test_failed_model_turn_reports_failure(self):
        self.assertEqual(self.execute(completion='failed'),1)
        result=json.loads(self.output.getvalue().splitlines()[-1]);self.assertTrue(result['is_error'])
