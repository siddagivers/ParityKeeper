#!/usr/bin/env python3
"""TEST ONLY. Fake Mini MultiPar process used for automated tests. NOT real PAR2.
This file must never be used for production backups or recovery.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
mode = args[0]
root = Path(next(x[2:] for x in args if x.startswith('/d')))
opts = [x for x in args[1:] if x.startswith('/') and (x in ('/b','/uo','/fu') or x.startswith(('/rr', '/ss', '/rn', '/lc', '/d')))]
others = [x for x in args[1:] if x not in opts]
par = Path(others[0])
vol = par.with_name(par.stem + '.vol0+1.par2')
if mode == 'c':
    names = [n.replace('\\', '/') for n in Path(others[1]).read_text('utf-8').splitlines()]
    objects = {}
    for name in names:
        data = (root / name).read_bytes()
        objects[name] = base64.b64encode(data).decode('ascii')
    par.write_text(json.dumps(sorted(objects)), 'utf-8')
    vol.write_text(json.dumps(objects), 'utf-8')
    print('Creating 100%')
    print('Fake PAR created', len(objects))
    sys.exit(0)
if not par.exists() or not vol.exists():
    sys.exit(1)
objects = json.loads(vol.read_text('utf-8'))
missing = []
for name, encoded in objects.items():
    p = root / name
    original = base64.b64decode(encoded)
    if not p.exists() or p.read_bytes() != original:
        missing.append((name, original))
if mode == 'v':
    print('Verifying 100%')
    sys.exit(0 if not missing else 132)
if mode == 'r':
    print('Repairing 100%')
    for name, original in missing:
        p = root / name
        p.parent.mkdir(exist_ok=True, parents=True)
        if p.exists():
            i = 1
            while p.with_name(p.name + '.' + str(i)).exists():
                i += 1
            p.rename(p.with_name(p.name + '.' + str(i)))
        p.write_bytes(original)
    sys.exit(16 if missing else 0)
sys.exit(1)
