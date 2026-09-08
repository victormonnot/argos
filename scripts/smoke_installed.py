"""Check the installed wheel, not a checkout; run with python -I.

No network port, vehicle or camera is opened. TestClient requests stay in process.
"""
from pathlib import Path
import tempfile
from importlib import metadata, resources

from fastapi.testclient import TestClient
import argos
from argos.console.app import create_app
from argos.console.config import ConsoleConfig


def main():
    location = Path(argos.__file__).resolve()
    if not any(part in {"site-packages", "dist-packages"} for part in location.parts):
        raise RuntimeError(f"expected an installed wheel, imported {location}")
    static = resources.files("argos.console") / "static"
    for filename in ("IBM-Plex-Sans-OFL.txt", "IBM-Plex-Mono-OFL.txt", "Marcellus-OFL.txt"):
        if "SIL OPEN FONT LICENSE" not in (static / "fonts" / filename).read_text():
            raise RuntimeError(f"missing bundled font licence: {filename}")
    with tempfile.TemporaryDirectory(prefix="argos-wheel-smoke-") as temporary:
        config = ConsoleConfig(recordings_dir=Path(temporary) / "captures")
        with TestClient(create_app(config)) as client:
            for path in ("/", "/app.css", "/app.js", "/sessions.css", "/sessions.js",
                         "/live.css", "/live.js", "/analysis.css", "/analysis.js",
                         "/control.css", "/control.js",
                         "/fonts/ibm-plex-sans-400-500-latin.woff2"):
                response = client.get(path)
                if response.status_code != 200 or not response.content:
                    raise RuntimeError(f"unavailable packaged resource: {path}")
            state = client.get("/api/state").json()
            if state["video"]["state"] != "unconfigured" or state["telemetry"]["state"] != "unconfigured":
                raise RuntimeError("unconfigured console invented an acquisition source")
            if (state["control"]["enabled"] or state["control"]["available"]
                    or state["control"]["owned"] or state["control"]["phase"] != "disabled"):
                raise RuntimeError("default console enabled flight control without opt-in")
            if client.get("/api/frame.jpg").status_code != 503:
                raise RuntimeError("unconfigured console returned an image")
    print(f"Installed ARGOS {metadata.version('argos')}: imports, web assets, fonts and empty state OK")


if __name__ == "__main__":
    main()
