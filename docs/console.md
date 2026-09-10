# ARGOS console

A local console for receiving the drone camera feed and telemetry, configuring
sources, and recording sessions. The interface is in English, works without
external web resources, and displays the entire image.
The **Observation** view uses the graphite Forge style: a main camera area,
measurements grouped under their source, and a side inspector. Fonts are bundled
with the application. The **Sessions** view lets you find local recordings,
verify their integrity, and replay their telemetry and optional captured video in the browser.
The console remains passive by default. **Flight controls** adds explicitly enabled
manual Gazebo/SITL control with held mouse/touch buttons and optional keyboard
shortcuts. The [web-control guide](web-control.md) documents that separate
GPS-free workflow, its launcher and control API.

## Starting the interface

From the repository root:

```sh
python -m pip install -e '.[console,mavlink]'
python -m argos.console
```

Open **http://127.0.0.1:8080**. Without source arguments, the console shows
unconfigured states, with no demonstration image or fabricated telemetry. Use
`--port` to change the HTTP port. The server listens only on the local machine;
it is not a public service or an authenticated multi-user interface.

## Gazebo camera

The console subscribes to the onboard sensor's `Image` topic using the
Gazebo Harmonic bindings (`gz.transport13`, `gz.msgs10`). It does not open a video
file. Gazebo must already be running with its camera sensor active.

```sh
python -m argos.console \
  --gazebo-topic /world/iris_runway/model/iris_with_gimbal/model/gimbal/link/pitch_link/sensor/camera/image \
  --gazebo-python-path /usr/lib/python3/dist-packages
```

This topic belongs to the `iris_with_gimbal` model in the `iris_runway` world.
Adjust it to your simulation; topics are not discovered or selected
automatically. If the simulation uses `GZ_PARTITION`, give the console the same
value. `--gazebo-python-path` is optional if the bindings are already importable.
It appends the explicitly supplied path after the virtual environment's paths,
without replacing its dependencies with system packages.

Image transport is independent of MAVLink: Gazebo provides the camera feed,
and ArduPilot SITL provides telemetry. An available camera does not prove that a
connection to SITL is established. The console neither starts nor configures the
autopilot.

The [SITL + Gazebo observation guide](sitl-observation.md) provides the complete
startup procedure verified on the ground, with both streams received together.

The adapter respects the pixel format and row stride. RGB_INT8, BGR_INT8, and
L_INT8 are supported; unknown formats and inconsistent sizes are rejected.
Limits are 4096 per dimension, 8,388,608 pixels, 32 MiB of raw image data, and
16 MiB for JPEG. The Gazebo timestamp remains available in the API as
`source_stamp`, separate from the local reception time, with no synchronization
assumed.

## Physical camera

A V4L2 input is available for a local Linux camera:

```sh
python -m pip install -e '.[console,mavlink,camera]'
python -m argos.console --camera-device /dev/video0
```

The source is then displayed as real. Only `/dev/videoN` devices are accepted:
no web URL or recorded video is used as a fallback. This path still needs to be
tested with the chosen hardware. It does not provide a complete onboard video
link; transport from a real drone must still be selected for its camera and
radio/network equipment.

A single worker reads the device and retains the latest image. On shutdown, the
display is invalidated immediately; only the worker releases the driver. If a
native driver remains blocked in `read`, the thread may outlast the one-second
shutdown wait, but it cannot publish new images.

## Received telemetry

Add passive link parameters to the same launch command:

```sh
python -m argos.console --environment simulation \
  --mavlink-bind 127.0.0.1:14551 --mavlink-peer 127.0.0.1:14550 \
  --sequence-scope component --system 1 --component 1
```

These ports are examples: the sender must already be transmitting to the chosen
local port, from the declared address **and source port**. The sequence counter
scope must be selected explicitly, as described in the
[transport documentation](mavlink-transport.md).
UDP, TCP, and serial are alternatives. For a SITL TCP output, use
`--mavlink-tcp 127.0.0.1:5760 --sequence-scope channel`. The TCP server must already
be listening; without `--sim-control`, the console connects without sending any
MAVLink message. A refused
or interrupted connection appears as an error; reconnection is explicit.
For serial, use
`--mavlink-device /dev/ttyUSB0 --baudrate 115200` with the scope and environment.
To receive both video and telemetry, combine these arguments with the camera
arguments. Without a camera, `--environment simulation|real` is required for a
MAVLink link. Provenance is configured by the user, not inferred from the values.

