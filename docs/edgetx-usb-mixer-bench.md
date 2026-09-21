# EdgeTX USB mixer bench

This is the next bench step after the [USB display exchange](edgetx-usb-display.md).
A PC sends a fixed sequence of small values to a Lua mixer script. An unused
radio channel shows 0%, +25%, 0%, -25%, then 0%. The aircraft stays disconnected
and **both radio modules are OFF in a dedicated copy of the model**.

This is not flight control. It does not connect vision to yaw, touch aircraft
configuration, or provide an independent flight failsafe. The script only reads
model settings; channel configuration is performed explicitly by the operator.

## Prepare a separate model

Keep the aircraft disconnected from USB and its battery. Leave the original
flight profile intact.

1. In **MDL / Model Select**, copy the existing model to a free slot with **Copy
   Model**. Then select the **copy** as the active model.
2. In its **SETUP** page, set the name to exactly **`ARGOS USB`** (uppercase, one
   space). Set **Internal RF = OFF** and **External RF = OFF** in this copy.
   The script checks that name and both module types on each callback. These
   are configuration checks, not an electrical measurement or RF interlock.
3. Inspect **CH32** in this copied model. Use it only if there is no existing
   mix and its output settings are neutral/default: no offset, reverse or curve,
   limits -100% and +100%. Stop to review the existing setup if it is occupied.
   Do not replace an existing channel or attach this script to a flight axis.

Copying a model does not by itself disable RF. Check both OFF settings before
installing or selecting the new script. Keep the physical aircraft disconnected
even when the script's configuration guard succeeds.

## Install and select the mixer script

1. With the radio powered on, connect its USB data port in **USB Storage** mode.
   Copy `scripts/edgetx/ArgMix.lua` to **`SCRIPTS/MIXES/ArgMix.lua`** on the SD
   card, then safely eject and unplug. This is a mixer file, separate from the
   earlier `SCRIPTS/TOOLS/ArgosUSB.lua` display tool.
2. Keep `ARGOS USB` selected. Open its **CUSTOM SCRIPTS** page, select an empty
   slot (normally **LUA1**), press the roller, and set **Script = ArgMix**.
   If the slot is already occupied, do not overwrite it. The outputs are
   **Val**, **Fsh** and **Seq**. Val should initially be 0.
3. In **MIXES**, add one mix on the unused **CH32** with **Source = the `Val`
   output of that Lua slot**, **Weight = 100**, **Offset = 0**, no switch, no
   curve, no delay or slow. Leave the flight channels untouched. Select the
   named output in the menu rather than assuming a numerical source ID.
4. Return to the main view, use PAGE to reach **CHANNELS MONITOR**, and use the
   roller to show channels **25–32**. CH32 is the last row. If the view says
   MIXERS MONITOR, click the roller to switch to CHANNELS MONITOR.
5. Keep **SYS > HARDWARE > USB-VCP = LUA**, with other Lua serial ports disabled.
   Connect USB and select **USB Serial (VCP)**. Return to the channel monitor.

Do not launch ArgosUSB or another standalone tool during this run: on the
Pocket, that reloads/suspends the mixer-script environment. ArgMix runs as a
model mixer script; it does not have a separate Tools screen.

## Start the fixed PC sequence

Use the existing Python environment with `pyserial==3.5`, on the computer
physically connected to the radio. Identify the radio's own serial port:

```sh
.venv/bin/python -m argos.backends.edgetx_mix_probe --list-ports
```

Then explicitly select it, for example:

```sh
.venv/bin/python -m argos.backends.edgetx_mix_probe --port /dev/ttyACM0
```

The standalone bundle requires `edgetx_mix_probe.py` and `edgetx_probe.py` to
stay in the same folder. From that folder, use the installed ARGOS venv:

```sh
"$HOME/argos/.venv/bin/python" ./edgetx_mix_probe.py --port /dev/ttyACM0
```

Replace the example port with the actual radio path; do not select an aircraft.
No device is auto-selected. The helper waits for the exact mixer-bench greeting
before writing, establishes a session, and sends only the fixed 60-message test.
There are no arbitrary channel/value options, reconnects or retries. Ctrl+C
closes the serial connection; no extra cleanup commands are sent.

Watch **CH32** while reading the PC phases: 0%, +25%, a pause to observe return
to 0%, -25%, then 0%. The PC requires matching acknowledgements and two expiry
reports. It cannot observe the final channel: successful ACKs must be paired
with the actual CHANNELS MONITOR result. Report unexpected movement or a value
that remains nonzero, keeping RF off and the aircraft disconnected.

## Check a physical USB disconnect

After the nominal pattern succeeds, keep the same RF-off model and disconnected
aircraft. Run the helper again and unplug USB at the PC during the +25% phase,
leaving the radio powered and its channel monitor visible. CH32 should return
promptly to zero and remain there. A serial error or timeout at the host is
expected after this deliberate disconnect. Record the observed channel behavior;
the host error alone does not demonstrate neutralization or measure its delay.
This checks loss of the physical USB link while Lua can still run, not a stopped
interpreter.

