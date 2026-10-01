"""Start, stop or inspect a filming take on the existing local ARGOS console.

This client only manages recording. It never starts a console, changes its
configuration or sends pilot/assistance commands. Stop waits up to five seconds
for writers; an unfinished close remains explicitly reported as finalizing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

REQUEST_TIMEOUT = 2.
FINALIZE_TIMEOUT = 5.
POLL_INTERVAL = .2
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ConsoleError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ConsoleClient:
    def __init__(self, port=8080):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        self.origin = f"http://127.0.0.1:{port}"
        # A proxy or redirect must not move a local recording mutation elsewhere.
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, path, body=None, *, timeout=REQUEST_TIMEOUT):
        if path not in {"/api/state", "/api/recordings/start", "/api/recordings/stop"}:
            raise ValueError("Unsupported recording endpoint")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Request timeout must be positive")
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Origin": self.origin})
        request = Request(self.origin + path, data=data, headers=headers,
                          method="POST" if data is not None else "GET")
        try:
            with self.opener.open(request, timeout=min(timeout, REQUEST_TIMEOUT)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            try:
                raw = exc.read(8193)
                payload = json.loads(raw) if len(raw) <= 8192 else {}
                detail = payload.get("detail", exc.reason) if isinstance(payload, dict) else exc.reason
            except (ValueError, OSError):
                detail = exc.reason
            finally:
                exc.close()
            raise ConsoleError(f"Console HTTP {exc.code}: {_text(detail)}") from exc
        except (OSError, URLError) as exc:
            outcome = " Request outcome is unknown; run status before retrying." if data is not None else ""
            raise ConsoleError(f"Cannot reach {self.origin}: {_text(exc)}.{outcome}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ConsoleError("Console response exceeds the client size bound")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ConsoleError("Console returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ConsoleError("Console returned an unexpected response")
        return value


def _text(value):
    return "".join(c if c.isprintable() else " " for c in str(value))[:500]


def _recording(value):
    if (not isinstance(value, dict)
            or value.get("state") not in {"idle", "recording", "finalizing", "complete", "error"}):
        raise ConsoleError("Console response has no valid recording status")
    for key in ("visual", "filming"):
        if key in value and not isinstance(value[key], dict):
            raise ConsoleError(f"Console returned invalid {key} status")
    if "filming" in value and not isinstance(value["filming"].get("camera"), dict):
        raise ConsoleError("Console returned no filming camera status")
    return value


def _state(client, *, timeout=REQUEST_TIMEOUT):
    value = client.request("/api/state", timeout=timeout)
    return _recording(value.get("recording")), value.get("yaw_assist")


def _components(recording):
    yield "Native", recording
    if "visual" in recording:
        yield "Visual", recording["visual"]
    if "filming" in recording:
        yield "Filming", recording["filming"]
        yield "Camera", recording["filming"]["camera"]


def finalization_pending(recording):
    for name, part in _components(recording):
        if part.get("state") not in {"complete", "error", "idle"}:
            return True
        if name in {"Filming", "Camera"} and part.get("writer_stopped") is not True:
            return True
    return False


def capture_failed(recording):
    return any(part.get("state") == "error" for _, part in _components(recording))


def perform(action, client, *, label="", clock=time.monotonic, sleep=time.sleep):
    """Return observed recording/radio state and any post-mutation read warning."""
    if action == "status":
        recording, radio = _state(client)
        return recording, radio, ""
    if action not in {"start", "stop"}:
        raise ValueError("Unknown recording action")
    if not isinstance(label, str) or len(label) > 80 or any(ord(c) < 32 for c in label):
        raise ValueError("Take label must contain at most 80 characters without controls")
    body = {"include_visual": True, "include_filming": True, "label": label.strip()} if action == "start" else {}
    recording = _recording(client.request(f"/api/recordings/{action}", body))
    if action == "start" and "filming" not in recording:
        raise ConsoleError("Start was acknowledged without filming status; inspect status before retrying")
    identifier = recording.get("id")
    radio, warning = None, ""
    deadline = clock() + FINALIZE_TIMEOUT
    # Start refreshes once for live radio information. Stop keeps polling every
    # writer, even when the native JSONL file has already reported complete.
    while action == "start" or finalization_pending(recording):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            observed, radio = _state(client, timeout=min(REQUEST_TIMEOUT, remaining))
        except ConsoleError as exc:
            warning = f"{action.capitalize()} was acknowledged, but status refresh failed: {exc}"
            break
        if observed.get("id") != identifier:
            radio = None
            warning = "Recording identity changed during status refresh; previous finalization is unconfirmed."
            break
        recording = observed
        if action == "start" or not finalization_pending(recording):
            break
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(POLL_INTERVAL, remaining))
    return recording, radio, warning


def print_status(recording, radio=None, *, action="status"):
    state = recording["state"]
    pending = finalization_pending(recording)
    if capture_failed(recording):
        heading = "Recording error; inspect the files and errors below."
    elif action == "stop" and pending or state in {"complete", "finalizing"} and pending:
        heading = "Finalizing: writers have not all stopped; run status again."
    elif state == "complete":
        filming = recording.get("filming")
        heading = ("Finalized with losses, missing evidence or a capture limit; inspect the counters."
                   if filming and filming.get("complete") is not True else "Recording finalized.")
    elif state == "idle":
        heading = "No recording has been started in this console session."
    else:
        heading = "Recording in progress."
    print(heading)
    print(f"Take: {_text(recording.get('id') or 'none')}")
    filming = recording.get("filming", {})
    if filming:
        print(f"Label: {_text(filming.get('label') or '(none)')}")
        print(f"Directory: {_text(filming.get('directory', 'unknown'))}")
    for name, part in _components(recording):
        description = f"{name}: {_text(part.get('state', 'unknown'))}"
        if name == "Camera":
            description += (f"; frames={_text(part.get('written_frames', '?'))}"
                            f"; dropped={_text(part.get('dropped_frames', '?'))}"
                            f"; observed_fps={_text(part.get('observed_fps') or 'unavailable')}")
        elif name == "Visual":
            description += f"; frames={_text(part.get('frames', '?'))}; dropped={_text(part.get('dropped', '?'))}"
        elif name == "Filming":
            description += (f"; events={_text(part.get('events', '?'))}"
                            f"; dropped={_text(part.get('dropped_events', '?'))}"
                            f"; failed={_text(part.get('failed_events', '?'))}"
                            f"; Pocket samples={_text(part.get('pilot_samples', '?'))}")
        if name in {"Filming", "Camera"}:
            description += f"; writer_stopped={part.get('writer_stopped') is True}"
        print(description)
        if part.get("error") or part.get("detail"):
            print(f"  {_text(part.get('error') or part.get('detail'))}")
    if isinstance(radio, dict):
        assist = radio.get("assistance") if isinstance(radio.get("assistance"), dict) else {}
        fresh = assist.get("fresh") is True
        phases = ", ".join(f"{axis}={_text(assist.get(axis, {}).get('state', 'unknown'))}"
                           for axis in ("yaw", "pitch") if isinstance(assist.get(axis, {}), dict))
        print(f"Pocket: connected={radio.get('connected') is True}; fresh Lua sample={fresh}; {phases}")
    else:
        print("Pocket live status: unavailable in this response.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--port", type=int, default=8080, help="existing localhost console port (default: 8080)")
    parser.add_argument("--label", default="", help="optional take label for start, at most 80 characters")
    args = parser.parse_args(argv)
    if args.action != "start" and args.label:
        parser.error("--label is only used with start")
    try:
        recording, radio, warning = perform(args.action, ConsoleClient(args.port), label=args.label)
        print_status(recording, radio, action=args.action)
        if warning:
            print(warning, file=sys.stderr)
        return 1 if capture_failed(recording) or warning else 0
    except (ConsoleError, ValueError) as exc:
        print(f"Filming command failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Status wait interrupted; check status to confirm recording/finalization.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
