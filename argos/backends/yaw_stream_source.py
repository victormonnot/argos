"""Read-only image demands for continuous yaw assistance.

The stream keeps selection identity through a short uncertain detection, but
withdraws authority (``valid=False``) during that gap. It does not send a valid
zero stick command as a substitute for returning manual control. HTTP is kept
outside the radio scheduler; callers publish only the newest immutable demand.
"""
from __future__ import annotations

from copy import copy
from dataclasses import dataclass
import http.client
import json
import time

from .vision_bench_source import (
    HTTP_TIMEOUT, MAX_BODY, PreviewError, PreviewValidator, _finite_float,
    _identity, _integer, _nonfinite, _number, _object, _unique_object,
)


RECOVERY_MAX_GAP = .7
RECOVERY_IMAGES = 2


@dataclass(frozen=True)
class YawDemand:
    value: int
    valid: bool
    selection_key: tuple | None
    deadline: float
    reason: str


class YawValidator:
    """Validate fresh demands without changing the fail-stop diagnostic bench.

    The legacy image boundary is reused on a copy, substituting only the stable
    selection epoch for its per-observation revision. Failed reads never erase
    the last accepted image's immutable deadline/order checks. Two distinct
    strong images are required after an observed or missed pause.
    """

    def __init__(self):
        self._key = None
        self._validator = None
        self._revision = None
        self._recovering = False
        self._strong_count = 0
        self._strong_sequence = None
        self._recovery_deadline = None
        self._last_strong_receipt = None

    def validate(self, snapshot, started, finished):
        try:
            return self._validate(snapshot, started, finished)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, PreviewError):
                raise
            raise PreviewError("Invalid continuous yaw snapshot") from exc

    def _validate(self, snapshot, started, finished):
        started = _number(started, "local request time")
        finished = _number(finished, "local response time")
        if not 0 <= finished - started <= HTTP_TIMEOUT:
            raise PreviewError("Console request exceeded its time budget or clock moved backwards")
        state = _object(snapshot, "console snapshot")
        if type(state["schema_version"]) is not int or state["schema_version"] != 1:
            raise PreviewError("Unsupported console schema")
        config = _object(state["configuration"], "console configuration")
        preview = _object(state["yaw_preview"], "yaw preview")
        if state["environment"] != "real" or config["environment"] != "real":
            raise PreviewError("A physical camera in the real environment is required")
        epoch = _integer(preview["selection_epoch"], "selection epoch", minimum=0)
        phase = preview["phase"]
        if preview["enabled"] is not True or phase in ("idle", "disabled", "stopped"):
            # An explicit selection is needed; do not retain identity from an
            # older target through a clear, source reset or terminal expiry.
            self._key = None
            self._validator = None
            self._revision = None
            self._recovering = False
            self._recovery_deadline = None
            self._last_strong_receipt = None
            return YawDemand(0, False, None, finished, "selection_required")
        if phase not in ("tracking", "paused"):
            raise PreviewError("Invalid continuous yaw phase")
        key = (_identity(preview["run_id"], "preview run"),
               _identity(preview["video_id"], "preview camera"),
               _integer(preview["target_id"], "selected person"), epoch,
               _identity(config["video_endpoint"], "camera endpoint"))
        revision = _integer(preview["revision"], "preview revision", minimum=0)
        same_selection = key == self._key
        if same_selection and self._revision is not None and revision < self._revision:
            raise PreviewError("Preview revision moved backwards")
        at = _number(state["at"], "server snapshot time")
        projected = dict(state)
        projected_preview = dict(preview, revision=epoch)
        projected["yaw_preview"] = projected_preview
        recovery_deadline = self._recovery_deadline if same_selection else None
        last_strong = self._last_strong_receipt if same_selection else None
        if phase == "paused":
            if preview["yaw"] != 0 or preview["error_x"] is not None:
                raise PreviewError("An uncertain target must withdraw its correction")
            declared = _number(preview["recovery_deadline_at"], "target recovery deadline")
            if not 0 < declared - at <= RECOVERY_MAX_GAP:
                raise PreviewError("Target recovery window expired or is invalid")
            recovery_deadline = min(declared, recovery_deadline or declared)
            if last_strong is not None:
                recovery_deadline = min(recovery_deadline, last_strong + RECOVERY_MAX_GAP)
            if at >= recovery_deadline:
                raise PreviewError("Target recovery window expired")
            # Only the projection enters the strict image validator. The
            # resulting demand remains invalid and never grants authority.
            projected_preview.update(phase="tracking", error_x=0.)
        validator = (copy(self._validator) if same_selection and self._validator is not None
                     else PreviewValidator())
        checked = validator.validate(projected, started, finished)

        missed_pause = (same_selection and self._revision is not None
                        and revision != self._revision)
        recovering = self._recovering if same_selection else False
        strong_count = self._strong_count if same_selection else 0
        strong_sequence = self._strong_sequence if same_selection else None
        if phase == "paused" or missed_pause:
            recovering, strong_count, strong_sequence = True, 0, None
        valid, reason = phase == "tracking", "tracking"
        if phase == "paused":
            valid, reason = False, "target_paused"
        elif recovering:
            if recovery_deadline is not None and at >= recovery_deadline:
                raise PreviewError("Target returned after its recovery window")
            if checked.frame_sequence != strong_sequence:
                strong_count += 1
                strong_sequence = checked.frame_sequence
            if strong_count < RECOVERY_IMAGES:
                valid, reason = False, "target_recovering"
            else:
                recovering = False
                recovery_deadline = None
        if phase == "tracking":
            last_strong = checked.received_at
        self._key, self._validator, self._revision = key, validator, revision
        self._recovering, self._strong_count = recovering, strong_count
        self._strong_sequence = strong_sequence
        self._recovery_deadline = recovery_deadline
        self._last_strong_receipt = last_strong
        return YawDemand(checked.value if valid else 0, valid, key,
                         checked.deadline if valid else finished, reason)


