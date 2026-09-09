# Recorded overlapping-detection metadata

This seven-image fixture comes from the September 9, 2026 Gazebo/SITL flight
with YOLOX-S, four CPU threads and the code later committed as `fd7dc69`.
Image 3069 contains the selected track 1 (confidence 0.834) and a smaller track 2
(confidence 0.357) fully inside it. Their IoU is about 0.438, below the existing
0.45 NMS threshold. Visual inspection of the separately retained JPEG found one
person. The fixture itself contains no pixels or ground-truth identities.

The following images, 3072 and 3075, each contain only the original track 1.
This is an example of a transient ambiguous observation, not proof that nested
boxes always refer to one person. A genuinely occluded second person could
produce similar geometry; both measurements must remain visible and assistance
must remain neutral during ambiguity.

`observations.jsonl` preserves original image sequences, source identities,
normalized boxes, scores, camera receipt times and availability times. Its 24
tick rows are collector polls, not exact controller execution timestamps. State
blobs and JPEG paths were removed. This metadata replay does not run detection
or appearance association again.

The explicit replay engagement starts just after image 3059 was observed and
ends at image 3078. The real flight engaged earlier and requested Manual/Land
after image 3069. Therefore the later recovery images belong to the recorded
camera path under those actual commands. Accepting them in this replay cannot
establish that a different closed-loop flight would have succeeded.

From the repository root with the base project installed:

```sh
PYTHONPATH=. python examples/replay_framing.py \
  examples/data/framing_overlap/observations.jsonl \
  --windows examples/data/framing_overlap/windows.json \
  --availability-uncertainty .05
```

The report compares nine confidence/pause pairs. The production settings are
confidence 0.5 and pause 0.6 seconds.

The baseline helper at `fd7dc69` latches takeover on image 3069. The bounded
ambiguity policy instead keeps its existing 600 ms deadline, sends zero framing
outputs, and requires two distinct fresh sole-target images to recover. It does
not suppress the extra box, change its confidence or select another identity.
A live trial is still required to evaluate the resulting camera path.

The original complete capture SHA-256 is
`d0ed2ec55f559b4136493e05eedff98bcacadddfc372a84c117bf46543a7ce06`.
