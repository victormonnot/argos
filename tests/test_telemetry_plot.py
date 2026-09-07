"""A rejected packet cannot reset a reception-age curve or its status bands."""
import io
import math
import subprocess
import sys

import pytest

from argos.backends.mavlink import Received, Recording, TelemetryLimits
from argos.harness.telemetry_plot import plot_recording, reception_series


def attitude(at, *, boot=100, roll=0., component=1):
    return Received(at, 1, component, 0, 30, "ATTITUDE", {
        "mavpackettype": "ATTITUDE", "time_boot_ms": boot,
        "roll": roll, "pitch": 0., "yaw": 0.,
        "rollspeed": 0., "pitchspeed": 0., "yawspeed": 0.,
    }, b"already decoded")


def test_plot_data_preserves_rejections_expiry_and_boot_changes_per_source():
    recording = Recording(0., 5., "test", (
        attitude(.5, component=42), attitude(1.), attitude(2., roll=math.nan),
        attitude(3.), attitude(4., boot=1),
    ))
    heartbeat, angles, position = reception_series(
        recording, system=1, component=1, limits=TelemetryLimits(1., .5, 1.))
    assert heartbeat.valid == position.valid == ()
    assert heartbeat.spans(0., 5.) == ((0., 5., "absent"),)
    assert angles.valid == (1., 3., 4.) and angles.rejected == (2.,)
    assert angles.repeated == (3.,) and angles.decreased == (4.,)
    assert angles.spans(0., 5.) == (
        (0., 1., "absent"), (1., .5, "recent"), (1.5, 1.5, "stale"),
        (3., .5, "recent"), (3.5, .5, "stale"),
        (4., .5, "recent"), (4.5, .5, "stale"),
    )
    assert angles.ages(5.) == ((1., 3., 3., 4., 4., 5.), (0., 2., 0., 1., 0., 1.))


def test_equal_dates_and_nonzero_origin_do_not_create_negative_spans():
    recording = Recording(10., 11., "test", (attitude(10.), attitude(10.), attitude(11.)))
    _, series, _ = reception_series(recording, system=1, component=1,
                                     limits=TelemetryLimits(1., .5, 1.))
    assert series.spans(10., 11.) == ((10., .5, "recent"), (10.5, .5, "stale"))
    assert series.repeated == (10., 11.)


@pytest.mark.parametrize("format", ["svg", "png"])
@pytest.mark.parametrize("empty", [False, True])
def test_headless_render_works_with_empty_and_nonempty_recordings(format, empty):
    pytest.importorskip("matplotlib")
    recording = Recording(0., 0. if empty else 2., "test", () if empty else (attitude(0.),))
    output = io.BytesIO()
    plot_recording(recording, output, system=1, component=1,
                   limits=TelemetryLimits(1., .5, 1.), format=format)
    raw = output.getvalue()
    if format == "svg":
        assert b"<svg" in raw and b"Reception age only" in raw
        assert b"ATTITUDE" in raw
        if empty:
            assert b"Zero-duration journal" in raw
    else:
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")


def test_plot_module_import_does_not_load_matplotlib():
    code = "import sys; import argos.harness.telemetry_plot; assert 'matplotlib' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)
