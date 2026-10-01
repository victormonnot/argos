"""Optional AP1 observations from Pocket, never command/authority input.

The packed flags are state, cause, selected mode, yaw phase, pitch phase,
yaw Lua validity and pitch Lua validity. Values are pre-mix calibrated stick
sources and the script's current outputs, not native mixer or FC readback.
"""
from copy import deepcopy
import re

from .edgetx_probe import ProbeError


MAX_LINE = 96
_UINT = rb"(0|[1-9][0-9]{0,9})"
_INT = rb"(0|-?[1-9][0-9]{0,3})"
_SAMPLE = re.compile(rb"AP1 ([0-9a-f]{8}) " + b" ".join([_UINT] * 3)
                     + rb" ([MTAF])([SMWATEPLIGCO])([NYD])([NAMR])([NAMR])([01])([01]) "
                     + b" ".join([_INT] * 6))


class PilotLines:
    """Preserve yaw's 64-byte lines; only AP1 diagnostics get 96 bytes."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        lines = []
        for value in data:
            if value == 10:
                lines.append(bytes(self.buffer).removesuffix(b"\r"))
                self.buffer.clear()
            else:
                self.buffer.append(value)
                limit = MAX_LINE if self.buffer.startswith(b"AP1 ") else 64
                if len(self.buffer) > limit:
                    raise ProbeError("oversized serial line; check the selected port and USB-VCP mode")
        return lines


def parse_pilot_sample(line):
    match = _SAMPLE.fullmatch(line)
    if match is None or len(line) > MAX_LINE:
        raise ProbeError("invalid AP1 pilot observation")
    (session, generation, ticket, ack, state, cause, mode, yaw_phase, pitch_phase,
     yaw_valid, pitch_valid, roll, pitch, throttle, yaw, lua_yaw, lua_pitch) = match.groups()
    generation, ticket, ack = map(int, (generation, ticket, ack))
    sticks = dict(zip(("roll", "pitch", "throttle", "yaw"), map(int, (roll, pitch, throttle, yaw))))
    lua_yaw, lua_pitch = int(lua_yaw), int(lua_pitch)
    yaw_valid, pitch_valid = yaw_valid == b"1", pitch_valid == b"1"
    if (max(generation, ticket, ack) > 2**31 - 1
            or any(abs(value) > 1024 for value in sticks.values())
            or abs(lua_yaw) > 205 or abs(lua_pitch) > 51):
        raise ProbeError("out-of-range AP1 pilot observation")
    if (cause not in {b"M": (b"S", b"M"), b"T": (b"W", b"T", b"E"),
                     b"A": (b"A",), b"F": (b"P", b"L", b"I", b"G", b"C", b"O")}[state]
            or ((state in (b"M", b"F")) != (mode == b"N"))
            or ((mode == b"N") != (yaw_phase == b"N"))
            or ((mode == b"D") == (pitch_phase == b"N"))
            or (yaw_valid and (state != b"A" or yaw_phase != b"A"))
            or (pitch_valid and (state != b"A" or pitch_phase != b"A" or mode != b"D"))
            or (not yaw_valid and lua_yaw != 0)
            or (not pitch_valid and lua_pitch != 0)):
        raise ProbeError("inconsistent AP1 pilot observation")
    return dict(schema_version=1, session=session.decode(), generation=generation,
                ticket=ticket, ack=ack, state=state.decode(), cause=cause.decode(),
                mode=mode.decode(), yaw_phase=yaw_phase.decode(), pitch_phase=pitch_phase.decode(),
                sticks=sticks, lua_outputs={"yaw": dict(valid=yaw_valid, value=lua_yaw),
                                          "pitch": dict(valid=pitch_valid, value=lua_pitch)})


class PilotObserver:
    """One correlated sample; repeats/old sessions cannot renew its receipt."""

    def __init__(self):
        self.sample = None
        self.count = 0

    def clear(self):
        self.sample = None

    def observe(self, line, *, status, session, pending_begin, received_at):
        sample = parse_pilot_sample(line)
        if status is None or pending_begin or sample["session"] != session:
            return
        expected = {key: getattr(status, key) for key in
                    ("session", "generation", "ticket", "ack", "state", "cause")}
        mode = getattr(status, "mode", "Y" if status.state in ("T", "A") else "N")
        expected.update(mode=mode,
                        yaw_phase=getattr(status, "yaw_phase", "A" if mode == "Y" else "N"),
                        pitch_phase=getattr(status, "pitch_phase", "N"))
        if any(sample[key] != value for key, value in expected.items()):
            return
        identity = (sample["session"], sample["generation"], sample["ticket"])
        if self.sample is not None and identity == tuple(self.sample[key] for key in
                                                         ("session", "generation", "ticket")):
            return
        self.sample = {**sample, "received_at": received_at}
        self.count += 1

    def snapshot(self):
        return dict(pilot_sample=deepcopy(self.sample), pilot_sample_count=self.count)
