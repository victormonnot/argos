"""Analysis cadence is a bounded local launch setting, not a browser source field."""
import sys
from types import SimpleNamespace

import pytest

from argos.console.config import ConsoleConfig


def test_default_vision_rate_remains_five():
    assert ConsoleConfig().vision_hz == 5


@pytest.mark.parametrize("rate", [1, 8, 10])
def test_configured_vision_rate_survives_camera_source_changes(rate):
    config = ConsoleConfig(vision_hz=rate)
    values = config.public() | {
        "environment": "real", "video_source": "device", "video_endpoint": "/dev/video2",
    }
    updated = config.with_sources(values)
    assert updated.vision_hz == rate
    assert updated.video_endpoint == "/dev/video2"
    assert "vision_hz" not in updated.public()
    with pytest.raises(ValueError, match="exactly the documented fields"):
        config.with_sources(values | {"vision_hz": 5})


@pytest.mark.parametrize("rate", [0, 11, -1, True, False, None, 8.0, "8", []])
def test_config_rejects_invalid_vision_rate(rate):
    with pytest.raises(ValueError, match="vision_hz must be an integer between 1 and 10"):
        ConsoleConfig(vision_hz=rate)


@pytest.mark.parametrize("arguments, expected", [([], 5), (["--vision-hz", "8"], 8)])
def test_cli_forwards_default_or_explicit_vision_rate(arguments, expected, monkeypatch):
    pytest.importorskip("fastapi")
    from argos.console import __main__ as cli
    from argos.console import app

    configurations, launches = [], []
    monkeypatch.setattr(sys, "argv", ["argos.console", *arguments])
    monkeypatch.setattr(app, "create_app", lambda config: configurations.append(config) or "fake-app")
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(
        run=lambda *args, **kwargs: launches.append((args, kwargs))))
    cli.main()

    assert len(configurations) == 1
    assert configurations[0].vision_hz == expected
    assert launches == [(("fake-app",), {"host": "127.0.0.1", "port": 8080, "access_log": False})]


@pytest.mark.parametrize("rate", ["0", "11", "-1", "8.5", "true"])
def test_cli_rejects_invalid_vision_rate_before_starting_console(rate, monkeypatch, capsys):
    from argos.console import __main__ as cli

    monkeypatch.setattr(sys, "argv", ["argos.console", "--vision-hz", rate])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    assert "--vision-hz" in capsys.readouterr().err
