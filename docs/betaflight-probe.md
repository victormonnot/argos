# Betaflight USB readout

This finite command-line probe reads a Betaflight controller over an explicitly
selected USB serial port. It displays firmware identity, reported arming state,
attitude and processed receiver channels. It is a bench diagnostic, separate
from the web console, MAVLink recording and flight assistance.

The supported wire layout is **MSP protocol 0 / API 1.48**. Other API versions or
non-Betaflight firmware are rejected before sampling. Compatibility is checked
against upstream source and synthetic serial exchanges; a successful automated
test is not evidence that a particular aircraft has been tested.

## Start

Install the optional serial dependency in the ARGOS environment:

```sh
.venv/bin/python -m pip install -e '.[msp]'
```

Existing installations with the `mavlink` extra already include the same pyserial
dependency. Neither a firmware update nor a receiver-protocol change is needed
when the controller already exposes MSP on USB.

For the first bench check, remove propellers, leave the flight battery unplugged
and connect the controller by USB. Keep the radio's arm switch off. Exit the
Betaflight CLI and **disconnect Configurator** before opening the probe. A CLI
prompt is not an MSP connection; the probe will not send `exit`, reboot the
controller, or take over a port from another program. It does not impose an
arming lock on the aircraft.

List ports on the computer physically connected to the controller:

```sh
.venv/bin/python -m argos.backends.betaflight_probe --list-ports
ls -l /dev/serial/by-id/
```

Listing reads port metadata and opens no device. Select the actual controller
port; do not assume its number or select the video capture adapter. For example,
if the controller is `/dev/ttyACM0`:

```sh
.venv/bin/python -m argos.backends.betaflight_probe \
  --port /dev/ttyACM0 --samples 20
```

A stable `/dev/serial/by-id/...` path can be used instead. The port is opened at
115200 baud with finite write/read deadlines; on POSIX an advisory exclusive lock
also prevents a second cooperating instance from opening it. Close other port
users even when an exclusive lock is available. The probe requests DTR/RTS low;
USB driver and device behavior on open remain hardware-dependent.

The default run prints 20 samples with at least 0.5 seconds between them, then
closes the port. `Ctrl+C` stops early. Use `--json` for JSON Lines, including an
identity record, samples, and a final `end` record on successful completion.
Errors go to stderr and return a nonzero exit code; partial output is not a
complete successful run. `--samples` is bounded to 1–300, `--interval` to
0.2–10 seconds and each `--timeout` to 0.05–5 seconds. These bounds make the run
finite; no actual sample rate is promised.

For transfer to a computer that does not yet have this repository revision,
`argos/backends/betaflight_probe.py` also runs as a **standalone file**, with only
Python 3.11+ and `pyserial==3.5` required:

```sh
.venv/bin/python /path/to/betaflight_probe.py --port /dev/ttyACM0
```

## Read the result

Verify the firmware and board identity first. Gently rotating the disarmed
airframe should change the reported angles. With an already connected radio,
moving one stick at a time should change its corresponding receiver value.
The RX must actually be powered: a USB connection to the flight controller does
not establish receiver power or RF connection.

Angles are roll/pitch/yaw in degrees. **MSP receiver values are already mapped
to roll, pitch, yaw, throttle, then AUX channels**. A radio configured as AETR
still yields yaw before throttle in this response; applying the radio map a
second time would mislabel those axes. These values are the controller's
processed `rcData`, which can contain held/default/failsafe values. Receiving
them does not establish fresh radio input.

Reported arming state uses the ARM permanent ID from `MSP_BOXIDS` page zero,
then the corresponding dynamic status bit. Other active mode IDs shown cover
only that first page. Arming-disable flags remain an uninterpreted bitmask whose
meaning depends on the firmware. A reported `DISARMED` state is an observation,
not a command or a guarantee about later state.

JSON records carry separate local receipt offsets for status, attitude and RC;
the fields are requested sequentially and are not one synchronized snapshot.
There are no FC sample timestamps here. Those offsets measure neither sensor
age nor radio latency.

## Wire scope and failures

Every outgoing packet is an empty MSPv1 read request from this fixed list:
API_VERSION (1), FC_VARIANT (2), FC_VERSION (3), BOARD_INFO (4), RC (105),
ATTITUDE (108), BOXIDS (119), STATUS_EX (150). No payload argument, arbitrary
command option, configuration write, arming request or override is exposed.
Read-only means **queries are transmitted**, not that the serial port is silent.

Fragmented replies and leading noise are handled; checksums, error responses and
payload lengths are checked. Jumbo/MSPv2 replies are outside this probe's scope.
Timeout, partial write, checksum failure or malformed data stops the run and
closes the port. It never retries or reconnects: MSPv1 has no transaction sequence
that could distinguish a late reply to a previous request. On timeout, check the
selected port, USB access and whether the CLI/Configurator still owns the link.

Public protocol references used for the decoder:
[MSP serialization at 26b3b6761](https://github.com/betaflight/betaflight/blob/26b3b6761/src/main/msp/msp.c),
[serial framing](https://github.com/betaflight/betaflight/blob/26b3b6761/src/main/msp/msp_serial.c),
[mode identifiers and packing](https://github.com/betaflight/betaflight/blob/26b3b6761/src/main/msp/msp_box.c).
