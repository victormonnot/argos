"""HTTP integration: manual control is explicit, source-bound and opt-in."""
from collections import deque

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
mav = pytest.importorskip("pymavlink.dialects.v20.ardupilotmega")
from fastapi.testclient import TestClient

from argos.backends.mavlink import MavlinkLink, SequenceScope
from argos.console.app import create_app
from argos.console.config import ConsoleConfig
from argos.console.control import REQUIRED_PARAMETERS
from argos.console.session import ConsoleSession

ORIGIN = {"origin": "http://testserver"}


class Wire:
    datagram = False

    def __init__(self):
        self.incoming = deque()
        self.outgoing = []

    def read(self):
        return self.incoming.popleft() if self.incoming else b""

    def write(self, data):
        self.outgoing.append(data)
        return len(data)

    def close(self):
        pass

    def receive(self, message):
        self.incoming.append(message.pack(mav.MAVLink(None, srcSystem=1, srcComponent=1)))


@pytest.fixture
def flight(tmp_path):
    now, wire = [0.], Wire()
    config = ConsoleConfig(sim_control=True, environment="simulation",
                           mavlink_tcp=("127.0.0.1", 5860), sequence_scope=SequenceScope.CHANNEL,
                           recordings_dir=tmp_path)
    session = ConsoleSession(config, clock=lambda: now[0], link_factory=lambda:
                             MavlinkLink(wire, sequence_scope=SequenceScope.CHANNEL))
    wire.receive(mav.MAVLink_heartbeat_message(2, 3, 1, 2, 3, 3))
    wire.receive(mav.MAVLink_simstate_message(*([0.] * 9), 0, 0))
    session.start()
    client = TestClient(create_app(session=session), raise_server_exceptions=False)
    try:
        yield client, session, wire, now
    finally:
        client.close()
        session.close()


def test_control_flag_requires_loopback_simulation_and_is_not_a_browser_source_setting():
    for values in ({}, {"environment": "real", "mavlink_tcp": ("127.0.0.1", 5860)},
                   {"environment": "simulation", "mavlink_tcp": ("192.0.2.1", 5860)}):
        with pytest.raises(ValueError, match="loopback"):
            ConsoleConfig(sim_control=True, **values)
    assert "sim_control" not in ConsoleConfig().public()


@pytest.mark.parametrize("endpoint", [(), ("127.0.0.1",), [], "127.0.0.1:5860"])
def test_control_flag_validates_tcp_shape_before_accessing_its_host(endpoint):
    with pytest.raises(ValueError, match="tuple"):
        ConsoleConfig(sim_control=True, environment="simulation", mavlink_tcp=endpoint)


@pytest.mark.parametrize("path", ["claim", "input", "action"])
def test_flight_requests_require_local_origin_before_any_send(flight, path):
    client, session, wire, now = flight
    before = len(wire.outgoing)
    for headers in ({}, {"origin": "https://example.org"}):
        assert client.post(f"/api/control/{path}", json={}, headers=headers).status_code == 403
    assert len(wire.outgoing) == before
    assert not session.control.state(0)["owned"]


def test_claim_is_exclusive_token_is_not_public_and_source_edits_are_blocked(flight):
    client, session, wire, now = flight
    reply = client.post("/api/control/claim", json={}, headers=ORIGIN)
    assert reply.status_code == 200
    token = reply.json()["token"]
    assert token not in client.get("/api/state").text
    assert client.post("/api/control/claim", json={}, headers=ORIGIN).status_code == 409
    assert client.post("/api/sources", json=session.config.public(), headers=ORIGIN).status_code == 409
    assert client.post("/api/sources/mavlink/reconnect", json={}, headers=ORIGIN).status_code == 409
    body = {"token": token, "seq": 1, "axes": dict(forward=1, right=0, up=0, yaw=0)}
    assert client.post("/api/control/input", json=body, headers=ORIGIN).status_code == 200
    assert client.post("/api/control/input", json=body, headers=ORIGIN).status_code == 422
    assert client.post("/api/control/action", json={"token": "other", "action": "arm"}, headers=ORIGIN).status_code == 409
    assert client.post("/api/control/action", json={"token": token, "action": "release"}, headers=ORIGIN).status_code == 200
    assert not client.get("/api/state").json()["control"]["owned"]


