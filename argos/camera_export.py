"""Export a finalized camera archive to a timestamped editing MP4, offline.

The original receive times remain authoritative. VFR holds the preceding image
across gaps; it neither reconstructs missing images nor measures exposure time.
FFmpeg with libx264 is an optional external executable, used only by this CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from argos.console.camera_recording import (
    INDEX_NAME, MANIFEST_NAME, MAX_BYTES, MAX_DURATION_S, MAX_FRAMES,
    MAX_INDEX_LINE_BYTES, MEDIA_NAME,
)
from argos.console.video import MAX_DIMENSION, MAX_JPEG_BYTES, MAX_PIXELS

TIME_SCALE = 1_000_000
MAX_MANIFEST_BYTES = 1024 * 1024
DROP_FIELDS = ("dropped_queue", "dropped_contention", "dropped_invalid", "dropped_limit")


def _integer(value, name, minimum=0, maximum=None):
    if (type(value) is not int or value < minimum
            or maximum is not None and value > maximum):
        raise ValueError(f"Invalid {name}")
    return value


def _number(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError(f"Invalid {name}: expected a finite nonnegative time")
    return value


def _json(raw):
    def reject_constant(value):
        raise ValueError(f"Nonfinite JSON number: {value}")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result
    value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _fingerprint(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Archive input must be a regular file: {path.name}")
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def _same(value, expected, name):
    if (value != expected
            or type(expected) in (bool, int) and type(value) is not type(expected)):
        raise ValueError(f"Inconsistent {name}")


def extract_archive(directory, destination, *, allow_drops=False, gap_threshold_s=.25):
    """Validate bounded inputs and copy indexed JPEGs into an owned temp directory."""
    from PIL import Image

    directory, destination = Path(directory).resolve(), Path(destination)
    gap_threshold_s = _number(gap_threshold_s, "gap threshold")
    if gap_threshold_s == 0:
        raise ValueError("Gap threshold must be positive")
    paths = {name: directory / name for name in (MANIFEST_NAME, INDEX_NAME, MEDIA_NAME)}
    revisions = {name: _fingerprint(path) for name, path in paths.items()}
    if revisions[MANIFEST_NAME][2] > MAX_MANIFEST_BYTES:
        raise ValueError("Camera manifest exceeds its size bound")
    manifest = _json(paths[MANIFEST_NAME].read_bytes())
    _same(manifest.get("schema"), 1, "manifest schema")
    _same(manifest.get("format"), "mjpeg_with_receive_timestamp_index", "archive format")
    if manifest.get("state") != "complete" or manifest.get("writer_stopped") is not True:
        raise ValueError("Camera archive is not finalized; stop recording and wait for completion")
    if manifest.get("error") or manifest.get("discarded_error") != 0:
        raise ValueError("Camera archive contains a writer error")
    session = manifest.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 128:
        raise ValueError("Invalid session_id")
    started = _number(manifest.get("started_at"), "started_at")
    ended = _number(manifest.get("ended_at"), "ended_at")
    if ended < started or ended - started > MAX_DURATION_S + 1:
        raise ValueError("Invalid archive time span")
    count = _integer(manifest.get("written_frames"), "written_frames", 1, MAX_FRAMES)
    _same(manifest.get("accepted_frames"), count, "accepted_frames")
    _same(manifest.get("buffered_frames"), 0, "buffered_frames")
    _same(manifest.get("buffered_bytes"), 0, "buffered_bytes")
    size = _integer(manifest.get("size_bytes"), "size_bytes", 4, MAX_BYTES)
    index_size = _integer(manifest.get("index_bytes"), "index_bytes", 1,
                          MAX_FRAMES * MAX_INDEX_LINE_BYTES)
    _same(size, revisions[MEDIA_NAME][2], "media size")
    _same(index_size, revisions[INDEX_NAME][2], "index size")
    _same(manifest.get("media"), MEDIA_NAME, "media name")
    _same(manifest.get("index"), INDEX_NAME, "index name")
    drops = sum(_integer(manifest.get(key), key) for key in DROP_FIELDS)
    _same(manifest.get("dropped_frames"), drops, "dropped_frames")
    bounded_stop = manifest.get("reason") in {"duration_limit", "size_limit", "frame_limit"}
    _same(manifest.get("complete"), drops == 0 and not bounded_stop, "complete flag")
    if (drops or bounded_stop) and not allow_drops:
        raise ValueError("Archive reports lost frames or a capture limit; inspect it and use --allow-drops explicitly")

    frames, gaps = [], []
    media_hash, index_hash = hashlib.sha256(), hashlib.sha256()
    offset, previous = 0, None
    with paths[INDEX_NAME].open("rb") as index, paths[MEDIA_NAME].open("rb") as media:
        while raw := index.readline(MAX_INDEX_LINE_BYTES + 1):
            if len(raw) > MAX_INDEX_LINE_BYTES or not raw.endswith(b"\n"):
                raise ValueError("Truncated or oversized camera index line")
            if len(frames) >= count:
                raise ValueError("Camera index has more frames than its manifest")
            index_hash.update(raw)
            row = _json(raw)
            _same(row.get("schema"), 1, "frame schema")
            _same(row.get("session_id"), session, "frame session")
            _same(row.get("frame"), len(frames), "frame number")
            _same(row.get("offset"), offset, "frame byte offset")
            sequence = _integer(row.get("sequence"), "sequence", 1)
            received = _number(row.get("received_at"), "received_at")
            elapsed = _number(row.get("elapsed_s"), "elapsed_s")
            if (not started <= received <= ended
                    or not math.isclose(elapsed, received - started, rel_tol=0, abs_tol=1e-8)):
                raise ValueError("Frame time is inconsistent with the recording session")
            width = _integer(row.get("width"), "width", 1, MAX_DIMENSION)
            height = _integer(row.get("height"), "height", 1, MAX_DIMENSION)
            if width * height > MAX_PIXELS:
                raise ValueError("Frame exceeds pixel bound")
            if (row.get("source") not in {"device", "gazebo"}
                    or not isinstance(row.get("endpoint"), str) or len(row["endpoint"]) > 1024
                    or not isinstance(row.get("source_id"), str)
                    or not 1 <= len(row["source_id"]) <= 64):
                raise ValueError("Invalid frame source")
            stamp = row.get("source_stamp")
            if stamp is not None:
                if not isinstance(stamp, dict):
                    raise ValueError("Invalid source_stamp")
                _integer(stamp.get("sec"), "source_stamp.sec")
                _integer(stamp.get("nsec"), "source_stamp.nsec", 0, 999_999_999)
            pts = round((received - (frames[0]["received_at"] if frames else received)) * TIME_SCALE)
            if previous:
                if any(row[key] != previous[key] for key in ("source_id", "source", "endpoint", "width", "height")):
                    raise ValueError("Source or dimensions changed; split this archive before export")
                if sequence <= previous["sequence"] or pts <= previous["video_pts_us"]:
                    raise ValueError("Frame sequences and receive times must increase at microsecond precision")
                delta = received - previous["received_at"]
                missing = sequence - previous["sequence"] - 1
                if missing or delta > gap_threshold_s:
                    gaps.append({"after_frame": previous["frame"], "before_frame": row["frame"],
                                 "from_elapsed_s": previous["elapsed_s"], "to_elapsed_s": elapsed,
                                 "interval_s": delta, "missing_sequences": missing,
                                 "reason": "sequence_gap" if missing else "long_receive_interval"})
            length = _integer(row.get("size_bytes"), "frame size", 4, MAX_JPEG_BYTES)
            if offset + length > size:
                raise ValueError("Frame exceeds media bounds")
            jpeg = media.read(length)
            if len(jpeg) != length or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise ValueError("Truncated or invalid indexed JPEG")
            try:
                with Image.open(io.BytesIO(jpeg)) as image:
                    if image.format != "JPEG" or image.size != (width, height):
                        raise ValueError("JPEG dimensions do not match the index")
                    image.load()
            except (OSError, SyntaxError) as exc:
                raise ValueError(f"Invalid JPEG at frame {row['frame']}") from exc
            filename = f"frame-{len(frames):06d}.jpg"
            with (destination / filename).open("xb") as output:
                output.write(jpeg)
            media_hash.update(jpeg)
            offset += length
            row.update(video_pts_us=pts, jpeg_sha256=hashlib.sha256(jpeg).hexdigest(), extracted_file=filename)
            frames.append(row)
            previous = row
    _same(len(frames), count, "index frame count")
    _same(offset, size, "indexed media size")
    _same(manifest.get("first_received_at"), frames[0]["received_at"], "first received time")
    _same(manifest.get("last_received_at"), frames[-1]["received_at"], "last received time")
    hashes = {MEDIA_NAME: media_hash.hexdigest(), INDEX_NAME: index_hash.hexdigest(),
              MANIFEST_NAME: _sha256(paths[MANIFEST_NAME])}
    for name, path in paths.items():
        _same(_fingerprint(path), revisions[name], f"unchanged source {name}")
    return {"manifest": manifest, "frames": frames, "gaps": gaps, "source_sha256": hashes,
            "source_revisions": revisions, "gap_threshold_s": gap_threshold_s}


def write_concat(frames, path):
    # Round absolute offsets first, so tiny per-frame rounding errors do not accumulate.
    lines = ["ffconcat version 1.0"]
    for index, frame in enumerate(frames):
        lines.extend([f"file {frame['extracted_file']}", f"option framerate {TIME_SCALE}"])
        duration = (frames[index + 1]["video_pts_us"] - frame["video_pts_us"]
                    if index + 1 < len(frames) else 1)
        lines.append(f"duration {duration / TIME_SCALE:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def ffmpeg_command(executable, concat, output):
    return [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-n",
            "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-map", "0:v:0", "-an", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-bf", "0", "-fps_mode", "vfr", "-enc_time_base", "1:1000000",
            "-video_track_timescale", str(TIME_SCALE), "-movflags", "+faststart", str(output)]


def _run(command, work, timeout, *, stdout=subprocess.DEVNULL):
    with tempfile.TemporaryFile() as errors:
        try:
            subprocess.run(command, cwd=work, stdin=subprocess.DEVNULL, stdout=stdout,
                           stderr=errors, check=True, timeout=timeout, shell=False)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 4000))
            detail = errors.read().decode("utf-8", errors="replace").strip()
            raise ValueError(f"FFmpeg failed or timed out: {detail or exc}") from exc


def verify_video(executable, video, frames, work, timeout):
    """Decode the actual MP4 and verify every retained frame's presentation time."""
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
               "-i", str(video), "-map", "0:v:0", "-an", "-fps_mode", "passthrough",
               "-enc_time_base", "1:1000000", "-f", "framehash", "-"]
    with tempfile.TemporaryFile() as output:
        _run(command, work, timeout, stdout=output)
        output.seek(0)
        position, end_us, timebase = 0, 0, False
        for raw in output:
            if len(raw) > 4096:
                raise ValueError("Unexpected oversized FFmpeg verification line")
            line = raw.decode("ascii").strip()
            if line.startswith("#tb 0:"):
                timebase = line.split(":", 1)[1].strip() == "1/1000000"
            if not line or line.startswith("#"):
                continue
            fields = line.split(",")
            if not timebase or len(fields) != 6 or position >= len(frames):
                raise ValueError("Unexpected FFmpeg decoded frame output")
            pts, duration = int(fields[2]), int(fields[3])
            if pts != frames[position]["video_pts_us"] or duration <= 0:
                raise ValueError("Exported MP4 changed camera presentation timestamps")
            end_us = pts + duration
            position += 1
    _same(position, len(frames), "decoded output frame count")
    return end_us


