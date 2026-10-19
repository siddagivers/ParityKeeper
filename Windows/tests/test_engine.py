import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
import engine

MOCK = str(Path(__file__).with_name('fake_par2j.py'))
OPTS = dict(small_pct=12, large_pct=8, small_mb=1, group_mb=16, max_files=50)


class ParityKeeperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'drive_A'
        self.mirror = Path(self.tmp.name) / 'drive_B'
        self.root.mkdir()
        self.mirror.mkdir()
        self.cancel = threading.Event()
        self.logs = []
        self.emit = lambda kind, obj: self.logs.append((kind, obj))

    def write(self, relative, content):
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    def scan(self):
        return engine.scan_changes(self.root, self.cancel, self.emit)

    def protect(self, changes):
        return engine.apply_changes(self.root, changes, OPTS, MOCK, self.cancel, self.emit)

    def test_add_check_corrupt_and_repair(self):
        self.write('photos/A.txt', b'good A' * 5000)
        self.write('photos/B.txt', b'good B' * 5000)
        self.write('movies/huge.bin', b'0' * 1024 * 1024)
        self.assertEqual(len(self.scan()), 3)
        outcome = self.protect(self.scan())
        self.assertEqual(outcome['files'], 3)
        self.assertEqual(self.scan(), [])
        self.assertEqual(engine.crc_check(self.root, self.cancel, self.emit), [])
        self.write('photos/B.txt', b'DAMAGED' * 1000)
        problems = engine.crc_check(self.root, self.cancel, self.emit)
        self.assertEqual([p.kind for p in problems], ['CORRUPT'])
        after = engine.repair_selected(self.root, problems, MOCK, self.cancel, self.emit)
        self.assertEqual(after, [])
        self.assertEqual((self.root / 'photos/B.txt').read_bytes(), b'good B' * 5000)
        self.assertEqual(engine.crc_check(self.root, self.cancel, self.emit), [])
        quarantine = list((self.root / engine.META / 'quarantine').rglob('B.txt.1'))
        self.assertEqual(len(quarantine), 1)

    def test_changed_not_autoaccepted_and_delete(self):
        self.write('dir/a', b'a' * 10000)
        self.write('dir/b', b'b' * 10000)
        self.protect(self.scan())
        (self.root / 'dir/a').unlink()
        items = self.scan()
        self.assertEqual(items[0].kind, 'MISSING')
        self.assertFalse(items[0].selected)
        self.assertEqual(engine.crc_check(self.root, self.cancel, self.emit)[0].kind, 'MISSING')
        items[0].selected = True
        self.protect(items)
        self.assertEqual(self.scan(), [])
        self.assertEqual(engine.crc_check(self.root, self.cancel, self.emit), [])
        self.write('dir/b', b'edited!')
        changes = self.scan()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, 'CHANGED')
        self.assertFalse(changes[0].selected)
        self.protect(changes)
        self.assertEqual(engine.crc_check(self.root, self.cancel, self.emit)[0].kind, 'CORRUPT')
        changes[0].selected = True
        self.protect(changes)
        self.assertEqual(engine.crc_check(self.root, self.cancel, self.emit), [])

    def test_still_protected_members_are_guarded_against_bitrot(self):
        self.write('d/a', b'Original-A')
        self.write('d/b', b'Original-B')
        self.protect(self.scan())
        self.write('d/a', b'changed!!')
        self.write('d/b', b'BROKEN')
        changes = self.scan()
        for item in changes:
            item.selected = item.path == 'd/a'
        with self.assertRaises(engine.SafetyError):
            self.protect(changes)
        with engine.db(self.root) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM files').fetchone()[0], 2)

    def test_missing_and_mirror_diff(self):
        self.write('x', b'x')
        self.protect(self.scan())
        with engine.db(self.mirror):
            pass
        differences = engine.compare_inventories(self.root, self.mirror, self.cancel, self.emit)
        self.assertEqual([d.kind for d in differences], ['ONLY PRIMARY'])

    def test_missing_par_volume_detection(self):
        self.write('x', b'content')
        self.protect(self.scan())
        vol = next((self.root / engine.META / 'par2').rglob('*.vol*.par2'))
        vol.unlink()
        issues = engine.crc_check(self.root, self.cancel, self.emit)
        self.assertEqual([x.kind for x in issues], ['PAR_MISSING'])


if __name__ == '__main__':
    unittest.main()
