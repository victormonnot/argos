"""Association recovery must allow reselection without reviving a lost target."""
from argos.console.yaw_preview import YawPreview
from argos.perception.image_tracks import ImageTracker


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
    stopped_revision = preview.revision
    assert preview.state(at)["phase"] == "stopped"

    # The midpoint matches both histories: neither uncertain identity survives.
    at, recovered = observe(3, .3)
    assert recovered not in (original, displaced)
    for sequence in range(4, 12):
        at, identity = observe(sequence, .3)
        assert identity == recovered
        state = preview.state(at)
        assert state["phase"] == "stopped"
        assert state["revision"] == stopped_revision
        assert state["target_id"] is None and state["yaw"] == 0.

    preview.select(recovered, revision=stopped_revision, now=at)
    for sequence in range(12, 28):
        at, identity = observe(sequence, .3)
        state = preview.state(at)
        assert state["phase"] == "tracking"
        assert state["target_id"] == identity == recovered
        assert state["yaw"] < 0.
