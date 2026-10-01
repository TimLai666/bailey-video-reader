import json
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import batch_reader as batch
import reader


def test_memory_guard_reduces_parallelism(monkeypatch):
    monkeypatch.setattr(batch,'available_memory_mb',lambda:(1500,['test']))
    assert batch.choose_workers(2)[0]==1


def test_memory_guard_unknown_is_sequential(monkeypatch):
    monkeypatch.setattr(batch,'available_memory_mb',lambda:(None,[]))
    assert batch.choose_workers(2)[0]==1


def test_memory_guard_refuses_unsafe_headroom(monkeypatch):
    monkeypatch.setattr(batch,'available_memory_mb',lambda:(800,['test']))
    with pytest.raises(RuntimeError,match='memory'):batch.choose_workers(1)


def test_workers_maximum_two():
    with pytest.raises(ValueError):batch.choose_workers(3)


def test_manifest_relative_paths(tmp_path):
    m=tmp_path/'inputs.json';m.write_text(json.dumps({'inputs':['one.mp4','two.wav']}))
    a=batch.parser().parse_args(['--manifest',str(m),'-o','out'])
    assert batch.read_inputs(a)==[str(tmp_path/'one.mp4'),str(tmp_path/'two.wav')]


def test_manifest_rejects_objects(tmp_path):
    m=tmp_path/'inputs.json';m.write_text('[{"url":"not supported"}]')
    a=batch.parser().parse_args(['--manifest',str(m),'-o','out'])
    with pytest.raises(ValueError):batch.read_inputs(a)


def test_no_url_job():
    with pytest.raises(ValueError,match='URL input'):batch.prepare_item(1,'https://example.invalid/video',{})


def test_source_and_caption_content_change_identity(tmp_path):
    source=tmp_path/'v.mp4';source.write_bytes(b'video')
    cap=tmp_path/'v.srt';cap.write_text('first')
    first=batch.prepare_item(1,str(source),{'option':'same'})
    cap.write_text('second')
    assert batch.prepare_item(1,str(source),{'option':'same'})['key']!=first['key']
    cap.write_text('first');source.write_bytes(b'changed')
    assert batch.prepare_item(1,str(source),{'option':'same'})['key']!=first['key']


def test_options_and_version_change_identity(tmp_path):
    source=tmp_path/'v.mp4';source.write_bytes(b'video')
    assert batch.prepare_item(1,str(source),{'version':1})['key']!=batch.prepare_item(1,str(source),{'version':2})['key']


def fixture_output(tmp_path):
    context={'reader_sha256':'readerhash','webm_duration_sha256':'helperhash','model_files':{'model.bin':'modelhash'},'versions':{'test':'1'},
             'python':'3.12','ffmpeg':'ffmpeg 7','options':{'language':'en'}}
    item={'key':'key','identity':{'source_sha256':'sourcehash','sidecars':{},'context':context}}
    for file in ('asr.json','frames.json','captions.json','alignment.json'):
        reader.write_json(tmp_path/file,{'tracks':[]} if file=='captions.json' else {})
    m={'status':'complete','source':{'sha256':'sourcehash'},'reader_sha256':'readerhash','webm_duration_sha256':'helperhash',
       'model_files':context['model_files'],'versions':context['versions'],'python_version':context['python'],
       'ffmpeg_version':context['ffmpeg'],'parameters':context['options'],
       'artifact_sha256':{p.name:reader.sha256(p) for p in tmp_path.glob('*.json')}}
    reader.write_json(tmp_path/'manifest.json',m)
    reader.write_json(tmp_path/'batch-job.json',{'key':'key','manifest_sha256':reader.sha256(tmp_path/'manifest.json')})
    return item


def test_resume_requires_exact_artifact_hashes(tmp_path):
    item=fixture_output(tmp_path)
    assert batch.validated_output(tmp_path,item)
    (tmp_path/'asr.json').write_text('{"edited":true}')
    assert not batch.validated_output(tmp_path,item)


def test_resume_rejects_wrong_context(tmp_path):
    item=fixture_output(tmp_path);item['key']='different'
    assert not batch.validated_output(tmp_path,item)


def test_duration_helper_change_invalidates_resume(tmp_path):
    item=fixture_output(tmp_path)
    item['identity']['context']['webm_duration_sha256']='changedhelper'
    assert not batch.validated_output(tmp_path,item)


