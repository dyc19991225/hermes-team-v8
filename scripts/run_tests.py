"""Run the public Windows regression suite without CLI installs or credentials."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from configure import REPO, configuration


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test_*.py', help='unittest discovery pattern')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('The full runtime suite is verified on Windows; cross-platform support is not claimed')
    with tempfile.TemporaryDirectory(prefix='hermes-v81-tests-') as name:
        root = Path(name)
        dispatch = root/'dispatch'
        dispatch.mkdir()
        for name in ('dispatcher.py', 'policy.py', 'project_manager.py', 'codex_worker.py',
                     'process_runtime.py', 'workflow.py', 'team-policy.py'):
            shutil.copy2(REPO/'dispatch'/name, dispatch/name)
        (dispatch/'tests').mkdir()
        for source in (REPO/'dispatch'/'tests').glob('*.py'):
            shutil.copy2(source, dispatch/'tests'/source.name)
        (root/'scripts').mkdir()
        shutil.copy2(REPO/'scripts'/'configure.py', root/'scripts'/'configure.py')
        (root/'config').mkdir()
        for name in ('agents.example.json', 'routing.example.json'):
            shutil.copy2(REPO/'config'/name, root/'config'/name)
        projects = [root/'projects'/name for name in ('project-alpha', 'smoke-sandbox')]
        for project in projects:
            project.mkdir(parents=True)
        agents, routing, registry = configuration(root/'registry.json',
                                                  list(zip(('project-alpha', 'smoke-sandbox'), projects)),
                                                  {agent: Path(sys.executable) for agent in ('codex', 'claude', 'agy')})
        for path, data in ((dispatch/'agents.json', agents), (dispatch/'routing.json', routing), (root/'registry.json', registry)):
            path.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
        home = root/'user-home'
        codex_home = home/'.codex'
        codex_home.mkdir(parents=True)
        (codex_home/'config.toml').write_text('[mcp_servers.offline_fixture]\ncommand="not-started"\n', encoding='utf-8')
        env = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
                   USERPROFILE=str(home), HOME=str(home), CODEX_HOME=str(codex_home),
                   LOCALAPPDATA=str(home/'local'), APPDATA=str(home/'roaming'), HERMES_HOME=str(home/'hermes'),
                   HERMES_TEAM_TEST_PROJECT=str(projects[0]), HERMES_TEAM_TEST_SANDBOX=str(projects[1]))
        # Do not pass provider credentials into the disposable test process.
        for key in list(env):
            upper = key.upper()
            if upper.endswith(('_API_KEY', '_API_TOKEN', '_ACCESS_TOKEN', '_AUTH_TOKEN')) or upper in ('GH_TOKEN', 'GITHUB_TOKEN'):
                del env[key]
        return subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'unittest', 'discover',
                               '-s', 'tests', '-p', args.pattern, '-v'], cwd=dispatch, env=env).returncode


if __name__ == '__main__':
    raise SystemExit(main())
