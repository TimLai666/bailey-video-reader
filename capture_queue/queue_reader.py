#!/usr/bin/env python3
"""Finite local capture→ASR queue. No browser, network, .env, or permanent daemon."""
from __future__ import annotations
import argparse,contextlib,fcntl,hashlib,json,math,os,re,signal,stat,subprocess,sys,tempfile,time,uuid
from pathlib import Path

SCHEMA='bailey-capture-queue/1'
EXTENSIONS={'.mp4','.webm','.mkv','.mov','.m4v','.avi'}
MAX_BYTES=2*1024**3

def require(ok,message):
    if not ok:raise ValueError(message)

def hashfile(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def fsync_directory(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def atomic_json(path,data):
    path=Path(path);tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    raw=(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode()
    with tmp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    fsync_directory(path.parent)

def read_json(path):
    with Path(path).open('rb') as f:raw=f.read(2*1024*1024+1)
    require(len(raw)<=2*1024*1024,'Queue metadata too large')
    value=json.loads(raw);require(isinstance(value,dict),'Invalid queue metadata');return value

def id_value(value):
    try:return str(uuid.UUID(value))
    except (ValueError,TypeError,AttributeError):raise ValueError('Job/session ID must be a UUID')

def queue_root(path):
    root=Path(path).resolve();require((root/'queue.json').is_file(),'Initialize the queue first')
    require(read_json(root/'queue.json').get('schema')==SCHEMA,'Unknown queue schema')
    require((root/'jobs').is_dir() and not (root/'jobs').is_symlink() and (root/'jobs').resolve().parent==root,'Unsafe jobs directory');return root

@contextlib.contextmanager
def locked(root,name='.state.lock',nonblock=False):
    with (root/name).open('a+b') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|(fcntl.LOCK_NB if nonblock else 0))
        except BlockingIOError:raise RuntimeError('Another consumer owns this queue')
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)

def initialize(path):
    root=Path(path).resolve();root.mkdir(parents=True,exist_ok=True)
    with locked(root):
        if (root/'queue.json').exists():require(read_json(root/'queue.json').get('schema')==SCHEMA,'Unknown queue schema')
        else:atomic_json(root/'queue.json',{'schema':SCHEMA,'created_epoch':time.time(),'consumer_limit':1})
        (root/'jobs').mkdir(exist_ok=True)
    return {'queue':str(root),'consumer_limit':1}

def job_path(root,job_id):
    path=root/'jobs'/id_value(job_id)
    require(not path.is_symlink() and path.resolve().parent==(root/'jobs').resolve(),'Unsafe job directory')
    return path

def load_job(root,job_id):
    p=job_path(root,job_id);j=read_json(p/'job.json');require(j.get('job_id')==p.name,'Job identity mismatch')
    require(j.get('schema')==SCHEMA,'Invalid job schema');id_value(j.get('session_id'))
    require(isinstance(j.get('source_id'),str) and 1<=len(j['source_id'])<=160,'Invalid source identity')
    require(j.get('state') in ('awaiting_transfer','ready','transcribing','completed','failed'),'Invalid job state')
    created=j.get('created_epoch');require(isinstance(created,(int,float)) and not isinstance(created,bool) and math.isfinite(created) and created>0,'Invalid job creation time')
    require(isinstance(j.get('attempts'),list) and all(isinstance(a,dict) for a in j['attempts']),'Invalid attempts metadata')
    require(isinstance(j.get('history'),list) and all(isinstance(h,dict) for h in j['history']),'Invalid history metadata')
    return p,j

def transition(path,j,state,detail=None):
    j['state']=state;j['updated_epoch']=time.time();j.setdefault('history',[]).append({'state':state,'at_epoch':j['updated_epoch'],'detail':detail});atomic_json(path/'job.json',j)

def register(path,session_id,source_id,job_id=None):
    root=queue_root(path);sid=id_value(session_id);jid=id_value(job_id or str(uuid.uuid4()))
    require(isinstance(source_id,str) and 1<=len(source_id)<=160 and not any(c in source_id for c in '\r\n'),'Source ID must be a short label, not credentials')
    with locked(root):
        p=job_path(root,jid)
        if p.exists():
            _,j=load_job(root,jid);require(j['session_id']==sid and j['source_id']==source_id,'Existing job has different source/session');return j
        p.mkdir();j={'schema':SCHEMA,'job_id':jid,'session_id':sid,'source_id':source_id,'attempts':[],'created_epoch':time.time()};transition(p,j,'awaiting_transfer');fsync_directory(root/'jobs')
    return j

def media_probe(path):
    r=subprocess.run(['/usr/bin/ffprobe','-v','error','-protocol_whitelist','file,pipe','-show_entries','format=duration:stream=index,codec_type,codec_name','-of','json',str(path)],capture_output=True,text=True,timeout=30)
    require(r.returncode==0,'Local file is not a readable supported media container')
    m=json.loads(r.stdout);duration=float(m.get('format',{}).get('duration',0));require(0<duration<=7200,'Media duration missing or over two hours')
    streams=m.get('streams',[]);require(any(s.get('codec_type')=='video' for s in streams),'Queue accepts already-landed video files only')
    return {'duration_seconds':duration,'streams':streams,'audio_status':'present' if any(s.get('codec_type')=='audio' for s in streams) else 'missing_audio'}

def ready(path,job_id,session_id,file,sha256):
    root=queue_root(path);sid=id_value(session_id);require(re.fullmatch('[0-9a-f]{64}',sha256 or '') is not None,'Expected SHA-256 is required')
    with locked(root):
        p,j=load_job(root,job_id);require(j['session_id']==sid,'Source/session mismatch')
        if (p/'ready.json').exists():
            r=read_json(p/'ready.json');require(r['sha256']==sha256 and r['session_id']==sid,'Job is immutably bound to different media/session');verify_ready(p,j);return j
        require(j['state']=='awaiting_transfer','Job is not awaiting transfer')
        src=Path(file).expanduser();require('://' not in str(file),'Only an existing local file may become ready')
        require(src.suffix.lower() in EXTENSIONS,'Unsupported input extension; environment/config files are never media')
        if not src.exists():
            j['last_error']='File has not landed locally';transition(p,j,'awaiting_transfer','missing_file');return j
        require(stat.S_ISREG(src.lstat().st_mode),'Input must be a regular file, not a symlink/device')
        require(0<src.stat().st_size<=MAX_BYTES,'Empty or oversized media file')
        input_dir=p/'input';require(not input_dir.is_symlink() and input_dir.resolve().parent==p.resolve(),'Unsafe input directory');input_dir.mkdir(exist_ok=True);suffix=src.suffix.lower();final=input_dir/('media'+suffix)
        temporary=input_dir/('staging-'+uuid.uuid4().hex+suffix)
        try:
            # This copy establishes queue-owned immutable bytes before readiness.
            h=hashlib.sha256();count=0
            descriptor=os.open(src,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(descriptor,'rb') as source,temporary.open('xb') as target:
                info=os.fstat(source.fileno());require(stat.S_ISREG(info.st_mode) and 0<info.st_size<=MAX_BYTES,'Input descriptor is not bounded regular media')
                for block in iter(lambda:source.read(1024*1024),b''):
                    count+=len(block);require(count<=MAX_BYTES,'Media grew beyond queue limit');h.update(block);target.write(block)
                target.flush();os.fsync(target.fileno())
            require(h.hexdigest()==sha256,'Source SHA-256 mismatch')
            probe=media_probe(temporary);os.replace(temporary,final);os.chmod(final,0o400);fsync_directory(input_dir)
            receipt={'schema':'bailey-capture-ready/1','job_id':j['job_id'],'session_id':sid,'source_id':j['source_id'],'media_relpath':str(final.relative_to(p)),'sha256':sha256,'bytes':count,'probe':probe,'timebase':'seconds from recording start; original programme source clock is not established','ready_epoch':time.time()}
            atomic_json(p/'ready.json',receipt);j['ready_manifest_sha256']=hashfile(p/'ready.json');j['media_sha256']=sha256;transition(p,j,'ready')
        except Exception as e:
            # Preserve source; remove only our incomplete temporary copy.
            if temporary.exists():temporary.unlink()
            j['last_error']=str(e);transition(p,j,'failed','ready_validation_failed');raise
    return j

def verify_ready(p,j):
    receipt=read_json(p/'ready.json')
    for key in ('job_id','session_id','source_id'):require(receipt.get(key)==j.get(key),'Ready manifest source/session mismatch')
    require(receipt.get('schema')=='bailey-capture-ready/1','Invalid ready manifest schema')
    if j.get('ready_manifest_sha256'):require(hashfile(p/'ready.json')==j['ready_manifest_sha256'],'Ready manifest SHA-256 changed')
    relative=receipt.get('media_relpath','');require(re.fullmatch(r'input/media\.(mp4|webm|mkv|mov|m4v|avi)',relative) is not None,'Unsafe media path')
    media=p/relative;require(not (p/'input').is_symlink(),'Unsafe input directory');require(media.is_file() and not media.is_symlink() and media.resolve().parent==(p/'input').resolve(),'Media missing or redirected')
    require(media.stat().st_size==receipt['bytes'] and hashfile(media)==receipt['sha256'],'Queued media SHA-256 changed')
    require(j.get('media_sha256',receipt['sha256'])==receipt['sha256'],'Job media SHA-256 mismatch')
    return media,receipt

def proc_identity(pid):
    try:
        value=Path(f'/proc/{pid}/stat').read_text();parts=value[value.rfind(')')+2:].split();return {'pid':pid,'start_ticks':parts[19],'state':parts[0]}
    except (OSError,IndexError):return None

def same_live_process(identity):
    if not identity:return False
    now=proc_identity(identity['pid']);return bool(now and now['start_ticks']==identity['start_ticks'] and now['state']!='Z')

def argv_hash(argv):
    return hashlib.sha256(('\0'.join(argv)+'\0').encode()).hexdigest()

def stop_expired_orphan(identity,attempt):
    # Only our still-matching process group and exact launched argv are eligible.
    pid=identity['pid']
    require(same_live_process(identity),'Orphan identity changed')
    require(os.getpgid(pid)==pid,'Unexpected orphan process group')
    raw=Path(f'/proc/{pid}/cmdline').read_bytes()
    require(hashlib.sha256(raw).hexdigest() in attempt.get('allowed_argv_sha256',[]),'Orphan command no longer matches this attempt')
    os.killpg(pid,signal.SIGTERM);stop=time.monotonic()+3
    while same_live_process(identity) and time.monotonic()<stop:time.sleep(.025)
    if same_live_process(identity):os.killpg(pid,signal.SIGKILL)

def valid_result(evidence,sha):
    try:
        m=read_json(evidence/'manifest.json');require(m.get('status')=='complete' and m.get('source',{}).get('sha256')==sha,'Reader did not certify this media')
        hashes=m.get('artifact_sha256');require(isinstance(hashes,dict),'Missing output hashes')
        for name in ('asr.json','frames.json','captions.json','alignment.json'):require(name in hashes,'Missing evidence stream')
        for relative,expected in hashes.items():
            p=evidence/relative;require(not Path(relative).is_absolute() and '..' not in Path(relative).parts and p.resolve().is_relative_to(evidence.resolve()),'Unsafe output path')
            require(p.is_file() and not p.is_symlink() and hashfile(p)==expected,'Output artifact hash mismatch')
        return True
    except (ValueError,KeyError,OSError,json.JSONDecodeError):return False

def quarantine(root,path,error):
    try:
        p=job_path(root,path.name);atomic_json(p/'failure.json',{'job_id':p.name,'state':'failed','error':str(error),'reason':'invalid_queue_metadata','at_epoch':time.time()})
    except (ValueError,OSError):pass

def recover_locked(root):
    active=[]
    for p in sorted((root/'jobs').iterdir()):
        if not p.is_dir():continue
        try:
            _,j=load_job(root,p.name)
            if j['state']=='awaiting_transfer' and (p/'ready.json').exists():
                _,r=verify_ready(p,j);j['media_sha256']=r['sha256'];j['ready_manifest_sha256']=hashfile(p/'ready.json');transition(p,j,'ready','recovered_atomic_ready_handoff')
            if j['state']!='transcribing':continue
            attempt=j['attempts'][-1]
            if same_live_process(attempt.get('process')):
                deadline=attempt.get('deadline_epoch')
                if isinstance(deadline,(int,float)) and time.time()>=deadline:
                    try:stop_expired_orphan(attempt['process'],attempt)
                    except Exception as e:active.append(j['job_id']);j['last_error']='Cannot safely stop expired orphan: '+str(e);atomic_json(p/'job.json',j);continue
                    attempt['state']='failed';j['last_error']='Orphan ASR exceeded persisted deadline';transition(p,j,'failed','orphan_timed_out');continue
                active.append(j['job_id']);continue
            _,r=verify_ready(p,j);relative=attempt.get('evidence_relpath','');require(re.fullmatch(r'attempts/[0-9]{4,}/evidence',relative) is not None,'Unsafe attempt path');evidence=p/relative;require(evidence.resolve().is_relative_to(p.resolve()),'Unsafe attempt path')
            if valid_result(evidence,r['sha256']):attempt['state']='completed';transition(p,j,'completed','recovered_completed_output')
            else:attempt['state']='interrupted';transition(p,j,'ready','recovered_interrupted_attempt')
        except Exception as e:
            if 'j' in locals() and j.get('job_id')==p.name:j['last_error']=str(e);transition(p,j,'failed','recovery_validation_failed')
            else:quarantine(root,p,e)
    return active

def recover(path):
    root=queue_root(path)
    with locked(root,'.consumer.lock',True),locked(root):active=recover_locked(root)
    return {'active_orphan_jobs':active,'status':'wait_for_existing_process' if active else 'recovered'}

def terminate_child(proc):
    if proc.poll() is not None:return
    try:os.killpg(proc.pid,signal.SIGTERM);proc.wait(timeout=3)
    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
    except ProcessLookupError:pass

def drain(path,reader_path,threads=2,language='auto',timeout=3600,max_jobs=None):
    root=queue_root(path);reader=Path(reader_path).resolve(strict=True);require(reader.is_file() and reader.suffix=='.py','Reader must be an explicit local Python script')
    require(1<=threads<=4,'Threads must be 1–4');require(timeout>0,'Timeout must be positive');require(max_jobs is None or max_jobs>0,'max-jobs must be positive')
    handled=[]
    with locked(root,'.consumer.lock',True):
        with locked(root):
            active=recover_locked(root)
            if active:return {'status':'wait_for_existing_process','active_orphan_jobs':active,'handled':[]}
        while max_jobs is None or len(handled)<max_jobs:
            with locked(root):
                selected=None;available=[]
                for p in sorted((root/'jobs').iterdir(),key=lambda p:p.name):
                    if not p.is_dir():continue
                    try:_,j=load_job(root,p.name)
                    except Exception as e:quarantine(root,p,e);continue
                    if j['state']=='ready':available.append((p,j))
                if available:selected=min(available,key=lambda item:(item[1]['created_epoch'],item[1]['job_id']))
                if selected is None:break
                p,j=selected
                try:media,r=verify_ready(p,j)
                except Exception as e:j['last_error']=str(e);transition(p,j,'failed','pre_asr_validation_failed');handled.append({'job_id':j['job_id'],'state':'failed'});continue
                try:
                    attempts_dir=p/'attempts'
                    require(not attempts_dir.is_symlink() and attempts_dir.resolve().parent==p.resolve(),'Unsafe attempts directory');attempts_dir.mkdir(exist_ok=True)
                    number=len(j['attempts'])+1;attempt_dir=attempts_dir/f'{number:04d}'
                    while attempt_dir.exists():number+=1;attempt_dir=attempts_dir/f'{number:04d}'
                    attempt_dir.mkdir(exist_ok=False);evidence=attempt_dir/'evidence'
                    attempt={'number':number,'state':'running','evidence_relpath':str(evidence.relative_to(p)),'started_epoch':time.time(),'deadline_epoch':time.time()+timeout,'reader_sha256':hashfile(reader)};j['attempts'].append(attempt);transition(p,j,'transcribing')
                except Exception as e:
                    j['last_error']=str(e);transition(p,j,'failed','attempt_setup_failed');handled.append({'job_id':j['job_id'],'state':'failed'});continue
            proc=None
            try:
                with (attempt_dir/'worker.log').open('wb') as log:
                    reader_args=[str(media),'-o',str(evidence),'--threads',str(threads),'--language',language]
                    command=[sys.executable,str(Path(__file__).with_name('queue_worker.py')),'--gate',str(attempt_dir/'start.json'),'--job',j['job_id'],'--reader',str(reader),'--',*reader_args]
                    proc=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    with locked(root):
                        attempt['process']=proc_identity(proc.pid);attempt['allowed_argv_sha256']=[argv_hash(command),argv_hash([sys.executable,str(reader),*reader_args])];atomic_json(p/'job.json',j)
                        atomic_json(attempt_dir/'start.json',{'job_id':j['job_id']})
                    try:code=proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:terminate_child(proc);raise RuntimeError('ASR worker timed out')
                with locked(root):
                    verify_ready(p,j);require(code==0 and valid_result(evidence,r['sha256']),'ASR failed or output integrity check failed');attempt['state']='completed';attempt['finished_epoch']=time.time();transition(p,j,'completed')
            except KeyboardInterrupt:
                if proc:terminate_child(proc)
                with locked(root):
                    attempt['finished_epoch']=time.time()
                    try:verify_ready(p,j);finished=valid_result(evidence,r['sha256'])
                    except Exception:finished=False
                    if finished:attempt['state']='completed';transition(p,j,'completed','completed_output_reconciled_during_interrupt')
                    else:attempt['state']='interrupted';transition(p,j,'ready','consumer_interrupted; retry uses a fresh attempt')
                raise
            except Exception as e:
                if proc:terminate_child(proc)
                with locked(root):attempt['state']='failed';attempt['finished_epoch']=time.time();j['last_error']=str(e);transition(p,j,'failed','worker_failed')
            handled.append({'job_id':j['job_id'],'state':j['state']})
    return {'status':'drained','handled':handled,'daemon':False,'consumer_limit':1}

def status(path):
    root=queue_root(path)
    with locked(root):
        jobs=[]
        for p in sorted((root/'jobs').iterdir()):
            try:_,j=load_job(root,p.name);jobs.append(j)
            except Exception as e:
                try:id_value(p.name);jobs.append({'job_id':p.name,'state':'failed','reason':'invalid_queue_metadata','error':str(e)})
                except ValueError:pass
    return {'schema':SCHEMA,'jobs':jobs}

def parser():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--queue',required=True);sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('init');sub.add_parser('status');sub.add_parser('recover')
    a=sub.add_parser('register');a.add_argument('--session',required=True);a.add_argument('--source-id',required=True);a.add_argument('--job-id')
    a=sub.add_parser('ready');a.add_argument('--job',required=True);a.add_argument('--session',required=True);a.add_argument('--file',required=True);a.add_argument('--sha256',required=True)
    a=sub.add_parser('drain');a.add_argument('--reader',required=True);a.add_argument('--threads',type=int,default=2);a.add_argument('--language',default='auto');a.add_argument('--timeout',type=float,default=3600);a.add_argument('--max-jobs',type=int)
    return p

def main():
    def stop(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop);args=parser().parse_args()
    try:
        if args.command=='init':result=initialize(args.queue)
        elif args.command=='register':result=register(args.queue,args.session,args.source_id,args.job_id)
        elif args.command=='ready':result=ready(args.queue,args.job,args.session,args.file,args.sha256)
        elif args.command=='drain':result=drain(args.queue,args.reader,args.threads,args.language,args.timeout,args.max_jobs)
        elif args.command=='recover':result=recover(args.queue)
        else:result=status(args.queue)
        print(json.dumps(result,ensure_ascii=False));return 0
    except KeyboardInterrupt:print(json.dumps({'status':'interrupted; active attempt safely reconciled'}));return 130
    except Exception as e:print(json.dumps({'status':'error','error':str(e)},ensure_ascii=False),file=sys.stderr);return 2
if __name__=='__main__':sys.exit(main())
