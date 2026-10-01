"""Offline queue tests with a self-authored mock reader; no browser capture."""
import hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import queue_reader as q
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='session')
def video(tmp_path_factory):
    p=tmp_path_factory.mktemp('own-video')/'fixture.mp4'
    subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=160x90:d=1','-c:v','libx264','-pix_fmt','yuv420p',str(p)],check=True)
    return p

@pytest.fixture
def queue(tmp_path):
    path=tmp_path/'queue';q.initialize(path);return path

def add(queue,video,label='test',jid=None):
    session=str(uuid.uuid4());j=q.register(queue,session,label,jid);q.ready(queue,j['job_id'],session,str(video),q.hashfile(video));return j

def job(queue,j):return q.load_job(q.queue_root(queue),j['job_id'])[1]

def mock_reader(tmp_path,delay=0,fail_ids=()):
    path=tmp_path/'mock_reader.py';path.write_text('''import argparse,hashlib,json,time,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('-o',required=True);p.add_argument('--threads');p.add_argument('--language');a=p.parse_args();out=Path(a.o)
(out.parent/'asr-started').write_text('mock ASR began')
time.sleep(DELAY)
if Path(a.source).parent.parent.name in FAIL:sys.exit(7)
out.mkdir()
for name in ['asr.json','frames.json','captions.json','alignment.json']:(out/name).write_text(json.dumps({'mock':True,'status':'fixture_only'}))
h=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
(out/'manifest.json').write_text(json.dumps({'status':'complete','source':{'sha256':h(a.source)},'artifact_sha256':{p.name:h(p) for p in out.iterdir()}}))
'''.replace('DELAY',repr(delay)).replace('FAIL',repr(tuple(fail_ids))))
    return path