The HUD uses HEARTBEAT, ATTITUDE, LOCAL_POSITION_NED, and battery data from
SYS_STATUS. The main view shows attitude in degrees, the declared mode, and the
received battery percentage; details include voltage, current, heartbeat,
declared armed state, and raw NED coordinates. The vertical NED component is not
renamed as height above ground or altitude above takeoff. GPS position and
individual descriptions of multiple batteries are not integrated.

Mode names are interpreted only for recognized ArduCopter vehicle types with
the custom mode flag set. An unknown mode remains identified by its raw value.
Battery handling respects MAVLink sentinels: voltage 65535, current −1, and
remaining charge −1 become unavailable values; 0% and 0 A remain displayable
measurements. Other negative current values remain signed measurements. Battery
reception age is independent of heartbeat reception age.

## Reading the interface

- **Onboard camera**: the complete image, its source, and its
  reception age. It is hidden when reception expires, stops, or can no longer be
  verified.
- **Video reception**: the control at the top opens sensor
  diagnostics. Its state remains independent of telemetry.
- **MAVLink telemetry**: the parent button sits directly above
  the four measurement previews. Its neutral highlight marks the open group;
  the red accent identifies only the selected measurement.
- **Measurements**: Mode, Battery (battery), Attitude, and Position
  locale (local position) each have a preview and a corresponding inspector
  section. The common source and selectors stay in place; only the selected
  details change. Link (link) and Events (events) are grouped separately
  under **Diagnostics**: they show reception details and an event log, rather
  than vehicle measurements. Each quantity has its own age and state. An absent
  or stale value appears as a dash, never as a measured zero.
- **Sources**: a separate settings button opens the environment, camera, MAVLink
  transport, and selected component in the **Configure sources** (configure
  sources) panel. Edit the fields and select **Apply** to restart the
  receivers and create an observation. Draft settings survive refreshes and
  navigation to other panels; the Sources button retains the **modified**
  (modified) indicator. Canceling the changes reloads the active settings without
  sending a request to the service.
- **Session recording**: a single button in the top bar opens
  the recording panel. **Start**, **Stop**, and **Download**
  (download) are grouped at the top of this panel, followed by the identifier,
  duration, message count, and any errors. Capture state remains visible on the
  top button while another panel is open. **Include video and flight events**
  is selected by default; clear it for a telemetry-only journal. Visual capture
  records images already received, matched detections and sampled flight state.
  The panel refers to this process's latest recording.
  In Sessions, new recordings expose their stored original configuration; older
  recordings without context explicitly indicate that this information is
  unavailable. The current configuration is never presented as their provenance.
- **Events**: a history limited to the last 60 camera and MAVLink events in
  this observation. This visual history does not replace the persistent MAVLink
  recording.
- **Expanded view / Full screen**: enlarge the camera
  by reducing peripheral details. Opening an inspector restores the panel.
  Controls support keyboard navigation; Escape closes the panel and returns
  focus to its trigger. Native browser keys remain available in inputs and
  selectors.

Image and telemetry states are separate. A responsive interface does not prove
that either is being received. Statuses use text labels as well as colors. On a
laptop, the camera, its status, and display controls fit on one screen; the
inspector context stays in place while its details scroll. On narrow screens,
the panel moves below Observation and the page scrolls normally. Opening details
does not resize the camera; **Expanded view** explicitly enlarges it.

Reception age thresholds are configurable: `--video-age` (1 s),
`--heartbeat-age` (1 s), `--attitude-age` (0.2 s), `--position-age` (0.4 s),
`--battery-age` (2 s).
These thresholds control the display. Times refer to local reception, not radio
latency, sensor exposure time, or flight safety.

The browser targets ten state reads per second and requests at most eight
images per second, with only one image request in flight. A response carrying
the same image identifier does not refresh that image's age. If the service
stops progressing for 1.5 s, values are hidden; time spent in the HTTP request is
conservatively included in expiration. This may hide data early, but prevents a
delayed response from making it appear newer.
This state polling rate can follow attitude received at 10 Hz without imposing
a polling interval longer than its 0.2 s display limit. Requests remain
sequential; a slowdown does not create a request queue.

## Changing sources and recording

