"""ParityKeeper Debian backend, using packaged par2cmdline for recovery.
Only standard Python library; file integrity is SHA256+CRC32.
"""
from __future__ import annotations

import collections
import functools
from contextlib import contextmanager
import dataclasses
import datetime as dt
import hashlib
import json
import math
import progress as progresslib
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zlib
from typing import Callable, Iterable

META = '.paritykeeper'
CHUNK = 8 * 1024 * 1024
MIB = 1024 * 1024


class Cancelled(Exception):
    pass


class SafetyError(Exception):
    pass


@dataclasses.dataclass
class Item:
    kind: str
    path: str
    size: int = 0
    group: str = ''
    detail: str = ''
    selected: bool = True


def fmt_size(n: int) -> str:
    n = float(n)
    for u in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or u == 'TiB':
            return f'{n:.1f} {u}'
        n /= 1024
    return str(n)


def normalize(path: str) -> str:
    return path.replace('\\', '/')


def file_path(root: Path, rel: str, *, allow_metadata: bool = False) -> Path:
    """Only regular relative archive paths, no junction/symlink traversal.

    Test with path components, not a string prefix: /archive-other must never
    be confused with /archive. A hostile or corrupt inventory must fail closed.
    """
    if (not isinstance(rel, str) or not rel or '\x00' in rel or '\\' in rel or
            rel.startswith('/') or any(x in ('', '.', '..') for x in rel.split('/')) or
            (os.name == 'nt' and ':' in rel)):
        raise SafetyError(f'Unsafe inventory path: {rel!r}')
    if str(PurePosixPath(rel)) != rel:
        raise SafetyError(f'Non-canonical inventory path: {rel!r}')
    root_resolved = root.resolve()
    if not allow_metadata and (rel.split('/')[0].lower() == META.lower() if os.name == 'nt' else rel.split('/')[0] == META):
        raise SafetyError('Archive metadata folder is not a protected source path')
    candidate = root_resolved
    for component in rel.split('/'):
        candidate = candidate / component
        if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
            raise SafetyError(f'Symlink/junction in protected path: {rel!r}')
    if not candidate.resolve(strict=False).is_relative_to(root_resolved):
        raise SafetyError(f'Protected path escapes selected archive: {rel!r}')
    return candidate


def par_index_file(root: Path, rel: str) -> Path:
    """Validate internal PAR index references; never trust database strings blindly."""
    components = rel.split('/')
    if len(components) != 4 or components[0] != META or components[1] != 'par2' or components[3] != 'data.par2':
        raise SafetyError(f'Unsafe recovery index path in inventory: {rel!r}')
    try:
        uuid.UUID(components[2])
    except (ValueError, AttributeError) as exc:
        raise SafetyError(f'Unexpected recovery set folder in inventory: {rel!r}') from exc
    return file_path(root, rel, allow_metadata=True)


def open_db(root: Path, *, initialize: bool = True) -> sqlite3.Connection:
    meta = root / META
    if initialize:
        meta.mkdir(exist_ok=True)
        (meta / 'par2').mkdir(exist_ok=True)
        (meta / 'snapshots').mkdir(exist_ok=True)
        (meta / 'quarantine').mkdir(exist_ok=True)
    else:
        if not (meta / 'inventory.sqlite3').is_file():
            raise FileNotFoundError('No ParityKeeper inventory in selected folder')
    conn = sqlite3.connect(meta / 'inventory.sqlite3', timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=60000')
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS sets(
      id TEXT PRIMARY KEY,
      index_rel TEXT NOT NULL,
      created TEXT NOT NULL,
      kind TEXT NOT NULL,
      percent INTEGER NOT NULL,
      file_count INTEGER NOT NULL,
      total_bytes INTEGER NOT NULL,
      state TEXT NOT NULL DEFAULT 'active'
    );
    CREATE TABLE IF NOT EXISTS files(
      path TEXT PRIMARY KEY COLLATE BINARY,
      size INTEGER NOT NULL,
      mtime_ns INTEGER NOT NULL,
      sha256 TEXT NOT NULL,
      crc32 TEXT NOT NULL,
      set_id TEXT NOT NULL REFERENCES sets(id),
      verified_at TEXT
    );
    CREATE INDEX IF NOT EXISTS files_by_set ON files(set_id);
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      when_utc TEXT NOT NULL,
      action TEXT NOT NULL,
      description TEXT NOT NULL
    );
    ''')
    # v1.1 databases have NOCASE primary keys. Preserve an independent SQLite
    # backup before atomically changing the primary-key collation to BINARY.
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='files'").fetchone()[0]
    if 'COLLATE NOCASE' in schema.upper():
        if not initialize:
            conn.close()
            raise SafetyError('Older case-insensitive inventory detected. Open the archive and run Scan '
                              'to migrate it safely before comparing inventories.')
        snapshot_dir = meta / 'snapshots'
        snapshot_dir.mkdir(exist_ok=True)
        backup_path = snapshot_dir / f'inventory_before_case_migration_{uuid.uuid4().hex}.sqlite3'
        try:
            with sqlite3.connect(backup_path) as backup:
                conn.backup(backup)
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE files_case_sensitive(
              path TEXT PRIMARY KEY COLLATE BINARY,
              size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
              sha256 TEXT NOT NULL, crc32 TEXT NOT NULL,
              set_id TEXT NOT NULL REFERENCES sets(id), verified_at TEXT)''')
            conn.execute('INSERT INTO files_case_sensitive SELECT * FROM files')
            conn.execute('DROP TABLE files')
            conn.execute('ALTER TABLE files_case_sensitive RENAME TO files')
            conn.execute('CREATE INDEX files_by_set ON files(set_id)')
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            conn.close()
            raise SafetyError(f'Inventory path migration failed; untouched backup: {backup_path}; '
                              f'error: {exc}') from exc
    return conn


