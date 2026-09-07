"""The architectural rules, as assertions rather than conventions.

Three rules are stated in prose elsewhere in this repository. A rule that only
lives in prose is re-litigated on every pull request and quietly broken in
between, so each one is checked here by parsing the source. Nothing is imported or
executed: the check reads what the files say, which means it also catches a
violation inside a module that happens to fail at import time.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT = "argos"
ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / PROJECT
CORE = PKG / "core"

# Libraries a control layer may reach for, but the contract never may. Any of these
# inside argos/core means a backend concern has leaked into the shared vocabulary.
FORBIDDEN_IN_CORE = {
    "cv2",
    "gz",
    "gz_camera",
    "mavsdk",
    "pymavlink",
    "serial",
    "torch",
    "ultralytics",
}

# Only these packages may import argos.core.truth. Ground truth is instrumentation:
# a control layer that reads it is measuring itself against an answer it would not
# have in flight. Packages that do not exist yet simply never match.
TRUTH_CONSUMERS = (f"{PROJECT}.core", f"{PROJECT}.harness", f"{PROJECT}.backends")

TRUTH_MODULE = f"{PROJECT}.core.truth"


def module_name(path: Path) -> str:
    """Dotted name of a source file, e.g. argos/core/command.py -> argos.core.command."""
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(path: Path) -> list[str]:
    """Every name this file imports, as absolute dotted paths.

    Relative imports are resolved against the file's own package, so
    ``from .command import X`` and ``from argos.core.command import X`` are seen as
    the same thing and an import escaping upward is seen for what it is.

    **Each imported name is recorded as well as the module it came from.** An
    earlier version recorded only the module, which made ``from argos.core import
    truth`` indistinguishable from ``from argos.core import Command``: both looked
    like an import of ``argos.core``. Both forms of that mistake were reproduced
    against this file before it was fixed. Recording ``argos.core.truth`` and
    ``argos.core.Command`` separately costs nothing, because a name that is a class
    rather than a module simply never matches a module rule.

    This reads source text. It catches ordinary import statements, which is what a
    regression looks like; it is not a sandbox, and a module reached through
    :mod:`importlib` or an attribute walk is invisible to it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module_name(path).rsplit(".", 1)[0] if path.name != "__init__.py" else module_name(path)

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base_module = node.module or ""
            else:
                base = package.split(".")
                # level 1 is the current package; each extra level climbs one more.
                climbed = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                parts = [*climbed, node.module] if node.module else climbed
                base_module = ".".join(parts)
            if base_module:
                found.append(base_module)
                # `from pkg import name` may be importing a submodule called `name`.
                found.extend(f"{base_module}.{alias.name}" for alias in node.names)
    return [name for name in found if name]


def source_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def top_level(name: str) -> str:
    return name.split(".", 1)[0]


def reaches_truth(imported: str) -> bool:
    """Whether a recorded import names the ground-truth module or something in it.

    An exact match or a dotted prefix, never a bare string prefix: a future
    ``argos.core.truthy`` must not be mistaken for it.
    """
    return imported == TRUTH_MODULE or imported.startswith(f"{TRUTH_MODULE}.")


core_files = pytest.mark.parametrize(
    "path", source_files(CORE), ids=lambda p: p.name
)


@core_files
def test_core_imports_nothing_from_the_project(path: Path) -> None:
    """argos.core is the root of the dependency graph and depends on no sibling.

    Everything imports the contract; the contract imports nobody. The moment core
    reaches back into perception or a backend, the graph stops being a tree and
    "no control layer imports a simulator" becomes unenforceable, because the
    simulator can arrive through the contract itself.
    """
    offenders = [
        name
        for name in imports_of(path)
        if top_level(name) == PROJECT and not name.startswith(f"{PROJECT}.core")
    ]
    assert not offenders, f"{path.name} imports from the project: {offenders}"


@core_files
def test_core_imports_no_backend_library(path: Path) -> None:
    """The contract names no camera, no autopilot protocol and no inference runtime.

    These are the dependencies that make a module unportable. Keeping them out of
    core is what lets the same types describe a point-mass simulation and an
    airframe carrying its own computer.
    """
    offenders = [name for name in imports_of(path) if top_level(name) in FORBIDDEN_IN_CORE]
    assert not offenders, f"{path.name} imports a backend library: {offenders}"


