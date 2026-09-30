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
import re
import time

from .vision_bench_source import (
    HTTP_TIMEOUT, MAX_BODY, PreviewError, PreviewValidator, _finite_float,
    _identity, _integer, _nonfinite, _number, _object, _unique_object,
)


RECOVERY_MAX_GAP = .7
CONTINUOUS_RECOVERY_MAX_GAP = 3.
RECOVERY_IMAGES = 2
MAX_YAW = .2
MAX_VALUE = 205  # round(20% * 1024); ArgVis remains limited to 128.


class _ContinuousPreviewValidator(PreviewValidator):
    """The continuous V3 transport's bound; legacy diagnostics stay separate."""

    MAX_YAW = MAX_YAW
    MAX_VALUE = MAX_VALUE


@dataclass(frozen=True)
class YawDemand:
    value: int
    valid: bool
    selection_key: tuple | None
    deadline: float
    reason: str
    # Diagnostic bounds on the host clock, not camera exposure timestamps.
    frame_sequence: int | None = None
    image_received_earliest: float | None = None
    image_received_latest: float | None = None


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
        self._target_id = None
        self._phase = None
        self._frame_timing = None

    def validate(self, snapshot, started, finished, *, explicit_selection=False):
        try:
            return self._validate(snapshot, started, finished, explicit_selection=explicit_selection)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, PreviewError):
                raise
            raise PreviewError("Invalid continuous yaw snapshot") from exc

    def _validate(self, snapshot, started, finished, *, explicit_selection):
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
        continuous = preview.get("continuous", False)
        if type(continuous) is not bool:
            raise PreviewError("Invalid continuous yaw mode")
        recovery_gap = CONTINUOUS_RECOVERY_MAX_GAP if continuous else RECOVERY_MAX_GAP
        if preview.get("recovery_max_gap_s", recovery_gap) != recovery_gap:
            raise PreviewError("Invalid target recovery limit")
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
            self._target_id = None
            self._phase = None
            self._frame_timing = None
            return YawDemand(0, False, None, finished, "selection_required")
        if phase not in ("tracking", "paused"):
            raise PreviewError("Invalid continuous yaw phase")
        target_id = _integer(preview["target_id"], "selected person")
        selection_id = (_integer(preview["selection_id"], "original selected person")
                        if continuous else target_id)
        key = (_identity(preview["run_id"], "preview run"),
               _identity(preview["video_id"], "preview camera"),
               selection_id, epoch,
               _identity(config["video_endpoint"], "camera endpoint"))
        revision = _integer(preview["revision"], "preview revision", minimum=0)
        same_selection = key == self._key
        if (same_selection and self._validator is not None
                and continuous != isinstance(self._validator, _ContinuousPreviewValidator)):
            raise PreviewError("Preview mode changed within a selection")
        if same_selection and self._revision is not None and revision < self._revision:
            raise PreviewError("Preview revision moved backwards")
        if (same_selection and target_id != self._target_id
                and revision == self._revision and not self._recovering):
            raise PreviewError("Target reassociation requires a recovery observation")
        at = _number(state["at"], "server snapshot time")
        projected = dict(state)
        projected_preview = dict(preview, revision=epoch, target_id=selection_id)
        projected["yaw_preview"] = projected_preview
        recovery_deadline = self._recovery_deadline if same_selection else None
        last_strong = self._last_strong_receipt if same_selection else None
        # A new pause after an accepted strong image is a new gap, even if
        # authority still awaited its second confirmation image. Anchor that
        # gap to the actual image receipt, never to polling or a weak image.
        if (same_selection and self._phase == "tracking" and revision != self._revision
                and last_strong is not None):
            recovery_deadline = last_strong + recovery_gap
        if phase == "paused":
            if preview["yaw"] != 0 or preview["error_x"] is not None:
                raise PreviewError("An uncertain target must withdraw its correction")
            declared = _number(preview["recovery_deadline_at"], "target recovery deadline")
            if not 0 < declared - at <= recovery_gap:
                raise PreviewError("Target recovery window expired or is invalid")
            recovery_deadline = min(declared, recovery_deadline or declared)
            if last_strong is not None:
                recovery_deadline = min(recovery_deadline, last_strong + recovery_gap)
            if at >= recovery_deadline:
                raise PreviewError("Target recovery window expired")
            # Only the projection enters the strict image validator. The
            # resulting demand remains invalid and never grants authority.
            projected_preview.update(phase="tracking", error_x=0.)
        validator = (copy(self._validator) if same_selection and self._validator is not None
                     else (_ContinuousPreviewValidator() if continuous else PreviewValidator()))
        checked = validator.validate(projected, started, finished)

        missed_pause = (same_selection and self._revision is not None
                        and revision != self._revision)
        recovering = self._recovering if same_selection else False
        strong_count = self._strong_count if same_selection else 0
        strong_sequence = self._strong_sequence if same_selection else None
        if explicit_selection and phase == "tracking":
            # Only the matching operator POST can clear an exhausted recovery.
            # The strict image validator above still checks receipt/order and
            # preserves an already observed image's immutable local deadline.
            recovering, strong_count, strong_sequence = False, 0, None
            recovery_deadline = None
        elif phase == "paused" or missed_pause:
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
        self._target_id = target_id
        self._phase = phase
        timing_key = key, checked.frame_sequence
        if self._frame_timing is None or self._frame_timing[0] != timing_key:
            # Snapshot generation occurred somewhere inside this HTTP request.
            # Subtract only a server-clock duration; never compare clock origins.
            age = at - checked.received_at
            self._frame_timing = timing_key, started - age, finished - age
        _, earliest, latest = self._frame_timing
        # Re-reading a frame preserves its original diagnostic receipt bounds,
        # just as the authority validator preserves its original deadline.
        return YawDemand(checked.value if valid else 0, valid, key,
                         checked.deadline if valid else finished, reason,
                         checked.frame_sequence, earliest, latest)


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
        self._context = None

    def read(self):
        return self._request("GET", "/api/vision/yaw-assist/state")

    def select_center(self, request_token):
        """Execute one explicit SC-cycle selection bound to the observed camera.

        No retry is performed; a delayed result is independently fenced by the
        radio session/generation at the stream owner.
        """
        if (not isinstance(request_token, str)
                or not re.fullmatch(r"[0-9a-f]{8}:(?:0|[1-9][0-9]{0,9})", request_token)
                or int(request_token.split(":")[1]) > 2**31 - 1):
            raise PreviewError("Invalid radio selection request token")
        if self._context is None:
            raise PreviewError("Read the current camera before selecting from the radio")
        return self._request("POST", "/api/vision/yaw-assist/select-center", {
            "request_id": request_token, "run_id": self._context[0], "video_id": self._context[1]})

    def _request(self, method, path, body=None):
        if self._closed:
            raise PreviewError("Continuous yaw source is closed")
        request_token = None if body is None else body["request_id"]
        requested_context = None if body is None else (body["run_id"], body["video_id"])
        started = self.clock()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=HTTP_TIMEOUT)
        self._connection = connection
        try:
            headers = {"Accept": "application/json", "Cache-Control": "no-cache", "Connection": "close"}
            if body is None:
                connection.request(method, path, headers=headers)
            else:
                headers["Content-Type"] = "application/json"
                headers["Origin"] = f"http://127.0.0.1:{self.port}"
                connection.request(method, path, body=json.dumps(body), headers=headers)
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
            if method == "POST":
                if (not isinstance(snapshot, dict) or set(snapshot) != {"request_id", "state"}
                        or snapshot["request_id"] != request_token):
                    raise PreviewError("Console selection response does not match its radio request")
                snapshot = snapshot["state"]
                if (snapshot["run_id"], snapshot["video"]["source_id"]) != requested_context:
                    raise PreviewError("Camera changed during radio target selection")
            finished = self.clock()
            validator = copy(self.validator)
            result = validator.validate(snapshot, started, finished,
                                        explicit_selection=method == "POST")
            checked_at = _number(self.clock(), "local completion time")
            if checked_at < finished or checked_at - started > HTTP_TIMEOUT:
                raise PreviewError("Console read exceeded its time budget or clock moved backwards")
            if result.valid and checked_at >= result.deadline:
                raise PreviewError("Console image expired while validating the response")
            context = (_identity(snapshot["run_id"], "console run"),
                       _identity(snapshot["video"]["source_id"], "camera source"))
            if method == "POST" and not result.valid:
                raise PreviewError("Radio selection did not produce a fresh visible target")
            self.validator, self._context = validator, context
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
