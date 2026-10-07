"""Frozen evidence, deterministic decisions and isolated comparison orchestration."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples import compare_trackers as comparison


def write_json(path, value):
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def cached_input(directory, *, sequence=1):
    (directory / "frames").mkdir()
    jpeg = b"original image bytes, never recompressed"
    (directory / "frames" / f"{sequence}.jpg").write_bytes(jpeg)
    frame = dict(sequence=sequence, sha256=hashlib.sha256(jpeg).hexdigest(),
                 received_at=.1, available_at=.15, worker_ms=12., width=64, height=48,
                 detections=[dict(box=[.1, .2, .2, .5], confidence=.9)], appearances=[None])
    path = directory / "inference.jsonl"
    path.write_text(json.dumps(frame) + "\n", encoding="utf-8")
    return frame, jpeg, comparison.digest(path)


def test_cache_preserves_original_pixels_measurements_and_clock(tmp_path):
    frame, jpeg, sha = cached_input(tmp_path)
    assert comparison.load_cache(tmp_path, sha) == [dict(frame, jpeg=jpeg)]


def test_linux_memory_peak_uses_current_executable_not_inherited_parent_high_water(monkeypatch):
    original = Path.read_text
    def read_status(path, **kwargs):
        if path == Path("/proc/self/status"):
            return "Name:\tpython\nVmHWM:\t12288 kB\nVmRSS:\t11648 kB\n"
        return original(path, **kwargs)
    monkeypatch.setattr(comparison.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", read_status)
    monkeypatch.setattr(comparison.resource, "getrusage", lambda *args:
        SimpleNamespace(ru_maxrss=196476))
    assert comparison.peak_rss() == 12288 * 1024


def test_linux_missing_current_process_high_water_cannot_fall_back_to_parent_peak(monkeypatch):
    original = Path.read_text
    def read_status(path, **kwargs):
        if path == Path("/proc/self/status"):
            return "Name:\tpython\nVmRSS:\t11648 kB\n"
        return original(path, **kwargs)
    monkeypatch.setattr(comparison.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", read_status)
    monkeypatch.setattr(comparison.resource, "getrusage", lambda *args:
        SimpleNamespace(ru_maxrss=196476))
    with pytest.raises((ValueError, RuntimeError)):
        comparison.peak_rss()


@pytest.mark.parametrize("changed", ["inference", "jpeg"])
def test_frozen_cache_rejects_changed_detections_or_originals(tmp_path, changed):
    _, _, sha = cached_input(tmp_path)
    if changed == "inference":
        (tmp_path / "inference.jsonl").write_text("{}\n")
        reason = "frozen inference cache changed"
    else:
        (tmp_path / "frames/1.jpg").write_bytes(b"replacement image")
        reason = "cached original JPEG changed"
    with pytest.raises(ValueError, match=reason):
        comparison.load_cache(tmp_path, sha)


@pytest.mark.parametrize("sequence", [True, 0, -1, 1.5, "../outside", 2**53])
def test_cached_sequence_cannot_be_ambiguous_or_form_an_arbitrary_path(tmp_path, sequence):
    frame, _, _ = cached_input(tmp_path)
    frame["sequence"] = sequence
    path = tmp_path / "inference.jsonl"
    path.write_text(json.dumps(frame) + "\n")
    with pytest.raises(ValueError, match="invalid cached sequence"):
        comparison.load_cache(tmp_path, comparison.digest(path))


def semantic_sample():
    return dict(events=[dict(at=.25, sequence=12, worker_ms=15., available_at=.22,
                            phase="tracking", target_id=3,
                            association=dict(native_track_ids=[3], native_detection_indices=[0],
                                             ephemeral_detections=[], tracker_wall_ms=1., tracker_cpu_ms=2.,
                                             image_provider_wall_ms=3., image_provider_cpu_ms=4.))],
                summary=dict(tracking_duration_s=.5, association_cost=dict(tracker_wall_ms=2.)),
                execution_cost=dict(wall_ms=6., cpu_ms=7.))


def test_semantic_hash_ignores_execution_cost_but_does_not_mutate_report():
    sample = semantic_sample()
    original = deepcopy(sample)
    changed = deepcopy(sample)
    changed["events"][0]["association"]["tracker_wall_ms"] = 800.
    changed["summary"]["association_cost"]["tracker_wall_ms"] = 900.
    changed["execution_cost"] = dict(wall_ms=1000., cpu_ms=2000.)
    assert comparison.semantic_digest(sample) == comparison.semantic_digest(changed)
    assert sample == original


@pytest.mark.parametrize("field,value", [
    ("at", .26), ("available_at", .23), ("worker_ms", 16.),
    ("sequence", 13), ("target_id", 4), ("phase", "paused"),
])
def test_semantic_hash_keeps_actual_schedules_and_identity_decisions(field, value):
    sample = semantic_sample()
    changed = deepcopy(sample)
    changed["events"][0][field] = value
    assert comparison.semantic_digest(sample) != comparison.semantic_digest(changed)


def test_semantic_hash_keeps_native_omissions_that_affect_target_recovery():
    sample = semantic_sample()
    changed = deepcopy(sample)
    changed["events"][0]["association"]["ephemeral_detections"] = [
        dict(detection_index=1, track_id=2**53-1)]
    assert comparison.semantic_digest(sample) != comparison.semantic_digest(changed)


def result_for(tracker, mode, cost=1., semantic="same-decisions", window_names=("loss",)):
    return dict(tracker=tracker, mode=mode, semantic_sha256=semantic,
                cost=dict(process_wall_s=cost, process_cpu_s=cost*2,
                          peak_rss_bytes=cost*1000, baseline_peak_rss_bytes=cost*900),
                windows=[dict(name=name, summary=dict(tracking_duration_s=.5),
                              frame_decisions=[dict(association=dict(
                                  tracker_wall_ms=cost, tracker_cpu_ms=cost*2,
                                  image_provider_wall_ms=cost*3, image_provider_cpu_ms=cost*4))])
                         for name in window_names])


def test_aggregate_never_pools_methods_modes_or_hides_repeat_divergence():
    runs = []
    for i, tracker in enumerate(comparison.TRACKERS):
        for j, mode in enumerate(comparison.MODES):
            for repetition in range(3):
                runs.append(result_for(tracker, mode, 100*i+10*j+repetition+1))
    runs[-1]["semantic_sha256"] = "different-decisions"
    result = comparison.aggregate(runs)
    assert result["image"]["recorded"]["cost"]["process_wall_s"]["median"] == 2.
    assert result["botsort"]["simulated"]["cost"]["process_wall_s"]["median"] == 212.
    assert result["bytetrack"]["recorded"]["association_cost"]["tracker_wall_ms"]["count"] == 3
    assert result["image"]["simulated"]["semantic_repeatable"] is True
    assert result["botsort"]["simulated"]["semantic_repeatable"] is False
    assert result["botsort"]["simulated"]["repeats"] == 3


@pytest.mark.parametrize("flag,value", [
    ("--repeats", "1"), ("--repeats", "11"), ("--repeats", "2.5"),
    ("--threads", "0"), ("--max-hz", "0"), ("--max-hz", "10.0"),
    ("--max-frames", "0"), ("--max-frames", "5001"),
])
def test_invalid_configuration_fails_before_source_or_model_access(monkeypatch, tmp_path, flag, value):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid configuration reached source/model access")
    monkeypatch.setattr(comparison, "read_windows", forbidden)
    monkeypatch.setattr(comparison, "NativeFlight", forbidden)
    monkeypatch.setattr(comparison, "YoloXPersonDetector", forbidden)
    output = tmp_path / "output"
    with pytest.raises(SystemExit) as error:
        comparison.main(["--flight-dir", str(tmp_path / "source"),
                         "--windows", str(tmp_path / "windows.json"),
                         "--bundle", str(tmp_path / "model"),
                         "--output-dir", str(output), flag, value])
    assert error.value.code != 0
    assert not output.exists()


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Exercise real freezing/orchestration without a model or heavy subprocess."""
    source = tmp_path / "native.flight"
    source.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    model = bundle / "model.onnx"
    model.write_bytes(b"frozen model")
    model_sha = comparison.digest(model)
    windows = tmp_path / "windows.json"
    write_json(windows, dict(windows=[dict(name="loss", start=.1, end=.5,
        selection=dict(at=.2, box=[.1, .2, .2, .5]))]))
    output = tmp_path / "output"
    inferred = []

    class Native:
        directory = source
        hashes = {"source": "source-hash"}
        drop_counts = dict(camera_frames=0, events=0)
        def __init__(self, path):
            assert path == source
        def read_frames(self, windows, max_frames):
            for sequence in (1, 2):
                jpeg = f"original-{sequence}".encode()
                yield dict(sequence=sequence, jpeg=jpeg, sha256=hashlib.sha256(jpeg).hexdigest(),
                           received_at=sequence/10, available_at=sequence/10+.01,
                           width=64, height=48, historical=dict(private="historic"))
        def verify(self):
            pass

    def infer(frame, detector, encoder):
        inferred.append(frame["sequence"])
        return dict(frame, worker_ms=3., inference_ms=1., appearances=[None],
                    detections=[dict(box=[.1, .2, .2, .5], confidence=.9)],
                    timings={stage: dict(wall_ms=1., cpu_ms=2.)
                             for stage in ("decode", "detect", "appearance")})

    monkeypatch.setattr(comparison, "NativeFlight", Native)
    monkeypatch.setattr(comparison, "YoloXPersonDetector", lambda **kwargs:
        SimpleNamespace(model=SimpleNamespace(filename=model.name, sha256=model_sha),
                        bundle=SimpleNamespace(path=bundle / "manifest.json")))
    monkeypatch.setattr(comparison, "AppearanceEncoder", object)
    monkeypatch.setattr(comparison, "infer", infer)
    monkeypatch.setattr(comparison, "provenance", lambda: dict(source_sha256={}))
    monkeypatch.setattr(comparison.importlib.metadata, "version", lambda name: "test-version")
    monkeypatch.setattr(comparison, "write_viewer", lambda *args: None)
    args = ["--flight-dir", str(source), "--windows", str(windows), "--bundle", str(bundle),
            "--output-dir", str(output)]
    return SimpleNamespace(args=args, output=output, model=model, windows=windows,
                           model_sha=model_sha, inferred=inferred)