def test_ground_truth_is_reachable_only_by_instrumentation() -> None:
    """Only the harness and the backends may import argos.core.truth.

    A control layer that reads ground truth is comparing itself against an answer
    it will not have in flight, and every measurement taken downstream of it is
    then meaningless. Placing the module apart makes that inconvenient; this test
    makes it impossible to merge.
    """
    offenders = []
    for path in source_files(PKG):
        name = module_name(path)
        if name.startswith(TRUTH_CONSUMERS):
            continue
        if any(reaches_truth(imported) for imported in imports_of(path)):
            offenders.append(name)
    assert not offenders, f"ground truth reached by a control layer: {offenders}"


def test_core_package_does_not_re_export_ground_truth() -> None:
    """`from argos.core import *` must not hand anybody a Truth.

    Reaching for ground truth has to be a deliberate, greppable import rather than
    something that arrives for free alongside World.
    """
    import argos.core as core

    assert "Truth" not in core.__all__
    assert not hasattr(core, "Truth")


# --------------------------------------------------------------------------
# The checks above are only worth their runtime if they catch what they claim.
# These write throwaway modules into the package and assert on the analysis.
# --------------------------------------------------------------------------


@pytest.fixture
def probe_module(tmp_path_factory):
    """Write a source file inside the package, yield its path, remove it after.

    It has to live inside the package rather than in a scratch directory, because
    the rules are expressed in terms of a module's dotted name and that name only
    exists relative to the package root.
    """
    directory = PKG / "_import_probe"
    directory.mkdir(exist_ok=True)
    (directory / "__init__.py").write_text("", encoding="utf-8")
    written: list[Path] = [directory / "__init__.py"]

    def write(source: str) -> Path:
        path = directory / f"probe_{len(written)}.py"
        path.write_text(source, encoding="utf-8")
        written.append(path)
        return path

    yield write

    for path in written:
        path.unlink(missing_ok=True)
    directory.rmdir()


@pytest.mark.parametrize(
    "source",
    [
        "from argos.core.truth import Truth",
        "from argos.core import truth",
        "from ..core import truth",
        "import argos.core.truth",
        "from argos.core.truth import Truth as T",
    ],
    ids=["submodule", "from-package", "relative", "plain-import", "aliased"],
)
def test_every_ordinary_way_of_reaching_ground_truth_is_detected(probe_module, source) -> None:
    """The five ways a control layer would actually write this import.

    Three of them used to pass unnoticed, because only the module a name came from
    was recorded and ``from argos.core import truth`` looked identical to
    ``from argos.core import Command``.
    """
    path = probe_module(source)
    assert any(reaches_truth(name) for name in imports_of(path)), (
        f"not detected: {source!r} -> {imports_of(path)}"
    )


@pytest.mark.parametrize(
    "source",
    [
        "from argos.core import Command, TargetView",
        "from argos.core.command import AttitudeCmd",
        "from argos.safety import CommandGate",
        "import numpy as np",
        "from ..core import Observation",
    ],
    ids=["names", "submodule", "sibling", "third-party", "relative-names"],
)
def test_ordinary_allowed_imports_are_not_flagged(probe_module, source) -> None:
    """Tightening the analysis must not trade false negatives for false positives.

    ``from argos.core import Command`` now records ``argos.core.Command``, which
    looks like a submodule path and is not one. It must not trip the rule.
    """
    path = probe_module(source)
    assert not any(reaches_truth(name) for name in imports_of(path))


def test_an_import_climbing_out_of_core_is_seen(probe_module) -> None:
    """The isolation rule has to resolve relative imports that leave the package.

    ``from .. import perception`` inside core is an escape written in the one form
    that carries no package name at all.
    """
    directory = PKG / "core"
    path = directory / "_probe_escape.py"
    path.write_text("from .. import perception\nfrom ..perception import detector\n", encoding="utf-8")
    try:
        names = imports_of(path)
        offenders = [
            name
            for name in names
            if top_level(name) == PROJECT and not name.startswith(f"{PROJECT}.core")
        ]
        assert offenders, f"escape not seen: {names}"
    finally:
        path.unlink(missing_ok=True)


# Modules on the far side of the gate: the door itself and the bounds it enforces.
# A package allowed to validate an input is not thereby allowed to hold these.
PAST_VALIDATION = (f"{PROJECT}.safety.gate", f"{PROJECT}.safety.envelope")


