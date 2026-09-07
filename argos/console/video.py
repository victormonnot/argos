"""Bounded, receive-only camera images for the observation console.

Gazebo and V4L2 are the only acquisition sources. Reception age describes this
process, not camera exposure time; a driver may itself buffer frames.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import io
import math
from pathlib import Path
import re
import sys
from threading import Event, RLock, Thread
import time
from typing import Callable


MAX_DIMENSION = 4096
MAX_PIXELS = 8_388_608
MAX_RAW_BYTES = 32 * 1024 * 1024
MAX_JPEG_BYTES = 16 * 1024 * 1024
_FORMATS = {"RGB_INT8": ("RGB", "RGB", 3),
            "BGR_INT8": ("RGB", "BGR", 3),
            "L_INT8": ("L", "L", 1)}


def _time(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("time must be a finite nonnegative number")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("time must be a finite nonnegative number")
    return float(value)


@dataclass(frozen=True)
class VideoSample:
    jpeg: bytes
    sequence: int
    received_at: float


class VideoStore:
    """One immutable JPEG plus status, with short atomic reader sections.

    Rejections preserve the previous valid image and its reception time.
    Errors and close are terminal for this store; create a new source to retry.
    A separate producer lock bounds conversion memory without blocking readers.
    """

    def __init__(self, *, source: str, endpoint: str | None = None,
                 age_limit: float = 1., clock: Callable[[], float] = time.monotonic):
        if source not in {"none", "gazebo", "device"}:
            raise ValueError("source must be none, gazebo, or device")
        if source == "none" and endpoint is not None:
            raise ValueError("an unconfigured source cannot have an endpoint")
        if source == "gazebo" and (not isinstance(endpoint, str) or
                len(endpoint) > 1024 or
                re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", endpoint) is None):
            raise ValueError("Gazebo requires an absolute camera Image topic")
        if source == "device" and (not isinstance(endpoint, str) or
                re.fullmatch(r"/dev/video[0-9]+", endpoint) is None):
            raise ValueError("a real camera requires a /dev/videoN device")
        age_limit = _time(age_limit)
        if age_limit == 0:
            raise ValueError("age_limit must be positive")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.source = source
        self.endpoint = endpoint
        self.clock = clock
        self.age_limit = age_limit
        self._lock = RLock()
        self._producer_lock = RLock()
        self._sample: VideoSample | None = None
        self._width: int | None = None
        self._height: int | None = None
        self._stamp: dict | None = None
        self._rejected = 0
        self._last_rejection = ""
        self._error: str | None = None
        self._stopped = False

    def reject(self, detail: str) -> None:
        with self._lock:
            if self._stopped or self._error is not None:
                return
            self._rejected += 1
            self._last_rejection = str(detail)[:400]

    def fail(self, detail: str) -> None:
        with self._lock:
            if not self._stopped:
                self._error = str(detail)[:400]

    def stop(self) -> None:
        with self._lock:
            self._stopped = True

    def accept_raw(self, *, width: int, height: int, step: int,
                   pixel_format: str, data: bytes, received_at: float,
                   source_stamp: dict | None = None) -> bool:
        """Validate dimensions/stride before decoding; never infer a format."""
        with self._producer_lock:
            with self._lock:
                if self.source == "none" or self._stopped or self._error is not None:
                    return False
            try:
                received_at = _time(received_at)
                with self._lock:
                    if self._sample and received_at < self._sample.received_at:
                        raise ValueError("reception clock moved backwards")
                if any(isinstance(v, bool) or not isinstance(v, int)
                       for v in (width, height, step)):
                    raise ValueError("dimensions and stride must be integers")
                if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION
                        and width * height <= MAX_PIXELS):
                    raise ValueError("image exceeds dimension/pixel limits")
                if pixel_format not in _FORMATS:
                    raise ValueError("unsupported pixel format; use RGB_INT8, BGR_INT8 or L_INT8")
                mode, rawmode, channels = _FORMATS[pixel_format]
                if step < width * channels or step * height > MAX_RAW_BYTES:
                    raise ValueError("invalid or oversized image stride")
                if not isinstance(data, bytes) or len(data) != step * height:
                    raise ValueError("image byte count does not match height and stride")
                stamp = None
                if source_stamp is not None:
                    sec, nsec = source_stamp["sec"], source_stamp["nsec"]
                    if (isinstance(sec, bool) or not isinstance(sec, int) or
                            isinstance(nsec, bool) or not isinstance(nsec, int) or
                            not 0 <= nsec < 1_000_000_000):
                        raise ValueError("invalid source timestamp")
                    stamp = {"sec": sec, "nsec": nsec}
                from PIL import Image
                picture = Image.frombytes(mode, (width, height), data, "raw", rawmode, step, 1)
                output = io.BytesIO()
                picture.save(output, format="JPEG", quality=85, subsampling=0)
                jpeg = output.getvalue()
                if len(jpeg) > MAX_JPEG_BYTES:
                    raise ValueError("encoded image exceeds byte limit")
            except ImportError:
                self.fail("Pillow is unavailable; install the console extra")
                return False
            except (ValueError, TypeError, KeyError, OSError) as exc:
                self.reject(str(exc))
                return False
            with self._lock:
                # Conversion ran without the reader lock: stop/fail may have
                # invalidated this source while Pillow was working.
                if self._stopped or self._error is not None:
                    return False
                if self._sample and received_at < self._sample.received_at:
                    self.reject("reception clock moved backwards")
                    return False
                sequence = 1 if self._sample is None else self._sample.sequence + 1
                self._sample = VideoSample(jpeg=jpeg, sequence=sequence, received_at=received_at)
                self._width, self._height, self._stamp = width, height, stamp
                return True

    def _status(self, now: float) -> tuple[str, str, float | None]:
        age = None if self._sample is None else now - self._sample.received_at
        if self.source == "none":
            return "unconfigured", "Aucune caméra configurée", age
        if self._error is not None:
            return "error", self._error, age
        if self._stopped:
            return "error", "Source caméra arrêtée", age
        if age is None:
            return "waiting", "En attente d’une image caméra valide", age
        if age < 0:
            return "waiting", "Nouvelle réception en cours de lecture", None
        if age > self.age_limit:
            return "stale", "Aucune image récente reçue", age
        return "recent", "Images reçues de la caméra", age

    def snapshot(self, now: float) -> dict:
        now = _time(now)
        with self._lock:
            state, detail, age = self._status(now)
            return {"source": self.source,
                    "label": {"none": "Aucune caméra", "gazebo": "Caméra Gazebo",
                              "device": "Caméra réelle"}[self.source],
                    "endpoint": self.endpoint, "state": state, "detail": detail,
                    "sequence": 0 if self._sample is None else self._sample.sequence,
                    "received_at": None if self._sample is None else self._sample.received_at,
                    "rx_age_s": age, "age_limit_s": self.age_limit,
                    "width": self._width, "height": self._height,
                    "rejected": self._rejected, "last_rejection": self._last_rejection,
                    "source_stamp": None if self._stamp is None else self._stamp.copy()}

    def latest(self, now: float) -> VideoSample | None:
        now = _time(now)
        with self._lock:
            return self._sample if self._status(now)[0] == "recent" else None


class GazeboCamera:
    """Subscribe to one gz.msgs.Image topic; never publish or call services."""

    def __init__(self, store: VideoStore, *, python_path: Path | None = None):
        if store.source != "gazebo":
            raise ValueError("GazeboCamera requires a gazebo store")
        self.store = store
        self.python_path = python_path
        self._lock = RLock()
        self._node = None
        self._formats: dict[int, str] = {}
        self._started = False
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True
            try:
                if self.python_path is not None:
                    path = Path(self.python_path)
                    if not path.is_dir():
                        raise ValueError("Gazebo Python path is not a directory")
                    if str(path) not in sys.path:
                        sys.path.append(str(path))
                transport = importlib.import_module("gz.transport13")
                messages = importlib.import_module("gz.msgs10.image_pb2")
                self._formats = {getattr(messages, name): name for name in _FORMATS}
                self._node = transport.Node()
                if not self._node.subscribe(messages.Image, self.store.endpoint, self._receive):
                    raise RuntimeError("Gazebo refused the Image subscription")
            except Exception as exc:
                self.store.fail(f"Gazebo camera unavailable: {exc}")

    def _receive(self, message) -> None:
        # Sample callback entry before waiting for a previous conversion's lock.
        try:
            received_at = self.store.clock()
        except Exception as exc:
            self.store.reject(f"Camera reception clock failed: {exc}")
            return
        with self._lock:
            if self._closed:
                return
            formats = self._formats
        try:
            pixel_format = formats.get(message.pixel_format_type, "UNKNOWN")
            stamp = None
            if message.HasField("header") and message.header.HasField("stamp"):
                stamp = {"sec": message.header.stamp.sec, "nsec": message.header.stamp.nsec}
            self.store.accept_raw(width=message.width, height=message.height,
                step=message.step, data=message.data, received_at=received_at,
                pixel_format=pixel_format, source_stamp=stamp)
        except Exception as exc:
            self.store.reject(f"Invalid Gazebo image: {exc}")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            node, self._node = self._node, None
            self.store.stop()
        # Do not hold the callback lock while waiting for transport cleanup.
        if node is not None:
            try:
                node.unsubscribe(self.store.endpoint)
            except Exception:
                pass


class DeviceCamera:
    """One V4L2 reader thread, holding no queue of frames.

    Only this worker releases VideoCapture. Close invalidates the store at once;
    a native driver blocked in read may outlive the one-second join timeout.
    """

    def __init__(self, store: VideoStore):
        if store.source != "device":
            raise ValueError("DeviceCamera requires a device store")
        self.store = store
        self._lock = RLock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._closed or self._thread is not None:
                return
            self._thread = Thread(target=self._run, name="argos-camera", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        capture = None
        try:
            cv2 = importlib.import_module("cv2")
            if self._stop.is_set():
                return
            capture = cv2.VideoCapture(self.store.endpoint, cv2.CAP_V4L2)
            if not capture.isOpened():
                self.store.fail("Cannot open physical camera; check device and permissions")
                return
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            while not self._stop.is_set():
                ok, frame = capture.read()
                received_at = self.store.clock()
                if self._stop.is_set():
                    break
                if not ok:
                    self.store.fail("Physical camera read failed")
                    break
                if (frame is None or frame.dtype.name != "uint8" or frame.ndim != 3
                        or frame.shape[2] != 3 or frame.shape[0] > MAX_DIMENSION
                        or frame.shape[1] > MAX_DIMENSION
                        or frame.shape[0] * frame.shape[1] > MAX_PIXELS):
                    self.store.reject("Physical camera returned an unsupported or oversized image")
                    continue
                height, width, _ = frame.shape
                self.store.accept_raw(width=width, height=height, step=width * 3,
                    pixel_format="BGR_INT8", data=frame.tobytes(), received_at=received_at)
        except ImportError as exc:
            self.store.fail(f"Physical camera dependency unavailable: install the camera extra ({exc})")
        except Exception as exc:
            self.store.fail(f"Physical camera unavailable: {exc}")
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            self.store.stop()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=1.)

    @property
    def worker_stopped(self) -> bool:
        """A closed reader may still hold its device inside a native call.

        Reopening must wait for that worker, otherwise repeated retries can
        accumulate blocked driver threads and concurrent device owners.
        """
        with self._lock:
            return self._thread is None or not self._thread.is_alive()