def test_cli_freezes_once_rotates_isolated_passes_and_excludes_warmups(pipeline, monkeypatch):
    calls = []
    def run_pass(output, windows, args, tracker, mode, label, cache_hash):
        cached = comparison.load_cache(output, cache_hash)
        assert [frame["sequence"] for frame in cached] == [1, 2]
        assert [frame["jpeg"] for frame in cached] == [b"original-1", b"original-2"]
        assert all("historical" not in frame for frame in cached)
        calls.append((tracker, mode, label, cache_hash, deepcopy(windows)))
        return result_for(tracker, mode, cost=999. if label == "warmup" else 2.)
    monkeypatch.setattr(comparison, "run_pass", run_pass)
    assert comparison.main(pipeline.args) == 0
    assert pipeline.inferred == [1, 1, 1, 2]  # Two warmups then one cache pass.
    assert len(calls) == 24
    assert len({call[3] for call in calls}) == 1
    assert all(call[4] == calls[0][4] for call in calls)
    for repetition, order in enumerate((comparison.TRACKERS,
                                       ("bytetrack", "botsort", "image"),
                                       ("botsort", "image", "bytetrack")), 1):
        measured = [(call[0], call[1]) for call in calls if call[2] == f"repeat-{repetition}"]
        assert measured == [(tracker, mode) for tracker in order for mode in comparison.MODES]
    report = json.loads((pipeline.output / "report.json").read_text())
    assert report["state"] == "complete"
    assert report["model"]["sha256"] == pipeline.model_sha
    assert report["cache"]["frames"] == 2
    assert report["comparison"]["image"]["recorded"]["repeats"] == 3
    assert report["comparison"]["image"]["recorded"]["cost"]["process_wall_s"]["max"] == 2.


