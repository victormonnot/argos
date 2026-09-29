"""Association recovery must not transfer selection to an uncertain identity."""
from argos.console.yaw_preview import RECOVERY_MAX_GAP, YawPreview
from argos.perception.image_tracks import TRACK_TTL, ImageTracker


def test_short_empty_frame_retains_selection_without_inventing_a_detection():
    tracker = ImageTracker()
    preview = YawPreview(True)
    assert RECOVERY_MAX_GAP <= TRACK_TTL
    person = {"box": [.6, .2, .2, .5], "confidence": .9}

    def observe(sequence, at, detections):
        associated = tracker.update(detections, at)
        preview.observe({"run_id": "run", "video_id": "camera",
                         "sequence": sequence, "received_at": at,
                         "width": 640, "height": 480,
                         "detections": associated}, now=at)
        return associated

    original = observe(1, 1., [person])[0]["track_id"]
    preview.select(original, revision=0, now=1.)
    assert observe(2, 1.2, []) == []
    state = preview.state(1.2)
    assert state["phase"] == "paused" and state["target_id"] == original
    assert state["yaw"] == 0 and state["error_x"] is None
    recovered = observe(3, 1.5, [person])[0]["track_id"]
    state = preview.state(1.5)
    assert state["phase"] == "tracking" and recovered == original
    assert state["revision"] == 2 and state["yaw"] > 0


def test_transient_ambiguity_recovers_after_explicit_preview_reselection():
    tracker = ImageTracker()
    preview = YawPreview(True)

    def observe(sequence, x):
        at = 1. + sequence * .125
        detections = tracker.update(
            [{"box": [x, .2, .2, .5], "confidence": .9}], at)
        preview.observe({"run_id": "run", "video_id": "camera",
                         "sequence": sequence, "received_at": at,
                         "width": 640, "height": 480,
                         "detections": detections}, now=at)
        return at, detections[0]["track_id"]

    at, original = observe(1, .2)
    preview.select(original, revision=preview.revision, now=at)
    at, displaced = observe(2, .4)
    assert displaced != original
    selected_revision = preview.revision
    assert preview.state(at)["phase"] == "paused"

    # The midpoint matches both histories: neither uncertain identity survives.
    at, recovered = observe(3, .3)
    assert recovered not in (original, displaced)
    for sequence in range(4, 12):
        at, identity = observe(sequence, .3)
        assert identity == recovered
        state = preview.state(at)
        assert state["yaw"] == 0.
        if at < 1.125 + RECOVERY_MAX_GAP:
            assert state["phase"] == "paused"
            assert state["revision"] == selected_revision
            assert state["target_id"] == original
        else:
            assert state["phase"] == "stopped"
            assert state["revision"] == selected_revision + 1
            assert state["target_id"] is None

    preview.select(recovered, revision=preview.revision, now=at)
    for sequence in range(12, 28):
        at, identity = observe(sequence, .3)
        state = preview.state(at)
        assert state["phase"] == "tracking"
        assert state["target_id"] == identity == recovered
        assert state["yaw"] < 0.
