"""Paths owned by scripts/run_tests.py, never by the user's live runtime."""
import os
from pathlib import Path

FIXTURE_PROJECT = Path(os.environ['HERMES_TEAM_TEST_PROJECT'])
FIXTURE_SANDBOX = Path(os.environ['HERMES_TEAM_TEST_SANDBOX'])
