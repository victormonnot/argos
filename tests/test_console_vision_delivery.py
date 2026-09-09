"""Deliver completed image measurements before send-time safety decisions."""
from pathlib import Path
from types import SimpleNamespace
import asyncio

import pytest

from argos.console.vision import VisionService
from test_console_flight_control import ZERO
from test_console_framing_api import flight
from test_console_vision import Context, complete, ready


@pytest.fixture
def queued_flight(flight):
    """Real service queues and lifecycle, with only the DNN process replaced."""
    f = flight
    session, now, link = f["session"], f["now"], f["link"]
    vision = VisionService(Path("model.onnx"), wall_clock=lambda: now[0],
                           process_context=Context())
    session.vision = vision
    vision.start()
    identifier, _ = ready(vision, session)
    complete(vision, session, identifier,
             detections=[{"box": [.5, .3, .1, .2], "confidence": .9}],
             width=2, height=2)
    session._observe_vision()
    assert f["select"]().status_code == 200
    assert f["engage"]().status_code == 200
    link.poll = lambda at: []
    link.report = lambda at: SimpleNamespace(closed=False, last_error="", rx_bytes=0, bad_bytes=0)
    link.close = lambda: None

    # Submit the next real camera receipt without completing its worker job.
    now[0] = .3
    session.video.accept_raw(width=2, height=2, step=6, pixel_format="RGB_INT8",
                             data=bytes(12), received_at=now[0])
    vision.tick(session)
    identifier, _ = vision._incoming.get_nowait()

    def deliver():
        vision._outgoing.put_nowait(("result", (identifier,
            {"width": 2, "height": 2, "inference_ms": 99.,
             "detections": [{"box": [.5, .3, .1, .2], "confidence": .9}]}, [None])))

    yield dict(f, vision=vision, deliver=deliver)
    asyncio.run(vision.aclose())


def test_result_ready_during_poll_reaches_guidance_before_send_time(queued_flight, monkeypatch):
    f = queued_flight
    session, vision, now, link = (f[k] for k in ("session", "vision", "now", "link"))
    refreshes = []
    tick = vision.tick
    def record_refresh(session):
        refreshes.append(now[0])
        tick(session)
    monkeypatch.setattr(vision, "tick", record_refresh)
    def poll(at):
        assert at == .3
        now[0] = .51  # The previous .05 image crossed its 450 ms boundary.
        f["deliver"]()
        return []
    link.poll = poll

    session.tick()

    assert refreshes == [.51]
    assert vision.frame(session).sample.received_at == .3
    assert f["control"].framing.phase == "active"
    assert f["control"].framing.last_loss is None
    message, sent_at = [(m, at) for m, at in link.sent if m.get_type() == "MANUAL_CONTROL"][-1]
    assert sent_at == .51 and message.r > 0 and message.z > 500
    assert vision._processed == 2 and vision._incoming.empty()


@pytest.mark.parametrize("operation", ["input", "action", "framing"])
def test_valid_control_mutation_observes_already_completed_result(queued_flight, operation):
    f = queued_flight
    f["now"][0] = .51
    f["deliver"]()
    if operation == "input":
        body = dict(token=f["token"], seq=2, axes=ZERO, mode_generation=0)
    elif operation == "action":
        body = dict(token=f["token"], action="land")
    else:
        body = dict(token=f["token"], operation="closer", intent=3, mode_generation=0)
    result = f["session"].control_request(operation, body)
    assert f["vision"]._processed == 2
    assert f["control"].framing.last_loss is None
    if operation != "action":
        assert result["control"]["framing"]["phase"] == "active"
        assert result["control"]["framing"]["frame_age_s"] == pytest.approx(.21)
    if operation == "framing":
        assert result["control"]["framing"]["reference_height"] > .2


def test_ready_result_does_not_renew_its_original_receipt_age(queued_flight):
    f = queued_flight
    # Keep only browser authority fresh, then deliver a genuinely stale image.
    f["control"].input(f["token"], 2, ZERO, link=f["link"], now=.3)
    f["now"][0] = .76
    f["deliver"]()
    f["session"].tick()
    assert f["vision"].frame(f["session"]).sample.received_at == .3
    framing = f["control"].framing
    assert framing.phase == "takeover" and framing.axes(.76) == ZERO
    assert framing.last_loss["reason"] == "Selected target image is stale"
    assert framing.last_loss["frame_age_s"] == pytest.approx(.46)
    message = f["link"].messages("MANUAL_CONTROL")[-1]
    assert (message.x, message.y, message.z, message.r) == (0, 0, 500, 0)


