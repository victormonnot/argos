"""Camera admission and lifetime tests; synthetic frames exist only here."""

from dataclasses import FrozenInstanceError
import io
import sys
from threading import Event, Thread
from types import SimpleNamespace

import numpy as np
import pytest

from argos.console import video
from argos.console.video import DeviceCamera, GazeboCamera, VideoStore

Image = pytest.importorskip("PIL.Image")


def store(**kwargs):
    return VideoStore(source="gazebo", endpoint="/camera/image", clock=lambda: 10., **kwargs)


def accept(target, at=10., **kwargs):
    args = dict(width=2, height=2, step=6, pixel_format="RGB_INT8",
                data=bytes([255, 0, 0]) * 4, received_at=at)
    args.update(kwargs)
    return target.accept_raw(**args)


def pixel(target, at=10.):
    return Image.open(io.BytesIO(target.latest(at).jpeg)).convert("RGB").getpixel((0, 0))


def test_only_explicit_camera_sources_are_accepted():
    for source, endpoint in [("demo", None), ("none", "/camera"),
                             ("device", "0"), ("device", "https://example.org/camera"),
                             ("device", "/tmp/video.mp4"), ("gazebo", "camera"),
                             ("gazebo", "https://example.org/camera")]:
        with pytest.raises(ValueError):
            VideoStore(source=source, endpoint=endpoint)
    assert VideoStore(source="none").snapshot(0.)["state"] == "unconfigured"
    assert store().snapshot(0.)["state"] == "waiting"


@pytest.mark.parametrize("age", [0., -1., True, float("nan"), float("inf")])
def test_age_limit_must_be_positive_finite(age):
    with pytest.raises(ValueError):
        store(age_limit=age)


def test_freshness_belongs_to_acquisition_not_http_reads_or_identical_pixels():
    target = store(age_limit=1.)
    assert accept(target)
    first = target.latest(10.)
    assert first.sequence == 1
    assert target.latest(11.) is first
    assert target.latest(11.001) is None
    assert target.snapshot(12.)["state"] == "stale"
    assert target.snapshot(12.)["received_at"] == 10.
    assert accept(target, at=12.)  # Same pixels, genuinely new callback reception.
    second = target.latest(12.)
    assert second.sequence == 2 and second.received_at == 12.
    assert second.jpeg == first.jpeg
    with pytest.raises(FrozenInstanceError):
        second.sequence = 99


@pytest.mark.parametrize("change", [
    {"pixel_format": "UNKNOWN"}, {"width": 0}, {"height": 4097},
    {"width": True}, {"step": 5}, {"step": 32 * 1024 * 1024},
    {"data": b"short"}, {"data": bytearray(12)},
    {"received_at": float("nan")}, {"received_at": 9.},
    {"source_stamp": {"sec": 1, "nsec": 1_000_000_000}},
])
def test_rejections_preserve_the_previous_image_and_reception(change):
    target = store()
    assert accept(target)
    first = target.latest(10.)
    assert not accept(target, **change)
    assert target.latest(10.5) is first
    state = target.snapshot(11.5)
    assert state["state"] == "stale"
    assert state["received_at"] == 10.
    assert state["sequence"] == 1 and state["rejected"] == 1
    assert state["last_rejection"]


@pytest.mark.parametrize("format,raw", [
    ("RGB_INT8", bytes([255, 0, 0, 255, 0, 0, 77, 88]) * 2),
    ("BGR_INT8", bytes([0, 0, 255, 0, 0, 255, 77, 88]) * 2),
])
def test_color_order_and_padded_rows(format, raw):
    target = store()
    assert accept(target, pixel_format=format, step=8, data=raw)
    red, green, blue = pixel(target)
    assert red > 245 and green < 10 and blue < 10


def test_luminance_stride_and_source_stamp_do_not_override_local_time():
    target = store()
    stamp = {"sec": 50000, "nsec": 7}
    assert accept(target, pixel_format="L_INT8", step=3,
                  data=bytes([100, 100, 255]) * 2, source_stamp=stamp)
    assert all(abs(value - 100) <= 2 for value in pixel(target))
    stamp["sec"] = 1
    snapshot = target.snapshot(10.5)
    assert snapshot["rx_age_s"] == .5
    assert snapshot["source_stamp"] == {"sec": 50000, "nsec": 7}
    snapshot["source_stamp"]["sec"] = 2
    assert target.snapshot(10.5)["source_stamp"]["sec"] == 50000


