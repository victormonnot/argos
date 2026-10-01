from copy import deepcopy
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from argos import filming


def status(state="recording", *, camera_state=None, writer_stopped=None, losses=0):
    closed = state == "complete" if writer_stopped is None else writer_stopped
    return {"state": state, "id": "take-id", "events": 0,
            "visual": {"state": state, "frames": 8, "dropped": 0},
            "filming": {"state": state, "id": "take-id", "label": "battery 2", "directory": "/recordings/take.flight",
                        "events": 50, "dropped_events": losses, "failed_events": 0, "pilot_samples": 20,
                        "writer_stopped": closed, "complete": closed and not losses,
                        "camera": {"state": camera_state or state, "written_frames": 30, "dropped_frames": losses,
                                   "writer_stopped": closed, "complete": closed and not losses}}}


class Client:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def request(self, path, body=None, *, timeout=filming.REQUEST_TIMEOUT):
        self.calls.append((path, body, timeout))
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def state(recording, **extras):
    return {"recording": recording, **extras}


def test_start_sets_explicit_capture_options_and_refreshes_only_status():
    client = Client([status(), state(status(), yaw_assist={"connected": True})])
    result, radio, warning = filming.perform("start", client, label=" battery 2 ")
    assert client.calls == [("/api/recordings/start", {"include_visual": True, "include_filming": True,
                                                     "label": "battery 2"}, 2.),
                            ("/api/state", None, 2.)]
    assert result["state"] == "recording"
    assert radio == {"connected": True}
    assert warning == ""


def test_status_is_read_only():
    client = Client([state(status())])
    filming.perform("status", client)
    assert client.calls == [("/api/state", None, 2.)]


