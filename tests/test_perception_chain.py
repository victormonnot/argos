"""The bench the audit asked for: interrupt the source, let time pass, watch it expire.

Perception, safety and the backend meet here, with the real objects. The one thing
still standing in is the detector, which returns a fixed answer rather than looking at
pixels: a real one needs weights and a measured cost, and it arrives with the
benchmark that reports them. Everything between the frame and the aircraft is the
shipped code.

The last test is the one worth reading. It shows that when a producer freezes its own
validity -- which is what the lab did -- **the safety layer does not catch it**, and
cannot: the target-age limit is computed from numbers the producer supplies, so a
producer reporting a stale view as fresh defeats it. That is why the fix belongs where
the frames are and not at the gate.
"""
from __future__ import annotations

import numpy as np
import pytest

from argos.backends import Applied, AttitudeSim
from argos.core import Detection, TargetView
from argos.guidance import VisualGuidance
from argos.perception import Frame, TrackPolicy, TrackState, Tracker
from argos.safety import CommandGate, Envelope, Intervention

TICK = 1.0 / 64.0
IMAGE = np.zeros((48, 64, 3), dtype=np.uint8)


class Camera:
    """A source a test starts and stops. No sensor, no transport, no jitter."""

    def __init__(self) -> None:
        self._frame: Frame | None = None
        self._seq = 0
        self.live = True

    def capture(self, now: float) -> None:
        """Take a frame, if the camera is still working."""
        if not self.live:
            return
        self._seq += 1
        self._frame = Frame(image=IMAGE.copy(), t_received=now, seq=self._seq)

    def read(self) -> Frame | None:
        return self._frame

    def close(self) -> None:
        self.live = False

    def reopen(self) -> None:
        """The camera starts working again. Nothing else about the run changes."""
        self.live = True


class FixedDetector:
    """Always finds one person, in the same place. A stand-in, and only that."""

    def __init__(self, cx: float = 0.75, h: float = 0.05) -> None:
        self.cx, self.h = cx, h

    def detect(self, image: np.ndarray) -> list[Detection]:
        return [Detection(cx=self.cx, cy=0.5, w=self.h / 2, h=self.h,
                          confidence=0.9, label="person")]


def flight(camera: Camera, cycles: int, world: AttitudeSim, gate: CommandGate,
           law: VisualGuidance, tracker: Tracker, detector: FixedDetector,
           frame_every: int = 4, designate: bool = True) -> list[bool]:
    """Run the loop: capture, detect, track, guide, submit, step. Records what was sent.

    ``designate`` is the operator, stubbed. With it on, the loop takes a lock as soon
    as there is something to lock onto, which is a convenience for setting a scenario
    up. **It must be turned off in any scenario about resuming**, or the bench quietly
    supplies the very act the test is trying to show is required: a designation that
    ends and then comes back because the harness re-took it proves nothing about the
    tracker. No production orchestrator exists yet; this shortcut is the bench's.
    """
    sent = []
    seen = -1
    for cycle in range(cycles):
        now = world.time()
        if cycle % frame_every == 0:
            camera.capture(now)

        frame = camera.read()
        if frame is not None and frame.seq != seen:
            seen = frame.seq
            detections = detector.detect(frame.image)
            if designate and not tracker.locked and detections:
                tracker.designate(detections[0], frame)
            else:
                tracker.update(frame, detections, now=now)

        view = tracker.view(now)
        cmd = law.step(view, now=now, engage=True)
        sent.append(gate.submit(cmd, world.observe(0).me, view).sent)
        world.step(TICK)
    return sent


def rig(coast: float = 1.5, max_frame_age: float = 0.3):
    world = AttitudeSim(command_lifetime=0.25, max_step=TICK)
    return (
        world,
        CommandGate(world),
        VisualGuidance(),
        Tracker(TrackPolicy(coast=coast, max_frame_age=max_frame_age)),
        FixedDetector(),
        Camera(),
    )


def test_the_whole_chain_flies_while_the_camera_is_working() -> None:
    """The baseline the interruption is measured against."""
    world, gate, law, tracker, detector, camera = rig()

    sent = flight(camera, 128, world, gate, law, tracker, detector)

    assert all(sent)
    assert tracker.telemetry.state in (TrackState.TRACKING, TrackState.COASTING)
    assert world.observe(0).me.pos[0] > 0.05          # it moved toward the target
    assert world.applied[Applied.EXPIRED] == 0


