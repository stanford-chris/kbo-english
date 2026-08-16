#!/usr/bin/env python3
"""
One KBO run at a time.

The bot's jobs sit on overlapping slots — live polls every 15 minutes, standings
every 30, results at 23:30, the roster builder at 22:00 — on the assumption that
each finishes in seconds. On 15 August 2026 a stalled card renderer stretched a
live poll over four minutes, so it was still going when the standings run fired.
Both had loaded kbo_history.json before either wrote it, so the second write
dropped the first's entry: the standings post lost its dedup record and went out
three times for the same night.

An exclusive flock around each run makes that impossible. A run that cannot take
the lock inside its budget exits without posting rather than queueing up behind
it. That trade suits the polled modes, where the next slot is minutes away and a
skipped poll leaves no trace, but not schedule (daily) or leaders (weekly),
which have no second chance — hence the per-mode budgets the callers pass in.

The kernel drops the lock when the process exits, however it exits, so a crash
cannot leave it held.
"""

import fcntl
import os
import time
from pathlib import Path

LOCKFILE = Path(__file__).parent / '.kbo_run.lock'

# Module-level because closing the file releases the lock: the handle has to
# outlive hold()'s frame, and live for as long as the run does.
_handle = None


def holder():
    """Whatever the current holder wrote about itself, for the waiting line."""
    try:
        return LOCKFILE.read_text().strip() or 'unknown'
    except OSError:
        return 'unknown'


def hold(mode, wait):
    """Take the run lock, waiting up to `wait` seconds for it.

    Returns True if it is held (and stays held until this process exits), False
    if another run still had it when the budget ran out — the caller should say
    so and return without posting."""
    global _handle
    _handle = open(LOCKFILE, 'a+')
    deadline = time.monotonic() + wait
    announced = False
    while True:
        try:
            fcntl.flock(_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if time.monotonic() >= deadline:
                _handle.close()
                _handle = None
                return False
            if not announced:                   # once, not every poll
                print(f'{mode}: waiting for the run lock (held by {holder()}).')
                announced = True
            time.sleep(2)
            continue
        # Leave a note for whoever waits on us next. The file is opened in
        # append mode, so truncating first is what puts this at offset 0.
        _handle.truncate(0)
        _handle.write(f'{mode} pid {os.getpid()}\n')
        _handle.flush()
        return True
