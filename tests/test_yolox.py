"""Model integrity and image geometry, without installing OpenCV or weights."""
import builtins
import hashlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from argos.perception import yolox


@pytest.fixture
def model_bytes(tmp_path, monkeypatch):
    data = b"test-only model bytes"
    monkeypatch.setattr(yolox, "MODEL_BYTES", len(data))
    monkeypatch.setattr(yolox, "MODEL_SHA256", hashlib.sha256(data).hexdigest())
    path = tmp_path / "model.onnx"
    path.write_bytes(data)
    return path, data


@pytest.fixture
def detector(monkeypatch, model_bytes):
    image = np.full((2, 4, 3), [11, 33, 77], dtype=np.uint8)
    runtime = SimpleNamespace(image=image, output=np.zeros((1, 3549, 85), dtype=np.float32))
    runtime.setInput = lambda value: setattr(runtime, "input", value)
    runtime.forward = lambda: runtime.output
    runtime.setPreferableBackend = lambda value: setattr(runtime, "backend", value)

    def load(value):
        assert value.tobytes() == model_bytes[1]  # Verify the bytes passed to DNN, not a path.
        return runtime

    fake = SimpleNamespace(
        IMREAD_COLOR=1, INTER_LINEAR=1,
        setNumThreads=lambda value: setattr(runtime, "threads", value),
        imdecode=lambda *args: runtime.image,
        resize=lambda image, size, **kwargs: np.full((size[1], size[0], 3), image[0, 0]),
        dnn=SimpleNamespace(readNetFromONNX=load, DNN_BACKEND_OPENCV=3, DNN_TARGET_CPU=0),
    )
    monkeypatch.setitem(sys.modules, "cv2", fake)
    result = yolox.YoloXPersonDetector(model_bytes[0])
    return result, runtime


def raw_box(detector, output, row, box, confidence=.8, width=4, height=2):
    left, top, box_width, box_height = box
    ratio = min(416 / width, 416 / height)
    centre = np.array([left + box_width / 2, top + box_height / 2]) * ratio
    size = np.array([box_width, box_height]) * ratio
    output[0, row, :2] = centre / detector._strides[row] - detector._grid[row]
    output[0, row, 2:4] = np.log(size / detector._strides[row])
    output[0, row, 4] = 1
    output[0, row, 5] = confidence


def test_loading_requires_exact_size_and_digest(model_bytes):
    path, data = model_bytes
    assert yolox.read_verified_model(path) == data
    for bad in (data[:-1], data + b"x", b"x" * len(data)):
        path.write_bytes(bad)
        with pytest.raises(ValueError, match="size/SHA-256"):
            yolox.read_verified_model(path)


def test_missing_model_fails_before_runtime_import(tmp_path, monkeypatch):
    original = builtins.__import__
    def importing(name, *args, **kwargs):
        assert name != "cv2", "missing weights must not initialize an optional runtime"
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(ValueError, match="setup_vision_model.py"):
        yolox.YoloXPersonDetector(tmp_path / "absent.onnx")


def test_missing_optional_runtime_has_actionable_message(model_bytes, monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)
    with pytest.raises(RuntimeError, match="optional ARGOS camera dependency"):
        yolox.YoloXPersonDetector(model_bytes[0])


def test_letterbox_preserves_bgr_range_and_dimensions(detector):
    subject, runtime = detector
    result = subject.detect(b"camera JPEG fixture")
    assert (result["width"], result["height"]) == (4, 2)
    assert result["detections"] == [] and result["inference_ms"] >= 0
    assert runtime.threads == 2 and runtime.backend == 3
    blob = runtime.input
    assert blob.shape == (1, 3, 416, 416) and blob.dtype == np.float32
    np.testing.assert_array_equal(blob[0, :, 0, 0], [11, 33, 77])
    np.testing.assert_array_equal(blob[0, :, 207, 415], [11, 33, 77])
    np.testing.assert_array_equal(blob[0, :, 208, 0], [114, 114, 114])


def test_grid_decode_person_score_and_nms(detector):
    subject, runtime = detector
    raw_box(subject, runtime.output, 0, [1, .25, 1, 1], .8)
    raw_box(subject, runtime.output, 1, [1.01, .25, 1, 1], .75)
    raw_box(subject, runtime.output, 2, [2.5, .5, .5, .8], .7)
    runtime.output[0, 2, 4] = .4  # Objectness makes this candidate fall below .35.
    raw_box(subject, runtime.output, 3, [3, 0, .5, .9], 0)
    runtime.output[0, 3, 7] = .99  # A confident different COCO class is not a person.
    detections = subject.detect(b"JPEG")["detections"]
    assert len(detections) == 1
    assert detections[0]["box"] == pytest.approx([.25, .125, .25, .5])
    assert detections[0]["confidence"] == pytest.approx(.8)


def test_boxes_are_clipped_and_padding_only_boxes_disappear(detector):
    subject, runtime = detector
    raw_box(subject, runtime.output, 0, [-.5, -.25, 2, 1], .9)
    raw_box(subject, runtime.output, 1, [1, 2.1, 1, .5], .8)
    result = subject.detect(b"JPEG")["detections"]
    assert len(result) == 1
    assert result[0]["box"] == pytest.approx([0, 0, .375, .375])


def test_output_limit_is_sixteen_real_boxes(detector):
    subject, runtime = detector
    for row in range(20):
        raw_box(subject, runtime.output, row, [(row % 5) * .7, (row // 5) * .4, .1, .1])
    assert len(subject.detect(b"JPEG")["detections"]) == 16


@pytest.mark.parametrize("jpeg", [None, bytearray(b"x"), b"", b"x" * (16 * 1024 * 1024 + 1)])
def test_invalid_input_is_not_inferred(detector, jpeg):
    subject, runtime = detector
    with pytest.raises(ValueError, match="camera JPEG"):
        subject.detect(jpeg)
    assert not hasattr(runtime, "input")


@pytest.mark.parametrize("image", [None, np.zeros((2, 2)), np.zeros((2, 2, 4)),
                                   np.zeros((4097, 1, 3)), np.zeros((0, 1, 3))])
def test_undecodable_or_invalid_dimensions_are_errors(detector, image):
    subject, runtime = detector
    runtime.image = image
    with pytest.raises(ValueError):
        subject.detect(b"invalid JPEG")
    assert not hasattr(runtime, "input")


@pytest.mark.parametrize("bad", [np.zeros((1, 10, 85)), np.full((1, 3549, 85), np.nan)])
def test_invalid_network_output_is_not_reported_as_empty_scene(detector, bad):
    subject, runtime = detector
    runtime.output = bad
    with pytest.raises(RuntimeError, match="invalid output tensor"):
        subject.detect(b"JPEG")


def test_box_decode_overflow_is_an_error(detector):
    subject, runtime = detector
    raw_box(subject, runtime.output, 0, [1, .25, 1, 1])
    runtime.output[0, 0, 2] = 1000
    with pytest.raises(RuntimeError, match="invalid box dimensions"):
        subject.detect(b"JPEG")
