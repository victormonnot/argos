"""Explicit, self-contained custom YOLOX-Nano exports; no network or code loading.

SHA-256 binds inference to the selected local artifact. It is integrity evidence,
not a signature, a quality endorsement or a replacement for official model pins.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re


FORMAT = "iris-yolox-onnx-v1"
MAX_MODEL_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate bundle field: {key}")
        result[key] = value
    return result


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _text(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 256


def _equal(value, expected):
    # JSON true must not masquerade as dimension 1 or category index 0/1.
    return json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True)


@dataclass(frozen=True)
class ModelBundle:
    path: Path
    manifest_sha256: str
    manifest: dict
    data: bytes
    person_index: int

    @property
    def class_count(self):
        return len(self.manifest["classes"])

    def identity(self):
        return {"variant": "custom", "architecture": "yolox_nano",
                "sha256": self.manifest["model"]["sha256"],
                "bundle_sha256": self.manifest_sha256,
                "person_index": self.person_index, "classes": self.manifest["classes"],
                "source": self.manifest["source"]}


def read_model_bundle(path: str | Path) -> ModelBundle:
    """Validate the declared contract and read exactly the hash-checked ONNX bytes."""
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / "manifest.json"
    path = path.resolve()
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("vision bundle manifest exceeds 1 MiB")
        manifest = json.loads(raw, object_pairs_hook=_object,
                              parse_constant=lambda value: (_ for _ in ()).throw(
                                  ValueError("nonfinite bundle number")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("vision bundle manifest is unavailable or invalid JSON") from exc
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or manifest.get("architecture") != "yolox_nano"):
        raise ValueError("vision bundle must declare iris-yolox-onnx-v1 / yolox_nano")
    input_spec, output = manifest.get("input"), manifest.get("output")
    expected = {"name": "images", "shape": [1, 3, 416, 416], "dtype": "float32",
                "color": "BGR", "range": [0, 255]}
    if (not isinstance(input_spec, dict)
            or any(not _equal(input_spec.get(k), v) for k, v in expected.items())):
        raise ValueError("vision bundle input must be BGR float32 0..255 at 1x3x416x416")
    letterbox = input_spec.get("letterbox")
    if (not isinstance(letterbox, dict) or letterbox.get("alignment") != "top_left"
            or not _equal(letterbox.get("value"), 114)
            or letterbox.get("interpolation", "opencv_linear") != "opencv_linear"):
        raise ValueError("vision bundle requires top-left 114 OpenCV linear letterboxing")
    classes = manifest.get("classes")
    if not isinstance(classes, list) or not 1 <= len(classes) <= 1000:
        raise ValueError("vision bundle requires 1..1000 explicit classes")
    identifiers, categories, names, people = set(), set(), set(), []
    for index, item in enumerate(classes):
        if (not isinstance(item, dict) or type(item.get("index")) is not int
                or item["index"] != index or not _text(item.get("id"))
                or type(item.get("category_id")) is not int or item["category_id"] < 1
                or not _text(item.get("name"))):
            raise ValueError("vision bundle classes need ordered indices, IDs, category IDs and names")
        name = item["name"].strip().casefold()
        if item["id"] in identifiers or item["category_id"] in categories or name in names:
            raise ValueError("vision bundle classes must have unique IDs, category IDs and names")
        identifiers.add(item["id"])
        categories.add(item["category_id"])
        names.add(name)
        if name == "person":
            people.append(index)
    if len(people) != 1:
        raise ValueError("vision bundle must explicitly name exactly one person class")
    expected = {"name": "output", "shape": [1, 3549, 5 + len(classes)],
                "encoding": "yolox_raw_grid", "strides": [8, 16, 32]}
    if (not isinstance(output, dict)
            or any(not _equal(output.get(k), v) for k, v in expected.items())):
        raise ValueError("vision bundle output must declare the raw YOLOX stride-grid tensor")
    model, provenance = manifest.get("model"), manifest.get("source")
    if (not isinstance(model, dict) or not isinstance(model.get("path"), str)
            or re.fullmatch(r"[A-Za-z0-9_-]+\.onnx", model["path"]) is None
            or type(model.get("size_bytes")) is not int
            or not 0 < model["size_bytes"] <= MAX_MODEL_BYTES
            or not _digest(model.get("sha256"))):
        raise ValueError("vision bundle needs a local ONNX filename, bounded size and SHA-256")
    if (not isinstance(provenance, dict) or not _digest(provenance.get("checkpoint_sha256"))
            or not _text(provenance.get("model_id")) or not _text(provenance.get("training_id"))):
        raise ValueError("vision bundle requires checkpoint, model and training provenance")
    model_path = (path.parent / model["path"]).resolve()
    if model_path.parent != path.parent:
        raise ValueError("vision bundle model must stay inside its bundle directory")
    try:
        with model_path.open("rb") as source:
            data = source.read(model["size_bytes"] + 1)
    except OSError as exc:
        raise ValueError("vision bundle ONNX file is unavailable") from exc
    if len(data) != model["size_bytes"] or hashlib.sha256(data).hexdigest() != model["sha256"]:
        raise ValueError("vision bundle model failed its size/SHA-256 check")
    return ModelBundle(path, hashlib.sha256(raw).hexdigest(), manifest, data, people[0])
