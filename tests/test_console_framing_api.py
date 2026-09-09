"""Operator intent, vehicle receipts and actual MAVLink output arbitrate framing."""

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("pymavlink")
from fastapi.testclient import TestClient
from argos.backends.mavlink import SequenceScope
from argos.console.control import FlightControl
from argos.console.framing import DETECTION_PAUSE
from argos.console.config import ConsoleConfig
from argos.console.session import ConsoleSession
from argos.console.app import create_app
from argos.console.video import VideoSample
from argos.console.vision import VisionService, AnalyzedFrame
from test_console_flight_control import Link, heartbeat, simstate, profile, landed, ZERO

ORIGIN = {"origin": "http://testserver"}
TOPIC = "/world/iris_runway/model/iris_with_gimbal/model/gimbal/link/pitch_link/sensor/camera/image"
OTHER_PERSON = [{"track_id": 2, "box": [0.5, 0.3, 0.1, 0.2], "confidence": 0.9}]


@pytest.fixture
def flight(tmp_path):
    now = [0.05]
    config = ConsoleConfig(
        sim_control=True,
        sim_framing=True,
        vision_model=Path("model.onnx"),
        video_source="gazebo",
        video_endpoint=TOPIC,
        environment="simulation",
        mavlink_tcp=("127.0.0.1", 5860),
        sequence_scope=SequenceScope.CHANNEL,
        recordings_dir=tmp_path,
    )
    session = ConsoleSession(config, clock=lambda: now[0])
    control, link = session.control, Link()
    session.link = link
    session._started = True
    heartbeat(control)
    simstate(control)
    landed(control)
    token = control.claim(link, 0.0)["token"]
    profile(control)
    control.action(token, "prepare", mode=2, link=link, now=0.0)
    heartbeat(control, mode=2)
    control.action(token, "arm", link=link, now=0.01)
    heartbeat(control, 0.02, armed=True, mode=2)
    landed(control, 0.02, 2)
    control.input(token, 1, ZERO, link=link, now=0.03)
    vision = VisionService(None)
    sequence = [0]

    def observe(*, at=None, detections=None):
        at = now[0] if at is None else at
        sequence[0] += 1
        ds = (
            [{"track_id": 1, "box": [0.5, 0.3, 0.1, 0.2], "confidence": 0.9}]
            if detections is None
            else detections
        )
        session.video.accept_raw(
            width=2,
            height=2,
            step=6,
            pixel_format="RGB_INT8",
            data=bytes(12),
            received_at=at,
        )
        sample = session.video.latest(at)
        candidate = AnalyzedFrame(
            sample,
            (session.run_id, session.video_source_id),
            {"width": 2, "height": 2, "detections": ds, "inference_ms": 1.0},
        )
        vision._frame = candidate
        vision._selection_history.append(
            (
                candidate.context,
                sample.sequence,
                at,
                frozenset(d["track_id"] for d in ds),
            )
        )
        session._observe_vision()
        return sample.sequence

    client = TestClient(create_app(session=session, vision=vision))
    observe()
    intent = [0]

    def request(operation, **fields):
        intent[0] += 1
        return client.post(
            "/api/control/framing",
            json=dict(token=token, operation=operation, intent=intent[0], **fields),
            headers=ORIGIN,
        )

    def select():
        return request(
            "select",
            run_id=session.run_id,
            video_id=session.video_source_id,
            frame_sequence=session.video.latest(now[0]).sequence,
            track_id=1,
        )

    def engage():
        return request(
            "engage", revision=control.framing.revision, input_seq=control._seq
        )

    yield locals()
    client.close()


def test_source_config_requires_explicit_validated_simulation_camera():
    with pytest.raises(ValueError, match="framing requires"):
        ConsoleConfig(sim_framing=True)
    assert not ConsoleConfig().sim_framing
    assert "sim_framing" not in ConsoleConfig().public()


