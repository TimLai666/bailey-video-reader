"""Synthetic byte fixtures only. These tests do NOT validate real browser capture."""
import base64,copy,io,json,struct,sys,uuid,zipfile
from pathlib import Path
import pytest
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import reader,browser_import,asr_backends


def fixture():
    b=io.BytesIO();Image.new('RGB',(32,24),'red').save(b,'JPEG');jpeg=base64.b64encode(b.getvalue()).decode();pcm=base64.b64encode(struct.pack('<h',1000)*8000).decode()
    j={'schema':'browser-playback-evidence/0.2','run_id':str(uuid.uuid4()),'tab_instance':str(uuid.uuid4()),'source':{'sha256':'1'*64,'captions':{'origin':'synthetic test sidecar','cues':[{'start':0,'end':1,'text':'This is a caption, not speech.'}]}},'settings':{'start_requested':0,'duration_requested':2,'frame_interval_seconds':1},'sample_rate':16000,'frames':[{'media_time':0,'jpeg':jpeg},{'media_time':1,'jpeg':jpeg}], 'audio_chunks':[{'seq':i,'sample_start':i*8000,'sample_end':(i+1)*8000,'context_frame_end':(i+1)*8000,'sample_rate':16000,'pcm_s16le':pcm} for i in range(2)],'clock':[{'source_time':i/10,'audio_context_time':i/10,'wall_ms':i*100,'paused':False,'ready_state':4,'playback_rate':1} for i in range(20)],'summary':{'source_end':2},'events':[]}
    for collection in ('frames','audio_chunks','clock'):
        for item in j[collection]:item.update(run_id=j['run_id'],tab_instance=j['tab_instance'])
    return j

def run_import(tmp_path,j,**kwargs):
    file=tmp_path/'evidence.json';file.write_text(json.dumps(j));args=reader.parser().parse_args([str(file),'--browser-evidence','--skip-browser-asr','-o',str(tmp_path/'out')]);
    for k,v in kwargs.items():setattr(args,k,v)
    return reader.process(args),tmp_path/'out'

def test_roundtrip_formats_and_timestamps(tmp_path):
    j=fixture();m,out=run_import(tmp_path,j)
    assert m['status']=='imported_evidence' and m['browser_session']['run_id']==j['run_id']
    assert m['completeness']['asr']=='intentionally_not_run'
    f=json.loads((out/'frames.json').read_text());assert [x['timestamp_sec'] for x in f['frames']]==[0,1]
    a=json.loads((out/'asr.json').read_text());assert a['chunks'][0]['source_start_estimate']==0 and a['chunks'][0]['duration_seconds']==1
    assert a['source_capture_gaps'][0]['start']==1 and a['source_capture_gaps'][0]['end']==2
    assert a['segments']==[] and a['remote_transmission'] is False
    assert json.loads((out/'captions.json').read_text())['tracks'][0]['segments'][0]['text'].startswith('This is a caption')
    assert (out/'alignment.json').exists() and (out/'browser_capture.json').exists()

def test_audio_gaps_split_no_fake_silence(tmp_path):
    j=fixture();j['audio_chunks'][1].update(seq=2,sample_start=16000,sample_end=24000,context_frame_end=24000)
    m,out=run_import(tmp_path,j);a=json.loads((out/'asr.json').read_text())
    assert len(a['chunks'])==2 and len(a['capture_gaps'])==1
    assert [g['duration_seconds'] for g in a['chunks']]==[.5,.5]
    assert [g['source_start_estimate'] for g in a['chunks']]==[0,1]

def test_no_audio_and_no_frames_explicit(tmp_path):
    j=fixture();j['audio_chunks']=[];j['frames']=[]
    m,out=run_import(tmp_path,j);assert m['completeness']['audio']=='missing' and m['completeness']['frames']=='missing'
    assert json.loads((out/'asr.json').read_text())['status']=='missing_audio'
    gap=json.loads((out/'frames.json').read_text())['coverage_gaps'][0];assert gap=={'start':0.,'end':2.,'status':'no_frames_captured'}

@pytest.mark.parametrize('collection',['frames','audio_chunks','clock'])
@pytest.mark.parametrize('key',['run_id','tab_instance'])
def test_reject_mixed_sessions(tmp_path,collection,key):
    j=fixture();j[collection][0][key]=str(uuid.uuid4())
    with pytest.raises(ValueError,match='Mixed'):run_import(tmp_path,j)
    assert not (tmp_path/'out').exists()

def test_two_sessions_stay_separate(tmp_path):
    j=fixture();j2=fixture();a=tmp_path/'a';b=tmp_path/'b';a.mkdir();b.mkdir();m,oa=run_import(a,j);n,ob=run_import(b,j2)
    assert m['browser_session']!=n['browser_session'] and oa!=ob

@pytest.mark.parametrize('member',['../bundle.json','/bundle.json','dir/bundle.json','..\\bundle.json','C:bundle.json'])
def test_reject_archive_paths(tmp_path,member):
    z=tmp_path/'bad.zip'
    with zipfile.ZipFile(z,'w') as f:f.writestr(member,json.dumps(fixture()))
    with pytest.raises(ValueError,match='Unsafe ZIP'):browser_import.load_bundle(z)

