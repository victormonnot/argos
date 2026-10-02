"""Prepare an isolated ARGOS DST model; never write to a mounted radio.

The ordinary Pocket source and existing ARGOS FLY artifact remain unchanged.
Pitch is a bounded experiment, enabled only in SC down + SB up (ANGLE).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import edgetx_profile as yaw

ProfileError = yaw.ProfileError
MODEL_NAME = "ARGOS DST"
SCRIPT_NAME = "ArgDst"
# Transport heartbeat is shared, but native authority is independent per axis.
# Seq is the common receipt marker: Fsh now represents YAW validity, not shared
# transport validity. Using Fsh for L5 would wrongly cancel pitch during yaw input.
# L10/L15 reset on their own raw freshness rising after first observing it low.
# Frozen-high validity cannot reopen an axis when its stick is recentered.
# No amplitude or duration of stick movement sets an SC-only takeover latch.
_GATE_ROWS = (
    ("FUNC_VPOS", "lua(0,3),0", "NONE", 0, 0),  # L01 heartbeat sign
    ("FUNC_AND", "L1,L1", "NONE", 3, 0),
    ("FUNC_AND", "!L1,!L1", "NONE", 3, 0),
    ("FUNC_OR", "L2,L3", "NONE", 0, 0),
    ("FUNC_VPOS", "lua(0,2),0", "!L4", 0, 0),  # L05 receipt + live heartbeat
    ("FUNC_AND", "L5,L9", "NONE", 0, 0),
    ("FUNC_AND", "L11,L12", "!L10", 0, 0),     # L07 yaw replacement
    # No delay. Duration stretches an observed gesture across Sticky's 100 ms
    # sampling; continued stick deflection is independently suppressed by Lua.
    ("FUNC_APOS", "Rud,20", "L12", 0, 2),
    ("FUNC_VPOS", "lua(0,1),0", "NONE", 0, 0), # L09 raw yaw validity
    ("FUNC_STICKY", "L8,L9", "NONE", 0, 0),
    ("FUNC_AND", "L6,!L8", "NONE", 0, 0),
    ("FUNC_OR", "SC0,SC2", "NONE", 0, 0),
    ("FUNC_APOS", "Ele,10", "SC2", 0, 2),
    ("FUNC_VPOS", "lua(0,5),0", "NONE", 0, 0), # L14 raw pitch validity
    ("FUNC_STICKY", "L13,L14", "NONE", 0, 0),
    ("FUNC_AND", "L14,L5", "!L13", 0, 0),
    ("FUNC_AND", "L16,!L15", "NONE", 0, 0),
    ("FUNC_AND", "L17,SC2", "SB0", 0, 0),     # L18 pitch replacement
)
_PITCH_MIX = {**yaw._NEW_MIX, "destCh": "1", "srcRaw": "lua(0,4)",
              "swtch": "L18", "name": "ArgPit"}


def _gate():
    return {str(index): {"func": func, "def": definition, "andsw": andsw,
                         "lsPersist": "0", "lsState": "0", "delay": str(delay),
                         "duration": str(duration)}
            for index, (func, definition, andsw, delay, duration) in enumerate(_GATE_ROWS)}


def _source(source):
    _, model, _ = yaw._source_model(source)
    yaw._require(model["header"]["name"] != MODEL_NAME, "Use the ordinary manual model")
    # ANGLE depends on CH6 = raw SB. Preserve pilot settings but refuse a custom
    # transformation/reversal that would invalidate SB up's known 900..1300 range.
    row = model["mixData"][5]
    yaw._require(row.get("weight", "100") == "100" and row.get("offset", "0") == "0",
                 "CH6/SB must have default weight and offset for ANGLE")
    limits = model.get("limitData", {}).get("5", {})
    yaw._require(isinstance(limits, dict), "Invalid CH6 output limits")
    for field in ("min", "max", "ppmCenter", "offset", "symetrical", "revert", "curve"):
        yaw._require(limits.get(field, "0") == "0", f"CH6 {field} must be default for ANGLE")
    return model


def prepare_profile(source: bytes) -> bytes:
    _source(source)
    base = yaw.prepare_profile(source)
    text, model, root = yaw._parse(base)
    nodes = {key.value: value for key, value in root.value}
    mix_nodes = nodes["mixData"].value
    # Insert directly before original CH3. Original CH2 text remains untouched.
    insertion = mix_nodes[2].start_mark.index
    insertion = text.rfind("\n", 0, insertion) + 1
    # Mapping starts at destCh; the preceding standalone dash belongs to CH3.
    insertion = text.rfind("\n", 0, insertion - 1) + 1
    newline = "\r\n" if "\r\n" in text else "\n"
    text = text[:insertion] + yaw._emit_row(_PITCH_MIX, newline) + text[insertion:]
    text = text.replace('name: "ARGOS FLY"', 'name: "ARGOS DST"', 1)
    text = text[:text.index("logicalSw:")]
    text += "logicalSw:" + newline
    for index, row in _gate().items():
        text += f"   {index}:{newline}"
        for key, value in row.items():
            encoded = value if value.isdigit() else json.dumps(value)
            text += f"      {key}: {encoded}{newline}"
    text += f'scriptsData:{newline}   0:{newline}      file: "ArgDst"{newline}      name: ""{newline}'
    result = text.encode()
    validate_profile(result, source=source)
    return result


def validate_profile(raw: bytes, *, source: bytes | None = None):
    _, model, _ = yaw._parse(raw)
    mixes = yaw._common(model)
    yaw._require(model["header"].get("name") == MODEL_NAME, "Expected model ARGOS DST")
    yaw._require(len(mixes) == 12, "Expected ten manual channels plus yaw and pitch replacements")
    yaw._require(mixes[2] == _PITCH_MIX, "CH2 pitch replacement must use L18 and Pit")
    yaw._require(mixes[5] == yaw._NEW_MIX, "CH4 yaw replacement must use L7 and Val")
    manual = mixes[:2] + mixes[3:5] + mixes[6:]
    for channel, row in enumerate(manual):
        if channel == 6:
            yaw._require(row == yaw._FLIP_MIX, "CH7 must stay fixed-low crash-flip")
        else:
            yaw._manual_row(row, channel, yaw._MANUAL_SOURCES[channel])
    gate = model.get("logicalSw")
    expected = _gate()
    # EdgeTX serializes transient Sticky states even with persistence disabled.
    # Both states may be read back; their reset/set policy remains exact.
    for index, label in (("9", "L10"), ("14", "L15")):
        if isinstance(gate, dict) and isinstance(gate.get(index), dict):
            state = gate[index].get("lsState")
            yaw._require(state in ("0", "1"), f"{label} state must be binary")
            expected[index]["lsState"] = state
    yaw._require(gate == expected, "Native L01-L18 distance gate differs")
    yaw._require(model.get("scriptsData") == {"0": {"file": SCRIPT_NAME, "name": ""}},
                 "Only ArgDst in LUA1 is allowed")
    # Check ANGLE assumptions on readback even when no ordinary source provided.
    angle = manual[5]
    yaw._require(angle.get("weight", "100") == "100" and angle.get("offset", "0") == "0",
                 "CH6/SB ANGLE mapping changed")
    limits = model.get("limitData", {}).get("5", {})
    yaw._require(isinstance(limits, dict), "Invalid CH6 output limits")
    for field in ("min", "max", "ppmCenter", "offset", "symetrical", "revert", "curve"):
        yaw._require(limits.get(field, "0") == "0", f"CH6 {field} must be default for ANGLE")
    if source is not None:
        original = _source(source)
        for key in set(original) | set(model):
            if key not in {"header", "mixData", "logicalSw", "scriptsData"}:
                yaw._require(original.get(key) == model.get(key), f"Source setting changed: {key}")
        yaw._require(model["header"] == {**original["header"], "name": MODEL_NAME},
                     "Receiver/header identity changed")
        for index, row in enumerate(manual):
            if index != 6:
                yaw._require(row == original["mixData"][index], f"Original CH{index+1} changed")
    return {"schema": "argos-edgetx-distance-profile-v3", "model_name": MODEL_NAME,
            "model_sha256": yaw._hash(raw), "source_sha256": yaw._hash(source) if source else None,
            "script": SCRIPT_NAME, "yaw_channel": 4, "pitch_channel": 2,
            "crash_flip_channel": 7, "crash_flip": "disabled",
            "internal_rf": "CRSF CH1–16", "external_rf": "OFF",
            "yaw_switch": "SC↑", "distance_switch": "SC↓", "angle_switch": "SB↑",
            "manual_switch": "SC middle", "native_gate": "L01–L18",
            "yaw_manual_release_percent": 20, "pitch_manual_release_percent": 10,
            "yaw_soft_return_ms": 200, "pitch_soft_return_ms": 200,
            "yaw_return_center_percent": 10, "pitch_return_center_percent": 5,
            "yaw_limit": 205, "pitch_limit": 51, "preservation_checked": source is not None,
            "hardware_validated": False}


def build_artifact(source_path: Path, output_dir: Path, script_path: Path):
    source = source_path.read_bytes()
    model, script = prepare_profile(source), script_path.read_bytes()
    yaw._require(b"ARGOS_DISTANCE_STREAM_V3" in script and b'"ARGOS DST"' in script,
                 "Expected ArgDst experimental script")
    manifest = {**validate_profile(model, source=source), "script_sha256": yaw._hash(script),
                "model_file": "model.yml", "script_file": "SCRIPTS/MIXES/ArgDst.lua"}
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    scripts = output_dir / "SCRIPTS" / "MIXES"
    scripts.mkdir(parents=True)
    (output_dir / "model.yml").write_bytes(model)
    (scripts / "ArgDst.lua").write_bytes(script)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "validate"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            if None in (args.source, args.script, args.output):
                raise ProfileError("prepare requires source, script and output")
            result = build_artifact(args.source, args.output, args.script)
        else:
            if args.model is None:
                raise ProfileError("validate requires model")
            result = validate_profile(args.model.read_bytes(), source=args.source.read_bytes() if args.source else None)
    except (ProfileError, OSError) as exc:
        parser.exit(2, f"Distance profile stopped: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