def test_selection_is_passive_then_engagement_uses_image_axes_at_same_manual_boundary(
    flight,
):
    f = flight
    c = f["control"]
    link = f["link"]
    now = f["now"]
    count = len(link.sent)
    assert f["select"]().status_code == 200
    assert len(link.sent) == count
    assert f["engage"]().status_code == 200
    assert not link.messages("MANUAL_CONTROL")[-1].r
    now[0] = 0.25
    f["observe"]()
    c.tick(link, now[0])
    assert c.framing.phase == "active"
    assert link.messages("MANUAL_CONTROL")[-1].r > 0
    assert c.state(now[0])["axes"] == ZERO  # browser neutral input is independent
    assert link.messages("MANUAL_CONTROL")[-1].z > 500  # person above center


@pytest.mark.parametrize("landed_state", [0, 1, 3, 4])
def test_only_actual_in_air_receipt_allows_engage(flight, landed_state):
    f = flight
    assert f["select"]().status_code == 200
    landed(f["control"], f["now"][0], landed_state)
    response = f["engage"]()
    assert response.status_code == 409 and "IN_AIR" in response.json()["detail"]
    assert f["control"].framing.phase == "selected"


def test_stop_supersedes_an_older_engage_even_when_requests_arrive_out_of_order(flight):
    f = flight
    assert f["select"]().status_code == 200
    old = dict(
        token=f["token"],
        operation="engage",
        intent=2,
        revision=f["control"].framing.revision,
        input_seq=1,
    )
    assert (
        f["client"]
        .post(
            "/api/control/framing",
            json=dict(token=f["token"], operation="stop", intent=3),
            headers=ORIGIN,
        )
        .status_code
        == 200
    )
    assert (
        f["client"].post("/api/control/framing", json=old, headers=ORIGIN).status_code
        == 409
    )
    assert f["control"].framing.phase != "active"


def test_manual_input_then_release_supersedes_an_old_engage(flight):
    f = flight
    c = f["control"]
    assert f["select"]().status_code == 200
    revision = c.framing.revision
    c.input(f["token"], 2, {**ZERO, "yaw": 0.2}, link=f["link"], now=0.06)
    c.input(f["token"], 3, ZERO, link=f["link"], now=0.07)
    response = f["request"]("engage", revision=revision, input_seq=1)
    assert response.status_code == 409
    # Even a client with the new selection revision cannot reuse old input evidence.
    response = f["request"]("engage", revision=c.framing.revision, input_seq=1)
    assert response.status_code == 409 and "manual input" in response.json()["detail"]


def test_neutral_keepalives_do_not_supersede_engage_or_replace_derived_output(flight):
    f = flight
    c = f["control"]
    assert f["select"]().status_code == 200
    revision = c.framing.revision
    c.input(f["token"], 2, ZERO, link=f["link"], now=0.06)
    assert f["request"]("engage", revision=revision, input_seq=1).status_code == 200
    f["now"][0] = 0.25
    f["observe"]()
    c.tick(f["link"], 0.25)
    yaw = c.framing.axes(0.25)["yaw"]
    assert yaw > 0
    c.input(f["token"], 3, ZERO, link=f["link"], now=0.26)
    assert c.framing.phase == "active" and c.framing.axes(0.26)["yaw"] == yaw


def test_manual_input_immediately_wins_at_wire_boundary(flight):
    f = flight
    c = f["control"]
    f["select"]()
    f["engage"]()
    f["now"][0] = 0.25
    f["observe"]()
    c.tick(f["link"], 0.25)
    c.input(f["token"], 2, {**ZERO, "yaw": -1}, link=f["link"], now=0.3)
    c.tick(f["link"], 0.31)
    assert c.framing.phase != "active"
    assert f["link"].messages("MANUAL_CONTROL")[-1].r == -300


