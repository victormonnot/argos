"""Sample existing camera/control observations without driving either source.

All timestamps belong to the console receipt clock. An analyzed image is only
recorded when its result is already available; inference is never awaited here.
Recording is an observer and a failure cannot interrupt the flight-control loop.
"""
from .views import safe_text


CONTROL_KEYS = ("at", "enabled", "available", "reason", "owned", "phase", "axes",
                "selected_mode", "throttle", "mode_generation", "mode_transition",
                "mode_transfer", "prepared", "input_seq", "framing", "vehicle",
                "command", "interruption", "last_error")
ACTION_LABELS = {"prepare": "Prepare flight mode", "arm": "Arm", "disarm": "Disarm",
                 "land": "Land", "release": "Release control", "mode": "Switch flight mode",
                 "switch_mode": "Switch flight mode"}
FRAMING_LABELS = {"select": "Select target", "engage": "Engage framing", "stop": "Manual control",
                  "clear": "Clear target", "closer": "Closer", "farther": "Farther"}


def capture_visual(session, now):
    recorder = session.recorder
    if not recorder.active or not recorder._visual_enabled or recorder._visual_error:
        return
    visual = recorder.visual
    if visual.snapshot()["state"] != "recording":
        return
    try:
        # Sample at most 10 Hz before building optional control snapshots.
        if getattr(session, "_visual_sample_key", None) == recorder.snapshot()["id"]:
            if now - session._visual_sample_at < .1:
                return
        session._visual_sample_key = recorder.snapshot()["id"]
        session._visual_sample_at = now
        image = analysis = None
        candidate = session.vision.frame(session) if session.vision is not None else None
        if candidate is not None:
            sample = candidate.sample
            image = {"jpeg": sample.jpeg, "sequence": sample.sequence,
                     "received_at": sample.received_at, "video_id": candidate.context[1],
                     "width": candidate.result["width"], "height": candidate.result["height"]}
            analysis = {"frame_sequence": sample.sequence, "video_id": candidate.context[1],
                        "detections": candidate.result["detections"],
                        "inference_ms": candidate.result["inference_ms"]}
        else:
            raw = session.video.latest_with_dimensions(now)
            if raw is not None:
                sample, width, height = raw
                image = {"jpeg": sample.jpeg, "sequence": sample.sequence,
                         "received_at": sample.received_at, "video_id": session.video_source_id,
                         "width": width, "height": height}
        control = session.control.state(now)
        visual.append(now, frame=image, vision=analysis,
                      control={key: control[key] for key in CONTROL_KEYS})
    except Exception:
        # Do not let optional media work enter the telemetry transport-error
        # path or acquire authority. Its own bounded status remains inspectable.
        try:
            visual.stop(now, reason="capture_error", detail="Visual capture stopped after an observation error")
        except Exception:
            pass


def capture_action(session, operation, values, *, status, error=""):
    """Record discrete operator requests, never capability tokens or keepalives."""
    try:
        if not session.recorder.active or not session.recorder._visual_enabled or session.recorder._visual_error:
            return
        if operation == "input" or not isinstance(values, dict):
            return
        if operation == "framing":
            requested = values.get("operation")
            label = FRAMING_LABELS.get(requested, "Framing request") if isinstance(requested, str) else "Framing request"
            if requested == "engage" and values.get("profile") == "pilot_throttle":
                label = "Engage framing with manual throttle"
        elif operation == "action":
            requested = values.get("action")
            label = ACTION_LABELS.get(requested, "Flight action") if isinstance(requested, str) else "Flight action"
        else:
            label = "Take control" if operation == "claim" else "Control request"
        detail = label + (" requested" if status == "accepted" else " refused")
        if error:
            detail += ": " + safe_text(error)[:240]
        session.recorder.visual.event(session.clock(), operation, detail, status=status)
    except Exception:
        # Instrumentation never changes the outcome of an operator action.
        pass