@contextmanager
def db(root: Path, *, initialize: bool = True):
    """Unlike sqlite3.Connection.__exit__, this closes the connection too."""
    conn = open_db(root, initialize=initialize)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def archive_lock(root: Path):
    """Cross-process exclusive access; never run two protection jobs on a disk."""
    directory = root / META
    directory.mkdir(exist_ok=True)
    with (directory / 'operation.lock').open('a+b') as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b'0')
            fh.flush()
        fh.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SafetyError('Another ParityKeeper process is using this archive. Close it first.') from exc
        try:
            yield
        finally:
            fh.seek(0)
            if os.name == 'nt':
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def exclusive_archive(fn):
    @functools.wraps(fn)
    def wrapped(root, *args, **kwargs):
        with archive_lock(root):
            return fn(root, *args, **kwargs)
    return wrapped


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def record_event(conn, action, description):
    conn.execute('INSERT INTO events(when_utc,action,description) VALUES(?,?,?)',
                 (timestamp(), action, description[:1000]))


def check_cancel(cancel: threading.Event):
    if cancel.is_set():
        raise Cancelled('Operation canceled by user')


def iterate_files(root: Path, cancel: threading.Event, emit: Callable) -> Iterable[tuple[str, int, int]]:
    stack = [root]
    errors = []
    while stack:
        check_cancel(cancel)
        here = stack.pop()
        try:
            with os.scandir(here) as scandir:
                for ent in scandir:
                    check_cancel(cancel)
                    if ent.name == META and here == root:
                        continue
                    try:
                        if ent.is_symlink() or (hasattr(ent, "is_junction") and ent.is_junction()):
                            continue  # no junction/links, which can escape the root
                        if ent.is_dir(follow_symlinks=False):
                            stack.append(Path(ent.path))
                        elif ent.is_file(follow_symlinks=False):
                            st = ent.stat(follow_symlinks=False)
                            rel = normalize(os.path.relpath(ent.path, root))
                            yield rel, st.st_size, st.st_mtime_ns
                    except OSError as exc:
                        errors.append(f'{ent.path}: {exc}')
        except OSError as exc:
            errors.append(f'{here}: {exc}')
    if errors:
        for error in errors[:20]:
            emit('log', f'SCAN ERROR: {error}')
        raise SafetyError(f'{len(errors)} folder/file access error(s). Scan incomplete; no changes/deletions allowed.')


@exclusive_archive
def scan_changes(root: Path, cancel: threading.Event, emit: Callable) -> list[Item]:
    """Metadata-only scan. Intentionally NEVER trusts mtimes as integrity proof."""
    with db(root) as conn:
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.execute('CREATE TEMP TABLE seen(path TEXT PRIMARY KEY COLLATE BINARY, size INTEGER, mtime_ns INTEGER)')
        batch = []
        count = 0
        for rel, size, mtime_ns in iterate_files(root, cancel, emit):
            batch.append((rel, size, mtime_ns))
            count += 1
            if len(batch) >= 1000:
                try:
                    conn.executemany('INSERT INTO seen VALUES(?,?,?)', batch)
                except sqlite3.IntegrityError as exc:
                    raise SafetyError('Duplicate path during scan (possibly case/Unicode collision). '
                                      'No inventory changes were applied.') from exc
                batch.clear()
            if count % 2000 == 0:
                emit('status', f'Scanned {count:,} files (metadata only)')
        if batch:
            try:
                conn.executemany('INSERT INTO seen VALUES(?,?,?)', batch)
            except sqlite3.IntegrityError as exc:
                raise SafetyError('Duplicate path during scan. No inventory changes were applied.') from exc
        check_cancel(cancel)
        changes = []
        for row in conn.execute('''SELECT s.path, s.size FROM seen s LEFT JOIN files f ON f.path=s.path
                    WHERE f.path IS NULL ORDER BY s.path'''):
            changes.append(Item('NEW', row['path'], row['size']))
        for row in conn.execute('''SELECT s.path, s.size, f.set_id FROM seen s JOIN files f ON f.path=s.path
                    WHERE s.size<>f.size OR s.mtime_ns<>f.mtime_ns ORDER BY s.path'''):
            changes.append(Item('CHANGED', row['path'], row['size'], row['set_id'],
                                'Not automatically accepted: may be corruption', selected=False))
        for row in conn.execute('''SELECT f.path, f.size, f.set_id FROM files f LEFT JOIN seen s ON s.path=f.path
                    WHERE s.path IS NULL ORDER BY f.path'''):
            changes.append(Item('MISSING', row['path'], row['size'], row['set_id'],
                                'Confirm intentional deletion; otherwise keep for repair', selected=False))
        emit('status', f'Quick scan complete: {count:,} files; {len(changes):,} differences')
        return changes


def digest(path: Path, cancel: threading.Event, on_bytes=None) -> tuple[int, int, str, str]:
    """SHA-256 and CRC32 in the same streaming read; detect writes during hashing."""
    before = path.stat()
    if not path.is_file():
        raise SafetyError(f'Not a regular file: {path}')
    sha = hashlib.sha256()
    crc = 0
    n = 0
    with path.open('rb', buffering=0) as stream:
        while True:
            check_cancel(cancel)
            b = stream.read(CHUNK)
            if not b:
                break
            sha.update(b)
            crc = zlib.crc32(b, crc)
            n += len(b)
            if on_bytes is not None:
                on_bytes(len(b))
    after = path.stat()
    if (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or n != before.st_size):
        raise SafetyError(f'File changed during hashing: {path}')
    return after.st_size, after.st_mtime_ns, sha.hexdigest(), f'{crc & 0xffffffff:08X}'