def test_lost_target_land_deadline_is_not_renewed_by_neutral_browser_traffic(flight):
    f = flight
    c = f["control"]
    f["select"]()
    f["engage"]()
    f["now"][0] = 0.2
    f["observe"](detections=OTHER_PERSON)
    c.tick(f["link"], 0.2)
    assert c.framing.phase == "takeover" and c.framing.axes(0.2) == ZERO
    for i in range(2, 22):
        at = 0.2 + i * 0.09
        heartbeat(c, at, armed=True)
        simstate(c, at)
        landed(c, at, 2)
        c.input(f["token"], i, ZERO, link=f["link"], now=at)
    c.tick(f["link"], 2.21)
    assert c._token is None
    assert c._command["action"] == "land"
    count = len(f["link"].sent)
    c.tick(f["link"], 2.4)
    assert len(f["link"].sent) == count  # no GCS/manual sends inhibiting LAND fallback


def test_explicit_manual_takeover_acknowledges_loss_without_landing(flight):
    f = flight
    c = f["control"]
    f["select"]()
    f["engage"]()
    f["now"][0] = 0.2
    f["observe"](detections=OTHER_PERSON)
    c.tick(f["link"], 0.2)
    assert f["request"]("stop").status_code == 200
    for i in range(2, 30):
        at = 0.2 + i * 0.09
        heartbeat(c, at, armed=True)
        simstate(c, at)
        landed(c, at, 2)
        c.input(f["token"], i, ZERO, link=f["link"], now=at)
        c.tick(f["link"], at)
    assert c._token is not None and c._command["action"] != "land"


@pytest.mark.parametrize("action", ["prepare", "arm"])
@pytest.mark.parametrize("lost", [False, True])
def test_rejected_ground_actions_do_not_stop_framing_or_acknowledge_loss(
    flight, action, lost
):
    f = flight
    c = f["control"]
    f["select"]()
    f["engage"]()
    f["now"][0] = 0.2
    f["observe"](detections=OTHER_PERSON if lost else None)
    c.tick(f["link"], 0.2)
    phase, revision = c.framing.phase, c.framing.revision
    deadline = c.framing.state(0.2)["takeover_remaining_s"]
    response = f["client"].post(
        "/api/control/action",
        json={"token": f["token"], "action": action},
        headers=ORIGIN,
    )
    assert response.status_code == 409
    assert c.framing.phase == phase and c.framing.revision == revision
    assert c.framing.state(0.2)["takeover_remaining_s"] == deadline


def test_old_displayed_frame_is_accepted_only_with_recent_history_and_current_target(
    flight,
):
    f = flight
    old_seq = f["session"].video.latest(0.05).sequence
    f["now"][0] = 0.25
    f["observe"]()
    r = f["request"](
        "select",
        run_id=f["session"].run_id,
        video_id=f["session"].video_source_id,
        frame_sequence=old_seq,
        track_id=1,
    )
    assert r.status_code == 200
    f["now"][0] = 0.85
    f["observe"]()
    # Keep owner alive so the rejected condition is the old displayed image.
    f["control"]._input_at = 0.85
    r = f["request"](
        "select",
        run_id=f["session"].run_id,
        video_id=f["session"].video_source_id,
        frame_sequence=old_seq,
        track_id=1,
    )
    assert r.status_code == 409 and "displayed detection expired" in r.json()["detail"]


def test_framing_requires_same_origin_and_owner_and_rejects_extra_data(flight):
    f = flight
    body = dict(token=f["token"], operation="stop", intent=1)
    assert f["client"].post("/api/control/framing", json=body).status_code == 403
    assert (
        f["client"]
        .post("/api/control/framing", json={**body, "token": "other"}, headers=ORIGIN)
        .status_code
        == 409
    )
    assert (
        f["client"]
        .post(
            "/api/control/framing", json={**body, "box": [0, 0, 1, 1]}, headers=ORIGIN
        )
        .status_code
        == 422
    )


