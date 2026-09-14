"""Automated receiver-authority experiment, called only by the isolated launcher.

The virtual pilot, assistance and passive observer are separate processes. The
scenario uses native receiver input for the pilot and never sends RC overrides.
Only actual rendered images enter framing. Telemetry drives this scripted pilot
and the checks, never the image guidance law.
"""
from __future__ import annotations

from collections import deque
import json
import math
from pathlib import Path
from queue import Empty, Full, Queue
import signal
import subprocess
import threading
import time
from urllib.request import Request, urlopen


class BenchFailure(RuntimeError):
    pass


def receiver_matches(fields, expected):
    """Require reported channels, never infer missing values from sent inputs."""
    return all(type(fields.get(f"chan{channel}_raw")) is int
               and fields[f"chan{channel}_raw"] == value
               for channel, value in expected.items())


def prove_switch_rejection(receipts, sends, *, switch_at, pilot_yaw, override_yaw):
    """Evidence needs conflicting transmissions overlapping actual FC readback.

    A worker merely releasing its channels cannot satisfy this experiment.
    Receipt times are host observation times, not exact firmware transition times.
    """
    if pilot_yaw == override_yaw:
        raise BenchFailure("Pilot and override stimuli must differ")
    post = [r for r in receipts if r["at"] >= switch_at]
    last_conflict = max((i for i, r in enumerate(post)
        if not receiver_matches(r["fields"], {4: pilot_yaw, 7: 1100})), default=-1)
    matched = post[last_conflict + 1:]
    distinct = {r["fields"].get("time_boot_ms") for r in matched}
    if len(matched) < 3 or len(distinct - {None}) < 3:
        raise BenchFailure("Insufficient distinct receiver observations after switch-off")
    first, last = matched[0]["at"], matched[-1]["at"]
    # Status contains the actual worker send time; status receipt alone is not a send.
    active_sends = {s["last_send_at"] for s in sends
                    if s.get("probe_active") is True
                    and isinstance(s.get("last_send_at"), (int, float))
                    and first <= s["last_send_at"] <= last
                    and len(s.get("last_override", [])) == 18
                    and s["last_override"][3] == override_yaw}
    if len(active_sends) < 2:
        raise BenchFailure("No overlapping conflicting override transmissions were evidenced")
    return {"receiver_samples": len(matched), "distinct_firmware_times": len(distinct),
            "conflicting_send_samples": len(active_sends), "first_receipt_at": first,
            "last_receipt_at": last, "pilot_yaw": pilot_yaw, "override_yaw": override_yaw}


def require_live_probe(status, now):
    """SIGKILL must interrupt an emitting process, not an already expired probe."""
    if (not status or status.get("probe_active") is not True
            or not 0 <= now - status.get("at", -1) <= .25
            or status.get("probe_remaining_s", 0) < .7
            or not isinstance(status.get("last_send_at"), (int, float))
            or not 0 <= now - status["last_send_at"] <= .25
            or len(status.get("last_override") or []) != 18
            or status["last_override"][3] != 1540):
        raise BenchFailure("SIGKILL requires a recent non-neutral send from a still-active probe")


def prove_framing_interval(states, *, start, end):
    samples = [s for s in states if start <= s["at"] <= end]
    if (len(samples) < 3 or samples[0]["at"] - start > .25
            or end - samples[-1]["at"] > .25
            or any(b["at"] - a["at"] > .3 for a, b in zip(samples, samples[1:]))):
        raise BenchFailure("Insufficient continuous assistance status during the throttle step")
    sends = set()
    for sample in samples:
        framing = sample["framing"]
        overrides = sample.get("last_override") or []
        at = sample.get("last_send_at")
        if (not framing["active"] or framing["paused"] or len(overrides) != 18
                or overrides[0] != 0 or overrides[2] != 0
                or not isinstance(at, (int, float)) or not 0 <= sample["at"] - at <= .15):
            raise BenchFailure("Camera framing paused, stopped, or claimed a pilot-owned channel")
        sends.add(at)
    if len(sends) < 3:
        raise BenchFailure("Assistance did not keep sending through the throttle step")
    return {"status_samples": len(samples), "distinct_send_samples": len(sends),
            "first_status_at": samples[0]["at"], "last_status_at": samples[-1]["at"]}


