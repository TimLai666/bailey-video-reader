from pathlib import Path
import sys, json, subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import reader


def test_pin():
    assert reader.verify_upstream()['commit'] == '0b4149d5c68fe3a5d6b01d6601c322378e703a89'


def test_gaps_are_not_silence_claims():
    gaps = reader.intervals_without_segments([{'start': 3, 'end': 4}, {'start': 3.5, 'end': 5}], 2, 7)
    assert [(x['start'], x['end']) for x in gaps] == [(2, 3), (5, 7)]
    assert all('unclassified' in x['status'] for x in gaps)


def test_caption_disagreement_kept_separate():
    frames = [{'id': 'f1', 'timestamp_sec': 11.0}]
    asr = {'segments': [{'id': 'a1', 'start': 10, 'end': 12, 'text': 'orange earns more profit'}]}
    captions = {'tracks': [{'id': 'c', 'segments': [{'id': 'c1', 'start': 10, 'end': 12, 'text': 'blue earns less profit'}]}]}
    data = reader.align_evidence(frames, asr, captions)
    assert data['utterances'][0]['frame_ids_within_utterance'] == ['f1']
    assert data['utterances'][0]['caption_comparisons'][0]['caption_ids'] == ['c1']
    assert asr['segments'][0]['text'] == 'orange earns more profit'


def test_nearest_frame_does_not_fake_overlap():
    data = reader.align_evidence([{'id':'f','timestamp_sec': 1}],
        {'segments':[{'id':'a','start': 2,'end':3,'text':'hello'}]}, {'tracks':[]})
    row = data['utterances'][0]
    assert row['frame_ids_within_utterance'] == []
    assert row['adjacent_frames'][0]['warning'] == 'nearest sampled frame, not inside utterance'


def test_local_only():
    args = reader.parser().parse_args(['https://example.invalid/film.mp4','-o','unused'])
    with pytest.raises(ValueError, match='local media only'):
        reader.process(args)


def test_fresh_output_required(tmp_path):
    src = tmp_path/'in.mp4';src.write_bytes(b'fake')
    out = tmp_path/'out';out.mkdir();(out/'old').write_text('old')
    with pytest.raises(ValueError, match='new or empty'):
        reader.process(reader.parser().parse_args([str(src),'-o',str(out)]))


def test_subprocess_no_network_protocol(monkeypatch):
    seen=[]
    def fake(cmd, **kwargs):
        seen.extend(cmd)
        assert 'shell' not in kwargs
        return subprocess.CompletedProcess(cmd,0,'{}','')
    monkeypatch.setattr(subprocess,'run',fake)
    reader.run(['ffprobe','-v','error','x.mp4'])
    assert seen[0] == '/usr/bin/ffprobe'
    assert seen[seen.index('-protocol_whitelist')+1] == 'file,pipe'


def test_partial_ffmpeg_failure_is_error(monkeypatch):
    monkeypatch.setattr(subprocess,'run',lambda cmd,**kw:subprocess.CompletedProcess(cmd,1,'','partial frames'))
    with pytest.raises(RuntimeError):reader.run(['ffmpeg','-i','x.mp4'])


def test_caption_window_source_clock(tmp_path):
    src = tmp_path/'x.mp4';src.write_bytes(b'placeholder')
    src.with_suffix('.srt').write_text('1\n00:00:08,000 --> 00:00:11,000\nKeep me\n\n2\n00:00:01,000 --> 00:00:02,000\nOutside\n')
    out = tmp_path/'out';(out/'captions').mkdir(parents=True)
    result=reader.collect_captions(src,[],out,9,12,[])
    assert result['tracks'][0]['segments'][0]['start']==8
    assert len(result['tracks'][0]['segments'])==1


def test_generated_artifacts_if_present():
    out=ROOT/'reports'/'synthetic_final'
    if not (out/'alignment.json').exists():pytest.skip('Integration test not run')
    m=json.loads((out/'manifest.json').read_text());a=json.loads((out/'asr.json').read_text())
    c=json.loads((out/'captions.json').read_text());f=json.loads((out/'frames.json').read_text())
    assert m['status']=='complete'
    assert a['status']=='ok' and a['device']=='cpu' and a['compute_type']=='int8'
    assert 'orange' in ' '.join(x['text'] for x in a['segments']).lower()
    assert 'blue product earns more profit' in c['tracks'][0]['segments'][-1]['text']
    assert all(x['dimensions']==[1600,900] for x in f['frames'])
    assert all(reader.sha256(out/k)==v for k,v in m['artifact_sha256'].items())


def test_real_nonzero_window():
    out=ROOT/'reports'/'synthetic_window_v2'
    if not (out/'alignment.json').exists():pytest.skip('Window integration not run')
    a=json.loads((out/'asr.json').read_text());f=json.loads((out/'frames.json').read_text())
    c=json.loads((out/'captions.json').read_text())
    assert a['segments'][0]['start'] >= 6.8
    assert a['segments'][-1]['end'] <= 12.9
    assert all(6.8 <= item['timestamp_sec'] <= 12.9 for item in f['frames'])
    assert 'orange' in a['segments'][0]['text']
    assert len(c['tracks'][0]['segments'])==1
    assert c['tracks'][0]['segments'][0]['start']==6.72 # retained source-time cue boundary


def test_real_no_audio():
    out=ROOT/'reports'/'silent'
    if not (out/'manifest.json').exists():pytest.skip('Silent integration not run')
    a=json.loads((out/'asr.json').read_text())
    assert a['status']=='no_audio_track' and a['segments']==[]


def test_native_chart_crop():
    from PIL import Image
    p=ROOT/'reports'/'profit_crop.png'
    if not p.exists():pytest.skip('Native crop not run')
    assert Image.open(p).size==(1450,700)
    d=json.loads(p.with_suffix('.json').read_text())
    assert 9 <= d['decoded_timestamp_sec'] < 9.1
    assert d['sha256']==reader.sha256(p)


@pytest.mark.parametrize('name',['audio_only_wav','audio_only_mp3'])
def test_real_audio_only(name):
    out=ROOT/'reports'/name
    if not (out/'manifest.json').exists():pytest.skip('Audio-only integration not run')
    m=json.loads((out/'manifest.json').read_text());a=json.loads((out/'asr.json').read_text())
    f=json.loads((out/'frames.json').read_text())
    assert m['status']=='complete' and m['input_modality']=='audio_only'
    assert a['status']=='ok' and len(a['segments'])>0
    assert f['frames']==[]
    assert (out/a['original_audio']['file']).stat().st_size>0


@pytest.mark.parametrize('name',['mandarin_audio','mandarin_fused'])
def test_mandarin_eighty_percent(name):
    out=ROOT/'reports'/name
    if not (out/'manifest.json').exists():pytest.skip('Licensed Mandarin fixture not run')
    a=json.loads((out/'asr.json').read_text())
    text=''.join(s['text'] for s in a['segments'])
    assert '80%' in text and '关税' in text
    assert a['status']=='ok'
    if name=='mandarin_fused':
        c=json.loads((out/'captions.json').read_text());f=json.loads((out/'frames.json').read_text())
        assert '百分之八十' in c['tracks'][0]['segments'][0]['text']
        assert len(f['frames'])==8