def export_camera(directory, output, *, ffmpeg="ffmpeg", allow_drops=False,
                  gap_threshold_s=.25, timeout=3600.):
    directory, output = Path(directory).resolve(), Path(output).absolute()
    if output.suffix.lower() != ".mp4":
        raise ValueError("Output must have an .mp4 extension")
    if output.resolve().is_relative_to(directory):
        raise ValueError("Export outside the source recording directory")
    sidecar = output.with_suffix(output.suffix + ".json")
    if any(path.exists() or path.is_symlink() for path in (output, sidecar)):
        raise ValueError("Output video or provenance sidecar already exists; choose a new name")
    if not output.parent.is_dir():
        raise ValueError("Output parent directory must already exist")
    timeout = _number(timeout, "timeout")
    if timeout == 0:
        raise ValueError("Timeout must be positive")
    executable = shutil.which(str(ffmpeg))
    if executable is None:
        raise ValueError("FFmpeg is not installed; provide an existing executable with --ffmpeg")
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=".argos-camera-export-", dir=output.parent) as temporary:
        work = Path(temporary)
        archive = extract_archive(directory, work, allow_drops=allow_drops, gap_threshold_s=gap_threshold_s)
        frames, manifest = archive["frames"], archive["manifest"]
        concat, encoded = work / "frames.ffconcat", work / "camera.mp4"
        write_concat(frames, concat)
        command = ffmpeg_command(executable, concat, encoded)
        _run(command, work, timeout)
        if not encoded.is_file() or encoded.stat().st_size == 0:
            raise ValueError("FFmpeg produced no video")
        end_us = verify_video(executable, encoded, frames, work, timeout)
        # Refuse publication if any recording input changed during extraction/encoding.
        for name, expected in archive["source_sha256"].items():
            path = directory / name
            _same(_fingerprint(path), archive["source_revisions"][name], f"unchanged source {name}")
            _same(_sha256(path), expected, f"unchanged source hash {name}")
        report = {
            "schema": 1, "state": "complete", "session_id": manifest["session_id"],
            "source_directory": str(directory), "source_sha256": archive["source_sha256"],
            "source_manifest": manifest, "output": output.name, "output_sha256": _sha256(encoded),
            "output_size_bytes": encoded.stat().st_size, "frame_count": len(frames),
            "timestamp_basis": "host_monotonic_camera_receipt_not_exposure",
            "video_time_base": "1/1000000", "source_offset_s": frames[0]["elapsed_s"],
            "first_received_at": frames[0]["received_at"], "last_received_at": frames[-1]["received_at"],
            "source_span_s": frames[-1]["received_at"] - frames[0]["received_at"],
            "video_duration_s": end_us / TIME_SCALE,
            "last_frame_duration_s": (end_us - frames[-1]["video_pts_us"]) / TIME_SCALE,
            "end_policy": "Ends after the final retained image's encoded packet; does not extend to recording stop.",
            "gap_policy": "Hold the preceding retained image until the next receipt; no interpolated or duplicated frames.",
            "gap_threshold_s": gap_threshold_s, "gaps": archive["gaps"],
            "unobserved_prefix_s": frames[0]["elapsed_s"],
            "unobserved_tail_s": manifest["ended_at"] - frames[-1]["received_at"],
            "allow_drops": allow_drops,
            "limitations": "Gap intervals locate missing evidence, not its cause or the exact times of missing frames. Hashes establish export provenance, not authenticity before export.",
            "encoding": {"codec": "libx264", "crf": 18, "pixel_format": "yuv420p",
                         "odd_dimensions": "Pad right/bottom to the next even dimension",
                         "ffmpeg": executable, "command": command},
            "frames": [{key: value for key, value in frame.items() if key != "extracted_file"}
                       for frame in frames],
            "export_wall_time_s": time.monotonic() - began,
        }
        metadata = work / "camera.mp4.json"
        metadata.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        # Hard links publish complete files without replacing a raced-in destination.
        os.link(encoded, output)
        try:
            os.link(metadata, sidecar)
        except BaseException:
            if output.exists() and os.path.samefile(encoded, output):
                output.unlink()
            raise
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path, help="finalized folder containing camera.json/index/MJPEG")
    parser.add_argument("output", type=Path, help="new .mp4 path outside the source folder (parent must exist)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="existing FFmpeg executable with libx264")
    parser.add_argument("--allow-drops", "--allow-incomplete", action="store_true",
                        help="explicitly export a finalized archive with lost frames or a capture limit")
    parser.add_argument("--gap-threshold", type=float, default=.25, metavar="SECONDS",
                        help="report receive intervals exceeding this duration (default: 0.25)")
    parser.add_argument("--timeout", type=float, default=3600., metavar="SECONDS",
                        help="timeout for each offline FFmpeg pass (default: 3600)")
    args = parser.parse_args(argv)
    try:
        result = export_camera(args.recording, args.output, ffmpeg=args.ffmpeg,
                               allow_drops=args.allow_drops, gap_threshold_s=args.gap_threshold,
                               timeout=args.timeout)
    except KeyboardInterrupt:
        print("Camera export interrupted; source files are unchanged.", file=sys.stderr)
        return 130
    except (OSError, ValueError, ImportError) as exc:
        print(f"Camera export failed: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {result['frame_count']} frames to {args.output}; "
          f"{result['video_duration_s']:.6f}s, {len(result['gaps'])} reported gaps.")
    print(f"Provenance and original receive timestamps: {args.output}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