def thread_count(kind: str, opts: dict | None) -> int:
    """Use the newest slider value at launch of each par2 subprocess."""
    opts = opts or {}
    provider = opts.get('thread_provider')
    count = provider(kind) if callable(provider) else opts.get(
        'small_threads' if kind == 'small' else 'large_threads', 4 if kind == 'small' else 2)
    count = int(count)
    if not (1 <= count <= 10):
        raise SafetyError('PAR2 CPU threads must be between 1 and 10')
    return count


def run_par(executable: str, args: list[str], cancel: threading.Event, emit: Callable,
            meter=None, phase: str = '', weight: int = 0, cwd: Path | None = None) -> int:
    """Runs Debian par2 directly and handles Stop button."""
    located = shutil.which(executable)
    if not located:
        raise SafetyError(f'PAR2 executable not found: {executable}. Install package: sudo apt install par2')
    cmd = [located] + args
    if meter is not None:
        meter.begin(phase or ('PAR2 ' + args[0]), weight)
    emit('log', 'PAR2: ' + ' '.join((c[:110] + '…' if len(c) > 110 else c) for c in args[:5]))
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=flags, cwd=cwd)
    chunks: queue.Queue = queue.Queue()

    def reader():
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                chunks.put(chunk)
        finally:
            chunks.put(None)

    worker = threading.Thread(target=reader, daemon=True)
    worker.start()
    buf = b''
    last_line = ''
    while proc.poll() is None:
        if meter is not None:
            meter.heartbeat()
        if cancel.is_set():
            proc.kill()
            proc.wait()
            raise Cancelled('PAR2 was canceled')
        try:
            c = chunks.get(timeout=0.2)
            if c:
                buf += c
                # Both carriage return and newline can indicate progress.
                parts = buf.replace(b'\r', b'\n').split(b'\n')
                buf = parts.pop()
                for part in parts:
                    s = part.decode('utf-8', 'replace').strip()
                    if s and s != last_line:
                        emit('log', s[:300])
                        if meter is not None:
                            fraction = progresslib.parse_percentage(s)
                            if fraction is not None:
                                meter.fraction(fraction)
                        last_line = s
        except queue.Empty:
            pass
    worker.join(timeout=3)
    if proc.stdout is not None:
        proc.stdout.close()
    while not chunks.empty():
        c = chunks.get_nowait()
        if c:
            buf += c
    if buf:
        for line in buf.replace(b'\r', b'\n').split(b'\n')[-8:]:
            if line.strip():
                emit('log', line.decode('utf-8', 'replace')[:300])
    emit('log', f'PAR2 exit code: {proc.returncode}')
    if meter is not None:
        meter.finish()
    return proc.returncode


def active_set_members(conn, sid: str) -> list[sqlite3.Row]:
    return conn.execute('SELECT * FROM files WHERE set_id=? ORDER BY path', (sid,)).fetchall()


def index_path(root: Path, conn, sid: str) -> Path:
    row = conn.execute('SELECT index_rel FROM sets WHERE id=? AND state=?', (sid, 'active')).fetchone()
    if row is None:
        raise SafetyError(f'Unknown or inactive PAR set: {sid}')
    return par_index_file(root, row['index_rel'])


def group_new_items(rows: list[tuple], opts: dict) -> list[tuple[str, list[tuple]]]:
    """Group small files from same directory in bounded, append-only groups."""
    small_cutoff = int(opts['small_mb']) * MIB
    max_group = int(opts['group_mb']) * MIB
    max_files = int(opts['max_files'])
    grouped = collections.defaultdict(list)
    output = []
    for row in rows:
        rel, sz = row[0], row[1]
        if sz >= small_cutoff:
            output.append(('large', [row]))
        else:
            parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
            grouped[parent].append(row)
    for _, sub in sorted(grouped.items()):
        sub.sort(key=lambda x: x[0])
        batch, length = [], 0
        for row in sub:
            sz = row[1]
            if batch and (len(batch) >= max_files or length + sz > max_group):
                output.append(('small', batch))
                batch, length = [], 0
            batch.append(row)
            length += sz
        if batch:
            output.append(('small', batch))
    return output


def par_options(kind: str, rows: list[tuple], opts: dict) -> tuple[list[str], int]:
    pct = int(opts['small_pct'] if kind == 'small' else opts['large_pct'])
    if kind != 'small':
        return [f'-r{pct}'], pct
    # Give groups enough PAR blocks to recover the largest ONE missing small file.
    # Still only guarantees recovery while no other files/blocks are damaged.
    total = sum(r[1] for r in rows)
    largest = max(r[1] for r in rows)
    slice_size = max(64 * 1024, 1 << max(0, math.ceil(math.log2(max(1, math.ceil(total / 2048))))))
    slices = sum(math.ceil(r[1] / slice_size) for r in rows)
    recovery = max(1, math.ceil(slices * pct / 100), math.ceil(largest / slice_size))
    return [f'-s{slice_size}', f'-c{recovery}'], pct


def check_space(root: Path, total: int, kind: str, opts: dict, emit: Callable, largest: int = 0):
    pct = int(opts['small_pct'] if kind == 'small' else opts['large_pct'])
    # Early warning; actual cost depends on PAR2 slice rounding and file count.
    expected = int(max(total * pct / 100, largest if kind == "small" else 0) * 1.25 + 8 * MIB)
    free = shutil.disk_usage(root).free
    if free < expected:
        raise SafetyError(f'Insufficient free space for recovery data: estimated {fmt_size(expected)}, '
                          f'available {fmt_size(free)} on {root}.')
    if free < expected * 2:
        emit('log', f'LOW SPACE: available {fmt_size(free)}, rough PAR requirement {fmt_size(expected)}')


