"""Public configuration and Hook entrypoints, using only owned temporary trees."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base/'public-copy'
        self.dispatch = self.repo/'dispatch'
        self.dispatch.mkdir(parents=True)
        for path in ROOT.glob('*.py'):
            shutil.copy2(path, self.dispatch/path.name)
        (self.repo/'scripts').mkdir()
        shutil.copy2(ROOT.parent/'scripts'/'configure.py', self.repo/'scripts'/'configure.py')
        shutil.copytree(ROOT.parent/'config', self.repo/'config')
        self.project = self.base/'business-project'
        self.project.mkdir()

    def cli(self, file, *args, payload=None):
        return subprocess.run([sys.executable, '-X', 'utf8', '-B', str(file), *args],
                              input=json.dumps(payload) if payload else None, capture_output=True,
                              text=True, encoding='utf-8', timeout=15)

    def configure(self, project=None):
        return self.cli(self.repo/'scripts'/'configure.py', '--project-root', str(project or self.project),
                        '--codex', sys.executable)

    def test_configure_init_bind_and_real_hook_use_only_the_public_copy(self):
        result = self.configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        agents = json.loads((self.dispatch/'agents.json').read_text(encoding='utf-8'))
        self.assertTrue(agents['agents']['codex']['enabled'])
        self.assertFalse(agents['agents']['claude']['enabled'])
        self.assertFalse(agents['agents']['agy']['enabled'])
        for args in (('init',), ('bind', '--session-id', 'public-fixture', '--project-id', 'project-alpha')):
            result = self.cli(self.dispatch/'dispatcher.py', *args)
            self.assertEqual(result.returncode, 0, result.stderr)
        command = 'python "'+str(self.dispatch/'dispatcher.py')+'" queue'
        payload = dict(hook_event_name='pre_tool_call', tool_name='terminal',
                       tool_input={'command': command}, session_id='public-fixture', cwd=str(self.project))
        result = self.cli(self.dispatch/'team-policy.py', payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response['action'], 'modify')
        self.assertIn('public-fixture', response['args']['command'])

    def test_reconfigure_refuses_to_overwrite_existing_local_configuration(self):
        self.assertEqual(self.configure().returncode, 0)
        paths = [self.dispatch/'agents.json', self.dispatch/'routing.json', self.repo/'local-config'/'registry.json']
        before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        result = self.configure()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already exists', result.stderr)
        self.assertEqual(before, [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths])

    def test_missing_registry_fails_closed_after_copying_to_an_arbitrary_path(self):
        self.assertEqual(self.configure().returncode, 0)
        path = self.dispatch/'routing.json'
        cfg = json.loads(path.read_text(encoding='utf-8'))
        cfg.pop('project_registry')
        path.write_text(json.dumps(cfg), encoding='utf-8')
        result = self.cli(self.dispatch/'dispatcher.py', 'init')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('registry configuration missing', result.stderr)
        payload = dict(hook_event_name='pre_tool_call', tool_name='read_file',
                       tool_input={'path': str(self.project/'local.txt')}, session_id='public-fixture', cwd=str(self.project))
        result = self.cli(self.dispatch/'team-policy.py', payload=payload)
        self.assertEqual(json.loads(result.stdout)['action'], 'block')

    def test_configure_rejects_business_project_inside_the_control_repository(self):
        result = self.configure(self.repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dispatch/'agents.json').exists())


if __name__ == '__main__':
    unittest.main()
