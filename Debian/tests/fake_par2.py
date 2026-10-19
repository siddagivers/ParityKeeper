#!/usr/bin/env python3
"""TEST ONLY: not a real PAR2 encoder. Simulates Linux par2cmdline."""
import base64
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
mode = args[0]
base = Path(next((x[2:] for x in args if x.startswith('-B')), '.'))
args = [x for x in args[1:] if not x.startswith(('-B','-t','-T','-s','-c','-r')) and x != '--']
index = Path(args[0])
vol = index.with_name(index.stem + '.vol00+1.par2')
if mode == 'c':
    objects = {}
    for name in args[1:]:
        source = base / name
        objects[name] = base64.b64encode(source.read_bytes()).decode('ascii')
    index.write_text(json.dumps(sorted(objects)), encoding='utf-8')
    vol.write_text(json.dumps(objects), encoding='utf-8')
    print('Creating 100%')
    sys.exit(0)
if not index.exists() or not vol.exists():
    sys.exit(4)
objects = json.loads(vol.read_text(encoding='utf-8'))
broken = []
for name, encoded in objects.items():
    source = base / name
    data = base64.b64decode(encoded)
    if not source.is_file() or source.read_bytes() != data:
        broken.append((name,data))
if mode == 'v':
    print('Verifying 100%')
    sys.exit(1 if broken else 0)
if mode == 'r':
    print('Repairing 100%')
    for name, data in broken:
        target = base / name
        target.parent.mkdir(exist_ok=True, parents=True)
        if target.is_file():
            i = 1
            while target.with_name(target.name + '.' + str(i)).exists():
                i += 1
            target.rename(target.with_name(target.name + '.' + str(i)))
        target.write_bytes(data)
    sys.exit(0)
sys.exit(3)