def test_error_and_stop_make_previous_image_unavailable_without_redating():
    for action in (lambda target: target.fail("source failed"), lambda target: target.stop()):
        target = store()
        assert accept(target)
        action(target)
        assert target.latest(10.1) is None
        assert not accept(target, at=10.2)
        snapshot = target.snapshot(10.5)
        assert snapshot["state"] == "error" and snapshot["received_at"] == 10.
        assert snapshot["sequence"] == 1


def test_query_before_receipt_does_not_claim_freshness():
    target = store()
    assert accept(target)
    assert target.latest(9.) is None
    assert target.snapshot(9.)["state"] == "waiting"
    assert target.snapshot(9.)["rx_age_s"] is None
    assert target.latest(10.) is not None


def test_jpeg_conversion_does_not_block_reads_or_refresh_old_image(monkeypatch):
    target = store()
    assert accept(target, at=0.)
    entered, release, read_done = Event(), Event(), Event()
    original = Image.frombytes
    published, observed = [], []

    def blocked_conversion(*args, **kwargs):
        entered.set()
        assert release.wait(5.)
        return original(*args, **kwargs)

    def read_status():
        # The previous image expires while another conversion is blocked.
        observed.extend([target.snapshot(2.), target.latest(2.)])
        read_done.set()

    monkeypatch.setattr(Image, "frombytes", blocked_conversion)
    producer = Thread(target=lambda: published.append(accept(target, at=.1)))
    reader = Thread(target=read_status)
    producer.start()
    try:
        assert entered.wait(2.)
        reader.start()
        assert read_done.wait(1.), "status reads must not wait for JPEG conversion"
        assert observed[0]["state"] == "stale"
        assert observed[0]["sequence"] == 1
        assert observed[0]["received_at"] == 0.
        assert observed[1] is None
    finally:
        release.set()
        producer.join(2.)
        if reader.ident is not None:
            reader.join(2.)
    assert published == [True]
    assert target.snapshot(2.)["received_at"] == .1
    assert target.snapshot(2.)["sequence"] == 2
    assert target.latest(2.) is None  # Encoding completion does not refresh age.


@pytest.mark.parametrize("action", ["stop", "fail"])
def test_source_invalidated_during_encoding_cannot_publish(monkeypatch, action):
    target = store()
    assert accept(target)
    entered, release, invalidated = Event(), Event(), Event()
    original = Image.frombytes
    published = []

    def blocked_conversion(*args, **kwargs):
        entered.set()
        assert release.wait(5.)
        return original(*args, **kwargs)

    def invalidate():
        target.stop() if action == "stop" else target.fail("source failed")
        invalidated.set()

    monkeypatch.setattr(Image, "frombytes", blocked_conversion)
    producer = Thread(target=lambda: published.append(accept(target, at=10.1)))
    invalidator = Thread(target=invalidate)
    producer.start()
    try:
        assert entered.wait(2.)
        invalidator.start()
        assert invalidated.wait(1.), "source invalidation must not wait for encoding"
        assert target.latest(10.2) is None
    finally:
        release.set()
        producer.join(2.)
        if invalidator.ident is not None:
            invalidator.join(2.)
    assert published == [False]
    assert target.snapshot(10.5)["sequence"] == 1
    assert target.snapshot(10.5)["received_at"] == 10.


class FakeNode:
    def __init__(self, *, accepted=True):
        self.accepted = accepted
        self.subscriptions = []
        self.unsubscriptions = []

    def subscribe(self, message_type, topic, callback):
        self.subscriptions.append((message_type, topic, callback))
        return self.accepted

    def unsubscribe(self, topic):
        self.unsubscriptions.append(topic)


def gazebo_modules(monkeypatch, node):
    message_type = type("Image", (), {})
    messages = SimpleNamespace(Image=message_type, RGB_INT8=3, BGR_INT8=8, L_INT8=1)
    modules = {"gz.transport13": SimpleNamespace(Node=lambda: node),
               "gz.msgs10.image_pb2": messages}
    original = video.importlib.import_module
    monkeypatch.setattr(video.importlib, "import_module",
                        lambda name: modules[name] if name in modules else original(name))


def message(**kwargs):
    header = SimpleNamespace(stamp=SimpleNamespace(sec=500, nsec=42), HasField=lambda field: True)
    data = dict(width=2, height=2, step=6, data=bytes([255, 0, 0]) * 4,
                pixel_format_type=3, header=header, HasField=lambda field: True)
    data.update(kwargs)
    return SimpleNamespace(**data)