def make_set(root: Path, conn, kind: str, incoming: list[tuple], old_sid: str | None,
             removed: set[str], opts: dict, exe: str, cancel: threading.Event, emit: Callable, meter=None):
    """One fully verified PAR2 set, then atomic SQLite switch, then obsolete-set cleanup.

    incoming are tuples (path,size,mtime_ns,sha256,crc32).
    """
    check_cancel(cancel)
    old_members = active_set_members(conn, old_sid) if old_sid else []
    updates = {r[0]: r for r in incoming}
    new_rows = []
    for old in old_members:
        rel = old['path']
        if rel in removed:
            continue
        if rel in updates:
            new_rows.append(updates.pop(rel))
        else:
            # CRITICAL: Do not create new parity over a silently corrupted survivor.
            emit('status', f'Checking unaffected group member: {rel}')
            if meter is not None:
                meter.begin('Hashing protected group member', old['size'])
            try:
                candidate = digest(file_path(root, rel), cancel,
                                   meter.step if meter is not None else None)
            except OSError as exc:
                raise SafetyError(f'Unchanged member is missing/unreadable: {rel}: {exc}') from exc
            if meter is not None:
                meter.finish()
            if candidate[2] != old['sha256'] or candidate[3] != old['crc32']:
                raise SafetyError(f'Existing protected member differs from baseline: {rel}. '
                                  'Run CRC check/repair before updating this PAR set.')
            new_rows.append((rel, *candidate))
    new_rows.extend(updates.values())
    new_rows.sort(key=lambda x: x[0])
    # Preserve parity for already tracked files until switch has succeeded.
    new_sid = str(uuid.uuid4())
    groupdir = root / META / 'par2' / new_sid
    index = groupdir / 'data.par2'
    groupdir.mkdir(parents=True, exist_ok=False)
    committed = False
    try:
        if new_rows:
            check_space(root, sum(x[1] for x in new_rows), kind, opts, emit, max(x[1] for x in new_rows))
            opts_par, pct = par_options(kind, new_rows, opts)
            # Pass relative POSIX paths with -B<root>. Debian stable's par2 0.8.1
            # does not reliably support the newer @<file> list syntax, so we
            # use argument vectors with no shell (Unicode and spaces are safe).
            filenames = []
            for rel, *_ in new_rows:
                if '\n' in rel or '\r' in rel:
                    raise SafetyError(f'Unsupported newline in filename: {rel!r}')
                filenames.append(rel)
            arg_bytes = sum(len(x.encode('utf-8')) + 1 for x in filenames)
            if arg_bytes > 1000000:
                raise SafetyError('PAR2 filename list too long. Lower Max files/group in Settings.')
            emit('status', f'Creating {kind} PAR set: {len(new_rows)} files ({fmt_size(sum(x[1] for x in new_rows))})')
            threads = thread_count(kind, opts)
            args = ['c', *opts_par, f'-t{threads}', f'-T{min(threads, 2)}',
                    f'-B{root}', str(index), '--', *filenames]
            emit('log', f'Create: {kind} set with {threads} PAR2 CPU threads')
            code = run_par(exe, args, cancel, emit, meter, 'Creating PAR2 recovery data',
                           sum(x[1] for x in new_rows), cwd=root)
            if code != 0 or not index.is_file():
                raise SafetyError(f'PAR2 CREATE failed (exit {code}); no inventory changes made')
            if sum(x[1] for x in new_rows) > 0 and not list(groupdir.glob('*.vol*.par2')):
                raise SafetyError('PAR2 created no recovery volumes. Refusing to mark files protected.')
            for rel, size, mtime, sha, crc in new_rows:
                st = file_path(root, rel).stat()
                if st.st_size != size or st.st_mtime_ns != mtime:
                    raise SafetyError(f'Source changed while PAR2 was built: {rel}; no changes committed')
            emit('status', 'Verifying newly-created PAR set')
            threads = thread_count(kind, opts)
            code = run_par(exe, ['v', f'-t{threads}', f'-T{min(threads, 2)}', f'-B{root}', str(index)],
                           cancel, emit, meter, 'Verifying new PAR2 recovery data',
                           sum(x[1] for x in new_rows), cwd=root)
            if code != 0:
                raise SafetyError(f'New PAR set did not verify (exit {code}); no inventory changes made')
            # Metadata (size/mtime) alone is insufficient: a write could restore
            # its original timestamp while MultiPar/PAR2 was running.
            emit('status', 'Rechecking SHA256 and CRC32 before approving the new baseline')
            for rel, size, _mtime, sha, crc in new_rows:
                check_cancel(cancel)
                again = digest(file_path(root, rel), cancel,
                               meter.step if meter is not None else None)
                if again[0] != size or again[2] != sha or again[3] != crc:
                    raise SafetyError(f'Source content changed during PAR creation: {rel}; '
                                      'existing baseline and recovery files preserved')
        else:
            pct = int(opts['small_pct'] if kind == 'small' else opts['large_pct'])
        with conn:
            if new_rows:
                conn.execute('INSERT INTO sets VALUES(?,?,?,?,?,?,?,?)',
                             (new_sid, normalize(str(index.relative_to(root))), timestamp(),
                              kind, pct, len(new_rows), sum(x[1] for x in new_rows), 'active'))
            if old_sid:
                conn.execute('DELETE FROM files WHERE set_id=?', (old_sid,))
                conn.execute("UPDATE sets SET state='retired' WHERE id=?", (old_sid,))
            if new_rows:
                conn.executemany('''INSERT INTO files(path,size,mtime_ns,sha256,crc32,set_id,verified_at)
                                  VALUES(?,?,?,?,?,?,?)''',
                                 [(rel, size, mtime, sha, crc, new_sid, timestamp())
                                  for rel, size, mtime, sha, crc in new_rows])
            record_event(conn, 'protect', f'{len(new_rows)} in new set {new_sid}; old set {old_sid or "none"}')
        committed = True
        if old_sid:
            old_folder = root / META / 'par2' / old_sid
            try:
                shutil.rmtree(old_folder)
            except OSError as exc:
                emit('log', f'Old retired PAR files kept (can remove later): {exc}')
        if not new_rows:
            shutil.rmtree(groupdir, ignore_errors=True)
        emit('log', f'COMMITTED: {len(new_rows)} protected files; removed {len(old_members) - len(new_rows) if old_sid else 0} old entries')
    except BaseException:
        if not committed:
            shutil.rmtree(groupdir, ignore_errors=True)
        raise


