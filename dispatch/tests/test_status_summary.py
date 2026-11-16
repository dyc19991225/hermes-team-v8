"""Bounded status observations: no model calls or worker launches."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dispatcher as d


class StatusSummaryTests(unittest.TestCase):
    def task(self, status='RUNNING'):
        return {
            'task_id': '26fcd24b-d652-43d5-919c-e96591594df8',
            'project_id': 'project-beta', 'agent': 'codex', 'status': status,
            'run_token': 'private-ownership-material', 'prompt_path': 'private-prompt',
            'error': None, 'result_path': 'evidence.json',
            'poll_after_seconds': 30 if status == 'RUNNING' else None,
            'streams': {name: {'path': name+'.log', 'bytes': 200000, 'tail': 'x'*2048}
                        for name in ('stdout', 'stderr')},
        }

    def test_running_summary_omits_log_bodies_and_ownership_material(self):
        task = self.task()
        with mock.patch.object(d, 'get_task', return_value=task), \
             mock.patch.object(d, 'inspect_task', return_value=task):
            result = d.task_status_summary(task['task_id'], wait_seconds=0)
        self.assertNotIn('run_token', result)
        self.assertNotIn('prompt_path', result)
        self.assertNotIn('tail', result['streams']['stdout'])
        self.assertEqual(result['streams']['stdout']['bytes'], 200000)
        self.assertTrue(result['execution_status_only'])
        self.assertLess(len(json.dumps(result)), 1500)

    def test_failed_status_keeps_bounded_diagnostic_evidence(self):
        task = self.task('FAILED')
        task['error'] = 'e'*2000
        task['streams']['stderr']['tail'] += 'specific failure'
        with mock.patch.object(d, 'get_task', return_value=task), \
             mock.patch.object(d, 'inspect_task', return_value=task):
            result = d.task_status_summary(task['task_id'], wait_seconds=0)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(len(result['error']), 800)
        self.assertEqual(len(result['streams']['stderr']['tail']), 512)
        self.assertTrue(result['streams']['stderr']['tail'].endswith('specific failure'))
        self.assertIsNone(result['poll_after_seconds'])

    def test_local_wait_returns_early_on_state_change(self):
        running, done = self.task(), self.task('SUCCEEDED')
        with mock.patch.object(d, 'get_task', side_effect=[running, running, done]), \
             mock.patch.object(d, 'inspect_task', return_value=done), \
             mock.patch.object(d.time, 'monotonic', side_effect=[0, .1, .6, .7]), \
             mock.patch.object(d.time, 'sleep') as sleep:
            result = d.task_status_summary(running['task_id'], wait_seconds=2)
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result['waited_seconds'], .7)

    def test_local_wait_has_a_hard_30_second_budget(self):
        task = self.task()
        with mock.patch.object(d, 'get_task', return_value=task) as get, \
             mock.patch.object(d, 'inspect_task', return_value=task), \
             mock.patch.object(d.time, 'monotonic', side_effect=[0, .1, 30, 30]), \
             mock.patch.object(d.time, 'sleep') as sleep:
            result = d.task_status_summary(task['task_id'])
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(.5)
        self.assertEqual(result['waited_seconds'], 30)
        self.assertEqual(result['status'], 'RUNNING')

    def test_terminal_status_does_not_sleep(self):
        task = self.task('FAILED')
        with mock.patch.object(d, 'get_task', return_value=task), \
             mock.patch.object(d, 'inspect_task', return_value=task), \
             mock.patch.object(d.time, 'sleep') as sleep:
            d.task_status_summary(task['task_id'])
        sleep.assert_not_called()

    def test_invalid_wait_budgets_are_rejected_before_observation(self):
        for value in (-1, 31, True, '30', float('nan'), float('inf')):
            with self.subTest(value=value), mock.patch.object(d, 'get_task') as get:
                with self.assertRaises(d.PolicyError):
                    d.task_status_summary('unused', wait_seconds=value)
                get.assert_not_called()

    def test_cli_uses_the_bounded_summary_without_new_flags_or_authority(self):
        task = self.task('SUCCEEDED')
        output = io.StringIO()
        with mock.patch.object(d, 'task_status_summary', return_value={'status': 'SUCCEEDED'}) as summarize, \
             contextlib.redirect_stdout(output):
            code = d.main(['status', '--task-id', task['task_id']])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {'status': 'SUCCEEDED'})
        self.assertEqual(summarize.call_args.args, (task['task_id'],))


if __name__ == '__main__':
    unittest.main()
