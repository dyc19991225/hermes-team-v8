"""V8 registry, scoped context, session binding and worker sandbox policy."""
from pathlib import Path
from contextlib import closing
import os, re, json, sqlite3, hashlib, sys, subprocess, stat, tomllib

class ProjectError(ValueError): pass
PRODUCTION_DISPATCH=Path(__file__).resolve().parent

def canonical(value):
    if not isinstance(value,str) or not value.strip(): raise ProjectError('Missing path')
    p=Path(value)
    if not p.is_absolute() or str(p).startswith(('\\\\','//')): raise ProjectError('Local absolute path required')
    if '..' in p.parts or any(':' in part for part in p.parts[1:]): raise ProjectError('Traversal or alternate stream rejected')
    if any(part.endswith((' ','.')) for part in p.parts[1:]):raise ProjectError('Ambiguous Windows path rejected')
    for part in (p,*p.parents):
        if part.exists() or part.is_symlink():
            if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()): raise ProjectError('Linked path rejected: '+str(part))
            if getattr(part.stat(),'st_file_attributes',0)&0x400: raise ProjectError('Reparse point rejected: '+str(part))
    return Path(os.path.normcase(str(p.resolve())))

def inside(path,root):
    p=canonical(str(path));r=canonical(str(root))
    if p!=r and r not in p.parents: raise ProjectError('Path outside bound project')
    if p.is_file() and p.stat().st_nlink>1: raise ProjectError('Hardlinked file rejected')
    return p

def audit_tree(root):
    """Walk once; validate each entry without re-statting every ancestor per file."""
    root=canonical(str(root))
    for current,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            path=Path(current)/name;info=path.lstat()
            if name.endswith((' ','.')):raise ProjectError('Ambiguous Windows filename rejected')
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                raise ProjectError('Linked/reparse entry rejected: '+str(path))
            if stat.S_ISREG(info.st_mode) and info.st_nlink>1:
                raise ProjectError('Hardlinked file rejected: '+str(path))

def registry(cfg):
    path=cfg.get('project_registry')
    if not path: raise ProjectError('V8 registry unavailable')
    data=json.loads(canonical(path).read_text(encoding='utf-8'))
    if data.get('schema_version')!=1 or not isinstance(data.get('projects'),list): raise ProjectError('Invalid registry schema')
    entries={};roots=[]
    for entry in data['projects']:
        pid=entry.get('id','')
        if not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',pid) or pid in entries: raise ProjectError('Invalid/duplicate project ID')
        r=canonical(entry.get('root'))
        if any(r==other or r in other.parents or other in r.parents for other in roots): raise ProjectError('Overlapping project roots')
        roots.append(r);entries[pid]=dict(entry,root=str(r))
    return entries

def project(pid,cfg):
    item=registry(cfg).get(pid)
    if not item or item.get('status')!='active': raise ProjectError('Unknown or inactive project: '+str(pid))
    if not Path(item['root']).is_dir(): raise ProjectError('Project directory does not exist')
    return item

def validate(task,cfg):
    item=project(task.get('project_id'),cfg)
    if canonical(task.get('project_root'))!=canonical(item['root']): raise ProjectError('project_id/root mismatch')
    if task.get('agent') not in item.get('agents',[]): raise ProjectError('Agent not allowed for project')
    return item

