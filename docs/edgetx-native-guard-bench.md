# EdgeTX native stale-output bench

This extends the [unused-channel mixer bench](edgetx-usb-mixer-bench.md).
It configures native EdgeTX logical switches to select the manual stick when
the PC/Lua producer stops changing its heartbeat. A separate fault fixture
deliberately keeps a nonzero correction and an asserted freshness flag, so the
native gate must remove the correction even though Lua never returns it to zero.

**RF-off bench only:** use the copied model **ARGOS USB**, both RF modules OFF,
and leave the aircraft disconnected from USB and battery. Use only CH32, with
neutral/default output settings. No flight channel, firmware, vision control
or aircraft command path is changed. These steps target Pocket / EdgeTX 2.12.4.

## Install the updated scripts

Stop the PC helper. With the powered radio in USB Storage mode, copy these files
to the SD card's SCRIPTS/MIXES directory, then safely eject and unplug:

- `scripts/edgetx/ArgMix.lua`: the PC mixer, now with a fourth output **Hbt**.
- `scripts/edgetx/ArgHld.lua`: the intentionally held-output fault fixture.

Keep ArgMix selected in CUSTOM SCRIPTS / LUA1. Reload it after replacing the
file and check that its outputs are **Val, Fsh, Seq, Hbt**, in that order.
The first three retain their earlier positions. Lua1 sources appear with a
boxed 1 followed by the output name. If Hbt is missing, correct the script
installation before configuring the gate.

The existing CH32 setup should have raw **Rud** first (weight100, offset0,
Trim unchecked, no switch) and **Val** second (weight100, offset0, Replace),
without curves, delay or slow on either row. Note the current Val **Switch**
setting before changing it. Reuse that same physical PC position in L07 below;
the example is **SC down for PC, SC middle for manual**. Do not infer the
position from an arrow alone: verify the actual switch behavior.

## Configure the native gate

On MDL / LOGICAL SWITCHES, use seven unused rows. The table assumes **L01–L07
are empty**; do not overwrite other logic. If other rows must be used, every
reference must be remapped consistently. Select each row, open Edit and fill
in Func, V1, V2, AND switch, Delay and Duration as follows:

| Row | Func | V1 | V2 | AND switch | Delay | Duration |
| --- | --- | --- | --- | --- | --- | --- |
| L01 | `a>x` | Lua1 Hbt | 0 | None | 0 | 0 |
| L02 | AND | L01 | L01 | None | **0.3 s** | 0 |
| L03 | AND | **!L01** | **!L01** | None | **0.3 s** | 0 |
| L04 | OR | L02 | L03 | None | 0 | 0 |
| L05 | `a>x` | Lua1 Fsh | 0 | **!L04** | 0 | 0 |
| L06 | `\|a\|<x` | **Raw Rud** | **10** | L05 | 0 | 0 |
| L07 | AND | L06 | **PC switch position** | None | 0 | 0 |

`!` means NOT: select the negated logical-switch entry. Use the source **Rud**,
not a numbered Input or the CH32 output. The 10 threshold is approximately 10%
of raw stick travel. Put 0.3 in **Delay**, not Duration. All Duration fields stay
zero; a Duration on a delta condition is not a timeout renewed by every change.

L01 follows heartbeat polarity. L02/L03 detect either polarity staying constant;
their OR also detects Hbt held at zero. L05 requires both declared freshness
and no stale heartbeat, L06 requires the yaw stick near center, and L07 adds
the operator's PC selector. These evaluations and delays run in the native
mixer, independently of Lua callbacks, while that native mixer remains healthy.

Finally, in MIXES, edit **only the second CH32 row (Val)**:
replace its physical Switch setting with **L07**. Keep **Multiplex = Replace**.
Raw Rud stays first and unconditional. When L07 is false, the native mixer skips
the replacement, so CH32 follows Rud rather than selecting a fixed zero.

## Check live commands and manual fallback

Use CHANNELS MONITOR / 25–32, not just the Lua output page. Keep the yaw stick
centered initially. With the PC switch in manual, CH32 must follow the stick.
Select PC and, with USB-VCP=LUA and USB Serial selected, run the existing helper
using the radio's actual serial port:

```sh
.venv/bin/python -m argos.backends.edgetx_mix_probe --port /dev/ttyACM0
```

Use the verified device path for this radio, not an aircraft or the example
path blindly. No new Python helper or dependency is needed.

Observe the normal 0%, +25%, 0%, -25%, 0% pattern with a centered stick.
Moving the selector to manual should hand CH32 to Rud while PC ACKs continue.
With the selector still on PC, moving yaw clearly outside the central 10%
should also give Rud control. **Recentering automatically permits the PC again
if commands are fresh**; leave the selector in manual when you want to keep
control. This automatic re-entry is only the current bench policy.

