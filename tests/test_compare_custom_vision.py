"""Custom-vs-official replay preserves inputs and separates observations from truth."""
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from examples import compare_custom_vision as compare
from test_compare_vision import jpeg, tracked_result


def frames_manifest(tmp_path, *, clip=True):
    frames = []
    for index in range(3):
        data = jpeg("red" if index % 2 else "green")
        path = tmp_path / f"frame{index}.jpg"
        path.write_bytes(data)
        item = {"id": str(index), "path": path.name, "captured_at": 10. + index * .1,
                "source_id": "camera", "scene_group": "scene", "split": "val",
                "width": 32, "height": 24, "sha256": hashlib.sha256(data).hexdigest(),
                "reference_status": "unannotated", "boxes": None}
        if clip:
            item["clip_id"] = "one" if index < 2 else "two"
        frames.append(item)
    path = tmp_path / "frames.json"
    path.write_text(json.dumps({"schema_version": 1, "frames": frames}))
    return path, frames


def test_clip_boundaries_reset_same_camera_and_preserve_actual_timestamps(tmp_path):
    path, frames = frames_manifest(tmp_path)
    images = compare.ManifestImages(path)
    rows = list(images)
    assert [r["reset_tracker"] for r in rows] == [True, False, True]
    assert [r["segment"] for r in rows] == [0, 0, 1]
    assert [r["received_at"] for r in rows] == [f["captured_at"] for f in frames]
    assert all(r["boxes"] is None for r in rows)
    images.verify_unchanged()
    (tmp_path / "frame1.jpg").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        images.verify_unchanged()


def test_isolated_reference_photos_never_create_temporal_measurements(tmp_path):
    path, frames = frames_manifest(tmp_path, clip=False)
    rows = list(compare.ManifestImages(path))
    assert all(r["reset_tracker"] and not r["continuity_observable"] for r in rows)
    assert [r["segment"] for r in rows] == [0, 1, 2]


@pytest.mark.parametrize("change,match", [
    (lambda f: f[1].update(captured_at=9.), "increase"),
    (lambda f: f[1].update(captured_at=True), "timestamps"),
    (lambda f: f[1].update(sha256="f" * 64), "SHA-256"),
    (lambda f: f[1].update(id=f[0]["id"]), "unique"),
    (lambda f: f[1].update(boxes=[]), "human_validated"),
    (lambda f: f[1].update(width=True), "dimensions"),
])
def test_invalid_frame_evidence_fails_closed(tmp_path, change, match):
    path, frames = frames_manifest(tmp_path)
    change(frames)
    path.write_text(json.dumps({"schema_version": 1, "frames": frames}))
    with pytest.raises(ValueError, match=match):
        list(compare.ManifestImages(path))


def test_evaluation_runs_identical_original_bytes_with_alternating_order(tmp_path):
    path, frames = frames_manifest(tmp_path)
    calls = []
    class Pipeline:
        def __init__(self, name):
            self.name = name
        def warmup(self, data):
            calls.append(("warmup", self.name))
        def process(self, frame):
            assert hashlib.sha256(frame["jpeg"]).hexdigest() == frame["sha256"]
            calls.append(("process", self.name, frame["id"], frame["reset_tracker"]))
            return tracked_result()
    output = io.StringIO()
    rows = compare.evaluate(compare.ManifestImages(path),
                            {name: Pipeline(name) for name in compare.MODELS}, output)
    assert len(rows) == 3 and len(output.getvalue().splitlines()) == 3
    assert [c[1] for c in calls if c[0] == "process"] == ["baseline", "candidate", "candidate", "baseline", "baseline", "candidate"]
    assert all("jpeg" not in r for r in rows)


def test_unannotated_frames_do_not_become_false_positive_reference(tmp_path):
    path, frames = frames_manifest(tmp_path)
    rows = list(compare.ManifestImages(path))
    for row in rows:
        row["models"] = {name: tracked_result() for name in compare.MODELS}
    rows[0]["boxes"] = [{"label": "person", "box": [9.6, 4.8, 16., 16.8]}]
    rows[0]["reference_status"] = "human_validated"
    rows[1]["split"] = "train"
    result = compare.summarize(rows)
    assert set(result) == {"train", "val"}
    val = result["val"]["baseline"]
    assert val["reference"] == {"labeled_frames": 1, "tp": 1, "fp": 0, "fn": 0,
                                "precision": 1., "recall": 1., "iou_threshold": .5,
                                "target_identity_metrics": None}
    assert val["continuity_observations"]["clip_local_track_ids"] == 2
    train = result["train"]["baseline"]["reference"]
    assert train["labeled_frames"] == 0 and train["fp"] == 0 and train["precision"] is None
    rows[2]["boxes"], rows[2]["reference_status"] = [], "human_validated"
    assert compare.summarize(rows)["val"]["baseline"]["reference"]["fp"] == 1


def test_cli_failure_records_partial_state_without_touching_source(tmp_path, monkeypatch):
    path, frames = frames_manifest(tmp_path)
    before = path.read_bytes()
    output = tmp_path.parent / f"{tmp_path.name}-results"
    def fail(path):
        raise ValueError("bad model")
    monkeypatch.setattr(compare, "read_model_bundle", fail)
    result = compare.main(["--frames-manifest", str(path), "--vision-bundle", str(tmp_path / "model"),
                           "--output-dir", str(output)])
    assert result == 1
    assert json.loads((output / "report.json").read_text())["state"] == "failed"
    assert path.read_bytes() == before


def test_replay_uses_single_decode_and_original_tracker_time():
    seen = []
    image = object()
    detections = [{"confidence": .8, "box": [.3, .2, .2, .5]}]
    def decode(data):
        seen.append(("decode", data))
        return image
    def detect(value):
        assert value is image
        return {"width": 32, "height": 24, "detections": detections, "inference_ms": 1.}
    def encode(value, boxes, **kwargs):
        assert value is image and boxes is detections
        return [None]
    def update(boxes, timestamp, **kwargs):
        seen.append(("track", timestamp))
        assert kwargs["appearances"] == [None]
        return tracked_result()["detections"]
    pipeline = object.__new__(compare.ComparisonPipeline)
    pipeline.detector = SimpleNamespace(decode_jpeg=decode, detect_bgr=detect)
    pipeline.encoder = SimpleNamespace(encode_bgr=encode)
    pipeline.tracker = SimpleNamespace(reset=lambda: seen.append(("reset",)), update=update)
    frame = {"jpeg": b"original", "width": 32, "height": 24,
             "received_at": 170., "reset_tracker": True}
    result = pipeline.process(frame)
    assert seen == [("reset",), ("decode", b"original"), ("track", 170.)]
    assert result["inference_ms"] == 1. and result["processing_ms"] >= 0