def test_reject_zip_symlink(tmp_path):
    z=tmp_path/'bad.zip';info=zipfile.ZipInfo('bundle.json');info.create_system=3;info.external_attr=(0o120777<<16)
    with zipfile.ZipFile(z,'w') as f:f.writestr(info,'/tmp/secret')
    with pytest.raises(ValueError,match='links'):browser_import.load_bundle(z)

def test_accept_single_safe_zip(tmp_path):
    z=tmp_path/'ok.zip';j=fixture()
    with zipfile.ZipFile(z,'w',zipfile.ZIP_DEFLATED) as f:f.writestr('bundle.json',json.dumps(j))
    assert browser_import.load_bundle(z)['run_id']==j['run_id']

def test_reject_zip_extra_members(tmp_path):
    z=tmp_path/'bad.zip'
    with zipfile.ZipFile(z,'w') as f:f.writestr('bundle.json','{}');f.writestr('extra','ignored')
    with pytest.raises(ValueError,match='only bundle'):browser_import.load_bundle(z)

@pytest.mark.parametrize('change',[lambda j:j['frames'][0].update(media_time=float('nan')),lambda j:j['audio_chunks'][1].update(sample_start=0),lambda j:j['audio_chunks'][0].update(pcm_s16le='junk!'),lambda j:j['source']['captions']['cues'][0].update(end=-1)])
def test_reject_bad_timestamps_or_payload(tmp_path,change):
    j=fixture();change(j)
    with pytest.raises(ValueError):run_import(tmp_path,j)

def test_unstable_clocks_not_false_alignment(tmp_path):
    j=fixture();j['clock'][-1]['audio_context_time']=10
    m,out=run_import(tmp_path,j);a=json.loads((out/'asr.json').read_text());assert a['chunks'][0]['source_start_estimate'] is None
    assert 'source_clock_mapping_unreliable_or_absent' in a['quality_flags']

def test_no_overwrite(tmp_path):
    j=fixture();run_import(tmp_path,j)
    with pytest.raises(ValueError,match='never overwrite'):run_import(tmp_path,j)

def test_local_asr_adapter_real_interface_mock_output(tmp_path,monkeypatch):
    j=fixture();calls=[]
    # No inference here: check actual backend invocation, with captions excluded.
    def fake(src,out,stream,model,language,start,end,threads):
        calls.append(src);return {'segments':[{'id':'asr_0','start':0.1,'end':0.3,'text':'spoken evidence','words':[]}]}
    monkeypatch.setattr(reader,'transcribe_audio',fake)
    m,out=run_import(tmp_path,j,skip_browser_asr=False)
    a=json.loads((out/'asr.json').read_text());assert len(calls)==1 and calls[0].name=='captured.wav'
    assert a['status']=='ok' and a['segments'][0]['text']=='spoken evidence' and a['segments'][0]['start']==.1

def test_remote_backend_rejected():
    with pytest.raises(ValueError,match='Only local'):asr_backends.get_backend('remote',None,None,None,None)


@pytest.mark.parametrize("shift",[1000,-1000])
def test_constant_offset_cannot_map_outside_window(tmp_path,shift):
    j=fixture()
    if shift>0:
        for c in j['clock']:c['source_time']+=shift
        with pytest.raises(ValueError,match='anchor source time'):run_import(tmp_path,j)
    else:
        for c in j['clock']:c['audio_context_time']-=shift
        m,out=run_import(tmp_path,j)
        a=json.loads((out/'asr.json').read_text())
        assert a['chunks'][0]['source_start_estimate'] is None
        assert 'mapped_audio_outside_capture_window' in a['quality_flags']

def test_one_anchor_not_sufficient(tmp_path):
    j=fixture();j['clock']=j['clock'][:1]
    m,out=run_import(tmp_path,j)
    assert json.loads((out/'asr.json').read_text())['chunks'][0]['source_start_estimate'] is None

def test_nonmonotonic_audio_clock_rejected(tmp_path):
    j=fixture();j['clock'][-1]['audio_context_time']=0
    with pytest.raises(ValueError,match='AudioContext'):run_import(tmp_path,j)


@pytest.mark.parametrize('collection',['frames','audio_chunks','clock'])
@pytest.mark.parametrize('key',['run_id','tab_instance'])
def test_missing_per_item_identity_rejected(tmp_path,collection,key):
    j=fixture();del j[collection][0][key]
    with pytest.raises(ValueError,match='Missing per-item'):run_import(tmp_path,j)


def test_nonzero_source_window_maps_audio_and_frames(tmp_path):
    j=fixture();j['settings']['start_requested']=5;j['summary']['source_end']=7
    for f in j['frames']:f['media_time']+=5
    for c in j['clock']:c['source_time']+=5
    m,out=run_import(tmp_path,j);a=json.loads((out/'asr.json').read_text());f=json.loads((out/'frames.json').read_text())
    assert a['chunks'][0]['source_start_estimate']==5 and f['frames'][0]['timestamp_sec']==5


def test_old_deployed_v01_is_explicitly_rejected(tmp_path):
    j=fixture();j['schema']='browser-playback-evidence/0.1'
    with pytest.raises(ValueError,match='Unsupported browser evidence schema'):run_import(tmp_path,j)
