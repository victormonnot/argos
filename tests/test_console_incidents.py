from argos.console.incidents import ReceptionIncidents, LABELS


def video(state="recent"):
    return {"state": state, "detail": "Camera de test"}


def telemetry(state="receiving", **states):
    return {"state": state, "detail": "Component sélectionné", **{
        name: {"state": states.get(name, "recent"),
               "fields": None if states.get(name) == "absent" else {}}
        for name in LABELS}}


def test_short_gaps_do_not_flood_notices_or_delay_cache_freshness():
    incidents = ReceptionIncidents()
    incidents.update(0., video(), telemetry())
    for i in range(10):
        assert incidents.update(i + .1, video(), telemetry(attitude="stale")) == []
        assert incidents.update(i + .4, video(), telemetry()) == []
    assert incidents.snapshot()["active"] == []


def test_loss_of_all_measures_is_one_incident_with_one_recovery():
    incidents = ReceptionIncidents()
    incidents.update(0., video(), telemetry())
    missing = telemetry("stale", **{name: "stale" for name in LABELS})
    assert incidents.update(1., video(), missing) == []
    events = incidents.update(2., video(), missing)
    assert len(events) == 1
    issue, = incidents.snapshot()["active"]
    assert issue["affected"] == list(LABELS)
    assert issue["since"] == 1.
    assert incidents.update(5., video(), missing) == []
    assert incidents.update(6., video(), telemetry()) == []
    assert incidents.snapshot()["active"][0]["state"] == "recovering"
    assert len(incidents.update(7., video(), telemetry())) == 1
    assert incidents.snapshot() == {"active": [], "last_recovery": {"source": "mavlink", "at": 7., "duration_s": 5.}}
    assert incidents.update(8., video(), telemetry()) == []


def test_partial_loss_keeps_same_incident_when_transport_breaks():
    incidents = ReceptionIncidents()
    incidents.update(0., video(), telemetry())
    incidents.update(1., video(), telemetry(attitude="stale"))
    incidents.update(2., video(), telemetry(attitude="stale"))
    issue, = incidents.snapshot()["active"]
    assert issue["title"] == "Partial telemetry"
    assert issue["affected"] == ["attitude"]
    assert incidents.update(3., video(), telemetry("error")) == []
    changed, = incidents.snapshot()["active"]
    assert changed["id"] == issue["id"]
    assert changed["title"] == "MAVLink reception interrupted"


def test_never_received_optional_measure_does_not_generate_incident():
    incidents = ReceptionIncidents()
    for now in (0., 1., 4., 20.):
        assert incidents.update(now, video("unconfigured"), telemetry(battery="absent")) == []
    assert incidents.snapshot()["active"] == []


def test_initial_wait_grace_and_explicit_errors():
    incidents = ReceptionIncidents()
    waiting = telemetry("waiting", **{name: "absent" for name in LABELS})
    assert incidents.update(0., video("waiting"), waiting) == []
    assert incidents.update(2.9, video("waiting"), waiting) == []
    assert len(incidents.update(3., video("waiting"), waiting)) == 2
    other = ReceptionIncidents()
    assert len(other.update(0., video("error"), telemetry("error"))) == 2


def test_reopen_is_not_recovery_and_remembers_previously_available_measures():
    incidents = ReceptionIncidents()
    incidents.update(0., video(), telemetry())
    incidents.update(1., video("error"), telemetry("error"))
    incidents.update(2., video("reconnecting"), telemetry("reconnecting"))
    absent = {name: "absent" for name in LABELS}
    incidents.update(5., video("waiting"), telemetry("waiting", **absent))
    incidents.update(8., video(), telemetry(battery="absent"))
    incidents.update(9., video(), telemetry(battery="absent"))
    issue, = incidents.snapshot()["active"]
    assert issue["source"] == "mavlink"
    assert issue["affected"] == ["battery"]
    incidents.update(10., video(), telemetry())
    assert len(incidents.update(11., video(), telemetry())) == 1
    assert incidents.snapshot()["active"] == []


def test_healthy_reopen_does_not_create_failure_or_false_recovery():
    incidents = ReceptionIncidents()
    incidents.update(0., video(), telemetry())
    assert incidents.update(1., video("reconnecting"), telemetry("reconnecting")) == []
    assert incidents.update(2., video(), telemetry()) == []
    assert incidents.snapshot() == {"active": [], "last_recovery": None}


def test_recovery_must_remain_stable_and_snapshot_is_defensive():
    incidents = ReceptionIncidents()
    incidents.update(0., video("error"), telemetry())
    incidents.update(1., video(), telemetry())
    incidents.update(1.5, video("stale"), telemetry())
    incidents.update(2., video(), telemetry())
    snapshot = incidents.snapshot()
    snapshot["active"][0]["affected"].clear()
    assert incidents.snapshot()["active"][0]["affected"] == ["video"]
    assert incidents.update(2.9, video(), telemetry()) == []
    assert len(incidents.update(3., video(), telemetry())) == 1
