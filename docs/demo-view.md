# Demo view

In **Observation**, choose **Demo view** above the camera. **Exit demo** restores
the ordinary display. The fullscreen button remains available when supported by
the browser. Opening Sources, the capture inspector or another workspace also
leaves the demo presentation.

The view centers the existing camera between compact axis controls on the left
and an image-based framing diagram on the right. The video keeps its source
aspect ratio without cropping or stretching; a 640 × 480 source stays 4:3.
A rolling 30-second history
spans the width below the camera and instruments. The desktop layout uses the
whole window, including fullscreen, with the camera sized to the available
height, with equally sized instrument columns on either side. On narrow
or very short screens the page scrolls; on narrow screens the instruments stack
below the video. All labels are in English.

The flight display is read-only. Entering or leaving it does not select a
target, change the radio mode or send a control request. Person-selection
buttons are hidden and inactive in the demo. Use the ordinary Pocket workflow
to select a target and choose the assistance mode.

**Record raw** in the camera header starts raw camera video and its existing
flight-log bundle without leaving demo. **Stop raw · mm:ss** stops the capture;
**Finalizing…** prevents another start until the writers finish. It requires a
recent physical camera source. The saved camera video has no browser overlays;
record the screen separately to keep the demo dashboard in a video. An existing
session-only capture shows **Stop capture**, without claiming raw video is being
saved. The global capture indicator still opens the ordinary recording inspector.

## Reading the overview

- **Selected target** retains the person's ID and detection confidence beside
  the box. **Tracked** (blue) means recent visual tracking; **Locked** (sand)
  additionally requires confirmed assistance on yaw or pitch. **Searching**
  (amber) appears only during the backend's bounded visual recovery window,
  with a recent image. **Lost** means recent evidence of loss without an active
  recovery window. Missing or expired visual evidence shows **Unavailable**.
- A horizontal line at image mid-height runs from the center to the selected
  person's horizontal position. It shows the left/right image offset relevant
  to yaw, without suggesting a climb or descent. It is dashed for tracking and
  solid for confirmed assistance, not a measured flight direction or commanded
  trajectory. Loss or stale visual evidence removes it; no predicted line
  follows a missing target.
- **Selected mode** shows **Manual**, **Yaw assist**, or **Yaw + apparent
  distance** from the Pocket report. This is independent of each axis's state.
- **Axis control** has separate Pilot and ARGOS bars for each axis. The circle
  marks the pilot's stick before mixing; the diamond marks the reported Lua
  correction. Each bar has its own fill and value, on the same signed scale
  normalized from `[-1024, 1024]` to `[-100%, 100%]`.
- A chevron marks the reported source in control, independently for yaw and
  pitch. A combined mode can show yaw with the pilot while pitch remains with
  ARGOS. The source does not follow command magnitude: a valid zero correction
  still displays `0%` and can remain active. Missing values use a dashed bar,
  no marker, and an em dash with an accessible **Unavailable** label.
- **Paused**, **Waiting**, and **Unavailable** reports have no active-source
  chevron. The small caption under an axis settles over 0.5 seconds and groups
  Paused/Waiting as **Standby**; fine hatching marks the interruption immediately.
  **Unavailable** and the separate **Last report** qualifier bypass this
  smoothing. Values and control-source chevrons always follow the exact report.
  Target loss suppresses assistance even if the last radio report preceded it.
  Roll and throttle remain pilot-owned when their reports are fresh and valid.

Axis states describe Pocket Lua reports, not native mixer gates or measured
flight-controller actuation. Apparent distance is based on image size; the demo
does not report metric range or aircraft motion.

## The shot

The radar-shaped diagram is a view of framing in the camera image. Horizontal
position moves the subject marker along a fixed arc. With a validated apparent
size reference, the subject's height relative to that reference controls the
marker's diameter; the dashed circle shows the centered size goal. Marker radius
is limited to 4–28 display pixels for readability; **Signal details** retains
the actual ratio beyond those display limits. The geometry
is schematic and does not measure physical range, bearing or camera field of
view. There is no scanning or prediction of a lost person's movement.

Yaw-only mode shows a centering guide without a size goal. Without a validated
reference the current image position can still appear as a constant-size
direction marker. Manual mode has no assistance goal. A temporarily lost target
can leave a static hollow **Last seen** marker for the same camera, run and
selection; unavailable evidence hides the current and goal markers. Changing
context clears remembered framing. **Signal details** exposes image centering
and apparent height/reference without requiring those numbers in the main view.
It also lists each axis's exact current report, independently of the settled
caption or a retained last report.