def test_frame_provider_failure_keeps_session_control_servicing_alive(flight):
    f = flight
    c = f["control"]
    f["select"]()
    f["engage"]()

    class FailedVision:
        def tick(self, session):
            pass

        def frame(self, session):
            raise RuntimeError("provider failed")

    f["session"].vision = FailedVision()
    f["link"].poll = lambda now: []
    f["link"].report = lambda now: SimpleNamespace(
        closed=False, last_error="", rx_bytes=0, bad_bytes=0
    )
    f["now"][0] = 0.2
    f["session"].tick()
    assert c.framing.phase == "takeover"
    assert f["link"].messages("MANUAL_CONTROL")[-1].x == 0
    c.input(f["token"], 2, {**ZERO, "yaw": -1}, link=f["link"], now=0.21)
    f["now"][0] = 0.3
    f["session"].tick()
    assert f["link"].messages("MANUAL_CONTROL")[-1].r == -300
    assert c._token is not None


@pytest.mark.parametrize("first_bad", [[], [
    {"track_id": 1, "box": [0.5, 0.3, 0.1, 0.2], "confidence": .4119},
]])
def test_four_hundred_ms_detection_gap_neutralizes_wire_then_resumes_only_same_target(flight, first_bad):
    f = flight
    c = f['control']
    f['select']()
    f['engage']()
    f['now'][0] = .2
    f['observe']()
    c.tick(f['link'], .2)
    assert f['link'].messages('MANUAL_CONTROL')[-1].r > 0
    reference = c.framing.state(.2)['reference_height']

    f['now'][0] = .3
    f['observe'](detections=first_bad)
    c.tick(f['link'], .3)
    state = c.framing.state(.3)
    assert state['active'] and state['paused']
    assert state['height'] is None and state['error_x'] is None
    wire = f['link'].messages('MANUAL_CONTROL')[-1]
    assert (wire.x, wire.y, wire.z, wire.r) == (0, 0, 500, 0)
    assert f['request']('closer').status_code == 409
    c.input(f['token'], 2, ZERO, link=f['link'], now=.35)

    f['now'][0] = .5
    f['observe'](detections=[])
    c.tick(f['link'], .5)
    c.tick(f['link'], .69)
    wire = f['link'].messages('MANUAL_CONTROL')[-1]
    assert (wire.x, wire.y, wire.z, wire.r) == (0, 0, 500, 0)
    assert c.framing.state(.69)['paused']
    f['now'][0] = .7
    f['observe'](detections=[{
        'track_id': 1, 'box': [0.5, 0.3, 0.1, 0.2], 'confidence': .5,
    }])
    assert c.framing.axes(.7) == ZERO
    c.tick(f['link'], .7)
    state = c.framing.state(.7)
    assert state['active'] and not state['paused']
    assert state['reference_height'] == reference
    assert state['axes']['yaw'] > 0
    c.tick(f['link'], .751)  # next ordinary 20 Hz manual-command boundary
    assert f['link'].messages('MANUAL_CONTROL')[-1].r > 0


def test_nested_ambiguity_preserves_http_boxes_and_neutral_wire_until_second_clean_image(flight):
    import json
    f, c = flight, flight['control']
    f['select']()
    f['engage']()
    f['now'][0] = .2
    f['observe']()
    c.tick(f['link'], .2)
    assert f['link'].messages('MANUAL_CONTROL')[-1].r > 0
    nested = [
        {'track_id': 1, 'box': [.5, .3, .1, .2], 'confidence': .9},
        {'track_id': 2, 'box': [.52, .32, .04, .12], 'confidence': .4},
    ]
    f['now'][0] = .3
    f['observe'](detections=nested)
    c.tick(f['link'], .3)
    response = f['client'].get('/api/vision/frame.jpg')
    assert response.status_code == 200
    assert json.loads(response.headers['x-vision-result'])['detections'] == nested
    c.input(f['token'], 2, ZERO, link=f['link'], now=.35)
    f['now'][0] = .5
    f['observe']()
    for at in (.5, .56, .61):
        c.tick(f['link'], at)
        wire = f['link'].messages('MANUAL_CONTROL')[-1]
        assert (wire.x, wire.y, wire.z, wire.r) == (0, 0, 500, 0)
        assert c.framing.state(at)['paused']
    f['now'][0] = .7
    f['observe']()
    c.tick(f['link'], .7)
    assert c.framing.phase == 'active' and not c.framing.state(.7)['paused']
    c.tick(f['link'], .751)
    assert f['link'].messages('MANUAL_CONTROL')[-1].r > 0


