"""Bounded, grouped reception incidents; no inference about radio or sensor health.

Value freshness remains immediate in the caches/browser. Only incident notices
are debounced, so a brief scheduling gap does not produce a stream of alerts.
An incident survives a receiver reopen until actual receipts resume.
"""

LABELS = {"heartbeat": "mode", "battery": "batterie", "attitude": "attitude",
          "local_position_ned": "position locale"}


class ReceptionIncidents:
    def __init__(self):
        self._active = {}
        self._pending = {}
        self._recovering = {}
        self._seen = set()
        self._next_id = 0
        self._last_recovery = None

    def _candidate(self, source, data):
        state = data["state"]
        if state == "unconfigured":
            return None
        if source == "video":
            if state == "recent":
                return None
            title = {"error": "Caméra indisponible", "waiting": "Image attendue",
                     "stale": "Images interrompues", "reconnecting": "Réouverture caméra"}[state]
            return title, data["detail"], ["video"], state
        self._seen.update(name for name in LABELS if data[name]["fields"] is not None)
        if state == "error":
            return "Réception MAVLink interrompue", data["detail"], list(LABELS), state
        if state == "reconnecting":
            return "Réouverture MAVLink", data["detail"], list(self._seen), state
        missing = [name for name in LABELS if name in self._seen and data[name]["state"] != "recent"]
        if missing:
            title = "Télémétrie périmée" if not any(data[name]["state"] == "recent" for name in LABELS) else "Télémétrie partielle"
            detail = "Sans réception valide récente : " + ", ".join(LABELS[name] for name in missing) + "."
            return title, detail, missing, "stale"
        if state == "waiting":
            return "Télémétrie attendue", data["detail"], [], state
        return None

    def update(self, now, video, telemetry):
        events = []
        for source, data in (("video", video), ("mavlink", telemetry)):
            candidate = self._candidate(source, data)
            active = self._active.get(source)
            if candidate is None:
                self._pending.pop(source, None)
                if active is None:
                    continue
                since = self._recovering.setdefault(source, now)
                active["state"] = "recovering"
                if now - since >= 1.:
                    duration = max(0., since - active["since"])
                    self._last_recovery = {"source": source, "at": now, "duration_s": duration}
                    label = "Images reçues à nouveau" if source == "video" else "Réceptions MAVLink rétablies"
                    events.append({"at": now, "level": "info", "message": f"{label} · interruption observée {duration:.1f} s"})
                    del self._active[source]
                    self._recovering.pop(source, None)
                continue

            self._recovering.pop(source, None)
            title, detail, affected, state = candidate
            # Reopening itself is an operator action, not evidence of a failure.
            # An existing incident stays open while the receiver is replaced.
            if state == "reconnecting" and active is None:
                self._pending.pop(source, None)
                continue
            since = self._pending.setdefault(source, now)
            debounce = 0. if state == "error" else 3. if state == "waiting" else 1.
            if active is None and now - since < debounce:
                continue
            if active is None:
                self._next_id += 1
                active = {"id": self._next_id, "source": source, "since": since}
                self._active[source] = active
                events.append({"at": now, "level": "error" if state == "error" else "warning",
                               "message": f"{title} · {detail}"})
            active.update(title=title, detail=detail, affected=affected, state="active")
        return events

    def snapshot(self):
        return {"active": [{**item, "affected": list(item["affected"])} for item in self._active.values()],
                "last_recovery": dict(self._last_recovery) if self._last_recovery else None}
