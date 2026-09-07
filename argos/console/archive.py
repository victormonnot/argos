"""Bounded, read-only local journal archive. Called outside the receiver loop.

Only a fully validated recording is exposed. One file and one source index are
cached; every use checks the open file identity again. Replaying backwards uses
indexed receptions, never a live cache or the current observation's clock.
"""
from bisect import bisect_left, bisect_right
from collections import Counter
import heapq
import hashlib
import io
import math
import os
from pathlib import Path
import re
import stat
from threading import Lock

from argos.backends.mavlink import RecordingError, TelemetryCache, TelemetryLimits, read_recording
from argos.backends.mavlink.health import HealthCache
from .views import battery_view, mode_view, reception_view, json_fields
from .context import parse_capture_context
from .analysis import analyze_recording
from .recording_limits import MAX_BYTES, MAX_EVENTS

LIST_LIMIT = 200
# These are analysis settings, not recovered capture configuration.
LIMITS = TelemetryLimits(1., .2, .4)
BATTERY_AGE = 2.
KINDS = {0: "heartbeat", 1: "battery", 30: "attitude", 32: "local_position_ned"}
IDENTIFIER = re.compile(r"[0-9a-f]{32}")


def replay_limits(context):
    if context is not None:
        ages = context["age_limits_s"]
        return {name: ages[name] for name in KINDS.values()}
    return {"heartbeat": LIMITS.heartbeat, "attitude": LIMITS.attitude,
            "local_position_ned": LIMITS.local_position_ned, "battery": BATTERY_AGE}


class ArchiveError(ValueError):
    def __init__(self, detail, status=422):
        super().__init__(detail)
        self.status = status


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class ReplayIndex:
    def __init__(self, recording, system, component, context=None):
        self.recording = recording
        self.source = (system, component)
        ages = replay_limits(context)
        limits = TelemetryLimits(ages["heartbeat"], ages["attitude"], ages["local_position_ned"])
        battery_age = ages["battery"]
        cache = TelemetryCache(system=system, component=component, limits=limits,
                               started_at=recording.started_at)
        health = HealthCache(system=system, component=component, age_limit=battery_age,
                             started_at=recording.started_at)
        self.empty = {name: reception_view(getattr(cache.snapshot(recording.started_at), name),
                                          getattr(limits, name))
                      for name in ("heartbeat", "attitude", "local_position_ned")}
        self.empty["battery"] = battery_view(health.snapshot(recording.started_at).battery, battery_age)
        self.samples = {name: ([], []) for name in KINDS.values()}
        self.all_times, self.source_times, self.rejected_times, self.rejections = [], [], [], []
        for event in recording.events:
            now = event.received_at
            self.all_times.append(now)
            if (event.system, event.component) != self.source:
                continue
            self.source_times.append(now)
            target = health if event.message_id == 1 else cache
            update = target.update(event, now)
            if update.accepted:
                name = KINDS[event.message_id]
                view = (battery_view(health.snapshot(now).battery, battery_age) if name == "battery"
                        else reception_view(getattr(cache.snapshot(now), name), getattr(limits, name)))
                times, values = self.samples[name]
                times.append(now)
                values.append(view)
            elif update.status.value == "rejected":
                self.rejected_times.append(now)
                self.rejections.append(update.detail)
        self.steps = sorted({at for times, _ in self.samples.values() for at in times})

    def snapshot(self, offset):
        start, end = self.recording.started_at, self.recording.ended_at
        if not math.isfinite(offset) or not 0 <= offset <= end - start:
            raise ArchiveError("Le curseur doit être compris entre le début et la fin du journal")
        now = min(end, start + offset)
        views, accepted = {}, 0
        for name, (times, values) in self.samples.items():
            count = bisect_right(times, now)
            accepted += count
            view = dict(values[count - 1] if count else self.empty[name])
            if count:
                age = now - view["received_at"]
                view.update(rx_age_s=age, state="recent" if age <= view["age_limit_s"] else "stale")
            views[name] = view
        seen = bisect_right(self.all_times, now)
        selected = bisect_right(self.source_times, now)
        rejected = bisect_right(self.rejected_times, now)
        previous, following = bisect_left(self.steps, now) - 1, bisect_right(self.steps, now)
        return {
            "at_s": offset, "system": self.source[0], "component": self.source[1],
            "previous_at_s": self.steps[previous] - start if previous >= 0 else None,
            "next_at_s": self.steps[following] - start if following < len(self.steps) else None,
            "received": seen, "accepted": accepted, "rejected": rejected,
            "ignored_source": seen - selected, "ignored_type": selected - accepted - rejected,
            "last_rejection": self.rejections[rejected - 1] if rejected else "",
            "mode": mode_view(views["heartbeat"]), **views,
        }


