"""Free-space guarding: a per-run floor under the work filesystem."""
# CHerSun 2026

import logging
import os
import shutil
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from pyqenc.utils.log_format import fmt_size_mb

logger = logging.getLogger(__name__)

FreeSpaceProbe = Callable[[Path], int]
"""Reads the bytes free on the filesystem containing a directory.

The OS boundary seam — tests substitute a fake filesystem instead of
touching a real volume."""


def _disk_free(path: Path) -> int:
    """Bytes free on the filesystem containing *path* (``shutil.disk_usage``)."""
    return shutil.disk_usage(path).free


class InsufficientSpaceError(RuntimeError):
    """Starting an item would break the configured free-space floor.

    Carries the human-facing message with the numbers; the owning phase
    catches it and converts it to its ``FAILED`` result — the loud stop.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SpaceGuard:
    """The run's free-space floor: no item starts when it would break it.

    One ``ensure`` call covers both check shapes: ``need=0`` when the item's
    size is unknowable beforehand (a plain floor check) and ``need=N`` when
    it is known or bounded (the floor must still hold *after* the item
    lands). Concurrency-safe by accounting, not reservation: concurrent
    items each add their ``need`` to a per-filesystem outstanding ledger for
    the duration of the ``with`` block, so bounded workers cannot
    collectively overshoot what each individually passed. The ledger is
    in-memory only — it coordinates pyqenc's own concurrent productions and
    reserves nothing against other processes; a mid-item ENOSPC is that
    item's ordinary failure, not the guard's concern.

    Per-filesystem keying (``os.stat`` device id): directories on different
    volumes keep independent ledgers, and a check always reads the free
    space of the exact filesystem the item writes to.

    The largest item size seen via :meth:`observe` feeds callers that must
    guess an unknown next size (the attempt reserve: a multiple of the
    worst attempt so far).
    """

    def __init__(self, min_free: int, probe: FreeSpaceProbe = _disk_free) -> None:
        """Store the floor and the free-space probe.

        Args:
            min_free: The configured floor (``min_free_space``) — bytes that
                      must stay free on the target filesystem after every
                      item lands.
            probe:    Bytes-free reader, injectable for tests.
        """
        self._min_free  = min_free
        self._probe     = probe
        self._lock      = threading.Lock()
        self._reserved: dict[object, int] = {}
        self._worst_seen = 0

    @property
    def min_free(self) -> int:
        """The configured floor in bytes."""
        return self._min_free

    @property
    def worst_seen(self) -> int:
        """The largest item size reported to :meth:`observe` so far."""
        with self._lock:
            return self._worst_seen

    def observe(self, size_bytes: int) -> None:
        """Record a produced item's size for the unknown-size callers.

        Args:
            size_bytes: The item's size in bytes (ignored when smaller than
                        the current worst).
        """
        with self._lock:
            self._worst_seen = max(self._worst_seen, size_bytes)

    @contextmanager
    def ensure(self, target_dir: Path, need: int) -> Generator[None]:
        """Admit one item to production, or refuse it loudly.

        Refuses when ``free - outstanding - need < min_free`` on the
        filesystem containing *target_dir*; on admission, ``need`` joins
        that filesystem's outstanding ledger until the block exits.

        Args:
            target_dir: The directory the item writes to (an existing
                        directory on the target filesystem).
            need:       The item's expected size in bytes — a known or
                        bounded size, or ``0`` when unknowable (floor check
                        only).

        Yields:
            Nothing; the block body performs the production.

        Raises:
            InsufficientSpaceError: When the floor would be broken. The
                message names the numbers and the config knob; nothing is
                reserved in that case.
        """
        device = os.stat(target_dir).st_dev
        with self._lock:
            free        = self._probe(target_dir)
            outstanding = self._reserved.get(device, 0)
            if free - outstanding - need < self._min_free:
                raise InsufficientSpaceError(
                    f"insufficient free space: {fmt_size_mb(free)} free, "
                    f"{fmt_size_mb(need + outstanding)} to be written, "
                    f"min_free_space floor {fmt_size_mb(self._min_free)} "
                    "(lower 'min_free_space' in config or free disk space)"
                )
            self._reserved[device] = outstanding + need
        try:
            yield
        finally:
            with self._lock:
                self._reserved[device] -= need
