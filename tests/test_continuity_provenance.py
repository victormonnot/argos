"""Historical evidence may survive relocation, never unrelated source edits."""
import hashlib

import pytest

from argos.harness.continuity_provenance import current_source_hashes


OLD_REPLAY = "argos/perception/continuity_replay.py"
REPLAY = "argos/harness/continuity_replay.py"
OLD_RUNNER = b'from argos.perception.continuity_replay import replay\nsource = "argos/perception/continuity_replay.py"\n'
RUNNER = OLD_RUNNER.replace(b"argos.perception.continuity_replay", b"argos.harness.continuity_replay").replace(
    OLD_REPLAY.encode(), REPLAY.encode())


def sha(content):
    return hashlib.sha256(content).hexdigest()


def source(repo, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("historical", [False, True])
def test_relocated_dependencies_bind_actual_paths_and_hashes(tmp_path, historical):
    replay = b"def replay(): pass\n"
    source(tmp_path, REPLAY, replay)
    for name in ("examples/compare_trackers.py", "examples/diagnose_continuity.py"):
        source(tmp_path, name, RUNNER)
    recorded = {OLD_REPLAY if historical else REPLAY: sha(replay),
                **{name: sha(OLD_RUNNER if historical else RUNNER)
                   for name in ("examples/compare_trackers.py", "examples/diagnose_continuity.py")}}
    unchanged = dict(recorded)
    current = current_source_hashes(tmp_path, recorded)
    assert current == {REPLAY: sha(replay), "examples/compare_trackers.py": sha(RUNNER),
                       "examples/diagnose_continuity.py": sha(RUNNER)}
    assert recorded == unchanged
    assert not (tmp_path / OLD_REPLAY).exists()


@pytest.mark.parametrize("name", [REPLAY, "examples/compare_trackers.py",
                                  "examples/diagnose_continuity.py", "argos/console/yaw_preview.py"])
def test_relocation_cannot_hide_other_source_changes(tmp_path, name):
    original = b"original production source\n" if name == REPLAY else OLD_RUNNER
    current = original if name == REPLAY else RUNNER
    source(tmp_path, name, current + b"unrelated change\n")
    recorded = {OLD_REPLAY if name == REPLAY else name: sha(original)}
    with pytest.raises(ValueError, match="dependency changed"):
        current_source_hashes(tmp_path, recorded)


def test_only_the_declared_runner_files_can_use_import_relocation(tmp_path):
    name = "other/compare_trackers.py"
    source(tmp_path, name, RUNNER)
    with pytest.raises(ValueError, match="dependency changed"):
        current_source_hashes(tmp_path, {name: sha(OLD_RUNNER)})


def test_replay_extension_exception_is_explicit_and_does_not_include_production(tmp_path):
    source(tmp_path, REPLAY, b"new replay extension")
    production = "argos/console/yaw_preview.py"
    source(tmp_path, production, b"changed production source")
    assert current_source_hashes(tmp_path, {OLD_REPLAY: sha(b"previous replay")},
                                 allowed_changes={REPLAY}) == {REPLAY: sha(b"new replay extension")}
    with pytest.raises(ValueError, match="dependency changed"):
        current_source_hashes(tmp_path, {OLD_REPLAY: sha(b"previous replay"), production: sha(b"original")},
                              allowed_changes={REPLAY})


def test_old_and_new_paths_cannot_shadow_a_recorded_binding(tmp_path):
    content = b"replay"
    source(tmp_path, REPLAY, content)
    with pytest.raises(ValueError, match="duplicate source dependency"):
        current_source_hashes(tmp_path, {OLD_REPLAY: sha(content), REPLAY: sha(content)})