@pytest.mark.parametrize('manual_action', ['direction', 'stop'])
def test_manual_takeover_between_ambiguity_confirmation_frames_cannot_auto_resume(flight, manual_action):
    f, c = flight, flight['control']
    f['select']()
    f['engage']()
    f['now'][0] = .3
    f['observe'](detections=[
        {'track_id': 1, 'box': [.5, .3, .1, .2], 'confidence': .9},
        {'track_id': 2, 'box': [.52, .32, .04, .12], 'confidence': .4},
    ])
    c.tick(f['link'], .3)
    c.input(f['token'], 2, ZERO, link=f['link'], now=.35)
    f['now'][0] = .5
    f['observe']()
    c.tick(f['link'], .5)
    assert c.framing.state(.5)['paused']
    f['now'][0] = .6
    if manual_action == 'direction':
        c.input(f['token'], 3, {**ZERO, 'yaw': -1}, link=f['link'], now=.6)
    else:
        assert f['request']('stop').status_code == 200
    c.tick(f['link'], .61)
    assert f['link'].messages('MANUAL_CONTROL')[-1].r == (-300 if manual_action == 'direction' else 0)
    f['now'][0] = .7
    f['observe']()
    c.tick(f['link'], .7)
    assert c.framing.phase != 'active' and not c.framing.state(.7)['paused']
    assert not c.framing.takeover_due(10.)


def test_second_ambiguity_confirmation_at_absolute_deadline_still_neutralizes_wire(flight):
    f, c = flight, flight['control']
    f['select']()
    f['engage']()
    f['now'][0] = .3
    nested = [
        {'track_id': 1, 'box': [.5, .3, .1, .2], 'confidence': .9},
        {'track_id': 2, 'box': [.52, .32, .04, .12], 'confidence': .4},
    ]
    f['observe'](detections=nested)
    c.tick(f['link'], .3)
    deadline = c.framing._pause_deadline
    c.input(f['token'], 2, ZERO, link=f['link'], now=.35)
    f['now'][0] = .7
    f['observe']()
    c.tick(f['link'], .7)
    c.input(f['token'], 3, ZERO, link=f['link'], now=.71)
    f['now'][0] = deadline
    f['observe']()
    c.tick(f['link'], deadline)
    state = c.framing.state(deadline)
    assert state['phase'] == 'takeover' and state['takeover_remaining_s'] == 2.
    assert state['last_loss']['detections'] == nested
    wire = f['link'].messages('MANUAL_CONTROL')[-1]
    assert (wire.x, wire.y, wire.z, wire.r) == (0, 0, 500, 0)


@pytest.mark.parametrize("manual_action", ["direction", "stop"])
def test_manual_action_wins_during_the_extended_neutral_pause(flight, manual_action):
    f, c = flight, flight['control']
    f['select']()
    f['engage']()
    f['now'][0] = .3
    f['observe'](detections=[])
    c.tick(f['link'], .3)
    c.input(f['token'], 2, ZERO, link=f['link'], now=.35)
    f['now'][0] = .5
    f['observe'](detections=[])
    c.tick(f['link'], .5)
    f['now'][0] = .7  # inside the new pause, beyond the old 350 ms window
    if manual_action == 'direction':
        c.input(f['token'], 3, {**ZERO, 'yaw': -1}, link=f['link'], now=.7)
    else:
        assert f['request']('stop').status_code == 200
    c.tick(f['link'], .71)
    assert not c.framing.state(.71)['active']
    assert not c.framing.state(.71)['paused']
    assert f['link'].messages('MANUAL_CONTROL')[-1].r == (-300 if manual_action == 'direction' else 0)
    f['now'][0] = .8
    f['observe']()
    c.tick(f['link'], .8)
    assert not c.framing.state(.8)['active']
    assert not c.framing.takeover_due(10.)
    assert not any(msg.command == 176 and msg.param2 == 9
                   for msg in f['link'].messages('COMMAND_LONG'))