@pytest.mark.parametrize("error", [RuntimeError("subprocess failed"),
                                 ImportError("optional tracker dependency missing"), KeyboardInterrupt()])
def test_failed_or_interrupted_pass_preserves_cache_and_marks_report(pipeline, monkeypatch, error):
    def fail(*args):
        raise error
    monkeypatch.setattr(comparison, "run_pass", fail)
    with pytest.raises(SystemExit) as result:
        comparison.main(pipeline.args)
    assert result.value.code == 1
    report = json.loads((pipeline.output / "report.json").read_text())
    assert report["state"] == ("interrupted" if isinstance(error, KeyboardInterrupt) else "failed")
    assert len(comparison.load_cache(pipeline.output, report["cache"]["sha256"])) == 2
    assert report["model"]["sha256"] == pipeline.model_sha
    assert "comparison" not in report


@pytest.mark.parametrize("bundle_as_manifest", [False, True])
def test_output_inside_bundle_is_rejected_without_touching_inputs(pipeline, monkeypatch, bundle_as_manifest):
    bundle = pipeline.model.parent
    manifest = bundle / "manifest.json"
    write_json(manifest, dict(model="model.onnx"))
    before = {str(path.relative_to(bundle)): path.read_bytes() for path in bundle.rglob("*") if path.is_file()}
    reached_source = []
    monkeypatch.setattr(comparison, "read_windows", lambda *args: reached_source.append(True))
    args = list(pipeline.args)
    args[args.index("--output-dir") + 1] = str(bundle / "comparison")
    args[args.index("--bundle") + 1] = str(manifest if bundle_as_manifest else bundle)
    with pytest.raises(SystemExit) as result:
        comparison.main(args)
    assert result.value.code == 1
    assert reached_source == []
    assert not (bundle / "comparison").exists()
    assert {str(path.relative_to(bundle)): path.read_bytes() for path in bundle.rglob("*") if path.is_file()} == before


