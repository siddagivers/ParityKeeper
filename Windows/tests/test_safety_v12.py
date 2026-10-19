"""v1.2 regression tests for case-sensitive inventories, safety and recovery codes."""
import os
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine

FAKE = str(Path(__file__).with_name('fake_par2j.py'))
OPTS = dict(small_pct=12, large_pct=8, small_mb=1, group_mb=16, max_files=12)

class SafetyV12Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)/'backup'
        self.root.mkdir()
        self.cancel = threading.Event()
        self.logs = []
        self.emit = lambda kind, value: self.logs.append((kind,value))

    def test_case_different_names_are_distinct(self):
        (self.root/'Photo.jpg').write_bytes(b'FIRST')
        (self.root/'photo.jpg').write_bytes(b'SECOND')
        changes = engine.scan_changes(self.root,self.cancel,self.emit)
        self.assertEqual({x.path for x in changes},{'Photo.jpg','photo.jpg'})
        engine.apply_changes(self.root,changes,OPTS,FAKE,self.cancel,self.emit)
        with engine.db(self.root) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM files').fetchone()[0],2)
        self.assertFalse(engine.crc_check(self.root,self.cancel,self.emit))

    def test_automatic_database_migration_preserves_backup(self):
        # Construct an older empty v1.1 database with NOCASE filenames.
        meta = self.root/engine.META
        meta.mkdir()
        with sqlite3.connect(meta/'inventory.sqlite3') as c:
            c.executescript('''CREATE TABLE sets (id TEXT PRIMARY KEY, index_rel TEXT NOT NULL,
               created TEXT NOT NULL, kind TEXT NOT NULL, percent INTEGER NOT NULL,
               file_count INTEGER NOT NULL, total_bytes INTEGER NOT NULL, state TEXT NOT NULL);
               CREATE TABLE files (path TEXT PRIMARY KEY COLLATE NOCASE,
               size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
               sha256 TEXT NOT NULL, crc32 TEXT NOT NULL, set_id TEXT NOT NULL REFERENCES sets(id),
               verified_at TEXT);
               CREATE INDEX files_by_set ON files(set_id);''')
        (self.root/'A').write_text('a')
        (self.root/'a').write_text('b')
        result=engine.scan_changes(self.root,self.cancel,self.emit)
        self.assertEqual({i.path for i in result},{'A','a'})
        with engine.db(self.root) as c:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='files'").fetchone()[0]
            self.assertIn('BINARY',sql)
        self.assertTrue(list((meta/'snapshots').glob('inventory_before_case_migration_*.sqlite3')))

    def test_missing_compare_has_clear_safety_error(self):
        with engine.db(self.root): pass
        mirror = Path(self.temporary.name)/'other'
        mirror.mkdir()
        with self.assertRaisesRegex(engine.SafetyError,'mirror'):
            engine.compare_inventories(self.root,mirror,self.cancel,self.emit)
        self.assertFalse((mirror/engine.META/'inventory.sqlite3').exists())

    def test_path_escape_symlink_and_metadata_rejected(self):
        outside=Path(self.temporary.name)/'outside'
        outside.mkdir()
        for item in ('../outside/test','a//b','a/./b','/tmp/file','.paritykeeper/inventory.sqlite3'):
            with self.subTest(item=item), self.assertRaises(engine.SafetyError):
                engine.file_path(self.root,item)
        if hasattr(os,'symlink'):
            try:
                (self.root/'bridge').symlink_to(outside, target_is_directory=True)
            except OSError:
                pass
            else:
                with self.assertRaises(engine.SafetyError):
                    engine.file_path(self.root,'bridge/file')

    def test_quarantine_does_not_overwrite_old_copies(self):
        p=self.root/'broken.jpg.1'
        p.write_bytes(b'old damaged')
        a=engine.quarantine_new_backups(self.root,{'broken.jpg':set()},self.emit)
        self.assertEqual(a,[])
        p.write_bytes(b'new damaged')
        b=engine.quarantine_new_backups(self.root,{'broken.jpg':set()},self.emit)
        self.assertEqual(b,[])
        copies=list((self.root/engine.META/'quarantine').rglob('broken.jpg.1'))
        self.assertEqual({x.read_bytes() for x in copies},{b'old damaged',b'new damaged'})

    def test_post_creation_rehash_fails_closed_even_if_mtime_same(self):
        source=self.root/'record.bin'
        source.write_bytes(b'AAAAA')
        original_run=engine.run_par
        def altered_run(exe,args,*other,**kw):
            result=original_run(exe,args,*other,**kw)
            if args[0]=='c':
                st=source.stat()
                source.write_bytes(b'BBBBB')
                os.utime(source,ns=(st.st_atime_ns,st.st_mtime_ns))
            if args[0]=='v':
                return 0   # simulate a repair engine returning an incorrect OK
            return result
        with patch.object(engine,'run_par',altered_run):
            with self.assertRaisesRegex(engine.SafetyError,'Source content changed'):
                engine.apply_changes(self.root,engine.scan_changes(self.root,self.cancel,self.emit),
                                     OPTS,FAKE,self.cancel,self.emit)
        with engine.db(self.root) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)
        self.assertEqual(__import__('json').loads((self.root/engine.META/'last_apply_state.json').read_text())['requires_new_scan'],True)

    def test_orphan_folders_are_inspected_but_not_deleted(self):
        with engine.db(self.root): pass
        orphan=self.root/engine.META/'par2'/'uncommitted'
        orphan.mkdir()
        (orphan/'valuable.par2').write_text('valuable')
        self.assertIn(orphan,engine.list_orphan_par_dirs(self.root))
        self.assertTrue((orphan/'valuable.par2').exists())

    def test_engine_specific_exit_codes(self):
        self.assertTrue(engine.multipar_can_attempt_repair(132))
        self.assertFalse(engine.multipar_can_attempt_repair(268))
        self.assertTrue(engine.multipar_repair_command_ok(16))
        self.assertFalse(engine.multipar_repair_command_ok(20))

if __name__=='__main__': unittest.main()