def test_gazebo_only_subscribes_and_rejects_unknown_format(monkeypatch):
    node = FakeNode()
    gazebo_modules(monkeypatch, node)
    target = store()
    camera = GazeboCamera(target)
    camera.start()
    camera.start()
    assert len(node.subscriptions) == 1
    callback = node.subscriptions[0][2]
    callback(message())
    assert target.snapshot(10.)["source_stamp"] == {"sec": 500, "nsec": 42}
    callback(message(pixel_format_type=0))
    assert target.snapshot(10.)["rejected"] == 1
    assert target.latest(10.).sequence == 1
    camera.close()
    camera.close()
    callback(message())
    assert target.latest(10.) is None
    assert target.snapshot(10.)["sequence"] == 1
    assert node.unsubscriptions == ["/camera/image"]


def test_gazebo_timestamps_before_jpeg_conversion(monkeypatch):
    node = FakeNode()
    gazebo_modules(monkeypatch, node)
    now = [10.]
    target = VideoStore(source="gazebo", endpoint="/camera/image", clock=lambda: now[0])
    original = Image.frombytes

    def delayed_conversion(*args, **kwargs):
        now[0] += 2.
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, "frombytes", delayed_conversion)
    camera = GazeboCamera(target)
    camera.start()
    node.subscriptions[0][2](message())
    assert target.snapshot(12.)["received_at"] == 10.
    assert target.latest(12.) is None
    camera.close()


def test_gazebo_explicit_python_path_appends_without_precedence(monkeypatch, tmp_path):
    node = FakeNode(accepted=False)
    gazebo_modules(monkeypatch, node)
    previous = sys.path[:]
    monkeypatch.setattr(sys, "path", previous[:])
    target = store()
    camera = GazeboCamera(target, python_path=tmp_path)
    camera.start()
    assert sys.path == previous + [str(tmp_path)]
    assert target.snapshot(10.)["state"] == "error"
    camera.close()


def test_gazebo_missing_bindings_is_an_explicit_error(monkeypatch):
    def missing(name):
        raise ImportError("bindings missing")

    monkeypatch.setattr(video.importlib, "import_module", missing)
    target = store()
    camera = GazeboCamera(target)
    camera.start()
    assert target.snapshot(10.)["state"] == "error"
    assert "bindings missing" in target.snapshot(10.)["detail"]
    assert target.latest(10.) is None
    camera.close()


class FakeCapture:
    def __init__(self):
        self.read_started = Event()
        self.allow_read = Event()
        self.released = Event()
        self.releases = 0
        self.reads = 0

    def isOpened(self):
        return True

    def set(self, key, value):
        return True

    def read(self):
        self.reads += 1
        self.read_started.set()
        assert self.allow_read.wait(5.)
        return True, np.full((2, 2, 3), [0, 0, 255], dtype=np.uint8)

    def release(self):
        self.releases += 1
        self.released.set()


def device_module(monkeypatch, capture):
    opened = []

    def factory(endpoint, backend):
        opened.append((endpoint, backend))
        return capture

    original = video.importlib.import_module
    cv2 = SimpleNamespace(VideoCapture=factory, CAP_V4L2=200, CAP_PROP_BUFFERSIZE=38)
    monkeypatch.setattr(video.importlib, "import_module",
                        lambda name: cv2 if name == "cv2" else original(name))
    return opened


def test_device_close_during_read_prevents_late_image_and_releases_once(monkeypatch):
    capture = FakeCapture()
    opened = device_module(monkeypatch, capture)
    target = VideoStore(source="device", endpoint="/dev/video0", clock=lambda: 10.)
    camera = DeviceCamera(target)
    camera.start()
    assert capture.read_started.wait(2.)
    camera.close()
    capture.allow_read.set()
    assert capture.released.wait(2.)
    camera.close()
    assert opened == [("/dev/video0", 200)]
    assert capture.releases == 1
    assert target.snapshot(10.)["sequence"] == 0
    assert target.snapshot(10.)["state"] == "error"


def test_device_read_failure_invalidates_last_image_and_keeps_clock(monkeypatch):
    capture = FakeCapture()

    def read():
        capture.reads += 1
        if capture.reads == 1:
            return True, np.full((2, 2, 3), [0, 0, 255], dtype=np.uint8)
        return False, None

    capture.read = read
    device_module(monkeypatch, capture)
    target = VideoStore(source="device", endpoint="/dev/video2", clock=lambda: 10.)
    camera = DeviceCamera(target)
    camera.start()
    assert capture.released.wait(2.)
    state = target.snapshot(10.5)
    assert state["sequence"] == 1 and state["received_at"] == 10.
    assert state["state"] == "error" and "read failed" in state["detail"]
    assert target.latest(10.5) is None
    camera.close()
    assert capture.releases == 1
