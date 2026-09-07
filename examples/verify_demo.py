"""Verify the distributed SITL subset without opening any acquisition source."""
from collections import Counter
import hashlib
import json
from pathlib import Path

from argos.backends.mavlink import read_recording


def main():
    directory = Path(__file__).resolve().parent / "demo"
    manifest = json.loads((directory / "manifest.json").read_text())
    name = manifest["file"]
    if Path(name).name != name:
        raise ValueError("demo filename must be local")
    data = (directory / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("demo does not match its manifest")
    with (directory / name).open("rb") as stream:
        recording = read_recording(stream)
    counts = dict(Counter(event.type_name for event in recording.events))
    if counts != manifest["message_counts"] or len(recording.events) != manifest["events"]:
        raise ValueError("demo message counts do not match its manifest")
    if (recording.started_at != manifest["started_at"] or recording.ended_at != manifest["ended_at"]
            or recording.codec_version != manifest["recorded_codec_version"]):
        raise ValueError("demo timing/codec metadata does not match its manifest")
    if recording.context is not None or any((event.system, event.component) != (1, 1)
                                           for event in recording.events):
        raise ValueError("unexpected demo provenance or component")
    print(f"SITL example: {len(recording.events)} verified frames, {recording.ended_at - recording.started_at:.3f} s")


if __name__ == "__main__":
    main()
