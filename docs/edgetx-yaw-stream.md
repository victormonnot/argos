# Continuous Pocket yaw assistance: software foundation

This is the transport and authority contract for `ARGOS FLY`. The
[one-command workflow](argos-fly.md) joins the console and stream; the
[profile guide](edgetx-fly-profile.md) prepares the radio model. Existing finite
`ArgVis` / CH32 diagnostics keep their own contract. Software tests and a prepared
profile do not establish flight readiness; the combined receiver check remains
necessary.

The scope is horizontal framing with a selected person, using a portable host.
Throttle, arming, roll and pitch remain manual. No MAVLink or aircraft USB link
is needed by the runtime; an MSP observer is useful during the later bench.

Continuous mode uses `yaw = 0.5 * error_x`, limited to ±20% normalized stick,
with the existing ±0.035 image-error deadband. This is a stick command, not a
fixed angular speed: the aircraft's rate profile and flight mode determine the
requested rotation. The finite ArgVis diagnostic remains at gain 0.25/±12.5%.
The increased continuous limit requires the matching V3 script and host. Read
back the installed SD script before using it, then confirm direction, manual
takeover and withdrawal at the new bound with the aircraft disarmed and props
removed. A subsequent short flight must assess overshoot and left/right
oscillation; passing software checks is not a physical stability measurement.

## Runtime contract

The host remains alive while the operator is in manual or the target is briefly
unavailable. It sends at most 10 commands per second, without waiting for an
acknowledgement of each command. A separate source reader supplies a single
replaceable latest value. Slow HTTP, inference or a blocked source reader must
not block the transmission scheduler or renew an old observation. Missed send
slots are skipped, not replayed as a burst.

The source reader polls the compact `/api/vision/yaw-assist/state` at up to20Hz.
Logging uses a separate bounded mailbox/thread, so a slow filesystem cannot
block serial scheduling. Source wall/thread-CPU metrics are available alongside
radio status; they do not measure total inference CPU. `source_poll.last_error`
retains the latest failure message (one line, at most 240 characters), including
after recovery; the error counter shows whether new failures are occurring.

`last_command_timing` associates a valid command's analyzed frame with the exact
sequence acknowledged in `AY1`. Image receipt is translated to an interval on
the host clock using the HTTP request/response times, without comparing clock
origins. The interval stays anchored on repeated reads of the same image.
Reported image-to-SET and image-to-ACK minimum/maximum durations include that
uncertainty; ACK means the host observed the radio status, not aircraft response
or the exact Lua acceptance instant. Capture before application receipt is not
included. Cumulative ACKs supply only the last accepted sequence, never invented
measurements for skipped commands. Diagnostics retain at most 32 pending entries
for less than one second and reset with the session/generation. The ordinary
status logger samples the latest result; it is not an exhaustive latency trace.

The return channel remains necessary: advancing radio status and command
acceptance establish connection health. Losing that health removes assistance;
it does not leave the writer blindly refreshing a nonzero value.

| Situation | Assistance | Recovery |
| --- | --- | --- |
| Fresh selected target, operator enabled | Bounded yaw value | Continue while fresh |
| Selected target centered | Valid assistance with zero yaw | Continue |
| Brief uncertain or missing detection | Invalid assistance; original manual mix | Same selection may recover from new strong images within its short recovery window |
| Selection cleared, changed or expired; source replaced | No assistance | Explicit selection and SC enable cycle |
| Intentional stick takeover | Original manual mix, latched | SC middle, then SC up with stick centered |
| Transport fault, process restart or prolonged loss | No assistance, re-enable required | Healthy connection plus a new SC enable cycle |

`Val=0` and `Fsh=0` means assistance is unavailable. It does **not** force the
final yaw channel to zero: the normal manual mix supplies that channel. A valid
centered target can instead have `Val=0` with `Fsh=1024`. Link heartbeat and
assistance validity are separate facts.

The console exposes a stable `selection_epoch` in addition to its existing
preview revision. Brief pauses change the revision but preserve the selection
epoch; explicit selection, clearing and terminal loss invalidate the epoch.
Existing finite diagnostics keep their original fail-stop behavior. A track ID
is an image association, not a guarantee of human identity. Continuous mode
retains a reference for3seconds of uncertain detections in otherwise fresh
images. Reassociation to a new track additionally requires a single strong
candidate, conservative appearance/geometry support and two distinct images.
`selection_id` retains the original association key across such a recovery.
Multiple candidates during loss, expired memory and stale/missing imagery remove
authority; a single visible stranger is not automatically accepted.

