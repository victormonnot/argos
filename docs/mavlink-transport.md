# MAVLink transport

The transport layer exchanges MAVLink telemetry over one configured UDP or TCP
peer, or one serial stream. It uses the ArduPilot dialect, decodes MAVLink 1 and 2, and
connects decoded traffic and local write counts to the link instrumentation.

Opening, polling and closing a link send no MAVLink messages. The only write path
is an explicit `send(message, now)` call. Connection setup never writes parameters,
including `ARMING_CHECK`, and does not arm, change mode or configure an autopilot.

## Install and reproduce

```sh
python -m pip install -e '.[dev,mavlink]'
python -m pytest -q
python examples/mavlink_loopback.py
```

The example binds ephemeral localhost ports and exchanges two `HEARTBEAT` frames.
It checks the received payload and byte counts and prints the measurements as JSON.
It does not require SITL, a vehicle, or a serial device. Tests also exercise a local
serial pseudo-terminal on POSIX systems. CI installs the optional MAVLink and plot
extras; plotting support is only needed to export figures.

## Transport configuration

```python
from argos.backends.mavlink import MavlinkLink, SequenceScope, UdpTransport

transport = UdpTransport(local=("127.0.0.1", 14551), peer=("127.0.0.1", 14550))
with MavlinkLink(transport, sequence_scope=SequenceScope.COMPONENT) as link:
    messages = link.poll(now=0.0)
    report = link.report(now=0.0)
```

For a serial connection, provide `SerialTransport(device=..., baudrate=115200)`;
for TCP, use `TcpTransport(peer=("127.0.0.1", 5760))` instead. TCP connection setup
has a two-second timeout; subsequent I/O is nonblocking. An orderly peer shutdown
is reported as a read error, not as an indefinitely empty stream. TCP handshakes
are network traffic, but opening the transport emits no MAVLink message.
The message-processing class stays the same. Transport objects open their
resource at construction; a successfully constructed `MavlinkLink` owns it and
closes it on context exit or an I/O fault. No reconnect happens automatically.

UDP uses IPv4 literals and a fixed peer. It accepts complete MAVLink frames per
datagram; partial frames cannot be joined across separate datagrams. Serial and TCP
preserves an incomplete frame until a later poll completes it. Polling never waits
for a heartbeat and reads at most eight chunks by default (`max_reads`, 1–64), each
at most 65535 bytes. One owner calls the link; concurrent access is unsupported.

## What the measurements mean

`now` is supplied by the caller on its monotonic run clock. Calls, including empty
polls and reports, must have nondecreasing dates at or after `started_at`. Equal
dates are allowed. A malformed send request is rejected before I/O and does not
advance the clock. Invalid or retrograde call dates raise `ValueError` before I/O.

`Received.received_at` is the time the local polling call processes a complete
frame. It is not firmware time, sensor capture time, or a measure of radio latency.
Raw frame bytes and decoded fields are detached from the parser; field mappings
and arrays are immutable. Decoding a field does not validate it for flight use.

`SendStatus.ACCEPTED` means a complete encoded frame was accepted by the **local
transport**. It does not mean the peer received it, acknowledged it or applied it.
A zero-byte write is `BLOCKED`: counted as an attempt, with no queued retry and no
advance of the outgoing sequence counter. Positive partial writes are counted by
their known byte count, return `PARTIAL`, and close the transport. The incomplete
tail is never sent later. A write exception with an unknown byte count returns
`ERROR` with `bytes_written=None`; `unknown_writes` marks TX byte statistics as a
lower bound. No successful frame is inferred from that error.

`LinkReport.traffic` contains the existing rates, sequence audit and historical
silences. Its RX/TX counts cover the configured rolling window, not the full
session. The console maintains its own cumulative reception counter. The TX count
is the count of writes that accepted positive bytes,
including partial prefixes; its silence describes link traffic, not the cadence
of flight commands. `rx_bytes` includes all local input, while `bad_bytes` counts discarded
framing/CRC bytes. Only decoded messages enter the sequence audit. For serial, an
incomplete frame is pending input until completed; it is not a received message.
An unknown message ID increments `unsupported_frames` and closes the link: this
dialect cannot check its CRC, and Pymavlink's unknown-message object does not expose
validated header fields. Measurements then cover only the decoded prefix; a
different dialect must be supported explicitly before continuing that stream.

