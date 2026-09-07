"""Archive analysis, provenance and storage across the HTTP boundary."""
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("pymavlink")

from argos.backends.mavlink import TelemetryLimits
from argos.console.config import ConsoleConfig, default_recordings_dir
from argos.console.context import capture_context
from argos.console import archive as archive_module
from test_console_archive import ID, client, journal, heartbeat, attitude, replay
from test_console_controls import configured, controlled_client, post


def analyse(client, **params):
    revision = client.get(f"/api/recordings/{ID}").json()["revision"]
    return client.get(f"/api/recordings/{ID}/analysis", params={"revision": revision, **params})


def test_analysis_describes_receipt_silences_and_filters_without_changing_live(client, tmp_path):
    journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1), (12., attitude(), 1),
                                      (14., heartbeat(), 42)])
    value = analyse(client, bins=5, gap_threshold_s=2).json()
    assert value["events"] == 3 and value["duration_s"] == 5
    assert sum(item["events"] for item in value["bins"]) == 3
    assert value["summary"] == {"mean_rate_hz": .6, "max_gap_s": 2.}
    assert value["gaps"] == [{"start_s": 2., "end_s": 4., "duration_s": 2., "boundary": "interior"}]
    filtered = analyse(client, system=1, component=1, message_id=0, bins=5, gap_threshold_s=2).json()
    assert filtered["events"] == 1
    assert filtered["gaps"] == [{"start_s": 1., "end_s": 5., "duration_s": 4., "boundary": "end"}]
    assert filtered["bins"][0]["max_age_s"] is None
    assert filtered["bins"][-1]["max_age_s"] == 4.
    assert client.get("/api/state").json()["telemetry"]["rx_messages"] == 0


@pytest.mark.parametrize("params", [{"system": 1}, {"component": 1}, {"bins": 0}, {"bins": 401},
                                    {"gap_threshold_s": 0}, {"gap_threshold_s": "nan"},
                                    {"gap_threshold_s": "inf"}, {"message_id": -1}])
def test_analysis_rejects_invalid_options(client, tmp_path, params):
    journal(tmp_path / f"{ID}.jsonl")
    assert analyse(client, **params).status_code == 422


def test_analysis_rejects_replaced_and_corrupt_journal(client, tmp_path):
    path = tmp_path / f"{ID}.jsonl"
    journal(path, [(11., heartbeat(), 1)])
    revision = client.get(f"/api/recordings/{ID}").json()["revision"]
    replacement = tmp_path / "replacement.jsonl"
    journal(replacement, [(12., heartbeat(), 1)])
    replacement.replace(path)
    assert client.get(f"/api/recordings/{ID}/analysis", params={"revision": revision}).status_code == 409
    path.write_bytes(path.read_bytes()[:-5])
    assert client.get(f"/api/recordings/{ID}/analysis", params={"revision": revision}).status_code == 422


def test_same_size_rewrite_invalidates_revision_even_with_identical_filesystem_timestamps(client, tmp_path, monkeypatch):
    # Reproduce a coarse timestamp filesystem deterministically. A valid rewrite
    # necessarily changes the completion digest, even if stat identity collides.
    monkeypatch.setattr(archive_module, "signature", lambda info: (info.st_dev, info.st_ino, info.st_size))
    path = tmp_path / f"{ID}.jsonl"
    before = journal(path, [(11., heartbeat(), 1)])
    meta = client.get(f"/api/recordings/{ID}").json()
    assert client.get(f"/api/recordings/{ID}/analysis", params={"revision": meta["revision"]}).status_code == 200
    after = journal(path, [(12., heartbeat(), 1)])
    assert len(before) == len(after)
    assert client.get(f"/api/recordings/{ID}/analysis", params={"revision": meta["revision"]}).status_code == 409
    assert client.get(meta["download_url"]).status_code == 409


def test_old_or_unknown_context_never_borrows_live_provenance(client, tmp_path):
    for context, status in [(None, "unavailable"), ({"format": "another.context", "version": 1}, "unknown")]:
        journal(tmp_path / f"{ID}.jsonl", context=context)
        meta = client.get(f"/api/recordings/{ID}").json()
        assert meta["context"] is None and meta["context_status"] == status
        assert meta["limits_origin"] == "analysis_defaults"
        assert meta["limits"]["heartbeat"] == 1.
        assert analyse(client).status_code == 200


def test_stored_configuration_and_limits_survive_restart_and_apply_to_replay(client, tmp_path):
    original = replace(configured(tmp_path), limits=TelemetryLimits(2.5, .7, .8), battery_age=4.)
    context = capture_context(original, "a" * 32, original.telemetry_endpoint,
                              captured_at_utc="2026-09-07T12:00:00Z")
    data = journal(tmp_path / f"{ID}.jsonl", [(11., heartbeat(), 1), (11., attitude(), 1)], context=context)
    meta = client.get(f"/api/recordings/{ID}").json()
    assert meta["context"] == context and meta["context_status"] == "recorded"
    assert meta["limits_origin"] == "capture" and meta["limits"]["heartbeat"] == 2.5
    value = replay(client, at=1.6).json()
    assert value["attitude"]["state"] == "recent" and value["attitude"]["age_limit_s"] == .7
    assert replay(client, at=3.).json()["heartbeat"]["state"] == "recent"
    assert client.get(meta["download_url"]).content == data
    assert client.get("/api/state").json()["configuration"]["environment"] == "unconfigured"


def test_malformed_known_capture_context_is_not_exposed(client, tmp_path):
    context = capture_context(ConsoleConfig(), "a" * 32, None)
    context["age_limits_s"]["heartbeat"] = -1
    journal(tmp_path / f"{ID}.jsonl", context=context)
    assert client.get(f"/api/recordings/{ID}").status_code == 422


def test_browser_capture_embeds_original_context_and_actual_udp_endpoint(tmp_path):
    with controlled_client(configured(tmp_path), real_udp=True) as (client, app, session, source, now):
        result = post(client, "/api/recordings/start", {})
        assert result.status_code == 200
        identifier = result.json()["id"]
        assert client.get(f"/api/recordings/{identifier}/analysis", params={"revision": "a" * 64}).status_code == 404
        now[0] = 1.
        assert post(client, "/api/recordings/stop", {}).status_code == 200
        meta = client.get(f"/api/recordings/{identifier}").json()
        context = meta["context"]
        assert context["run_id"] == session.run_id
        assert context["configuration"] == session.config.public()
        assert context["telemetry_endpoint"] == session._endpoint
        assert "127.0.0.1:0 " not in context["telemetry_endpoint"]
        assert context["captured_at_utc"].endswith("Z")


def test_storage_default_is_stable_outside_launch_directory_and_explicit_override_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = Path.home() / ".local/share/argos/recordings"
    assert default_recordings_dir() == expected
    monkeypatch.chdir(tmp_path)
    assert ConsoleConfig().recordings_dir == expected
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert ConsoleConfig().recordings_dir == tmp_path / "data/argos/recordings"
    monkeypatch.setenv("XDG_DATA_HOME", "relative-data")
    assert ConsoleConfig().recordings_dir == expected
    assert ConsoleConfig(recordings_dir=tmp_path).recordings_dir == tmp_path


def test_catalog_exposes_actual_storage_directory(client, tmp_path):
    assert client.get("/api/recordings").json()["directory"] == str(tmp_path.resolve())