def launch(queue,reader):
    return subprocess.Popen([sys.executable,str(ROOT/'queue_reader.py'),'--queue',str(queue),'drain','--reader',str(reader)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

def wait_until(predicate,seconds=5):
    stop=time.monotonic()+seconds
    while time.monotonic()<stop:
        if predicate():return
        time.sleep(.025)
    raise AssertionError('Expected state not reached')

def test_missing_file_never_ready(queue,tmp_path):
    sid=str(uuid.uuid4());j=q.register(queue,sid,'not landed');r=q.ready(queue,j['job_id'],sid,str(tmp_path/'missing.webm'),'0'*64)
    assert r['state']=='awaiting_transfer' and not (q.job_path(queue,j['job_id'])/'ready.json').exists()

def test_enqueue_b_while_a_transcribes_and_b_failure_does_not_block_c(queue,video,tmp_path):
    a=add(queue,video,'A');b_id=str(uuid.uuid4());reader=mock_reader(tmp_path,delay=.8,fail_ids=[b_id]);proc=launch(queue,reader)
    try:
        wait_until(lambda:list((q.job_path(queue,a['job_id'])/'attempts').glob('*/asr-started')) if (q.job_path(queue,a['job_id'])/'attempts').exists() else False)
        assert job(queue,a)['state']=='transcribing'
        began=time.monotonic();b=add(queue,video,'B',b_id);c=add(queue,video,'C');enqueue_seconds=time.monotonic()-began
        assert enqueue_seconds<.6 and job(queue,a)['state']=='transcribing'
        stdout,stderr=proc.communicate(timeout=8);assert proc.returncode==0,(stdout,stderr)
        assert [job(queue,j)['state'] for j in (a,b,c)]==['completed','failed','completed']
        assert q.drain(queue,reader)['handled']==[]
    finally:
        if proc.poll() is None:proc.terminate();proc.wait(timeout=5)

def test_duplicate_register_ready_and_drain_are_idempotent(queue,video,tmp_path):
    j=add(queue,video);same=q.register(queue,j['session_id'],j['source_id'],j['job_id']);assert same['job_id']==j['job_id']
    r=mock_reader(tmp_path);q.drain(queue,r);q.ready(queue,j['job_id'],j['session_id'],str(video),q.hashfile(video));assert q.drain(queue,r)['handled']==[]
    assert len(job(queue,j)['attempts'])==1

def test_session_mismatch_refused(queue,video):
    j=add(queue,video)
    with pytest.raises(ValueError,match='session'):q.ready(queue,j['job_id'],str(uuid.uuid4()),str(video),q.hashfile(video))

def test_ready_hash_mismatch_fails_only_job(queue,video):
    sid=str(uuid.uuid4());j=q.register(queue,sid,'wrong hash')
    with pytest.raises(ValueError,match='SHA-256'):q.ready(queue,j['job_id'],sid,str(video),'0'*64)
    assert job(queue,j)['state']=='failed'

def test_mutated_queued_file_isolated(queue,video,tmp_path):
    a=add(queue,video,'tampered');c=add(queue,video,'C');p=q.job_path(queue,a['job_id'])/'input/media.mp4';p.chmod(0o600);p.write_bytes(b'corrupt')
    q.drain(queue,mock_reader(tmp_path));assert job(queue,a)['state']=='failed' and job(queue,c)['state']=='completed'

def test_sigterm_interrupt_resumes_fresh_attempt(queue,video,tmp_path):
    j=add(queue,video);reader=mock_reader(tmp_path,delay=10);proc=launch(queue,reader)
    try:
        p=q.job_path(queue,j['job_id']);wait_until(lambda:bool(list(p.glob('attempts/*/asr-started'))));proc.terminate();stdout,stderr=proc.communicate(timeout=5)
        assert proc.returncode==130,(stdout,stderr);assert job(queue,j)['state']=='ready' and job(queue,j)['attempts'][0]['state']=='interrupted'
        reader=mock_reader(tmp_path);q.drain(queue,reader);assert job(queue,j)['state']=='completed' and len(job(queue,j)['attempts'])==2
    finally:
        if proc.poll() is None:proc.kill();proc.wait()

def test_atomic_receipt_recovery(queue,video):
    j=add(queue,video);p,current=q.load_job(queue,j['job_id']);current['state']='awaiting_transfer';current.pop('ready_manifest_sha256');q.atomic_json(p/'job.json',current)
    q.recover(queue);assert job(queue,j)['state']=='ready'

def test_stale_transcribing_recovered(queue,video):
    j=add(queue,video);p,current=q.load_job(queue,j['job_id']);current['state']='transcribing';current['attempts']=[{'state':'running','number':1,'evidence_relpath':'attempts/0001/evidence','process':{'pid':99999999,'start_ticks':'0'}}];q.atomic_json(p/'job.json',current)
    q.recover(queue);assert job(queue,j)['state']=='ready'

def test_single_consumer_lock(queue,tmp_path):
    with q.locked(queue,'.consumer.lock',True):
        with pytest.raises(RuntimeError,match='Another consumer'):q.drain(queue,mock_reader(tmp_path))

def test_tampered_session_receipt_is_failed(queue,video,tmp_path):
    j=add(queue,video);p=q.job_path(queue,j['job_id']);r=q.read_json(p/'ready.json');r['session_id']=str(uuid.uuid4());q.atomic_json(p/'ready.json',r)
    q.drain(queue,mock_reader(tmp_path));assert job(queue,j)['state']=='failed'

def test_source_config_file_never_read(queue,tmp_path):
    sid=str(uuid.uuid4());j=q.register(queue,sid,'not media');file=tmp_path/'.env';file.write_text('test-only fake value')
    with pytest.raises(ValueError,match='extension'):q.ready(queue,j['job_id'],sid,str(file),'0'*64)
    assert job(queue,j)['state']=='awaiting_transfer'

def test_no_audio_status_retained(queue,video):
    j=add(queue,video);r=q.read_json(q.job_path(queue,j['job_id'])/'ready.json');assert r['probe']['audio_status']=='missing_audio'


def test_leftover_attempt_directory_never_overwritten(queue,video,tmp_path):
    j=add(queue,video);p=q.job_path(queue,j['job_id'])/'attempts/0001';p.mkdir(parents=True);(p/'keep.txt').write_text('retain')
    q.drain(queue,mock_reader(tmp_path));assert (p/'keep.txt').read_text()=='retain' and job(queue,j)['attempts'][0]['number']==2


def test_recovery_rejects_attempt_traversal(queue,video):
    j=add(queue,video);p,current=q.load_job(queue,j['job_id']);current['state']='transcribing';current['attempts']=[{'state':'running','number':1,'evidence_relpath':'../../outside','process':None}];q.atomic_json(p/'job.json',current)
    q.recover(queue);assert job(queue,j)['state']=='failed'


def test_source_symlink_not_read(queue,video,tmp_path):
    link=tmp_path/'link.mp4';link.symlink_to(video);sid=str(uuid.uuid4());j=q.register(queue,sid,'symlink')
    with pytest.raises(ValueError,match='regular file'):q.ready(queue,j['job_id'],sid,str(link),q.hashfile(video))


def test_live_orphan_not_reprocessed(queue,video):
    j=add(queue,video);p,current=q.load_job(queue,j['job_id']);current['state']='transcribing';current['attempts']=[{'state':'running','number':1,'evidence_relpath':'attempts/0001/evidence','process':q.proc_identity(os.getpid())}];q.atomic_json(p/'job.json',current)
    result=q.recover(queue);assert result['active_orphan_jobs']==[j['job_id']] and job(queue,j)['state']=='transcribing'


def test_malformed_job_isolated_from_next(queue,video,tmp_path):
    a=add(queue,video,'malformed');c=add(queue,video,'C');p,current=q.load_job(queue,a['job_id']);del current['created_epoch'];q.atomic_json(p/'job.json',current)
    q.drain(queue,mock_reader(tmp_path));assert job(queue,c)['state']=='completed'
    statuses={j['job_id']:j['state'] for j in q.status(queue)['jobs']};assert statuses[a['job_id']]=='failed'


def test_unsafe_attempt_directory_does_not_block_next(queue,video,tmp_path):
    a=add(queue,video,'bad directory');c=add(queue,video,'C');outside=tmp_path/'outside';outside.mkdir();(q.job_path(queue,a['job_id'])/'attempts').symlink_to(outside,target_is_directory=True)
    q.drain(queue,mock_reader(tmp_path));assert job(queue,a)['state']=='failed' and job(queue,c)['state']=='completed' and not list(outside.iterdir())


def test_recorded_pid_is_asr_process_not_wrapper_child(queue,video,tmp_path):
    j=add(queue,video);reader=mock_reader(tmp_path,delay=5);proc=launch(queue,reader)
    try:
        wait_until(lambda:bool(list(q.job_path(queue,j['job_id']).glob('attempts/*/asr-started'))));pid=job(queue,j)['attempts'][0]['process']['pid']
        raw=Path(f'/proc/{pid}/cmdline').read_bytes();assert str(reader).encode() in raw and b'queue_worker.py' not in raw
    finally:
        if proc.poll() is None:proc.terminate();proc.communicate(timeout=5)


def test_expired_orphan_stopped_and_next_job_runs(queue,video,tmp_path):
    a=add(queue,video,'orphan');c=add(queue,video,'C');reader=mock_reader(tmp_path,delay=10);proc=launch(queue,reader);orphan=None
    try:
        wait_until(lambda:bool(list(q.job_path(queue,a['job_id']).glob('attempts/*/asr-started'))));p,current=q.load_job(queue,a['job_id']);orphan=current['attempts'][0]['process'];proc.kill();proc.communicate(timeout=3)
        current['attempts'][0]['deadline_epoch']=time.time()-1;q.atomic_json(p/'job.json',current);q.recover(queue)
        assert job(queue,a)['state']=='failed' and not q.same_live_process(orphan)
        reader=mock_reader(tmp_path);q.drain(queue,reader);assert job(queue,c)['state']=='completed'
    finally:
        if proc.poll() is None:proc.kill();proc.wait()
        if orphan and q.same_live_process(orphan):os.killpg(orphan['pid'],9)


def test_interrupt_after_output_commit_does_not_repeat_completed_asr(queue,video,tmp_path,monkeypatch):
    j=add(queue,video);reader=mock_reader(tmp_path);original=q.valid_result;calls=[]
    def interrupt_once(*args):
        calls.append(1)
        if len(calls)==1:raise KeyboardInterrupt
        return original(*args)
    monkeypatch.setattr(q,'valid_result',interrupt_once)
    with pytest.raises(KeyboardInterrupt):q.drain(queue,reader)
    assert job(queue,j)['state']=='completed' and q.drain(queue,reader)['handled']==[]


@pytest.mark.parametrize('history',[None,'bad',{},[1]])
def test_malformed_history_isolated(queue,video,tmp_path,history):
    a=add(queue,video,'bad history');c=add(queue,video,'C');p,current=q.load_job(queue,a['job_id']);current['history']=history;q.atomic_json(p/'job.json',current)
    q.drain(queue,mock_reader(tmp_path));assert job(queue,c)['state']=='completed'
    states={j['job_id']:j['state'] for j in q.status(queue)['jobs']};assert states[a['job_id']]=='failed'
