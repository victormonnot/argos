"""Continuous vision selection: brief recovery, immutable age and bounded HTTP."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

import pytest

from argos.backends import yaw_stream_source as source
from argos.console.yaw_preview import YawPreview


def snapshot(*, at=10., received=9.9, sequence=42):
    return {
        "schema_version": 1, "at": at, "run_id": "run-one", "environment": "real",
        "reconnecting": None,
        "configuration": {"environment": "real", "video_source": "device",
                          "video_endpoint": "/dev/video3"},
        "video": {"source_id": "camera-one", "source": "device", "state": "recent",
                  "endpoint": "/dev/video3", "received_at": received,
                  "rx_age_s": at - received, "age_limit_s": 1.},
        "vision": {"configured": True, "state": "recent", "frame_age_s": at - received,
                   "age_limit_s": 1., "inference_ms": 30.},
        "yaw_preview": {"enabled": True, "phase": "tracking", "revision": 1,
                        "selection_epoch": 1, "target_id": 7, "run_id": "run-one",
                        "video_id": "camera-one", "frame_sequence": sequence,
                        "frame_received_at": received, "frame_age_s": at - received,
                        "frame_max_age_s": .45, "error_x": .4, "yaw": .1,
                        "yaw_limit": .125, "recovery_deadline_at": None},
    }


def pause(state, *, deadline=10.6):
    state["yaw_preview"].update(phase="paused", yaw=0., error_x=None,
                                revision=2, recovery_deadline_at=deadline)
    return state


def test_demand_is_immutable_and_charges_full_http_time():
    demand = source.YawValidator().validate(snapshot(), 100., 100.04)
    assert demand.valid and demand.value == 102 and demand.reason == "tracking"
    assert demand.deadline == pytest.approx(100.35)
    assert demand.selection_key == ("run-one", "camera-one", 7, 1, "/dev/video3")
    with pytest.raises(FrozenInstanceError):
        demand.value = 128


def test_centered_target_zero_is_valid_but_occlusion_zero_withdraws_authority():
    validator = source.YawValidator()
    centered = snapshot()
    centered["yaw_preview"].update(yaw=0., error_x=0.)
    active = validator.validate(centered, 100., 100.01)
    paused = validator.validate(pause(snapshot(at=10.1, received=10., sequence=43)),
                                100.1, 100.11)
    assert active.value == paused.value == 0
    assert active.valid and not paused.valid
    assert active.selection_key == paused.selection_key
    assert paused.reason == "target_paused" and paused.deadline == 100.11


@pytest.mark.parametrize("observe_pause", [True, False])
def test_short_same_target_gap_requires_two_distinct_recovery_images(observe_pause):
    validator = source.YawValidator()
    first = validator.validate(snapshot(), 100., 100.01)
    if observe_pause:
        validator.validate(pause(snapshot(at=10.1, received=10., sequence=43)), 100.1, 100.11)
    recovery = snapshot(at=10.2, received=10.1, sequence=44)
    recovery["yaw_preview"]["revision"] = 2
    one = validator.validate(recovery, 100.2, 100.21)
    assert not one.valid and one.reason == "target_recovering"
    repeated = deepcopy(recovery)
    repeated["at"] += .01
    repeated["yaw_preview"]["frame_age_s"] += .01
    two = validator.validate(repeated, 100.22, 100.23)
    assert not two.valid  # Polling the same image cannot confirm recovery.
    new = snapshot(at=10.3, received=10.2, sequence=45)
    new["yaw_preview"]["revision"] = 2
    recovered = validator.validate(new, 100.3, 100.31)
    assert recovered.valid and recovered.selection_key == first.selection_key


def test_repeated_image_never_renews_deadline_even_across_bad_reads():
    validator = source.YawValidator()
    first = validator.validate(snapshot(), 100., 100.01)
    bad = snapshot(at=10.02)
    bad["vision"]["configured"] = False
    with pytest.raises(source.PreviewError):
        validator.validate(bad, 100.1, 100.11)
    fresh_read = validator.validate(snapshot(at=10.03), 100.2, 100.21)
    assert fresh_read.deadline == first.deadline
    with pytest.raises(source.PreviewError, match="expired"):
        validator.validate(snapshot(at=10.04), 100.36, 100.37)


@pytest.mark.parametrize("kind", ["epoch", "target", "run", "camera", "endpoint"])
def test_identity_change_is_visible_to_host_rearm_gate(kind):
    validator = source.YawValidator()
    old = validator.validate(snapshot(), 100., 100.01)
    state = snapshot(at=10.1, received=10., sequence=43)
    preview = state["yaw_preview"]
    if kind == "epoch":
        preview["selection_epoch"] += 1
    elif kind == "target":
        preview["target_id"] += 1
    elif kind == "run":
        state["run_id"] = preview["run_id"] = "new-run"
    elif kind == "camera":
        state["video"]["source_id"] = preview["video_id"] = "new-camera"
    else:
        state["video"]["endpoint"] = state["configuration"]["video_endpoint"] = "/dev/video4"
    new = validator.validate(state, 100.1, 100.11)
    assert new.valid and new.selection_key != old.selection_key


@pytest.mark.parametrize("phase", ["stopped", "idle", "disabled"])
def test_terminal_loss_or_clear_discards_identity(phase):
    validator = source.YawValidator()
    validator.validate(snapshot(), 100., 100.01)
    state = snapshot(at=10.1, received=10., sequence=43)
    state["yaw_preview"].update(phase=phase, target_id=None, yaw=0., error_x=None,
                                selection_epoch=2)
    demand = validator.validate(state, 100.1, 100.11)
    assert not demand.valid and demand.value == 0 and demand.selection_key is None


@pytest.mark.parametrize("mutation", ["stale", "rewritten", "receipt", "clock", "epoch",
                                      "pause_value", "expired_pause", "pause_renewal"])
def test_invalid_evidence_never_returns_an_active_demand(mutation):
    validator = source.YawValidator()
    validator.validate(snapshot(), 100., 100.01)
    state = snapshot(at=10.1, received=10., sequence=43)
    if mutation == "stale":
        state = snapshot(at=10.5, received=9.9)
    elif mutation == "rewritten":
        state = snapshot(at=10.1)
        state["yaw_preview"]["yaw"] = -.1
    elif mutation == "receipt":
        state = snapshot(at=10.1, received=9.9, sequence=43)
    elif mutation == "clock":
        state["at"] = 10.
    elif mutation == "epoch":
        state["yaw_preview"]["selection_epoch"] = True
    elif mutation == "pause_value":
        pause(state)["yaw_preview"]["yaw"] = .1
    elif mutation == "expired_pause":
        pause(state, deadline=10.1)
    else:
        pause(state, deadline=11.)
    with pytest.raises(source.PreviewError):
        validator.validate(state, 100.1, 100.11)


def test_continuous_consumer_and_diagnostic_bench_keep_distinct_recovery_contracts():
    from argos.backends.vision_bench_source import PreviewValidator
    preview, streaming, bench = YawPreview(True), source.YawValidator(), PreviewValidator()
    identity = None
    for index in range(5):
        at = 10. + index * .1
        preview.observe({"run_id": "run-one", "video_id": "camera-one", "sequence": index + 1,
                         "received_at": at, "width": 640, "height": 480,
                         "detections": ([] if index == 1 else [{"track_id": 7,
                             "confidence": .9, "box": [.6, .2, .2, .5]}])}, now=at)
        if index == 0:
            preview.select(7, revision=0, now=at)
        state = snapshot(at=at, received=at, sequence=index + 1)
        state["yaw_preview"] = preview.state(at)
        demand = streaming.validate(state, 100. + index * .1, 100.01 + index * .1)
        if index == 0:
            identity = demand.selection_key
            bench.validate(state, 100., 100.01)
        else:
            with pytest.raises(source.PreviewError):
                bench.validate(state, 100. + index * .1, 100.01 + index * .1)
        assert demand.selection_key == identity
        assert demand.valid == (index in (0, 3, 4))


class Reply:
    status = 200

    def __init__(self, body):
        self.body = body

    def getheader(self, name, default=None):
        return {"Content-Type": "application/json", "Content-Length": str(len(self.body))}.get(name, default)

    def read(self, limit):
        return self.body[:limit]


def test_http_errors_do_not_latch_source_and_no_radio_is_opened(monkeypatch):
    responses = [OSError("unavailable"), Reply(json.dumps(snapshot()).encode())]
    calls = []

    class Connection:
        def __init__(self, host, port, *, timeout):
            calls.append((host, port, timeout))

        def request(self, method, path, *, headers):
            assert (method, path) == ("GET", "/api/vision/yaw-assist/state")

        def getresponse(self):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        def close(self):
            pass

    monkeypatch.setattr(source.http.client, "HTTPConnection", Connection)
    clock = iter([100., 100.1, 100.11, 100.12])
    reader = source.YawSource(clock=lambda: next(clock))
    with pytest.raises(source.PreviewError, match="unavailable"):
        reader.read()
    assert reader.read().valid
    assert calls == [("127.0.0.1", 8080, .15)] * 2
    reader.close()
    with pytest.raises(source.PreviewError, match="closed"):
        reader.read()


@pytest.mark.parametrize("body", [b'{"schema_version":1,"schema_version":1}',
                                   b'{"at":NaN}', b"x" * (source.MAX_BODY + 1)])
def test_http_rejects_duplicate_nonfinite_and_oversized_json(monkeypatch, body):
    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Reply(body)

        def close(self):
            pass

    monkeypatch.setattr(source.http.client, "HTTPConnection", Connection)
    with pytest.raises(source.PreviewError):
        source.YawSource(clock=lambda: 100.).read()


def fly_snapshot(**kwargs):
    state = snapshot(**kwargs)
    state["yaw_preview"].update(continuous=True, selection_id=7, recovery_max_gap_s=3.)
    return state


def test_three_second_recovery_preserves_original_identity_after_verified_reassociation():
    validator = source.YawValidator()
    first = validator.validate(fly_snapshot(), 100., 100.01)
    validator.validate(pause(fly_snapshot(at=11., received=10.9, sequence=43), deadline=12.9),
                       101., 101.01)
    returned = fly_snapshot(at=12., received=11.9, sequence=44)
    returned["yaw_preview"].update(target_id=81, revision=2)
    assert not validator.validate(returned, 102., 102.01).valid
    confirmed = fly_snapshot(at=12.1, received=12., sequence=45)
    confirmed["yaw_preview"].update(target_id=81, revision=2)
    demand = validator.validate(confirmed, 102.1, 102.11)
    assert demand.valid and demand.selection_key == first.selection_key


def test_reassociation_without_recovery_or_excessive_recovery_window_is_rejected():
    validator = source.YawValidator()
    validator.validate(fly_snapshot(), 100., 100.01)
    changed = fly_snapshot(at=10.1, received=10., sequence=43)
    changed["yaw_preview"]["target_id"] = 81
    with pytest.raises(source.PreviewError, match="reassociation"):
        validator.validate(changed, 100.1, 100.11)
    changed = pause(fly_snapshot(at=10.1, received=10., sequence=43), deadline=13.2)
    with pytest.raises(source.PreviewError, match="window"):
        validator.validate(changed, 100.1, 100.11)


@pytest.mark.parametrize("wrong", [None, "token", "camera"])
def test_radio_selection_http_is_bound_to_observed_context_and_echoed_transaction(monkeypatch, wrong):
    target = snapshot(at=10.1, received=10., sequence=43)
    target["yaw_preview"].update(selection_epoch=2, revision=2)
    response = {"request_id": "1234abcd:3", "state": target}
    if wrong == "token":
        response["request_id"] = "ffffffff:3"
    elif wrong == "camera":
        target["run_id"] = target["yaw_preview"]["run_id"] = "other-run"
    responses = [snapshot(), response]
    requests = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body=None, *, headers):
            requests.append((method, path, None if body is None else json.loads(body)))

        def getresponse(self):
            return Reply(json.dumps(responses.pop(0)).encode())

        def close(self):
            pass

    monkeypatch.setattr(source.http.client, "HTTPConnection", Connection)
    times = iter([100., 100.01, 100.02, 100.1, 100.11, 100.12])
    reader = source.YawSource(clock=lambda: next(times))
    with pytest.raises(source.PreviewError, match="Read the current"):
        reader.select_center("1234abcd:3")
    reader.read()
    if wrong:
        with pytest.raises(source.PreviewError):
            reader.select_center("1234abcd:3")
    else:
        selected = reader.select_center("1234abcd:3")
        assert selected.valid and selected.selection_key[3] == 2
    assert requests[-1] == ("POST", "/api/vision/yaw-assist/select-center", {
        "request_id": "1234abcd:3", "run_id": "run-one", "video_id": "camera-one"})
    assert not responses
