"""Explicitly download ONE CC BY 4.0 sample, with revision and byte-hash checks.

No dataset script is executed. If the public viewer changes revision, fail rather
than silently substituting a different sample. License/source: mandarin-source.json.
"""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
import httpx

ROOT = Path(__file__).resolve().parents[1]
data=json.loads((ROOT/'tests'/'mandarin-source.json').read_text())
out=ROOT/'fixtures'/'mandarin';out.mkdir(parents=True,exist_ok=True)
with httpx.Client(follow_redirects=True,timeout=60) as client:
    r=client.get(data['viewer_row_source_url']);r.raise_for_status()
    row=r.json()['rows'][0]['row']
    if row['raw_transcription']!=data['reference_transcript']:
        raise RuntimeError('Reference text changed; manual review required')
    audio=row['audio']
    if isinstance(audio,list):audio=audio[0]
    url=audio['src']
    if urlparse(url).hostname!='datasets-server.huggingface.co' or data['dataset_revision'] not in url:
        raise RuntimeError('Unverified asset host or dataset revision')
    r=client.get(url);r.raise_for_status();blob=r.content
    if hashlib.sha256(blob).hexdigest()!=data['audio_sha256']:
        raise RuntimeError('Audio SHA-256 mismatch')
    dest=out/(data['fixture_id']+'.wav');dest.write_bytes(blob)
(out/'reference.txt').write_text(data['reference_transcript']+'\n',encoding='utf-8')
(out/'ATTRIBUTION.txt').write_text(data['attribution']+'\n'+data['license']['url']+'\n')
print(dest)
