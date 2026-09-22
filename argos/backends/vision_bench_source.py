"""Read-only loopback preview source for the RF-off Pocket bench.

The console's session clock and this process's monotonic clock have different
origins. Convert only a *remaining lifetime*, charging the entire HTTP round
trip against that lifetime. Re-reading an image can never extend its deadline.
This module neither sends radio commands nor runs a detector.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import math
import re
import time


MAX_BODY = 256 * 1024
HTTP_TIMEOUT = .15
MAX_FRAME_AGE = .45
MAX_YAW = .125
MAX_SAFE_INTEGER = 2**53 - 1


class PreviewError(ValueError):
    """The selected preview is unavailable or no longer safe to reuse."""


@dataclass(frozen=True)
class PreviewValue:
    value: int
    deadline: float
    frame_sequence: int
    target_id: int
    revision: int
    run_id: str
    video_id: str
    received_at: float
    inference_ms: float | None


def _number(value, name, *, minimum=0., maximum=None):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if (not valid or value < minimum
            or (maximum is not None and value > maximum)):
        raise PreviewError(f"Invalid {name}")
    return float(value)


def _integer(value, name, *, minimum=1):
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise PreviewError(f"Invalid {name}")
    return value


def _identity(value, name):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise PreviewError(f"Invalid {name}")
    return value


def _object(value, name):
    if not isinstance(value, dict):
        raise PreviewError(f"Invalid {name}")
    return value


def _recent_age(value, limit, name):
    age = _number(value, name)
    bound = _number(limit, f"{name} limit")
    if bound <= 0 or age > min(bound, MAX_FRAME_AGE):
        raise PreviewError(f"Stale {name}")
    return age


class PreviewValidator:
    """One immutable selection, permanently stopped on its first invalid read."""

    def __init__(self):
        self.failed = False
        self._identity = None
        self._previous = None
        self._previous_metadata = None
        self._server_at = None
        self._finished = None

    def validate(self, snapshot, started, finished):
        if self.failed:
            raise PreviewError("Preview source has stopped; start a new bench")
        try:
            return self._validate(snapshot, started, finished)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self.failed = True
            if isinstance(exc, PreviewError):
                raise
            raise PreviewError("Invalid console preview snapshot") from exc

    def _validate(self, snapshot, started, finished):
        started = _number(started, "local request time")
        finished = _number(finished, "local response time")
        elapsed = finished - started
        if not 0 <= elapsed <= HTTP_TIMEOUT:
            raise PreviewError("Console request exceeded its time budget or clock moved backwards")
        if self._finished is not None and started < self._finished:
            raise PreviewError("Local clock moved backwards")

        state = _object(snapshot, "console snapshot")
        if type(state["schema_version"]) is not int or state["schema_version"] != 1:
            raise PreviewError("Unsupported console schema")
        config = _object(state["configuration"], "console configuration")
        video = _object(state["video"], "video state")
        vision = _object(state["vision"], "vision state")
        preview = _object(state["yaw_preview"], "yaw preview")
        if (state["environment"] != "real" or config["environment"] != "real"
                or config["video_source"] != "device" or video["source"] != "device"
                or video["state"] != "recent" or state.get("reconnecting") is not None):
            raise PreviewError("A recent physical camera in the real environment is required")
        endpoint = config["video_endpoint"]
        if (not isinstance(endpoint, str) or not re.fullmatch(r"/dev/video[0-9]+", endpoint)
                or video["endpoint"] != endpoint):
            raise PreviewError("Physical camera identity does not match")
        if vision["configured"] is not True or vision["state"] != "recent":
            raise PreviewError("Recent person detection is required")
        if preview["enabled"] is not True or preview["phase"] != "tracking":
            raise PreviewError("Select a person in Yaw preview before starting the bench")

        at = _number(state["at"], "server snapshot time")
        if self._server_at is not None and at <= self._server_at:
            raise PreviewError("Console snapshot time did not advance")
        run_id = _identity(preview["run_id"], "preview run")
        video_id = _identity(preview["video_id"], "preview camera")
        if run_id != state["run_id"] or video_id != video["source_id"]:
            raise PreviewError("Preview does not belong to the current console camera")
        target_id = _integer(preview["target_id"], "selected person")
        revision = _integer(preview["revision"], "preview revision", minimum=0)
        identity = run_id, video_id, target_id, revision, endpoint
        if self._identity is not None and identity != self._identity:
            raise PreviewError("Selected person or camera changed; start a new bench")

        sequence = _integer(preview["frame_sequence"], "image sequence")
        received_at = _number(preview["frame_received_at"], "image receipt")
        age = _number(preview["frame_age_s"], "preview image age")
        limit = _number(preview["frame_max_age_s"], "preview age limit", maximum=MAX_FRAME_AGE)
        if limit <= 0 or received_at > at:
            raise PreviewError("Invalid preview image receipt or age limit")
        video_received = _number(video["received_at"], "camera receipt")
        video_age = _recent_age(video["rx_age_s"], video["age_limit_s"], "camera image age")
        vision_age = _recent_age(vision["frame_age_s"], vision["age_limit_s"], "detector image age")
        if video_received > at or video_received < received_at:
            raise PreviewError("Camera receipt is inconsistent with the preview image")
        if at - video_received > min(MAX_FRAME_AGE, video["age_limit_s"]):
            raise PreviewError("Camera image receipt is stale")
        age = max(age, at - received_at, video_age, vision_age)
        cap = _number(preview["yaw_limit"], "yaw limit", maximum=MAX_YAW)
        if cap <= 0:
            raise PreviewError("Invalid yaw limit")
        yaw = _number(preview["yaw"], "proposed yaw", minimum=-cap, maximum=cap)
        error = _number(preview["error_x"], "horizontal error", minimum=-1., maximum=1.)
        inference = vision["inference_ms"]
        if inference is not None:
            inference = _number(inference, "detector duration")
        metadata = received_at, yaw, error, cap, limit, inference
        deadline = finished + (limit - age - elapsed)
        previous = self._previous
        if previous is not None:
            if sequence < previous.frame_sequence:
                raise PreviewError("Analyzed image sequence moved backwards")
            if sequence == previous.frame_sequence:
                if metadata != self._previous_metadata:
                    raise PreviewError("The same analyzed image changed its metadata")
                deadline = min(deadline, previous.deadline)
            elif received_at <= previous.received_at:
                raise PreviewError("A new analyzed image must have a newer receipt")
        if deadline <= finished:
            raise PreviewError("Selected image expired before the console read completed")
        value = int(round(yaw * 1024))
        if not -128 <= value <= 128:
            raise PreviewError("Proposed yaw exceeds the RF-off bench limit")
        result = PreviewValue(value, deadline, sequence, target_id, revision,
                              run_id, video_id, received_at, inference)
        self._identity, self._previous = identity, result
        self._previous_metadata = metadata
        self._server_at, self._finished = at, finished
        return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreviewError("Duplicate key in console JSON")
        result[key] = value
    return result


def _nonfinite(token):
    raise PreviewError(f"Nonfinite number in console JSON: {token}")


def _finite_float(token):
    value = float(token)
    if not math.isfinite(value):
        _nonfinite(token)
    return value


class PreviewSource:
    """GET /api/state from IPv4 loopback only, without redirects or proxies.

    Socket timeouts bound individual blocking operations, not OS scheduling or
    an entire slow-header response. A second elapsed-time check rejects any
    complete response that exceeded the total budget, before returning a value.
    """

    def __init__(self, port=8080, *, clock=time.monotonic):
        if type(port) is not int or not 1 <= port <= 65535:
            raise PreviewError("Console port must be an integer from 1 to 65535")
        self.port, self.clock = port, clock
        self.validator = PreviewValidator()
        self._connection = None
        self._closed = False

    def read(self):
        if self._closed or self.validator.failed:
            raise PreviewError("Preview source has stopped; start a new bench")
        try:
            started = self.clock()
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=HTTP_TIMEOUT)
            self._connection = connection
            connection.request("GET", "/api/state", headers={
                "Accept": "application/json", "Cache-Control": "no-cache", "Connection": "close"})
            response = connection.getresponse()
            if response.status != 200:
                raise PreviewError(f"Console returned HTTP {response.status}; no redirects followed")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise PreviewError("Console response is not JSON")
            encoding = response.getheader("Content-Encoding", "identity")
            if encoding.lower() != "identity":
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
            result = self.validator.validate(snapshot, started, self.clock())
            checked_at = _number(self.clock(), "local completion time")
            if checked_at < started or checked_at < self.validator._finished:
                raise PreviewError("Local clock moved backwards")
            if checked_at - started > HTTP_TIMEOUT or checked_at >= result.deadline:
                raise PreviewError("Console preview expired while validating the response")
            self.validator._finished = checked_at
            return result
        except (OSError, http.client.HTTPException, ValueError, TypeError,
                OverflowError, RecursionError) as exc:
            self.validator.failed = True
            if isinstance(exc, PreviewError):
                raise
            raise PreviewError(f"Console preview read failed: {exc}") from exc
        finally:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def close(self):
        self._closed = True
        if self._connection is not None:
            self._connection.close()
            self._connection = None