## Check manual takeover on the unused channel

This second configuration uses CH32 to exercise a native radio-switch handover
between the yaw stick and the PC value. Keep **ARGOS USB**, both RF modules OFF
and the aircraft disconnected. It changes only the bench channel, not a flight
axis. SC is a bench selector here, not a selected flight-assistance switch.

On the MIXES page, long-press the existing CH32 Val row and select **Insert
Before**. Configure the new first row as **Source = Rud** (the raw stick source,
not a numbered Input), **Weight = 100**, **Offset = 0**, **Trim unchecked**,
and **Switch unset**. Keep curves, delays and slow settings off.

Return to the list, long-press the original Val row, select **Edit**, then set
**Switch = SC up** and **Multiplex = Replace** (possibly abbreviated REPL).
The Multiplex field is available after the first row has been inserted. Keep
Val's weight at 100 and its offset, curves, delays and slow settings at zero.
The result must contain exactly these two CH32 rows, in this order:

| Order | Source | Weight | Switch | Multiplex |
| --- | --- | --- | --- | --- |
| First | Raw Rud, Trim unchecked | 100 | None | Add/default |
| Second | Lua1 Val | 100 | SC up | Replace |

The native mixer evaluates the physical switch and skips the replacement when
it is off, leaving the preceding Rud value. This does not require a Lua call to
perform the handover. The radio's native mixer must still be functioning.

1. With SC in the middle and no PC test running, move the yaw stick left/right.
   CH32 should follow it and return near zero when centered.
2. Center the stick, put SC up and run the same finite PC helper. During +25%
   or -25%, move SC to the middle: CH32 should immediately follow the stick,
   even as the PC continues sending. Move the stick briefly to verify control.
3. Keep SC in the middle through the remaining PC phases. Those phases should
   no longer move CH32; the helper's acknowledgements can still complete.

Report the channel behavior, not just ACKs. In this intermediate configuration,
moving the stick alone does **not** cancel the replacement while SC is up;
SC must be moved to manual. USB loss while SC remains up returns the selected
Lua value to zero, not to the stick. Automatic fallback to the stick and expiry
independent of Lua still require a separate gate. Do not enable RF with this
bench configuration. Leave SC in the middle when finished.

## Timeout scope

On a running script, no valid new SET for 300 ms returns Val and Fsh to zero
and emits one IDLE report. Invalid frames and repeated/older sequence numbers
do not renew that interval. The interpreter is not a real-time scheduler: output
updates and checks happen only when `run()` is called.

**A stopped or suspended Lua interpreter may leave stored values.** This script's
timeout and model guard cannot act when it is not running. Ordinary script errors
and global interpreter failure have different native handling, and reading a
Lua output with `getValue` does not itself establish freshness. Do not use this
bench timeout as an independent expiry mechanism for flight commands. Pilot
priority and expiry that survives script failure require separate design and
validation before a physical yaw test.

At the end, confirm CH32 is zero, stop the PC helper, unplug USB, and leave the
bench model's RF OFF. After the manual-takeover check, put SC in the middle and
center the yaw stick; CH32 should be near zero. Select the original flight model
when leaving the bench;
do not turn on RF in the bench copy as a shortcut to an aircraft test.

## Wire contract and software checks

LF-terminated ASCII frames are bounded. The greeting is
`ARGOS_USB_MIX_BENCH_V1`. A BEGIN carries an eight-character lowercase hex session
token and resets output; its reply is READY with the same token. Each SET carries
that token, a strictly increasing canonical sequence number 1–120 and one of
`-256`, `0`, `256`. ACK echoes those fields. IDLE echoes the current session and
last sequence after expiry. Tokens separate runs; they are not authentication.

The Lua source returns Val (-256/0/256), Fsh (0/1024) and Seq. These are returned
values, not calls to channel/model setters. A wrong model name, active/missing RF
module configuration, getter error or missing serial API resets local state and
returns three zeros without announcing readiness. All these checks require the
script to run. No aircraft or original model file is supplied or modified.

```sh
.venv/bin/python -m pytest -q tests/test_edgetx_mix_probe.py
lua tests/edgetx_usb_mix_test.lua
```

Sources: [model copying](https://manual.edgetx.org/bw-radios/model-select),
[model name and RF setup](https://manual.edgetx.org/bw-radios/model-select/setup),
[output scale](https://luadoc.edgetx.org/lua-api-programming/output-table-syntax),
[Lua mixer lifetime](https://luadoc.edgetx.org/overview/script-types/mixes-scripts),
[2.12.4 getters](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/lua/api_model.cpp),
[native mixer checks](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/mixer.cpp),
[insert-before menu](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/gui/common/stdlcd/model_mixes.cpp),
[mix editor](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/gui/128x64/model_mix_edit.cpp).
