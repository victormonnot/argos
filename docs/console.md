# ARGOS observation console

A local console for receiving the drone camera feed and telemetry, configuring
sources, and recording MAVLink. The interface is in French, works without
external web resources, and displays the entire image.
The **Observation** view uses the graphite Forge style: a main camera area,
measurements grouped under their source, and a side inspector. Fonts are bundled
with the application. The **Sessions** view lets you find local recordings,
verify their integrity, and replay their telemetry in the browser.

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
be listening; the console connects without sending any MAVLink message. A refused
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

- **Caméra embarquée** (onboard camera): the complete image, its source, and its
  reception age. It is hidden when reception expires, stops, or can no longer be
  verified.
- **Réception vidéo** (video reception): the control at the top opens sensor
  diagnostics. Its state remains independent of telemetry.
- **Télémétrie MAVLink** (MAVLink telemetry): the parent button sits directly above
  the four measurement previews. Its neutral highlight marks the open group;
  the red accent identifies only the selected measurement.
- **Mesures** (measurements): Mode, Batterie (battery), Attitude, and Position
  locale (local position) each have a preview and a corresponding inspector
  section. The common source and selectors stay in place; only the selected
  details change. Liaison (link) and Événements (events) are grouped separately
  under **Diagnostics**: they show reception details and an event log, rather
  than vehicle measurements. Each quantity has its own age and state. An absent
  or stale value appears as a dash, never as a measured zero.
- **Sources**: a separate settings button opens the environment, camera, MAVLink
  transport, and selected component in the **Configurer les sources** (configure
  sources) panel. Edit the fields and select **Appliquer** (apply) to restart the
  receivers and create an observation. Draft settings survive refreshes and
  navigation to other panels; the Sources button retains the **modifiées**
  (modified) indicator. Canceling the changes reloads the active settings without
  sending a request to the service.
- **Journal MAVLink** (MAVLink recording): a single button in the top bar opens
  the recording panel. **Démarrer** (start), **Arrêter** (stop), and **Télécharger**
  (download) are grouped at the top of this panel, followed by the identifier,
  duration, message count, and any errors. Capture state remains visible on the
  top button while another panel is open. Capture covers subsequent MAVLink
  receptions, not video. The panel refers to this process's latest recording.
  In Sessions, new recordings expose their stored original configuration; older
  recordings without context explicitly indicate that this information is
  unavailable. The current configuration is never presented as their provenance.
- **Événements**: a history limited to the last 60 camera and MAVLink events in
  this observation. This visual history does not replace the persistent MAVLink
  recording.
- **Vue étendue / plein écran** (expanded view / full screen): enlarge the camera
  by reducing peripheral details. Opening an inspector restores the panel.
  Controls support keyboard navigation; Escape closes the panel and returns
  focus to its trigger. Native browser keys remain available in inputs and
  selectors.

Image and telemetry states are separate. A responsive interface does not prove
that either is being received. Statuses use text labels as well as colors. On a
laptop, the camera, its status, and display controls fit on one screen; the
inspector context stays in place while its details scroll. On narrow screens,
the panel moves below Observation and the page scrolls normally. Opening details
does not resize the camera; **Vue étendue** explicitly enlarges it.

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
under **Sessions → Emplacement des journaux** (recording location). It is
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
JSONL recording and its integrity check. An error reported by the MAVLink
transport closes the receptions already written with an explicit end reason:
they remain downloadable and available for analysis. Capture also stops
automatically at **100,000 frames** or before **32 MiB**, reserving space to
finish the file. The reason is visible; no subsequent capture starts
automatically. A period without messages does not, by itself, close the capture.

