"""Single-turn stdio adapter for the Codex app-server protocol verified locally.

Authentication stays in the original user. The server must acknowledge the exact
named project permission profile before a turn can be submitted.
"""
import json,subprocess,sys
from collections import deque

def configure_streams():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')

def emit(value):print(json.dumps(value,ensure_ascii=True),flush=True)

def main(probe=False):
    configure_streams()
    spec=json.loads(sys.stdin.read());name='hermes_v8'
    args=[spec['executable'],'app-server','--stdio',
          '-c','permissions.'+name+'.extends='+json.dumps(':workspace' if spec['write'] else ':read-only'),
          '-c','permissions.'+name+'.filesystem='+spec['filesystem_toml'],
          '-c','permissions.'+name+'.network.enabled=true',
          '-c','sqlite_home='+json.dumps(spec['runtime']),
          '-c','log_dir='+json.dumps(spec['runtime']+'/logs'),
          '-c','memories.use_memories=false','-c','memories.generate_memories=false',
          '-c','features.memories=false','-c','features.apps=false',
          '-c','notify=[]','-c','windows.sandbox="elevated"']
    for server in spec['disabled_mcp']:
        args+=['-c','mcp_servers.'+server+'.enabled=false']
    process=subprocess.Popen(args,cwd=spec['root'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=sys.stderr,text=True,encoding='utf-8',errors='replace',bufsize=1,
        creationflags=0x08000000 if sys.platform=='win32' else 0)
    serial=0
    pending=deque()
    def send(method,params=None,notification=False):
        nonlocal serial
        serial+=1;request={'method':method}
        if params is not None:request['params']=params
        if not notification:request['id']=serial
        process.stdin.write(json.dumps(request)+'\n');process.stdin.flush();return serial
    def read_message():
        line=process.stdout.readline()
        if not line:raise RuntimeError('Codex app-server closed before completion')
        response=json.loads(line)
        if 'method' in response and 'id' in response:
            # All interactive or tool-approval requests fail closed.
            method=response['method']
            if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
                answer={'id':response['id'],'result':{'decision':'decline'}}
            else:answer={'id':response['id'],'error':{'code':-32601,'message':'Unattended project worker cannot grant interactive authority'}}
            process.stdin.write(json.dumps(answer)+'\n');process.stdin.flush()
        return response
    def receive():
        return pending.popleft() if pending else read_message()
    def request(method,params):
        target=send(method,params)
        while True:
            message=read_message()
            if message.get('id')==target and 'method' not in message:
                if 'error' in message:raise RuntimeError(str(message['error']))
                return message['result']
            if 'method' in message:pending.append(message)
    try:
        request('initialize',{'clientInfo':{'name':'hermes_team_v8','version':'8.1.0'},'capabilities':{'experimentalApi':True}})
        send('initialized',notification=True)
        started=request('thread/start',{'cwd':spec['root'],'approvalPolicy':'never','permissions':name,'ephemeral':True})
        active=started.get('activePermissionProfile') or {}
        if active.get('id')!=name or started.get('sandbox',{}).get('type')=='dangerFullAccess':
            raise RuntimeError('Codex did not activate the required named project profile')
        emit({'type':'boundary.verified','profile':name,'sandbox':started.get('sandbox'),'cwd':started.get('cwd')})
        if probe:
            emit({'type':'result','subtype':'success','is_error':False,'probe_only':True,'model_turns':0})
            return 0
        thread=started['thread']['id']
        turn=request('turn/start',{'threadId':thread,'permissions':name,'approvalPolicy':'never',
            'input':[{'type':'text','text':spec['prompt'],'text_elements':[]}]})
        turn_id=turn['turn']['id'];final_text=''
        while True:
            message=receive();method=message.get('method');params=message.get('params') or {}
            if method=='item/agentMessage/delta':
                delta=params.get('delta','');final_text+=delta;emit({'type':'agent.delta','text':delta})
            elif method=='item/completed':
                item=params.get('item') or {}
                if item.get('type')=='agentMessage' and item.get('text'):final_text=item['text']
                emit({'type':'item.completed','item':item})
            elif method=='turn/completed' and params.get('turn',{}).get('id')==turn_id:
                completed=params['turn'];success=completed.get('status')=='completed'
                emit({'type':'result','subtype':'success' if success else 'error','is_error':not success,
                    'result':final_text,'error':completed.get('error')})
                return 0 if success else 1
            elif method=='error':emit({'type':'server.error','error':params})
            elif method=='thread/tokenUsage/updated':
                emit({'type':'usage','usage':params.get('tokenUsage',{}).get('total',{}),'cumulative':True})
    finally:
        process.terminate()
        try:process.wait(timeout=8)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        for stream in (process.stdin,process.stdout):
            if stream:stream.close()

if __name__=='__main__':
    try:sys.exit(main(probe=sys.argv[1:]==['--probe']))
    except Exception as exc:
        emit({'type':'result','subtype':'error','is_error':True,'error':str(exc)})
        sys.exit(1)