The source also requires two distinct strong images after a pause before
restoring authority. If another pause follows the first strong image, its
window is bounded by that image's receipt time plus three seconds. Repeated
polls and weak images cannot renew it; correction stays withdrawn until the
new gap has its own two-image confirmation.

A strong detection can still lack usable appearance evidence, for example when
its crop touches the image edge. Such an image does not erase the last usable
appearance reference. That reference retains its original box and image receipt
time and expires after three seconds; later detections without appearance do not
renew it. Recovery logs distinguish missing reference and candidate descriptors
and record the reference age. Recovery still requires a usable current candidate
and all similarity, geometry and two-image checks.

A fresh radio enable generation requests center selection asynchronously. The
server keeps a current valid target, or chooses the closest confident person
unless two candidates are nearly tied. The request is bound to the camera/run
and radio session/generation; only its matching completed response may adopt a
new selection without a second SC cycle. Delayed results cannot authorize a
different generation. A failed selection requires another deliberate cycle.
An explicit matching selection response can clear an exhausted source recovery
even when the server still tracks the same target. This retains the existing
image-order and immutable-deadline checks; a failed or delayed response cannot
reset them or authorize a later read.

## Mode 2 takeover policy

The initial software policy treats yaw deflection **greater than 25% for at
least 200 ms** as deliberate takeover. Deflection **greater than 50%** requests
immediate native manual priority. Both latch manual control until an explicit
SC middle-to-up cycle. Smaller movements or a shorter excursion past 25% do
not latch a takeover. These are starting values for the combined hardware
check, not measured ergonomics for a particular operator.

The old `|Rud| < 10` gate must not remain on the final assisted mix. Otherwise
it can still interrupt assistance while the Mode 2 pilot adjusts throttle,
regardless of the new Lua policy. Read the raw Rud source, not a processed
channel with a curve or trim. Lua also reads the native takeover latch so a
large movement between Lua callbacks cannot silently re-enable on recentering.

The following is the **profile contract**, not an instruction to modify a
currently connected radio. The final profile installer/guide must verify these
unused rows, sources and mappings before any RF test:

| Row | Function | V1 | V2 | AND switch | Delay | Duration |
| --- | --- | --- | --- | --- | --- | --- |
| L01 | a>x | Lua1 Hbt | 0 | None | 0 | 0 |
| L02 | AND | L01 | L01 | None | 0.3 s | 0 |
| L03 | AND | !L01 | !L01 | None | 0.3 s | 0 |
| L04 | OR | L02 | L03 | None | 0 | 0 |
| L05 | a>x | Lua1 Fsh | 0 | !L04 | 0 | 0 |
| L06 | AND | L05 | !L11 | None | 0 | 0 |
| L07 | AND | L06 | SC up | !L10 | 0 | 0 |
| L08 | \|a\|>x | Raw Rud | 25 | None | 0.2 s | **0.2 s** |
| L09 | OR | L08 | L11 | SC up | 0 | 0 |
| L10 | Sticky | L09 | SC middle | None | 0 | 0 |
| L11 | \|a\|>x | Raw Rud | 50 | None | 0 | **0.2 s** |

L08 and L11 stretch a completed takeover event long enough for the native
Sticky input, which is sampled on the 100 ms timer, to observe it. Without this,
a short large movement between Lua callbacks can return to center before L10
sees it. Heartbeat detector durations remain zero; adding a duration there would
wrongly expire the stale-output condition. Pause briefly at SC middle (about
0.2 s) before SC up so the native latch also observes its reset.
L09's SC-up condition prevents manual yaw from setting L10 while SC middle is
already held. Sticky resets on an input edge, not continuously while its reset
input is true; allowing that manual set would block the next enable cycle.
L10 persistence is off. L10 is read with zero-based logical
switch index 9. The normal manual yaw mix remains unconditional; the last Lua
replacement is selected by L07. Model-specific direction, limits, curves and
trim require review on CH4. The final model keeps crash flip disabled on CH7,
separate from SC. The software reads but does not write model configuration,
throttle or arming channels.

Native timer granularity is 100 ms. These nominal delays are not a measured
physical response-time bound. The native guard requires the radio mixer to
remain alive; it is not the aircraft's RF-loss failsafe.

## Compact stream protocol

The script announces `ARGOS_YAW_STREAM_V3`. The host writes nothing before
recognizing that greeting. Lines are ASCII, LF-terminated and bounded to 64
bytes of content. The version is distinct from every existing bench protocol.

