"""Recreate all existing integration artifacts in this staging checkout, offline."""
from pathlib import Path
import json,subprocess,sys,time
ROOT=Path(__file__).resolve().parents[1];PYTHON=sys.executable

def run(args,expected=0):
    t=time.monotonic();p=subprocess.run([PYTHON,*args],cwd=ROOT,capture_output=True,text=True)
    print(json.dumps({'command':args,'exit':p.returncode,'seconds':round(time.monotonic()-t,3),'tail':p.stdout[-400:]+p.stderr[-300:]},ensure_ascii=False),flush=True)
    if p.returncode!=expected:raise RuntimeError(f'Unexpected exit {p.returncode} for {args[0]}')

for source,name,extra in [
 ('fixtures/chart_speech/chart_speech.mp4','synthetic_final',['--language','en']),
 ('fixtures/chart_speech/chart_speech.mp4','synthetic_window_v2',['--language','en','--start','6.8','--end','12.9']),
 ('fixtures/silent.mp4','silent',[]),
 ('fixtures/chart_speech/audio_0.wav','audio_only_wav',['--language','en']),
 ('fixtures/speech.mp3','audio_only_mp3',['--language','en']),
 ('fixtures/mandarin/fleurs-cmn_hans_cn-validation-179.wav','mandarin_audio',['--language','zh']),
 ('fixtures/mandarin/mandarin_chart.mkv','mandarin_fused',['--language','zh'])]:
 run(['reader.py',source,'-o','reports/'+name,*extra])
run(['refine_frame.py','fixtures/chart_speech/chart_speech.mp4','--at','9','--crop','180,230,1450,700','-o','reports/profit_crop.png'])
inputs=['fixtures/chart_speech/chart_speech.mp4','fixtures/mandarin/mandarin_chart.mkv','fixtures/chart_speech/audio_0.wav','fixtures/speech.mp3']
for workers,name in [(1,'reviewed_sequential'),(2,'reviewed_parallel_two')]:run(['batch_reader.py',*inputs,'-o','reports/batch_benchmark/'+name,'--workers',str(workers)])
parallel=ROOT/'reports/batch_benchmark/reviewed_parallel_two'
run(['batch_reader.py',*inputs,'-o',str(parallel),'--workers','2','--resume'])
report=json.loads((parallel/'batch.json').read_text());target=parallel/report['items'][0]['output']/'asr.json'
target.write_text(target.read_text()+'\n')  # intentional recoverable fixture hash tamper
run(['batch_reader.py',*inputs,'-o',str(parallel),'--workers','2','--resume'])
bad=ROOT/'reports/corrupt-test-media.bin';bad.write_bytes(b'controlled invalid test fixture')
run(['batch_reader.py',inputs[0],str(ROOT/'reports/missing-test-file.mp4'),str(bad),inputs[2],'-o','reports/batch_failures','--workers','2'],expected=2)
run(['batch_reader.py',inputs[0],'-o','reports/batch_timeout','--job-timeout-seconds','0.01'],expected=2)
run(['-m','pytest','-q','tests'])