The diagram reads the selected detection from the image currently displayed.
It uses `yaw_assist.distance_preview.reference_height` only for fresh combined
mode when the preview reports `experimental: true`, `valid: true`, and a usable
reference. The existing reference field has no independent camera, target or
timestamp identity, so the display also relies on the current matching preview
and image context. It does not turn this field into a separately synchronized
measurement. Invalid or missing reference data cannot create a size goal.

## Recent history

The bottom timeline summarizes the last 30 seconds of locally observed yaw,
pitch and target states. Its two colored axis groups are **ARGOS**
and **MANUAL**. ARGOS is an assistance family, not proof of continuous control:
Paused/Waiting intervals have fine hatching. Waiting can include the wait for a
fresh command after the pilot recenters a stick. A blank/dark gap means unknown
evidence and never inherits the previous group.

A category change must persist for 0.5 seconds to create a new colored block;
a shorter excursion is hatched within the existing group. Once confirmed, the
new block begins at the observed transition time. Text appears only on portions
longer than 2 seconds with enough screen space. Hatching also covers brief
manual takeovers: their live Pilot chevron and exact state remain immediate.
**MANUAL** follows Pocket-reported ownership, never a threshold applied to stick
movement. The frontend changes no control, takeover or recenter settings.

**INSPECT** reads the original observed states, including Paused, Waiting and
brief Manual changes, rather than the grouped blocks. **Signal details** shows
the exact current state of every axis. These are browser observations; reports
between browser updates can still be missed. The persisted filming logs retain
their original evidence and are not rewritten by this presentation.

Every locally observed transition is kept in a bounded 512-point history;
the timeline redraws at most four times per second during ordinary updates.
DOM blocks are reused (128 groups per lane, plus 128 hatch spans per axis).
If interruption density exceeds that display pool, older hatches merge
conservatively instead of disappearing into solid assistance. Unknown intervals
and missed time remain gaps. Context changes reset the appropriate history or
caption memory; nothing before entry is invented. Inspect neither replays the
camera nor changes control, and this history is not flight-controller feedback.

## Data and freshness

The view uses the existing `/api/state` and camera requests. It adds no endpoint,
capture pipeline, telemetry polling loop or external dependency. Camera images retain the existing
paired image/detection handling and freshness checks.

The optional `yaw_assist` data contains `pilot_sample` (schema version 1),
`pilot_sample_age_s`, `pilot_sample_fresh`, and `assistance`. The latter must have
`source: "pocket_lua_report"`, `fresh: true`, `selected_mode: "M" | "Y" | "D"`,
and independent `yaw` / `pitch` objects with `state`, `valid` and `value`.
The display consumes these derived states, never global `radio_state` or ACK as
proof of axis activity. Raw AP1 mode `N` is not a display-mode value.

Current reports expire at 350 ms, including elapsed browser time and request
transit, matching the service's receipt limit. In demo only, the last validated
mode and bars may remain until that same report reaches **1 second total age**.
They are dimmed and marked **Last report · age**, with no active-source chevron.
Repeated HTTP snapshots cannot renew this deadline. Beyond it, they become
**Unavailable**. This is display memory: the timeline stays unknown during the
gap, and neither **Locked** nor the live radar goal survives on stale evidence.

Only age expiry permits this brief hold. A new report takes effect immediately;
invalid/contradictory data, disconnection, target loss, or a changed run, camera,
radio session/generation or selection clears the retained report. Leaving demo
or hiding the tab also clears it. The camera keeps its independent freshness
checks. Normal console and radio/control freshness thresholds are unchanged.
A disconnected service or radio, unknown source, or missing/malformed field
cannot light an assisted indicator. A running older backend without these optional fields keeps the
camera usable and shows **Unavailable** for absent report data.

Browser regression tests use isolated synthetic reports and images; they do
not access a live radio or validate physical response. They cover independent
axis bars, valid zero versus absent values, framing provenance and lost targets,
local history, presentation settling with exact inspection, responsive video
proportions, bounded last-report retention and
in-place raw recording. Synthetic recording responses do not capture hardware. Run them with:

```sh
npm run test:browser -- tests/browser/demo-view.spec.cjs tests/browser/demo-recording.spec.cjs
```