def expire_detection_pause(f):
    """Keep the image and browser alive while the first detection gap expires."""
    c = f['control']
    f['now'][0] = .2
    bad_sequence = f['observe'](detections=[])
    c.tick(f['link'], .2)
    for seq, at in enumerate((.3, .5, .7), 2):
        f['now'][0] = at
        f['observe'](detections=[])
        c.input(f['token'], seq, ZERO, link=f['link'], now=at)
        c.tick(f['link'], at)
    takeover_at = .2 + DETECTION_PAUSE + .01
    f['now'][0] = takeover_at
    f['observe']()  # a late good frame cannot replace first-bad-image evidence
    c.tick(f['link'], takeover_at)
    return bad_sequence, takeover_at, seq


def test_dropout_deadline_wins_over_late_good_frame_and_keepalives(flight):
    f = flight
    c = f['control']
    f['select']()
    f['engage']()
    _, takeover_at, last_seq = expire_detection_pause(f)
    assert c.framing.phase == 'takeover'
    assert c.framing.axes(takeover_at) == ZERO
    for offset in range(1, 20):
        at = takeover_at + offset * .1
        heartbeat(c, at, armed=True)
        simstate(c, at)
        landed(c, at, 2)
        c.input(f['token'], last_seq + offset, ZERO, link=f['link'], now=at)
    c.tick(f['link'], takeover_at + 1.999)
    assert c.state(takeover_at + 1.999)['owned']
    c.tick(f['link'], takeover_at + 2.)
    assert c._token is None and c._command['action'] == 'land'


def test_late_expired_input_does_not_erase_target_loss_from_http_state(flight):
    f = flight
    c = f['control']
    assert f['select']().status_code == 200
    assert f['engage']().status_code == 200
    bad_sequence, takeover_at, last_seq = expire_detection_pause(f)
    for offset in range(1, 20):
        at = takeover_at + offset * .1
        heartbeat(c, at, armed=True)
        simstate(c, at)
        landed(c, at, 2)
        c.input(f['token'], last_seq + offset, ZERO, link=f['link'], now=at)
    expired_at = takeover_at + 2.01
    f['now'][0] = expired_at
    response = f['client'].post('/api/control/input', json={
        'token': f['token'], 'seq': last_seq + 20, 'axes': ZERO, 'throttle': 0,
    }, headers=ORIGIN)
    assert response.status_code == 409
    before = f['client'].get('/api/state').json()['control']
    interruption = before['interruption']
    assert not before['owned'] and before['command']['action'] == 'land'
    assert interruption['at'] == pytest.approx(expired_at)
    assert interruption['lease_started_at'] == before['lease_started_at'] == 0.
    assert 'manual takeover' in interruption['reason']
    loss = interruption['framing_loss']
    assert loss['at'] == pytest.approx(takeover_at)
    assert loss['evidence_at'] == pytest.approx(.2)
    assert loss['sequence'] == bad_sequence and loss['detections'] == []
    assert loss['target_id'] == 1
    f['now'][0] = 3.
    heartbeat(c, 3., armed=False, mode=9)
    landed(c, 3.)
    after = f['client'].get('/api/state').json()['control']
    assert after['framing']['reason'] == 'Drone disarmed; framing stopped'
    assert after['interruption'] == interruption
    assert after['framing']['last_loss'] == loss
