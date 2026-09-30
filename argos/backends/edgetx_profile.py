"""Prepare one reviewed Pocket flight-model copy, without touching a radio.

Only the supported ordinary manual AETR Pocket layout is accepted. This is not
an arbitrary EdgeTX model converter. Original text outside the listed edits is
retained, including receiver identity, output curves, trims and YAML scalars.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


class ProfileError(ValueError):
    """The model does not meet the reviewed profile contract."""


MODEL_NAME = "ARGOS FLY"
SCRIPT_NAME = "ArgFly"
_MANUAL_SOURCES = ("I3", "I1", "I2", "I0", "SA", "SB", "SC", "SD", "SE", "P1")
_GATE_ROWS = (
    ("FUNC_VPOS", "lua(0,3),0", "NONE", 0, 0),
    ("FUNC_AND", "L1,L1", "NONE", 3, 0),
    ("FUNC_AND", "!L1,!L1", "NONE", 3, 0),
    ("FUNC_OR", "L2,L3", "NONE", 0, 0),
    ("FUNC_VPOS", "lua(0,1),0", "!L4", 0, 0),
    ("FUNC_AND", "L5,!L11", "NONE", 0, 0),
    ("FUNC_AND", "L6,SC0", "!L10", 0, 0),
    ("FUNC_APOS", "Rud,25", "NONE", 2, 2),
    ("FUNC_OR", "L8,L11", "NONE", 0, 0),
    ("FUNC_STICKY", "L9,SC1", "NONE", 0, 0),
    ("FUNC_APOS", "Rud,50", "NONE", 0, 2),
)
_NEW_MIX = {
    "destCh": "3", "srcRaw": "lua(0,0)", "carryTrim": "1",
    "mixWarn": "0", "mltpx": "REPL", "delayPrec": "0", "speedPrec": "0",
    "flightModes": "000000000", "weight": "100", "offset": "0",
    "swtch": "L7", "delayUp": "0", "delayDown": "0", "speedUp": "0",
    "speedDown": "0", "name": "ArgYaw",
}
_FLIP_MIX = {**_NEW_MIX, "destCh": "6", "srcRaw": "MAX", "weight": "-100",
             "mltpx": "ADD", "swtch": "NONE", "name": "FlipLo"}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse(raw: bytes) -> tuple[str, dict[str, Any], Any]:
    try:
        import yaml
    except ImportError as exc:
        raise ProfileError('Install the profile dependency: pip install -e ".[radio-profile]"') from exc
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 2_000_000:
        raise ProfileError("Expected a nonempty model file smaller than 2 MB")
    try:
        text = raw.decode("utf-8")
        # Model exports need no aliases, custom tags or executable constructors.
        for token in yaml.scan(text):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken,
                                  yaml.tokens.TagToken)):
                raise ProfileError("YAML aliases, anchors and tags are not supported")
        node = yaml.compose(text, Loader=yaml.BaseLoader)

        def convert(current: Any, depth: int = 0) -> Any:
            if depth > 32:
                raise ProfileError("Model YAML is nested too deeply")
            if isinstance(current, yaml.ScalarNode):
                return current.value
            if isinstance(current, yaml.SequenceNode):
                return [convert(item, depth + 1) for item in current.value]
            if isinstance(current, yaml.MappingNode):
                result = {}
                for key, value in current.value:
                    if not isinstance(key, yaml.ScalarNode) or key.value in result:
                        raise ProfileError("Model YAML has a complex or duplicate key")
                    result[key.value] = convert(value, depth + 1)
                return result
            raise ProfileError("Invalid model YAML")

        model = convert(node)
        if not isinstance(model, dict):
            raise ProfileError("Expected a model YAML mapping")
        return text, model, node
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise ProfileError("Cannot read model YAML") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProfileError(message)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"Missing or invalid {field}")
    return value


def _common(model: dict[str, Any]) -> list[dict[str, str]]:
    _mapping(model.get("header"), "header")
    modules = _mapping(model.get("moduleData"), "moduleData")
    internal = _mapping(modules.get("0"), "internal RF module")
    _require(internal.get("type") == "TYPE_CROSSFIRE"
             and internal.get("channelsStart", "0") == "0"
             and internal.get("channelsCount") == "16",
             "Internal RF must already be CRSF CH1–16 in the manual source model")
    external = _mapping(modules.get("1", {}), "external RF module")
    _require(external.get("type", "TYPE_NONE") == "TYPE_NONE", "External RF must be OFF")
    _require(set(modules) <= {"0", "1"}, "Unexpected RF module index")
    for field in ("customFn", "swashR"):
        _require(not model.get(field), f"Review {field} before preparing this profile")
    for field in ("modelLSDisabled", "modelCustomScriptsDisabled"):
        _require(model.get(field, "GLOBAL") in ("GLOBAL", "OFF"), f"Review {field} before preparing")
    mixes = model.get("mixData")
    _require(isinstance(mixes, list) and all(isinstance(row, dict) for row in mixes),
             "Expected mixData as a list")
    inputs = model.get("expoData")
    _require(isinstance(inputs, list) and len(inputs) == 4
             and all(isinstance(row, dict) for row in inputs), "Review nonstandard pilot Inputs first")
    for index, source in enumerate(("Rud", "Ele", "Thr", "Ail")):
        matches = [row for row in inputs if row.get("chn") == str(index)]
        _require(len(matches) == 1 and matches[0].get("srcRaw") == source,
                 f"Input I{index} must keep its original {source} mapping")
    # A hard-low mix can be reversed or offset by output settings. Refuse that.
    limits = _mapping(model.get("limitData", {}), "limitData")
    flip = _mapping(limits.get("6", {}), "CH7 output settings")
    for field in ("min", "max", "ppmCenter", "offset", "symetrical", "revert", "curve"):
        _require(flip.get(field, "0") == "0", f"CH7 {field} must be neutral/default")
    return mixes


def _manual_row(row: dict[str, Any], channel: int, source: str) -> None:
    _require(row.get("destCh") == str(channel) and row.get("srcRaw") == source,
             f"CH{channel + 1} must retain its manual {source} mix")
    for key, expected in (("mltpx", "ADD"), ("swtch", "NONE"),
                          ("flightModes", "000000000")):
        _require(row.get(key, expected) == expected, f"CH{channel + 1} must be unconditional")
    # Additional fields are retained. A Lua/LS reference outside the new gate
    # could introduce authority in another channel and requires manual review.
    _require(not any("lua(" in str(value) or re.fullmatch(r"!?L\d+", str(value))
                     for value in row.values()), "Existing Lua/logical mix needs review")


def _source_model(raw: bytes) -> tuple[str, dict[str, Any], Any]:
    text, model, node = _parse(raw)
    _require(model["header"].get("name") not in ("ARGOS RF", "ARGOS USB", "ARGOS VIS", MODEL_NAME)
             if isinstance(model.get("header"), dict) else False,
             "Use the original ordinary manual model, not an ARGOS bench or flight copy")
    mixes = _common(model)
    _require(len(mixes) == len(_MANUAL_SOURCES), "Expected the reviewed ten-channel manual layout")
    for channel, source in enumerate(_MANUAL_SOURCES):
        _manual_row(mixes[channel], channel, source)
    for field in ("logicalSw", "scriptsData"):
        _require(field not in model, f"Source {field} must be unused; do not overwrite existing logic")
    return text, model, node


def _gate() -> dict[str, dict[str, str]]:
    return {str(index): {"func": func, "def": definition, "andsw": andsw,
                         "lsPersist": "0", "lsState": "0", "delay": str(delay),
                         "duration": str(duration)}
            for index, (func, definition, andsw, delay, duration) in enumerate(_GATE_ROWS)}


def _emit_row(row: dict[str, str], newline: str) -> str:
    lines = [" -"]
    for key, value in row.items():
        encoded = value if re.fullmatch(r"-?\d+", value) else json.dumps(value)
        lines.append(f"   {key}: {encoded}")
    return newline.join(lines) + newline


def prepare_profile(source: bytes) -> bytes:
    """Return the final model copy. Source bytes and every other file are untouched."""
    text, original, root = _source_model(source)
    newline = "\r\n" if "\r\n" in text else "\n"
    nodes = {key.value: value for key, value in root.value}
    header = nodes["header"]
    name = next(value for key, value in header.value if key.value == "name")
    sequence = nodes["mixData"]
    # Sequence-item marks start at the mapping, not its dash. Work on the
    # exported sequence text so every original manual row stays byte-for-byte.
    start, end = sequence.start_mark.index, sequence.end_mark.index
    sequence_text = text[start:end]
    chunks = re.split(r"(?m)(?=^[ ]*-[ ]*\r?$)", sequence_text)
    chunks = [chunk for chunk in chunks if chunk.strip()]
    _require(len(chunks) == len(original["mixData"]), "Unsupported mixData text layout")
    rebuilt = []
    for index, chunk in enumerate(chunks):
        rebuilt.append(_emit_row(_FLIP_MIX, newline) if index == 6 else chunk)
        if index == 3:
            rebuilt.append(_emit_row(_NEW_MIX, newline))
    replacement = "".join(rebuilt)
    # The original first dash starts after one space; avoid doubled indentation.
    if replacement.startswith(" -") and start > 0 and text[start - 1] == " ":
        replacement = replacement[1:]
    updates = [(start, end, replacement),
               (name.start_mark.index, name.end_mark.index, json.dumps(MODEL_NAME))]
    for first, last, value in sorted(updates, reverse=True):
        text = text[:first] + value + text[last:]
    if not text.endswith(newline):
        text += newline
    text += "logicalSw:" + newline
    for index, row in _gate().items():
        text += f"   {index}:{newline}"
        for key, value in row.items():
            encoded = value if value.isdigit() else json.dumps(value)
            text += f"      {key}: {encoded}{newline}"
    text += f'scriptsData:{newline}   0:{newline}      file: "ArgFly"{newline}      name: ""{newline}'
    result = text.encode("utf-8")
    validate_profile(result, source=source)
    return result


def validate_profile(raw: bytes, *, source: bytes | None = None) -> dict[str, Any]:
    """Check the profile contract; optionally prove preservation against its source.

    A passing result identifies a local file, not what is installed on a radio.
    Physical model readback and receiver/Mode 2/yaw-sign checks still follow.
    """
    _, model, _ = _parse(raw)
    mixes = _common(model)
    _require(model["header"].get("name") == MODEL_NAME, "Expected model ARGOS FLY")
    _require(len(mixes) == 11, "Expected ten manual channels plus one yaw replacement")
    manual = mixes[:4] + mixes[5:]
    for channel, row in enumerate(manual):
        if channel == 6:
            _require(row == _FLIP_MIX, "CH7 must be the sole unconditional fixed-low crash-flip mix")
        else:
            _manual_row(row, channel, _MANUAL_SOURCES[channel])
    _require(mixes[4] == _NEW_MIX, "CH4 Lua replacement must follow manual yaw and use L7")
    expected_gate = _gate()
    gate = model.get("logicalSw")
    # EdgeTX saves the Sticky runtime result even when persistence is off.
    # Only L10's binary state is observational; every policy field stays exact.
    if isinstance(gate, dict) and isinstance(gate.get("9"), dict):
        sticky_state = gate["9"].get("lsState")
        _require(sticky_state in ("0", "1"), "Native L10 lsState must be 0 or 1")
        expected_gate["9"]["lsState"] = sticky_state
    _require(gate == expected_gate, "Native L01–L11 gate differs from the reviewed contract")
    _require(model.get("scriptsData") == {"0": {"file": SCRIPT_NAME, "name": ""}},
             "Only ArgFly in LUA1 is allowed")
    if source is not None:
        _, original, _ = _source_model(source)
        for key in set(original) | set(model):
            if key not in {"header", "mixData", "logicalSw", "scriptsData"}:
                _require(original.get(key) == model.get(key), f"Source setting changed: {key}")
        expected_header = {**original["header"], "name": MODEL_NAME}
        _require(model["header"] == expected_header, "Receiver/header identity changed")
        for index, row in enumerate(manual):
            if index != 6:
                _require(row == original["mixData"][index], f"Original CH{index + 1} row changed")
    return {
        "schema": "argos-edgetx-profile-v1", "model_name": MODEL_NAME,
        "model_sha256": _hash(raw), "source_sha256": _hash(source) if source else None,
        "script": SCRIPT_NAME, "yaw_channel": 4, "crash_flip_channel": 7,
        "crash_flip": "disabled", "internal_rf": "CRSF CH1–16", "external_rf": "OFF",
        "enable_switch": "SC↑", "manual_switch": "SC middle", "native_gate": "L01–L11",
        "takeover_percent": 25, "takeover_dwell_ms": 200, "immediate_takeover_percent": 50,
        "preservation_checked": source is not None, "hardware_validated": False,
    }


def build_artifact(source_path: Path, output_dir: Path, script_path: Path) -> dict[str, Any]:
    """Write a new local review directory, never overwrite an existing model."""
    source = source_path.read_bytes()
    model = prepare_profile(source)
    script = script_path.read_bytes()
    _require(b"ARGOS_YAW_STREAM_V2" in script and b'"ARGOS FLY"' in script,
             "Expected the repository ArgFly.lua script")
    manifest = {**validate_profile(model, source=source), "script_sha256": _hash(script),
                "model_file": "model.yml", "script_file": "SCRIPTS/MIXES/ArgFly.lua"}
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    scripts = output_dir / "SCRIPTS" / "MIXES"
    scripts.mkdir(parents=True)
    (output_dir / "model.yml").write_bytes(model)
    (scripts / "ArgFly.lua").write_bytes(script)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare/validate one ARGOS FLY model locally; no radio access.")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Create a new review directory from the ordinary manual model")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--script", type=Path, required=True)
    check = sub.add_parser("validate", help="Validate a saved ARGOS FLY model file")
    check.add_argument("--model", type=Path, required=True)
    check.add_argument("--source", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = build_artifact(args.source, args.output, args.script)
        else:
            result = validate_profile(args.model.read_bytes(),
                                      source=args.source.read_bytes() if args.source else None)
    except (ProfileError, OSError) as exc:
        parser.exit(2, f"Profile stopped: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