@exclusive_archive
def apply_changes(root: Path, items: list[Item], opts: dict, exe: str,
                  cancel: threading.Event, emit: Callable) -> dict:
    additions = [x for x in items if x.kind in ('NEW', 'CHANGED') and x.selected]
    deletions = [x for x in items if x.kind == 'MISSING' and x.selected]
    if not additions and not deletions:
        return {'done': 0}
    to_remove = {x.path for x in deletions}
    with db(root) as conn:
        affected: dict[str, list[Item]] = {}
        fresh: list[tuple[str, int]] = []
        for item in additions:
            check_cancel(cancel)
            old = conn.execute('SELECT set_id FROM files WHERE path=?', (item.path,)).fetchone()
            if item.kind == 'CHANGED' and not old:
                raise SafetyError(f'Item no longer tracked: {item.path}. Rescan.')
            if item.kind == 'NEW' and old:
                raise SafetyError(f'Item already tracked: {item.path}. Rescan.')
            # A 7 TB first import must be incremental. Hash each small PAR group
            # only when ready to build it; never hash the ENTIRE drive up front.
            if old:
                affected.setdefault(old['set_id'], []).append(item)
            else:
                fresh.append((item.path, item.size))
        for item in deletions:
            old = conn.execute('SELECT set_id FROM files WHERE path=?', (item.path,)).fetchone()
            if not old:
                raise SafetyError(f'Deleted item no longer tracked: {item.path}. Rescan.')
            if file_path(root, item.path).exists():
                raise SafetyError(f'File has reappeared; deletion not applied: {item.path}')
            affected.setdefault(old['set_id'], [])
        # Estimate ~one full read for hashing and two for PAR2 create+verify.
        # An existing set is re-read to validate unchanged members before updating.
        full_io = sum(x.size for x in additions)
        for sid, changed in affected.items():
            members = active_set_members(conn, sid)
            incoming = {x.path for x in changed}
            full_io += sum(m['size'] for m in members if m['path'] not in incoming
                           and m['path'] not in to_remove)
            resulting = (sum(m['size'] for m in members if m['path'] not in incoming
                             and m['path'] not in to_remove)
                         + sum(x.size for x in changed))
            full_io += 3 * resulting
        full_io += 3 * sum(x.size for x in additions if x.kind == 'NEW')
        meter = progresslib.WorkMeter(full_io, emit, 'Updating PAR2 protection')
        completed = 0
        checkpoint = root / META / 'last_apply_state.json'
        def write_checkpoint(state):
            # Atomic replace; diagnostic only, NEVER replay approvals automatically.
            tmp = checkpoint.with_name(checkpoint.name + '.tmp')
            try:
                tmp.write_text(json.dumps({'state': state, 'groups_committed': completed,
                             'updated_utc': timestamp(), 'requires_new_scan': True}, indent=2), encoding='utf-8')
                os.replace(tmp, checkpoint)
            except OSError as exc:
                emit('log', f'WARNING: could not save diagnostic checkpoint: {exc}')
        write_checkpoint('running')
        try:
            for sid, changed in affected.items():
                check_cancel(cancel)
                oldset = conn.execute('SELECT kind FROM sets WHERE id=?', (sid,)).fetchone()
                if oldset is None:
                    raise SafetyError(f'Old PAR group missing from inventory: {sid}')
                hashed = []
                for item in changed:
                    emit('status', f'Hashing approved modification: {item.path}')
                    meter.begin('Hashing approved modification', item.size)
                    hashed.append((item.path, *digest(file_path(root, item.path), cancel, meter.step)))
                    meter.finish()
                make_set(root, conn, oldset['kind'], hashed, sid, to_remove,
                         opts, exe, cancel, emit, meter)
                completed += 1
                write_checkpoint('running')
            for kind, batch in group_new_items(fresh, opts):
                check_cancel(cancel)
                hashed = []
                for rel, _old_size in batch:
                    emit('status', f'Hashing new file: {rel}')
                    if conn.execute('SELECT 1 FROM files WHERE path=?', (rel,)).fetchone():
                        raise SafetyError(f'Already protected (rescan required): {rel}')
                    meter.begin('Hashing new file', _old_size)
                    hashed.append((rel, *digest(file_path(root, rel), cancel, meter.step)))
                    meter.finish()
                # A file may have grown since Scan; prevent oversized small groups.
                if kind == 'small' and (sum(x[1] for x in hashed) > int(opts['group_mb']) * MIB
                                        or any(x[1] >= int(opts['small_mb']) * MIB for x in hashed)):
                    raise SafetyError('Files grew since Scan. Rescan before applying changes.')
                make_set(root, conn, kind, hashed, None, set(), opts, exe, cancel, emit, meter)
                completed += 1
                write_checkpoint('running')
        except BaseException:
            write_checkpoint('interrupted; scan again to continue safely')
            raise
        write_checkpoint('completed')
        snapshot_db(root, conn, emit)
        meter.done()
        return {'done': completed, 'files': len(additions), 'deleted': len(deletions)}


