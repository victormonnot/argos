# ARGOS FLY: one final Pocket model

This profile connects the continuous `ArgFly` producer to **CH4 yaw**. Its
ordinary manual model supplies throttle, arming, roll, pitch and manual yaw.
SC middle selects manual; displayed **SC↑** permits assistance after a deliberate
middle-to-up cycle with yaw centered. Leave SC at middle for about 0.2 s during
that cycle so the native takeover latch observes its reset.

This is a preparation and installation workflow. A generated file is not proof
that it is installed, nor that the aircraft is ready for assisted flight. The
combined receiver checks follow installation, with propellers removed.

## What changes

Use the normal **manual Pocket model** as the source. Do not use ARGOS RF, whose
diagnostic model fixes throttle and arming low, or repurpose ARGOS VIS/CH32.
The preparer accepts the reviewed AETR mapping and rejects conflicting scripts,
logical switches, custom functions or a nonstandard source layout.

| Setting | Prepared model |
| --- | --- |
| Name | ARGOS FLY |
| CH4 | Original manual yaw row, then Lua1 Val / Replace / L07 / trim off |
| CH7 | One unconditional MAX / −100% / trim off; crash flip disabled |
| LUA1 | ArgFly; Val, Fsh, Seq, Hbt |
| Logical switches | The [L01–L11 native gate](edgetx-yaw-stream.md#mode-2-takeover-policy) |
| RF | Original internal CRSF CH1–16, external OFF |
| Other settings | Original manual rows, receiver identity, Inputs, curves, limits, trims and telemetry retained |

SC no longer requests crash flip. All pilot channels retain their original
sources. The tool never writes RADIO settings, calibration, the active-model
selection or a mounted SD card. It does not change flight-controller settings.

The native gate includes two **0.2 s Duration** fields on L08 and L11. These
stretch takeover events for the native Sticky input's 100 ms sampling interval;
without them a brief stick excursion could disappear before the latch sees it.
L02/L03 heartbeat detector **Duration stays zero**, and L10 persistence stays
off. Yaw greater than 25% for the configured dwell requests latched manual;
greater than 50% also bypasses assistance through native logic. These initial
values still need the Mode 2 throttle-handling check on the actual Pocket.

## Reproduce and inspect locally

From the repository checkout, install the optional parser once:

```sh
.venv/bin/python -m pip install -e ".[radio-profile]"
```

Save the ordinary manual model export as `manual.yml` in a private local
directory. Prepare a **new** output directory; an existing directory is never
overwritten. Use the repository version of ArgFly matching the host checkout:

```sh
.venv/bin/python -m argos.backends.edgetx_profile prepare \
  --source "$HOME/.local/share/argos/manual.yml" \
  --script scripts/edgetx/ArgFly.lua \
  --output "$HOME/.local/share/argos/fly-profile"
```

The output contains `model.yml`, `SCRIPTS/MIXES/ArgFly.lua`, and a `manifest.json`
with source/model/script SHA-256 values and the profile contract. Keep personal
exports and generated models outside Git. `model.yml` is a review artifact; its
name is not an instruction to replace a similarly named radio file.

Validate against the saved source to check both the gate and preservation:

```sh
.venv/bin/python -m argos.backends.edgetx_profile validate \
  --model "$HOME/.local/share/argos/fly-profile/model.yml" \
  --source "$HOME/.local/share/argos/manual.yml"
```

Unknown or duplicate YAML fields that affect the gate fail validation. Original
text outside the changed fields is preserved, including zero-padded masks,
receiver IDs and strings such as `OFF`. An old `semver` in a model export is
serialization metadata; read the installed firmware version on the radio.

## Install once, when the radio is available

1. Keep the aircraft disconnected. On the Pocket, duplicate the ordinary
   manual model once and name the copy **ARGOS FLY**. Select the original model
   again before USB Storage, so the file being replaced is inactive.
2. In USB Storage, save a current backup of MODELS and RADIO. Identify the
   **actual file belonging to ARGOS FLY by its header name**; do not assume a
   model number. Check the current ordinary manual model still matches the
   source used above. If it changed, regenerate from that current source so
   newer pilot settings are preserved.
3. Copy the prepared `model.yml` over that identified, inactive ARGOS FLY file.
   Copy `ArgFly.lua` to SD `SCRIPTS/MIXES/ArgFly.lua`. If a compiled `ArgFly.luac`
   already exists, move that compiled file to the backup so the new source is
   compiled. Do not replace RADIO files, model indexes or another model.
4. Safely eject, restart the radio, then select **ARGOS FLY**, with SC middle
   and the aircraft still disconnected. Confirm ArgFly in LUA1, internal CRSF
   CH1–16 and external OFF. The channel monitor should show ordinary manual
   roll/pitch/throttle/yaw/ARM, CH7 fixed low, and SC must not change CH7.
5. Save/read back the resulting model and ArgFly source. Validate the readback
   and record its hashes. A script/profile hash checked only on the PC does
   not establish which version the radio loaded. Then use USB Serial with
   USB-VCP Lua for the continuous launcher.

No extra diagnostic profile is needed. The first aircraft session uses this
same final profile for the grouped props-off validation: received yaw sign
left/right, throttle movement in Mode 2 without incidental takeover, manual
latch/reset, target loss/reselection, transport reconnect and RF failsafe.
The runtime should count A→T transitions during stable tracking so radio lease
gaps can be distinguished from visual target loss before adjusting timing.

## Scope of verification

```sh
.venv/bin/python -m pytest -q tests/test_edgetx_profile.py
```

The synthetic model tests check preservation, conflict refusal, native gate
fields and malformed YAML without including a private radio backup. They do
not execute the full EdgeTX mixer, RF stack or flight controller.

Encoding references are pinned to EdgeTX 2.12.4:
[Pocket YAML definitions](https://github.com/EdgeTX/edgetx/blob/def35ad324896b45d6607d4778536b1bc5360d20/radio/src/storage/yaml/yaml_datastructs_128x64.cpp),
[source/switch parsing](https://github.com/EdgeTX/edgetx/blob/def35ad324896b45d6607d4778536b1bc5360d20/radio/src/storage/yaml/yaml_datastructs_funcs.cpp),
and [native logical-switch evaluation and timer](https://github.com/EdgeTX/edgetx/blob/def35ad324896b45d6607d4778536b1bc5360d20/radio/src/switches.cpp).
