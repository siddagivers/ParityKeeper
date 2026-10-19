"""Byte-based, live-updating ETA for long archive operations.

The data amount is derived from the inventory and actual reads. Estimates are
based on observed throughput and are unavailable until enough work is measured.
The work units for PAR subprocesses represent approximate I/O, not exact bytes
read internally by MultiPar.
"""
from __future__ import annotations
import re
import time

# PAR2 may print percentages on carriage-return progress messages. Require a
# progress verb or a line beginning with a percentage; don't parse redundancy
# options (/rr12) or incidental percentages in log lines.
_PERCENT = re.compile(r'(?i)(?:^\s*|\b(?:progress|creating|verifying|repairing|checking|encoding|calculating|processing|loading|writing)\b[^\r\n]{0,72}?)(\d{1,3}(?:\.\d+)?)\s*%')


def parse_percentage(line: str):
    match = _PERCENT.search(line)
    if not match:
        return None
    value = float(match.group(1))
    return value / 100.0 if 0 <= value <= 100 else None


def duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return 'measuring speed…'
    sec = int(round(seconds))
    if sec < 60:
        return f'{sec}s'
    if sec < 3600:
        return f'{sec // 60}m {sec % 60:02}s'
    if sec < 86400:
        return f'{sec // 3600}h {(sec % 3600) // 60:02}m'
    return f'{sec // 86400}d {(sec % 86400) // 3600:02}h'


class WorkMeter:
    """Tracks estimated byte-work, including PAR commands run as child processes.

    Progress is monotonic. No fake per-second progress is generated when a PAR
    subprocess does not report %. ETA stays provisional until observed work.
    """
    def __init__(self, total: int, emit, label: str):
        self.total = max(1, int(total))
        self.completed = 0
        self.phase = ''
        self.phase_weight = 0
        self.phase_done = 0
        self.emit = emit
        self.label = label
        self.started = time.monotonic()
        self.last_emit = 0.
        self.last_done = 0
        self.last_time = self.started
        self.velocity = None
        self._send(force=True)

    def add_total(self, amount: int):
        self.total += max(0, int(amount))

    def begin(self, description: str, weight: int):
        # Previous stage must be explicitly completed by its owner.
        self.phase = description
        self.phase_weight = max(0, int(weight))
        self.phase_done = 0
        self._send(force=True)

    def step(self, bytes_done: int):
        self.phase_done = min(self.phase_weight, self.phase_done + max(0, int(bytes_done)))
        self._send()

    def fraction(self, value: float):
        self.phase_done = max(self.phase_done, min(self.phase_weight,
                              int(self.phase_weight * max(0., min(1., value)))))
        self._send()

    def heartbeat(self):
        """Refresh elapsed/ETA during a silent but running MultiPar process."""
        self._send()

    def finish(self):
        self.completed += self.phase_weight
        self.phase_weight = 0
        self.phase_done = 0
        self._send(force=True)

    def _send(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_emit < 0.35:
            return
        done = min(self.total, self.completed + self.phase_done)
        elapsed = max(0.001, now - self.started)
        # Overall measured effective speed includes the overhead of hashing,
        # creation and verification, not theoretical bus speed.
        rate = done / elapsed if done else 0.
        eta = (self.total - done) / rate if rate > 0 and elapsed >= 0.75 else None
        value = dict(label=self.label, stage=self.phase, done=done, total=self.total,
                     percent=100 * done / self.total, elapsed=elapsed, speed=rate,
                     eta_seconds=eta, estimated=True)
        self.emit('progress', value)
        self.last_emit = now

    def done(self):
        self.completed = self.total
        self.phase_weight = self.phase_done = 0
        self.phase = 'Finished'
        self._send(force=True)