```text
AB1 <host-session>
AY1 <host-session> <generation> <ticket> <accepted-sequence> <state> <cause>
AS1 <host-session> <generation> <ticket> <sequence> <valid> <value>
```

`AB1` starts a fresh host session and removes command authority. `AY1` is radio
status; it includes a radio-issued ticket, not a host timestamp. `AS1` is a
newest-state command. Sessions use eight lowercase hexadecimal characters;
`00000000` denotes an unbound radio. Sequence numbers increase within a session.
Tickets and generations are cyclic 31-bit unsigned counters (0..2147483647);
sequences use 1..2147483647 and require a new session before exhaustion. Integer
clock arithmetic also handles EdgeTX's signed clock rollover. The script uses
Lua 5.3 integer operations, including on EdgeTX's 32-bit Lua build.
Values are in -205..205 (`round(0.20 * 1024)`, approximately 20% stick travel), and invalid
assistance has value zero. States are M (manual / enable required), T (enabled
without valid assistance), A (active) and F (fault / enable required).

The single-character cause is S (start/session), M (manual switch), W (new enable
waiting), A (active), T (invalid target), E (ticket expiry), P (pilot takeover),
L (silence), I (serial I/O), G (model guard), C (clock), or O (parser overflow).
V1 and V2 peers receive no host writes. The host counts **observed** A→T transitions by reported
cause, including T versus E, for the combined hardware check; missed status
reports may hide transitions. The300ms ticket lease has not been widened.

The radio remembers only four issued tickets. Each ticket expires 300 ms after
**radio issuance**, even if its command arrives later. Output expiry is tied to
that same deadline: receipt of an old command cannot give it another 300 ms.
Tickets belong to the current enable generation; SC re-enable and new host
sessions invalidate old generations. This avoids synchronized PC/radio clocks
and per-command stop-and-wait while rejecting delayed queued commands.

The parser has bounded line storage and callback work. It publishes only the
newest accepted command from a batch. Heartbeat changes once for that published
batch, so two commands cannot cancel each other's heartbeat transition.
Duplicates, expired tickets and old generations cannot sustain freshness.

The host still limits the source image age to 450 ms at emission. The ticket
window is a **separate residual lifetime**, not a guarantee of output stopping
at the image's original 450 ms deadline. Lua expiry is evaluated on callbacks;
the native heartbeat guard covers a producer that stops updating while the
native mixer continues. Neither mechanism measures camera sensor latency or
proves a hard real-time physical cutoff.

## Targeted software verification

Tests cover brief occlusion/recovery, expired or changed selection, a blocked
source, delayed or missing reports, stale USB queues, disconnect/reconnect,
session and enable-generation changes, bounded framing, heartbeat batches, and
Mode 2 takeover thresholds/duration. They use the actual Python and Lua logic
with small deterministic fixtures; no recorded-flight replay campaign or broad
scheduler search is required.

Run the targeted contract tests with Lua 5.3 or newer on `PATH`:

```sh
python -m pytest -q tests/test_console_yaw_preview.py tests/test_yaw_stream_source.py tests/test_edgetx_yaw_stream.py tests/test_yaw_stream_lua.py
```

The last file drives the actual script through a small process adapter and runs
the Lua policy harness. Without Lua it is explicitly skipped; CI installs Lua.
The standalone entry point is `python -m argos.backends.edgetx_yaw_stream --help`.
Its optional `--duration` bounds a bench run; ordinary runtime has no duration
limit. Use the linked final-profile and launcher guides for setup; this document
is not a first-flight validation.

## Remaining combined hardware acceptance

Use the final profile and runtime for one grouped preparation session, with
propellers removed and the battery connected for camera power. Check current
receiver mapping/rates/failsafe and observe the received yaw with MSP. In
particular:

- With SC up and the target tracked, dose throttle as in flight without
  intentional yaw. Assistance must remain enabled.
- Compare the vision command with the known manual left-yaw stick direction
  in Betaflight: a target left of the image must request that same left-yaw
  direction; a target right must request right yaw. Check for image mirroring
  and radio output reversals before flight.
- Check short target loss/recovery, deliberate stick takeover and SC re-enable,
  camera loss, USB loss/reconnection, and radio-link loss.
- A disarmed MSP/RXLOSS observation does not prove armed motor cutoff. Include
  the appropriate props-off RF failsafe verification separately within the
  same preparation session.

Receiver sign verification precedes flight. The first short manual hover and
low-authority assisted activation still need to establish physical response,
gain and closed-loop stability. Normalized yaw percent is not degrees/second.