def test_a_camera_that_stops_expires_the_target_and_levels_the_aircraft() -> None:
    """The audit's bench, run through the shipped chain.

    The camera is switched off and nothing else changes: the loop keeps running at its
    own cadence, the clock keeps advancing, and the designation ends because time
    passed rather than because an image arrived to say so.
    """
    world, gate, law, tracker, detector, camera = rig()
    flight(camera, 128, world, gate, law, tracker, detector)
    moving = float(world.observe(0).me.vel[0])
    assert moving > 0.05

    camera.close()                                    # and nothing else
    flight(camera, 128, world, gate, law, tracker, detector)

    assert tracker.telemetry.state is TrackState.STALLED
    assert not tracker.view(world.time()).has
    assert world.last_flown.pitch == pytest.approx(0.0)
    assert world.last_flown.roll == pytest.approx(0.0)
    assert float(world.observe(0).me.vel[0]) < moving  # slowing on drag alone


def test_the_loop_keeps_holding_so_nothing_expires_at_the_backend() -> None:
    """Levelling off here is a decision, not a timeout.

    The law returns a neutral once the designation is gone and the gate deposits it,
    so the aircraft is commanded level at the cycle the camera loss was noticed. The
    backend's expiry is the second line and never has to fire.
    """
    world, gate, law, tracker, detector, camera = rig()
    flight(camera, 64, world, gate, law, tracker, detector)
    camera.close()
    sent = flight(camera, 128, world, gate, law, tracker, detector)

    assert all(sent)
    assert world.applied[Applied.EXPIRED] == 0


def test_a_producer_that_freezes_its_own_validity_defeats_the_safety_layer() -> None:
    """Why the fix has to live in perception, stated as a failing alternative.

    This models the ported defect directly: a view whose ``has``, ``found`` and
    ``age`` were computed once and then handed out unchanged, which is what a tracker
    does when its validity is refreshed by frames instead of by the clock. The camera
    is dead for two full seconds.

    The gate accepts every one of these commands. It is not a hole in the gate: the
    target-age limit is ``age + (now - t)``, both supplied by the producer, so a
    producer reporting a two-second-old observation as fresh is not something a
    downstream check can see. Only the layer holding the frames knows.
    """
    world = AttitudeSim(command_lifetime=0.25, max_step=TICK)
    gate = CommandGate(world)
    law = VisualGuidance()

    frozen_age = 0.0
    accepted = 0
    for _ in range(128):                              # two seconds at 64 Hz
        now = world.time()
        stale = TargetView(has=True, found=True, error_x=0.5, size=0.05,
                           age=frozen_age, t=now)     # the age never grows
        result = gate.submit(law.step(stale, now=now, engage=True),
                             world.observe(0).me, stale)
        accepted += result.sent
        world.step(TICK)

    assert accepted == 128
    assert gate.last.interventions == ()
    assert world.observe(0).me.vel[0] > 0.05          # still flying at it
    assert Envelope().max_target_age == 1.0           # a limit that never fired


def test_the_same_limit_does_fire_once_the_age_is_honest() -> None:
    """The control: let the age grow with the clock and the gate refuses on its own.

    So the safety layer works exactly as documented. What it cannot do is compensate
    for a producer that misreports, which is the whole argument for fixing this where
    the frames are.
    """
    world = AttitudeSim(command_lifetime=0.25, max_step=TICK)
    gate = CommandGate(world)
    law = VisualGuidance()

    refusals = []
    start = world.time()
    for _ in range(128):
        now = world.time()
        honest = TargetView(has=True, found=False, error_x=0.5, size=0.05,
                            age=now - start, t=now)
        result = gate.submit(law.step(honest, now=now, engage=True),
                             world.observe(0).me, honest)
        refusals.append(result.interventions)
        world.step(TICK)

    assert (Intervention.STALE_TARGET,) in refusals


def test_a_camera_that_comes_back_does_not_resume_tracking_by_itself() -> None:
    """The end of a designation is final all the way through the chain.

    The camera dies, the designation ends, the camera comes back and keeps producing
    perfectly good detections of the same person in the same place. Nothing resumes:
    taking a lock is an operator act and so is getting one back.

    The bench's automatic designation is **off** for the phases that matter. With it
    on this test would pass without the tracker doing anything, because the harness
    would have supplied the operator.
    """
    world, gate, law, tracker, detector, camera = rig()
    flight(camera, 128, world, gate, law, tracker, detector)
    assert tracker.locked

    camera.close()
    flight(camera, 64, world, gate, law, tracker, detector, designate=False)
    assert not tracker.locked
    assert tracker.telemetry.state is TrackState.STALLED

    camera.reopen()
    flight(camera, 128, world, gate, law, tracker, detector, designate=False)

    assert not tracker.locked
    assert not tracker.view(world.time()).has
    assert tracker.telemetry.state is TrackState.STALLED
    assert world.last_flown.pitch == pytest.approx(0.0)

    # ... and the operator can take it back, at which point the chain flies again.
    frame = camera.read()
    tracker.designate(detector.detect(frame.image)[0], frame)
    resumed = flight(camera, 128, world, gate, law, tracker, detector, designate=False)

    assert all(resumed)
    assert tracker.telemetry.state in (TrackState.TRACKING, TrackState.COASTING)
    assert world.last_flown.pitch < 0.0            # advancing again
