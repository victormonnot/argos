"""Bounded passive inspection of decoded messages, before telemetry admission.

The owner calls append/snapshot in monotonic clock order. Rates are approximate
rolling receipt rates: 30 count buckets bound memory independently of message
frequency. Their boundary uncertainty is at most window / 30 seconds. Receipt
time is local processing time, not vehicle time or physical link latency.
"""
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import math

from argos.backends.mavlink import Received
from .views import json_fields


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("time must be a finite number")
    return float(value)


@dataclass
class _Type:
    latest: Received
    count: int = 0
    buckets: deque = field(default_factory=lambda: deque(maxlen=31))


class LiveMessages:
    """Keep the most recently received message for each source/type identity.

    At most max_types identities survive. Eviction is least recently received;
    a returning identity starts a new retained counter, not a lifetime total.
    The caller's link counters remain the authority for total traffic.
    """

    def __init__(self, *, window=3., max_types=256):
        window = _time(window)
        if window <= 0:
            raise ValueError("window must be positive")
        if isinstance(max_types, bool) or not isinstance(max_types, int) or not 1 <= max_types <= 4096:
            raise ValueError("max_types must be an integer in 1..4096")
        self.window = window
        self.resolution = window / 30
        self.max_types = max_types
        self.evicted_types = 0
        self._types = OrderedDict()
        self._clock = None

    def _check(self, now):
        now = _time(now)
        if self._clock is not None and now < self._clock:
            raise ValueError("time predates the last accepted call")
        return now

    def append(self, event: Received):
        now = self._check(event.received_at)
        bucket = math.floor(now / self.resolution)
        key = (event.system, event.component, event.message_id)
        item = self._types.get(key)
        if item is None:
            if len(self._types) == self.max_types:
                self._types.popitem(last=False)
                self.evicted_types += 1
            item = self._types[key] = _Type(event)
        else:
            self._types.move_to_end(key)
        self._clock = now
        item.latest = event
        item.count += 1
        if item.buckets and item.buckets[-1][0] == bucket:
            previous, count = item.buckets.pop()
            item.buckets.append((previous, count + 1))
        else:
            item.buckets.append((bucket, 1))
        self._prune(item, now)

    def _prune(self, item, now):
        oldest = math.floor((now - self.window) / self.resolution)
        while item.buckets and item.buckets[0][0] < oldest:
            item.buckets.popleft()

    def snapshot(self, now):
        now = self._check(now)
        self._clock = now
        result = []
        for key in sorted(self._types):
            item = self._types[key]
            event = item.latest
            self._prune(item, now)
            age = now - event.received_at
            # Expire without needing another append. The partially overlapping
            # oldest bucket makes a non-zero rate an approximation, never latency.
            hz = 0. if age >= self.window else sum(count for _, count in item.buckets) / self.window
            result.append({
                "system": event.system, "component": event.component,
                "message_id": event.message_id, "type_name": event.type_name,
                "count": item.count, "last_received_at": event.received_at,
                "rx_age_s": age, "hz": hz, "sequence": event.sequence,
                "fields": json_fields(event.fields), "frame_hex": event.frame.hex(),
                "frame_bytes": len(event.frame),
                "wire_version": {0xfe: 1, 0xfd: 2}.get(event.frame[0]) if event.frame else None,
            })
        return {"window_s": self.window, "rate_resolution_s": self.resolution,
                "max_types": self.max_types, "evicted_types": self.evicted_types,
                "types": result}
