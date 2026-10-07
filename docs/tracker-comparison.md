# Offline tracker comparison

`examples/compare_trackers.py` compares ImageTracker, official ByteTrack and
official BoT-SORT without learned ReID on the same completed native `.flight`
recording, custom YOLOX bundle and explicit selections. It extends the
[continuity diagnostic](continuity-diagnostics.md); no production tracker
selector, active model, flight configuration or service is changed.

Provision optional dependencies in a separate environment:

```sh
python -m venv dist/tracking-venv
dist/tracking-venv/bin/pip install -e '.[tracking,dev,console]'
dist/tracking-venv/bin/python examples/compare_trackers.py \
  --flight-dir /path/to/recording.flight \
  --windows /path/to/windows.json \
  --bundle /path/to/custom-model-bundle \
  --threads 4 --max-hz 10 --repeats 3 \
  --output-dir /path/to/new-comparison
```

Inputs and output constraints are those of the continuity diagnostic. The
output must be new or empty, outside the recording and model bundle. The native
recording, window file, model, inference cache and implementation files are
hashed and checked again before completion. Failures retain an explicit failed
report and partial artifacts; no incomplete run is reported as a comparison.

## Frozen evidence and association contract

One decode/detector/heuristic-appearance pass supplies all variants. The cache
retains original JPEG hashes, image receipt times, normalized measured boxes,
scores and aligned descriptors. The production confidence floor is 0.35, NMS
threshold 0.45, at most 16 detections. This comparison does not evaluate the
additional low-score evidence between 0.1 and 0.35 that native ByteTrack and
BoT-SORT could otherwise use.

The exact upstream revisions, MIT licenses, source hashes and import/provenance
adaptations are in [the vendor manifest](../argos/perception/_vendor/README.md).
Native association, Kalman filters and BoT-SORT sparse optical flow camera
motion compensation are preserved. Learned ReID is disabled. ImageTracker
retains its existing color/gradient heuristic; target recovery uses that same
heuristic for all three variants. No result implies that learned ReID is needed.

Native parameters are declared, not tuned on the loss examples: high threshold
0.5, birth 0.6, score fusion and seven lost updates at nominal 10 Hz. Native
strict `>` high-confidence boundaries differ from ImageTracker's `>=0.5`;
seven updates are not a wall-clock equivalent of ImageTracker's 0.7 seconds.
Irregular analysis cadence and native expiry order matter. A frame missing
from the common schedule does not generate a fictitious empty update.

Each native track carries its exact input detection index. The offline bridge
uses only that association ID and reconstructs output from the **original
measured box and score**. Kalman-smoothed and predicted boxes never supply new
observations to selection. Native confirmation differs: ByteTrack hides
unconfirmed tracks, while BoT-SORT returns them. The replay retains every
omitted detector candidate using a unique one-image identity, marked ephemeral
and reserved above `2**52`. This prevents native suppression from hiding a
strong distractor from the unchanged ambiguity policy. Such identities must
be excluded from persistent-track counts. The ImageTracker path preserves its
original display-only weak identities and behavior.

Continuous `YawPreview` and dry `YawValidator` are unchanged. Each window starts
with fresh association state and one explicit selection by box, after the
declared prehistory. No native flight control, radio, HTTP server or device is
opened. A valid dry demand is a software observation, not flight authority.

## Schedules and measured costs

Both modes use the same frozen worker durations across all variants:

- Recorded mode admits the same logged image sequences at their saved
  observation times. These are upper-bound availability proxies; exact historic
  worker completion and intervening polls remain unknown.
- Simulated mode runs the production latest-image/single-pending-job
  `VisionService` boundary with cached worker results and a 10 ms clock. Actual
  measured association/GMC work does **not** block this clock. Image ages are
  therefore controlled scheduling results, not end-to-end latency benchmarks.

Tracker/GMC wall and CPU costs are measured separately for each update. BoT-SORT
also needs pixels: the harness's extra JPEG decode is reported separately as
image-provider cost, not attributed to the association algorithm. A future live
integration would need to decide how pixels cross the worker/owner boundary.
Both methods use instrumented adapters; diagnostic construction and adapter
validation are included in tracker costs and differ between implementations.

Two detector warmup images precede one common measured inference pass. For each
tracker/mode, a separate warmup process precedes three measured fresh processes
(configurable from two to ten). Tracker order rotates between repetitions;
recorded mode precedes simulated mode. Each process resets trackers between
windows, controls OpenCV/BLAS thread counts and seeds OpenCV/NumPy. The warmup
does not warm Python state in later fresh processes. Semantic hashes exclude
execution measurements and retain actual IDs, schedules and decisions; the
report records whether outcomes agree across repetitions.
The `process_*` CPU/wall timers start on entry to the replay job, after initial
Python and shared-module startup; per-update measurements have the narrower
association/image-provider scopes above.

RSS is the replay process high-water mark, including imports, cached JPEGs,
descriptors and diagnostic traces, excluding the detector weights loaded in
the parent process. The pre-tracker-import high-water mark is also reported.
On Linux the source is `/proc/self/status` `VmHWM`, which describes the current
executable's address space. `getrusage().ru_maxrss` can retain a parent's
pre-exec peak and must not be used for this isolated-process comparison.
Their difference is not an exact tracker allocation measurement. These costs
are useful desktop diagnostics, not laptop budgets or sustained live resource
measurements. CPU seconds can exceed wall seconds when work uses several cores.

## Reading the evidence

`report.json` contains provenance, parameters, aggregate costs and representative
software outcomes. `passes/` retains every job, result and process log, including
warmups. `inference.jsonl` and `frames/` are the common frozen input. `index.html`
shows synchronized original images with all three association/selection results;
an image omitted by a schedule is explicitly unanalyzed, without carried-forward
boxes presented as fresh measurements.

Compare detector misses, association changes, recovery refusals/confirmations,
software pauses/stops and dry-consumer admission separately. Local ID totals
alone cannot rank person continuity. Software phase durations are not time
following the correct human. Review exact source frames around divergent
associations and stop causes, and retain uncertain identity/visibility labels.

Human-confirmed intent at a few checkpoints and assistant-drawn approximate
boxes remain sparse evidence. Exact-frame overlap with such a box must be
labelled as overlap with that reference, not human ground truth. Skipped
checkpoints must not be replaced with nearby frames. Dense identity/visibility
annotations and independent scenes are needed before global IDF1/HOTA, a wrong
person rate or a general quality recommendation is justified.
