"""Create OFFLINE synthetic importer fixture. This is NOT browser-captured media."""
import argparse,base64,hashlib,io,json,subprocess,uuid
from pathlib import Path
from PIL import Image
p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();root=Path(__file__).resolve().parents[1];fixture=root/'fixtures/chart_speech';src=fixture/'chart_speech.mp4'
raw=subprocess.check_output(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error','-protocol_whitelist','file,pipe','-i',str(src),'-t','12','-vn','-ar','16000','-ac','1','-f','s16le','pipe:1'])
raw=raw[:12*16000*2]
frames=[]
for i in range(12):
 with Image.open(fixture/('revenue.png' if i<6.72 else 'profit.png')) as im:
  im.thumbnail((1280,720));b=io.BytesIO();im.save(b,'JPEG');frames.append({'media_time':i,'jpeg':base64.b64encode(b.getvalue()).decode()})
chunks=[]
for i,lo in enumerate(range(0,len(raw)//2,5*16000)):
 part=raw[lo*2:(lo+5*16000)*2];hi=lo+len(part)//2;chunks.append({'seq':i,'sample_start':lo,'sample_end':hi,'context_frame_end':hi,'sample_rate':16000,'pcm_s16le':base64.b64encode(part).decode()})
j={'schema':'browser-playback-evidence/0.2','synthetic_fixture':True,'run_id':str(uuid.uuid4()),'tab_instance':str(uuid.uuid4()),'source':{'title':'OFFLINE synthetic importer fixture; NOT browser capture','sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'origin':'Self-created CC0 chart/speech, decoded offline solely for importer testing','captions':{'origin':'deliberately inconsistent independent synthetic sidecar','cues':[{'start':0,'end':6.72,'text':'The blue bar has higher revenue.'},{'start':6.72,'end':12.99,'text':'The blue product earns more profit after costs.'}]}},'settings':{'start_requested':0,'duration_requested':12,'frame_interval_seconds':1},'sample_rate':16000,'frames':frames,'audio_chunks':chunks,'clock':[{'source_time':i/10,'audio_context_time':i/10,'wall_ms':i*100,'paused':False,'ready_state':4,'playback_rate':1} for i in range(121)],'summary':{'source_end':12,'runtime_claim':'synthetic timestamps; no browser runtime measurement'},'events':[]}
for collection in ('frames','audio_chunks','clock'):
 for item in j[collection]:item.update(run_id=j['run_id'],tab_instance=j['tab_instance'])
out=Path(a.output);out.parent.mkdir(exist_ok=True,parents=True);out.write_text(json.dumps(j));print(out)