def test_actual_wire_profile_and_arming_receipts_flow_through_session(flight):
    client, session, wire, now = flight
    token = client.post("/api/control/claim", json={}, headers=ORIGIN).json()["token"]
    for key, value in REQUIRED_PARAMETERS.items():
        wire.receive(mav.MAVLink_param_value_message(key.encode(), value, 9, len(REQUIRED_PARAMETERS), 0))
    now[0] = .1
    while wire.incoming:
        session.tick()
    assert client.get("/api/state").json()["control"]["profile"]["ready"]
    response = client.post("/api/control/action", json={"token": token, "action": "arm"}, headers=ORIGIN)
    assert response.status_code == 200
    assert response.json()["control"]["command"]["observed"] is False
    wire.receive(mav.MAVLink_command_ack_message(400, 0))
    wire.receive(mav.MAVLink_heartbeat_message(2, 3, 129, 2, 4, 3))
    now[0] = .2
    session.tick()
    state = client.get("/api/state").json()["control"]
    assert state["vehicle"]["armed"] and state["command"]["observed"]
    now[0] = 1.
    session.tick()
    state = client.get("/api/state").json()["control"]
    assert not state["owned"] and state["command"]["action"] == "land"


@pytest.mark.parametrize("path,body", [("claim", {"extra": 1}), ("input", []),
                                     ("action", {"token": "x"})])
def test_invalid_control_bodies_are_rejected(flight, path, body):
    client, session, wire, now = flight
    assert client.post(f"/api/control/{path}", json=body, headers=ORIGIN).status_code == 422
    assert not wire.outgoing


def arm_request(client, session, wire, now, *, observe=True):
    token = client.post("/api/control/claim", json={}, headers=ORIGIN).json()["token"]
    for key, value in REQUIRED_PARAMETERS.items():
        wire.receive(mav.MAVLink_param_value_message(key.encode(), value, 9, len(REQUIRED_PARAMETERS), 0))
    now[0] = .1
    while wire.incoming:
        session.tick()
    assert client.post("/api/control/action", json={"token": token, "action": "arm"},
                       headers=ORIGIN).status_code == 200
    if observe:
        wire.receive(mav.MAVLink_heartbeat_message(2, 3, 129, 2, 4, 3))
        now[0] = .2
        session.tick()
    return token


def test_slow_acquisition_expires_input_before_transmitting_stale_maneuver(flight, monkeypatch):
    client, session, wire, now = flight
    token = arm_request(client, session, wire, now)
    now[0] = .3
    body = {"token": token, "seq": 1, "axes": dict(forward=1, right=0, up=0, yaw=0)}
    assert client.post("/api/control/input", json=body, headers=ORIGIN).status_code == 200
    before = len(wire.outgoing)
    read = wire.read

    def stalled_read():
        now[0] += .8
        return read()

    monkeypatch.setattr(wire, "read", stalled_read)
    session.tick()
    state = session.state()["control"]
    assert not state["owned"] and state["phase"] == "expired"
    decoder = mav.MAVLink(None)
    messages = [message for data in wire.outgoing[before:]
                for message in decoder.parse_buffer(data) or []]
    assert any(message.get_type() == "COMMAND_LONG" and message.param2 == 9 for message in messages)
    assert all(message.x == 0 for message in messages if message.get_type() == "MANUAL_CONTROL")
    assert state["command"]["sent_at"] == now[0]


@pytest.mark.parametrize("observe_arm", [False, True])
def test_same_source_reconnect_recovers_passively_without_erasing_flight_uncertainty(flight, observe_arm):
    client, session, wire, now = flight
    token = arm_request(client, session, wire, now, observe=observe_arm)
    previous_control = session.control
    session.link.close()
    now[0] = .3
    session.tick()
    assert not session.control.state(now[0])["owned"]
    assert client.post("/api/sources", json=session.config.public(), headers=ORIGIN).status_code == 409

    replacement = Wire()
    replacement.receive(mav.MAVLink_heartbeat_message(2, 3, 129 if observe_arm else 1, 2, 4, 3))
    replacement.receive(mav.MAVLink_simstate_message(*([0.] * 9), 0, 0))
    session._link_factory = lambda: MavlinkLink(replacement, sequence_scope=SequenceScope.CHANNEL)
    response = client.post("/api/sources/mavlink/reconnect", json={}, headers=ORIGIN)
    assert response.status_code == 200
    assert session.control is previous_control
    assert not replacement.outgoing
    assert client.post("/api/control/claim", json={}, headers=ORIGIN).status_code == 409
    assert client.post("/api/sources", json=session.config.public(), headers=ORIGIN).status_code == 409
    assert client.post("/api/control/action", json={"token": token, "action": "arm"},
                       headers=ORIGIN).status_code == 409

    # Fresh disarmed/landed receipts resolve a ground-disarmed GCS fallback, even
    # if its mode stays AltHold. A new explicit claim is still required to send.
    replacement.receive(mav.MAVLink_heartbeat_message(2, 3, 1, 2, 4, 3))
    replacement.receive(mav.MAVLink_extended_sys_state_message(0, 1))
    now[0] = .4
    session.tick()
    assert not replacement.outgoing
    response = client.post("/api/control/claim", json={}, headers=ORIGIN)
    assert response.status_code == 200 and response.json()["token"] != token