Normal server shutdown attempts to close the capture with the service shutdown
reason. **Terminé** (completed) describes a correctly finalized file, not a
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
received battery messages. Web replay includes those battery measurements;
video recording is not provided. See the
[recording limits](mavlink-transport.md#recording-and-offline-inspection).

## Sessions and replay

1. Open **Sessions**, then select a recording. The list shows its size and file
   modification date, which is not the capture start time. **Actualiser**
   (refresh) rereads the directory. A file is only presented as valid after its
   frames, ending, and checksum have all been verified.
2. Select the system/component found in the frames. If the recording contains
   multiple components, none is selected automatically. An empty file remains
   inspectable and downloadable, with no measurements to replay.
3. Move the cursor, use **Précédente / Suivante** (previous / next) to jump to an
   accepted measurement reception, or **Lire / Pause** (play / pause) to advance
   at ×0.5, ×1, ×2, or ×4. The slider's native keyboard controls also work. Time
   starts at the beginning of the recording and includes silence before and
   after receptions.
4. Inspect mode, battery, attitude, and NED position, then open reception
   details for admission counters and `time_boot_ms` counters. Rejections are
   those that have already occurred at the cursor, with no data drawn from later
   in the file.
5. Download the verified JSONL or return to **Observation**. Navigation changes
   neither the sources nor an active capture. Capture state remains visible in
   Sessions, with a **Gérer la capture** (manage capture) shortcut. Leaving
   Sessions or hiding the browser tab pauses replay; unapplied source settings
   are preserved.

**Three views of the same file.** **Mesures** reconstructs the four telemetry
views at the cursor. **Bilan des réceptions à cet instant** (reception summary at
this instant) gives the counts of messages received, used, ignored, or rejected
up to that instant; it is not a message list. **Messages MAVLink** (MAVLink
messages), opened only on request, provides inspection of all recorded frames
in reception order, including other components, types unused by the four
measurement views, and payloads rejected by their caches.

This whole-recording view is independent of the cursor and pauses replay. Its
source and type filters apply only to this view. Pages contain at most 50
messages; each row shows its index in the file, relative time, type,
system/component, and sequence number. Opening a row reveals its decoded fields
with their MAVLink names and units, message identifier, frame version, size,
and original bytes in hexadecimal. Fields are not converted to the units used
in Mesures. To preserve values in JavaScript, integers outside the exact range
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
context is included in the JSONL and its checksum. **Mesures** replay uses the
recorded thresholds even if the current console uses different settings. The
full context is available in **Analyse** (analysis). Wall-clock time comes from
the capture computer; calculations always use the recorded local reception
times.

Older recordings without context remain readable. File modification time never
becomes capture time, and Sessions never borrows sources from Observation.
Without recognized context, the default analysis thresholds remain displayed:
heartbeat 1 s, battery 2 s, attitude 0.2 s, and position 0.4 s. These are reception
age thresholds, not guarantees of physical measurement freshness. Recognized
but malformed ARGOS context is rejected.

**Reception analysis.** The third tab is computed on demand from the completed
recording. It does not control the Mesures cursor. Source and type filters cover
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
**Messages MAVLink** retains exact times and original frames for detailed
inspection. Loading, errors, and file revision are checked; a delayed response
cannot replace another recording or different filters. Leaving Analyse cancels
its request.

The list exposes the **200 most recently modified files** in the configured
directory and reports the total count. Only console-generated filenames—a
32-character hexadecimal identifier followed by `.jsonl`—can be opened. Arbitrary paths, symbolic links, and special files
are not accepted. Web replay and download are limited to **32 MiB and 100,000
frames per file**; older files exceeding these limits remain on disk. New
captures are closed automatically before exceeding these bounds. This release
includes no import, deletion, or video recording.

Validation and indexing run outside the reception loop. The cache retains at
most one file and one component's index. Each access rechecks the file's identity
and its checksum-bearing ending, even when two writes of the same size have
matching timestamps; a revision ties the cursor and download to the opened
content. A modified file must be reopened. Downloads send the bytes that were
actually verified, without reopening a path that may have changed in the
meantime. Captures active in the process or marked as failed by it are not
offered as completed recordings.

## Reception incidents and recovery

In Observation, the permanent **Incidents et reprise** (incidents and recovery)
button, next to Réception vidéo and Journal MAVLink, opens reception status even
when there is no incident. It indicates what remains available, missing
measurements, elapsed time since detection, and recovery. A compact banner also
appears when reception needs attention. Shortcuts from **Vidéo** (video) and
**Diagnostics → Liaison** remain available.

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

**Réouvrir la caméra** (reopen camera) or **Réouvrir MAVLink** (reopen MAVLink)
keeps the active settings and replaces only the selected receiver while the
other acquisition continues. Opening takes place outside the HTTP loop.
Observation time and history remain shared; the replaced receiver's latest
values and counters reset. A distinct source identity prevents an old image or
inspection response from being attributed to the new connection. A successful
open waits for new receptions before reporting recovery. These buttons do not
apply draft settings in Sources.

The recording must be stopped before deliberately reopening MAVLink. A
transport failure closes the recording already written if storage remains
available; a file error leaves an incomplete capture. Reopening does not start
a new capture automatically. Reopening the camera can preserve an ongoing
MAVLink recording. Incompatible concurrent actions are rejected. If a native
V4L2 reader remains blocked, the console refuses to accumulate more readers;
hardware tests remain to be done.

## Live MAVLink inspection

The **MAVLink en direct** (live MAVLink) tab, between Observation and Sessions,
opens a wide view of current receptions. The **Diagnostics → Messages ↗**
shortcut opens the same view. On a computer, the type list and fields occupy
two independently scrolling columns; controls remain above these areas, without
a floating bar covering messages. On small screens, **Types reçus** (received
types) and **Voir les champs** (view fields) switch between the list and fields
without requiring a long page scroll. **Retour à Observation** (return to
Observation) restores the previous view; capture and draft source settings are
preserved.

The inspector shows message types and identifiers, their system and component,
received count, latest reception age, and approximate rate over three seconds.
This rate falls to zero during silence without waiting for a new message. It
does not measure radio latency or loss.

Source and type/ID filters select a message. Its latest decoded fields and
hexadecimal frame can be inspected, including types ignored and payloads
rejected by the HUD's measurement views. **Figer** (freeze) retains an explicitly
dated snapshot for reading values; acquisition and any active recording
continue. **Reprendre** (resume) resumes only this display.

Memory retains at most 256 source/type pairs, with frequency counters in 100 ms
buckets. The least recently received pairs are evicted at the limit; their
counters restart from zero if they return. The eviction count is displayed.
Large integers and non-finite values share the exact serialization used by
Messages MAVLink in Sessions. Decoding counters expose invalid bytes,
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
owns the capture file; `archive.py` verifies and indexes recordings;
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
JSON body and the exact local console Origin; they modify receivers and the
recording without sending MAVLink messages. HTML, CSS, and JavaScript are
separate files under `static/` and included in the Python package. No frontend
framework, CDN, or build server is needed. OFL licenses and provenance for the
IBM Plex Sans, IBM Plex Mono, and Marcellus fonts are retained under
`static/fonts/`.

The server lifecycle follows FastAPI's [lifespan mechanism](https://fastapi.tiangolo.com/advanced/events/).
The adapter uses [Gazebo Transport Python subscriptions](https://gazebosim.org/api/transport/13/python.html).
These adapters publish no flight or camera commands.

```sh
python -m pip install -e '.[dev,mavlink,plot,console,console-test]'
python -m pytest -q
node --check argos/console/static/app.js
node --check argos/console/static/sessions.js
node --check argos/console/static/live.js
node --check argos/console/static/analysis.js
```

## Deferred work

Observation, MAVLink en direct, and Sessions are integrated. Placement, contrast,
and spacing will be refined through testing of this interface. Video from real
hardware and its transport still need to be defined and validated; the ground
SITL + Gazebo test does not validate them.

C++ and the custom MAVLink dialect remain deferred as agreed.