An invalid configuration leaves the active session unchanged. A valid
configuration replaces the session: previous images, values, counters, and
events are cleared, even if the new device fails to open. The opening error
remains visible; no fallback source is selected. The Gazebo bindings path and
reception age thresholds remain local startup options.

An active recording must be stopped before changing sources. Files are created
under `~/.local/share/argos/recordings/` by default. If `XDG_DATA_HOME` specifies
an absolute path, the directory becomes `$XDG_DATA_HOME/argos/recordings/`.
Use `--recordings-dir` to choose another location. The effective path is shown
under **Sessions → Recording location** (recording location). It is
independent of the launch directory and is not stored in the repository. This
avoids temporary directories; it is not a backup or a guarantee against disk
failure. Older directories are not imported automatically: open them with
`--recordings-dir`, or copy completed recordings into the selected directory
without overwriting its files. Each capture receives a unique name without
overwriting previous files. The inspector's download button refers to this
process's most recent completed recording. **Sessions** also finds older files,
including after a restart. Changing sources preserves this latest download;
restarting resets the in-memory capture status without deleting files or their
access through Sessions.

Original frames are recorded before display filtering: other sources and
payloads rejected by the caches remain inspectable. Stopping finalizes the
JSONL recording and its integrity check. For telemetry-only capture, a MAVLink
transport error closes the receptions already written with an explicit end reason.
With visual capture enabled, recording continues so an available camera can
still be reviewed; absent telemetry remains absent. Capture also stops
automatically at **100,000 frames** or before **32 MiB**, reserving space to
finish the file. The reason is visible; no subsequent capture starts
automatically. A period without messages does not, by itself, close the capture.

Normal server shutdown attempts to close the capture with the service shutdown
reason. **Complete** (completed) describes a correctly finalized file, not a
successful mission or the absence of incidents. A write failure, full disk, or
abrupt shutdown may leave an incomplete file: it remains on disk, but no verified
download is advertised. A link failure does not hide a possible error while
closing the file on disk.

Verification and replay are also available from the command line:

```sh
python examples/mavlink_recording.py inspect ~/.local/share/argos/recordings/RECORDING_ID.jsonl
```

Replace `RECORDING_ID` with the identifier of the recording to inspect.

