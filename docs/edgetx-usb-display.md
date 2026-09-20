# EdgeTX USB display test

This bench tool checks a PC-to-radio USB serial exchange with an EdgeTX Lua
script. The radio displays a counter; the PC requires an acknowledgement for
each numbered message. It does not read or alter model settings, mix channels,
send RC commands, control an aircraft, or integrate with the web console.

The implementation targets EdgeTX 2.12.x and the Pocket's 128x64 display.
Software checks use a restricted Lua runtime and synthetic serial exchanges;
these are not evidence of a working USB link on a physical radio. The tool's
greeting identifies this diagnostic protocol, not the firmware or radio model.

## Prepare the radio

Keep the aircraft disconnected throughout. The radio's normal RF output is not
disabled by this tool. Preserve the existing radio/model configuration and check
the selected model after a firmware update before later aircraft tests.

1. Connect the powered radio through its USB **data** port and select **USB
   Storage**. Copy `scripts/edgetx/ArgosUSB.lua` to `SCRIPTS/TOOLS/ArgosUSB.lua`
   on its SD card. No model file needs replacing.
2. Eject the storage device normally, then unplug USB.
3. In **SYS > HARDWARE > Serial Port**, change **USB-VCP** to **LUA**. Press the
   roller on the value, turn it to choose LUA, then press again to validate.
   Record the previous value for restoration. On the Pocket bench setup AUX1
   stays OFF; another port configured for Lua can claim the same serial API.
4. In **SYS > Tools**, run **ArgosUSB**. It should show `WAITING FOR PC`.
5. Plug in the USB data cable and choose **USB Serial (VCP)**. Return to/open
   ArgosUSB if the connection menu left it. Keep the tool visible during the test.

Storage and joystick modes are not this serial connection. If the tools list
does not show the script, it can also be executed from **SYS > SD CARD > SCRIPTS
> TOOLS > ArgosUSB.lua > Execute**. Do not assign it as a mixer or special function.

## Run on the connected PC

The Python helper needs Python 3.11+ and `pyserial==3.5`. Existing ARGOS
installations with the `mavlink` or `msp` extra already have this dependency;
otherwise install it in the project's virtual environment:

```sh
.venv/bin/python -m pip install 'pyserial==3.5'
```

On the computer physically connected to the **radio**, enumerate serial devices:

```sh
.venv/bin/python -m argos.backends.edgetx_probe --list-ports
ls -l /dev/serial/by-id/
```

This lists metadata without opening devices. Select the Pocket's port; do not
reuse an aircraft's saved USB path or assume the same `/dev/ttyACM` number.
Close other serial terminal programs. For example, if the radio is `/dev/ttyACM0`:

```sh
.venv/bin/python -m argos.backends.edgetx_probe --port /dev/ttyACM0
```

For a second computer without this repository revision, the Python file also
runs standalone with the same dependencies:

```sh
.venv/bin/python /path/to/edgetx_probe.py --port /dev/ttyACM0
```

The helper listens for up to five seconds before transmitting anything. After
the script's exact greeting, it sends 20 numbered pings, half a second apart,
with a two-second deadline per acknowledgement. `--samples` accepts 1–120;
Ctrl+C exits early. Failure or unplugging ends the run; there is no retry or
automatic reconnection. The port closes on completion, interruption and errors.
Serial opens at 115200 baud with finite writes, DTR/RTS requested low, and a
POSIX advisory exclusive lock. Device/driver behavior on open remains external.

## Expected result

The radio changes to `RECEIVING`, its `Received` count increases and `Last`
shows the numbered message. The PC prints `ACK 1/20` through `ACK 20/20`, then
`Completed 20 display exchanges`. This verifies processing by the Lua tool;
it does not verify RF transport, stick calibration, flight control or latency.

After 1.5 seconds without a valid ping, the still-running tool displays
`NO RECENT MESSAGE`. This is a display status, **not a flight failsafe**: a stopped
script cannot update the screen. RTN/EXIT closes the tool. Unplug USB and restore
the earlier USB-VCP setting when finished if needed for other workflows.

If the PC times out before the greeting, it sends no ping. Check the selected
device, visible ArgosUSB tool, USB-VCP=LUA and USB Serial connection mode. A Lua
error or `Serial API missing` should be reported as displayed; do not switch
receiver protocol or flash aircraft firmware to fix this USB-only check.

## Protocol and local checks

All messages are short ASCII lines terminated by LF:

| Direction | Message |
| --- | --- |
| Radio to PC, every 0.5 s while tool runs | `ARGOS_USB_DISPLAY_V1` |
| PC to radio, counter 1–120 | `ARGOS_USB_PING 1` |
| Radio to PC, matching counter | `ARGOS_USB_ACK 1` |

There is no arbitrary payload or executable instruction field. The greeting is
a diagnostic guard against an accidentally selected port/mode, not authentication.
The Lua parser caps work and retained input, rejects noncanonical/out-of-range
messages and discards oversized lines through their delimiter. Only time, LCD
and serial APIs are used; no model, telemetry or channel write API is called.

```sh
.venv/bin/python -m pytest -q tests/test_edgetx_probe.py
lua tests/edgetx_usb_display_test.lua
```

The Lua harness runs with Lua 5.2 and an explicit mocked API allowlist. It checks
fragmentation, noise/overflow, replies, display aging and exit; it is not an
EdgeTX hardware simulator.

Primary references: [EdgeTX hardware settings](https://manual.edgetx.org/bw-radios/radio-settings/hardware),
[2.12.4 serial Lua API](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/lua/api_general.cpp),
[2.12.4 monochrome hardware editor](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/gui/common/stdlcd/radio_hardware.cpp).