def snapshot_db(root: Path, conn: sqlite3.Connection, emit: Callable):
    """3 rotating SQLite snapshots; snapshots stay on this drive (not offsite backups)."""
    folder = root / META / 'snapshots'
    folder.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    target = folder / f'inventory_{stamp}.sqlite3'
    with sqlite3.connect(target) as backup_conn:
        conn.backup(backup_conn, pages=256, sleep=0.1)
    for old in sorted(folder.glob('inventory_*.sqlite3'), reverse=True)[3:]:
        try:
            old.unlink()
        except OSError as ex:
            emit('log', f'Snapshot cleanup: {ex}')
    emit('log', 'Inventory snapshot saved')


@exclusive_archive
def crc_check(root: Path, cancel: threading.Event, emit: Callable) -> list[Item]:
    issues: list[Item] = []
    with db(root) as conn:
        n = conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        bytes_to_check = conn.execute('SELECT COALESCE(SUM(size),0) FROM files').fetchone()[0]
        meter = progresslib.WorkMeter(bytes_to_check, emit, 'Checksum integrity check')
        ok = 0
        total = 0
        emit('log', f'Full integrity check for {n:,} protected files (reads every byte; no timestamp shortcuts)')
        for row in conn.execute('SELECT * FROM files ORDER BY path'):
            check_cancel(cancel)
            total += 1
            rel = row['path']
            meter.begin(f'Hashing {rel}', row['size'])
            try:
                size, mtime, sha, crc = digest(file_path(root, rel), cancel, meter.step)
                if sha != row['sha256'] or crc != row['crc32'] or size != row['size']:
                    issue = Item('CORRUPT', rel, size, row['set_id'],
                                 f'Expected SHA256 {row["sha256"][:16]}…; got {sha[:16]}…', True)
                    issues.append(issue)
                    emit('issue', issue)
                else:
                    ok += 1
                    # Content identical; harmless timestamp metadata update is safe.
                    conn.execute('UPDATE files SET size=?,mtime_ns=?,verified_at=? WHERE path=?',
                                 (size, mtime, timestamp(), rel))
            except (OSError, SafetyError) as exc:
                issue = Item('UNREADABLE', rel, row['size'], row['set_id'], str(exc), True)
                if isinstance(exc, FileNotFoundError):
                    issue.kind = 'MISSING'
                issues.append(issue)
                emit('issue', issue)
            meter.finish()
            if total % 25 == 0 or total == n:
                emit('status', f'Integrity check: {total:,}/{n:,}; good {ok:,}; issues {len(issues):,}')
                if total % 250 == 0:
                    conn.commit()
        conn.commit()
        for rec in conn.execute("SELECT * FROM sets WHERE state='active'"):
            idx = par_index_file(root, rec['index_rel'])
            if not idx.exists() or not list(idx.parent.glob('*.vol*.par2')):
                issue = Item('PAR_MISSING', f'[PAR set {rec["id"]}]', rec['total_bytes'], rec['id'],
                             'Index or recovery-volume files missing. Check/rebuild manually.', False)
                issues.append(issue)
                emit('issue', issue)
        record_event(conn, 'checksum', f'{total} checked; {len(issues)} issues')
        meter.done()
        emit('log', f'Checksum check finished: {ok:,} good files, {len(issues):,} issues')
        return issues


@exclusive_archive
def verify_all_par(root: Path, exe: str, cancel: threading.Event, emit: Callable, threads: dict | None = None) -> list[Item]:
    issues = []
    with db(root) as conn:
        sets = conn.execute("SELECT * FROM sets WHERE state='active' ORDER BY created").fetchall()
        meter = progresslib.WorkMeter(sum(r['total_bytes'] for r in sets), emit, 'PAR2 recovery verification')
        for i, row in enumerate(sets, 1):
            check_cancel(cancel)
            emit('status', f'Checking PAR recovery set {i}/{len(sets)}')
            index = par_index_file(root, row['index_rel'])
            if not index.exists():
                issue = Item('PAR_MISSING', f'[PAR set {row["id"]}]', group=row['id'],
                             detail='Recovery index is missing', selected=False)
                issues.append(issue)
                emit('issue', issue)
                meter.begin('Missing recovery index', row['total_bytes'])
                meter.finish()
                continue
            nthreads = thread_count(row['kind'], threads)
            code = run_par(exe, ['v', f'-t{nthreads}', f'-T{min(nthreads, 2)}', f'-B{root}', str(index)],
                           cancel, emit, meter, f'Verify PAR set {i}/{len(sets)}', row['total_bytes'], cwd=root)
            if code != 0:
                issue = Item('PAR_VERIFY', f'[PAR set {row["id"]}]', group=row['id'],
                             detail=f'PAR2 returned {code}; sources/parity need attention', selected=False)
                issues.append(issue)
                emit('issue', issue)
        meter.done()
    return issues


