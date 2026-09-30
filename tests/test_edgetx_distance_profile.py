"""Only synthetic model inputs; no personal radio files enter the repository."""
from pathlib import Path
import shutil
import subprocess

import pytest

from argos.backends import edgetx_distance_profile as distance
from argos.backends import edgetx_profile as yaw
from test_edgetx_profile import manual


def test_isolated_model_preserves_original_and_yaw_profile(manual):
    original = bytes(manual)
    result = distance.prepare_profile(manual)
    report = distance.validate_profile(result, source=manual)
    assert manual == original
    assert report['model_name'] == 'ARGOS DST'
    assert report['pitch_channel'] == 2 and report['yaw_channel'] == 4
    assert report['angle_switch'] == 'SB↑'
    assert b'ArgFly' not in result and b'ArgDst' in result
    assert result.count(b'flightModes: 000000000') == 12
    assert report['preservation_checked'] and not report['hardware_validated']
    assert yaw.validate_profile(yaw.prepare_profile(manual))['model_name'] == 'ARGOS FLY'


def test_crlf_preserved_and_twice_identical(manual):
    source = manual.replace(b'\n', b'\r\n')
    result = distance.prepare_profile(source)
    assert b'\n' not in result.replace(b'\r\n', b'')
    assert result == distance.prepare_profile(source)


@pytest.mark.parametrize('before,after', [
    (b'"Ele,25"', b'"Rud,25"'), (b'"Ele,50"', b'"Ele,100"'),
    (b'"L16,SC1"', b'"L16,SC2"'), (b'"L5,!L19"', b'"L5,L5"'),
    (b'"L17,SC2"', b'"L17,SC0"'), (b'"SB0"', b'"SB1"'),
    (b'"lua(0,5),0"', b'"lua(0,1),0"'), (b'"L18"', b'"NONE"'),
    (b'"lua(0,4)"', b'"lua(0,0)"'), (b'weight: -100', b'weight: 100'),
    (b'"ArgDst"', b'"ArgFly"'), (b'lsPersist: 0', b'lsPersist: 1'),
    (b'duration: 2', b'duration: 0'),
])
def test_native_and_channel_contract_refuses_mutations(manual,before,after):
    result = distance.prepare_profile(manual)
    assert before in result
    with pytest.raises(distance.ProfileError):
        distance.validate_profile(result.replace(before,after,1))


def test_original_arming_throttle_roll_pitch_and_yaw_rows_are_preserved(manual):
    result = distance.prepare_profile(manual)
    _, before, _ = yaw._parse(manual)
    _, after, _ = yaw._parse(result)
    rows = after['mixData'][:2] + after['mixData'][3:5] + after['mixData'][6:]
    for channel in (0,1,2,3,4,5,7,8,9):
        assert rows[channel] == before['mixData'][channel]
    assert after['logicalSw']['9']['def'] == 'L16,SC1'
    assert after['logicalSw']['17']['andsw'] == 'SB0'
    assert after['logicalSw']['16']['andsw'] == 'L7'


def test_angle_gate_requires_unmodified_sb_mapping(manual):
    # Scope alteration to SB row only, not another pilot channel.
    before = b'srcRaw: "SB"\n   weight: 100'
    assert before in manual
    with pytest.raises(distance.ProfileError, match='CH6'):
        distance.prepare_profile(manual.replace(before,b'srcRaw: "SB"\n   weight: -100'))
    with pytest.raises(distance.ProfileError, match='CH6'):
        distance.prepare_profile(manual.replace(b'limitData:',b'limitData:\n   5:\n      revert: 1'))


def test_native_stale_output_path_applies_to_pitch(manual):
    _, model, _ = yaw._parse(distance.prepare_profile(manual))
    g = model['logicalSw']
    # L18 depends on Psh AND L7. L7 depends on native heartbeat L5, immediate
    # Rud/Ele blocker L19 and latched takeover L10 independently of Lua.
    assert g['17']['def'] == 'L17,SC2' and g['17']['andsw'] == 'SB0'
    assert g['16']['def'] == 'lua(0,5),0' and g['16']['andsw'] == 'L7'
    assert g['6']['def'] == 'L6,L12' and g['6']['andsw'] == '!L10'
    assert g['5']['def'] == 'L5,!L19'
    assert g['18']['def'] == 'L11,L14'
    assert g['14']['def'] == 'L13,L14' and g['14']['andsw'] == 'SC2'
    assert g['13']['def'] == 'Ele,50' and g['13']['andsw'] == 'SC2'
    assert g['4']['andsw'] == '!L4' and g['3']['def'] == 'L2,L3'
    assert g['1']['delay'] == '3' and g['2']['delay'] == '3'


def test_build_separate_artifact_no_overwrite(manual,tmp_path):
    source=tmp_path/'manual.yml'; source.write_bytes(manual)
    target=tmp_path/'dst'
    script=Path(__file__).parents[1]/'scripts/edgetx/ArgDst.lua'
    manifest=distance.build_artifact(source,target,script)
    assert manifest['script_file']=='SCRIPTS/MIXES/ArgDst.lua'
    assert source.read_bytes()==manual
    with pytest.raises(FileExistsError):
        distance.build_artifact(source,target,script)


def test_actual_distance_lua_cases():
    lua=shutil.which('lua5.4') or shutil.which('lua')
    if lua is None:
        pytest.skip('Lua 5.3+ required')
    root=Path(__file__).parents[1]
    result=subprocess.run([lua,'tests/edgetx_distance_test.lua'],cwd=root,
                          text=True,capture_output=True,timeout=15)
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'passed 13 distance Lua tests' in result.stdout


def test_delayed_pitch_duration_cannot_set_latch_in_manual(manual):
    result = distance.prepare_profile(manual)
    before, after = result.split(b'def: "L13,L14"',1)
    assert after.startswith(b'\n      andsw: "SC2"')
    with pytest.raises(distance.ProfileError, match='gate'):
        distance.validate_profile(before + b'def: "L13,L14"' + after.replace(
            b'andsw: "SC2"', b'andsw: "NONE"',1))
