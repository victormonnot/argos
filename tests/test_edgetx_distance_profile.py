"""Synthetic profile contracts; no personal radio files or hardware simulation."""
from pathlib import Path
import re
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
    assert report['schema'] == 'argos-edgetx-distance-profile-v3'
    assert report['native_gate'] == 'L01–L18'
    assert report['yaw_manual_release_percent'] == 20
    assert report['pitch_manual_release_percent'] == 10
    assert report['yaw_soft_return_ms'] == report['pitch_soft_return_ms'] == 200
    assert report['yaw_return_center_percent'] == 10
    assert report['pitch_return_center_percent'] == 5
    assert 'manual_return_center_percent' not in report
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
    (b'"Ele,10"', b'"Rud,10"'), (b'"Rud,20"', b'"Rud,25"'),
    (b'"Rud,20"', b'"Rud,10"'), (b'"Ele,10"', b'"Ele,20"'),
    (b'"L8,L9"', b'"L8,SC1"'), (b'"L13,L14"', b'"L13,SC1"'),
    (b'"L17,SC2"', b'"L17,SC0"'), (b'"SB0"', b'"SB1"'),
    (b'"lua(0,2),0"', b'"lua(0,1),0"'), (b'"L18"', b'"NONE"'),
    (b'"lua(0,4)"', b'"lua(0,0)"'), (b'weight: -100', b'weight: 100'),
    (b'"ArgDst"', b'"ArgFly"'), (b'lsPersist: 0', b'lsPersist: 1'),
    (b'duration: 2', b'duration: 0'),
    (b'"L14,L5"', b'"L14,L7"'), (b'"L5,L9"', b'"L5,L14"'),
    (b'"L8,L9"', b'"L8,!L9"'), (b'"L13,L14"', b'"L13,!L14"'),
    (b'"!L10"', b'"NONE"'), (b'"!L13"', b'"NONE"'),
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
    assert after['logicalSw']['9']['def'] == 'L8,L9'
    assert after['logicalSw']['14']['def'] == 'L13,L14'
    assert after['logicalSw']['17']['andsw'] == 'SB0'


def test_angle_gate_requires_unmodified_sb_mapping(manual):
    before = b'srcRaw: "SB"\n   weight: 100'
    assert before in manual
    with pytest.raises(distance.ProfileError, match='CH6'):
        distance.prepare_profile(manual.replace(before,b'srcRaw: "SB"\n   weight: -100'))
    with pytest.raises(distance.ProfileError, match='CH6'):
        distance.prepare_profile(manual.replace(b'limitData:',b'limitData:\n   5:\n      revert: 1'))


def _dependency_contract(gates, switch):
    """Traverse declared dependencies only; no timer or mixer execution model."""
    seen = set()
    def visit(index):
        if index in seen:
            return
        seen.add(index)
        row = gates[str(index-1)]
        for field in ('def','andsw'):
            for match in re.finditer(r'(?<![A-Za-z0-9_])!?L(\d+)\b', row[field]):
                visit(int(match.group(1)))
    visit(switch)
    text = ' '.join(gates[str(index-1)]['def']+' '+gates[str(index-1)]['andsw']
                    for index in sorted(seen))
    return seen, text


def test_each_native_axis_depends_on_its_own_stick_and_validity(manual):
    _, model, _ = yaw._parse(distance.prepare_profile(manual))
    g = model['logicalSw']
    yaw_path, yaw_text = _dependency_contract(g,7)
    pitch_path, pitch_text = _dependency_contract(g,18)
    assert 'Rud,20' in yaw_text and 'lua(0,1),0' in yaw_text
    assert 'Ele' not in yaw_text and 'lua(0,5)' not in yaw_text and 15 not in yaw_path
    assert 'Ele,10' in pitch_text and 'lua(0,5),0' in pitch_text
    assert 'Rud' not in pitch_text and 'lua(0,1)' not in pitch_text and 10 not in pitch_path
    assert 'Ail' not in yaw_text + pitch_text
    # Both depend on receipt and live heartbeat; neither requires other-axis freshness.
    assert yaw_path & pitch_path == {1,2,3,4,5}
    assert g['4']['def'] == 'lua(0,2),0' and g['4']['andsw'] == '!L4'
    assert g['1']['delay'] == g['2']['delay'] == '3'
    assert g['1']['duration'] == g['2']['duration'] == '0'


def test_full_throw_has_no_amplitude_duration_or_sc_only_native_latch(manual):
    _, model, _ = yaw._parse(distance.prepare_profile(manual))
    g = model['logicalSw']
    stick_rows = [row for row in g.values() if row['func']=='FUNC_APOS']
    assert {row['def'] for row in stick_rows} == {'Rud,20','Ele,10'}
    assert all(row['delay']=='0' and row['duration']=='2' for row in stick_rows)
    sticky_rows = [row for row in g.values() if row['func']=='FUNC_STICKY']
    assert {row['def'] for row in sticky_rows} == {'L8,L9','L13,L14'}
    assert all(row['andsw']=='NONE' and row['lsPersist']=='0' for row in sticky_rows)
    assert all('SC1' not in row['def'] for row in sticky_rows)


