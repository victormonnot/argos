"""Console-to-bench boundary: clocks, immutable selection and bounded HTTP."""
from dataclasses import FrozenInstanceError
from http.client import BadStatusLine
import json

import pytest

from argos.backends import vision_bench_source as source


def snapshot(*, at=10., received=9.9, sequence=42):
    return {
        "schema_version": 1, "at": at, "run_id": "run-one", "environment": "real",
        "reconnecting": None,
        "configuration": {"environment": "real", "video_source": "device",
                          "video_endpoint": "/dev/video3"},
        "video": {"source_id": "camera-one", "source": "device", "state": "recent",
                  "endpoint": "/dev/video3", "received_at": received,
                  "rx_age_s": at - received, "age_limit_s": 1.},
        "vision": {"configured": True, "state": "recent", "frame_age_s": at - received,
                   "age_limit_s": 1., "inference_ms": 100.},
        "yaw_preview": {"enabled": True, "phase": "tracking", "revision": 1,
                        "target_id": 7, "run_id": "run-one", "video_id": "camera-one",
                        "frame_sequence": sequence, "frame_received_at": received,
                        "frame_age_s": at - received, "frame_max_age_s": .45,
                        "error_x": .4, "yaw": .1, "yaw_limit": .125},
    }


def test_deadline_charges_entire_request_and_value_is_immutable():
    result = source.PreviewValidator().validate(snapshot(), 100., 100.04)
    assert result.value == 102
    assert result.deadline == pytest.approx(100.35)
    assert result.received_at == 9.9
    assert result.frame_sequence == 42 and result.target_id == 7 and result.revision == 1
    assert result.inference_ms == 100.
    with pytest.raises(FrozenInstanceError):
        result.value = 128


@pytest.mark.parametrize(("yaw", "expected"), [(-.125, -128), (0., 0), (.125, 128)])
def test_normalized_yaw_conversion(yaw, expected):
    state = snapshot()
    state["yaw_preview"]["yaw"] = yaw
    assert source.PreviewValidator().validate(state, 100., 100.01).value == expected


def test_same_image_can_be_read_but_never_renews_its_deadline():
    validator = source.PreviewValidator()
    first = validator.validate(snapshot(), 100., 100.04)
    # The remote clock advances less than the local clock: preserve the first
    # image deadline even when a naive conversion would grant extra lifetime.
    second = validator.validate(snapshot(at=10.05), 100.20, 100.21)
    assert second.deadline == first.deadline
    assert second.frame_sequence == first.frame_sequence
    with pytest.raises(source.PreviewError, match="expired"):
        validator.validate(snapshot(at=10.10), 100.36, 100.37)


def test_new_image_extends_deadline_without_changing_selection():
    validator = source.PreviewValidator()
    first = validator.validate(snapshot(), 100., 100.01)
    second = validator.validate(snapshot(at=10.2, received=10.1, sequence=43), 100.2, 100.21)
    assert second.deadline > first.deadline
    assert (second.target_id, second.revision, second.run_id, second.video_id) == (
        first.target_id, first.revision, first.run_id, first.video_id)


@pytest.mark.parametrize("path,value", [
    (("environment",), "simulation"),
    (("schema_version",), True),
    (("schema_version",), 2),
    (("configuration", "environment"), "simulation"),
    (("configuration", "video_source"), "gazebo"),
    (("configuration", "video_endpoint"), "http://camera"),
    (("video", "endpoint"), "/dev/video2"),
    (("video", "source"), "gazebo"),
    (("video", "state"), "waiting"),
    (("video", "source_id"), "other"),
    (("video", "received_at"), 10.1),
    (("video", "received_at"), 9.8),
    (("video", "rx_age_s"), -.01),
    (("video", "rx_age_s"), .46),
    (("video", "age_limit_s"), .01),
    (("vision", "configured"), 1),
    (("vision", "state"), "error"),
    (("vision", "frame_age_s"), .46),
    (("vision", "inference_ms"), float("nan")),
    (("reconnecting",), "video"),
    (("at",), float("nan")),
    (("at",), True),
    (("at",), -1),
    (("yaw_preview", "enabled"), 1),
    (("yaw_preview", "phase"), "stopped"),
    (("yaw_preview", "run_id"), "different"),
    (("yaw_preview", "video_id"), ""),
    (("yaw_preview", "target_id"), 0),
    (("yaw_preview", "target_id"), True),
    (("yaw_preview", "target_id"), 2**53),
    (("yaw_preview", "target_id"), 7.),
    (("yaw_preview", "revision"), -1),
    (("yaw_preview", "revision"), False),
    (("yaw_preview", "frame_sequence"), 0),
    (("yaw_preview", "frame_sequence"), 2**53),
    (("yaw_preview", "frame_received_at"), 10.01),
    (("yaw_preview", "frame_received_at"), -1),
    (("yaw_preview", "frame_age_s"), .46),
    (("yaw_preview", "frame_max_age_s"), .451),
    (("yaw_preview", "frame_max_age_s"), 0.),
    (("yaw_preview", "frame_max_age_s"), True),
    (("yaw_preview", "yaw_limit"), .126),
    (("yaw_preview", "yaw_limit"), 0),
    (("yaw_preview", "yaw"), .126),
    (("yaw_preview", "yaw"), -.126),
    (("yaw_preview", "yaw"), True),
    (("yaw_preview", "yaw"), float("inf")),
    (("yaw_preview", "error_x"), 1.01),
    (("yaw_preview", "error_x"), float("nan")),
])
def test_invalid_snapshot_permanently_stops_reader(path, value):
    state = snapshot()
    node = state
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    validator = source.PreviewValidator()
    with pytest.raises(source.PreviewError):
        validator.validate(state, 100., 100.01)
    assert validator.failed
    with pytest.raises(source.PreviewError, match="stopped"):
        validator.validate(snapshot(), 100.1, 100.11)