This reader validates the frames and the entire recording; its historical
telemetry view covers heartbeat, attitude, and NED. The recording also contains
received battery messages. Web replay includes those battery measurements.
The CLI reads JSONL telemetry; visual sidecars are replayed in Sessions. See the
[recording limits](mavlink-transport.md#recording-and-offline-inspection).

## Sessions and replay

1. Open **Sessions**, then select a recording. The list shows its size and file
   modification date, which is not the capture start time. **Refresh**
   (refresh) rereads the directory. A file is only presented as valid after its
   frames, ending, and checksum have all been verified.
2. **Flight replay** opens when a completed visual capture is available.
   **Measurements** opens for older telemetry-only journals. Select a
   system/component for telemetry; if several are present, none is selected
   automatically. Camera-only sessions still support video playback, with
   no telemetry measurements invented.
3. Move the cursor, use **Previous / Next** to jump to an
   captured visual sample or accepted measurement reception, or **Play / Pause** to advance
   at ×0.5, ×1, ×2, or ×4. The slider's native keyboard controls also work. Time
   starts at the beginning of the recording and includes silence before and
   after receptions.
4. Inspect mode, battery, attitude, and NED position, then open reception
   details for admission counters and `time_boot_ms` counters. Rejections are
   those that have already occurred at the cursor, with no data drawn from later
   in the file.
5. Download the verified JSONL or return to **Observation**. Navigation changes
   neither the sources nor an active capture. Capture state remains visible in
   Sessions, with a **Manage capture** shortcut. Leaving
   Sessions or hiding the browser tab pauses replay; unapplied source settings
   are preserved.

**Telemetry views of the same journal.** **Measurements** reconstructs the four telemetry
views at the cursor. **Reception summary at this point** (reception summary at
this instant) gives the counts of messages received, used, ignored, or rejected
up to that instant; it is not a message list. **MAVLink messages** (MAVLink
messages), opened only on request, provides inspection of all recorded frames
in reception order, including other components, types unused by the four
measurement views, and payloads rejected by their caches.

This whole-recording view is independent of the cursor and pauses replay. Its
source and type filters apply only to this view. Pages contain at most 50
messages; each row shows its index in the file, relative time, type,
system/component, and sequence number. Opening a row reveals its decoded fields
with their MAVLink names and units, message identifier, frame version, size,
and original bytes in hexadecimal. Fields are not converted to the units used
in Measurements. To preserve values in JavaScript, integers outside the exact range
±(2^53−1) and non-finite values `NaN`, `Infinity`, and `-Infinity` are displayed as
explicit strings. Bytes remain unchanged. An undecodable frame or one with an
invalid CRC does not enter the recording: this is not a capture of every byte
on the transport.

**History remains separate from live reception.** The current camera feed is
never presented as video from the recording. The latest historical measurements
remain visible even when stale, with their state and age **at the cursor**. A
measurement not yet received remains absent; no points are interpolated.
Admission caches and serialization are shared with Observation, including
rejected NaNs, the distinction between zero and an unavailable value, mode
names, and raw NED coordinates. A delayed response cannot replace a newly
selected file, component, or cursor; during manual seeking, old values are
removed until the new instant is confirmed.

New console recordings retain their UTC start time, original configuration
(declared environment, sources, endpoint actually opened, selected
system/component, and sequence scope), and reception age thresholds. This
context is included in the JSONL and its checksum. **Measurements** replay uses the
recorded thresholds even if the current console uses different settings. The
full context is available in **Analysis**. Wall-clock time comes from
the capture computer; calculations always use the recorded local reception
times.

Older recordings without context remain readable. File modification time never
becomes capture time, and Sessions never borrows sources from Observation.
Without recognized context, the default analysis thresholds remain displayed:
heartbeat 1 s, battery 2 s, attitude 0.2 s, and position 0.4 s. These are reception
age thresholds, not guarantees of physical measurement freshness. Recognized
but malformed ARGOS context is rejected.

**Reception analysis.** The third tab is computed on demand from the completed
recording. It does not control the Measurements cursor. Source and type filters cover
all recorded decoded frames, including messages ignored by the display or whose
values were rejected.

- **Rate**: reception count divided by the duration of each interval. Intervals
  of approximately one second cover the entire capture, including silence;
  longer captures are grouped into at most 160 intervals. The effective width
  is displayed, and exact boundaries are available at the analysis cursor.
  This is not a transmission rate prescribed to the autopilot.
- **Maximum reception age**: the longest time since the last matching reception
  within each interval. The peak before a new reception is preserved. Before
  the first reception, age is unknown; it is not calculated from the beginning
  of the file. It is not the age of the sensor measurement.
- **Periods without reception**: intervals between receptions, the initial
  wait, and the final silence. The duration threshold is set in this view
  (initially 1 s). Only the 100 longest periods are detailed, with the total count
  and truncation stated explicitly. A period without reception does not prove
  packet loss or identify its cause.

A filter with no receptions retains a zero rate and unknown age. No rate can be
calculated for a capture of zero duration. Curves are interval aggregates;
**MAVLink messages** retains exact times and original frames for detailed
inspection. Loading, errors, and file revision are checked; a delayed response
cannot replace another recording or different filters. Leaving Analysis cancels
its request.

The list exposes the **200 most recently modified files** in the configured
directory and reports the total count. Only console-generated filenames—a
32-character hexadecimal identifier followed by `.jsonl`—can be opened. Arbitrary paths, symbolic links, and special files
are not accepted. Web replay and download are limited to **32 MiB and 100,000
frames per file**; older files exceeding these limits remain on disk. New
captures are closed automatically before exceeding these bounds. The interface
has no import or deletion action. Visual sidecars have their own limits below.

Validation and indexing run outside the reception loop. The cache retains at
most one file and one component's index. Each access rechecks the file's identity
and its checksum-bearing ending, even when two writes of the same size have
matching timestamps; a revision ties the cursor and download to the opened
content. A modified file must be reopened. Downloads send the bytes that were
actually verified, without reopening a path that may have changed in the
meantime. Captures active in the process or marked as failed by it are not
offered as completed recordings.

## Visual flight replay

For a first visit without a simulator or camera, follow the
[provided recorded-flight walkthrough](../examples/demo-flight/README.md). It
uses this same console and the files in the source checkout; no live values or
flight commands are simulated by the replay.

Open **Session recording**, leave **Include video and flight events** selected,
and start before the part of the flight you want to retain. Stop, wait for visual
finalization, then open the session in **Sessions → Flight replay**. Video,
matched detection boxes, sampled control/framing state and recent events follow
the same cursor. Replay never sends flight commands. It pauses on navigation,
manual seeking and hidden browser tabs; obsolete requests and image loads are
cancelled when the cursor or selected session changes.

Capture works with recent video even without an open MAVLink link. This supports
an analog receiver connected through a Linux/V4L2 USB capture device, once the
actual adapter has been tested. Analog OSD text remains pixels in the image;
it does not populate the ARGOS telemetry HUD. Telemetry requires a separate
MAVLink connection. Real-camera control remains disabled by the existing
simulation restrictions.

Visual recording samples at **up to 10 Hz**, without encoding a movie or running
extra detector inference. It saves JPEG images already received, and boxes only
when they belong to that exact image and source. With analysis active, retained
images follow completed detector results (currently up to 5 Hz); the sampling
ceiling is not a guaranteed video frame rate. The timeline uses local receipt
and availability times, not synchronized camera exposure timestamps. An image
may have arrived shortly before Start; its original age is preserved. A detection
that completed later never appears at an earlier cursor position. Missing video,
stale images, sampling gaps and media ending before the journal are explicit.
No frames, measurements or boxes are interpolated to fill those gaps.

Control state is sampled, so events between samples can be absent. Discrete
operator requests record service acceptance/refusal; sampled state transitions
are labelled separately. Neither proves execution or replaces autopilot logs.
The event panel shows the latest **50 events at or before the cursor**, with a
count when earlier events are omitted; the downloaded sidecar retains all events
within its capture limit. Owner tokens and request bodies are excluded.

Each session has an unchanged `<id>.jsonl` journal and, when enabled, an
`<id>.visual.sqlite3` sidecar in the same recording directory. Download both from
Sessions to retain the full session. Keep their names together when copying them
to another console's recording directory. The sidecar is bound to the journal's
identifier, original start and run identity. Older journals without it remain
readable; an invalid sidecar does not invalidate independent telemetry replay.
This first version has no MP4 export, audio, bundle import or 3D reconstruction.

A dedicated writer thread receives a bounded queue of **8 items**. It never waits
for disk on the control loop. Queue overflow visibly ends media capture;
telemetry and flight control continue. Visual limits are **256 MiB**, **one hour**,
**36,000 samples**, **20,000 events**, and **2 MiB per JPEG** (at most 4096 pixels
per dimension and 8,388,608 total pixels). Reaching a limit preserves a completed
partial capture and reports its end reason. A write/finalization failure leaves
media unavailable; it never silently starts a replacement recording. Normal Stop
finalizes asynchronously; reopening while it finishes asks you to wait.

Sidecar validation and JPEG reads run outside the reception loop. Files are read
without database writes, with bounded rows, dimensions and counts. Symlinks,
wrong session bindings and incomplete or changed content are rejected. Separate
journal and media revisions keep metadata, seeking, images and downloads tied to
the content opened by the browser.

`POST /api/recordings/start` accepts `{"include_visual": true}`; an empty object
retains telemetry-only behavior for existing clients. Session metadata includes
`visual`. The read-only routes are:

- `GET /api/recordings/{id}/visual?revision=…&visual_revision=…&at=…`
- `GET /api/recordings/{id}/visual/frames/{index}.jpg?revision=…&visual_revision=…`
- `GET /api/recordings/{id}/visual/download?revision=…&visual_revision=…`

## Reception incidents and recovery

In Observation, the permanent **Incidents and recovery**
button, next to Video reception and Session recording, opens reception status even
when there is no incident. It indicates what remains available, missing
measurements, elapsed time since detection, and recovery. A compact banner also
appears when reception needs attention. Shortcuts from **Video** and
**Diagnostics → Link** remain available.

A MAVLink interruption groups its effects on measurements into a single
incident. An interruption affecting only some measurements leaves the others
usable. Measurements never
received are marked absent, without assuming they were being transmitted.
After reopening, a measurement received previously is still expected before
the incident can close. When all reception is absent at startup, sources get a
three-second grace period; stale states must persist for one second to open an
incident. An explicit error is reported immediately. Recovery is confirmed
after one stable second. **These delays apply to notifications: stale images
and measurements are always hidden as soon as their thresholds expire.**

The history retains incident onset and recovery within the observation's
60 events. The recovery banner remains for ten seconds; the latest recovery
remains available in the panel. These incidents are held in memory and are not
added to existing MAVLink recording formats. Failure to reach the local service
makes source availability unverifiable; it is presented separately from a
MAVLink or camera interruption. An action in progress that suspends updates
does not create a false service outage. Recent reception proves neither a
healthy radio link nor a recent physical measurement; no failure cause is
inferred.

**Reopen camera** or **Reopen MAVLink**
keeps the active settings and replaces only the selected receiver while the
other acquisition continues. Opening takes place outside the HTTP loop.
Observation time and history remain shared; the replaced receiver's latest
values and counters reset. A distinct source identity prevents an old image or
inspection response from being attributed to the new connection. A successful
open waits for new receptions before reporting recovery. These buttons do not
apply draft settings in Sources.

The recording must be stopped before deliberately reopening MAVLink. A
transport failure closes telemetry-only captures if storage remains available;
visual sessions can continue without telemetry. A journal file error leaves an
incomplete capture. Reopening does not start a new capture automatically.
Reopening the camera can preserve an ongoing session, with a new video source
identity so old detections cannot label its images. Incompatible concurrent actions are rejected. If a native
V4L2 reader remains blocked, the console refuses to accumulate more readers;
hardware tests remain to be done.

## Live MAVLink inspection

The **Live MAVLink** tab, between Observation and Sessions,
opens a wide view of current receptions. The **Diagnostics → Messages ↗**
shortcut opens the same view. On a computer, the type list and fields occupy
two independently scrolling columns; controls remain above these areas, without
a floating bar covering messages. On small screens, **Received types** (received
types) and **View fields** switch between the list and fields
without requiring a long page scroll. **Back to Observation** (return to
Observation) restores the previous view; capture and draft source settings are
preserved.

The inspector shows message types and identifiers, their system and component,
received count, latest reception age, and approximate rate over three seconds.
This rate falls to zero during silence without waiting for a new message. It
does not measure radio latency or loss.

Source and type/ID filters select a message. Its latest decoded fields and
hexadecimal frame can be inspected, including types ignored and payloads
rejected by the HUD's measurement views. **Freeze** retains an explicitly
dated snapshot for reading values; acquisition and any active recording
continue. **Resume** resumes only this display.

Memory retains at most 256 source/type pairs, with frequency counters in 100 ms
buckets. The least recently received pairs are evicted at the limit; their
counters restart from zero if they return. The eviction count is displayed.
Large integers and non-finite values share the exact serialization used by
MAVLink messages in Sessions. Decoding counters expose invalid bytes,
unsupported frames, and read errors; the invalid bytes themselves are not
captured in this view.

The browser polls this inspector at most twice per second, only while it is
open, visible, and not frozen. Returning to Observation, switching to Sessions,
or hiding the browser tab cancels the request. An old response cannot replace
a new connection; a frozen service or endpoint cannot leave rates presented as
current.

ARGOS provides no fault-injection controls. Simulated outages and delays remain
in development tests. Fault scenarios and the communication proxy will belong
to the separate MAVLink Fault Lab project.

## Code organization and verification

`argos/console/config.py` validates sources; `session.py` owns MAVLink reception
and events; `video.py` acquires and retains the latest image. `recording.py`
owns the JSONL capture; `visual_capture.py` samples existing observations and
`visual_recording.py` writes and reads the bounded visual sidecar;
`archive.py` verifies and indexes telemetry recordings;
`analysis.py` calculates curves and periods without reception; `context.py`
validates the configuration embedded in captures. `views.py` shares the
presentation of admitted measurements and MAVLink fields. `incidents.py` groups
incidents; `live.py` retains the bounded source/type inspector state. `app.py`
exposes local resources,
`GET /api/state`, `GET /api/frame.jpg`, `POST /api/sources`,
`POST /api/recordings/start`, `POST /api/recordings/stop`,
`POST /api/sources/{video|mavlink}/reconnect` (empty JSON object),
`GET /api/mavlink/messages`,
`GET /api/recordings`, `GET /api/recordings/{identifier}`,
`GET /api/recordings/{identifier}/replay?revision=…&at=…&system=…&component=…`
and `GET /api/recordings/{identifier}/download`. The frame view uses
`GET /api/recordings/{identifier}/messages?revision=…&offset=0&limit=50`, with
optional `system`/`component` filters (both required together) and `message_id`.
Analysis uses `GET /api/recordings/{identifier}/analysis?revision=…&bins=160&gap_threshold_s=1`,
with the same filters; the API limits the number of intervals to 1–400,
detailed gaps to 100, and the threshold to a positive value no greater than
86,400 s. The web view adapts `bins` to duration (`ceil(duration_s)`, between
1 and 160). The API accepts 1–100 messages per page and shares revision and
integrity verification with other reads. WOFF2 fonts are served by
`GET /fonts/{filename}` from an explicit allowlist. POST requests require a
JSON body and the exact local console Origin. The source and recording routes
above do not themselves issue pilot commands. When simulation control is enabled,
`control.py` additionally handles the selected vehicle reports, browser lease
and MAVLink transmissions. `POST /api/control/{claim|input|action}` and the
`control` field in `GET /api/state` are described in the
[control API guide](web-control.md#control-api-and-recordings).
HTML, CSS, and JavaScript are
separate files under `static/` and included in the Python package. No frontend
framework, CDN, or build server is needed. OFL licenses and provenance for the
IBM Plex Sans, IBM Plex Mono, and Marcellus fonts are retained under
`static/fonts/`.

The server lifecycle follows FastAPI's [lifespan mechanism](https://fastapi.tiangolo.com/advanced/events/).
The adapter uses [Gazebo Transport Python subscriptions](https://gazebosim.org/api/transport/13/python.html).
The camera adapters publish no camera commands. Opt-in simulation flight commands
use the separate `FlightControl` path.

```sh
python -m pip install -e '.[dev,mavlink,plot,console,console-test]'
python -m pytest -q
node --check argos/console/static/app.js
node --check argos/console/static/sessions.js
node --check argos/console/static/live.js
node --check argos/console/static/analysis.js
node --check argos/console/static/control.js
```

## Deferred work

Observation, Live MAVLink, Sessions and opt-in simulation Flight controls are integrated. Placement, contrast,
and spacing will be refined through testing of this interface. Video from real
hardware and its transport still need to be defined and validated; the ground
SITL + Gazebo test does not validate them.
Radio/HITL integration, autonomous target tracking and GPS-free horizontal
position hold are not implemented by the manual-control panel.

C++ and the custom MAVLink dialect remain deferred as agreed.

## Framing report

Open **Sessions → Flight replay → Framing report** in a completed visual session.
The report is read on demand and uses the existing archive; it does not require
Gazebo, detector inference or connected hardware. The supplied recorded flight
can be used immediately. The separate **Analysis** tab still describes MAVLink
reception timing.

The assistance timeline and durations distinguish Manual, Full framing,
manual-throttle framing, pauses, takeover requests, inactive control and unknown
coverage. These are sampled service states, not measured airborne durations.
Intervals separate changes of target, profile, distance response, size reference
and recorded pause/loss reason. Select an interval, an event or a curve to seek
the corresponding recorded moment. A time slider and **View this moment in
replay** provide the same navigation from the keyboard or touch screen.

The horizontal curve expresses signed center error as a percentage of half the
image width: zero is centered, −100% is the left edge and +100% the right edge.
The apparent-size curve shows `100 × (height − reference) / reference`; zero
matches the selected size. A positive value means a larger box, not a measured
metric distance. The report does not score vertical centering, which belongs to
the pilot in manual-throttle framing. Legacy recordings with no profile or
response value retain that uncertainty.

A sampled control state supports a duration only up to the next sample and the
recorded sample-age limit (currently 0.35 seconds). Longer gaps and time after
visual recording ends are unknown. Curves use valid active framing observations
with recent recorded image evidence and split at gaps or setting changes. They
summarize sampled image measurements; they cannot establish correct identity,
physical tracking accuracy or every transition between samples.

Reports retain at most 1,200 chart points, 1,500 intervals and the latest 500
session/control events. For long sessions, chart reduction preserves signed
extrema in time bins and keeps separate segment identities. Totals use the full
validated sample sequence; reductions and omitted intervals/events are stated
in the interface. Downloading the original session preserves its full archive.

The read-only route is
`GET /api/recordings/{id}/framing-report?revision=…&visual_revision=…`.
It requires both revisions from current session metadata and the same verified
journal/visual binding as replay. Reopen the session if its files have changed.
