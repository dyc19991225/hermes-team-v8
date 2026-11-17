"""Wizard regression: owned temporary homes, fake CLIs, no model requests."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
import windows_setup as setup
from ruamel.yaml import YAML


@contextlib.contextmanager
def database(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='team-wizard-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base/'public repo 中文'
        self.repo.mkdir()
        shutil.copytree(ROOT/'config', self.repo/'config')
        shutil.copytree(ROOT/'skills', self.repo/'skills')
        (self.repo/'dispatch').mkdir()
        for path in (ROOT/'dispatch').glob('*.py'):
            shutil.copy2(path, self.repo/'dispatch'/path.name)
        self.project = self.base/'business 中文'
        self.project.mkdir()
        self.home = self.base/'hermes home'
        self.home.mkdir()
        self.original = b'# keep this comment\nmodel: fixture-model\nchannels:\n  example:\n    token: fixture-only-no-real-key\nhooks_auto_accept: false\nhooks:\n  pre_tool_call:\n  - matcher: other-tool\n    command: C:/example/other-check.py\n    timeout: 4\n'
        (self.home/'config.yaml').write_bytes(self.original)
        (self.home/'auth.json').write_text('fixture-auth-must-not-change', encoding='utf-8')

    def plan(self, **kwargs):
        return setup.plan_setup(self.repo, self.project, 'project-alpha', {'codex': Path(sys.executable)},
                                self.home, Path(sys.executable), **kwargs)

    def test_merge_preserves_comments_channels_other_hooks_and_no_auto_approval(self):
        plan = self.plan()
        backup = setup.apply_plan(plan)
        raw = (self.home/'config.yaml').read_bytes()
        cfg = YAML(typ='safe').load(raw)
        self.assertIn(b'# keep this comment', raw)
        self.assertEqual(cfg['model'], 'fixture-model')
        self.assertEqual(cfg['channels']['example']['token'], 'fixture-only-no-real-key')
        self.assertFalse(cfg['hooks_auto_accept'])
        hooks = cfg['hooks']['pre_tool_call']
        self.assertEqual(len(hooks), 2)
        self.assertEqual(hooks[0]['command'], 'C:/example/other-check.py')
        self.assertIn(sys.executable, hooks[1]['command'])
        self.assertTrue(hooks[1]['fail_closed'])
        self.assertFalse((self.home/'shell-hooks-allowlist.json').exists())
        self.assertEqual((self.home/'auth.json').read_text(), 'fixture-auth-must-not-change')
        self.assertTrue((backup/'manifest.json').is_file())

    def test_repeat_install_has_no_changes_and_does_not_duplicate_hook(self):
        setup.apply_plan(self.plan())
        self.assertEqual(self.plan().changes, [])
        self.assertIsNone(setup.apply_plan(self.plan()))

    def test_backup_restore_roundtrips_original_bytes_and_removes_new_configs(self):
        backup = setup.apply_plan(self.plan())
        setup.restore_backup(self.repo, backup)
        self.assertEqual((self.home/'config.yaml').read_bytes(), self.original)
        self.assertFalse((self.repo/'dispatch'/'agents.json').exists())
        self.assertFalse((self.home/'skills'/'team-project-manager'/'SKILL.md').exists())

    def test_restore_refuses_to_overwrite_post_install_user_changes(self):
        backup = setup.apply_plan(self.plan())
        path = self.home/'config.yaml'
        path.write_bytes(path.read_bytes()+b'# later user change\n')
        with self.assertRaisesRegex(setup.SetupError, '其他程序修改'):
            setup.restore_backup(self.repo, backup)
        self.assertIn(b'later user change', path.read_bytes())

    def test_legacy_team_hook_requires_explicit_migration_and_other_hooks_survive(self):
        path = self.home/'config.yaml'
        path.write_bytes(self.original+b'  - matcher: terminal\n    command: C:/old/dispatch/team-policy.py\n')
        before = path.read_bytes()
        with self.assertRaisesRegex(setup.SetupError, '其他 Team Policy'):
            self.plan()
        self.assertEqual(path.read_bytes(), before)
        plan = self.plan(replace_team=True)
        self.assertEqual(plan.replaced_hooks, 1)
        setup.apply_plan(plan)
        data = YAML(typ='safe').load(path.read_bytes())
        self.assertEqual(len(data['hooks']['pre_tool_call']), 2)

    def test_invalid_yaml_duplicate_keys_and_shared_hook_anchors_are_rejected(self):
        for raw in (b'hooks: [broken', b'model: a\nmodel: b\n',
                    b'hooks:\n  pre_tool_call: &shared []\n  post_tool_call: *shared\n'):
            with self.subTest(raw=raw), self.assertRaises(setup.SetupError):
                setup.merge_hooks(raw, 'python C:/example/team-policy.py')

    def test_mid_write_failure_rolls_back_only_written_files(self):
        plan = self.plan()
        original_write = setup.atomic_write
        target_paths = {change.path for change in plan.changes}
        calls = 0
        def broken(path, raw):
            nonlocal calls
            if path in target_paths:
                calls += 1
                if calls == 2:
                    raise OSError('fixture write failure')
            return original_write(path, raw)
        with mock.patch.object(setup, 'atomic_write', side_effect=broken):
            with self.assertRaises(OSError):
                setup.apply_plan(plan)
        self.assertFalse((self.repo/'dispatch'/'agents.json').exists())
        self.assertEqual((self.home/'config.yaml').read_bytes(), self.original)

    def test_restore_recovers_after_install_process_exits_mid_write(self):
        expected = self.plan()
        code = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import windows_setup as setup
repo, project, home = map(Path, sys.argv[2:])
exe = Path(sys.executable)
plan = setup.plan_setup(repo, project, 'project-alpha', {'codex': exe}, home, exe)
write = setup.atomic_write
first = plan.changes[0].path
def write_and_exit(path, raw, **kwargs):
    write(path, raw, **kwargs)
    if path == first:
        os._exit(73)
setup.atomic_write = write_and_exit
setup.perform_install(plan, 'project-alpha', exe)
"""
        result = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT/'scripts'),
                                 str(self.repo), str(self.project), str(self.home)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 73, result.stderr)
        flag = self.repo/'dispatch'/'.v71-maintenance'
        self.assertTrue(flag.exists())
        first, *rest = expected.changes
        self.assertEqual(first.path.read_bytes(), first.after)
        for change in rest:
            self.assertEqual(setup.read_optional(change.path), change.before)
        backup, = (self.repo/'local-config'/'backups').iterdir()
        with mock.patch.object(setup, 'REPO', self.repo), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(['--restore', str(backup)]), 0)
            self.assertEqual(setup.main(['--restore', str(backup)]), 0)
        self.assertFalse(flag.exists())
        for change in expected.changes:
            self.assertEqual(setup.read_optional(change.path), change.before)
        self.assertEqual((self.home/'auth.json').read_text(), 'fixture-auth-must-not-change')

    def test_changed_file_after_preview_aborts_before_any_write(self):
        plan = self.plan()
        (self.home/'config.yaml').write_bytes(self.original+b'# concurrent writer\n')
        with self.assertRaisesRegex(setup.SetupError, '发生变化'):
            setup.apply_plan(plan)
        self.assertFalse((self.repo/'dispatch'/'agents.json').exists())

    def test_active_task_or_retained_lock_stops_installation(self):
        path = self.repo/'dispatch'/'state.sqlite3'
        with database(path) as conn:
            conn.execute('CREATE TABLE tasks(status TEXT)')
            conn.execute("INSERT INTO tasks VALUES ('RUNNING')")
        with self.assertRaisesRegex(setup.SetupError, '运行或排队'):
            self.plan()
        with database(path) as conn:
            conn.execute('DELETE FROM tasks')
            conn.execute('CREATE TABLE project_locks(task_id TEXT)')
            conn.execute("INSERT INTO project_locks VALUES ('fixture')")
        with self.assertRaisesRegex(setup.SetupError, '保留锁'):
            self.plan()

    def test_hardlinked_config_is_rejected(self):
        os.link(self.home/'config.yaml', self.base/'other-config.yaml')
        with self.assertRaisesRegex(setup.SetupError, '硬链接'):
            self.plan()

    def test_existing_project_identity_cannot_silently_move_to_another_root(self):
        setup.apply_plan(self.plan())
        other = self.base/'other-project'
        other.mkdir()
        with self.assertRaisesRegex(setup.SetupError, '已绑定另一个目录'):
            setup.plan_setup(self.repo, other, 'project-alpha', {'codex': Path(sys.executable)}, self.home, Path(sys.executable))

    def test_add_project_preserves_prior_projects_and_custom_routing(self):
        setup.apply_plan(self.plan())
        routing = self.repo/'dispatch'/'routing.json'
        data = json.loads(routing.read_text(encoding='utf-8'))
        data['fixture_custom_setting'] = 'keep'
        routing.write_text(json.dumps(data), encoding='utf-8')
        other = self.base/'second-project'
        other.mkdir()
        plan = setup.plan_setup(self.repo, other, 'project-beta', {'codex': Path(sys.executable)}, self.home, Path(sys.executable))
        setup.apply_plan(plan)
        registry = json.loads((self.repo/'local-config'/'registry.json').read_text(encoding='utf-8'))
        self.assertEqual({x['id'] for x in registry['projects']}, {'project-alpha', 'project-beta'})
        self.assertEqual(json.loads(routing.read_text(encoding='utf-8'))['fixture_custom_setting'], 'keep')

    def test_skill_is_rendered_for_spaces_unicode_and_selected_project_only(self):
        setup.apply_plan(self.plan())
        text = (self.home/'skills'/'team-project-manager'/'SKILL.md').read_text(encoding='utf-8')
        self.assertIn(self.repo.as_posix(), text)
        self.assertIn(self.project.as_posix(), text)
        self.assertNotIn('C:\\path\\to', text)
        self.assertNotIn('进入示例项目 B', text)

    def test_cli_detection_checks_only_versions_and_ignores_script_shims(self):
        cli = self.base/'codex.exe'
        cli.write_bytes(b'fixture')
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, 'fixture-cli 1.0\n', '')
        env = {'LOCALAPPDATA': str(self.base/'missing-local'), 'APPDATA': str(self.base/'missing-roaming'), 'USERPROFILE': str(self.base/'missing-user')}
        found = setup.detect_tools(self.repo, env=env, which=lambda name: str(cli) if name == 'codex.exe' else None, run=run)
        self.assertEqual(set(found), {'codex'})
        self.assertEqual(calls, [[cli, '--version']])
        self.assertIsNone(setup.probe_cli(self.base/'codex.cmd', run=run))

    def test_cli_timeout_is_not_treated_as_a_working_installation(self):
        cli = self.base/'codex.exe'
        cli.write_bytes(b'fixture')
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired('fixture', 8)
        self.assertIsNone(setup.probe_cli(cli, run=run))

    def test_hermes_probe_uses_help_instead_of_the_network_update_check(self):
        cli = self.base/'hermes.exe'
        cli.write_bytes(b'fixture')
        with mock.patch.object(setup, 'native_run'):
            def run(argv, **kwargs):
                self.assertEqual(argv, [cli, '--help'])
                return subprocess.CompletedProcess(argv, 0, 'usage: hermes\n', '')
            result = setup.probe_cli(cli, run=run)
        self.assertEqual(result['probe'], 'help')

    def test_native_probe_timeout_stops_its_owned_process(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            setup.native_run([sys.executable, '-c', 'import time; time.sleep(20)'], timeout=.1)

    def test_verification_runs_real_dispatcher_hook_without_a_model_or_trust_write(self):
        setup.apply_plan(self.plan())
        def run(argv, **kwargs):
            if len(argv) >= 3 and argv[1:3] in (['config', 'check'], ['hooks', 'doctor']):
                return subprocess.CompletedProcess(argv, 1 if argv[1] == 'hooks' else 0, '', '')
            return setup.native_run(argv, **kwargs)
        result = setup.verify_installation(self.repo, self.home, 'project-alpha', Path(sys.executable), run=run)
        self.assertTrue(result['hook_session_injection'])
        self.assertTrue(result['control_plane_read_blocked'])
        self.assertEqual(result['hooks_doctor']['status'], 'needs_attention')
        with database(self.repo/'dispatch'/'state.sqlite3') as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM session_projects').fetchone()[0], 0)
        self.assertFalse((self.home/'shell-hooks-allowlist.json').exists())

    def test_native_hooks_doctor_zero_exit_with_untrusted_hook_is_not_a_pass(self):
        result = subprocess.CompletedProcess(['hermes', 'hooks', 'doctor'], 0,
                     'Checking 1 configured shell hook(s)...\nnot allowlisted — hook will NOT fire at runtime\n1 issue(s) found.\n', '')
        status = setup.diagnostic_status('hooks_doctor', result)
        self.assertEqual(status['exit_code'], 0)
        self.assertEqual(status['status'], 'needs_attention')
        self.assertEqual(status['reason'], 'hook_trust_pending')
        for output in ('', 'No shell hooks configured — nothing to check.'):
            result.stdout = output
            self.assertEqual(setup.diagnostic_status('hooks_doctor', result)['status'], 'needs_attention')

    def test_native_hooks_doctor_requires_explicit_healthy_report(self):
        result = subprocess.CompletedProcess(['hermes', 'hooks', 'doctor'], 0,
                                            '\x1b[32mAll shell hooks look healthy.\x1b[0m\n', '')
        self.assertEqual(setup.diagnostic_status('hooks_doctor', result)['status'], 'passed')
        result.returncode = 1
        self.assertEqual(setup.diagnostic_status('hooks_doctor', result)['status'], 'needs_attention')

    def test_selected_hermes_home_must_have_a_working_runtime_and_matching_config(self):
        for code, stdout in ((1, ''), (0, str(self.base/'another-home/config.yaml'))):
            def run(argv, **kwargs):
                self.assertEqual(argv[1:], ['config', 'path'])
                self.assertEqual(kwargs['env']['HERMES_HOME'], str(self.home))
                return subprocess.CompletedProcess(argv, code, stdout, '')
            with self.subTest(code=code), self.assertRaises(setup.SetupError):
                setup.validate_hermes_home(Path(sys.executable), self.home, run=run)
        self.assertEqual((self.home/'config.yaml').read_bytes(), self.original)

    def test_selected_initialized_hermes_home_is_accepted_without_model_calls(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, str(self.home/'config.yaml')+'\n', '')
        setup.validate_hermes_home(Path(sys.executable), self.home, run=run)

    def test_interactive_cancel_changes_no_configuration(self):
        detected = {name: {'path': sys.executable, 'version': 'fixture'} for name in setup.TOOLS}
        answers = ['', '', 'n', 'n', '', str(self.project), '', 'n']
        with mock.patch.object(setup, 'REPO', self.repo), mock.patch.object(setup, 'detect_tools', return_value=detected), \
             mock.patch.object(setup, 'validate_hermes_home'), \
             mock.patch.object(setup, 'hermes_home_for', return_value=self.home), mock.patch('builtins.input', side_effect=answers), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(setup.main([]), 0)
        self.assertFalse((self.repo/'dispatch'/'agents.json').exists())
        self.assertEqual((self.home/'config.yaml').read_bytes(), self.original)
        self.assertNotIn('fixture-only-no-real-key', output.getvalue())

    def test_failed_post_install_check_restores_original_configuration(self):
        detected = {name: {'path': sys.executable, 'version': 'fixture'} for name in setup.TOOLS}
        answers = ['', '', 'n', 'n', '', str(self.project), '', 'y']
        with mock.patch.object(setup, 'REPO', self.repo), mock.patch.object(setup, 'detect_tools', return_value=detected), \
             mock.patch.object(setup, 'validate_hermes_home'), \
             mock.patch.object(setup, 'hermes_home_for', return_value=self.home), mock.patch('builtins.input', side_effect=answers), \
             mock.patch.object(setup, 'verify_installation', side_effect=setup.SetupError('fixture check failure')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(setup.main([]), 1)
        self.assertEqual((self.home/'config.yaml').read_bytes(), self.original)
        self.assertFalse((self.repo/'dispatch'/'agents.json').exists())
        self.assertFalse((self.repo/'dispatch'/'.v71-maintenance').exists())

    def test_interactive_success_initializes_database_and_preserves_credentials(self):
        detected = {name: {'path': sys.executable, 'version': 'fixture'} for name in setup.TOOLS}
        answers = ['', '', 'n', 'n', '', str(self.project), '', 'y', 'n']
        original_verify = setup.verify_installation
        def run(argv, **kwargs):
            if argv[1:3] in (['config', 'check'], ['hooks', 'doctor']):
                return subprocess.CompletedProcess(argv, 0, '', '')
            return setup.native_run(argv, **kwargs)
        def verify(*args):
            self.assertTrue((self.repo/'dispatch'/'.v71-maintenance').is_file())
            return original_verify(*args, run=run)
        with mock.patch.object(setup, 'REPO', self.repo), mock.patch.object(setup, 'detect_tools', return_value=detected), \
             mock.patch.object(setup, 'validate_hermes_home'), \
             mock.patch.object(setup, 'hermes_home_for', return_value=self.home), mock.patch('builtins.input', side_effect=answers), \
             mock.patch.object(setup, 'verify_installation', side_effect=verify), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(setup.main([]), 0)
        self.assertTrue((self.repo/'dispatch'/'state.sqlite3').is_file())
        self.assertFalse((self.repo/'dispatch'/'.v71-maintenance').exists())
        self.assertEqual((self.home/'auth.json').read_text(), 'fixture-auth-must-not-change')
        self.assertNotIn('fixture-only-no-real-key', output.getvalue())

    def test_existing_maintenance_marker_is_never_deleted(self):
        flag = self.repo/'dispatch'/'.v71-maintenance'
        flag.write_text('another installer', encoding='utf-8')
        with self.assertRaisesRegex(setup.SetupError, '已有维护'):
            setup.perform_install(self.plan(), 'project-alpha', Path(sys.executable))
        self.assertEqual(flag.read_text(), 'another installer')
        self.assertFalse((self.repo/'dispatch'/'agents.json').exists())

    def test_interrupt_during_verification_rolls_back_and_releases_owned_gate(self):
        def interrupted(*args):
            raise KeyboardInterrupt()
        with self.assertRaisesRegex(setup.SetupError, '已恢复'):
            setup.perform_install(self.plan(), 'project-alpha', Path(sys.executable), verify=interrupted)
        self.assertEqual((self.home/'config.yaml').read_bytes(), self.original)
        self.assertFalse((self.repo/'dispatch'/'agents.json').exists())
        self.assertFalse((self.repo/'dispatch'/'.v71-maintenance').exists())

    def test_bootstrap_check_only_is_read_only_and_finds_supported_python(self):
        before = (self.home/'config.yaml').read_bytes()
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                 str(ROOT/'scripts'/'install-windows.ps1'), '-CheckOnly'],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIsNotNone(report['python'])
        self.assertGreaterEqual(report['python']['version'][1], 13)
        self.assertEqual((self.home/'config.yaml').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
