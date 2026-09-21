# Disarmed EdgeTX / Betaflight RF yaw bench

This diagnostic extends the [native guard bench](edgetx-native-guard-bench.md)
from an unused radio channel to received yaw values in Betaflight. The path is
PC USB → Pocket Lua/native mixer → internal CRSF/ELRS → receiver → Betaflight.
A second USB cable provides **read-only MSP observations** from Betaflight.
There is no motor test, vision input, console integration or flight capability.

Use a Pocket running EdgeTX 2.12.4 and the supported controller identity:
Betaflight 2026.6.0-alpha, MSP 1.48, manufacturer BEFH, board BETAFPVG473_V2.
Version checks do not verify a firmware binary or its build hash. The receiver
must already be bound. The required flight-controller mapping is yaw CH4,
throttle CH3, ARM CH5/AUX1 high, and crash flip CH7/AUX3 high.

**Remove propellers and leave the aircraft battery disconnected throughout.**
The aircraft is powered only by USB. Exit its CLI and disconnect Configurator.
The transmitter stays powered by its own battery. Successful RC readout does
not establish motor direction, a flight failsafe or physical framing.

## Prepare a separate model with the aircraft disconnected

Keep the original flight model and the proven **ARGOS USB** model. Duplicate
ARGOS USB into a new model named exactly **ARGOS RF**; select the copy and
initially keep **both RF modules OFF**. Do not enable RF in ARGOS USB: ArgMix
intentionally refuses to operate with RF enabled.

Copy `scripts/edgetx/ArgRF.lua` to the SD card's `SCRIPTS/MIXES/ArgRF.lua` via
USB Storage, eject properly and unplug. Select **ArgRF in CUSTOM SCRIPTS / LUA1**
of ARGOS RF. Its outputs are **Val, Fsh, Seq, Hbt**, in that order. The native
L01–L07 configuration from the previous bench must remain intact, including
the actual PC selector in L07. Do not change that selector based on an assumed
physical arrow direction. Keep the selector in manual while setting up.

In MIXES, modify **only the new model**:

| Channel | Required configuration |
| --- | --- |
| CH3 throttle | One unconditional row: Source MAX, Weight **−100**, Offset 0, Trim off, no curve, no delay or slow. |
| CH5 ARM | Same single constant-low row. Remove the previous switch source. |
| CH7 crash flip | Same single constant-low row. Remove the previous switch source. |
| CH4 yaw | Preserve its original first manual row, including Input, weight and trim. Insert a **second row after it**: Source LUA1 Val, Weight 100, Offset 0, Trim off, Switch **L07**, Multiplex **Replace**, no curve, delay or slow. |
| CH32 | Delete its two experimental rows in this copy; it is no longer the test channel. |

On the mono screen, long-press a populated MIXES row to access Edit/Insert After.
MAX is the constant source, not a maximum-throttle setting. CH3/5/7 must each
have only that one constant row, with Switch None. Preserve the original CH4
OUTPUTS settings (including its existing curve, limits and direction). Do not
replace the manual CH4 Input with the raw-stick CH32 experiment.

On CHANNELS MONITOR, with RF still OFF, verify CH3, CH5 and CH7 stay at **−100%**
while moving their old controls. Verify CH4 follows the yaw stick in both
directions. Leave the other sticks centered and the throttle stick down.
ArgRF remains inactive while RF is off, so no PC correction is expected yet.

Only after those checks, enable **Internal RF = CRSF**, channels **CH1–CH16**,
using the existing receiver number/binding settings; keep **External RF OFF**.
Do not rebind or alter ELRS/Betaflight firmware for this test. ArgRF checks the
model name, module types/channel range, and final radio outputs for CH3/5/7 on
every callback. These guards cannot operate if Lua itself stops; the already
tested native gate remains required.

## Verify the copied model reaches the receiver

Connect the battery-free, propeller-free controller to the laptop by USB and
keep the Pocket on, selector in manual. Identify **both distinct devices** with
`ls -l /dev/serial/by-id/`; numbering such as ttyACM0 can change. Use their actual
stable paths in the commands below. Existing read-only diagnostics are sufficient:

```sh
.venv/bin/python -m argos.backends.betaflight_probe \
  --port /dev/serial/by-id/YOUR_BETAFLIGHT_DEVICE --samples 20
```