@pytest.mark.parametrize("bad", [None, [], {}, {"schema_version": 1}])
def test_bad_structure_is_latched(bad):
    validator = source.PreviewValidator()
    with pytest.raises(source.PreviewError):
        validator.validate(bad, 100., 100.01)
    assert validator.failed


@pytest.mark.parametrize("started,finished", [
    (100., 99.9), (100., 100.151), (False, 100.),
    (100., float("nan")), (100., float("inf")), (-1., 0.)])
def test_bad_request_clock_is_latched(started, finished):
    validator = source.PreviewValidator()
    with pytest.raises(source.PreviewError):
        validator.validate(snapshot(), started, finished)
    assert validator.failed


def test_local_clock_regression_between_requests_is_rejected():
    validator = source.PreviewValidator()
    validator.validate(snapshot(), 100., 100.01)
    with pytest.raises(source.PreviewError, match="Local clock"):
        validator.validate(snapshot(at=10.1), 100., 100.02)


@pytest.mark.parametrize("new_at", [10., 9.99])
def test_frozen_or_regressing_server_clock_cannot_renew_image(new_at):
    validator = source.PreviewValidator()
    validator.validate(snapshot(), 100., 100.01)
    with pytest.raises(source.PreviewError, match="did not advance"):
        validator.validate(snapshot(at=new_at), 100.1, 100.11)


@pytest.mark.parametrize("field,value", [
    ("target_id", 8), ("revision", 2), ("run_id", "new-run"), ("video_id", "new-camera"),
    ("yaw", -.1), ("error_x", -.4), ("frame_received_at", 9.95),
    ("frame_max_age_s", .44), ("yaw_limit", .124), ("frame_sequence", 41)])
def test_changed_selection_or_same_image_metadata_is_rejected(field, value):
    validator = source.PreviewValidator()
    validator.validate(snapshot(), 100., 100.01)
    state = snapshot(at=10.1)
    state["yaw_preview"][field] = value
    if field == "run_id":
        state["run_id"] = value
    elif field == "video_id":
        state["video"]["source_id"] = value
    with pytest.raises(source.PreviewError):
        validator.validate(state, 100.1, 100.11)
    assert validator.failed


def test_new_sequence_requires_strictly_newer_receipt():
    validator = source.PreviewValidator()
    validator.validate(snapshot(), 100., 100.01)
    with pytest.raises(source.PreviewError, match="newer receipt"):
        validator.validate(snapshot(at=10.1, sequence=43), 100.1, 100.11)


def test_preview_underreported_age_does_not_hide_stale_receipt():
    state = snapshot(at=10.5, received=9.9)
    state["yaw_preview"]["frame_age_s"] = 0.
    state["vision"]["frame_age_s"] = 0.
    state["video"].update(received_at=10.5, rx_age_s=0.)
    with pytest.raises(source.PreviewError, match="expired"):
        source.PreviewValidator().validate(state, 100., 100.01)


def test_rtt_can_expire_otherwise_fresh_image():
    with pytest.raises(source.PreviewError, match="expired"):
        source.PreviewValidator().validate(snapshot(at=10.25), 100., 100.11)


def test_optional_inference_duration_is_not_a_freshness_clock():
    state = snapshot()
    state["vision"]["inference_ms"] = None
    assert source.PreviewValidator().validate(state, 100., 100.01).inference_ms is None