For physical USB loss, start another finite run and unplug USB during a nonzero
phase without moving the selector or stick. CH32 should leave the PC value;
with the stick centered, it should return near zero. The host disconnect error
is expected. Now move the stick and check manual response with PC still selected.
This is a changed configuration: fallback is to Rud, not an active Val=0 mix.

## Test a deliberately held producer

Stop the PC helper and unplug USB. Keep the aircraft disconnected and RF OFF.
In CUSTOM SCRIPTS, temporarily replace **ArgMix with ArgHld in the same LUA1
slot**. It has the same four output names and indices. Keep the logical switches
and CH32 sources intact. ArgHld does not need a PC connection and has no serial
I/O or model/channel setters.

In ArgHld's script settings, **Pol=1** means the heartbeat will be held positive;
**Pol=0** means it will be held negative. Selecting/reloading the script or
changing Pol restarts the exercise:

1. For the first **10 seconds**, ArgHld returns Val=256 (+25%), Fsh=1024 and a
   heartbeat alternating every 100 ms. With PC selected and Rud centered, CH32
   should show +25%. Return to CHANNELS MONITOR in time to observe this phase.
2. After that, ArgHld keeps **Val=256 and Fsh=1024**, and holds Hbt at the chosen
   polarity. Without touching the selector or stick, CH32 should fall back to
   near zero after the native delay. **If CH32 remains at +25%, the gate has
   failed this case**; retain RF OFF and review the configuration.
3. Leave PC selected and move yaw: CH32 should now follow the stick. Check the
   CUSTOM SCRIPTS output page: the Pocket displays **Val=25.0, Fsh=100.0**,
   Seq about 0.1, and **Hbt fixed at +100.0 or -100.0**. These retained values distinguish native rejection
   from a producer that simply zeroed its own output.
4. Change Pol to exercise the other held polarity, center Rud and repeat. Report
   the +25% phase, subsequent fallback and retained-output values for both cases.

The Pocket scales Lua outputs to percentages on the script page as well: the
raw returned 256/1024 values appear as 25.0/100.0. Merely
seeing zero without first seeing +25% does not establish a working healthy path.
If navigation took too long, change Pol and return to the monitor to restart the
10-second observation window. Do not interpret a missing/erroring script whose
outputs become zero as a successful held-output test.

When finished, put the selector in manual, restore **ArgMix in LUA1** and verify
its four output names. Leave RF OFF. ArgHld is fault injection and must not be
left as the active producer for ordinary USB tests.

## Scope and software verification

ArgMix toggles Hbt only when it accepts a new SET; sequence gaps still cause one
toggle, and repeated/invalid messages or ordinary callbacks do not sustain it.
BEGIN, guard failure and its scheduled expiry clear Hbt and Fsh. This does not
make arbitrary incoming vision data fresh: the current helper sends a fixed
10 Hz bench pattern, and a future controller must enforce its own image age.

Native delay timers have 100 ms granularity. The configured 0.3 s delay is not
a measured end-to-end latency or a hard real-time bound. On initialization, a
producer already holding Fsh high can be selected during the initial detection
interval; this gate does not require a complete heartbeat cycle before first
activation. Lost/aliased heartbeat changes may conservatively reject commands.

ArgHld simulates **held inputs to the native mixer**, not an actual interpreter
crash or a hung radio. The fixture keeps executing its RF/name guards while its
outputs are deliberately fixed. The native gate requires the EdgeTX mixer and
its timers to continue; it cannot protect against that firmware stopping. No
flight-mode transition, RF delivery, aircraft yaw correction, flight re-entry
policy or complete flight failsafe is established by this RF-off bench.

```sh
lua tests/edgetx_usb_mix_test.lua
lua tests/edgetx_usb_hold_test.lua
.venv/bin/python -m pytest -q tests/test_edgetx_mix_probe.py tests/test_edgetx_probe.py
```

The Lua harnesses check accepted-message heartbeat behavior and held-output
fixtures with restricted mocked APIs. They do not execute the native radio
mixer. Physical channel observations are still required for the configuration.

Sources: [logical-switch settings](https://manual.edgetx.org/bw-radios/model-select/logical-switches),
[native delay evaluation](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/switches.cpp),
[native mixer](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/mixer.cpp),
[script output display](https://github.com/EdgeTX/edgetx/blob/v2.12.4/radio/src/gui/128x64/model_custom_scripts.cpp),
[Lua VALUE input](https://luadoc.edgetx.org/lua-api-programming/input-table-syntax),
[Lua output scale](https://luadoc.edgetx.org/lua-api-programming/output-table-syntax).
