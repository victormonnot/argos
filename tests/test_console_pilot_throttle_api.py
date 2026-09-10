"""Pilot-throttle assistance through the real HTTP and MAVLink boundaries."""

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("pymavlink")
from fastapi.testclient import TestClient

from argos.backends.mavlink import SequenceScope
from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.session import ConsoleSession
from argos.console.vision import AnalyzedFrame, VisionService
from test_console_flight_control import Link, ZERO, heartbeat, landed, profile, simstate


ORIGIN = {"origin": "http://testserver"}
TOPIC = "/world/iris_runway/model/iris_with_gimbal/model/gimbal/link/pitch_link/sensor/camera/image"
PERSON = {"track_id": 1, "box": [.5, .3, .1, .2], "confidence": .9}


@pytest.fixture
def pilot_flight(tmp_path, request):
    """Inject only vehicle/image receipts; pilot actions use actual HTTP routes."""
    mode = getattr(request, "param", 0)
    now = [0.]
    config = ConsoleConfig(
        sim_control=True, sim_framing=True, vision_model=Path("model.onnx"),
        video_source="gazebo", video_endpoint=TOPIC, environment="simulation",
        mavlink_tcp=("127.0.0.1", 5860), sequence_scope=SequenceScope.CHANNEL,
        recordings_dir=tmp_path,
    )
    session = ConsoleSession(config, clock=lambda: now[0])
    control, link = session.control, Link()
    session.link, session._started = link, True
    vision = VisionService(None)
    vision._context = session.run_id, session.video_source_id
    client = TestClient(create_app(session=session, vision=vision))

    def post(operation, body):
        return client.post(f"/api/control/{operation}", json=body, headers=ORIGIN)

    heartbeat(control, mode=mode)
    simstate(control)
    landed(control)
    claimed = post("claim", {})
    assert claimed.status_code == 200
    token = claimed.json()["token"]
    profile(control)
    assert post("action", {"token": token, "action": "prepare", "mode": mode}).status_code == 200
    heartbeat(control, mode=mode)
    now[0] = .01
    assert post("action", {"token": token, "action": "arm"}).status_code == 200
    heartbeat(control, .02, armed=True, mode=mode)
    landed(control, .02, 2)
    seq, intent = [0], [0]

    def pilot_input(throttle, *, axes=None, **fields):
        seq[0] += 1
        return post("input", {
            "token": token, "seq": seq[0], "axes": ZERO if axes is None else axes,
            "throttle": throttle, "mode_generation": control.state(now[0])["mode_generation"],
            **fields,
        })

    now[0] = .03
    assert pilot_input(.42 if mode == 0 else 0.).status_code == 200

    def observe(detections=None):
        session.video.accept_raw(width=2, height=2, step=6, pixel_format="RGB_INT8",
                                 data=bytes(12), received_at=now[0])
        sample = session.video.latest(now[0])
        detections = [PERSON] if detections is None else detections
        candidate = AnalyzedFrame(sample, (session.run_id, session.video_source_id), {
            "width": 2, "height": 2, "detections": detections, "inference_ms": 1.,
        })
        vision._frame = candidate
        vision._selection_history.append((candidate.context, sample.sequence, now[0],
                                          frozenset(item["track_id"] for item in detections)))
        session._observe_vision()
        return sample.sequence

    def framing(operation, **fields):
        intent[0] += 1
        return post("framing", {"token": token, "operation": operation,
                                 "intent": intent[0], **fields})

    def select():
        return framing("select", run_id=session.run_id, video_id=session.video_source_id,
                       frame_sequence=session.video.latest(now[0]).sequence, track_id=1,
                       mode_generation=control.state(now[0])["mode_generation"])

    def engage(**fields):
        return framing("engage", revision=control.framing.revision, input_seq=seq[0],
                       mode_generation=control.state(now[0])["mode_generation"], **fields)

    def refresh(at, detections=None):
        now[0] = at
        heartbeat(control, at, armed=True, mode=control.state(at)["selected_mode"])
        simstate(control, at)
        landed(control, at, 2)
        observe(detections)

    now[0] = .05
    observe()
    yield locals()
    client.close()


def test_nonzero_throttle_selection_is_passive_and_advertises_matching_profile(pilot_flight):
    f = pilot_flight
    count = len(f["link"].sent)
    response = f["select"]()
    assert response.status_code == 200
    control = response.json()["control"]
    assert control["throttle"] == .42 and len(f["link"].sent) == count
    assert control["framing"]["profiles"]["pilot_throttle"]["available"]
    assert not control["framing"]["profiles"]["full"]["available"]


