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
    (b'"L23,SC2"', b'"L23,SC0"'), (b'"SB0"', b'"SB1"'),
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
    # L18 depends on soft release L23 and Psh/L7. L7 depends on heartbeat L5, immediate
    # Rud/Ele blocker L19 and latched takeover L10 independently of Lua.
    assert g['17']['def'] == 'L23,SC2' and g['17']['andsw'] == 'SB0'
    assert g['22']['def'] == 'L17,!L22' and g['22']['andsw'] == '!L20'
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
    assert 'distance Lua tests' in result.stdout


def test_delayed_pitch_duration_cannot_set_latch_in_manual(manual):
    result = distance.prepare_profile(manual)
    before, after = result.split(b'def: "L13,L14"',1)
    assert after.startswith(b'\n      andsw: "SC2"')
    with pytest.raises(distance.ProfileError, match='gate'):
        distance.validate_profile(before + b'def: "L13,L14"' + after.replace(
            b'andsw: "SC2"', b'andsw: "NONE"',1))



def test_soft_pitch_release_keeps_native_yaw_and_strong_latch_unchanged(manual):
    _, model, _ = yaw._parse(distance.prepare_profile(manual))
    g = model['logicalSw']
    # No Ail input or L20-L23 dependency can withdraw yaw through L07.
    assert g['6']['def'] == 'L6,L12' and g['6']['andsw'] == '!L10'
    assert g['9']['def'] == 'L16,SC1'
    assert g['7']['def'] == 'Rud,25' and g['7']['delay'] == '2'
    assert g['10']['def'] == 'Rud,50' and g['10']['delay'] == '0'
    assert g['12']['def'] == 'Ele,25' and g['12']['delay'] == '2'
    assert g['13']['def'] == 'Ele,50' and g['13']['delay'] == '0'
    assert all('Ail' not in row['def'] for row in g.values())
    assert g['19'] == dict(func='FUNC_APOS', **{'def': 'Ele,10'}, andsw='SC2',
                           lsPersist='0', lsState='0', delay='0', duration='2')
    assert g['20']['def'] == 'lua(0,5),0' and g['20']['andsw'] == 'NONE'
    assert g['21']['def'] == 'L20,L21' and g['21']['lsPersist'] == '0'
    assert g['22']['def'] == 'L17,!L22' and g['22']['andsw'] == '!L20'


@pytest.mark.parametrize('before,after', [
    (b'"Ele,10"', b'"Ele,25"'), (b'"L20,L21"', b'"L20,!L21"'),
    (b'"L17,!L22"', b'"L17,L17"'), (b'"!L20"', b'"NONE"'),
])
def test_soft_pitch_release_gate_refuses_weakening(manual,before,after):
    result = distance.prepare_profile(manual)
    assert before in result
    with pytest.raises(distance.ProfileError, match='gate'):
        distance.validate_profile(result.replace(before,after,1))


def _soft_sticky_state(raw, value):
    prefix, sticky = raw.split(b'def: "L20,L21"',1)
    return prefix + b'def: "L20,L21"' + sticky.replace(b'lsState: 0', b'lsState: '+value,1)


@pytest.mark.parametrize('value', [b'0',b'1'])
def test_readback_accepts_soft_latch_transient_state_only(manual,value):
    raw = _soft_sticky_state(distance.prepare_profile(manual),value)
    assert distance.validate_profile(raw,source=manual)['native_gate'] == 'L01–L23'
    prefix, sticky = raw.split(b'def: "L20,L21"',1)
    with pytest.raises(distance.ProfileError, match='gate'):
        distance.validate_profile(prefix+b'def: "L20,L21"'+sticky.replace(
            b'lsPersist: 0', b'lsPersist: 1',1))


@pytest.mark.parametrize('value', [b'-1',b'2',b'01',b'true'])
def test_readback_rejects_invalid_soft_latch_state(manual,value):
    with pytest.raises(distance.ProfileError, match='L22 state'):
        distance.validate_profile(_soft_sticky_state(distance.prepare_profile(manual),value))


class _StickyEdges:
    """Contract fixture for one EdgeTX Sticky, not an EdgeTX mixer simulation.

    The pinned 2.12.4 switches.cpp logicalSwitchesTimerTick selects only the
    set input while clear or reset input while latched, sharing one edge bit.
    This deliberately models that easily missed edge convention only. Delays,
    source lookup, scheduler and hardware outputs are NOT simulated here.
    """
    def __init__(self):
        self.state = False
        self.last = False

    def tick(self, set_input, reset_input):
        now = reset_input if self.state else set_input
        if now != self.last:
            if not self.last:
                self.state = not self.state
            self.last = now
        return self.state


def test_frozen_psh_high_cannot_reopen_pitch_after_soft_recenter():
    latch = _StickyEdges()
    assert not latch.tick(False,True)
    assert latch.tick(True,True)
    # Psh remains frozen high; recentering Ele changes only the set input.
    assert all(latch.tick(False,True) for _ in range(8))
    # A real low output must be observed before fresh-high can release.
    assert latch.tick(False,False)
    assert not latch.tick(False,True)


def test_simultaneous_soft_release_and_psh_low_still_recovers_after_fresh_high():
    latch = _StickyEdges()
    assert latch.tick(True,False)
    assert latch.tick(True,False)
    assert latch.tick(False,False)
    assert not latch.tick(False,True)
    # Negative control: inverted reset fails with the simultaneous set/low.
    wrong = _StickyEdges()
    assert wrong.tick(True,True)
    assert wrong.tick(False,True)
    assert wrong.tick(False,False)  # high Psh still leaves the wrong latch set.


def test_soft_event_stretch_and_reset_contract_requires_a_new_psh_edge(manual):
    _, model, _ = yaw._parse(distance.prepare_profile(manual))
    gates = model['logicalSw']
    assert gates['19']['delay'] == '0' and gates['19']['duration'] == '2'
    assert gates['21']['def'] == 'L20,L21'
    assert gates['20']['andsw'] == 'NONE'  # raw Psh; no circular L18 dependency.
    prefix, tail = distance.prepare_profile(manual).split(b'def: "Ele,10"',1)
    with pytest.raises(distance.ProfileError, match='gate'):
        distance.validate_profile(prefix+b'def: "Ele,10"'+tail.replace(
            b'duration: 2', b'duration: 0',1))