def context(pid,cfg):
    item=project(pid,cfg);root=Path(item['root'])
    sections=[f"PROJECT_ID: {pid}\nPROJECT_ROOT: {root}\nOnly this project's files and scoped memory belong to this task."]
    profiles=Path(cfg['project_registry']).parent/'profiles'
    profile=inside(profiles/(pid+'.json'),profiles)
    if profile.is_file():
        data=json.loads(profile.read_text(encoding='utf-8'))
        if data.get('project_id')!=pid:raise ProjectError('Profile/project mismatch')
        sections.append('--- Project profile ---\n'+json.dumps(data,ensure_ascii=False))
    remaining=96*1024
    for name in ('AGENTS.md','CLAUDE.md','PROJECT.md','docs/decisions.md','docs/progress.md'):
        p=inside(root/name,root)
        if not p.is_file():continue
        size=min(remaining,24*1024)
        if not size:break
        with p.open('rb') as f:raw=f.read(size)
        remaining-=len(raw)
        sections.append(f'--- {pid}/{name} (scoped reference) ---\n'+raw.decode('utf-8',errors='replace'))
    memory=Path(cfg['project_registry']).parent/'memory'/pid
    for name in ('decisions.md','progress.md'):
        p=inside(memory/name,memory)
        if p.is_file() and remaining>0:
            with p.open('rb') as f:raw=f.read(min(remaining,16*1024))
            remaining-=len(raw);sections.append(f'--- scoped memory/{name} ---\n'+raw.decode('utf-8',errors='replace'))
    return '\n\n'.join(sections)

def migrate(conn,cfg):
    entries=registry(cfg);by_root={x['root']:pid for pid,x in entries.items()}
    for table in ('tasks','project_locks'):
        cols={r['name'] for r in conn.execute('PRAGMA table_info('+table+')')}
        if 'project_id' not in cols:conn.execute('ALTER TABLE '+table+' ADD COLUMN project_id TEXT')
        for row in conn.execute('SELECT DISTINCT project_root FROM '+table).fetchall():
            try:pid=by_root.get(str(canonical(row['project_root'])))
            except ProjectError:pid=None
            if pid:conn.execute('UPDATE '+table+' SET project_id=? WHERE project_id IS NULL AND project_root=?',(pid,row['project_root']))
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id)')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_locks_project_id ON project_locks(project_id) WHERE project_id IS NOT NULL')
    conn.execute('CREATE TABLE IF NOT EXISTS session_projects(session_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,bound_at TEXT NOT NULL)')

def bound_project(session_id,db):
    if not isinstance(session_id,str) or not session_id or len(session_id)>200:raise ProjectError('Missing/invalid session ID')
    with closing(sqlite3.connect(Path(db).as_uri()+'?mode=ro',uri=True)) as conn:
        row=conn.execute('SELECT project_id FROM session_projects WHERE session_id=?',(session_id,)).fetchone()
    if not row:raise ProjectError('Session has no project; use Dispatcher bind first')
    return row[0]

def bind(session_id,pid,db,cfg):
    project(pid,cfg)
    if not isinstance(session_id,str) or not session_id.strip() or len(session_id)>200:raise ProjectError('Invalid session ID')
    with closing(sqlite3.connect(db)) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT INTO session_projects VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(session_id) DO UPDATE SET project_id=excluded.project_id,bound_at=excluded.bound_at",(session_id,pid))
        c.commit()
    return {'session_id':session_id,'project_id':pid,'context':context(pid,cfg)}

def read_prompt(task,prompts):
    p=inside(task['prompt_path'],prompts)
    if p.name!=task['task_id']+'.txt':raise ProjectError('Prompt is not a task-owned snapshot')
    raw=p.read_bytes()
    if not task.get('prompt_sha256') or hashlib.sha256(raw).hexdigest()!=task['prompt_sha256']:raise ProjectError('Missing or changed task snapshot hash')
    return raw.decode('utf-8')

def sandbox_argv(argv,root,deny,write,codex,extra_write=(),codex_home=None):
    # The existing elevated Windows backend requires :root read. Explicit deny
    # rules isolate other registered projects and the dispatcher control plane.
    fs={Path(p).as_posix():'deny' for p in deny}
    fs[Path(root).as_posix()]='write' if write else 'read'
    fs[Path(argv[0]).resolve().parent.as_posix()]='read'
    user_dir=Path(os.environ.get('USERPROFILE',str(Path.home())))
    for name in ('.codex','.claude','.claude.json','.agy'):
        p=user_dir/name
        if p.exists():fs[p.as_posix()]='read'
    for p in extra_write:fs[Path(p).as_posix()]='write'
    name='hermes_v8'
    inline='{'+','.join(json.dumps(k)+'='+json.dumps(v) for k,v in fs.items())+'}'
    return [codex,'sandbox','-P',name,'-C',str(root),'-c',f'permissions.{name}.extends=":workspace"',
            '-c',f'permissions.{name}.filesystem='+inline,
            '-c','shell_environment_policy.inherit="all"',
            '-c','shell_environment_policy.set={CODEX_HOME='+json.dumps(str(codex_home or user_dir/'.codex'))+'}',
            '-c',f'permissions.{name}.network.enabled=true',*argv]

