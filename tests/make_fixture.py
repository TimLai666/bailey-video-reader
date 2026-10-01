"""Original synthetic speech + charts; no third-party media or website download."""
from pathlib import Path
import json, subprocess
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'fixtures' / 'chart_speech'
OUT.mkdir(parents=True, exist_ok=True)
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

def call(args):
    subprocess.run(args, check=True, capture_output=True)

def image(name, title, values, labels, footer):
    im = Image.new('RGB', (1920, 1080), '#fafafa')
    d = ImageDraw.Draw(im)
    f = lambda n: ImageFont.truetype(FONT, n)
    d.text((100, 80), title, fill='#172b3a', font=f(58))
    d.text((100, 175), 'Atlas vs Boreal | USD millions', fill='#334155', font=f(38))
    d.line((240, 280, 240, 820, 1660, 820), fill='#64748b', width=5)
    scale = 500 / max(values)
    for x, value, label, color in zip((480, 1180), values, labels, ('#1976d2', '#ef8c23')):
        top = 820-value*scale
        d.rectangle((x-120, top, x+120, 820), fill=color)
        d.text((x-70, top-65), str(value), fill='#172b3a', font=f(52))
        d.text((x-130, 840), label, fill='#172b3a', font=f(44))
    d.text((100, 970), footer, fill='#334155', font=f(30))
    im.save(OUT / name)

image('revenue.png', 'Revenue comparison', [120, 80], ['Atlas', 'Boreal'], 'Synthetic test data | No investment recommendation')
image('profit.png', 'Profit after costs', [10, 30], ['Atlas', 'Boreal'], 'Revenue is not profit | Synthetic test data')
texts = [
    'The blue bar is the higher revenue result. Revenue alone does not tell us which product is more profitable.',
    'After costs, the orange product earns more profit. The unit on this chart is millions of dollars.',
]
durations = []
for i, (text, img) in enumerate(zip(texts, ('revenue.png', 'profit.png'))):
    (OUT / f'speech_{i}.txt').write_text(text)
    call(['ffmpeg', '-y', '-f', 'lavfi', '-i', f'flite=textfile={OUT / f"speech_{i}.txt"}:voice=slt',
          '-ar', '24000', '-af', 'apad=pad_dur=0.6', str(OUT / f'audio_{i}.wav'), '-hide_banner', '-loglevel', 'error'])
    probe = json.loads(subprocess.check_output(['ffprobe', '-v','error','-show_format','-of','json',str(OUT/f'audio_{i}.wav')]))
    dur = float(probe['format']['duration']);durations.append(dur)
    call(['ffmpeg','-y','-loop','1','-framerate','10','-i',str(OUT/img),'-i',str(OUT/f'audio_{i}.wav'),
          '-t',str(dur),'-c:v','libx264','-preset','ultrafast','-crf','20','-pix_fmt','yuv420p','-c:a','aac',
          str(OUT/f'part_{i}.mp4'),'-hide_banner','-loglevel','error'])
(OUT/'concat.txt').write_text("file 'part_0.mp4'\nfile 'part_1.mp4'\n")
call(['ffmpeg','-y','-f','concat','-safe','0','-i',str(OUT/'concat.txt'),'-c','copy',str(OUT/'chart_speech.mp4'),'-hide_banner','-loglevel','error'])
def tc(t):
    ms=round(t*1000);s,milli=divmod(ms,1000);m,s=divmod(s,60);h,m=divmod(m,60)
    return f'{h:02}:{m:02}:{s:02},{milli:03}'
# The second caption is deliberately wrong, to test independent evidence.
(OUT/'chart_speech.srt').write_text(f'1\n00:00:00,000 --> {tc(durations[0])}\nThe blue bar has higher revenue.\n\n2\n{tc(durations[0])} --> {tc(sum(durations))}\nThe blue product earns more profit after costs.\n')
(OUT/'ground_truth.json').write_text(json.dumps({'license':'Original synthetic test fixture, dedicated to CC0-1.0',
 'creation':'Pillow charts and installed FFmpeg/libflite synthetic voice; no external media',
 'durations':durations,'speech':texts,'charts':[{'Atlas_revenue':120,'Boreal_revenue':80},{'Atlas_profit':10,'Boreal_profit':30}],
 'deliberate_caption_error':'Second caption says blue earns more profit; original speech says orange, and chart shows Boreal 30 > Atlas 10'},indent=2))
print(OUT/'chart_speech.mp4')
