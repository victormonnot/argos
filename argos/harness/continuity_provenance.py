"""Bind current replay sources while accepting the exact harness relocation.

Historical reports keep their original paths and hashes. Only the two runner
files whose imports/source lists changed get a reversible relocation check;
other dependency changes still fail unless the caller already permits them.
"""
from __future__ import annotations

import hashlib


_OLD_REPLAY = "argos/perception/continuity_replay.py"
_REPLAY = "argos/harness/continuity_replay.py"
_RELOCATED_RUNNERS = {"examples/compare_trackers.py", "examples/diagnose_continuity.py"}


def current_source_hashes(repo, recorded, *, allowed_changes=()):
    """Verify recorded dependencies and return actual current paths/hashes.

The caller's allowed changes retain their pre-existing experimental meaning;
relocation does not grant an exception to any additional dependency.
"""
    current = {}
    for original_name, expected in recorded.items():
        name = _REPLAY if original_name == _OLD_REPLAY else original_name
        if name in current:
            raise ValueError(f"duplicate source dependency after relocation: {name}")
        content = (repo / name).read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if name not in allowed_changes and actual != expected:
            historical = content
            if name in _RELOCATED_RUNNERS:
                historical = historical.replace(b"argos.harness.continuity_replay",
                                                b"argos.perception.continuity_replay")
                historical = historical.replace(_REPLAY.encode(), _OLD_REPLAY.encode())
            if hashlib.sha256(historical).hexdigest() != expected:
                raise ValueError(f"source comparison dependency changed: {original_name}")
        current[name] = actual
    return current
