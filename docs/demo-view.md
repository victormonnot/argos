# Demo view

In **Observation**, choose **Demo view** above the camera. **Exit demo** restores
the ordinary display. The fullscreen button remains available when supported by
the browser. Opening Sources, the capture inspector or another workspace also
leaves the demo presentation.

The view enlarges the existing camera player without cropping its image. It
shows the selected person's marker, selected Pocket mode, and separate yaw,
pitch, roll and throttle readouts. All labels are in English. On narrow screens
the overview appears below the video and the page scrolls.

This is a read-only presentation. Entering or leaving it does not select a
target, change the radio mode or send a control request. Person-selection
buttons are hidden and inactive in the demo. Use the ordinary Pocket workflow
to select a target and choose the assistance mode. The capture indicator remains
accessible; opening its inspector restores the ordinary recording controls.

## Reading the overview

- **Selected target** identifies the person selected in the current camera/run.
  **Visible in camera** requires recent analysis and a matching detection in the
  displayed image. Temporary target loss retains the identity and shows
  **Assistance paused**. Missing or expired visual evidence shows **Visibility
  unavailable**, without implying an active search.
- **Selected mode** shows **Manual**, **Yaw assist**, or **Yaw + apparent
  distance** from the Pocket report. This is independent of each axis's state.
- **Yaw / Pitch** show **Manual**, **Assisted**, **Paused**, **Waiting**, or
  **Unavailable** independently. A combined mode can report yaw manual while
  pitch is assisted. Target loss suppresses an assisted indicator even if the
  last radio report preceded that loss.
- **Roll / Throttle** remain pilot-owned in these modes. They display **Manual**
  only with a fresh report, known mode and valid corresponding stick sample.
- **Pilot stick** is the reported input before mixing, normalized from
  `[-1024, 1024]` to `[-100%, 100%]`. A reported zero displays `0%`; missing data
  displays **Unavailable**.

Axis states describe Pocket Lua reports, not native mixer gates or measured
flight-controller actuation. Apparent distance is based on image size; the demo
does not report metric range or aircraft motion.

## Data and freshness

The view uses the existing `/api/state` and camera requests. It adds no endpoint,
capture pipeline or external dependency. Camera images retain the existing
paired image/detection handling and freshness checks.

The optional `yaw_assist` data contains `pilot_sample` (schema version 1),
`pilot_sample_age_s`, `pilot_sample_fresh`, and `assistance`. The latter must have
`source: "pocket_lua_report"`, `fresh: true`, `selected_mode: "M" | "Y" | "D"`,
and independent `yaw` / `pitch` objects with `state`, `valid` and `value`.
The display consumes these derived states, never global `radio_state` or ACK as
proof of axis activity. Raw AP1 mode `N` is not a display-mode value.

Reports expire at 350 ms, including elapsed browser time and request transit,
matching the service's receipt limit. A disconnected service or radio, stale
sample, unknown source, or missing/malformed field cannot light an assisted
indicator. A running older backend without these optional fields keeps the
camera usable and shows **Unavailable** for absent report data.

Browser regression tests use isolated synthetic reports and images; they do
not access a live radio or validate physical response. Run them with:

```sh
npm run test:browser -- tests/browser/demo-view.spec.cjs
```