def prove_disengaged(states, *, start, end):
    samples = [s for s in states if start <= s["at"] <= end]
    if len(samples) < 3 or end - samples[-1]["at"] > .25:
        raise BenchFailure("No recent assistance status after source recovery")
    if any(s["framing"]["active"] or s["probe_active"] for s in samples):
        raise BenchFailure("Assistance resumed without explicit engagement")
    return {"status_samples": len(samples), "last_status_at": samples[-1]["at"]}


def prove_camera_correction(states, receipts, *, start, end):
    """Correlate a non-neutral image correction with controller readback."""
    for state in states:
        if not (start <= state["at"] <= end and state["framing"]["active"]
                and not state["framing"]["paused"]):
            continue
        overrides = state.get("last_override") or []
        sent = state.get("last_send_at")
        if len(overrides) != 18 or not isinstance(sent, (int, float)) or sent < start:
            continue
        for channel in (2, 4):
            value = overrides[channel - 1]
            if not 1440 <= value <= 1560 or value == 1500:
                continue
            for receipt in receipts:
                if (sent <= receipt["at"] <= min(end, sent + .3)
                        and receiver_matches(receipt["fields"], {channel: value, 7: 1900})):
                    return {"channel": channel, "pwm": value, "sent_at": sent,
                            "receipt_at": receipt["at"],
                            "firmware_ms": receipt["fields"].get("time_boot_ms")}
    raise BenchFailure("No non-neutral camera correction was observed at the controller")


class JsonChild:
    """Bounded protocol reader; each child has its own process and sender clock."""

    def __init__(self, command, *, directory, name, env):
        self.name = name
        self.messages = Queue(maxsize=512)
        self.overflow = threading.Event()
        self.stderr = (directory / f"{name}.stderr.log").open("w")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, text=True, bufsize=1, env=env, start_new_session=True)
        self.latest = None
        self.expected_exit = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                item = json.loads(line)
                self.messages.put_nowait(item)
            except (ValueError, Full):
                self.overflow.set()
                return

    def send(self, value):
        self.process.stdin.write(json.dumps(value, allow_nan=False) + "\n")
        self.process.stdin.flush()

    def close(self):
        self.expected_exit = True
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.thread.join(timeout=1)
        self.process.stdout.close()
        self.stderr.close()