def reaches_past_validation(directory: Path) -> list[str]:
    """Every import in ``directory`` that goes beyond the admission predicates.

    The whole ``argos.safety`` package counts, because importing it executes its
    ``__init__`` and binds ``CommandGate`` and ``Envelope`` under that name: a rule
    that only listed the submodules would be satisfied by the one import that hands
    over everything it forbids.
    """
    offenders = []
    for path in source_files(directory):
        for imported in imports_of(path):
            if imported in PAST_VALIDATION or imported.startswith(
                tuple(f"{f}." for f in PAST_VALIDATION)
            ):
                offenders.append(f"{module_name(path)} -> {imported}")
            if imported == f"{PROJECT}.safety":
                offenders.append(f"{module_name(path)} -> {imported} (package, too broad)")
    return offenders


def test_guidance_may_validate_but_may_not_hold_the_gate() -> None:
    """The law imports the admission predicates, and must not import the door.

    `argos.guidance.visual` reads `argos.safety.validate` so that one definition of
    "usable observation" serves both layers. That is a dependency worth allowing and
    worth bounding: a law holding a CommandGate could emit, and the single-exit
    property would then depend on it choosing not to.

    Validating an input is not the same as being bounded by the envelope. The law is
    subject to the gate exactly as an operator is.
    """
    offenders = reaches_past_validation(PKG / "guidance")
    assert not offenders, f"guidance reaches past validation: {offenders}"


def test_a_backend_may_validate_but_may_not_hold_the_gate() -> None:
    """Same bound, and here it also keeps the dependency from turning back on itself.

    A backend reads `argos.safety.validate` for the same reason the law does: it
    declines a command it cannot integrate, and "usable" should mean one thing in
    the project. Holding the gate would be worse than in guidance, because the gate
    holds the world: a world that could reach its own gate could deposit into itself,
    and "no command reaches the world except through submit" would stop being a
    property of the structure.
    """
    offenders = reaches_past_validation(PKG / "backends")
    assert not offenders, f"a backend reaches past validation: {offenders}"


CONTROL_LAYERS = (f"{PROJECT}.guidance", f"{PROJECT}.perception")


def imports_any_of(directory: Path, packages: tuple[str, ...]) -> list[str]:
    """Every import in ``directory`` naming one of ``packages`` or something inside it."""
    return [
        f"{module_name(path)} -> {imported}"
        for path in source_files(directory)
        for imported in imports_of(path)
        if any(imported == pkg or imported.startswith(f"{pkg}.") for pkg in packages)
    ]


def test_a_backend_imports_no_control_layer() -> None:
    """Physics does not import the thing steering it.

    The dependency runs one way: a control layer speaks `World`, and a backend
    implements it. A backend importing guidance would invert that and quietly make
    the two unportable together, which is the exact coupling `World` exists to
    prevent.

    Perception is on the list for a second reason. A backend that produced target
    views would be handing the guidance law something it knows from the inside of the
    simulation, which is the shape of cheating this architecture exists to stop; the
    ground-truth rule already forbids reading it directly, and this closes the way
    round through a sensor the backend invented for itself.
    """
    offenders = imports_any_of(PKG / "backends", CONTROL_LAYERS)
    assert not offenders, f"a backend imports a control layer: {offenders}"


def test_perception_may_validate_but_may_not_hold_the_gate() -> None:
    """The tracker shares the admission predicates and stays under the same bound.

    `argos.perception.track` reads `argos.safety.validate` so that "usable input"
    means one thing across the project: the same check keeps a NaN out of a tracker's
    memory as keeps it out of the guidance filter. Holding the gate would let the
    layer that produces observations also decide they may be emitted, and the single
    exit would stop being a property of the structure.
    """
    offenders = reaches_past_validation(PKG / "perception")
    assert not offenders, f"perception reaches past validation: {offenders}"


def test_perception_imports_no_law_and_no_backend() -> None:
    """What is seen does not depend on what is being flown, or on what flies it.

    A perception layer importing guidance would make the sensor's behaviour a function
    of the control law; importing a backend would tie it to one vehicle. Both are the
    couplings that make a port require touching a control layer.
    """
    forbidden = (f"{PROJECT}.guidance", f"{PROJECT}.backends")
    offenders = imports_any_of(PKG / "perception", forbidden)
    assert not offenders, f"perception reaches sideways: {offenders}"