def test_new_queued_result_cannot_undo_an_already_latched_takeover(queued_flight):
    f = queued_flight
    f["now"][0] = .51
    f["control"].tick(f["link"], .51)
    framing = f["control"].framing
    assert framing.phase == "takeover"
    loss = framing.last_loss
    remaining = framing.state(.51)["takeover_remaining_s"]
    f["deliver"]()
    f["now"][0] = .52
    f["session"].control_request("input", dict(token=f["token"], seq=2, axes=ZERO))
    assert f["vision"]._processed == 2
    assert framing.phase == "takeover" and framing.axes(.52) == ZERO
    assert framing.last_loss == loss
    assert framing.state(.52)["takeover_remaining_s"] == pytest.approx(remaining - .01)


@pytest.mark.parametrize("through_poll", [False, True])
def test_fresh_queued_result_cannot_renew_an_expired_browser_lease(queued_flight, through_poll):
    f = queued_flight
    old_input_at = f["control"]._input_at
    def poll(at):
        f["now"][0] = .7
        f["deliver"]()
        return []
    if through_poll:
        f["link"].poll = poll
        f["session"].tick()
    else:
        poll(.3)
        with pytest.raises(RuntimeError):
            f["session"].control_request("input", dict(token=f["token"], seq=2, axes=ZERO))
    assert f["vision"]._processed == 2
    assert f["control"]._input_at == old_input_at
    state = f["control"].state(.7)
    assert not state["owned"] and state["phase"] == "expired"
    assert "Browser inputs expired" in state["interruption"]["reason"]
    assert state["interruption"]["framing_loss"] is None
    commands = [(m, at) for m, at in f["link"].sent if m.get_type() == "COMMAND_LONG"]
    message, sent_at = commands[-1]
    assert message.command == 176 and message.param2 == 9  # DO_SET_MODE: LAND
    assert sent_at == .7


def test_wrong_source_ready_result_stays_rejected_before_flight_evaluation(queued_flight):
    f = queued_flight
    f["now"][0] = .51
    f["session"].video_source_id = "replacement-camera"
    f["deliver"]()
    f["session"].tick()
    assert f["vision"].frame(f["session"]) is None
    assert f["vision"]._processed == 0
    assert f["control"].framing.phase == "takeover"
    assert f["control"].framing.axes(.51) == ZERO


def test_refresh_exception_clears_observation_and_keeps_manual_task_running(queued_flight, monkeypatch):
    f = queued_flight
    def fail(session):
        raise RuntimeError("broken optional worker provider")
    monkeypatch.setattr(f["vision"], "tick", fail)
    f["now"][0] = .4
    f["session"].tick()
    assert not f["session"]._error
    assert f["control"].framing.phase == "takeover"
    assert f["control"].framing.last_loss["sequence"] is None
    message = f["link"].messages("MANUAL_CONTROL")[-1]
    assert (message.x, message.y, message.z, message.r) == (0, 0, 500, 0)


def test_invalid_fields_do_not_consume_a_ready_result(queued_flight):
    f = queued_flight
    f["now"][0] = .51
    f["deliver"]()
    with pytest.raises(ValueError, match="Invalid command fields"):
        f["session"].control_request("input", dict(token=f["token"], seq=2, axes=ZERO, unknown=True))
    assert f["vision"]._processed == 1
    assert not f["vision"]._outgoing.empty()
    assert f["control"]._seq == 1 and f["control"]._input_at == .03


@pytest.mark.parametrize("refusal", ["token", "generation"])
def test_refresh_does_not_bypass_input_authority_or_mode_generation(queued_flight, refusal):
    f = queued_flight
    f["now"][0] = .51
    f["deliver"]()
    body = dict(token=f["token"], seq=2, axes=ZERO, mode_generation=0)
    if refusal == "token":
        body["token"] = "another-browser"
    else:
        f["control"]._mode_generation = 1
    with pytest.raises(RuntimeError):
        f["session"].control_request("input", body)
    assert f["vision"]._processed == 2
    assert f["control"].framing.phase == "active"
    assert f["control"]._seq == 1 and f["control"]._input_at == .03
    assert f["control"].state(.51)["owned"]
