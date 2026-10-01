#!/usr/bin/env python3
"""Bounded launch handshake: never start ASR before supervisor PID metadata is durable."""
import argparse,json,os,sys,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--gate',required=True);p.add_argument('--job',required=True);p.add_argument('--reader',required=True);p.add_argument('reader_args',nargs=argparse.REMAINDER);a=p.parse_args()
stop=time.monotonic()+10;gate=Path(a.gate)
while not gate.exists():
    if time.monotonic()>stop:sys.exit(3)
    time.sleep(.05)
try:
    with gate.open() as f:g=json.load(f)
    if g.get('job_id')!=a.job:sys.exit(4)
except (OSError,ValueError):sys.exit(4)
args=a.reader_args[1:] if a.reader_args[:1]==['--'] else a.reader_args
os.execv(sys.executable,[sys.executable,a.reader,*args])
