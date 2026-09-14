"""Validate existing comparison results and bind them to read-only native pixels.

This module neither imports a detector/tracker runtime nor performs inference.
Saved boxes are observations to display, never trusted source paths or commands.
Call verify_unchanged immediately before publishing a completed visual export.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from argos.console.archive import RecordingArchive
from argos.console.visual_recording import MAX_SAMPLES, VisualArchive
from .evaluation_report import summarize


MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_FRAMES_BYTES = 64 * 1024 * 1024
MAX_FRAME_LINE_BYTES = 32 * 1024
MAX_ROWS = 5000
VARIANTS = frozenset(("tiny", "s"))
FRAME_FIELDS = ("index", "received_at", "available_at", "sequence", "video_id", "width", "height")
ROW_FIELDS = frozenset((*FRAME_FIELDS, "jpeg_sha256", "segment", "excluded_before",
                        "at_s", "available_at_s", "models"))


class ComparisonExportError(ValueError):
    """Malformed, incomplete, changed, or incorrectly bound comparison input."""


def _fail(message):
    raise ComparisonExportError(message)


def _integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"Invalid {name}")
    return value


def _number(value, name, *, minimum=None, maximum=None):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if (not finite
            or (minimum is not None and value < minimum)
            or (maximum is not None and value > maximum)):
        _fail(f"Invalid finite {name}")
    return value


def _text(value, name, maximum=4096):
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(f"Invalid {name}")
    return value


def _hex(value, length, name):
    if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None:
        _fail(f"Invalid {name}")
    return value


def _object(value, name):
    if not isinstance(value, dict):
        _fail(f"Invalid {name} object")
    return value


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True)
class _InputFile:
    path: Path
    signature: tuple
    sha256: str
    maximum: int

    def verify(self):
        _, current = _read_input(self.path, self.maximum)
        if (current.signature, current.sha256) != (self.signature, self.sha256):
            _fail(f"Comparison input changed: {self.path.name}")


def _read_input(path, maximum):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
                _fail(f"Comparison input must be a nonempty bounded regular file: {path.name}")
            data = source.read(maximum + 1)
            after = os.fstat(source.fileno())
            current = path.stat(follow_symlinks=False)
            if (len(data) != before.st_size or len(data) > maximum
                    or not stat.S_ISREG(current.st_mode)
                    or _signature(before) != _signature(after)
                    or _signature(before) != _signature(current)):
                _fail(f"Comparison input changed while reading: {path.name}")
        return data, _InputFile(path, _signature(before), hashlib.sha256(data).hexdigest(), maximum)
    except OSError as exc:
        raise ComparisonExportError(
            f"Missing or inaccessible completed comparison input (no symlinks): {path.name}") from exc


def _json(data, name):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                _fail(f"Duplicate JSON key in {name}: {key}")
            result[key] = value
        return result

    def floating(value):
        return _number(float(value), f"JSON number in {name}")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_float=floating,
                          parse_constant=lambda value: _fail(f"Non-finite JSON in {name}"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ComparisonExportError):
            raise
        raise ComparisonExportError(f"Invalid JSON in {name}") from exc


def _report(value):
    report = _object(value, "comparison report")
    if (report.get("format") != "argos.vision-comparison"
            or type(report.get("version")) is not int or report["version"] != 1):
        _fail("Unsupported comparison report format/version")
    if report.get("state") != "complete":
        _fail("Comparison is failed, partial, or unfinished; a complete report is required")
    source = _object(report.get("source"), "source")
    _hex(source.get("id"), 32, "source recording ID")
    _hex(source.get("telemetry_revision"), 64, "journal revision")
    _hex(source.get("visual_revision"), 64, "visual revision")
    _text(source.get("directory"), "source directory")
    _number(source.get("duration_s"), "source duration", minimum=0)
    _integer(source.get("selected_frames"), 1, MAX_ROWS, "selected frame count")
    total = _integer(source.get("archive_frame_rows"), 1, MAX_SAMPLES, "archive frame count")
    examined = _integer(source.get("examined_frame_rows"), 1, total, "examined frame count")
    if (type(source.get("truncated")) is not bool or source["truncated"] != (examined < total)
            or _integer(source.get("unevaluated_frame_rows"), 0, total, "unevaluated frame count") != total - examined):
        _fail("Inconsistent source truncation counts")
    _integer(source.get("visual_recording_dropped"), 0, 100000, "visual dropped count")
    configuration = _object(report.get("configuration"), "configuration")
    _integer(configuration.get("threads"), 1, 6, "inference threads")
    _integer(configuration.get("max_frames"), source["selected_frames"], MAX_ROWS, "configured frame limit")
    _integer(configuration.get("warmup_frames"), 0, 1000, "warmup frame count")
    _integer(configuration.get("timed_passes"), 1, 1000, "timed pass count")
    _text(configuration.get("inference_order"), "inference order")
    models = _object(report.get("models"), "model provenance")
    if set(models) != VARIANTS:
        _fail("Comparison models must be exactly tiny and s")
    for variant, model in models.items():
        _object(model, f"{variant} model")
        _text(model.get("label"), "model label", 120)
        _integer(model.get("input_size"), 1, 4096, "model input size")
        _hex(model.get("sha256"), 64, "historical model digest")
        _text(model.get("path"), "historical model path")
    _object(report.get("provenance"), "historical software provenance")
    limits = report.get("limits")
    if not isinstance(limits, list) or len(limits) > 100:
        _fail("Invalid comparison limitations")
    for limit in limits:
        _text(limit, "comparison limitation")
    return report


def _rows(data, report):
    lines = data.splitlines()
    if not 1 <= len(lines) <= MAX_ROWS:
        _fail("Comparison must contain between 1 and 5000 paired image rows")
    rows = []
    context, segment, previous_index, previous_received = None, -1, -1, None
    for line in lines:
        if not line or len(line) > MAX_FRAME_LINE_BYTES:
            _fail("Comparison frame line is empty or exceeds 32 KiB")
        row = _object(_json(line, "frames.jsonl"), "frame row")
        if set(row) != ROW_FIELDS:
            _fail("Unexpected or missing comparison frame fields")
        index = _integer(row["index"], previous_index + 1,
                         report["source"]["examined_frame_rows"] - 1, "strictly increasing frame index")
        _integer(row["sequence"], 0, 2**53 - 1, "source sequence")
        _hex(row["video_id"], 32, "video ID")
        _hex(row["jpeg_sha256"], 64, "JPEG digest")
        for key in ("width", "height"):
            _integer(row[key], 1, 4096, "frame " + key)
        if row["width"] * row["height"] > 8_388_608:
            _fail("Frame dimensions exceed the archive bound")
        received = _number(row["received_at"], "frame receipt", minimum=0)
        _number(row["available_at"], "frame availability", minimum=received)
        _number(row["at_s"], "receipt offset")
        _number(row["available_at_s"], "availability offset", minimum=0)
        if type(row["excluded_before"]) is not bool:
            _fail("Invalid excluded-image boundary")
        current_context = row["video_id"], row["width"], row["height"]
        if current_context != context:
            segment += 1
        elif received <= previous_received:
            _fail("Frame receipts must increase within each source segment")
        if _integer(row["segment"], 0, MAX_ROWS - 1, "source segment") != segment:
            _fail("Source segment disagrees with source/dimension transitions")
        models = _object(row["models"], "paired model results")
        if set(models) != VARIANTS:
            _fail("Each image must contain exactly tiny and s results")
        for variant, model in models.items():
            _object(model, f"{variant} result")
            if set(model) != {"detections", "inference_ms", "processing_ms"}:
                _fail("Unexpected or missing model result fields")
            for key in ("inference_ms", "processing_ms"):
                _number(model[key], key, minimum=0)
            detections = model["detections"]
            if not isinstance(detections, list) or len(detections) > 16:
                _fail("Each model result supports at most 16 detections")
            identities = set()
            for detection in detections:
                _object(detection, "detection")
                if set(detection) != {"box", "confidence", "track_id"}:
                    _fail("Unexpected or missing detection fields")
                box = detection["box"]
                if not isinstance(box, list) or len(box) != 4:
                    _fail("Detection box needs four normalized coordinates")
                x, y, width, height = (_number(v, "box coordinate", minimum=0, maximum=1) for v in box)
                if width <= 0 or height <= 0 or x + width > 1 + 1e-9 or y + height > 1 + 1e-9:
                    _fail("Detection box exceeds the image")
                _number(detection["confidence"], "confidence", minimum=0, maximum=1)
                identity = _integer(detection["track_id"], 1, 2**53 - 1, "display track ID")
                if identity in identities:
                    _fail("Display track IDs must be unique within each model image")
                identities.add(identity)
        rows.append(row)
        context, previous_received, previous_index = current_context, received, index
    source = report["source"]
    if len(rows) != source["selected_frames"]:
        _fail("Selected frame count disagrees with paired rows")
    skipped = report.get("skipped_rows")
    if not isinstance(skipped, list) or len(skipped) > MAX_SAMPLES:
        _fail("Invalid skipped archive rows")
    excluded, skipped_ids, counts, prior = set(), set(), Counter(), -1
    for item in skipped:
        if not isinstance(item, dict) or set(item) != {"index", "reason"}:
            _fail("Invalid skipped archive row")
        idx = _integer(item["index"], prior + 1, source["examined_frame_rows"] - 1, "skipped frame index")
        if item["reason"] not in ("duplicate_image", "non_increasing_receipt"):
            _fail("Unknown skipped image reason")
        skipped_ids.add(idx)
        counts[item["reason"]] += 1
        if item["reason"] == "non_increasing_receipt":
            excluded.add(idx)
        prior = idx
    skipped_counts = _object(source.get("skipped_counts"), "skipped counts")
    for count in skipped_counts.values():
        _integer(count, 1, MAX_SAMPLES, "skipped count")
    if (skipped_ids.intersection(row["index"] for row in rows)
            or len(skipped) + len(rows) != source["examined_frame_rows"]
            or skipped_counts != dict(counts)):
        _fail("Skipped rows disagree with the selected input coverage")
    prior = -1
    for row in rows:
        if row["excluded_before"] != any(prior < idx < row["index"] for idx in excluded):
            _fail("Excluded-image boundary disagrees with skipped rows")
        prior = row["index"]
    return rows


@dataclass
class LoadedComparison:
    """Validated saved results. Exporters must finish with verify_unchanged()."""

    report: dict
    rows: list[dict]
    comparison_directory: Path
    source_directory: Path
    journal_revision: str
    visual_revision: str
    input_sha256: dict[str, str]
    _journal: RecordingArchive = field(repr=False)
    _visual: VisualArchive = field(repr=False)
    _binding: dict = field(repr=False)
    _inputs: tuple = field(repr=False)
    _frames: dict = field(repr=False)
    _identifier: str = field(repr=False)

    def read_frame(self, row):
        """Return pixels only for an originally validated comparison row."""
        try:
            index = _integer(row.get("index"), 0, MAX_SAMPLES - 1, "frame index")
            expected = self._frames.get(index)
            if expected is None or any(row.get(key) != expected[key] for key in expected):
                _fail("Requested frame is not bound to this comparison")
            frame = self._visual.frame_record(self._identifier, index, **self._binding,
                                               revision=self.visual_revision)
            if (any(frame[key] != expected[key] for key in FRAME_FIELDS)
                    or hashlib.sha256(frame["jpeg"]).hexdigest() != expected["jpeg_sha256"]):
                _fail(f"Comparison frame {index} metadata/JPEG differs from its native archive")
            return frame["jpeg"]
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
            if isinstance(exc, ComparisonExportError):
                raise
            raise ComparisonExportError(f"Unable to bind comparison frame to its archive: {exc}") from exc

    def verify_unchanged(self):
        """Recheck original report, paired rows, journal and visual file revisions."""
        for source in self._inputs:
            source.verify()
        try:
            journal = self._journal.metadata(self._identifier)
            visual = self._visual.metadata(self._identifier, **self._binding)
            if (journal["revision"] != self.journal_revision
                    or visual.get("revision") != self.visual_revision):
                _fail("Native source capture changed during export")
        except (ValueError, OSError, KeyError) as exc:
            if isinstance(exc, ComparisonExportError):
                raise
            raise ComparisonExportError(f"Native source capture changed or became invalid: {exc}") from exc


def load_comparison(comparison_dir, recordings_dir=None):
    """Load completed version-1 paired results and validate every source JPEG.

    A recordings_dir override permits relocation only: both original archive
    revisions and recorded source metadata must still agree. Historical software
    and model hashes are retained as data, without loading those models or files.
    The returned report summary is recomputed from rows, not trusted from disk.
    """
    directory = Path(comparison_dir).expanduser().resolve()
    report_data, report_input = _read_input(directory / "report.json", MAX_REPORT_BYTES)
    report = _report(_json(report_data, "report.json"))
    rows_data, rows_input = _read_input(directory / "frames.jsonl", MAX_FRAMES_BYTES)
    rows = _rows(rows_data, report)
    source = report["source"]
    source_directory = Path(recordings_dir if recordings_dir is not None else source["directory"]).expanduser().resolve()
    journal, visual = RecordingArchive(source_directory), VisualArchive(source_directory)
    try:
        metadata = journal.metadata(source["id"])
        context = metadata.get("context")
        if not isinstance(context, dict) or not context.get("run_id"):
            _fail("Native capture has no recorded run identity")
        binding = {"started_at": metadata["started_at"], "run_id": context["run_id"]}
        media = visual.metadata(source["id"], **binding)
        configuration = context["configuration"]
        expected = {"telemetry_revision": metadata["revision"], "visual_revision": media.get("revision"),
                    "duration_s": metadata["duration_s"], "environment": configuration["environment"],
                    "video_source": configuration["video_source"], "archive_frame_rows": media.get("frames"),
                    "visual_recording_dropped": media.get("dropped")}
        if (media.get("state") != "complete" or media["ended_at"] > metadata["ended_at"]
                or any(source.get(key) != value for key, value in expected.items())):
            _fail("Comparison source binding differs from the native recording metadata/revisions")
        for row in rows:
            if (row["at_s"] != row["received_at"] - metadata["started_at"]
                    or row["available_at_s"] != row["available_at"] - metadata["started_at"]):
                _fail("Comparison frame offsets differ from the original recorded clock")
        frame_keys = (*FRAME_FIELDS, "jpeg_sha256", "at_s", "available_at_s")
        loaded = LoadedComparison(report, rows, directory, source_directory,
            source["telemetry_revision"], source["visual_revision"],
            {"report.json": report_input.sha256, "frames.jsonl": rows_input.sha256},
            journal, visual, binding, (report_input, rows_input),
            {row["index"]: {key: row[key] for key in frame_keys} for row in rows}, source["id"])
        for row in rows:
            loaded.read_frame(row)
        report["summary"] = summarize(rows)
        loaded.verify_unchanged()
        return loaded
    except (ValueError, OSError, KeyError, TypeError) as exc:
        if isinstance(exc, ComparisonExportError):
            raise
        raise ComparisonExportError(f"Invalid comparison source binding: {exc}") from exc
