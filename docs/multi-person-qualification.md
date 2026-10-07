# Offline multi-person selection qualification

The controlled harness compares the current continuous `YawPreview`, the held
motion prototype, and the `motion_held_candidates` prototype. It feeds each the
same explicit detections, native track IDs, 208-value unit appearance vectors,
source receipts, initial selection, and 10 ms owner polling timeline. The actual
`YawValidator` evaluates every resulting snapshot without a device or transport.
No flight code imports the experiment.

The new policy opens a candidate-comparison episode only when a lost or paused
selection has more than one strong candidate. It remains in that episode if a
competitor disappears, and requires two fresh qualifying images, including for
a returning selected ID. Outside that episode the held-motion behavior remains
unchanged. In particular, an active selected native ID remains trusted; this
experiment does not solve tracker ID reuse or prove a person's identity.

## Freeze, then execute

Use the repository environment containing the ordinary ARGOS dependencies:

```bash
python examples/qualify_multi_person.py --freeze-manifest /tmp/argos-scenarios.json
python examples/qualify_multi_person.py \
  --scenario-manifest /tmp/argos-scenarios.json \
  --output-dir /tmp/argos-multi-person-results
```

Freezing writes the complete inputs and a SHA256 sidecar. Execution verifies the
exact file bytes, requires a new output directory, runs every condition twice,
checks identical semantic traces excluding measured execution cost, and verifies
inputs remain unchanged. A pre-execution implementation manifest binds the
selection policy, production validators, appearance contract, harness, CLI, and
viewer by SHA256; execution also verifies those sources have not changed. The resulting `report.json` and standalone `index.html`
retain all scenarios and their full timelines. No recording, detector, native
association tracker, HTTP endpoint, camera device, or command transport is opened.
The dry snapshot uses the existing device-shaped validator contract only.

## What is measured

Eighteen fixed scenarios cover a bystander during intact tracking, target ID
change, return of the old ID beside someone else, ambiguous crossing followed by
resolution, target departure, unknown appearance, a near tie below the appearance
threshold, the exact 0.05 separation boundary, incompatible geometry, ambiguity
between two confirmation images, missing detections or descriptors, repeated
polls, stale images, and recovery deadline expiry. A paired control reverses
candidate order and confidence priority while keeping physical identity evidence.

Two deliberate counterexamples remain in the headline totals:

- A different person replaces the departed target with indistinguishable
  appearance and geometry under a new native ID.
- A tracker assigns the actively selected native ID to a different person.

These are actual wrong-person selections and dry admissions under the prescribed
observations. Marking them as known limitations does not remove them from counts.
A synthetic scenario success rate would not estimate real-world safety or accuracy.

Symbolic `A` and `B` labels occur only in `evaluator_truth`. The policy receives
only a copied `observation`; it receives neither those labels nor the scenario
name, expected outcome, reference box, or annotation. The evaluator joins each
selected numeric ID to the prescribed person label after selection and validation.
Unit tests relabel the evaluator truth and verify unchanged policy and consumer
traces, while wrong-person counts change as expected.

The report distinguishes:

- Fresh selection on a distinct delivered image from repeated owner polls.
- Selected target images from wrong-person selected images.
- Preview recovery from later dry consumer confirmation.
- Dry admissions of the target from actual admissions of the wrong person.
- Phase durations and dry-admitted durations, including wrong-person duration.

No new measurement appears during a repeated poll. An old image can remain drawn
in the viewer after expiry; its selected highlight and admission disappear with
the actual trace. Selected highlights are evaluator labels on synthetic boxes,
not predictions, camera pixels, or synthesized photographic evidence.

Execution cost covers preview observe/select/state calls and diagnostic callbacks.
It excludes evaluation, dry validation, detection, association, camera processing,
IPC, and command transport. Wall and CPU measurements do not delay the prescribed
clock and are not a live latency or resource budget qualification.

Real recordings with multiple people, occlusion, exits, similar clothing, native
association errors, and representative camera timing remain necessary before a
production decision. The synthetic controls establish only whether the bounded
selection contract behaves as specified for these exact observable inputs.
