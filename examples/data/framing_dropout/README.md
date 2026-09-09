# Recorded framing dropout metadata

These two small fixtures come from Gazebo camera observations captured on
2026-09-08 with ARGOS revision `1e41013` and its unchanged YOLOX-Tiny detector.
They retain original availability times (`at`), camera receipt times
(`received_at`), image sequences, source identifiers and detection results.
Tick rows are collector polls, approximately 50 ms apart, **not actual controller
execution timestamps**. State blobs and JPEG paths were removed; no pixels are
bundled. This is a replay of recorded detection metadata, not video re-inference.

| Fixture | Recorded frame rows | Recorded failure neighborhood |
| --- | ---: | --- |
| `observations-short.jsonl` | 13 | Confidence .412, then an empty result; confidence .864 returned on the same ID after about .401 s. |
| `observations-higher.jsonl` | 13 | Confidence .385 then .367; confidence .547 returned on the same ID after about .449 s. |

Visual inspection of the separately retained source JPEGs found the full person
still visible around both failures. That observation is a human review of these
examples, **not ground-truth annotation or a detector accuracy metric**. The
fixtures cannot measure general false-positive rates or physical distance.

Each window is an explicit, independent offline engagement two seconds before
the first failing confidence result. One preceding image initializes it. The
window ends at the first recovery image, before the operator's manual stop.
It therefore tests whether that recovery point is admitted; it does not claim
an uninterrupted full flight or any duration after recovery. A takeover remains
latched within its window. Different guidance would change the camera path in
a real flight, so live Gazebo validation remains necessary.

Use Python 3.11+ with the base project installed (`python -m pip install -e .`);
the normal package import requires NumPy. Camera, vision and MAVLink extras are
not needed. From the repository root, compare all nine continuation-confidence/
pause pairs:

```sh
PYTHONPATH=. python examples/replay_framing.py examples/data/framing_dropout/observations-short.jsonl --windows examples/data/framing_dropout/windows-short.json --availability-uncertainty .05
PYTHONPATH=. python examples/replay_framing.py examples/data/framing_dropout/observations-higher.jsonl --windows examples/data/framing_dropout/windows-higher.json --availability-uncertainty .05
```

Engagement confidence remains .5. The unchanged helper enforces source,
identity, box geometry and .45 s image freshness. Both fixtures reproduce a
takeover with confidence .5 / pause .35 and a recovery with confidence .5 /
pause .6. The .8 s candidate adds no benefit in these examples. Reports include
the input/helper hashes, measured poll gaps and timing limitations.

The capture snapshots used for extraction had
SHA-256 hashes:

- Short: `610ab436cda6c4fa81952bb7917b59744b1fed15a97e4b6498aba7c41b625f66`
- Higher: `196fe99d0cb0def64423ab696860323c49555c5f3e8322b0e5a31ae2e10b1f2d`
