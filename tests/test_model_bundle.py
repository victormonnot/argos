"""Custom model admission, explicit person mapping and preserved official defaults."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from argos.perception.model_bundle import read_model_bundle
from argos.perception.yolox import YoloXPersonDetector
from test_yolox import raw_box


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "bundle"
    directory.mkdir()
    data = b"test graph bytes"
    (directory / "model.onnx").write_bytes(data)
    manifest = {"format": "iris-yolox-onnx-v1", "architecture": "yolox_nano",
        "input": {"name": "images", "shape": [1, 3, 416, 416], "dtype": "float32",
                  "color": "BGR", "range": [0, 255],
                  "letterbox": {"alignment": "top_left", "value": 114,
                                "interpolation": "opencv_linear"}},
        "output": {"name": "output", "shape": [1, 3549, 7],
                   "encoding": "yolox_raw_grid", "strides": [8, 16, 32]},
        "classes": [{"index": 0, "id": "car", "category_id": 7, "name": "Car"},
                    {"index": 1, "id": "people", "category_id": 3, "name": "Person"}],
        "model": {"path": "model.onnx", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()},
        "source": {"checkpoint_sha256": "a" * 64, "model_id": "model", "training_id": "training"}}
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest))
    return SimpleNamespace(directory=directory, path=path, data=data, manifest=manifest)


def save(bundle, value):
    bundle.path.write_text(json.dumps(value))


def test_bundle_validates_bytes_and_explicit_person_index(bundle):
    verified = read_model_bundle(bundle.directory)
    assert verified.data == bundle.data and verified.person_index == 1
    assert verified.identity()["source"]["model_id"] == "model"
    assert verified.manifest_sha256 == hashlib.sha256(bundle.path.read_bytes()).hexdigest()
    (bundle.directory / "model.onnx").write_bytes(b"x" * len(bundle.data))
    with pytest.raises(ValueError, match="SHA-256"):
        read_model_bundle(bundle.path)


@pytest.mark.parametrize("section,key,value", [
    ("input", "shape", [True, 3, 416, 416]),
    ("input", "color", "RGB"), ("input", "range", [0, 1]),
    ("output", "shape", [1, 3549, 85]), ("output", "encoding", "decoded"),
    ("output", "strides", [8, 32, 16]),
    ("model", "path", "../outside.onnx"), ("model", "path", "/tmp/model.onnx"),
    ("model", "path", "https://host/model.onnx"), ("model", "size_bytes", True),
    ("model", "size_bytes", 129 * 1024 * 1024), ("model", "sha256", "a" * 63),
    ("source", "checkpoint_sha256", None), ("source", "model_id", ""),
])
def test_incompatible_contracts_rejected_before_runtime(bundle, section, key, value):
    manifest = deepcopy(bundle.manifest)
    manifest[section][key] = value
    save(bundle, manifest)
    with pytest.raises(ValueError):
        read_model_bundle(bundle.path)


@pytest.mark.parametrize("field,value", [("name", "human"), ("index", 0), ("index", True),
                                          ("category_id", 7), ("id", "car")])
def test_person_mapping_never_guessed_or_ambiguous(bundle, field, value):
    manifest = deepcopy(bundle.manifest)
    manifest["classes"][1][field] = value
    save(bundle, manifest)
    with pytest.raises(ValueError):
        read_model_bundle(bundle.path)


def test_symlink_cannot_escape_bundle(bundle, tmp_path):
    external = tmp_path / "other.onnx"
    external.write_bytes(bundle.data)
    model = bundle.directory / "model.onnx"
    model.unlink()
    model.symlink_to(external)
    with pytest.raises(ValueError, match="inside"):
        read_model_bundle(bundle.path)


def test_duplicate_json_fields_rejected(bundle):
    bundle.path.write_text('{"format":"one","format":"two"}')
    with pytest.raises(ValueError, match="duplicate"):
        read_model_bundle(bundle.path)


@pytest.fixture
def custom_runtime(bundle, monkeypatch):
    runtime = SimpleNamespace(output=np.zeros((1, 3549, 7), dtype=np.float32), inputs=[])
    def set_input(value, name):
        assert name == "images"
        runtime.inputs.append(value)
    def forward(name):
        assert name == "output"
        return runtime.output
    runtime.setInput, runtime.forward = set_input, forward
    runtime.setPreferableBackend = lambda value: None
    def load(data):
        assert data.tobytes() == bundle.data
        return runtime
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(
        IMREAD_COLOR=1, INTER_LINEAR=1, setNumThreads=lambda value: None,
        imdecode=lambda *args: np.full((2, 4, 3), [11, 33, 77], dtype=np.uint8),
        resize=lambda image, size, **kwargs: np.full((size[1], size[0], 3), image[0, 0]),
        dnn=SimpleNamespace(readNetFromONNX=load, DNN_BACKEND_OPENCV=3)))
    return runtime


def test_custom_graph_probed_then_correct_person_column_decoded(bundle, custom_runtime):
    subject = YoloXPersonDetector(bundle_path=bundle.path)
    assert len(custom_runtime.inputs) == 1
    assert custom_runtime.inputs[0].shape == (1, 3, 416, 416)
    raw_box(subject, custom_runtime.output, 0, [1, .25, 1, 1], .99)  # car only
    assert subject.detect(b"fixture JPEG")["detections"] == []
    custom_runtime.output[0, 0, 6] = .8
    result = subject.detect(b"fixture JPEG")["detections"]
    assert len(result) == 1 and result[0]["confidence"] == pytest.approx(.8)
    assert result[0]["box"] == pytest.approx([.25, .125, .25, .5])
    np.testing.assert_array_equal(custom_runtime.inputs[-1][0, :, 0, 0], [11, 33, 77])


@pytest.mark.parametrize("bad", [np.zeros((1, 3549, 85), dtype=np.float32),
    np.full((1, 3549, 7), np.nan), np.full((1, 3549, 7), 1.5),
    np.full((1, 3549, 7), -.01), np.zeros((1, 3549, 7), dtype=np.int64)])
def test_invalid_actual_graph_rejected_at_startup(bundle, custom_runtime, bad):
    custom_runtime.output = bad
    with pytest.raises(RuntimeError, match="invalid"):
        YoloXPersonDetector(bundle_path=bundle.path)


def test_custom_path_does_not_disable_official_hash_check(bundle):
    with pytest.raises(ValueError, match="size/SHA-256"):
        YoloXPersonDetector(bundle.directory / "model.onnx", variant="nano")
    with pytest.raises(ValueError, match="not both"):
        YoloXPersonDetector(bundle.directory / "model.onnx", bundle_path=bundle.path)


def test_custom_config_survives_source_changes_and_reaches_worker(bundle):
    from argos.console.config import ConsoleConfig
    from argos.console.vision import VisionService
    config = ConsoleConfig(vision_bundle=bundle.path)
    assert config.has_vision and config.with_sources(config.public()).vision_bundle == bundle.path
    assert "vision_bundle" not in config.public()
    seen = []
    class Context:
        def Queue(self, **kwargs):
            return object()
        def Process(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(start=lambda: None)
    service = VisionService(None, bundle_path=config.vision_bundle, process_context=Context())
    service.start()
    args = seen[0]["args"]
    assert args[0] is None and args[3] == "nano" and args[5] == str(bundle.path)
    assert args[6] == hashlib.sha256(bundle.path.read_bytes()).hexdigest()
    assert service.model.variant == "custom"
    with pytest.raises(ValueError, match="not both"):
        ConsoleConfig(vision_bundle=bundle.path, vision_model=Path("official.onnx"))


@pytest.mark.parametrize("name", ["fly", "distance"])
def test_launcher_explicit_selection_and_rollback(bundle, name):
    from argos import fly, distance
    module = {"fly": fly, "distance": distance}[name]
    config = bundle.directory / "settings.json"
    config.write_text(json.dumps({"camera_device": "/dev/video2", "radio_port": "/dev/ttyACM0",
                                 "profile": "model.yml", "vision_model": "/tmp/official.onnx"}))
    args = SimpleNamespace(config=config, vision_bundle=bundle.path)
    values = module.load_settings(args)
    assert values["vision_bundle"] == str(bundle.path) and "vision_model" not in values
    config.write_text(json.dumps(values))
    values = module.load_settings(SimpleNamespace(config=config, vision_model=Path("/tmp/official.onnx")))
    assert values["vision_model"] == "/tmp/official.onnx" and "vision_bundle" not in values


def test_console_cli_accepts_bundle_without_starting_worker(bundle, monkeypatch):
    from argos.console import __main__ as cli, app
    seen = []
    monkeypatch.setattr(sys, "argv", ["argos.console", "--vision-bundle", str(bundle.path)])
    monkeypatch.setattr(app, "create_app", lambda config: seen.append(config) or "fake-app")
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **kw: None))
    cli.main()
    assert seen[0].vision_bundle == bundle.path and seen[0].vision_model is None


def test_worker_refuses_manifest_changed_between_setup_and_start(bundle, custom_runtime):
    from queue import Queue
    from argos.console.vision import _worker
    incoming, outgoing = Queue(), Queue()
    _worker(None, incoming, outgoing, "nano", 2, str(bundle.path), "b" * 64)
    kind, detail = outgoing.get_nowait()
    assert kind == "error" and "changed before worker startup" in detail
    assert outgoing.empty()  # Never announced ready or processed an image.


def test_custom_app_keeps_identity_and_physical_preview_enabled(bundle, tmp_path):
    from argos.console.app import create_app
    from argos.console.config import ConsoleConfig
    config = ConsoleConfig(environment="real", video_source="device", video_endpoint="/dev/video999",
                           vision_bundle=bundle.path, recordings_dir=tmp_path / "recordings")
    app = create_app(config)
    assert app.state.vision.model.variant == "custom"
    assert app.state.vision.bundle_identity["person_index"] == 1