def test_real_console_api_snapshot_satisfies_bench_contract(tmp_path):
    """Exercise production schema and selection with fabricated camera pixels."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from pathlib import Path
    from fastapi.testclient import TestClient
    from argos.console.app import create_app
    from argos.console.config import ConsoleConfig
    from argos.console.session import ConsoleSession
    from argos.console.vision import AnalyzedFrame, VisionService

    config = ConsoleConfig(environment="real", video_source="device",
        video_endpoint="/dev/video3", vision_model=Path("synthetic.onnx"), recordings_dir=tmp_path)
    session = ConsoleSession(config, clock=lambda: 10.)
    session._started = True  # No application lifespan: no camera or detector process.
    vision = VisionService(config.vision_model)
    vision._ready = True
    vision._context = session.run_id, session.video_source_id
    client = TestClient(create_app(session=session, vision=vision))
    try:
        session.video.accept_raw(width=2, height=2, step=6, pixel_format="RGB_INT8",
                                 data=bytes(12), received_at=9.9)
        sample = session.video.latest(10.)
        vision._frame = AnalyzedFrame(sample, vision._context, {
            "width": 2, "height": 2, "inference_ms": 100.,
            "detections": [{"track_id": 7, "box": [.6, .2, .2, .5], "confidence": .9}],
        })
        vision._selection_history.append((vision._context, sample.sequence, 9.9, frozenset({7})))
        selected = client.post("/api/vision/yaw-preview", headers={"origin": "http://testserver"},
            json={"action": "select", "run_id": session.run_id,
                  "video_id": session.video_source_id, "frame_sequence": sample.sequence,
                  "track_id": 7, "revision": 0})
        assert selected.status_code == 200, selected.text
        response = client.get("/api/state")
        assert response.status_code == 200
        result = source.PreviewValidator().validate(response.json(), 100., 100.02)
        assert result.value == 102 and result.target_id == 7
        assert result.inference_ms == 100.
        assert result.deadline == pytest.approx(100.35)
        assert result.run_id == session.run_id and result.video_id == session.video_source_id
    finally:
        client.close()
        session.close()


class Response:
    def __init__(self, body=None, *, status=200, headers=None):
        self.body = json.dumps(snapshot()).encode() if body is None else body
        self.status = status
        self.headers = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
        self.requests = []

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, amount):
        self.requests.append(amount)
        return self.body[:amount]


@pytest.fixture
def http(monkeypatch):
    class Connection:
        response = Response()
        made = []
        failure = None

        def __init__(self, host, port, *, timeout):
            self.host, self.port, self.timeout = host, port, timeout
            self.sent, self.closed = [], False
            self.made.append(self)

        def request(self, method, path, *, headers):
            self.sent.append((method, path, headers))
            if self.failure is not None:
                raise self.failure

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    monkeypatch.setattr(source.http.client, "HTTPConnection", Connection)
    return Connection


def ticks(*times):
    iterator = iter(times)
    return lambda: next(iterator)


def test_http_gets_only_loopback_state_and_closes_connection(http, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://external.invalid:8080")
    reader = source.PreviewSource(port=8181, clock=ticks(100., 100.04, 100.041))
    result = reader.read()
    connection, = http.made
    assert (connection.host, connection.port, connection.timeout) == ("127.0.0.1", 8181, .15)
    assert [(method, path) for method, path, _ in connection.sent] == [("GET", "/api/state")]
    assert http.response.requests == [source.MAX_BODY + 1]
    assert connection.closed
    assert result.value == 102
    assert result.deadline == pytest.approx(100.35)
    reader.close()
    with pytest.raises(source.PreviewError, match="stopped"):
        reader.read()
    assert len(http.made) == 1


@pytest.mark.parametrize("port", [0, 65536, -1, True, 8080., "8080", "http://example.com"])
def test_console_accepts_only_integer_port(port):
    with pytest.raises(source.PreviewError):
        source.PreviewSource(port=port)


@pytest.mark.parametrize("response", [
    Response(status=302, headers={"Location": "http://external.invalid/"}),
    Response(status=404),
    Response(headers={"Content-Type": "text/html"}),
    Response(headers={"Content-Encoding": "gzip"}),
    Response(headers={"Content-Length": str(source.MAX_BODY + 1)}),
    Response(headers={"Content-Length": "-1"}),
    Response(headers={"Content-Length": "1.5"}),
    Response(headers={"Content-Length": "９"}),
    Response(headers={"Content-Length": "100000"}),
    Response(body=b"x" * (source.MAX_BODY + 1)),
    Response(body=b"not JSON"),
    Response(body=b"\xff"),
    Response(body=b'{"a": 1, "a": 2}'),
    Response(body=b'{"a": {"b": 1, "b": 2}}'),
    Response(body=b'{"a": NaN}'),
    Response(body=b'{"a": Infinity}'),
    Response(body=b'{"a": 1e9999}'),
    Response(body=b"[" * 2000 + b"]" * 2000),
    Response(body=b"[]"),
])
def test_bad_http_response_is_terminal_and_not_redirected(http, response):
    http.response = response
    reader = source.PreviewSource(clock=lambda: 100.)
    with pytest.raises(source.PreviewError):
        reader.read()
    assert http.made[0].closed
    with pytest.raises(source.PreviewError, match="stopped"):
        reader.read()
    assert len(http.made) == 1


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), ConnectionRefusedError(),
                                   BadStatusLine("broken")])
def test_http_failures_are_terminal_and_close_connection(http, failure):
    http.failure = failure
    reader = source.PreviewSource(clock=lambda: 100.)
    with pytest.raises(source.PreviewError, match="read failed"):
        reader.read()
    assert reader.validator.failed and http.made[0].closed


@pytest.mark.parametrize("times", [
    (100., 100.151), (100., 99.), (100., 100.01, 100.151),
    (100., 100.01, 100.), (100., 100.01, float("nan"))])
def test_total_http_and_validation_budget_is_checked_before_return(http, times):
    reader = source.PreviewSource(clock=ticks(*times))
    with pytest.raises(source.PreviewError):
        reader.read()
    assert reader.validator.failed and http.made[0].closed