Move **only yaw**, then center it. Check received yaw changes both ways while
roll/pitch stay near center, throttle and AUX1/AUX3 stay near 1000, and the
controller remains DISARMED. The normal USB-only baseline can include
DSHOT_TELEM (bit 20); RXLOSS must be absent. If the copy does not connect, inspect
its receiver number/model-match configuration rather than rebinding blindly.

## Run the finite PC sequence

Keep the aircraft USB attached. Connect the powered Pocket through USB Serial,
with USB-VCP = Lua; do not run a Lua Tools script alongside ArgRF. Select the
same PC switch position already verified in the previous bench and center yaw.
Stop other serial helpers before running this command:

```sh
.venv/bin/python -m argos.backends.edgetx_rf_probe \
  --pocket-port /dev/serial/by-id/YOUR_POCKET_DEVICE \
  --fc-port /dev/serial/by-id/YOUR_BETAFLIGHT_DEVICE
```

With the transfer kit, use its `edgetx_rf_probe.py` file instead of `-m ...`;
keep `betaflight_probe.py` and `edgetx_probe.py` beside it. Only Python 3.11+ and pyserial 3.5
are required; no new vision dependency is involved.

The fixed sequence is **0 → +12.5% → expiry → −12.5% → 0 → expiry**,
with 60 acknowledged messages. Watch **CH4** and the received **yaw** printed by
the host. Output curves, trims and CRSF scaling affect exact receiver numbers;
look for a repeatable positive/negative change and return near center.
The printed RC observation precedes its next SET, so it is not an instantaneous
response to that next command. ACK proves script receipt only. Completion does
not automatically assert that the configured mix or RF path passed.

On another finite run, change to manual during a nonzero phase, then move yaw:
the received value must follow the stick while PC messages continue. Also test
stick takeover with PC still selected by moving yaw clearly outside its central
10%. Recentering permits the PC again while commands remain fresh; select manual
to retain control. This automatic re-entry policy is **bench-only**.

For USB loss, unplug **only the Pocket's USB cable**, keeping the radio powered
and the controller USB connected. Watch CH4 leave the PC value before moving
the stick, then check manual response. The dual-port host stops on that error;
it does not claim a post-disconnection receiver trace. After it exits, the
read-only Betaflight probe can confirm receiver-side manual control. This later
readout is not a measurement of fallback latency. No repeated RF-off held-value
fixture is needed in this RF profile; never install ArgHld there.

Finish with the selector in manual, disconnect the controller, and turn internal
RF OFF in ARGOS RF. Keep the profile for bench work only; its fixed throttle and
ARM deliberately make it unsuitable for flying.

## What the helper enforces

The host checks the supported FC identity and a fresh DISARMED STATUS_EX/RC
batch before its first Pocket write and before every value command. It requires
low throttle, ARM and crash flip, centered roll/pitch without subsequent large
changes, and no arming blockers except the USB-only DSHOT_TELEM flag (zero also
passes). Each guard batch and its age immediately before a write are bounded
to 150 ms, with a separate bounded serial-write timeout; this does not measure
when bytes arrive at the radio.
Consecutive active commands stop if their 250 ms gap budget is exceeded.
Slow USB or an unexpected flag causes a stopped test, not an automatic retry.

MSP traffic uses the existing allowlisted empty read requests. The Pocket
protocol is distinct from the RF-off mixer protocol and accepts only raw
−128/0/128 values, with a maximum of 120 sequence numbers per session. The script
toggles its heartbeat only for accepted new messages and clears freshness after
300 ms without one, when its callbacks continue. It checks final radio outputs
through `getOutputValue`, including OUTPUTS limits and direction.

The helper closes both ports on errors/interruption, sends no cleanup SET, and
never retries or reconnects. Native heartbeat rejection and the first manual
yaw mix provide radio-side fallback; the PC cannot guarantee it by sending a
last zero. Neither this guard nor USB observations establish physical sample
age, a hard real-time bound, full radio-firmware liveness or flight suitability.

Software checks use synthetic serial replies/clocks and restricted Lua API mocks:

```sh
.venv/bin/python -m pytest -q tests/test_edgetx_rf_probe.py
lua tests/edgetx_rf_yaw_test.lua
```

These tests do not run the radio's native mixer or establish RF delivery.

Sources: [EdgeTX 2.12.4 module API](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/lua/api_model.cpp),
[final output API](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/lua/api_general.cpp),
[native mixer](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/mixer.cpp).