def test_missing_optional_detector_dependency_leaves_explicit_failed_report(pipeline, monkeypatch):
    def unavailable(**kwargs):
        raise ImportError("missing optional image runtime")
    monkeypatch.setattr(comparison, "YoloXPersonDetector", unavailable)
    with pytest.raises(SystemExit) as result:
        comparison.main(pipeline.args)
    assert result.value.code == 1
    report = json.loads((pipeline.output / "report.json").read_text())
    assert report["state"] == "failed"
    assert report["error"] == "missing optional image runtime"
    assert pipeline.inferred == []


@pytest.mark.parametrize("changed", ["model", "windows", "cache"])
def test_final_integrity_check_cannot_mark_replaced_inputs_complete(pipeline, monkeypatch, changed):
    calls = []
    def run_pass(output, windows, args, tracker, mode, label, cache_hash):
        calls.append(label)
        if len(calls) == 24:
            target = {"model": pipeline.model, "windows": pipeline.windows,
                      "cache": output / "inference.jsonl"}[changed]
            target.write_bytes(target.read_bytes() + b"\nchanged")
        return result_for(tracker, mode)
    monkeypatch.setattr(comparison, "run_pass", run_pass)
    with pytest.raises(SystemExit) as result:
        comparison.main(pipeline.args)
    assert result.value.code == 1
    report = json.loads((pipeline.output / "report.json").read_text())
    assert report["state"] == "failed"
    assert "changed during comparison" in report["error"]


def test_subprocess_job_pins_cache_budget_and_preserves_failure_log(tmp_path, monkeypatch):
    (tmp_path / "passes").mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1, stdout="started\n", stderr="specific failure\n")
    monkeypatch.setattr(comparison.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="repeat-1-botsort-recorded failed"):
        comparison.run_pass(tmp_path, [dict(name="window")], SimpleNamespace(threads=4, max_hz=10),
                            "botsort", "recorded", "repeat-1", "frozen-cache-sha")
    command, kwargs = calls[0]
    job = json.loads(Path(command[-1]).read_text())
    assert command[-2] == "--worker"
    assert job["cache_sha256"] == "frozen-cache-sha"
    assert job["threads"] == 4
    assert job["max_hz"] == 10
    assert kwargs["env"]["OMP_NUM_THREADS"] == "4"
    assert kwargs["env"]["OPENBLAS_NUM_THREADS"] == "4"
    assert kwargs["env"]["MKL_NUM_THREADS"] == "4"
    assert kwargs["timeout"] == 300
    assert (tmp_path / "passes/repeat-1-botsort-recorded.log").read_text() == "started\nspecific failure\n"
