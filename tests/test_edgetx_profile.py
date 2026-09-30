"""Synthetic public models only; personal radio backups remain outside Git."""
from pathlib import Path

import pytest

from argos.backends.edgetx_profile import (
    ProfileError, build_artifact, prepare_profile, validate_profile,
)


@pytest.fixture
def manual() -> bytes:
    text = '''semver: 2.12.4
header:
   name: "Manual"
   modelId:
      0:
         val: 17
moduleData:
   0:
      type: TYPE_CROSSFIRE
      channelsStart: 0
      channelsCount: 16
      mod:
         crsf:
            telemetryBaudrate: 0
mixData:
'''
    for index, source in enumerate(("I3", "I1", "I2", "I0", "SA", "SB", "SC", "SD", "SE", "P1")):
        text += f''' -
   destCh: {index}
   srcRaw: "{source}"
   weight: 100
   offset: 0
   mltpx: ADD
   swtch: "NONE"
   flightModes: 000000000
   carryTrim: 0
   delayUp: 0
   delayDown: 0
   speedUp: 0
   speedDown: 0
'''
    text += 'expoData:\n'
    for index, source in enumerate(("Rud", "Ele", "Thr", "Ail")):
        text += f' -\n   chn: {index}\n   srcRaw: {source}\n   weight: 100\n'
    text += '''limitData:
   3:
      curve: 12
      offset: 0
curves:
   11:
      name: "OWN"
flightModeData:
   0:
      trim:
         3:
            value: 7
timers:
   0:
      mode: OFF
      value: 01234
switchWarningState: AuBuCuDu
modelRegistrationID: "test\\x15id"
'''
    return text.encode()


def test_prepare_preserves_pilot_settings_and_receiver_identity(manual):
    result = prepare_profile(manual)
    report = validate_profile(result, source=manual)
    assert report["model_name"] == "ARGOS FLY"
    assert report["preservation_checked"] is True
    assert report["hardware_validated"] is False
    assert report["yaw_channel"] == 4
    assert b'val: 17' in result
    assert b'mode: OFF\n      value: 01234' in result
    assert b'curve: 12' in result and b'value: 7' in result
    assert b'modelRegistrationID: "test\\x15id"' in result
    assert result.count(b'flightModes: 000000000') == 11
    assert b'FUNC_APOS' in result and b'"Rud,25"' in result
    assert b'"L9,SC1"' in result and b'"L6,SC0"' in result
    assert b'"lua(0,0)"' in result
    assert b'name: "ArgYaw"' in result and b'name: "FlipLo"' in result  # Pocket mix names: max 6 chars.
    assert result.count(b'duration: 2') == 2  # Native Sticky must see short takeover pulses.
    # Unchanged root blocks survive byte-for-byte; no YAML round-trip of them.
    assert result[result.index(b'expoData:'):result.index(b'logicalSw:')] == manual[manual.index(b'expoData:'):]


def test_crlf_and_original_source_are_preserved(manual):
    source = manual.replace(b'\n', b'\r\n')
    original = bytes(source)
    result = prepare_profile(source)
    assert b'\n' not in result.replace(b'\r\n', b'')
    assert source == original
    validate_profile(result, source=source)


@pytest.mark.parametrize("before,after,reason", [
    (b'"Manual"', b'"ARGOS RF"', "ordinary manual"),
    (b'type: TYPE_CROSSFIRE', b'type: TYPE_NONE', "Internal RF"),
    (b'channelsCount: 16', b'channelsCount: 8', "Internal RF"),
    (b'srcRaw: "I2"', b'srcRaw: "MAX"', "CH3"),
    (b'srcRaw: "SA"', b'srcRaw: "MAX"', "CH5"),
    (b'srcRaw: "I0"', b'srcRaw: "lua(0,0)"', "CH4"),
    (b'srcRaw: Thr', b'srcRaw: Rud', "Input I2"),
    (b'limitData:', b'limitData:\n   6:\n      revert: 1', "CH7"),
])
def test_refuses_unreviewed_or_bench_source(manual, before, after, reason):
    with pytest.raises(ProfileError, match=reason):
        prepare_profile(manual.replace(before, after))


@pytest.mark.parametrize("extra,reason", [
    (b'logicalSw:\n   0:\n      func: FUNC_AND\n', "logicalSw"),
    (b'scriptsData:\n   0:\n      file: Other\n', "scriptsData"),
    (b'customFn:\n   0:\n      func: OVERRIDE_CHANNEL\n', "customFn"),
])
def test_never_overwrites_existing_logic(manual, extra, reason):
    with pytest.raises(ProfileError, match=reason):
        prepare_profile(manual + extra)


