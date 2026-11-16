#!/usr/bin/env python3
"""Stage F: Comprehensive 10-point Dry Run verification suite (no external CLI calls)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from _support import FIXTURE_PROJECT, FIXTURE_SANDBOX
from unittest import mock

DISPATCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DISPATCH_ROOT))

import dispatcher
import policy


class DryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.db_path = self.tmp_dir / "test.sqlite3"
        self.prompts_dir = self.tmp_dir / "prompts"
        self.results_dir = self.tmp_dir / "results"
        cfg=dispatcher.load_routing();cfg.pop('project_registry',None)
        patch=mock.patch.object(dispatcher,'load_routing',return_value=cfg);patch.start();self.addCleanup(patch.stop)
        dispatcher.init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_01_create_and_02_uuid_and_10_result_path(self) -> None:
        task = dispatcher.create_task(
            {
                "idempotency_key": "dry-01",
                "agent": "codex",
                "project_root": str(FIXTURE_PROJECT),
                "task_type": "code_review",
                "risk_level": "low",
                "execution_mode": "read_only",
                "approval_state": "not_required",
                "prompt": "只读测试任务",
            },
            db_path=self.db_path,
            prompts_dir=self.prompts_dir,
        )
        self.assertTrue(policy.is_valid_uuid(task["task_id"]))
        self.assertEqual(task["status"], "CREATED")

        dry = dispatcher.dry_run_task(
            task["task_id"],
            db_path=self.db_path,
            results_dir=self.results_dir,
        )
        self.assertTrue(dry["ok"])
        self.assertEqual(dry["status"], "DRY_RUN")
        self.assertTrue(dry["result_path"].endswith(f"{task['task_id']}.json"))

    def test_03_agent_whitelist(self) -> None:
        with self.assertRaises(policy.PolicyError):
            dispatcher.create_task(
                {
                    "idempotency_key": "bad-agent",
                    "agent": "evil-agent",
                    "project_root": str(FIXTURE_PROJECT),
                    "task_type": "code_review",
                    "risk_level": "low",
                    "execution_mode": "read_only",
                    "prompt": "x",
                },
                db_path=self.db_path,
                prompts_dir=self.prompts_dir,
            )

    def test_04_project_dir_whitelist(self) -> None:
        with self.assertRaises(policy.PolicyError):
            dispatcher.create_task(
                {
                    "idempotency_key": "bad-dir",
                    "agent": "codex",
                    "project_root": r"C:\Windows\System32",
                    "task_type": "code_review",
                    "risk_level": "low",
                    "execution_mode": "read_only",
                    "prompt": "x",
                },
                db_path=self.db_path,
                prompts_dir=self.prompts_dir,
            )

    def test_05_risk_level_and_approval_gate(self) -> None:
        with self.assertRaises(policy.PolicyError):
            dispatcher.create_task(
                {
                    "idempotency_key": "bad-risk",
                    "agent": "codex",
                    "project_root": str(FIXTURE_PROJECT),
                    "task_type": "code_write",
                    "risk_level": "nuclear",
                    "execution_mode": "workspace_write",
                    "prompt": "x",
                },
                db_path=self.db_path,
                prompts_dir=self.prompts_dir,
            )
        high_task = dispatcher.create_task(
            {
                "idempotency_key": "high-unapproved",
                "agent": "codex",
                "project_root": str(FIXTURE_PROJECT),
                "task_type": "code_write",
                "risk_level": "high",
                "execution_mode": "workspace_write",
                "approval_state": "pending",
                "prompt": "高风险待审批",
            },
            db_path=self.db_path,
            prompts_dir=self.prompts_dir,
        )
        self.assertEqual(high_task["status"], "AWAITING_APPROVAL")
        with self.assertRaises(policy.PolicyError):
            dispatcher.dry_run_task(high_task["task_id"], db_path=self.db_path, results_dir=self.results_dir)

    def test_06_duplicate_idempotency(self) -> None:
        spec = {
            "idempotency_key": "dup-01",
            "agent": "agy",
            "project_root": str(FIXTURE_SANDBOX),
            "task_type": "smoke_test",
            "risk_level": "low",
            "execution_mode": "plan",
            "prompt": "smoke",
        }
        dispatcher.create_task(spec, db_path=self.db_path, prompts_dir=self.prompts_dir)
        with self.assertRaises(policy.PolicyError):
            dispatcher.create_task(spec, db_path=self.db_path, prompts_dir=self.prompts_dir)

    def test_07_project_write_lock_mechanism(self) -> None:
        t1 = dispatcher.create_task(
            {
                "idempotency_key": "lock-1",
                "agent": "codex",
                "project_root": str(FIXTURE_PROJECT),
                "task_type": "code_write",
                "risk_level": "medium",
                "execution_mode": "workspace_write",
                "prompt": "w1",
            },
            db_path=self.db_path,
            prompts_dir=self.prompts_dir,
        )
        t2 = dispatcher.create_task(
            {
                "idempotency_key": "lock-2",
                "agent": "codex",
                "project_root": str(FIXTURE_PROJECT),
                "task_type": "code_fix",
                "risk_level": "medium",
                "execution_mode": "workspace_write",
                "prompt": "w2",
            },
            db_path=self.db_path,
            prompts_dir=self.prompts_dir,
        )
        conn = dispatcher.connect_db(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                "INSERT INTO project_locks(project_root, task_id, lock_mode, acquired_at) VALUES (?, ?, 'write', '2026-11-10T00:00:00Z')",
                (t1["project_root"], t1["task_id"]),
            )
            conn.execute("COMMIT;")
        finally:
            conn.close()
        with self.assertRaises(policy.PolicyError):
            dispatcher.dry_run_task(t2["task_id"], db_path=self.db_path, results_dir=self.results_dir)

    def test_08_cli_argv_generation_all_three_agents(self) -> None:
        cases = [
            ("codex", "smoke_test", "read_only", "-s"),
            ("claude", "smoke_test", "plan", "--permission-mode"),
            ("agy", "smoke_test", "plan", "--sandbox"),
        ]
        for idx, (agent, ttype, mode, expected_flag) in enumerate(cases):
            t = dispatcher.create_task(
                {
                    "idempotency_key": f"argv-{idx}",
                    "agent": agent,
                    "project_root": str(FIXTURE_SANDBOX),
                    "task_type": ttype,
                    "risk_level": "low",
                    "execution_mode": mode,
                    "prompt": "只返回：HERMES_TEAM_V7_SMOKE_OK",
                },
                db_path=self.db_path,
                prompts_dir=self.prompts_dir,
            )
            dry = dispatcher.dry_run_task(t["task_id"], db_path=self.db_path, results_dir=self.results_dir)
            self.assertTrue(Path(dry["argv"][0]).is_absolute())
            self.assertIn(expected_flag, dry["argv"])
            joined = " ".join(dry["argv"])
            for bad in policy.FORBIDDEN_SUBSTRINGS:
                self.assertNotIn(bad, joined)

    def test_09_timeout_enforcement_and_sqlite_pragmas(self) -> None:
        conn = dispatcher.connect_db(self.db_path)
        try:
            jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            bt = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
            self.assertEqual(jm.lower(), "wal")
            self.assertEqual(fk, 1)
            self.assertGreaterEqual(bt, 5000)
        finally:
            conn.close()

        t = dispatcher.create_task(
            {
                "idempotency_key": "timeout-01",
                "agent": "codex",
                "project_root": str(FIXTURE_SANDBOX),
                "task_type": "smoke_test",
                "risk_level": "low",
                "execution_mode": "read_only",
                "timeout": 1,
                "prompt": "timeout test",
            },
            db_path=self.db_path,
            prompts_dir=self.prompts_dir,
        )
        invocation={'argv':[sys.executable,'-u','-c','import time; time.sleep(10)'],
            'cwd':str(self.tmp_dir),'stdin_text':None,'timeout':1,'json_mode':'jsonl'}
        with mock.patch('dispatcher.build_cli_invocation',return_value=invocation):
            report = dispatcher.run_task(
                t["task_id"], timeout_override=1, db_path=self.db_path, results_dir=self.results_dir
            )
        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(report["timed_out"])
        row = dispatcher.get_task(t["task_id"], db_path=self.db_path)
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("Timed out", row["error"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DryRunTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("DRY_RUN_PASSED")
        sys.exit(0)
    sys.exit(1)