class Scenario:
    def __init__(self, *, console_url, mavlink_peer, assistance_peer, rc_peer,
                 output_dir, python, env):
        # The launcher starts the three SITL serial endpoints; this scenario
        # never accepts a remote hostname or a serial-device destination.
        for host, port in (mavlink_peer, assistance_peer, rc_peer):
            if host != "127.0.0.1" or type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError("The radio bench requires explicit IPv4 loopback ports")
        if not console_url.startswith("http://127.0.0.1:"):
            raise ValueError("The radio bench requires a local passive console")
        self.url = console_url.rstrip("/")
        self.peer, self.assistance_peer, self.rc_peer = mavlink_peer, assistance_peer, rc_peer
        self.directory = Path(output_dir)
        self.python, self.env = python, env
        self.started = time.monotonic()
        self.trace = (self.directory / "bench-events.jsonl").open("w")
        self.children = []
        self.radio = self.worker = self.link = None
        self.latest = {}
        self.parameters = {}
        self.receipts = deque(maxlen=2000)
        self.worker_states = deque(maxlen=2000)
        self.channels = [1500, 1500, 1100, 1500, 1100, 1100, 1100, 1100]
        self.observation = None
        self.offset = None
        self.context = None
        self.last_frame_poll = 0.
        self.last_forwarded_frame = None
        self.recording = None
        self.report = {"schema_version": 1, "passed": False, "checks": [],
                       "started_at_monotonic": self.started,
                       "scope": "SITL receiver arbitration and rendered-camera framing; no hardware validation"}
        self.event_id = 0

    def event(self, kind, **values):
        self.event_id += 1
        item = {"id": self.event_id, "at": time.monotonic(), "kind": kind, **values}
        self.trace.write(json.dumps(item, allow_nan=False) + "\n")
        self.trace.flush()
        return item

    def stage(self, name):
        print(f"Radio bench: {name}", flush=True)
        self.event("stage", name=name)

    def checked(self, name, **evidence):
        self.report["checks"].append({"name": name, "passed": True, **evidence})
        self.event("check_passed", name=name, evidence=evidence)

    def http(self, route, value=None, *, timeout=.35):
        body = None if value is None else json.dumps(value).encode()
        headers = {} if body is None else {"Content-Type": "application/json", "Origin": self.url}
        return urlopen(Request(self.url + route, data=body, headers=headers), timeout=timeout)

    def pump(self):
        now = time.monotonic()
        if now - self.started > 240:
            raise BenchFailure("Scenario exceeded its four-minute bound")
        if self.link is not None:
            for _ in range(200):
                msg = self.link.recv_match(blocking=False)
                if msg is None:
                    break
                if msg.get_srcSystem() != 1 or msg.get_srcComponent() != 1:
                    continue
                kind = msg.get_type()
                fields = msg.to_dict()
                received = time.monotonic()
                self.latest[kind] = (received, fields)
                if kind == "PARAM_VALUE":
                    key = fields["param_id"]
                    if isinstance(key, bytes):
                        key = key.decode("ascii").rstrip("\x00")
                    self.parameters[key] = fields["param_value"]
                if kind in {"RC_CHANNELS", "HEARTBEAT", "EXTENDED_SYS_STATE", "STATUSTEXT",
                            "VFR_HUD", "COMMAND_ACK", "ATTITUDE_TARGET"}:
                    entry = self.event("telemetry", message=kind, fields=fields)
                    if kind == "RC_CHANNELS":
                        self.receipts.append(entry)
        for child in self.children:
            if child.overflow.is_set():
                raise BenchFailure(f"Invalid or overflowing {child.name} output")
            for _ in range(100):
                try:
                    value = child.messages.get_nowait()
                except Empty:
                    break
                self.event("child", child=child.name, value=value)
                if value.get("kind") == "status":
                    child.latest = value
                    if child is self.worker:
                        self.worker_states.append(value)
                if value.get("kind") == "rejected":
                    raise BenchFailure(f"{child.name} rejected {value.get('op')}: {value.get('reason')}")
                if value.get("event") == "error":
                    raise BenchFailure(f"{child.name} protocol error: {value}")
            if child.process.poll() is not None and not child.expected_exit:
                raise BenchFailure(f"{child.name} exited unexpectedly ({child.process.returncode})")
        if self.offset is not None and now - self.last_frame_poll >= .02:
            self.last_frame_poll = now
            try:
                with self.http("/api/vision/frame.jpg", timeout=.2) as response:
                    headers = response.headers
                    result = json.loads(headers["X-Vision-Result"])
                    context = (headers["X-Run-Id"], headers["X-Video-Id"])
                    if context != self.context:
                        raise BenchFailure("Observer camera source changed during the scenario")
                    observation = {"run_id": context[0], "video_id": context[1],
                        "sequence": int(headers["X-Frame-Sequence"]),
                        "received_at": float(headers["X-Frame-Received-At"]) + self.offset,
                        "detections": result["detections"]}
                    # The native recorder retains the JPEG; no extra detector is run.
                    response.read()
                self.observation = observation
                identity = (id(self.worker), context, observation["sequence"])
                if (self.worker is not None and self.worker.process.poll() is None
                        and identity != self.last_forwarded_frame):
                    self.worker.send({"op": "observe", "observation": observation})
                    self.last_forwarded_frame = identity
            except (OSError, ValueError, KeyError):
                # Do not stamp old data as new. The worker's own image deadline runs.
                pass

    def wait(self, predicate, *, timeout, reason):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            result = predicate()
            if result:
                return result
            time.sleep(.02)
        raise BenchFailure(reason)

    def hold(self, seconds):
        end = time.monotonic() + seconds
        self.wait(lambda: time.monotonic() >= end, timeout=seconds + 1, reason="Hold timed out")

    def fields(self, kind, *, age=1.5):
        item = self.latest.get(kind)
        return item[1] if item is not None and 0 <= time.monotonic() - item[0] <= age else {}

    def expect_channels(self, expected, *, timeout=4., duration=.25):
        start = time.monotonic()
        collected = []
        last_boot = None
        cursor = self.event_id

        def check():
            nonlocal last_boot, cursor
            for entry in self.receipts:
                if entry["id"] <= cursor or entry["at"] < start:
                    continue
                cursor = entry["id"]
                at, fields = entry["at"], entry["fields"]
                boot = fields.get("time_boot_ms")
                if not receiver_matches(fields, expected):
                    collected.clear()
                elif boot != last_boot:
                    collected.append((at, boot))
                last_boot = boot
            if len(collected) >= 3 and collected[-1][0] - collected[0][0] >= duration:
                return {"samples": len(collected), "first_receipt_at": collected[0][0],
                        "last_receipt_at": collected[-1][0], "first_firmware_ms": collected[0][1],
                        "last_firmware_ms": collected[-1][1], "channels": expected}
            return False

        return self.wait(check, timeout=timeout, reason=f"Receiver did not retain pilot/override values {expected}")

    def pilot(self, **channels):
        for key, value in channels.items():
            self.channels[int(key.removeprefix("ch")) - 1] = value
        self.radio.send({"channels": list(self.channels)})
        return self.event("pilot_request", channels=list(self.channels))

    def parameter(self, name, value=None):
        from pymavlink import mavutil
        self.parameters.pop(name, None)
        if value is None:
            self.link.mav.param_request_read_send(1, 1, name.encode(), -1)
        else:
            self.link.mav.param_set_send(1, 1, name.encode(), value, mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
            self.event("simulation_parameter_request", name=name, value=value)
        answer = self.wait(lambda: (name, self.parameters[name]) if name in self.parameters else None,
                           timeout=3, reason=f"No parameter reply for {name}")[1]
        if value is not None and not math.isclose(answer, value, abs_tol=1e-5):
            raise BenchFailure(f"Parameter {name} did not confirm {value}: {answer}")
        return answer

    def start_worker(self):
        name = f"assistance-{sum(c.name.startswith('assistance') for c in self.children) + 1}"
        self.worker = JsonChild([self.python, "-m", "argos.backends.sitl_assistance",
            "--mavlink-peer", f"{self.assistance_peer[0]}:{self.assistance_peer[1]}"],
            directory=self.directory, name=name, env=self.env)
        self.children.append(self.worker)
        self.event("process_started", child=name, pid=self.worker.process.pid)
        self.wait(lambda: self.worker.latest and self.worker.latest.get("ready"), timeout=25,
                  reason="Assistance did not confirm its simulation and parameter guards")

    def ground_probe(self):
        self.worker.send({"op": "probe", "seconds": 2., "pitch": 1530, "yaw": 1540})

    def run(self):
        from pymavlink import mavutil
        self.stage("initialize independent receiver and passive observation")
        self.link = mavutil.mavlink_connection(f"tcp:{self.peer[0]}:{self.peer[1]}",
            source_system=254, source_component=190, dialect="ardupilotmega", autoreconnect=False)
        self.radio = JsonChild([self.python, "-m", "argos.backends.sitl_radio", "--rc-peer",
            f"{self.rc_peer[0]}:{self.rc_peer[1]}"], directory=self.directory, name="virtual-radio", env=self.env)
        self.children.append(self.radio)
        self.event("process_started", child="virtual-radio", pid=self.radio.process.pid)
        self.wait(lambda: self.fields("HEARTBEAT") and self.fields("RC_CHANNELS"), timeout=25,
                  reason="SITL receiver telemetry did not arrive")
        # Landed-state is not in the pinned firmware's ordinary stream groups.
        self.wait(lambda: self.latest.get("SIMSTATE"), timeout=5,
                  reason="A simulator identity receipt is required before requests")
        self.link.mav.command_long_send(1, 1, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                                       0, 245, 200000, 0, 0, 0, 0, 0)
        for name, expected in {"GPS1_TYPE": 0., "GPS2_TYPE": 0., "FS_GCS_ENABLE": 0.,
                "RC_OVERRIDE_TIME": .5, "FS_THR_ENABLE": 3., "FLTMODE_CH": 5.,
                "RC6_OPTION": 153., "RC7_OPTION": 46., "FLTMODE1": 0.,
                "FLTMODE4": 2., "FLTMODE6": 9., "SIM_RC_FAIL": 0.}.items():
            actual = self.parameter(name)
            if not math.isclose(actual, expected, abs_tol=1e-5):
                raise BenchFailure(f"Unexpected bench parameter {name}: {actual}")
        if self.parameter("ARMING_SKIPCHK") != 0:
            raise BenchFailure("Arming checks must remain enabled")
        requested = time.monotonic()
        with self.http("/api/state", timeout=2) as response:
            state = json.load(response)
        if state["environment"] != "simulation" or state["control"]["enabled"]:
            raise BenchFailure("An independent passive simulation observer is required")
        self.offset = requested - state["at"]
        self.context = (state["run_id"], state["video"]["source_id"])
        self.event("console_clock_mapping", source_at=state["at"], request_started_at=requested,
                   offset=self.offset, roundtrip_s=time.monotonic() - requested,
                   method="conservative fixed shared-host monotonic offset", context=self.context)
        with self.http("/api/recordings/start", {"include_visual": True}, timeout=3) as response:
            self.recording = json.load(response)
        self.report["recording"] = self.recording
        self.start_worker()

        self.stage("ground test: firmware rejects corrections when pilot switch is off")
        self.pilot(ch2=1460, ch4=1420, ch7=1900)
        self.expect_channels({2: 1460, 4: 1420, 7: 1900})
        self.ground_probe()
        self.expect_channels({2: 1530, 4: 1540}, duration=.15)
        off = self.pilot(ch7=1100)["at"]
        self.expect_channels({2: 1460, 4: 1420, 7: 1100}, duration=.35)
        evidence = prove_switch_rejection(list(self.receipts), list(self.worker_states),
            switch_at=off, pilot_yaw=1420, override_yaw=1540)
        self.checked("firmware_switch_rejection", **evidence)
        self.hold(2.)

        self.stage("ground test: kill assistance while the receiver and observer continue")
        self.pilot(ch7=1900)
        self.expect_channels({4: 1420, 7: 1900})
        self.ground_probe()
        self.expect_channels({4: 1540}, duration=.15)
        killed = self.worker
        before = dict(killed.latest or {})
        require_live_probe(before, time.monotonic())
        killed.expected_exit = True
        killed.process.kill()  # SIGKILL: no cooperative release or shutdown handler.
        kill_at = self.event("assistance_sigkill", pid=killed.process.pid, last_status=before)["at"]
        killed.process.wait(timeout=3)
        resumed = self.expect_channels({2: 1460, 4: 1420, 7: 1900})
        with self.http("/api/state", timeout=2) as response:
            observer = json.load(response)
        if observer["run_id"] != self.context[0] or self.radio.process.poll() is not None:
            raise BenchFailure("Pilot or observer failed during assistance process loss")
        self.checked("assistance_process_loss", pid=killed.process.pid, kill_at=kill_at,
            first_pilot_receipt_after_kill_s=resumed["first_receipt_at"] - kill_at,
            configured_override_timeout_s=.5, receiver=resumed,
            observer_run_id=observer["run_id"], pilot_pid=self.radio.process.pid)
        restart_at = time.monotonic()
        self.start_worker()
        self.hold(.7)
        recovery = prove_disengaged(list(self.worker_states), start=restart_at, end=time.monotonic())
        self.expect_channels({4: 1420, 7: 1900})
        self.checked("restart_stays_disengaged", pid=self.worker.process.pid, **recovery)

        self.stage("fly with actual camera framing and pilot-owned throttle")
        self.pilot(ch1=1500, ch2=1500, ch3=1100, ch4=1500, ch5=1100, ch6=1100, ch7=1100)
        self.expect_channels({2: 1500, 3: 1100, 4: 1500, 6: 1100})
        self.hold(2.)
        ground_alt = self.wait(lambda: self.fields("VFR_HUD"), timeout=5,
                               reason="No barometric altitude for the scripted pilot")["alt"]
        self.pilot(ch6=1900)
        self.wait(lambda: self.fields("HEARTBEAT").get("base_mode", 0) & 128,
                  timeout=15, reason="Normal radio arming was refused; inspect STATUSTEXT")
        self.pilot(ch3=1620)
        self.wait(lambda: self.fields("VFR_HUD").get("alt", ground_alt) - ground_alt >= 1.3,
                  timeout=12, reason="The scripted pilot did not reach the test height")
        self.pilot(ch3=1500, ch7=1900)
        self.wait(lambda: self.fields("EXTENDED_SYS_STATE").get("landed_state") == 2,
                  timeout=5, reason="SITL did not report IN_AIR")
        self.expect_channels({3: 1500, 7: 1900})

        def target():
            obs = self.observation
            if obs is None or not 0 <= time.monotonic() - obs["received_at"] < .35:
                return None
            detections = obs["detections"]
            if len(detections) != 1:
                return None
            d = detections[0]
            x, y, w, h = d["box"]
            if d["confidence"] >= .5 and .08 <= h <= .45 and min(x, y, 1-x-w, 1-y-h) > .005:
                return d["track_id"]
            return None

        track_id = self.wait(target, timeout=12, reason="No admissible person in the rendered camera")
        engage_at = time.monotonic()
        self.worker.send({"op": "engage", "track_id": track_id})
        self.wait(lambda: self.worker.latest and self.worker.latest["framing"]["active"],
                  timeout=3, reason="Rendered-camera framing did not engage")
        step_evidence = []
        for throttle in (1480, 1520, 1500):
            step_at = self.pilot(ch3=throttle)["at"]
            evidence = self.expect_channels({3: throttle}, duration=.5)
            status = self.worker.latest
            continuous = prove_framing_interval(list(self.worker_states), start=step_at,
                                               end=time.monotonic())
            step_evidence.append({"throttle_pwm": throttle, "receiver": evidence,
                                  "framing": status["framing"], "continuous": continuous})
        correction = prove_camera_correction(list(self.worker_states), list(self.receipts),
                                             start=engage_at, end=time.monotonic())
        self.checked("pilot_throttle_during_camera_framing", target_id=track_id,
                     steps=step_evidence, observed_correction=correction)
        self.pilot(ch7=1100)
        self.wait(lambda: self.worker.latest and not self.worker.latest["framing"]["active"],
                  timeout=3, reason="Pilot switch did not disengage normal framing")
        self.expect_channels({2: 1500, 3: 1500, 4: 1500, 7: 1100})
        restored_at = self.pilot(ch7=1900)["at"]
        self.hold(.7)
        recovery = prove_disengaged(list(self.worker_states), start=restored_at, end=time.monotonic())
        self.checked("airborne_pilot_takeover_and_no_resume", **recovery)

        self.stage("separate simulated receiver-loss fallback and landing")
        self.pilot(ch7=1100)
        if (self.fields("HEARTBEAT").get("custom_mode") != 0
                or not self.fields("HEARTBEAT").get("base_mode", 0) & 128
                or self.fields("EXTENDED_SYS_STATE").get("landed_state") != 2):
            raise BenchFailure("Receiver-loss test requires armed airborne Stabilize first")
        injection_at = time.monotonic()
        self.parameter("SIM_RC_FAIL", 1.)
        self.wait(lambda: self.latest.get("HEARTBEAT", (0,))[0] > injection_at
                  and self.fields("HEARTBEAT").get("custom_mode") == 9,
                  timeout=8, reason="Simulated radio failure did not select LAND")
        self.checked("receiver_failure_selects_land", injection="SIM_RC_FAIL=1; UDP silence alone is not receiver loss")
        self.wait(lambda: self.fields("EXTENDED_SYS_STATE").get("landed_state") == 1,
                  timeout=30, reason="SITL did not report landing")
        self.pilot(ch3=1100, ch5=1900, ch6=1100, ch7=1100)
        self.parameter("SIM_RC_FAIL", 0.)
        self.wait(lambda: self.fields("HEARTBEAT") and not self.fields("HEARTBEAT").get("base_mode", 0) & 128,
                  timeout=12, reason="SITL did not disarm after landing")
        self.checked("landed_disarmed", final_mode=self.fields("HEARTBEAT").get("custom_mode"))
        self.report["passed"] = True

    def close(self):
        # On an unsuccessful flight, request Land through the surviving pilot
        # before shutting down our isolated simulator. No force-disarm is used.
        if not self.report["passed"] and self.radio is not None and self.radio.process.poll() is None:
            try:
                self.pilot(ch5=1900, ch7=1100)
            except (OSError, ValueError):
                pass
        cleanup_errors = []
        for child in reversed(self.children):
            try:
                child.close()
            except Exception as exc:
                cleanup_errors.append(f"{child.name}: {exc}")
        if self.link is not None:
            try:
                self.link.close()
            except Exception as exc:
                cleanup_errors.append(f"monitor: {exc}")
        if cleanup_errors:
            self.report["cleanup_errors"] = cleanup_errors
            self.report["passed"] = False
        if self.recording is not None:
            try:
                with self.http("/api/recordings/stop", {}, timeout=5) as response:
                    self.report["recording_final"] = json.load(response)
                deadline = time.monotonic() + 5
                while True:
                    status = self.report["recording_final"]
                    visual = status.get("visual", {})
                    if status.get("id") != self.recording["id"] or visual.get("id") != self.recording["id"]:
                        raise BenchFailure("Recording identity changed during finalization")
                    if status.get("state") != "complete" or visual.get("state") not in {"finalizing", "complete"}:
                        raise BenchFailure("Telemetry or visual recording failed to complete")
                    if visual["state"] == "complete":
                        if visual.get("frames", 0) < 1 or visual.get("samples", 0) < 1:
                            raise BenchFailure("The recording contains no camera frames or samples")
                        break
                    if time.monotonic() >= deadline:
                        raise BenchFailure("Visual recording finalization timed out")
                    time.sleep(.05)
                    with self.http("/api/state", timeout=1) as response:
                        self.report["recording_final"] = json.load(response)["recording"]
            except Exception as exc:
                self.report["recording_error"] = str(exc)
                self.report["passed"] = False
        self.report["elapsed_s"] = time.monotonic() - self.started
        self.report["trace"] = "bench-events.jsonl"
        self.trace.close()
        (self.directory / "report.json").write_text(json.dumps(self.report, indent=2, allow_nan=False) + "\n")


def run_scenario(**kwargs):
    scenario = Scenario(**kwargs)
    try:
        scenario.run()
    except KeyboardInterrupt:
        scenario.report["passed"] = False
        scenario.report["interrupted"] = True
        scenario.event("interrupted")
    except Exception as exc:
        scenario.report["error"] = f"{type(exc).__name__}: {exc}"
        scenario.event("failure", error=scenario.report["error"])
        print(f"Radio bench failed: {exc}", flush=True)
    finally:
        scenario.close()
    return scenario.report