def quarantine_new_backups(root: Path, before: dict[str, set[str]], emit: Callable) -> list[str]:
    """Move newly created damaged-original copies into unique quarantine paths.

    Return failures. A quarantine error must never be mistaken for full success.
    """
    destroot = root / META / 'quarantine' / (dt.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
    failures = []
    for rel, known in before.items():
        source = file_path(root, rel)
        base = source.name
        try:
            for sibling in source.parent.iterdir():
                if not sibling.is_file() or not sibling.name.startswith(base + '.'):
                    continue
                suffix = sibling.name[len(base) + 1:]
                if not suffix.isdecimal() or sibling.name in known:
                    continue
                target = destroot.joinpath(*rel.split('/')).with_name(base + '.' + suffix)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = target.with_name(target.name + '_' + uuid.uuid4().hex[:8])
                shutil.move(str(sibling), str(target))
                emit('log', f'Damaged original preserved in quarantine: {target}')
        except OSError as exc:
            failures.append(f'{rel}: {exc}')
            emit('log', f'QUARANTINE ERROR: {rel}: {exc} — inspect archive manually')
    return failures


def list_orphan_par_dirs(root: Path) -> list[Path]:
    """Report unknown PAR2 directories; NEVER remove automatically on startup.

    Uncommitted directories can be useful for recovery after power failure.
    """
    par = root / META / 'par2'
    if not par.is_dir() or not (root / META / 'inventory.sqlite3').is_file():
        return []
    with db(root, initialize=False) as conn:
        ids = {r[0] for r in conn.execute('SELECT id FROM sets')}
    return sorted(p for p in par.iterdir() if p.is_dir() and p.name not in ids)


def linux_par2_verify_repairable(code: int) -> bool:
    """Debian par2cmdline returns enums: 0 OK, 1 repairable, 2+ failure."""
    return code in (0, 1)


def linux_par2_repair_success(code: int) -> bool:
    return code == 0


@exclusive_archive
def repair_selected(root: Path, issues: list[Item], exe: str,
                    cancel: threading.Event, emit: Callable, threads: dict | None = None) -> list[Item]:
    """Repairs complete sets, only when all known corrupt/missing members are selected."""
    selected = [x for x in issues if x.selected and x.kind in ('CORRUPT', 'MISSING', 'UNREADABLE')]
    if not selected:
        return issues
    groups = {x.group for x in selected}
    for sid in groups:
        unselected = [x.path for x in issues if x.group == sid and not x.selected
                      and x.kind in ('CORRUPT', 'MISSING', 'UNREADABLE')]
        if unselected:
            raise SafetyError('A PAR set includes unchecked bad files. Select ALL bad files in the same '
                              f'group, or skip the whole group. First unchecked file: {unselected[0]}')
    repaired = set()
    with db(root) as conn:
        sizes = {sid: conn.execute('SELECT total_bytes FROM sets WHERE id=?', (sid,)).fetchone()['total_bytes']
                 for sid in groups}
        meter = progresslib.WorkMeter(sum(3 * size for size in sizes.values()), emit, 'PAR2 repair')
        for sid in sorted(groups):
            check_cancel(cancel)
            members = active_set_members(conn, sid)
            index = index_path(root, conn, sid)
            if not index.is_file():
                emit('log', f'Cannot repair set {sid}: index PAR2 missing')
                meter.begin('Missing recovery index', sizes[sid] * 3)
                meter.finish()
                continue
            set_kind = conn.execute('SELECT kind FROM sets WHERE id=?', (sid,)).fetchone()['kind']
            nthreads = thread_count(set_kind, threads)
            emit('status', f'Verifying PAR set {sid} before repair ({nthreads} CPU threads)')
            code = run_par(exe, ['v', f'-t{nthreads}', f'-T{min(nthreads, 2)}', f'-B{root}', str(index)],
                           cancel, emit, meter, 'Pre-repair PAR verification', sizes[sid], cwd=root)
            # par2cmdline EXIT CODES ARE NOT A BITMASK:
            # 0 = healthy, 1 = broken but repairable, 2 = not repairable,
            # 3..8 = command, parity, IO or memory errors.
            if not linux_par2_verify_repairable(code):
                emit('log', f'PAR set {sid} not repairable (par2 verify exit {code}); skipped')
                meter.begin('Skipped unrecoverable repair/rehash', 2 * sizes[sid])
                meter.finish()
                continue
            # Without -p, par2cmdline keeps its backups of damaged originals.
            # Afterwards put any newly created *.1, *.2 etc into quarantine.
            backups_before = {}
            for r in members:
                p = file_path(root, r['path'])
                try:
                    backups_before[r['path']] = {s.name for s in p.parent.iterdir()
                                                   if s.is_file() and s.name.startswith(p.name + '.')}
                except OSError as exc:
                    raise SafetyError(f'Cannot inspect damaged-original backups for {r["path"]}; '
                                      f'repair stopped: {exc}') from exc
            emit('status', f'Repairing PAR set {sid}; preserving damaged originals when produced by par2')
            nthreads = thread_count(set_kind, threads)
            code = run_par(exe, ['r', f'-t{nthreads}', f'-T{min(nthreads, 2)}', f'-B{root}', str(index)],
                           cancel, emit, meter, 'Repairing selected PAR group', sizes[sid], cwd=root)
            quarantine_errors = quarantine_new_backups(root, backups_before, emit)
            if not linux_par2_repair_success(code):
                emit('log', f'Repair command failed/incomplete (par2 exit {code}); verify files manually')
                meter.begin('Skipped post-repair rehash', sizes[sid])
                meter.finish()
                continue
            all_good = True
            meter.begin('SHA256 check of repaired PAR group', sizes[sid])
            for r in members:
                check_cancel(cancel)
                try:
                    size, mt, sha, crc = digest(file_path(root, r['path']), cancel, meter.step)
                    if size != r['size'] or sha != r['sha256'] or crc != r['crc32']:
                        all_good = False
                        emit('log', f'POST-REPAIR HASH MISMATCH: {r["path"]}')
                    else:
                        conn.execute('UPDATE files SET mtime_ns=?,verified_at=? WHERE path=?',
                                     (mt, timestamp(), r['path']))
                except OSError as exc:
                    all_good = False
                    emit('log', f'POST-REPAIR UNREADABLE {r["path"]}: {exc}')
                    meter.step(r['size'])
            meter.finish()
            conn.commit()
            if all_good and not quarantine_errors:
                repaired.add(sid)
                record_event(conn, 'repair', f'Repaired/verified set {sid}')
                emit('log', f'REPAIRED and SHA256 verified all files in set {sid}')
            elif quarantine_errors:
                emit('log', f'WARNING: set {sid} hashes are OK but damaged-original quarantine failed; '
                            'issue retained for manual inspection')
            else:
                emit('log', f'WARNING: set {sid} still has damaged/missing files')
        meter.done()
    # Only clear issues of PAR sets confirmed fully intact after repair.
    return [x for x in issues if x.group not in repaired or x.kind.startswith('PAR_')]




@exclusive_archive
def rebuild_par(root: Path, issues: list[Item], opts: dict, exe: str,
                cancel: threading.Event, emit: Callable) -> dict:
    """Recreates missing/bad parity ONLY if every member matches approved SHA256.

    Contents are not rebaselined, and old parity isn't purged until new set verifies.
    """
    groups = {x.group for x in issues if x.selected and x.kind.startswith('PAR_')}
    if not groups:
        return {'done': 0}
    with db(root) as conn:
        sizes = {sid: conn.execute('SELECT total_bytes FROM sets WHERE id=?', (sid,)).fetchone()['total_bytes']
                 for sid in groups}
        meter = progresslib.WorkMeter(sum(3 * size for size in sizes.values()), emit, 'Rebuilding recovery data')
        n = 0
        for sid in sorted(groups):
            check_cancel(cancel)
            setrow = conn.execute("SELECT kind FROM sets WHERE id=? AND state='active'", (sid,)).fetchone()
            if setrow is None:
                raise SafetyError(f'Cannot rebuild: missing active set {sid}')
            emit('status', f'Rebuilding PAR set {sid}; checking baseline hashes first')
            make_set(root, conn, setrow['kind'], [], sid, set(), opts, exe, cancel, emit, meter)
            n += 1
        snapshot_db(root, conn, emit)
        meter.done()
        return {'done': n}




def selftest(exe: str, cancel: threading.Event, emit: Callable) -> dict:
    """Non-destructive real PAR2 compatibility test in temporary directory."""
    opts = dict(small_pct=20, large_pct=10, small_mb=1, group_mb=16, max_files=16, small_threads=2, large_threads=2)
    with tempfile.TemporaryDirectory(prefix='ParityKeeper_Selftest_') as folder:
        root = Path(folder)
        rel = 'slovenské obrázky/žltý test.txt'
        src = file_path(root, rel)
        src.parent.mkdir(parents=True, exist_ok=True)
        expected = ('Žltý text + príloha\n' * 3000).encode('utf-8')
        src.write_bytes(expected)
        (root / 'another text.txt').write_bytes(b'second file' * 1000)
        emit('log', 'SELF-TEST: Create a multi-file PAR set with Unicode filenames')
        changes = scan_changes(root, cancel, emit)
        apply_changes(root, changes, opts, exe, cancel, emit)
        problems = crc_check(root, cancel, emit)
        if problems:
            raise SafetyError(f'Self-test: healthy sources reported {len(problems)} issues')
        emit('log', 'SELF-TEST: Deliberately corrupt the test file, then repair it')
        src.write_bytes(b'CORRUPTED' * 1000)
        problems = crc_check(root, cancel, emit)
        if not any(x.kind == 'CORRUPT' and x.path == rel for x in problems):
            raise SafetyError('Self-test failed to detect deliberate corruption')
        remaining = repair_selected(root, problems, exe, cancel, emit)
        if remaining or src.read_bytes() != expected:
            raise SafetyError('Self-test: PAR2 did not correctly restore original bytes')
        if crc_check(root, cancel, emit):
            raise SafetyError('Self-test: integrity still damaged after repair')
        emit('log', 'SELF-TEST PASSED: create / verify / UTF-8 / detect corruption / repair / hash verify')
        return {'passed': True}


def compare_inventories(root_a: Path, root_b: Path, cancel: threading.Event,
                        emit: Callable) -> list[Item]:
    """Read-only comparison of last approved baselines on independently managed drives."""
    if root_a.resolve() == root_b.resolve():
        raise SafetyError('Choose two different folders/drives to compare.')
    for label, folder in [('primary', root_a), ('mirror', root_b)]:
        if not (folder / META / 'inventory.sqlite3').is_file():
            raise SafetyError(f'No inventory in {label}: {folder}. Scan/protect that archive first.')
    with db(root_a, initialize=False) as a, db(root_b, initialize=False) as b:
        a.execute('ATTACH DATABASE ? AS other', (str(root_b / META / 'inventory.sqlite3'),))
        rows = a.execute('''SELECT a.path AS p1, b.path AS p2, a.sha256 AS h1, b.sha256 AS h2,
                       a.size AS s1, b.size AS s2
                       FROM main.files a LEFT JOIN other.files b ON a.path=b.path
                       WHERE b.path IS NULL OR a.sha256<>b.sha256 OR a.size<>b.size
                       UNION ALL
                       SELECT NULL AS p1, b.path AS p2, NULL AS h1, b.sha256 AS h2,
                       NULL AS s1, b.size AS s2
                       FROM other.files b LEFT JOIN main.files a ON a.path=b.path
                       WHERE a.path IS NULL''')
        diff = []
        for row in rows:
            check_cancel(cancel)
            if row['p1'] is None:
                diff.append(Item('ONLY MIRROR', row['p2'], row['s2'], detail='Absent from first inventory', selected=False))
            elif row['p2'] is None:
                diff.append(Item('ONLY PRIMARY', row['p1'], row['s1'], detail='Absent from mirror inventory', selected=False))
            else:
                diff.append(Item('DIFFERENT', row['p1'], row['s1'], detail='Stored SHA256/size differ', selected=False))
        emit('log', f'Baseline comparison: {len(diff):,} differences. No files were copied or modified.')
        return diff
