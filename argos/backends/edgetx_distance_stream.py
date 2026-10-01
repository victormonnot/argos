"""Isolated yaw plus bounded apparent-size pitch transport for ArgDst.

The established yaw demo transport is unchanged. This fork retains its ticket,
clock, freshness, reconnect and spacing checks, with an independent wire greeting
and explicit radio mode. A separate model and native pitch gate are required.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import re
import secrets
import time

from .edgetx_probe import ProbeError, open_port
from .edgetx_pilot_sample import PilotObserver
from .edgetx_yaw_stream import SourceSample, SelectionResult
from .yaw_stream_source import MAX_VALUE


HELLO = b"ARGOS_DISTANCE_STREAM_V3"
INTERVAL = .1
MIN_COMMAND_INTERVAL = .05
WRITE_TIMEOUT = .02
HEALTH_TIMEOUT = 1.
READ_BUDGET = 4
TIMING_PENDING_LIMIT = 32
MAX_SEQUENCE = 2**31 - 1
MAX_PITCH = 51
MAX_LINE = 96
# EdgeTX Lua 5.3 LUA_32BITS keeps nonnegative integer counters exact to 31 bits.
COUNTER_MODULUS = 2**31
_STATUS = re.compile(rb"DY3 ([0-9a-f]{8}) ([0-9]{1,10}) ([0-9]{1,10}) ([0-9]{1,10}) ([MTAF]) ([SMWATEPLIGCO]) ([NYD]) ([NAMR]) ([NAMR])")
_BLOCKED = re.compile(rb"ARGOS_DISTANCE_BLOCKED (model|internal_rf|external_rf|crash_flip|selector|stick|logical_switch|api_exception|serial_api|clock|mode|pitch) ([A-Za-z0-9_.:+-]{1,24})")


class Lines:
    """Bound the distinct protocol without changing the established yaw reader."""

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
                if len(self.buffer) > MAX_LINE:
                    raise ProbeError("oversized distance-stream serial line")
        return lines


@dataclass(frozen=True)
class RadioStatus:
    session: str
    generation: int
    ticket: int
    ack: int
    state: str
    cause: str
    mode: str
    yaw_phase: str
    pitch_phase: str


def parse_status(line: bytes) -> RadioStatus:
    match = _STATUS.fullmatch(line)
    if match is None:
        raise ProbeError("invalid distance-stream radio status")
    session, generation, ticket, ack, state, cause, mode, yaw_phase, pitch_phase = match.groups()
    generation, ticket, ack = int(generation), int(ticket), int(ack)
    if generation >= COUNTER_MODULUS or ticket >= COUNTER_MODULUS or ack > MAX_SEQUENCE:
        raise ProbeError("out-of-range distance-stream radio status")
    if cause not in {b"M": (b"S", b"M"), b"T": (b"W", b"T", b"E"),
                    b"A": (b"A",), b"F": (b"P", b"L", b"I", b"G", b"C", b"O")}[state]:
        raise ProbeError("inconsistent distance-stream radio state and cause")
    if (state in (b"M", b"F")) != (mode == b"N"):
        raise ProbeError("inconsistent distance-stream radio state and mode")
    if (mode == b"N") != (yaw_phase == b"N"):
        raise ProbeError("inconsistent distance-stream yaw phase and mode")
    if (mode == b"D") == (pitch_phase == b"N"):
        raise ProbeError("inconsistent distance-stream pitch phase and mode")
    return RadioStatus(session.decode(), generation, ticket, ack, state.decode(),
                       cause.decode(), mode.decode(), yaw_phase.decode(), pitch_phase.decode())


def _advances(value, previous):
    """Nonnegative int31 counters wrap; duplicate/old reports confer no freshness."""
    return 0 < (value - previous) % COUNTER_MODULUS < COUNTER_MODULUS // 2


class DistanceStream:
    """One USB connection; fresh images may advance the 10 Hz refresh sender.

    Commands remain at least 50 ms apart. A new image or withdrawn target may
    send before the next refresh, without waiting for a SET acknowledgement.

    Tickets are radio-issued capabilities with a radio-side 300 ms lifetime.
    Late queued commands cannot acquire a new lifetime on receipt. This bound
    is not an end-to-end guarantee that output expires at an image's deadline:
    local image deadlines forbid new transmissions, and the existing lease
    expires separately on the radio's callback clock.
    """

    def __init__(self, port, *, clock=time.monotonic, nonce=lambda: secrets.token_hex(4),
                 request_selection=None):
        self.port = port
        self.clock = clock
        self.nonce = nonce
        self.request_selection = request_selection
        self.lines = Lines()
        self.pilot = PilotObserver()
        self.port.timeout = 0
        self.port.write_timeout = WRITE_TIMEOUT
        self.started_at = self.last_time = clock()
        self.greeted = False
        self.session = None
        self.status = None
        self.last_status_at = None
        self.last_ack_at = None
        self.pending_since = None
        self.sent_sequence = 0
        self.next_send_at = self.started_at
        self._last_command_at = None
        self._last_frame_key = None
        self.session_started_at = self.started_at
        self.selection_key = None
        self.source_bad_since = None
        self.source_fault = False
        self.pending_begin = False
        self.failed = False
        self.reason = "waiting for ArgDst greeting"
        self._backpressure_since = None
        self._reset_without_session = False
        self.selection_request = None
        self.selection_requested_at = None
        self.selection_pending = False
        self.selection_failed = False
        self.last_sent_valid = None
        self.last_sent_command = None
        self.last_sent_pitch = 0
        self.last_sent_pitch_valid = False
        self.a_to_t_total = 0
        self.a_to_t_causes = {}
        self._timing_pending = {}
        self.last_command_timing = None
        self.command_timing_samples = 0

    def snapshot(self):
        """Bounded status for the local UI/logger; accepted state is radio-reported."""
        status = self.status
        return dict(**self.pilot.snapshot(), connected=not self.failed and self.greeted, reason=self.reason,
                    session=self.session, radio_state=status.state if status else None,
                    radio_cause=status.cause if status else None,
                    radio_mode=status.mode if status else None,
                    radio_yaw_phase=status.yaw_phase if status else None,
                    radio_pitch_phase=status.pitch_phase if status else None,
                    last_sent_pitch=self.last_sent_pitch,
                    last_sent_pitch_valid=self.last_sent_pitch_valid,
                    generation=status.generation if status else None,
                    ticket=status.ticket if status else None,
                    ack=status.ack if status else None, sent_sequence=self.sent_sequence,
                    last_sent_valid=self.last_sent_valid, selection_pending=self.selection_pending,
                    last_sent_command=deepcopy(self.last_sent_command),
                    selection_request=self.selection_request, selection_failed=self.selection_failed,
                    radio_a_to_t_total=self.a_to_t_total,
                    radio_a_to_t_causes=dict(self.a_to_t_causes),
                    command_timing_samples=self.command_timing_samples,
                    last_command_timing=(dict(self.last_command_timing)
                                         if self.last_command_timing is not None else None))

    def _clear_timing(self):
        self._timing_pending.clear()
        self.last_command_timing = None

    def _prune_timing(self, now):
        self._timing_pending = {
            seq: item for seq, item in self._timing_pending.items()
            if now - item["write_finished_at"] < HEALTH_TIMEOUT}

    def _remember_timing(self, seq, demand, started, finished):
        """Optional diagnostics never grant or withdraw command authority."""
        frame = getattr(demand, "frame_sequence", None)
        earliest = getattr(demand, "image_received_earliest", None)
        latest = getattr(demand, "image_received_latest", None)
        if type(earliest) not in (int, float) or type(latest) not in (int, float):
            return
        try:
            earliest, latest = float(earliest), float(latest)
        except (ValueError, OverflowError):
            return
        if (type(frame) is not int or frame < 0
                or not math.isfinite(earliest) or not math.isfinite(latest)
                or not 0 <= earliest <= latest <= started <= finished):
            return
        self._prune_timing(finished)
        if len(self._timing_pending) >= TIMING_PENDING_LIMIT:
            del self._timing_pending[next(iter(self._timing_pending))]
        self._timing_pending[seq] = dict(
            frame_sequence=frame, image_received_earliest=earliest,
            image_received_latest=latest, write_started_at=started,
            write_finished_at=finished)

    def _observe_timing(self, status, previous, now):
        if previous is None or status.generation != previous.generation:
            self._clear_timing()
            return
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            return
        self._prune_timing(now)
        if status.ack <= previous.ack:
            return
        # ACK is cumulative: only this exact last-accepted sequence is known
        # to have reached Lua. Skipped intermediate commands yield no samples.
        item = self._timing_pending.get(status.ack)
        self._timing_pending = {seq: value for seq, value in self._timing_pending.items()
                                if seq > status.ack}
        if item is None:
            return
        earliest, latest = item["image_received_earliest"], item["image_received_latest"]
        started, finished = item["write_started_at"], item["write_finished_at"]
        if now < finished:
            return
        timing = dict(
            session=self.session, generation=status.generation,
            command_sequence=status.ack, frame_sequence=item["frame_sequence"],
            valid=True, ack_observed_at=now,
            image_to_set_min_ms=(started - latest) * 1000,
            image_to_set_max_ms=(finished - earliest) * 1000,
            image_to_ack_min_ms=(now - latest) * 1000,
            image_to_ack_max_ms=(now - earliest) * 1000,
            set_to_ack_min_ms=(now - finished) * 1000,
            set_to_ack_max_ms=(now - started) * 1000)
        if any(not math.isfinite(value) or value < 0
               for name, value in timing.items() if name.endswith("_ms")):
            return
        self.command_timing_samples += 1
        self.last_command_timing = timing

    def _cancel_selection(self):
        self.selection_pending = self.selection_failed = False
        self.selection_request = self.selection_requested_at = None

    def _now(self):
        now = self.clock()
        if not math.isfinite(now) or now < self.last_time:
            raise ProbeError("invalid or regressing monotonic clock")
        self.last_time = now
        return now

    def _rotate(self, now, reason):
        previous_session = self.session
        self.session = self.nonce()
        if (not isinstance(self.session, str)
                or not re.fullmatch(r"[0-9a-f]{8}", self.session)
                or self.session in ("00000000", previous_session)):
            raise ProbeError("invalid session nonce")
        self.status = None
        self.pilot.clear()
        self.last_status_at = None
        self.last_ack_at = None
        self.pending_since = None
        self.sent_sequence = 0
        self.session_started_at = now
        self.next_send_at = now
        self._last_frame_key = None
        self.pending_begin = True
        self._cancel_selection()
        self.last_sent_valid = None
        self.last_sent_command = None
        self.last_sent_pitch = 0
        self.last_sent_pitch_valid = False
        self._clear_timing()
        self.reason = reason + "; SC middle then the desired assist position required"

    def _receive(self, now):
        for _ in range(READ_BUDGET):
            chunk = self.port.read(256)
            if not chunk:
                break
            for line in self.lines.feed(chunk):
                if not line:
                    continue
                if line.startswith(b"ARGOS_DISTANCE_BLOCKED"):
                    diagnostic = _BLOCKED.fullmatch(line)
                    if diagnostic is None:
                        raise ProbeError("invalid ArgDst blocked diagnostic")
                    reason, observed = (part.decode("ascii") for part in diagnostic.groups())
                    self.reason = f"ArgDst blocked: {reason} (observed {observed}); radio remains manual"
                    raise ProbeError(self.reason)
                if line == HELLO:
                    if not self.greeted:
                        self.greeted = True
                        self._rotate(now, "radio connected")
                    continue
                # An unrelated serial device gets no writes before our greeting.
                if not self.greeted:
                    continue
                if line.startswith(b"AP1"):
                    self.pilot.observe(line, status=self.status, session=self.session,
                                       pending_begin=self.pending_begin, received_at=self.clock())
                    continue
                status = parse_status(line)
                if status.session == "00000000":
                    if not self._reset_without_session:
                        self._rotate(now, "radio session reset")
                        self._reset_without_session = True
                    continue
                if status.session != self.session or self.pending_begin:
                    continue
                previous = self.status
                if previous is not None:
                    if status.generation == previous.generation:
                        if status.mode != previous.mode:
                            raise ProbeError("radio mode changed without a new generation")
                        if not _advances(status.ticket, previous.ticket):
                            continue
                        if status.ack < previous.ack:
                            raise ProbeError("radio acknowledgement regressed")
                    elif not _advances(status.generation, previous.generation):
                        continue
                if status.ack > self.sent_sequence:
                    raise ProbeError("radio acknowledged a command never sent")
                # Timestamp the received report, not the earlier loop entry.
                # This diagnostic sample does not change authority timestamps.
                self._observe_timing(status, previous, self.clock())
                self._reset_without_session = False
                self.last_status_at = now
                if previous is None or status.generation != previous.generation:
                    self.pending_since = None
                    self.last_ack_at = now
                elif status.ack > previous.ack:
                    self.last_ack_at = now
                    self.pending_since = None if status.ack == self.sent_sequence else now
                if status.state not in ("T", "A"):
                    self.pending_since = None
                    self._cancel_selection()
                if previous is not None and previous.state == "A" and status.state == "T":
                    self.a_to_t_total += 1
                    self.a_to_t_causes[status.cause] = self.a_to_t_causes.get(status.cause, 0) + 1
                if (self.request_selection is not None and status.state == "T" and status.cause == "W"
                        and (previous is None or status.generation != previous.generation)):
                    self.selection_request = f"{self.session}:{status.generation}"
                    self.selection_requested_at = now
                    self.selection_pending, self.selection_failed = True, False
                    self.request_selection(self.selection_request, status.mode)
                self.status = status
        # Drain bounded chunks over later ticks; never send against an old
        # status while a backlog remains to be read.
        return getattr(self.port, "in_waiting", 0) == 0

    def _write(self, packet, now):
        if getattr(self.port, "out_waiting", 0):
            if self._backpressure_since is None:
                self._backpressure_since = now
            if now - self._backpressure_since >= HEALTH_TIMEOUT:
                raise ProbeError("serial output stalled; reconnect requires a new SC cycle")
            return False
        self._backpressure_since = None
        started = self._now()
        written = self.port.write(packet)
        if written != len(packet) or self._now() - started > WRITE_TIMEOUT:
            raise ProbeError("serial write was partial or exceeded its bounded timeout")
        return True

    def _source(self, sample, now):
        if (not isinstance(sample, SourceSample)
                or not math.isfinite(sample.completed_at) or sample.completed_at > now):
            raise ProbeError("invalid source mailbox timestamp")
        result = None
        if self.selection_pending and sample.selection_result is not None:
            candidate = sample.selection_result
            if not isinstance(candidate, SelectionResult) or type(candidate.success) is not bool:
                raise ProbeError("invalid radio selection transaction result")
            if candidate.request_token == self.selection_request:
                if (not math.isfinite(candidate.completed_at)
                        or not self.selection_requested_at <= candidate.completed_at <= sample.completed_at
                        or (candidate.selection_key is not None and not isinstance(candidate.selection_key, tuple))):
                    raise ProbeError("invalid radio selection transaction result")
                result = candidate
                if not result.success or result.selection_key is None:
                    # A failed POST is final for this SC cycle, even when GET
                    # validation is also failing. Success still requires the
                    # fresh, validated demand below before it can bind a key.
                    self.selection_pending = False
                    self.selection_failed = True
        bad = sample.error or sample.demand is None or now - sample.completed_at >= HEALTH_TIMEOUT
        if bad:
            if self.source_bad_since is None:
                self.source_bad_since = min(now, sample.completed_at)
            if now - self.source_bad_since >= HEALTH_TIMEOUT and not self.source_fault:
                self.source_fault = True
                if self.greeted:
                    self._rotate(now, "source unavailable")
            return None
        self.source_bad_since = None
        self.source_fault = False
        demand = sample.demand
        if (type(demand.value) is not int or not -MAX_VALUE <= demand.value <= MAX_VALUE
                or type(demand.valid) is not bool or not math.isfinite(demand.deadline)
                or (demand.selection_key is not None and not isinstance(demand.selection_key, tuple))
                or type(getattr(demand, "pitch", None)) is not int
                or not -MAX_PITCH <= demand.pitch <= MAX_PITCH
                or type(getattr(demand, "pitch_valid", None)) is not bool
                or getattr(demand, "mode", None) not in ("N", "Y", "D")):
            raise ProbeError("invalid source demand")
        if self.selection_pending:
            if result is None:
                return None  # An in-flight pre-enable HTTP read confers no authority.
            self.selection_pending = False
            if result.selection_key != demand.selection_key:
                self.selection_failed = True
                return None
            # This exact new key was selected by a fresh radio-observed SC cycle.
            # Bind it once without requiring a second cycle for the same action.
            self.selection_key = result.selection_key
        if self.selection_failed:
            return None
        if demand.selection_key != self.selection_key:
            self.selection_key = demand.selection_key
            if self.greeted:
                self._rotate(now, "selected target changed")
        return demand

    def step(self, sample: SourceSample):
        """One short nonblocking iteration; caller polls independently of HTTP."""
        if self.failed:
            raise ProbeError("failed USB connection must be closed")
        try:
            now = self._now()
            self._prune_timing(now)
            drained = self._receive(now)
            demand = self._source(sample, now)
            if not self.greeted:
                if now - self.started_at >= 5.:
                    raise ProbeError("timeout waiting for ARGOS_DISTANCE_STREAM_V3")
                return
            if self.pending_begin:
                # A non-command prefix invalidates a partial previous input.
                if drained and self._write(f"#\nDB3 {self.session}\n".encode(), now):
                    self.pending_begin = False
                    self.session_started_at = now
                return
            last_health = (self.session_started_at if self.last_status_at is None
                           else self.last_status_at)
            if now - last_health >= HEALTH_TIMEOUT:
                self._rotate(now, "radio status stopped advancing")
                return
            if self.status is None:
                self.reason = "waiting for radio session status"
                return
            if self.pending_since is not None and now - self.pending_since >= HEALTH_TIMEOUT:
                self._rotate(now, "radio acknowledgements stopped advancing")
                return
            state = self.status.state
            if state not in ("T", "A"):
                self.reason = "manual: SC middle then the desired assist position required" if state == "M" else "radio fault: return SC to middle"
                return
            if not drained:
                return
            if self.sent_sequence >= MAX_SEQUENCE:
                self._rotate(now, "sequence exhausted")
                return
            # Recheck the clock beside the bounded write, including after I/O.
            now = self._now()
            valid = bool(demand is not None and demand.valid and demand.selection_key is not None
                         and demand.deadline > now and not self.source_fault
                         and demand.mode == self.status.mode)
            pitch_valid = bool(valid and self.status.mode == "D" and demand.pitch_valid)
            # Keep the physical spacing even across session rotation. Neither
            # an accumulated backlog nor a stalled scheduler earns a burst.
            if (self._last_command_at is not None
                    and now < self._last_command_at + MIN_COMMAND_INTERVAL):
                return
            frame = getattr(demand, "frame_sequence", None) if valid else None
            frame_key = ((demand.selection_key, frame)
                         if type(frame) is int and frame >= 0 else None)
            new_frame = (frame_key is not None
                         and (self._last_frame_key is None
                              or frame_key[0] != self._last_frame_key[0]
                              or frame_key[1] > self._last_frame_key[1]))
            withdrawing = (self.last_sent_valid is True and not valid
                           or self.last_sent_pitch_valid is True and not pitch_valid)
            if now < self.next_send_at and not (new_frame or withdrawing):
                return
            value = demand.value if valid else 0
            pitch = demand.pitch if pitch_valid else 0
            seq = self.sent_sequence + 1
            # Keep producing the same-reference demand during a temporary stick
            # correction of any amplitude. The radio releases each axis locally
            # and requires a new post-recenter ticket before restoring it. Axis
            # phase changes are not target-selection transactions.
            packet = (f"DS3 {self.session} {self.status.generation} {self.status.ticket} "
                      f"{seq} {int(valid)} {value} {int(pitch_valid)} {pitch}\n").encode()
            if len(packet) - 1 > MAX_LINE:
                raise ProbeError("distance command exceeds the radio line limit")
            if self._write(packet, now):
                self.sent_sequence = seq
                self.last_sent_valid = valid
                self.last_sent_pitch = pitch
                self.last_sent_pitch_valid = pitch_valid
                finished = self._now()
                self.last_sent_command = dict(session=self.session, generation=self.status.generation,
                    sequence=seq, mode=self.status.mode, yaw=dict(valid=valid, value=value),
                    pitch=dict(valid=pitch_valid, value=pitch), frame_sequence=frame,
                    write_finished_at=finished)
                self._last_command_at = finished
                self.next_send_at = finished + INTERVAL  # no catch-up bursts
                if new_frame:
                    self._last_frame_key = frame_key
                if valid:
                    self._remember_timing(seq, demand, now, finished)
                if self.pending_since is None:
                    self.pending_since = now
                if self.selection_pending:
                    self.reason = "manual requested: selecting target for this SC cycle"
                elif self.selection_failed:
                    self.reason = "manual: target selection failed; SC middle then the desired assist position to select again"
                elif not valid:
                    self.reason = "manual requested: target temporarily unavailable"
                elif self.status.yaw_phase in ("M", "R") or self.status.pitch_phase in ("M", "R"):
                    phases = (("yaw", self.status.yaw_phase), ("pitch", self.status.pitch_phase))
                    self.reason = "; ".join(
                        f"{axis} temporarily manual" if phase == "M" else
                        f"{axis} waiting for a fresh command after recentering"
                        for axis, phase in phases if phase in ("M", "R"))
                elif state == "A" and self.status.ack > 0:
                    self.reason = "radio reports assistance active"
                else:
                    self.reason = "correction requested: waiting for radio acceptance"
        except BaseException:
            self._clear_timing()
            self.failed = True
            raise


def run_stream(device, source, *, duration=None, opener=open_port, clock=time.monotonic,
               sleep=time.sleep, report=print, on_status=None, stop_event=None,
               worker_factory=None):
    """Keep the integrated source alive across USB reconnects; every connection rearms.

    The worker must support ``request_selection(token, mode)``. This experimental
    transport intentionally has no HTTP fallback or standalone arming shortcut.
    """
    if worker_factory is None:
        raise ValueError("distance stream requires its integrated local source worker")
    worker = worker_factory(source, clock=clock)
    worker.start()
    started = clock()
    port = stream = None
    reconnect_at = started
    previous_reason = None
    next_status_at = started
    try:
        while ((stop_event is None or not stop_event.is_set())
               and (duration is None or clock() - started < duration)):
            now = clock()
            if stream is None and now >= reconnect_at:
                try:
                    port = opener(device)
                    port.reset_input_buffer()
                    stream = DistanceStream(port, clock=clock,
                                       request_selection=getattr(worker, "request_selection", None))
                except (OSError, ProbeError):
                    if port is not None:
                        port.close()
                    port = None
                    reconnect_at = now + 1.
            if stream is not None:
                try:
                    stream.step(worker.snapshot())
                    reason = stream.reason
                except (OSError, ProbeError) as exc:
                    reason = f"USB stopped: {exc}; waiting to reconnect, manual control required"
                    port.close()
                    port = stream = None
                    reconnect_at = clock() + 1.
                if reason != previous_reason:
                    report(reason)
                    previous_reason = reason
            if on_status is not None and now >= next_status_at:
                status = (stream.snapshot() if stream is not None else
                          dict(connected=False, reason=previous_reason or "waiting for Pocket USB"))
                metrics = getattr(worker, "metrics", None)
                status["source_poll"] = metrics() if metrics is not None else {}
                on_status(status)
                next_status_at = now + INTERVAL
            sleep(.005)
    finally:
        if port is not None:
            port.close()
        worker.close()
        if on_status is not None:
            status = stream.snapshot() if stream is not None else {}
            metrics = getattr(worker, "metrics", None)
            status.update(connected=False, reason="sending stopped", stopped=True,
                          source_poll=metrics() if metrics is not None else {})
            on_status(status)
