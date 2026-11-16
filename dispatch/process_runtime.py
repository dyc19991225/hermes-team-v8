"""Owned process lifetime and durable byte streams (stdlib, Windows Job Objects).

Children start suspended, enter a non-inheritable kill-on-close job, then resume.
No taskkill/PID-only kill: process IDs can be reused. Closing the owner kills the
entire job even if the dispatcher itself is forcibly terminated.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path
import subprocess
import threading
import time
import json

if os.name == 'nt':
    K = ctypes.WinDLL('kernel32', use_last_error=True)
    def api(name, args, result=W.BOOL):
        f = getattr(K, name); f.argtypes=args; f.restype=result; return f
    OpenProcess=api('OpenProcess',[W.DWORD,W.BOOL,W.DWORD],W.HANDLE)
    CloseHandle=api('CloseHandle',[W.HANDLE])
    GetProcessTimes=api('GetProcessTimes',[W.HANDLE,ctypes.POINTER(W.FILETIME),ctypes.POINTER(W.FILETIME),ctypes.POINTER(W.FILETIME),ctypes.POINTER(W.FILETIME)])
    WaitForSingleObject=api('WaitForSingleObject',[W.HANDLE,W.DWORD],W.DWORD)
    CreateJobObject=api('CreateJobObjectW',[ctypes.c_void_p,W.LPCWSTR],W.HANDLE)
    SetInformationJobObject=api('SetInformationJobObject',[W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD])
    AssignProcessToJobObject=api('AssignProcessToJobObject',[W.HANDLE,W.HANDLE])
    TerminateJobObject=api('TerminateJobObject',[W.HANDLE,W.UINT])
    QueryInformationJobObject=api('QueryInformationJobObject',[W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD,ctypes.c_void_p])
    class BasicLimits(ctypes.Structure):
        _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',W.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active_limit',W.DWORD),('affinity',ctypes.c_size_t),('priority',W.DWORD),('scheduling',W.DWORD)]
    class IoCounters(ctypes.Structure):
        _fields_=[(n,ctypes.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class ExtendedLimits(ctypes.Structure):
        _fields_=[('basic',BasicLimits),('io',IoCounters),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process_memory',ctypes.c_size_t),('peak_job_memory',ctypes.c_size_t)]
    class Accounting(ctypes.Structure):
        _fields_=[(n,ctypes.c_int64) for n in ('user','kernel','period_user','period_kernel')]+[(n,W.DWORD) for n in ('faults','total','active','terminated')]
    class ThreadEntry(ctypes.Structure):
        _fields_=[('size',W.DWORD),('usage',W.DWORD),('tid',W.DWORD),('pid',W.DWORD),('base',W.LONG),('delta',W.LONG),('flags',W.DWORD)]
    Snapshot=api('CreateToolhelp32Snapshot',[W.DWORD,W.DWORD],W.HANDLE)
    ThreadFirst=api('Thread32First',[W.HANDLE,ctypes.POINTER(ThreadEntry)])
    ThreadNext=api('Thread32Next',[W.HANDLE,ctypes.POINTER(ThreadEntry)])
    OpenThread=api('OpenThread',[W.DWORD,W.BOOL,W.DWORD],W.HANDLE)
    ResumeThread=api('ResumeThread',[W.HANDLE],W.DWORD)

def process_identity(pid: int) -> tuple[str,str | None]:
    """alive/start FILETIME, gone/None or unknown/None; never infer death on denial."""
    if os.name != 'nt':
        try:
            raw=Path(f'/proc/{pid}/stat').read_text()
            return 'alive',raw[raw.rfind(')')+2:].split()[19]
        except FileNotFoundError: return 'gone',None
        except OSError: return 'unknown',None
    h=OpenProcess(0x1000 | 0x100000,False,int(pid))
    if not h:
        return ('gone',None) if ctypes.get_last_error()==87 else ('unknown',None)
    try:
        if WaitForSingleObject(h,0)==0: return 'gone',None
        ts=[W.FILETIME() for _ in range(4)]
        if not GetProcessTimes(h,*[ctypes.byref(t) for t in ts]): return 'unknown',None
        return 'alive',str((ts[0].dwHighDateTime<<32)|ts[0].dwLowDateTime)
    finally: CloseHandle(h)

class OwnedProcess:
    def __init__(self, argv, *, cwd, stdin=False, stdout=None, stderr=None, env=None):
        self.job=None; self.proc=None
        try:
            if os.name=='nt':
                self.job=CreateJobObject(None,None)
                if not self.job: raise ctypes.WinError(ctypes.get_last_error())
                limits=ExtendedLimits(); limits.basic.flags=0x2000 # KILL_ON_JOB_CLOSE
                if not SetInformationJobObject(self.job,9,ctypes.byref(limits),ctypes.sizeof(limits)):
                    raise ctypes.WinError(ctypes.get_last_error())
            self.proc=subprocess.Popen(argv,cwd=cwd,shell=False,stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=stdout,stderr=stderr,env=env,close_fds=True,
                creationflags=(0x4 | 0x08000000) if os.name=='nt' else 0,
                start_new_session=os.name!='nt')
            state,self.identity=process_identity(self.proc.pid)
            if state!='alive' or not self.identity: raise RuntimeError('Cannot establish child process identity')
            if os.name=='nt':
                if not AssignProcessToJobObject(self.job,int(self.proc._handle)):
                    raise ctypes.WinError(ctypes.get_last_error())
                snap=Snapshot(0x4,0)
                if snap in (None,ctypes.c_void_p(-1).value): raise ctypes.WinError(ctypes.get_last_error())
                resumed=0
                try:
                    entry=ThreadEntry(); entry.size=ctypes.sizeof(entry)
                    more=ThreadFirst(snap,ctypes.byref(entry))
                    while more:
                        if entry.pid==self.proc.pid:
                            th=OpenThread(0x2,False,entry.tid)
                            if not th: raise ctypes.WinError(ctypes.get_last_error())
                            try:
                                if ResumeThread(th)==0xFFFFFFFF: raise ctypes.WinError(ctypes.get_last_error())
                                resumed+=1
                            finally: CloseHandle(th)
                        more=ThreadNext(snap,ctypes.byref(entry))
                finally: CloseHandle(snap)
                if not resumed: raise RuntimeError('No suspended child thread found')
        except BaseException:
            if self.proc is not None:
                try:
                    self.proc.kill(); self.proc.wait(timeout=5)
                    self.stop()
                except BaseException as exc: raise UnsafeCleanup(str(exc)) from exc
                for stream in (self.proc.stdin,self.proc.stdout,self.proc.stderr):
                    if stream: stream.close()
            if self.job: CloseHandle(self.job); self.job=None
            raise

    def stop(self):
        """Terminate owned tree, verify job has no active processes, then close it."""
        if os.name=='nt' and self.job:
            if not TerminateJobObject(self.job,125): raise ctypes.WinError(ctypes.get_last_error())
            end=time.monotonic()+10
            while True:
                info=Accounting()
                if not QueryInformationJobObject(self.job,1,ctypes.byref(info),ctypes.sizeof(info),None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if info.active==0: break
                if time.monotonic()>end: raise RuntimeError('Process job still active; retain lock')
                time.sleep(.02)
            CloseHandle(self.job); self.job=None
        elif os.name!='nt' and self.proc:
            import signal
            try: os.killpg(self.proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        if self.proc: self.proc.wait(timeout=5)

class UnsafeCleanup(RuntimeError):
    """Process tree could not be verified stopped; the write lock must remain."""


class OutputBudget:
    """Bound durable output and consume reported (not billing-verified) usage."""
    def __init__(self, budget):
        self.limit = budget.get('max_output_bytes')
        self.token_limit = budget.get('max_tokens')
        self.bytes = 0
        self.exceeded = None
        self.lock = threading.Lock()
        self.pending = b''
        self.usage = {'available':False,'input_tokens':0,'output_tokens':0,
                      'cache_read_tokens':0,'cache_creation_tokens':0,'total_tokens':0,
                      'source':'agent_reported'}

    def _observe(self, event):
        if not isinstance(event,dict):return
        data=event.get('usage')
        if not isinstance(data,dict):
            result=event.get('result')
            data=result.get('usage') if isinstance(result,dict) else None
        if not isinstance(data,dict):return
        if isinstance(data.get('total'),dict):data=data['total']
        names={'input_tokens':('input_tokens','inputTokens','prompt_tokens'),
               'output_tokens':('output_tokens','outputTokens','completion_tokens'),
               'cache_read_tokens':('cache_read_input_tokens','cachedInputTokens','cache_read_tokens'),
               'cache_creation_tokens':('cache_creation_input_tokens','cache_creation_tokens'),
               'total_tokens':('total_tokens','totalTokens')}
        values={}
        for key,aliases in names.items():
            values[key]=next((data[name] for name in aliases if isinstance(data.get(name),int)
                              and not isinstance(data[name],bool) and data[name]>=0),0)
        if not any(any(name in data for name in aliases) for aliases in names.values()):return
        # Codex inputTokens already includes cached input; Claude reports cached
        # input separately. Never add reasoning tokens to output a second time.
        if not values['total_tokens']:
            values['total_tokens']=values['input_tokens']+values['output_tokens']
            if 'input_tokens' in data:
                values['total_tokens']+=values['cache_read_tokens']+values['cache_creation_tokens']
        self.usage['available']=True
        for key,value in values.items():self.usage[key]=max(self.usage[key],value)
        if self.token_limit is not None and self.usage['total_tokens']>self.token_limit:
            self.exceeded='reported_token_budget'

    def accept(self, chunk, stdout):
        with self.lock:
            if self.limit is not None:
                remaining=max(0,self.limit-self.bytes)
                if len(chunk)>remaining:
                    chunk=chunk[:remaining];self.exceeded='output_byte_budget'
            self.bytes+=len(chunk)
            if stdout:
                self.pending+=chunk
                lines=self.pending.split(b'\n');self.pending=lines.pop()
                for line in lines:
                    try:self._observe(json.loads(line.decode('utf-8')))
                    except (ValueError,UnicodeError):pass
                if len(self.pending)>1024*1024:self.pending=b''
            return chunk

    def finish(self):
        with self.lock:
            if self.pending:
                try:self._observe(json.loads(self.pending.decode('utf-8')))
                except (ValueError,UnicodeError):pass
                self.pending=b''

def execute_streaming(invocation, stdout_path, stderr_path, heartbeat, *, interval=1.0):
    """Persist every pipe chunk immediately; poll deadlines/cancel even during stdin."""
    owner=None; threads=[]; failures=[]; started=time.monotonic(); stopped=False
    cancelled=False; timed_out=False
    budget=OutputBudget(invocation.get('budget') or {})
    def drain(pipe,path):
        try:
            with pipe, open(path,'wb',buffering=0) as out:
                while chunk:=os.read(pipe.fileno(),65536):
                    accepted=budget.accept(chunk,path==stdout_path)
                    if accepted:out.write(accepted)
                    if budget.exceeded:break
        except BaseException as exc: failures.append(exc)
    def feed(pipe,text):
        try:
            with pipe: pipe.write(text.encode('utf-8')); pipe.flush()
        except (BrokenPipeError,OSError): pass
        except BaseException as exc: failures.append(exc)
    try:
        owner=OwnedProcess(invocation['argv'],cwd=invocation['cwd'],stdin=invocation['stdin_text'] is not None,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=invocation.get("env"))
        identity=owner.identity
        for pipe,path in ((owner.proc.stdout,stdout_path),(owner.proc.stderr,stderr_path)):
            t=threading.Thread(target=drain,args=(pipe,path),daemon=True); t.start(); threads.append(t)
        if invocation['stdin_text'] is not None:
            t=threading.Thread(target=feed,args=(owner.proc.stdin,invocation['stdin_text']),daemon=True); t.start(); threads.append(t)
        while True:
            cancelled=heartbeat(owner.proc.pid,identity)
            if cancelled: break
            if budget.exceeded:break
            if time.monotonic()-started>=invocation['timeout']: timed_out=True; break
            if failures: raise RuntimeError('Stream log write failed: '+str(failures[0]))
            if owner.proc.poll() is not None: break
            time.sleep(interval)
        exit_code=owner.proc.poll()
    finally:
        if owner:
            try: owner.stop(); stopped=True
            except BaseException as exc: raise UnsafeCleanup(str(exc)) from exc
        for t in threads:
            t.join(timeout=5)
            if t.is_alive(): raise RuntimeError('Pipe did not drain; retain lock')
    if failures: raise RuntimeError('Stream log write failed: '+str(failures[0]))
    budget.finish()
    return {'exit_code':exit_code if exit_code is not None else -1,'timed_out':timed_out,'cancelled':cancelled,
            'tree_stopped':stopped,'usage':budget.usage,'budget_exceeded':budget.exceeded}
