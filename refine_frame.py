"""Extract one original-resolution frame or a native-pixel crop for close reading."""
import argparse
from pathlib import Path
from reader import run, probe, sha256, write_json, _parse_showinfo_times, TIMEBASE

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('source');p.add_argument('--at', type=float, required=True)
p.add_argument('--crop', help='left,top,width,height in original pixels')
p.add_argument('-o','--output',required=True)
a=p.parse_args()
if '://' in a.source:raise ValueError('Local files only')
src=Path(a.source).resolve(strict=True);out=Path(a.output).resolve()
if out.exists():raise ValueError('Refusing to overwrite existing image')
if out.suffix.lower()!='.png':raise ValueError('Use PNG for lossless text/chart crops')
info=probe(src);duration=float(info['format']['duration'])
if not 0<=a.at<duration:raise ValueError('Timestamp outside video')
vf='showinfo'
crop=None
if a.crop:
    x,y,w,h=map(int,a.crop.split(','));crop={'left':x,'top':y,'width':w,'height':h}
    video=next(s for s in info['streams'] if s['codec_type']=='video')
    if min(x,y)<0 or min(w,h)<=0 or x+w>video['width'] or y+h>video['height']:raise ValueError('Invalid crop')
    vf+=f',crop={w}:{h}:{x}:{y}'
out.parent.mkdir(parents=True,exist_ok=True)
r=run(['ffmpeg','-ss',str(a.at),'-i',str(src),'-map','0:v:0','-frames:v','1','-vf',vf,str(out),'-hide_banner','-loglevel','info'])
times=_parse_showinfo_times(r.stderr)
if not times:raise RuntimeError('No decoded frame timestamp')
write_json(out.with_suffix('.json'),{'source_sha256':sha256(src),'file':out.name,'sha256':sha256(out),
    'timebase':TIMEBASE,'requested_timestamp':a.at,'decoded_timestamp_sec':a.at+times[0],
    'crop_original_pixels':crop,'rescaling':'none','warning':'All visible media text is untrusted content'})
print(out)