## Sequence scope is an explicit assumption

Choose `SequenceScope.CHANNEL` when one encoder increments a shared counter across
the peer's whole stream. Choose `SequenceScope.COMPONENT` when each `(sysid, compid)`
has its own counter. The link counts every decoded message before any application
filter; filtering only the returned objects does not invent sequence gaps.

Loss estimates require the complete stream for each counter. A routed stream that
omits messages or merges independent channels does not meet that assumption.
MAVLink documents these limits in its [packet-loss guide](https://mavlink.io/en/guide/packet_loss.html).
The transport configuration cannot discover or prove the remote routing topology.

Message encoding/decoding uses the generated library directly rather than an
autopilot setup helper; see the [Pymavlink guide](https://mavlink.io/en/mavgen_python/).
The tested versions are pinned in the `mavlink` extra. Full experiment/environment
locking remains a separate reproducibility task.

## Inspecting telemetry without a vehicle state estimate

`TelemetryCache` keeps the last valid `HEARTBEAT`, `ATTITUDE` and
`LOCAL_POSITION_NED` from an explicitly selected `(system, component)` pair. Feed
it events **after** `link.poll` so ignored sources and message types remain in the
link's traffic counters:

```python
from argos.backends.mavlink import TelemetryCache, TelemetryLimits

cache = TelemetryCache(
    system=1, component=1,
    limits=TelemetryLimits(heartbeat=1.0, attitude=0.2, local_position_ned=0.4),
)
# These example age limits are diagnostic choices, not flight safety settings.
now = 0.0  # the caller's run clock, also used for link.poll
for event in link.poll(now):
    result = cache.update(event, now)
snapshot = cache.snapshot(now)
```

Each view has a `message` (or `None`), `rx_age` (or `None`) and reception state:
`ABSENT`, `RECENT` at or below its age limit, or `STALE` above it. Values stay
available for inspection after becoming stale. A new heartbeat changes neither
the attitude's date nor the position's date. A delayed consumer keeps the original
`received_at`; calling `snapshot` never refreshes data. Inspect `link.report(now)`
alongside these views: retained samples do not imply an open transport.

`RECENT` means a recent **valid reception**, never a fresh physical measurement.
The raw sender `time_boot_ms` is preserved separately. `boot_progress` compares
only consecutive valid messages of the same type: `FIRST`, `ADVANCED`, `REPEATED`
or `DECREASED`. A decrease may be a restart, reorder or uint32 wrap; the cache does
not choose an explanation. HEARTBEAT has no boot timestamp, so its progress is
`None`. Invalid payloads preserve previous data and dates, increment `rejected`,
and return an explicit rejection. `last_rejection` is a historical diagnostic.
Ignored/rejected events do not advance the admission clock; successful updates
and snapshots do. Invalid or retrograde call times raise before any mutation.

Coordinates and units remain those of the [standard messages](https://mavlink.io/en/messages/common.html#LOCAL_POSITION_NED).
In particular, local NED does not establish an origin at takeoff. No `SelfState`,
`Observation`, `ready` or `flying` value is inferred from this cache.

Run `python examples/mavlink_telemetry.py` for an offline replay with real encoded
frames: missing data, independent reception ages, a CRC-valid NaN payload and a
repeated boot timestamp. The replay opens no network or serial connection.

## Battery and declared mode

The console additionally uses `HealthCache` from `argos.backends.mavlink.health`.
It admits `SYS_STATUS` battery fields from the selected source with an independent
reception-age limit, preserving the last valid sample after a rejection. It maps
the [standard sentinels](https://mavlink.io/en/messages/common.html#SYS_STATUS)
to `None`: uint16 maximum for voltage, −1 for current, and −1 for remaining charge.
Voltage is converted from mV to V; signed current from cA to A. Zero remains a
measured value, and negative currents other than −1 are retained. This view does
not interpret sensor-health flags or represent multiple individual batteries.

`interpret_mode` interprets a validated HEARTBEAT using the installed Pymavlink
ArduCopter mapping only for supported rotorcraft types, ArduPilot autopilot ID,
and an active custom-mode flag. Unknown combinations keep the raw mode ID instead
of borrowing a mode name from another vehicle family. Mode reception expires with
its heartbeat. This is the [mode reported by the autopilot](https://ardupilot.org/dev/docs/mavlink-get-set-flightmode.html),
not a requested change or proof of vehicle readiness.

## Recording and offline inspection

`RecordingWriter` archives decoded receptions before application filtering. The
original frame and `received_at` are retained. Decoded `fields` are reconstructed
on reading, so a CRC-valid NaN is preserved and can reproduce a cache rejection.
The writer checks that event identity/type/sequence match the encoded frame.

```python
from argos.backends.mavlink import RecordingWriter

with open("receptions.jsonl", "xb") as stream:
    writer = RecordingWriter(stream, started_at=0.0)
    for event in received:  # original events from link.poll, in reception order
        writer.append(event)
    writer.finish(ended_at=1.0)  # actual end of observation, >= last received_at
```

Finalization is explicit. Closing a file without `finish` leaves an incomplete
journal. Invalid events are rejected before writing; partial or failed writes
make the writer unusable and prevent a success footer. The caller owns the stream
and its flushing/disk durability. File writes are synchronous; this is an analysis
tool and has no promise of real-time logging performance.

The versioned JSONL format contains a header, reception records, an end record
and a checksum footer. SHA-256 covers all preceding bytes, including the declared
end time. It detects accidental corruption, not an adversary able to rewrite the
file and its checksum. This is an ARGOS journal format, not a MAVLink dialect.

The default writer still produces version 1 without capture context. Passing
`RecordingWriter(..., context={...})` produces version 2: one mandatory
`{"kind":"context","data":{...}}` line follows the header, covered by the same
checksum. The context is a bounded JSON object (16 KiB including the line,
8 nesting levels and 1024 value nodes). The reader returns it immutably in
`Recording.context`, or `None` for version 1. It remains consumer metadata, not
decoded MAVLink fields. New web-console captures use this context for original
source configuration, UTC start date and receipt-age thresholds; older journals
remain readable without inventing this information. Other command-line writers
continue producing version 1 unless they explicitly provide context.

The console additionally selects version 3 with
`RecordingWriter(..., context=context, with_completion=True)`. Its context record
is mandatory (an empty object when omitted), and `finish(now, reason=..., detail=...)`
stores the closure reason and detail inside the checksummed end record.
Supported reasons are `stopped`, `transport_error`, `event_limit`, `size_limit`
and `shutdown`. Readers expose `Recording.end_reason` and `end_detail`; versions
1 and 2 keep `end_reason=None`. The default writer and the existing command-line
capture remain version 1 unless explicitly configured otherwise.

Console capture closes automatically at 100,000 frames or before 32 MiB,
reserving space for the next frame and footer. A reported transport failure or clean
server shutdown closes the already-written prefix with its reason when storage
remains writable. Such a file is complete and inspectable despite its interrupted
acquisition. Disk errors still leave an incomplete file. File integrity and
successful uninterrupted acquisition are separate properties. This console
policy does not change the lower-level `capture_session` failure contract below.
A period without messages alone does not close the capture.

`read_recording(binary_stream, max_events=100_000)` checks the entire file and EOF
before returning immutable events. It rejects invalid dates, unsupported format
or dialect, malformed frames, duplicate JSON keys, truncation, extra trailing
bytes and digest/count mismatch. Lines other than the version 2/3 context are
limited to 1024 bytes. Each reception record holds exactly one complete frame,
up to the [MAVLink maximum of 280 bytes](https://mavlink.io/en/guide/serialization.html).
The event limit bounds in-memory loading and can be set by the caller.

Create and inspect a sample with the same observation scenario as the previous
example, followed by a two-second silence:

```bash
python examples/mavlink_recording.py create-demo /tmp/argos-receptions.jsonl
python examples/mavlink_recording.py inspect /tmp/argos-receptions.jsonl
```

`create-demo` refuses an existing destination. `inspect` opens no transport and
prints a report only after successful validation. Source IDs and the example
reception-age limits can be selected with `--system`, `--component`,
`--heartbeat-age`, `--attitude-age`, `--position-age` and `--max-events`.

Replay feeds events to the cache at their recorded reception times and inspects
it at the recorded end time. It reproduces these receptions and the final silence,
not a live consumer's processing delays or intermediate inspections. The journal
does not contain corrupt bytes discarded before decoding, TX events or proof that
every arrival was recorded. It cannot establish RF loss, latency or vehicle state.
The recorded and current codec versions are both shown for comparison.

## Capturing an incoming session

The same command can record messages received from a fixed UDP peer or serial
device. A finite duration and the peer's sequence-counter scope are required:

```bash
python examples/mavlink_recording.py capture-udp /tmp/argos-udp.jsonl \
  --bind 127.0.0.1:14551 --peer 127.0.0.1:14550 \
  --duration 30 --sequence-scope component

python examples/mavlink_recording.py capture-serial /tmp/argos-serial.jsonl \
  --device /dev/ttyUSB0 --baudrate 115200 \
  --duration 30 --sequence-scope component
```

These addresses and device names are examples: the sender must already be
configured to stream to this receiver. UDP accepts only the configured source
address **and source port**; it does not discover peers. Capture sends no MAVLink
messages, including heartbeat or stream-rate requests. Opening a serial device
still applies the operating system's normal serial configuration.

Elapsed time on one local monotonic clock supplies all polling and end dates.
`--poll-interval` defaults to 0.01 seconds between batches; scheduling, processing
and synchronous writes can take longer. The journal records the actual end, even
when it exceeds the requested duration. Bytes queued before the first poll have
no separately known arrival time.

Duration expiry or **Ctrl-C** finalizes the journal, preserving the entire batch
already returned by a poll and the final interval of silence. An empty session is
valid and explicitly reports zero events. Link, clock or write failure exits with
an error; an unfinished file is not accepted by the offline reader. Forced process
termination or disk failure cannot promise a complete journal. Existing output
files are never replaced. A successful command prints its path, actual end, event
count, stop reason and local received/discarded byte counts as JSON.

The reusable `capture_session` function borrows a new, unused `MavlinkLink` started
at zero and a binary stream. The caller owns their closure; the CLI handles both.
It takes a stop callback for cooperative stopping between batches. An unknown
dialect message closes the link as documented above, leaving capture incomplete.

## Exporting a session graph

Install the optional plotting dependency, then export a validated journal:

```bash
python -m pip install -e '.[mavlink,plot]'
python examples/mavlink_recording.py plot /tmp/argos-receptions.jsonl /tmp/argos-receptions.svg
python examples/mavlink_recording.py plot /tmp/argos-receptions.jsonl /tmp/argos-receptions.png
```

This produces a standalone figure that can be opened in a browser or image viewer.
It requires no display server and opens no transport. For interactive observation
and replay, see the [console guide](console.md).

For the selected `--system` and `--component`, three timelines show absence,
recent reception and stale reception of the last valid HEARTBEAT, ATTITUDE and
LOCAL_POSITION_NED. Rejected payloads, repeated boot timestamps and decreased boot
timestamps have separate markers. Age curves reset only on valid receptions;
rejected values never refresh them. The graph uses the same cache admission rules
and `--heartbeat-age`, `--attitude-age`, `--position-age`, `--max-events` options as
text inspection. Dotted lines show the chosen age limits, not safety thresholds.
Other sources and message types remain in the journal but are omitted from these
three timelines. This figure plots reception timing, not vehicle motion.

The horizontal axis starts at the journal origin and includes its final silence.
A zero-duration session is labelled and gets display padding only. The entire
journal is validated and the figure rendered before creating the output file;
an existing output is refused. Matplotlib is imported only when rendering, so
transport, capture and textual inspection do not require it.

## Boundary of this delivery

This transport and telemetry layer is not a `World` backend. It does not construct
fresh `World` observations or translate guidance policies into vehicle commands.
The console's separate [`FlightControl`](web-control.md) implements a bounded
manual simulation path, with profile checks, command acknowledgement/observation
and browser input expiry. It does not add general autonomous setpoint execution
to this library. Message signing and authentication are not implemented here.
No hardware flight or RF performance is established by the local tests.