def worker_invocation(inv,task,cfg,agents,*,prepare=True):
    item=validate(task,cfg);root=Path(item['root'])
    # Refuse links before execution. This also avoids giving a worker an in-root
    # junction or hardlink to another project's content.
    audit_tree(root)
    all_roots=[x['root'] for x in registry(cfg).values() if x['id']!=item['id'] and Path(x['root']).exists()]
    # Existing unregistered sibling projects are excluded as well.
    for p in root.parent.iterdir():
        if p.is_dir() and canonical(str(p))!=canonical(str(root)):all_roots.append(str(p.resolve()))
    dispatch=Path(__file__).resolve().parent
    runtime_base=dispatch.parent/'worker-runtime'
    runtime=runtime_base/item['id']/task['task_id']
    if prepare:runtime.mkdir(parents=True,exist_ok=True)
    for p in (dispatch,Path(cfg['project_registry']).parent,dispatch.parent/'secrets',dispatch.parent/'runtime'):
        if p.exists() and p!=root and root not in p.parents and p not in root.parents:all_roots.append(str(p))
    for p in runtime_base.iterdir() if runtime_base.exists() else ():
        if p.name!=item['id']:all_roots.append(str(p))
    user_dir=Path(os.environ.get('USERPROFILE',str(Path.home())))
    for relative in ('.claude/projects','.claude/tasks','.claude/plans','.claude/todos','.claude/debug','.claude/history.jsonl',
                     '.gemini/antigravity-cli/conversations','.gemini/antigravity-cli/brain','.gemini/antigravity-cli/history.jsonl',
                     '.gemini/antigravity-cli/conversation_summaries.db','.gemini/antigravity-cli/jetbox_summaries_proto.pb'):
        p=user_dir/relative
        if p.exists():all_roots.append(str(p))
    codex=agents['agents']['codex']['executable']
    writing=bool(cfg['task_types'][task['task_type']].get('write'))
    result=dict(inv)
    argv=list(inv['argv']);extra=[runtime]
    if task['agent']=='codex' and Path(argv[0]).resolve()==Path(codex).resolve():
        shared=Path(os.environ.get('CODEX_HOME') or str(Path(os.environ.get('USERPROFILE',str(Path.home())))/'.codex'))
        base_config=tomllib.loads((shared/'config.toml').read_text(encoding='utf-8'))
        disabled=list(base_config.get('mcp_servers',{}))
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+',name) for name in disabled):raise ProjectError('Unsupported shared MCP key')
        fs={str(Path(value)):'deny' for value in set(all_roots)}
        for entry in shared.iterdir():
            if entry.name not in ('packages','vendor_imports','bin','.sandbox-bin'):
                fs[str(entry)]='deny'
        fs[str(root)]='write' if writing else 'read'
        fs[str(runtime)]='write'
        inline='{'+','.join(json.dumps(k)+'='+json.dumps(v) for k,v in fs.items())+'}'
        request=dict(executable=codex,root=str(root),write=writing,filesystem_toml=inline,
            runtime=str(runtime),disabled_mcp=disabled,prompt=inv['stdin_text'])
        result['argv']=[sys.executable,str(dispatch/'codex_worker.py')]
        result['stdin_text']=json.dumps(request,ensure_ascii=False)
        result['env']=dict(os.environ,CODEX_HOME=str(shared))
        result['boundary']='codex-native-profile: verified via installed app-server protocol'
        return result
    if task['agent']=='agy' and Path(argv[0]).resolve()==Path(agents['agents']['agy']['executable']).resolve():
        if (root/'.agents'/'hooks.json').exists():raise ProjectError('Project AGY hooks require review before isolated execution')
        denied=['command(*)','mcp(*)','read_url(*)','execute_url(*)']
        denied += ['read_file('+Path(value).as_posix()+')' for value in sorted(set(all_roots))]
        denied += ['write_file('+(root/name).as_posix()+')' for name in ('.git','.agents')]
        if not writing:denied.append('write_file(*)')
        settings={'allowNonWorkspaceAccess':False,'toolPermission':'request-review',
            'permissions':{'allow':['read_file('+root.as_posix()+')']+(['write_file('+root.as_posix()+')'] if writing else []),
                           'deny':denied,'ask':[]}}
        result['argv']=[*argv,'--settings',json.dumps(settings,ensure_ascii=False),'--disable-slash-commands',
            '--log-file',str(runtime/'agy.log')]
        result['boundary']='agy-native-session-file-policy: terminal, MCP and browser denied'
        return result
    if task['agent']=='claude' and Path(argv[0]).resolve()==Path(agents['agents']['claude']['executable']).resolve():
        argv+=['--restricted','--strict-mcp-config','--disable-slash-commands',
               '--tools','Read,Grep,Glob,Edit,Write' if writing else 'Read,Grep,Glob']
        shared_settings=user_dir/'.claude'/'settings.json'
        if shared_settings.is_file():argv+=['--settings',str(shared_settings)]
    result['argv']=sandbox_argv(argv,root,sorted(set(all_roots)),writing,codex,extra)
    result['boundary']='windows-elevated: deny-other-projects-and-control-plane'
    return result