class RecordingArchive:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self._lock = Lock()
        self._key = self._recording = self._data = self._index = None
        self._revision = None
        self._context = None

    def catalog(self, recording_status):
        def entries():
            try:
                with os.scandir(self.directory) as files:
                    for item in files:
                        identifier = item.name.removesuffix(".jsonl")
                        if item.name != f"{identifier}.jsonl" or not IDENTIFIER.fullmatch(identifier):
                            continue
                        try:
                            info = item.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        state = "unverified"
                        if recording_status["id"] == identifier and recording_status["state"] in ("recording", "error"):
                            state = recording_status["state"]
                        elif info.st_size > MAX_BYTES:
                            state = "too_large"
                        yield {"id": identifier, "size_bytes": info.st_size,
                               "modified_at": info.st_mtime, "state": state}
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ArchiveError("Le dossier des journaux est inaccessible", 503) from exc

        total = 0

        def counted():
            nonlocal total
            for item in entries():
                total += 1
                yield item

        items = heapq.nlargest(LIST_LIMIT, counted(), key=lambda item: (item["modified_at"], item["id"]))
        return {"items": items, "total": total, "limit": LIST_LIMIT, "directory": str(self.directory),
                "max_bytes": MAX_BYTES, "max_events": MAX_EVENTS}

    def _load(self, identifier):
        if not IDENTIFIER.fullmatch(identifier):
            raise ArchiveError("Journal introuvable", 404)
        try:
            # No symlinks, directories, devices or blocking FIFO opens.
            fd = os.open(self.directory / f"{identifier}.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ArchiveError("Journal introuvable", 404)
                if before.st_size > MAX_BYTES:
                    raise ArchiveError("Journal trop volumineux pour cette relecture (32 Mio maximum)", 413)
                key = (identifier, signature(before))
                if key == self._key:
                    # Filesystems can give successive same-size writes identical
                    # timestamps. Also compare the bounded completion footer so
                    # a newly finalized version cannot reuse the previous index.
                    stream.seek(max(0, before.st_size - 1024))
                    tail = stream.read(1025)
                    if tail == self._data[-1024:] and signature(before) == signature(os.fstat(stream.fileno())):
                        return
                    stream.seek(0)
                # Release the previous file/index before parsing the next one.
                self._key = self._recording = self._data = self._index = self._context = None
                data = stream.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES or signature(before) != signature(os.fstat(stream.fileno())):
                    raise ArchiveError("Le journal a changé pendant sa lecture ; réessayez")
            try:
                recording = read_recording(io.BytesIO(data), max_events=MAX_EVENTS)
                context = parse_capture_context(recording.context) if recording.context is not None else None
            except (RecordingError, ValueError) as exc:
                raise ArchiveError(f"Journal incomplet, invalide ou hors limite : {exc}") from exc
            self._key, self._data, self._recording = key, data, recording
            self._context = context
            self._revision = hashlib.sha256(data).hexdigest()
        except OSError as exc:
            raise ArchiveError("Journal introuvable ou inaccessible", 404) from exc

    def metadata(self, identifier):
        with self._lock:
            self._load(identifier)
            recording = self._recording
            sources = Counter((event.system, event.component) for event in recording.events)
            return {
                "id": identifier, "revision": self._revision, "integrity": "verified", "events": len(recording.events),
                "duration_s": recording.ended_at - recording.started_at,
                "started_at": recording.started_at, "ended_at": recording.ended_at,
                "codec_version": recording.codec_version,
                "end_reason": recording.end_reason, "end_detail": recording.end_detail,
                "sources": [{"system": system, "component": component, "events": count}
                            for (system, component), count in sorted(sources.items())],
                "download_url": f"/api/recordings/{identifier}/download?revision={self._revision}",
                "context": json_fields(self._context),
                "context_status": ("recorded" if self._context is not None else
                                   "unknown" if recording.context is not None else "unavailable"),
                "limits": replay_limits(self._context),
                "limits_origin": "capture" if self._context is not None else "analysis_defaults",
            }

    def replay(self, identifier, offset, system, component, revision):
        with self._lock:
            self._load(identifier)
            if revision != self._revision:
                raise ArchiveError("Le fichier a changé ; ouvrez à nouveau le journal", 409)
            if not (1 <= system <= 255 and 1 <= component <= 255):
                raise ArchiveError("Choisissez un système et un composant entre 1 et 255")
            if self._index is None or self._index.source != (system, component):
                if not any((event.system, event.component) == (system, component) for event in self._recording.events):
                    raise ArchiveError("Ce composant n’apparaît pas dans le journal")
                self._index = ReplayIndex(self._recording, system, component, self._context)
            return {"id": identifier, **self._index.snapshot(offset)}

    def analysis(self, identifier, revision, system=None, component=None, message_id=None,
                 bins=160, gap_threshold_s=1.):
        with self._lock:
            self._load(identifier)
            if revision != self._revision:
                raise ArchiveError("Le fichier a changé ; ouvrez à nouveau le journal", 409)
            try:
                result = analyze_recording(self._recording, system=system, component=component,
                                           message_id=message_id, bins=bins, gap_threshold_s=gap_threshold_s)
            except ValueError as exc:
                raise ArchiveError(str(exc)) from exc
            return {"id": identifier, "revision": self._revision, **result}

    def messages(self, identifier, revision, offset=0, limit=50, system=None, component=None, message_id=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ArchiveError("La page attend un décalage positif ou nul et une limite de 1 à 100 messages")
        if ((system is None) != (component is None)
                or any(value is not None and (type(value) is not int or not 0 <= value <= 255)
                       for value in (system, component))):
            raise ArchiveError("Choisissez ensemble un système et un composant entre 0 et 255")
        if message_id is not None and (type(message_id) is not int or not 0 <= message_id <= 16777215):
            raise ArchiveError("L’identifiant de message doit être compris entre 0 et 16777215")
        with self._lock:
            self._load(identifier)
            if revision != self._revision:
                raise ArchiveError("Le fichier a changé ; ouvrez à nouveau le journal", 409)
            counts, items, total = Counter(), [], 0
            for index, event in enumerate(self._recording.events, start=1):
                counts[event.message_id, event.type_name] += 1
                if system is not None and (event.system, event.component) != (system, component):
                    continue
                if message_id is not None and event.message_id != message_id:
                    continue
                if offset <= total < offset + limit:
                    items.append({
                        "index": index, "at_s": event.received_at - self._recording.started_at,
                        "received_at": event.received_at, "system": event.system, "component": event.component,
                        "sequence": event.sequence, "message_id": event.message_id, "type_name": event.type_name,
                        "fields": json_fields(event.fields), "frame_hex": event.frame.hex(),
                        "frame_bytes": len(event.frame), "wire_version": 1 if event.frame[0] == 0xfe else 2,
                    })
                total += 1
            return {
                "id": identifier, "revision": self._revision, "total": total,
                "offset": offset, "limit": limit, "items": items,
                "message_types": [{"message_id": kind, "type_name": name, "count": count}
                                  for (kind, name), count in sorted(counts.items())],
            }

    def download(self, identifier, revision=None):
        with self._lock:
            self._load(identifier)
            if revision is not None and revision != self._revision:
                raise ArchiveError("Le fichier a changé ; ouvrez à nouveau le journal", 409)
            # Send exactly the validated bytes, never reopen a mutable path.
            return self._data