def test_resume_rejects_manifest_tampering(tmp_path):
    item=fixture_output(tmp_path)
    m=json.loads((tmp_path/'manifest.json').read_text());m['source']['sha256']='wrong'
    reader.write_json(tmp_path/'manifest.json',m)
    assert not batch.validated_output(tmp_path,item)


def test_resume_rejects_partial_output(tmp_path):
    assert not batch.validated_output(tmp_path,{'key':'x'})


@pytest.mark.parametrize('filename,value',[('manifest.json',[]),('manifest.json',None),('batch-job.json',[]),('batch-job.json',None)])
def test_resume_rejects_wrong_json_shapes(tmp_path,filename,value):
    item=fixture_output(tmp_path);reader.write_json(tmp_path/filename,value)
    assert not batch.validated_output(tmp_path,item)


def test_resume_rejects_non_dict_artifact_map(tmp_path):
    item=fixture_output(tmp_path);p=tmp_path/'manifest.json';m=json.loads(p.read_text());m['artifact_sha256']=[]
    reader.write_json(p,m);reader.write_json(tmp_path/'batch-job.json',{'key':'key','manifest_sha256':reader.sha256(p)})
    assert not batch.validated_output(tmp_path,item)


def test_queued_caption_change_is_detected(tmp_path):
    source=tmp_path/'v.mp4';source.write_bytes(b'video')
    caption=tmp_path/'v.srt';caption.write_text('before')
    item=batch.prepare_item(1,str(source),{})
    assert batch.identity_still_matches(item)
    caption.write_text('after')
    assert not batch.identity_still_matches(item)


def test_worker_model_identity_must_match(tmp_path):
    item=fixture_output(tmp_path);p=tmp_path/'manifest.json';m=json.loads(p.read_text());m['model_files']={'model.bin':'changed'}
    reader.write_json(p,m);reader.write_json(tmp_path/'batch-job.json',{'key':'key','manifest_sha256':reader.sha256(p)})
    assert not batch.validated_output(tmp_path,item)


def test_gapped_history_does_not_overwrite(tmp_path):
    (tmp_path/'run-0001.json').write_text('first');(tmp_path/'run-0003.json').write_text('third')
    assert batch.next_run_path(tmp_path).name=='run-0004.json'
    assert (tmp_path/'run-0003.json').read_text()=='third'


def test_output_directory_lock(tmp_path):
    import fcntl
    args=batch.parser().parse_args(['x.mp4','-o',str(tmp_path)])
    with (tmp_path/'.batch.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(RuntimeError,match='already owns'):batch.run_batch(args)


def test_benchmark_outputs_if_present():
    for name,workers in [('reviewed_sequential',1),('reviewed_parallel_two',2)]:
        p=ROOT/'reports'/'batch_benchmark'/name/'batch.json'
        if not p.exists():pytest.skip('Batch media benchmark not run')
        d=json.loads(p.read_text())
        assert d['status']=='preprocessing_complete'
        assert d['workers']==workers and d['counts']['total']==4
        assert d['analysis_status']=='not_performed'
        assert all(i['coverage']['asr_status']=='ok' for i in d['items'])
        assert all(i['analysis_status']=='not_performed' for i in d['items'])


def test_failure_does_not_stop_other_items_if_present():
    p=ROOT/'reports'/'batch_failures'/'batch.json'
    if not p.exists():pytest.skip('Failure media test not run')
    d=json.loads(p.read_text())
    assert d['status']=='finished_with_failures'
    assert d['counts']['failed_or_incomplete']==2
    assert d['counts']['preprocessed']+d['counts']['reused_verified']==2
    assert any(i['status']=='failed_preflight' for i in d['items'])


def test_real_resume_and_tamper_recovery_if_present():
    root=ROOT/'reports'/'batch_benchmark'/'reviewed_parallel_two'/'runs'
    if not (root/'run-0003.json').exists():pytest.skip('Resume/tamper integration not run')
    resume=json.loads((root/'run-0002.json').read_text())
    repair=json.loads((root/'run-0003.json').read_text())
    assert resume['counts']['reused_verified']==4 and resume['counts']['preprocessed']==0
    assert repair['counts']['reused_verified']==3 and repair['counts']['preprocessed']==1
    assert repair['items'][0]['output']!=resume['items'][0]['output']


def test_real_worker_timeout_if_present():
    p=ROOT/'reports'/'batch_timeout'/'batch.json'
    if not p.exists():pytest.skip('Worker timeout integration not run')
    d=json.loads(p.read_text())
    assert d['status']=='finished_with_failures'
    assert d['items'][0]['status']=='timed_out'