@pytest.mark.parametrize("before,after,reason", [
    (b'"Rud,25"', b'"Rud,10"', "gate"),
    (b'"L9,SC1"', b'"L9,SC0"', "gate"),
    (b'"L6,SC0"', b'"L6,SC2"', "gate"),
    (b'"!L10"', b'"NONE"', "gate"),
    (b'lsPersist: 0', b'lsPersist: 1', "gate"),
    (b'duration: 0', b'duration: 3', "gate"),
    (b'duration: 2', b'duration: 0', "gate"),
    (b'carryTrim: 1', b'carryTrim: 0', "replacement"),
    (b'"L7"', b'"NONE"', "replacement"),
    (b'"REPL"', b'"ADD"', "replacement"),
    (b'weight: -100', b'weight: 100', "CH7"),
    (b'"ArgFly"', b'"ArgVis"', "LUA1"),
])
def test_validator_rejects_unsafe_gate_or_mix_edit(manual, before, after, reason):
    result = prepare_profile(manual)
    assert before in result
    with pytest.raises(ProfileError, match=reason):
        validate_profile(result.replace(before, after, 1))


def test_source_comparison_rejects_pilot_tuning_or_identity_change(manual):
    result = prepare_profile(manual)
    for before, after, reason in [(b'value: 7', b'value: 8', 'flightModeData'),
                                  (b'curve: 12', b'curve: 13', 'limitData'),
                                  (b'val: 17', b'val: 18', 'identity')]:
        with pytest.raises(ProfileError, match=reason):
            validate_profile(result.replace(before, after), source=manual)


def _edit_sticky_state(result, value):
    prefix, sticky = result.split(b'func: "FUNC_STICKY"', 1)
    return prefix + b'func: "FUNC_STICKY"' + sticky.replace(b'lsState: 0', b'lsState: ' + value, 1)


@pytest.mark.parametrize("state", [b'0', b'1'])
def test_readback_accepts_only_binary_transient_l10_state(manual, state):
    result = _edit_sticky_state(prepare_profile(manual), state)
    report = validate_profile(result, source=manual)
    assert report["preservation_checked"] is True
    assert report["hardware_validated"] is False


@pytest.mark.parametrize("state", [b'-1', b'2', b'true', b'01'])
def test_readback_rejects_nonbinary_l10_state(manual, state):
    with pytest.raises(ProfileError, match="L10 lsState"):
        validate_profile(_edit_sticky_state(prepare_profile(manual), state))


def test_transient_state_exception_does_not_relax_gate_policy(manual):
    result = _edit_sticky_state(prepare_profile(manual), b'1')
    prefix, sticky = result.split(b'func: "FUNC_STICKY"', 1)
    persistent = prefix + b'func: "FUNC_STICKY"' + sticky.replace(b'lsPersist: 0', b'lsPersist: 1', 1)
    wrong_reset = result.replace(b'"L9,SC1"', b'"L9,SC0"')
    other_state = result.replace(b'lsState: 0', b'lsState: 1', 1)
    for changed in (persistent, wrong_reset, other_state):
        with pytest.raises(ProfileError, match="gate"):
            validate_profile(changed)


@pytest.mark.parametrize("raw", [
    b'', b'[]', b'header: {}\nheader: {}\n', b'header: &h {}\nother: *h\n',
    b'!!python/object:example {}', b'\xff', b'{broken',
    b'header: x\n', b'header: {}\nmoduleData: {}\n',
])
def test_malformed_yaml_fails_closed(raw):
    with pytest.raises(ProfileError):
        prepare_profile(raw)


def test_build_artifact_is_reproducible_and_refuses_overwrite(manual, tmp_path):
    source = tmp_path / 'manual.yml'
    source.write_bytes(manual)
    script = Path(__file__).parents[1] / 'scripts/edgetx/ArgFly.lua'
    first = build_artifact(source, tmp_path / 'first', script)
    second = build_artifact(source, tmp_path / 'second', script)
    assert first == second
    assert (tmp_path / 'first/model.yml').read_bytes() == prepare_profile(manual)
    assert (tmp_path / 'first/SCRIPTS/MIXES/ArgFly.lua').read_bytes() == script.read_bytes()
    assert source.read_bytes() == manual
    with pytest.raises(FileExistsError):
        build_artifact(source, tmp_path / 'first', script)
