"""Version 1.1 regression tests: CPU threads and computed progress."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import progress

FAKE = str(Path(__file__).with_name('fake_par2j.py'))
OPTS = dict(small_pct=12, large_pct=8, small_mb=1, group_mb=16,
            max_files=20, small_threads=9, large_threads=2)


class V11Tests(unittest.TestCase):
    def test_thread_limits(self):
        self.assertEqual(engine.thread_count('small', OPTS), 9)
        self.assertEqual(engine.thread_count('large', OPTS), 2)
        live = {'small': 8, 'large': 1}
        opts = {**OPTS, 'thread_provider': lambda kind: live[kind]}
        self.assertEqual(engine.thread_count('small', opts), 8)
        live['small'] = 3
        self.assertEqual(engine.thread_count('small', opts), 3)
        with self.assertRaises(engine.SafetyError):
            engine.thread_count('small', {'small_threads': 11})

    def test_percentage_parsing(self):
        self.assertEqual(progress.parse_percentage('Progress 37.5%'), .375)
        self.assertEqual(progress.parse_percentage(' 42%'), .42)
        self.assertEqual(progress.parse_percentage('Verifying : 100 %'), 1)
        self.assertIsNone(progress.parse_percentage('Redundancy 12 %'))
        self.assertIsNone(progress.parse_percentage('CPU thread: 8 / 16'))

    def test_live_progress_and_real_cli_args(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'small.txt').write_bytes(b'A' * 10000)
            cancel = threading.Event()
            events = []
            emit = lambda kind, data: events.append((kind, data))
            changes = engine.scan_changes(root, cancel, emit)
            engine.apply_changes(root, changes, OPTS, FAKE, cancel, emit)
            commands = [msg for kind, msg in events if kind == 'log' and msg.startswith('MultiPar:')]
            self.assertTrue(commands)
            self.assertTrue(all('/lc9' in cmd for cmd in commands), commands)
            self.assertEqual(engine.crc_check(root, cancel, emit), [])
            latest = [event for kind, event in events if kind == 'progress']
            self.assertTrue(latest, 'No progress events were emitted')
            self.assertTrue(any(p['label'] == 'Checksum integrity check' for p in latest))
            self.assertTrue(any(p['percent'] == 100 for p in latest))
            # MultiPar verify and repair honor their own per-file-kind limits too.
            engine.verify_all_par(root, FAKE, cancel, emit, {'small_threads': 4, 'large_threads': 2})
            self.assertTrue(any('/lc4' in msg for kind, msg in events if kind == 'log' and 'MultiPar:' in msg))

    def test_eta_measured_not_invented(self):
        events = []
        meter = progress.WorkMeter(100, lambda k, v: events.append((k, v)), 'test')
        self.assertIsNone(events[-1][1]['eta_seconds'])
        meter.begin('Test operation', 100)
        meter.step(40)
        meter.finish()
        meter.done()
        self.assertEqual(events[-1][1]['done'], 100)
        self.assertAlmostEqual(events[-1][1]['percent'], 100)


if __name__ == '__main__':
    unittest.main()