def guard_hook(payload,dispatch_root):
    """Return a decision or None to continue existing V7 policy validation."""
    cfg=json.loads((dispatch_root/'routing.json').read_text(encoding='utf-8'))
    if not cfg.get('project_registry'):
        if dispatch_root.resolve()==PRODUCTION_DISPATCH.resolve():
            return {'action':'block','message':'V8 project policy: production registry configuration missing'}
        return None # legacy offline fixture compatibility
    tool=payload.get('tool_name');args=payload.get('tool_input') or {};session=payload.get('session_id');db=dispatch_root/'state.sqlite3'
    def blocked(msg):return {'action':'block','message':'V8 project policy: '+msg}
    try:
        if tool=='delegate_task':return {} if args.get('action')=='list' else blocked('Use the shared Dispatcher workers')
        if tool in ('session_search','read_terminal'):return blocked('Cross-session history is not a scoped project source')
        if tool=='terminal':
            import shlex
            cmd=args.get('command','')
            if any(x in cmd for x in ('\n','\r','\x00',';','&','|','>','<','`','$(')):return blocked('Shell composition rejected')
            parts=shlex.split(cmd,posix=False)
            parts=[x[1:-1] if len(x)>1 and x[0]==x[-1] and x[0] in '\"\'' else x for x in parts]
            if len(parts)>=3 and Path(parts[0]).name.lower() in ('python','python.exe','python3','python3.exe') and canonical(parts[1])==canonical(str(dispatch_root/'dispatcher.py')):
                sub=parts[2];rest=parts[3:]
                if sub=='approve':
                    return blocked('Approval is only available in the local operator terminal')
                if sub in ('run','enqueue','queue','run-next','review','doctor','usage'):
                    import workflow
                    return workflow.guard_command(sub,rest,session,db,cfg,dispatch_root)
                if sub in ('bind','context','project-status') and '--session-id' not in rest:
                    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',session):return blocked('Missing runtime session identity')
                    if sub=='bind':
                        if len(rest)!=2 or rest[0]!='--project-id':return blocked('bind requires one project ID')
                        project(rest[1],cfg)
                    elif rest:return blocked('Unexpected project command arguments')
                    new_parts=parts[:3]+['--session-id',session]+rest
                    return {'action':'modify','args':dict(args,command=subprocess.list2cmdline(new_parts))}
                if sub=='projects':return {} if not rest else blocked('projects takes no arguments')
                if sub=='bind':
                    if len(rest)!=4 or rest[0]!='--session-id' or rest[2]!='--project-id' or rest[1]!=session:return blocked('Bind must use this exact session ID')
                    project(rest[3],cfg);return {}
                if sub=='context':
                    if rest!=['--session-id',session]:return blocked('Context must use this session ID')
                    project(bound_project(session,db),cfg);return {}
                if sub=='list':return blocked('Use project-status for scoped tasks')
                if sub=='project-status':return {} if rest==['--session-id',session] else blocked('Use this session ID')
                if sub in ('run','dry-run','cancel','status'):
                    pid=bound_project(session,db)
                    if '--task-id' in rest:
                        tid=rest[rest.index('--task-id')+1]
                        with closing(sqlite3.connect(db)) as c:
                            row=c.execute('SELECT project_id FROM tasks WHERE task_id=?',(tid,)).fetchone()
                        if not row or row[0]!=pid:return blocked('Task belongs to another project')
                if sub=='create':
                    if len(rest)!=2 or rest[0]!='--file':return blocked('Invalid create arguments')
                    p=inside(rest[1],dispatch_root/'inbox')
                    spec=json.loads(p.read_text(encoding='utf-8-sig'));validate(spec,cfg)
                    if spec.get('approval_state')=='approved':return blocked('Task cannot self-approve')
                    if spec['project_id']!=bound_project(session,db):return blocked('Task differs from bound project')
                    if spec.get('session_id')!=session:return blocked('Task must carry this session ID')
                return None
            # Use scoped file tools / Dispatcher for project inspection. Arbitrary
            # git options can execute helpers or redirect output despite their name.
            executable=Path(parts[0]).name.lower().removesuffix('.exe') if parts else ''
            if executable in ('hermes','codex','claude','agy'):return None
            return blocked('Use scoped file tools or Dispatcher for project work')
        if tool=='write_file':
            spec=json.loads(args.get('content',''));validate(spec,cfg)
            if spec['project_id']!=bound_project(session,db):return blocked('Task differs from bound project')
            if spec.get('approval_state')=='approved':return blocked('Task cannot self-approve')
            if 'session_id' not in spec:
                spec['session_id']=session
                return {'action':'modify','args':dict(args,content=json.dumps(spec,ensure_ascii=False))}
            if spec.get('session_id')!=session:return blocked('Task must carry this session ID')
            if spec.get('prompt_path'):inside(spec['prompt_path'],spec['project_root'])
            return None
        if tool in ('read_file','search_files'):
            # The manager skill is a fixed system resource, not project memory.
            bundled_skill=Path(__file__).resolve().parents[1]/'skills'/'team-project-manager'/'SKILL.md'
            hermes_home=Path(os.environ.get('HERMES_HOME',str(Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'hermes')))
            skill=hermes_home/'skills'/'team-project-manager'/'SKILL.md'
            raw=args.get('path','.')
            base=payload.get('cwd')
            if not Path(raw).is_absolute():
                if not base:raise ProjectError('Explicit absolute project path required')
                raw=str(Path(base)/raw)
            if tool=='read_file' and canonical(raw) in (canonical(str(skill)),canonical(str(bundled_skill))):return {}
            root=project(bound_project(session,db),cfg)['root'];inside(raw,root)
            # Searches can traverse links internally. Reject a search tree containing links.
            p=Path(raw)
            if tool=='search_files' and p.is_dir():
                audit_tree(p)
            return {}
        return None
    except (ProjectError,OSError,ValueError,KeyError,TypeError,IndexError,sqlite3.Error) as exc:return blocked(str(exc))