class YawSource:
    """Bounded loopback JSON reads; transient read failures raise PreviewError.

    A caller may keep polling after an error, but must immediately publish an
    invalid demand. Error recovery never extends a previously read image's
    deadline. ``close`` is permanent; there is no reconnect to a radio here.
    """

    def __init__(self, port=8080, *, clock=time.monotonic):
        if type(port) is not int or not 1 <= port <= 65535:
            raise PreviewError("Console port must be an integer from 1 to 65535")
        self.port, self.clock = port, clock
        self.validator = YawValidator()
        self._connection = None
        self._closed = False

    def read(self):
        if self._closed:
            raise PreviewError("Continuous yaw source is closed")
        started = self.clock()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=HTTP_TIMEOUT)
        self._connection = connection
        try:
            connection.request("GET", "/api/state", headers={
                "Accept": "application/json", "Cache-Control": "no-cache", "Connection": "close"})
            response = connection.getresponse()
            if response.status != 200:
                raise PreviewError(f"Console returned HTTP {response.status}; no redirects followed")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise PreviewError("Console response is not JSON")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise PreviewError("Encoded console responses are not accepted")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isascii() or not length.isdigit()
                                       or int(length) > MAX_BODY):
                raise PreviewError("Invalid or oversized console response length")
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise PreviewError("Console response exceeds the byte limit")
            if length is not None and len(body) != int(length):
                raise PreviewError("Console response ended before its declared length")
            snapshot = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                                  parse_constant=_nonfinite, parse_float=_finite_float)
            finished = self.clock()
            result = self.validator.validate(snapshot, started, finished)
            checked_at = _number(self.clock(), "local completion time")
            if checked_at < finished or checked_at - started > HTTP_TIMEOUT:
                raise PreviewError("Console read exceeded its time budget or clock moved backwards")
            if result.valid and checked_at >= result.deadline:
                raise PreviewError("Console image expired while validating the response")
            return result
        except (OSError, http.client.HTTPException, ValueError, TypeError,
                OverflowError, RecursionError) as exc:
            if isinstance(exc, PreviewError):
                raise
            raise PreviewError(f"Console yaw read failed: {exc}") from exc
        finally:
            connection.close()
            self._connection = None

    def close(self):
        self._closed = True
        if self._connection is not None:
            self._connection.close()