@pytest.mark.parametrize('axis,raw,release', [
    ('Rud', 154, False), ('Rud', -154, False),  # About 15%: Mode 2 margin.
    ('Rud', 205, False), ('Rud', -205, False),
    ('Rud', 206, True), ('Rud', -206, True),
    ('Ele', 102, False), ('Ele', -102, False),
    ('Ele', 103, True), ('Ele', -103, True),
    ('Rud', 1024, True), ('Ele', -1024, True),
])
def test_native_stick_thresholds_preserve_pitch_and_widen_only_yaw(
        manual, axis, raw, release):
    # Only the firmware's source comparison is represented here, not scheduling
    # or the full mixer. EdgeTX 2.12.4 edgetx.h calc100toRESX rounds to nearest;
    # switches.cpp LS_FUNC_APOS uses a strict abs(x)>y comparison. In particular,
    # 20% maps to 205, not truncation to 204; Lua must use that same boundary.
    # https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/edgetx.h
    # https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/switches.cpp
    _, model, _ = yaw._parse(distance.prepare_profile(manual))
    row = next(row for row in model['logicalSw'].values()
               if row['func'] == 'FUNC_APOS' and row['def'].startswith(axis + ','))
    percent = int(row['def'].split(',')[1])
    threshold = (percent * 1024 + 50) // 100
    assert threshold == {'Rud': 205, 'Ele': 102}[axis]
    assert (abs(raw) > threshold) is release
    # Native immediate release and pulse stretching remain independent of Lua.
    assert row['delay'] == '0' and row['duration'] == '2'
    assert row['andsw'] == {'Rud': 'L12', 'Ele': 'SC2'}[axis]


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


def _sticky_state(raw, definition, value):
    marker = b'def: "'+definition+b'"'
    prefix, sticky = raw.split(marker,1)
    return prefix + marker + sticky.replace(b'lsState: 0', b'lsState: '+value,1)


@pytest.mark.parametrize('definition', [b'L8,L9',b'L13,L14'])
@pytest.mark.parametrize('value', [b'0',b'1'])
def test_readback_accepts_only_binary_transient_states(manual,definition,value):
    raw = _sticky_state(distance.prepare_profile(manual),definition,value)
    assert distance.validate_profile(raw,source=manual)['native_gate'] == 'L01–L18'
    marker = b'def: "'+definition+b'"'
    prefix, sticky = raw.split(marker,1)
    with pytest.raises(distance.ProfileError, match='gate'):
        distance.validate_profile(prefix+marker+sticky.replace(b'lsPersist: 0',b'lsPersist: 1',1))


@pytest.mark.parametrize('definition,label', [(b'L8,L9','L10'),(b'L13,L14','L15')])
@pytest.mark.parametrize('value', [b'-1',b'2',b'01',b'true'])
def test_readback_rejects_invalid_axis_latch_states(manual,definition,label,value):
    with pytest.raises(distance.ProfileError, match=label+' state'):
        distance.validate_profile(_sticky_state(distance.prepare_profile(manual),definition,value))


class _StickyEdges:
    """One Sticky edge contract, NOT an EdgeTX mixer/scheduling simulation.

    Pinned 2.12.4 switches.cpp logicalSwitchesTimerTick selects the set input
    while clear or reset input while latched, sharing one edge-history bit.
    Timers, source lookup, waveform sampling and hardware are not modeled.
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


@pytest.mark.parametrize('axis,gate,latch_index,definition,reset_index', [
    ('yaw',7,10,'L8,L9',9), ('pitch',18,15,'L13,L14',14),
])
def test_frozen_axis_validity_cannot_reopen_after_recenter(
        manual,axis,gate,latch_index,definition,reset_index):
    _, model, _ = yaw._parse(distance.prepare_profile(manual))
    rows = model['logicalSw']
    assert rows[str(latch_index-1)]['def'] == definition
    assert rows[str(reset_index-1)]['andsw'] == 'NONE'
    dependencies, _ = _dependency_contract(rows,gate)
    assert latch_index in dependencies
    latch = _StickyEdges()
    assert not latch.tick(False,True)
    assert latch.tick(True,True)
    assert all(latch.tick(False,True) for _ in range(8))
    assert latch.tick(False,False)
    assert not latch.tick(False,True)


@pytest.mark.parametrize('held_ticks', [1,2,20,100])
def test_large_or_long_gesture_still_resets_on_fresh_axis_validity(held_ticks):
    # Once either axis exceeds its own threshold, holding that predicate never
    # changes the reset policy; amplitude adds no further latch.
    latch = _StickyEdges()
    assert latch.tick(True,False)
    for _ in range(held_ticks):
        assert latch.tick(True,False)
    assert latch.tick(False,False)
    assert not latch.tick(False,True)


def test_axes_keep_separate_sticky_edge_history():
    yaw_latch, pitch_latch = _StickyEdges(), _StickyEdges()
    assert yaw_latch.tick(True,False)
    assert not pitch_latch.tick(False,True)
    assert yaw_latch.tick(False,False)
    assert not yaw_latch.tick(False,True)
    assert not pitch_latch.tick(False,True)
    assert pitch_latch.tick(True,False)
    assert not yaw_latch.tick(False,True)


def test_simultaneous_release_and_validity_low_needs_noninverted_reset():
    latch = _StickyEdges()
    assert latch.tick(True,False)
    assert latch.tick(True,False)
    assert latch.tick(False,False)
    assert not latch.tick(False,True)
    wrong = _StickyEdges()
    assert wrong.tick(True,True)
    assert wrong.tick(False,True)
    assert wrong.tick(False,False)
