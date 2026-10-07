# Offline tracker sources

These modules are used only by the explicit offline comparison adapter in
`argos.perception.tracker_comparison`. Production VisionService continues to
use ImageTracker. No model, device or flight control is loaded by these modules.

| Algorithm | Official repository | Pinned revision | License |
| --- | --- | --- | --- |
| ByteTrack | https://github.com/FoundationVision/ByteTrack | `d1bf0191adff59bc8fcfeaa0b33d3d1642552a99` | MIT, Yifu Zhang |
| BoT-SORT | https://github.com/NirAharon/BoT-SORT | `251985436d6712aaf682aaaf5f71edb4987224bd` | MIT, Nir Aharon |

Each directory contains its upstream license and an exact `adaptations.patch`.
`provenance.json` records upstream paths and SHA-256 digests of both original
and adapted files. The included tracker, matching, Kalman filter and base-track
implementations are official source, together with BoT-SORT's GMC module.

Local adaptations are limited to:

- Relative imports so two independently named tracker packages coexist.
- NumPy compatibility: removed `np.float` aliases become Python `float`, and
  `np.float_` becomes `np.float64` (not float32).
- Removal of unused Torch imports; moving FastReID and debug-only Matplotlib
  imports into their existing optional branches. The adapter disables ReID.
- A `detection_index` field passed through original detector filtering,
  track creation, update and reactivation. It is observation provenance only;
  association costs, assignment, state transitions and Kalman filters do not
  consult it. The adapter uses the index to return original normalized boxes
  and confidence, never Kalman-smoothed or predicted boxes as new observations.

No fallback implementation of LAP, IoU or GMC replaces the upstream code.
Dependencies are the optional `tracking` extra. Use an isolated environment,
for example `python -m venv dist/tracking-venv` followed by
`dist/tracking-venv/bin/pip install -e '.[tracking,dev,console]'`.

The frozen baseline uses high threshold 0.5, birth threshold 0.6, score fusion,
association limits 0.8/0.5/0.7, and a seven-update lost-track buffer
(`frame_rate=10`, `track_buffer=21`). Upstream low threshold remains 0.1 but
the adapter accepts only the common detector cache at confidence >=0.35. There
is no hidden low-threshold detector experiment. Strict upstream `>`/`<` tests
exclude confidence exactly 0.5 from both association passes. These differences
from ImageTracker's >=0.5 births are retained and reported, not tuned on clips.

The seven-update buffer is not a wall-clock TTL. Skipped frames and irregular
analysis change its elapsed duration; comparisons must report this difference
from ImageTracker's 0.7 seconds. No synthetic empty update fills skipped images.
The official cleanup order also permits association before the expired-lost
cleanup runs; seven updates is the configured buffer, not a guaranteed last
possible association boundary.

BoT-SORT uses native sparse optical flow GMC at downscale 2 on original BGR
images. Learned ReID is disabled; ImageTracker retains its existing color and
gradient descriptor, and selected-target recovery may use that same descriptor
for all methods. GMC exceptions propagate; no silent geometric fallback is
claimed to be the official algorithm.

Native confirmation behavior is preserved: ByteTrack omits unconfirmed tracks;
BoT-SORT returns them. The adapter reports confirmation flags and all unassigned
detector rows. The replay must retain those rows in its selection policy (with
explicit nonpersistent identities) so suppression does not hide ambiguity or
give a method fewer detector candidates. Persistent-track metrics must exclude
those replay-only identities. Source-row mapping remains exact even when native
outputs change order or include geometrically overlapping measurements.

Official sources retain their original global ID counters. The adapter isolates
the counter per instance under a lock, allowing repeatable independent replays;
this changes numbering scope, not association decisions. OpenCV threading and
random seed are controlled by the offline runner, not these native modules.
