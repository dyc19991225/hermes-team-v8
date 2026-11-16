"""Generate local configuration from public templates; never import credentials."""
import argparse
import json
from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[1]


def configuration(registry_path, projects, executables):
    """Also used by the offline test runner with temporary Python executables."""
    agents = json.loads((REPO/'config'/'agents.example.json').read_text(encoding='utf-8'))
    routing = json.loads((REPO/'config'/'routing.example.json').read_text(encoding='utf-8'))
    for agent, entry in agents['agents'].items():
        entry['enabled'] = agent in executables
        if entry['enabled']:
            entry['executable'] = str(executables[agent])
    enabled = list(executables)
    routing['project_registry'] = str(registry_path)
    routing['project_whitelist'] = [str(root) for _, root in projects]
    registry = {'schema_version': 1, 'execution_policy': 'serial_all_workers',
                'projects': [{'id': pid, 'name': pid, 'root': str(root), 'status': 'active',
                              'agents': enabled, 'default_agent': 'codex',
                              'max_concurrent_writes': 1} for pid, root in projects]}
    return agents, routing, registry


def existing_absolute(raw, *, directory=False):
    path = Path(raw).expanduser()
    if not path.is_absolute() or not (path.is_dir() if directory else path.is_file()):
        raise ValueError('An existing absolute '+('directory' if directory else 'file')+' is required: '+raw)
    return path.resolve()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-id', default='project-alpha')
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--codex', required=True, help='Absolute installed Codex executable, also needed by the Claude sandbox adapter')
    parser.add_argument('--claude', help='Optional absolute Claude Code executable')
    parser.add_argument('--agy', help='Optional absolute AGY executable')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', args.project_id):
        parser.error('Invalid project ID')
    try:
        project = existing_absolute(args.project_root, directory=True)
        if project == REPO or project in REPO.parents or REPO in project.parents:
            raise ValueError('The business project and this control repository must have separate roots')
        executables = {name: existing_absolute(value) for name in ('codex', 'claude', 'agy')
                       if (value := getattr(args, name))}
        dispatch = REPO/'dispatch'
        registry_path = REPO/'local-config'/'registry.json'
        agents, routing, registry = configuration(registry_path, [(args.project_id, project)], executables)
        targets = [(dispatch/'agents.json', agents), (dispatch/'routing.json', routing), (registry_path, registry)]
        if any(path.exists() for path, _ in targets):
            raise ValueError('Local configuration already exists; review it manually instead of overwriting')
        created = []
        try:
            for path, data in targets:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('x', encoding='utf-8', newline='\n') as output:
                    created.append(path)
                    json.dump(data, output, ensure_ascii=False, indent=2)
                    output.write('\n')
        except BaseException:
            for path in reversed(created):
                path.unlink()
            raise
        (dispatch/'inbox').mkdir(exist_ok=True)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print('Local configuration created. No accounts, Hook approvals or credentials were changed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