@pytest.mark.parametrize(
    "source",
    [
        "from argos.safety import CommandGate",
        "from argos.guidance import VisualGuidance",
        "from argos.backends import AttitudeSim",
        "from ..backends.attitude_sim import AttitudeSim",
    ],
    ids=["gate", "law", "backend", "relative-backend"],
)
def test_perception_reaching_out_of_its_layer_is_seen(source) -> None:
    """The two rules above, exercised against the package they police."""
    path = PKG / "perception" / "_probe_reach.py"
    path.write_text(source + "\n", encoding="utf-8")
    try:
        caught = reaches_past_validation(PKG / "perception") or imports_any_of(
            PKG / "perception", (f"{PROJECT}.guidance", f"{PROJECT}.backends")
        )
        assert caught, f"not detected: {source!r}"
    finally:
        path.unlink(missing_ok=True)


def test_perception_importing_core_and_validation_is_not_flagged() -> None:
    """The imports the rules are meant to allow must survive them."""
    path = PKG / "perception" / "_probe_ok.py"
    path.write_text(
        "from argos.core import Detection, TargetView\n"
        "from argos.safety.validate import check_detection\n",
        encoding="utf-8",
    )
    try:
        assert not reaches_past_validation(PKG / "perception")
        assert not imports_any_of(
            PKG / "perception", (f"{PROJECT}.guidance", f"{PROJECT}.backends")
        )
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "source",
    [
        "from argos.safety import CommandGate",
        "from argos.safety.gate import CommandGate",
        "from argos.safety.envelope import Envelope",
        "from ..safety.gate import CommandGate",
        "import argos.safety.gate",
    ],
    ids=["package", "gate-module", "envelope-module", "relative", "plain-import"],
)
def test_a_backend_reaching_past_validation_is_seen(source) -> None:
    """The rule above is only worth its runtime if it catches what it claims.

    Written into the real package, because the rule is expressed in terms of a
    module's dotted name and that name exists only relative to the package root.
    """
    path = PKG / "backends" / "_probe_gate.py"
    path.write_text(source + "\n", encoding="utf-8")
    try:
        assert reaches_past_validation(PKG / "backends"), f"not detected: {source!r}"
    finally:
        path.unlink(missing_ok=True)


def test_a_backend_importing_validation_alone_is_not_flagged() -> None:
    """Tightening the rule must not forbid the import it is meant to allow."""
    path = PKG / "backends" / "_probe_ok.py"
    path.write_text(
        "from argos.safety.validate import check_payload, check_state\n", encoding="utf-8"
    )
    try:
        assert not reaches_past_validation(PKG / "backends")
    finally:
        path.unlink(missing_ok=True)


def project_imports_of(path: Path) -> list[str]:
    """Every import in ``path`` that names something inside this project."""
    return [
        f"{module_name(path)} -> {imported}"
        for imported in imports_of(path)
        if top_level(imported) == PROJECT
    ]


def test_the_link_accounting_depends_on_nothing_in_this_project() -> None:
    """"Handed numbers, returns numbers" is a claim, so it is checked.

    `argos.harness.link` is meant to be pointable at a CRSF link that shares none of
    MAVLink's framing, and at a bench with no radio in it. That is only true while it
    imports nothing from here: the first import of a project type would tie the
    instrument to the thing it is supposed to be able to measure from the outside.

    It is a stronger rule than the ones above and it applies to this module only. The
    rest of the harness legitimately reads what it measures, ground truth included.
    """
    offenders = project_imports_of(PKG / "harness" / "link.py")
    assert not offenders, f"the link accounting reached into the project: {offenders}"


@pytest.mark.parametrize(
    "source",
    [
        "from argos.core import Command",
        "from argos.core.command import AttitudeCmd",
        "import argos.safety",
        "from ..core import Observation",
    ],
    ids=["names", "submodule", "plain-import", "relative"],
)
def test_a_dependency_creeping_into_the_link_accounting_is_seen(source) -> None:
    """The rule above, exercised through the same predicate it uses.

    Written that way on purpose: an earlier version re-implemented the filter inline,
    so blinding the rule left this passing and the pair proved nothing. A detection
    test that does not run the code under test is a second implementation being
    checked against itself.
    """
    path = PKG / "harness" / "_probe_dep.py"
    path.write_text(source + "\n", encoding="utf-8")
    try:
        assert project_imports_of(path), f"not detected: {source!r}"
    finally:
        path.unlink(missing_ok=True)


def test_third_party_imports_in_the_accounting_are_not_flagged() -> None:
    """The rule forbids project dependencies, not the standard library."""
    path = PKG / "harness" / "_probe_ok.py"
    path.write_text("import math\nfrom collections import deque\n", encoding="utf-8")
    try:
        assert not project_imports_of(path)
    finally:
        path.unlink(missing_ok=True)
