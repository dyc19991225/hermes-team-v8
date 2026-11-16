#!/usr/bin/env python3
"""Stage I: Offline unit tests for HERMES_HOME/agent-hooks/team-policy.py."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from _support import FIXTURE_PROJECT, FIXTURE_SANDBOX

DISPATCH_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = DISPATCH_ROOT / 'team-policy.py'
sys.path.insert(0, str(HOOK_PATH.parent))
sys.path.insert(0, str(DISPATCH_ROOT))

import dispatcher
import importlib.util

spec = importlib.util.spec_from_file_location("team_policy", str(HOOK_PATH))
team_policy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(team_policy)


def make_payload(tool_name: str, tool_input: dict, cwd: str = str(DISPATCH_ROOT)) -> dict:
    return {
        "hook_event_name": "pre_tool_call",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": cwd,
        "session_id": "unit-test",
        "extra": {},
    }


class TeamPolicyOfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "inbox").mkdir()
        (self.root / "prompts").mkdir()
        (self.root / "results").mkdir()
        shutil.copy2(DISPATCH_ROOT / "agents.json", self.root / "agents.json")
        shutil.copy2(DISPATCH_ROOT / "routing.json", self.root / "routing.json")
        shutil.copy2(DISPATCH_ROOT / "dispatcher.py", self.root / "dispatcher.py")
        shutil.copy2(DISPATCH_ROOT / "policy.py", self.root / "policy.py")
        routing=json.loads((self.root/'routing.json').read_text());routing.pop('project_registry',None)
        (self.root/'routing.json').write_text(json.dumps(routing))
        self.db_path = self.root / "state.sqlite3"
        dispatcher.init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _insert_task(
        self,
        *,
        task_id: str | None = None,
        agent: str = "codex",
        project_root: str = str(FIXTURE_PROJECT),
        task_type: str = "code_review",
        risk_level: str = "low",
        execution_mode: str = "read_only",
        status: str = "CREATED",
        approval_state: str = "not_required",
    ) -> str:
        tid = task_id or str(uuid.uuid4())
        conn = dispatcher.connect_db(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, idempotency_key, created_at, updated_at, project_root,
                    agent, task_type, risk_level, execution_mode, status,
                    prompt_path, result_path, exit_code, duration, retry_count,
                    error, approval_state
                ) VALUES (?, ?, '2026-11-10T00:00:00Z', '2026-11-10T00:00:00Z', ?, ?, ?, ?, ?, ?, 'p.txt', NULL, NULL, NULL, 0, NULL, ?)
                """,
                (
                    tid,
                    f"idem-{tid}",
                    project_root,
                    agent,
                    task_type,
                    risk_level,
                    execution_mode,
                    status,
                    approval_state,
                ),
            )
            conn.execute("COMMIT;")
        finally:
            conn.close()
        return tid

    def test_01_read_tools_allowed(self) -> None:
        for tool in ("read_file", "search_files", "skills_list", "skill_view", "session_search"):
            res = team_policy.evaluate_hook(make_payload(tool, {"path": str(FIXTURE_PROJECT/'package.json')}), self.root)
            self.assertEqual(res, {}, f"{tool} should be allowed")

    def test_02_direct_business_write_blocked_and_valid_inbox_allowed(self) -> None:
        # Business source write blocked
        res1 = team_policy.evaluate_hook(
            make_payload("write_file", {"path": str(FIXTURE_PROJECT/'server'/'app.mjs'), "content": "x"}),
            self.root,
        )
        self.assertEqual(res1.get("action"), "block")

        # Traversal out of inbox blocked
        res2 = team_policy.evaluate_hook(
            make_payload("write_file", {"path": str(self.root / "inbox" / ".." / "tests" / "evil.json"), "content": "{}"}),
            self.root,
        )
        self.assertEqual(res2.get("action"), "block")

        # Invalid JSON inside inbox blocked
        res3 = team_policy.evaluate_hook(
            make_payload("write_file", {"path": str(self.root / "inbox" / "bad.json"), "content": "not-json"}),
            self.root,
        )
        self.assertEqual(res3.get("action"), "block")

        # Valid relative path with mixed case & forward slashes inside inbox allowed
        valid_spec = {
            "idempotency_key": "ok-01",
            "agent": "codex",
            "project_root": str(FIXTURE_PROJECT),
            "task_type": "code_review",
            "risk_level": "low",
            "execution_mode": "read_only",
            "approval_state": "not_required",
            "prompt": "只读审阅",
        }
        res4 = team_policy.evaluate_hook(
            make_payload("write_file", {"path": "INBOX/valid-task.json", "content": json.dumps(valid_spec)}, cwd=str(self.root)),
            self.root,
        )
        self.assertEqual(res4, {})

    def test_03_patch_always_blocked(self) -> None:
        res = team_policy.evaluate_hook(
            make_payload("patch", {"path": str(FIXTURE_PROJECT/'AGENTS.md'), "old_string": "a", "new_string": "b"}),
            self.root,
        )
        self.assertEqual(res.get("action"), "block")

    def test_04_dangerous_terminal_commands_blocked_and_readonly_allowed(self) -> None:
        for bad_cmd in (
            "git status && echo pwned",
            "echo x | sh",
            "hermes config set approvals.mode off",
            "python -c \"open('x','w').write('y')\"",
            "codex exec --dangerously-bypass-approvals-and-sandbox hi",
            "git reset --hard HEAD",
            "rmdir /s /q foo",
        ):
            res = team_policy.evaluate_hook(make_payload("terminal", {"command": bad_cmd}), self.root)
            self.assertEqual(res.get("action"), "block", f"Expected block for {bad_cmd}")

        for ok_cmd in (
            "pwd",
            f'git -C "{FIXTURE_PROJECT}" status --short',
            "hermes config check",
            "hermes hooks list",
            "codex --version",
        ):
            res = team_policy.evaluate_hook(make_payload("terminal", {"command": ok_cmd}), self.root)
            self.assertEqual(res, {}, f"Expected allow for {ok_cmd}: {res}")

    def test_05_unknown_agent_blocked(self) -> None:
        tid = self._insert_task(agent="unknown-agent")
        cmd = f'python "{self.root / "dispatcher.py"}" dry-run --task-id {tid}'
        res = team_policy.evaluate_hook(make_payload("terminal", {"command": cmd}), self.root)
        self.assertEqual(res.get("action"), "block")

    def test_06_unknown_workdir_blocked(self) -> None:
        tid = self._insert_task(project_root=r"C:\Windows\Temp")
        cmd = f'python "{self.root / "dispatcher.py"}" dry-run --task-id {tid}'
        res = team_policy.evaluate_hook(make_payload("terminal", {"command": cmd}), self.root)
        self.assertEqual(res.get("action"), "block")

    def test_07_invalid_uuid_blocked(self) -> None:
        cmd = f'python "{self.root / "dispatcher.py"}" dry-run --task-id not-a-uuid'
        res = team_policy.evaluate_hook(make_payload("terminal", {"command": cmd}), self.root)
        self.assertEqual(res.get("action"), "block")

    def test_08_duplicate_or_locked_task_blocked(self) -> None:
        tid1 = self._insert_task(task_type="code_write", risk_level="medium", execution_mode="workspace_write", status="RUNNING")
        tid2 = self._insert_task(task_type="code_write", risk_level="medium", execution_mode="workspace_write", status="CREATED")
        conn = dispatcher.connect_db(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                "INSERT INTO project_locks(project_root, task_id, lock_mode, acquired_at) VALUES (?, ?, 'write', '2026-11-10T00:00:00Z')",
                (str(FIXTURE_PROJECT), tid1),
            )
            conn.execute("COMMIT;")
        finally:
            conn.close()
        # Running tid1 again -> blocked (status RUNNING)
        res1 = team_policy.evaluate_hook(
            make_payload("terminal", {"command": f'python "{self.root / "dispatcher.py"}" run --task-id {tid1}'}),
            self.root,
        )
        self.assertEqual(res1.get("action"), "block")
        # Running tid2 while tid1 holds lock -> blocked
        res2 = team_policy.evaluate_hook(
            make_payload("terminal", {"command": f'python "{self.root / "dispatcher.py"}" run --task-id {tid2}'}),
            self.root,
        )
        self.assertEqual(res2.get("action"), "block")

    def test_09_unapproved_high_risk_blocked(self) -> None:
        tid = self._insert_task(
            task_type="code_write",
            risk_level="high",
            execution_mode="workspace_write",
            status="CREATED",
            approval_state="pending",
        )
        res = team_policy.evaluate_hook(
            make_payload("terminal", {"command": f'python "{self.root / "dispatcher.py"}" run --task-id {tid}'}),
            self.root,
        )
        self.assertEqual(res.get("action"), "block")

        # Valid approved low-risk task allowed on dry-run
        ok_tid = self._insert_task(
            task_type="smoke_test",
            risk_level="low",
            execution_mode="read_only",
            status="CREATED",
            approval_state="not_required",
        )
        res_ok = team_policy.evaluate_hook(
            make_payload("terminal", {"command": f'python "{self.root / "dispatcher.py"}" dry-run --task-id {ok_tid}'}),
            self.root,
        )
        self.assertEqual(res_ok, {})

    def test_10_malformed_json_stdin_fail_closed(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="{broken json!!!",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        parsed = json.loads(proc.stdout.strip())
        self.assertEqual(parsed.get("action"), "block")

    def test_11_delegate_and_other_write_tools_blocked(self) -> None:
        self.assertEqual(
            team_policy.evaluate_hook(make_payload("delegate_task", {"tasks": [{"goal": "bypass"}]}), self.root).get("action"),
            "block",
        )
        self.assertEqual(
            team_policy.evaluate_hook(make_payload("delegate_task", {"action": "list"}), self.root),
            {},
        )
        for other in ("execute_code", "skill_manage", "memory", "cronjob_manage", "unknown_future_tool"):
            self.assertEqual(
                team_policy.evaluate_hook(make_payload(other, {}), self.root).get("action"),
                "block",
            )


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TeamPolicyOfflineTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