def test_stop_waits_for_camera_after_native_jsonl_has_closed():
    pending = status("complete", camera_state="finalizing", writer_stopped=False)
    client = Client([pending, state(pending), state(status("complete"))])
    now = [0.]
    result, _, warning = filming.perform("stop", client, clock=lambda: now[0],
                                        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert client.calls[0] == ("/api/recordings/stop", {}, 2.)
    assert all(call[0] == "/api/state" for call in client.calls[1:])
    assert not filming.finalization_pending(result)
    assert warning == ""


def test_finalization_wait_is_bounded_and_never_claims_completion(capsys):
    pending = status("complete", camera_state="finalizing", writer_stopped=False)
    client = Client([pending] + [state(pending)] * 30)
    now = [0.]
    result, _, _ = filming.perform("stop", client, clock=lambda: now[0],
                                   sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert now[0] == 5.
    assert len(client.calls) <= 27
    assert all(0 < timeout <= 2. for _, _, timeout in client.calls)
    filming.print_status(result, action="stop")
    output = capsys.readouterr().out
    assert "Finalizing:" in output
    assert "Recording finalized." not in output
    assert "writer_stopped=False" in output


def test_finalized_with_losses_remains_visible(capsys):
    complete = status("complete", losses=3)
    assert not filming.finalization_pending(complete)
    filming.print_status(complete)
    output = capsys.readouterr().out
    assert "Finalized with losses" in output
    assert "dropped=3" in output
    assert "Pocket samples=20" in output
    assert "/recordings/take.flight" in output


def test_camera_cadence_is_observed_and_missing_pilot_evidence_is_not_complete(capsys):
    complete = status("complete")
    complete["filming"].update(complete=False, pilot_required=True, pilot_samples=0)
    complete["filming"]["camera"]["observed_fps"] = 29.9
    filming.print_status(complete)
    output = capsys.readouterr().out
    assert "observed_fps=29.9" in output
    assert "missing evidence" in output and "Pocket samples=0" in output


def test_recording_identity_change_does_not_adopt_another_take():
    pending = status("complete", writer_stopped=False)
    replacement = status()
    replacement["id"] = "different"
    client = Client([pending, state(replacement, yaw_assist={"connected": True})])
    result, radio, warning = filming.perform("stop", client)
    assert result["id"] == "take-id"
    assert radio is None
    assert "unconfirmed" in warning
    assert len(client.calls) == 2


def test_refresh_failure_preserves_acknowledged_start_and_does_not_retry_post():
    client = Client([status(), filming.ConsoleError("lost connection")])
    result, _, warning = filming.perform("start", client)
    assert result["state"] == "recording"
    assert "Start was acknowledged" in warning
    assert [call[0] for call in client.calls].count("/api/recordings/start") == 1


@pytest.mark.parametrize("value", [None, [], {}, {"state": "surprise"}, {"state": "complete", "filming": {}}])
def test_malformed_state_cannot_claim_completion(value):
    client = Client([state(value)])
    with pytest.raises(filming.ConsoleError):
        filming.perform("status", client)


def test_missing_camera_writer_flag_is_not_complete():
    value = status("complete")
    del value["filming"]["camera"]["writer_stopped"]
    assert filming.finalization_pending(value)


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, **kwargs):
        self.calls.append((request, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return Response(self.response)


def test_http_uses_localhost_matching_origin_json_and_bounded_timeout():
    client = filming.ConsoleClient(8081)
    client.opener = Opener(b'{"state":"recording"}')
    client.request("/api/recordings/start", {"include_visual": True, "include_filming": True}, timeout=99)
    request, options = client.opener.calls[0]
    assert request.full_url == "http://127.0.0.1:8081/api/recordings/start"
    assert request.get_header("Origin") == "http://127.0.0.1:8081"
    assert request.get_header("Content-type") == "application/json"
    assert request.method == "POST"
    assert json.loads(request.data) == {"include_visual": True, "include_filming": True}
    assert options["timeout"] == 2.


def test_client_disables_proxies_and_redirects(monkeypatch):
    handlers = []
    monkeypatch.setattr(filming, "build_opener", lambda *values: handlers.extend(values))
    filming.ConsoleClient()
    assert handlers[0].proxies == {}
    assert handlers[1].redirect_request(None, None, 302, "redirect", {}, "https://external.invalid") is None


@pytest.mark.parametrize("payload", [b"[]", b"not JSON", b"x" * (filming.MAX_RESPONSE_BYTES + 1)])
def test_http_rejects_malformed_or_oversized_response(payload):
    client = filming.ConsoleClient()
    client.opener = Opener(payload)
    with pytest.raises(filming.ConsoleError):
        client.request("/api/state")


def test_http_error_and_uncertain_mutation_outcome_are_explicit():
    client = filming.ConsoleClient()
    client.opener = Opener(HTTPError(client.origin, 409, "Conflict", {}, io.BytesIO(b'{"detail":"Already recording"}')))
    with pytest.raises(filming.ConsoleError, match="Already recording"):
        client.request("/api/recordings/start", {})
    client.opener = Opener(URLError("disconnected"))
    with pytest.raises(filming.ConsoleError, match="outcome is unknown"):
        client.request("/api/recordings/stop", {})
    assert len(client.opener.calls) == 1


def test_cli_reports_camera_error_and_returns_nonzero(monkeypatch, capsys):
    failed = status("complete", camera_state="error")
    failed["filming"]["camera"]["error"] = "disk full"
    monkeypatch.setattr(filming, "ConsoleClient", lambda port: Client([state(failed)]))
    assert filming.main(["status"]) == 1
    output = capsys.readouterr().out
    assert "Recording error" in output and "disk full" in output


def test_cli_rejects_bad_label_and_port_before_a_request(monkeypatch):
    with pytest.raises(ValueError, match="Port"):
        filming.ConsoleClient(0)
    client = Client([])
    with pytest.raises(ValueError, match="label"):
        filming.perform("start", client, label="bad\nlabel")
    assert client.calls == []


def test_radio_summary_uses_fresh_lua_axis_states_without_claiming_fc(capsys):
    radio = {"connected": True, "assistance": {"fresh": True,
             "yaw": {"state": "assisted"}, "pitch": {"state": "manual"}}}
    filming.print_status(status(), radio)
    output = capsys.readouterr().out
    assert "fresh Lua sample=True" in output
    assert "yaw=assisted, pitch=manual" in output