def test_throttle_packet_racing_engage_keeps_selection_and_uses_latest_gas(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    assert f["select"]().status_code == 200
    revision, observed_seq = c.framing.revision, f["seq"][0]
    f["now"][0] = .06
    assert f["pilot_input"](.57).status_code == 200
    response = f["framing"]("engage", revision=revision, input_seq=observed_seq,
                             profile="pilot_throttle", mode_generation=0)
    assert response.status_code == 200
    assert response.json()["control"]["framing"]["profile"] == "pilot_throttle"
    assert response.json()["control"]["throttle"] == .57
    f["refresh"](.2)
    c.tick(f["link"], .2)
    wire = f["link"].messages("MANUAL_CONTROL")[-1]
    assert wire.r > 0 and wire.y == 0 and wire.z == 570
    assert c.framing.state(.2)["axes"]["up"] == 0

    revision = c.framing.revision
    for at, gas in ((.3, 0.), (.4, 1.), (.5, .481), (.6, .481)):
        f["refresh"](at)
        response = f["pilot_input"](gas)
        assert response.status_code == 200
        assert response.json()["control"]["framing"]["active"]
        assert c.framing.revision == revision
        c.tick(f["link"], at)
        assert f["link"].messages("MANUAL_CONTROL")[-1].z == round(gas * 1000)


@pytest.mark.parametrize("fields", [{}, {"profile": "full"}])
def test_stabilize_never_silently_converts_full_engage_to_pilot_throttle(pilot_flight, fields):
    f = pilot_flight
    assert f["select"]().status_code == 200
    response = f["engage"](**fields)
    assert response.status_code == 409 and "AltHold" in response.json()["detail"]
    assert not f["control"].framing.state(f["now"][0])["active"]


@pytest.mark.parametrize("pilot_flight", [2], indirect=True)
def test_pilot_throttle_rejects_althold_while_legacy_default_still_engages(pilot_flight):
    f = pilot_flight
    assert f["select"]().status_code == 200
    response = f["engage"](profile="pilot_throttle")
    assert response.status_code == 409 and "Stabilize" in response.json()["detail"]
    response = f["engage"]()
    assert response.status_code == 200
    assert response.json()["control"]["framing"]["profile"] == "full"


@pytest.mark.parametrize("value", [None, True, False, 0, [], {}, "", "radio", "Pilot_throttle"])
def test_profile_validation_is_strict_at_http_boundary(pilot_flight, value):
    f = pilot_flight
    assert f["select"]().status_code == 200
    response = f["engage"](profile=value)
    assert response.status_code == 422
    assert not f["control"].framing.state(f["now"][0])["active"]


@pytest.mark.parametrize("operation", ["stop", "clear", "closer", "farther"])
def test_profile_cannot_be_smuggled_into_other_framing_operations(pilot_flight, operation):
    assert pilot_flight["framing"](operation, profile="pilot_throttle").status_code == 422


def test_pause_and_recovery_neutralize_assistance_but_keep_pilot_gas(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    assert f["select"]().status_code == 200
    assert f["engage"](profile="pilot_throttle").status_code == 200
    f["refresh"](.2)
    c.tick(f["link"], .2)
    assert f["link"].messages("MANUAL_CONTROL")[-1].r > 0
    f["refresh"](.3, [])
    response = f["pilot_input"](.6)
    assert response.status_code == 200 and response.json()["control"]["framing"]["paused"]
    c.tick(f["link"], .3)
    wire = f["link"].messages("MANUAL_CONTROL")[-1]
    assert (wire.x, wire.y, wire.z, wire.r) == (0, 0, 600, 0)
    assert f["framing"]("closer").status_code == 409
    f["refresh"](.5)
    assert f["pilot_input"](.55).status_code == 200
    c.tick(f["link"], .5)
    assert c.framing.phase == "active" and not c.framing.state(.5)["paused"]
    assert f["link"].messages("MANUAL_CONTROL")[-1].z == 550


def test_throttle_activity_does_not_acknowledge_loss_or_extend_land_deadline(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    assert f["select"]().status_code == 200
    assert f["engage"](profile="pilot_throttle").status_code == 200
    other = [{**PERSON, "track_id": 2}]
    f["refresh"](.2, other)
    c.tick(f["link"], .2)
    assert c.framing.phase == "takeover"
    for i in range(1, 20):
        at = .2 + i * .1
        f["refresh"](at, other)
        gas = .4 + (i % 2) * .1
        response = f["pilot_input"](gas)
        assert response.status_code == 200
        state = response.json()["control"]["framing"]
        assert state["phase"] == "takeover"
        assert state["takeover_remaining_s"] == pytest.approx(2.2 - at)
        c.tick(f["link"], at)
        wire = f["link"].messages("MANUAL_CONTROL")[-1]
        assert (wire.x, wire.y, wire.z, wire.r) == (0, 0, round(gas * 1000), 0)
    f["refresh"](2.2, other)
    assert f["pilot_input"](.65).status_code == 409
    assert c.state(2.2)["owned"] is False and c.state(2.2)["command"]["action"] == "land"
    assert f["link"].messages("MANUAL_CONTROL")[-1].z == 500
    assert c.interruption["framing_loss"]["profile"] == "pilot_throttle"


@pytest.mark.parametrize("operation", ["stop", "direction"])
def test_manual_takeover_preserves_gas_and_wins_delayed_engage(pilot_flight, operation):
    f, c = pilot_flight, pilot_flight["control"]
    assert f["select"]().status_code == 200
    old = {"token": f["token"], "operation": "engage", "intent": 2,
           "profile": "pilot_throttle", "revision": c.framing.revision,
           "input_seq": f["seq"][0], "mode_generation": 0}
    assert f["engage"](profile="pilot_throttle").status_code == 200
    f["refresh"](.2)
    assert f["pilot_input"](.63).status_code == 200
    if operation == "stop":
        assert f["framing"]("stop").status_code == 200
    else:
        assert f["pilot_input"](.63, axes={**ZERO, "yaw": -1}).status_code == 200
    c.tick(f["link"], .2)
    wire = f["link"].messages("MANUAL_CONTROL")[-1]
    assert wire.z == 630 and wire.r == (-300 if operation == "direction" else 0)
    assert not c.framing.state(.2)["active"]
    assert f["post"]("framing", old).status_code == 409
    assert not c.framing.state(.2)["active"]


def test_gas_edit_supersedes_pending_flight_mode_switch_without_stopping_framing(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    assert f["select"]().status_code == 200
    assert f["engage"](profile="pilot_throttle").status_code == 200
    old_seq = f["seq"][0]
    assert f["pilot_input"](.55).status_code == 200
    response = f["post"]("action", {"token": f["token"], "action": "switch_mode",
                                   "mode": 2, "mode_generation": 0, "input_seq": old_seq})
    assert response.status_code == 409 and "superseded" in response.json()["detail"]
    assert c.framing.phase == "active" and c.state(f["now"][0])["mode_generation"] == 0


def test_mode_transfer_clears_shared_lock_and_fences_delayed_profile_requests(pilot_flight):
    f, c = pilot_flight, pilot_flight["control"]
    assert f["select"]().status_code == 200
    assert f["engage"](profile="pilot_throttle").status_code == 200
    old_revision = c.framing.revision
    response = f["post"]("action", {"token": f["token"], "action": "switch_mode",
                                   "mode": 2, "mode_generation": 0, "input_seq": f["seq"][0]})
    assert response.status_code == 200
    state = response.json()["control"]
    assert state["mode_generation"] == 1 and state["framing"]["target_id"] is None
    assert not state["framing"]["active"]
    f["now"][0] = .1
    heartbeat(c, .1, armed=True, mode=2)
    landed(c, .1, 2)
    assert f["pilot_input"](0.).status_code == 200
    assert c.state(.1)["mode_generation"] == 2
    for operation, fields in (
        ("engage", {"revision": old_revision, "input_seq": 1, "profile": "pilot_throttle"}),
        ("select", {"run_id": f["session"].run_id, "video_id": f["session"].video_source_id,
                    "frame_sequence": f["session"].video.latest(.1).sequence, "track_id": 1}),
        ("closer", {}), ("farther", {}),
    ):
        response = f["framing"](operation, mode_generation=0, **fields)
        assert response.status_code == 409 and response.json()["code"] == "stale_mode_generation"
        assert c.state(.1)["framing"]["target_id"] is None
    assert f["framing"]("stop").status_code == 200


@pytest.mark.parametrize("changes", [
    {"sim_control": False},
    {"video_source": "device", "video_endpoint": "/dev/video0", "environment": "real"},
    {"environment": "real"},
    {"mavlink_tcp": ("192.0.2.1", 5860)},
])
def test_new_profile_does_not_bypass_existing_simulation_admission(changes):
    fields = dict(sim_control=True, sim_framing=True, vision_model=Path("model.onnx"),
                  video_source="gazebo", video_endpoint=TOPIC, environment="simulation",
                  mavlink_tcp=("127.0.0.1", 5860), sequence_scope=SequenceScope.CHANNEL)
    with pytest.raises(ValueError):
        ConsoleConfig(**(fields | changes))


def test_unconfigured_console_cannot_enable_assistance_from_http_payload(tmp_path):
    session = ConsoleSession(ConsoleConfig(recordings_dir=tmp_path))
    with TestClient(create_app(session=session)) as client:
        response = client.post("/api/control/framing", json={
            "token": "no-lease", "operation": "engage", "intent": 1,
            "profile": "pilot_throttle", "revision": 0, "input_seq": 0,
        }, headers=ORIGIN)
        assert response.status_code == 409
        assert session.control.state(session.clock())["enabled"] is False
