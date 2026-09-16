"""Block schedules: map virtual time to block numbers and back (relative episode time in ms)."""

from __future__ import annotations

import bisect
from dataclasses import dataclass


class BlockScheduleError(ValueError):
    pass


class BlockSchedule:
    def block_at_or_after(self, time_ms: int) -> int: ...
    def time_of(self, block: int) -> int: ...
    def block_containing(self, time_ms: int) -> int: ...
    @property
    def first_block(self) -> int: ...
    @property
    def last_block(self) -> int: ...


@dataclass(slots=True)
class FixedIntervalSchedule(BlockSchedule):
    """Generated fixtures: block ``n`` occurs at ``origin_ms + (n - first) * interval_ms``."""

    first: int
    origin_ms: int
    interval_ms: int
    last: int

    def block_at_or_after(self, time_ms: int) -> int:
        if time_ms <= self.origin_ms:
            return self.first
        n = (time_ms - self.origin_ms + self.interval_ms - 1) // self.interval_ms
        b = self.first + n
        if b > self.last:
            raise BlockScheduleError("beyond block schedule")
        return b

    def time_of(self, block: int) -> int:
        if block < self.first or block > self.last:
            raise BlockScheduleError(f"block {block} outside schedule")
        return self.origin_ms + (block - self.first) * self.interval_ms

    def block_containing(self, time_ms: int) -> int:
        if time_ms < self.origin_ms:
            return self.first - 1
        b = self.first + (time_ms - self.origin_ms) // self.interval_ms
        return min(b, self.last)

    @property
    def first_block(self) -> int:
        return self.first

    @property
    def last_block(self) -> int:
        return self.last


class TableSchedule(BlockSchedule):
    """Historical packs: explicit sorted (block, time_ms) table; no interpolation is invented."""

    def __init__(self, rows: list[tuple[int, int]]) -> None:
        if not rows:
            raise BlockScheduleError("empty block table")
        rows = sorted(rows)
        self._blocks = [b for b, _ in rows]
        self._times = [t for _, t in rows]
        for i in range(1, len(rows)):
            if self._blocks[i] != self._blocks[i - 1] + 1:
                raise BlockScheduleError("block table must be contiguous")
            if self._times[i] < self._times[i - 1]:
                raise BlockScheduleError("block times must be non-decreasing")

    def block_at_or_after(self, time_ms: int) -> int:
        i = bisect.bisect_left(self._times, time_ms)
        if i >= len(self._blocks):
            raise BlockScheduleError("beyond block table")
        return self._blocks[i]

    def time_of(self, block: int) -> int:
        i = block - self._blocks[0]
        if i < 0 or i >= len(self._blocks):
            raise BlockScheduleError(f"block {block} outside table")
        return self._times[i]

    def block_containing(self, time_ms: int) -> int:
        i = bisect.bisect_right(self._times, time_ms) - 1
        if i < 0:
            return self._blocks[0] - 1
        return self._blocks[i]

    @property
    def first_block(self) -> int:
        return self._blocks[0]

    @property
    def last_block(self) -> int:
        return self._blocks[-1]
