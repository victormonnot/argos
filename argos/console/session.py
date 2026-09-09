"""One event-loop owner for MAVLink; video owns its separate acquisition lock.

HTTP reads never poll or refresh source receipts. The session's clock is local
monotonic elapsed time; camera and autopilot clocks are not synchronized by it.
"""
from collections import deque
import asyncio
import time
from uuid import uuid4

from argos.backends.mavlink import MavlinkLink, SerialTransport, TcpTransport, TelemetryCache, UdpTransport
from argos.backends.mavlink.health import HealthCache
from .config import ConsoleConfig
from .control import FlightControl
from .recording import ConsoleRecorder
from .incidents import ReceptionIncidents
from .live import LiveMessages
from .status import StatusTexts, system_status_view
from .video import DeviceCamera, GazeboCamera, VideoStore
from .views import battery_view, mode_view, reception_view, safe_text


class ConsoleSession:
    def __init__(self, config: ConsoleConfig, *, clock=None, link_factory=None, recorder=None):
        self.config = config
        self.vision = None
        origin = time.monotonic()
        self.clock = clock or (lambda: time.monotonic() - origin)
        self.run_id = uuid4().hex
        self.video_source_id = uuid4().hex
        self.connection_id = uuid4().hex
        self.status = StatusTexts(system=config.system, component=config.component,
                                  connection_id=self.connection_id)
        self.video = VideoStore(source=config.video_source, endpoint=config.video_endpoint,
                                age_limit=config.video_age, clock=self.clock)
        self.cache = TelemetryCache(system=config.system, component=config.component,
                                    limits=config.limits)
        self.health = HealthCache(system=config.system, component=config.component,
                                  age_limit=config.battery_age)
        self.recorder = recorder or ConsoleRecorder(config.recordings_dir)
        self._endpoint = config.telemetry_endpoint
        self._link_factory = link_factory or self._open_link
        self.link = self.camera = None
        self._report = None
        self._rx_messages = 0
        self._error = ""
        self._started = self._closed = False
        self._events = deque(maxlen=60)
        self._event_id = 0
        self._last_rejected = 0
        self.reconnecting = None
        self.replacing = False
        self._reconnect_task = None
        self.incidents = ReceptionIncidents()
        self.messages = LiveMessages()
        self.control = FlightControl(enabled=config.sim_control, framing_enabled=config.sim_framing, system=config.system,
                                     component=config.component)

    def _open_link(self):
        config = self.config
        if config.mavlink_tcp is not None:
            transport = TcpTransport(peer=config.mavlink_tcp)
        elif config.mavlink_bind is not None:
            transport = UdpTransport(local=config.mavlink_bind, peer=config.mavlink_peer)
            bound = transport.local_address
            self._endpoint = f"UDP {bound[0]}:{bound[1]} ← {config.mavlink_peer[0]}:{config.mavlink_peer[1]}"
        else:
            transport = SerialTransport(device=config.mavlink_device, baudrate=config.baudrate)
        try:
            return MavlinkLink(transport, sequence_scope=config.sequence_scope)
        except Exception:
            transport.close()
            raise

    def start(self):
        if self._started or self._closed:
            raise RuntimeError("a console session starts exactly once")
        self._started = True
        if self.config.has_telemetry:
            try:
                self.link = self._link_factory()
            except Exception as exc:
                self._error = f"Unable to open MAVLink: {exc}"
        if self.config.video_source != "none":
            self.camera = (GazeboCamera(self.video, python_path=self.config.gazebo_python_path)
                           if self.config.video_source == "gazebo" else DeviceCamera(self.video))
            try:
                self.camera.start()
            except Exception as exc:
                self.video.fail(f"Unable to open camera: {exc}")
        self.tick()

    def _event(self, at, level, message):
        self._event_id += 1
        self._events.append({"id": self._event_id, "at": at, "level": level, "message": safe_text(message)})

    def _check_reconnect(self, source):
        if source not in {"video", "mavlink"}:
            raise ValueError("Unknown source")
        if source == "mavlink":
            self.control.check_reconnect()
        else:
            self.control.check_reconfigure()
        if self._closed or not self._started:
            raise RuntimeError("The session is not open")
        if self.reconnecting or self.replacing:
            raise RuntimeError("A source is already reopening")
        if source == "mavlink":
            if not self.config.has_telemetry:
                raise RuntimeError("Configure a MAVLink source first")
            if self.recorder.active:
                raise RuntimeError("Stop recording before reopening the MAVLink link")
        elif self.config.video_source == "none":
            raise RuntimeError("Configure a camera first")

    async def reconnect(self, source):
        """Reopen one passive receiver without pausing the other acquisition.

        The session owns the shielded operation: a disconnected HTTP client
        cannot cancel cleanup or abandon a socket returned by a worker thread.
        Only the event-loop owner detaches and installs acquisition objects.
        """
        self._check_reconnect(source)
        self.reconnecting = source
        self._event(self.clock(), "info", "Reopening requested: " +
                    ("camera" if source == "video" else "MAVLink link"))
        if source == "video":
            previous, self.camera = self.camera, None
            self.video.stop()
            self.video_source_id = uuid4().hex
            self.video = VideoStore(source=self.config.video_source,
                                    endpoint=self.config.video_endpoint,
                                    age_limit=self.config.video_age, clock=self.clock)
        else:
            previous, self.link = self.link, None
            self.connection_id = uuid4().hex
            self.status.reconnect(self.connection_id, self.clock())
            self.cache = TelemetryCache(system=self.config.system, component=self.config.component,
                                        limits=self.config.limits)
            self.health = HealthCache(system=self.config.system, component=self.config.component,
                                      age_limit=self.config.battery_age)
            self.messages = LiveMessages()
            self._report = None
            self._rx_messages = self._last_rejected = 0
            self._error = ""
            # Recover reception of this same source without forgetting an armed
            # vehicle or an uncertain command, and without restoring authority.
            self.control.begin_reconnect(self.clock())
        self._reconnect_task = asyncio.create_task(self._reopen(source, previous))
        await asyncio.shield(self._reconnect_task)
        return self.state()

    def _replace_receiver(self, source, previous):
        # No event-loop polling can touch this detached receiver. Camera close
        # and TCP/device open may wait on native I/O, so all run in the worker.
        if previous is not None:
            previous.close()
            if isinstance(previous, DeviceCamera) and not previous.worker_stopped:
                raise RuntimeError("The previous camera reader has not released the device yet")
        if self._closed:
            return None
        if source == "mavlink":
            result = self._link_factory()
        else:
            result = (GazeboCamera(self.video, python_path=self.config.gazebo_python_path)
                      if self.config.video_source == "gazebo" else DeviceCamera(self.video))
            try:
                result.start()
            except BaseException:
                result.close()
                raise
        if self._closed:
            result.close()
            return None
        return result

    async def _reopen(self, source, previous):
        try:
            result = await asyncio.to_thread(self._replace_receiver, source, previous)
            if self._closed:
                if result is not None:
                    await asyncio.to_thread(result.close)
            elif source == "mavlink":
                self.link = result
            else:
                self.camera = result
        except Exception as exc:
            detail = f"Reopening {'MAVLink' if source == 'mavlink' else 'camera'} failed: {exc}"
            if source == "mavlink":
                self._error = detail
                self.link = previous
            else:
                self.video.fail(detail)
                # Retain a blocked native reader, so the next retry checks its
                # actual termination before creating any replacement worker.
                self.camera = previous
        finally:
            self.reconnecting = None
            self._reconnect_task = None
            if not self._closed:
                self.tick()

    async def prepare_replacement(self):
        """Do not bypass the physical reader bound through full source edits."""
        self.control.check_reconfigure()
        if not isinstance(self.camera, DeviceCamera):
            return
        if self.reconnecting:
            raise RuntimeError("A source is already reopening")
        self.reconnecting = "video"

        async def retire():
            try:
                self.video.stop()
                await asyncio.to_thread(self.camera.close)
            finally:
                self.reconnecting = None
                self._reconnect_task = None

        self._reconnect_task = asyncio.create_task(retire())
        await asyncio.shield(self._reconnect_task)
        if self._closed:
            raise RuntimeError("The session is stopped")
        if not self.camera.worker_stopped:
            raise RuntimeError("The previous camera reader has not released the device yet")

    def _observe_vision(self, *, refresh=False):
        try:
            if refresh and self.vision is not None:
                # Advance the bounded worker queues; never wait for inference.
                # A queued observation must reach the flight decision before
                # that decision evaluates the preceding image's receipt age.
                self.vision.tick(self)
            candidate = self.vision.frame(self) if self.vision is not None else None
            observation = None if candidate is None else {
                "run_id": candidate.context[0], "video_id": candidate.context[1],
                "sequence": candidate.sample.sequence, "received_at": candidate.sample.received_at,
                "detections": candidate.result["detections"],
            }
        except Exception:
            # Optional perception must not terminate the MAVLink/control task.
            # Unavailable observations trigger the normal manual-takeover path.
            observation = None
        self.control.framing.observe(observation)

    def tick(self):
        if self._closed:
            return
        vision_refreshed = False
        now = self.clock()
        if self.link is not None and not self._error:
            try:
                for event in self.link.poll(now):
                    self._rx_messages += 1
                    self.recorder.append(event)
                    self.messages.append(event)
                    self.status.append(event, now=now)
                    cache = self.health if event.message_id == 1 else self.cache
                    cache.update(event, now)
                    self.control.append(event, now=now)
                self._observe_vision(refresh=True)
                vision_refreshed = True
                # Polling/recording may have stalled. Evaluate the input deadline
                # at send time, never with the older receipt-batch timestamp.
                now = self.clock()
                self.control.tick(self.link, now)
                self._report = self.link.report(now)
                if self._report.closed:
                    self._error = f"MAVLink link interrupted: {self._report.last_error}"
            except Exception as exc:
                self._error = f"MAVLink reception interrupted: {exc}"
                self.link.close()
        if not vision_refreshed:
            self._observe_vision(refresh=True)
        if self.link is None or self._error:
            now = self.clock()
            self.control.tick(None, now)
        if self._error and self.recorder.active:
            self.recorder.stop(now, reason="transport_error", detail=self._error)
        snapshot = self.state(now)
        video, telemetry = snapshot["video"], snapshot["telemetry"]
        for event in self.incidents.update(now, video, telemetry):
            self._event(event["at"], event["level"], event["message"])
        rejected = telemetry["rejected"]
        if rejected != self._last_rejected:
            self._last_rejected = rejected
            self._event(now, "warning", f"Telemetry rejected ({rejected} in total) : {telemetry['last_rejection']}")

    def state(self, now=None):
        now = self.clock() if now is None else now
        snapshot = self.cache.snapshot(now)
        health = self.health.snapshot(now)
        views = {}
        for name in ("heartbeat", "attitude", "local_position_ned"):
            views[name] = reception_view(getattr(snapshot, name), getattr(self.config.limits, name))
        views["battery"] = battery_view(health.battery, self.config.battery_age)
        mode = mode_view(views["heartbeat"])
        if not self.config.has_telemetry:
            state, detail = "unconfigured", "No telemetry source configured"
        elif self.reconnecting == "mavlink":
            state, detail = "reconnecting", "Reopening MAVLink link"
        elif self._error or self._closed:
            state, detail = "error", self._error or "Session stopped"
        elif any(view["state"] == "recent" for view in views.values()):
            state, detail = "receiving", "Telemetry received from the selected component"
        elif any(view["fields"] is not None for view in views.values()):
            state, detail = "stale", "Stale telemetry: no recent valid reception"
        else:
            state, detail = "waiting", "Waiting for telemetry from the selected component"
        report = self._report
        video = self.video.snapshot(now)
        video["source_id"] = self.video_source_id
        if self.reconnecting == "video":
            video.update(state="reconnecting", detail="Reopening camera")
        return {
            "schema_version": 1, "run_id": self.run_id, "at": now,
            "environment": self.config.environment,
            "configuration": self.config.public(), "recording": self.recorder.snapshot(),
            "control": self.control.state(now),
            "video": video, "reconnecting": self.reconnecting,
            "reception": self.incidents.snapshot(),
            "telemetry": {
                "state": state, "detail": safe_text(detail), "endpoint": self._endpoint,
                "connection_id": self.connection_id,
                "system": self.config.system, "component": self.config.component,
                "rx_messages": self._rx_messages,
                "rx_bytes": report.rx_bytes if report else 0,
                "bad_bytes": report.bad_bytes if report else 0,
                "accepted": snapshot.accepted + health.accepted,
                "ignored_source": snapshot.ignored_source + health.ignored_source,
                "ignored_type": snapshot.ignored_type + health.ignored_type,
                "rejected": snapshot.rejected + health.rejected,
                "last_rejection": " · ".join(filter(None, (snapshot.last_rejection, health.last_rejection))),
                "mode": mode, **views,
                "autopilot_status": {
                    "declaration": system_status_view(views["heartbeat"]),
                    "texts": self.status.snapshot(now),
                },
            },
            "events": list(self._events),
        }

    def live_messages(self):
        now = self.clock()
        report = self._report
        return {"run_id": self.run_id, "connection_id": self.connection_id,
                "at": now, "state": self.state(now)["telemetry"]["state"],
                "rx_messages": self._rx_messages,
                "rx_bytes": report.rx_bytes if report else 0,
                "bad_bytes": report.bad_bytes if report else 0,
                "unsupported_frames": report.unsupported_frames if report else 0,
                "read_errors": report.read_errors if report else 0,
                **self.messages.snapshot(now)}

    def control_request(self, operation, values):
        if (self._closed or not self._started or self.replacing or self.reconnecting
                or self.link is None or self._error):
            raise RuntimeError("An open simulation link is required")
        if not isinstance(values, dict):
            raise ValueError("This action requires a JSON object")
        if operation == "framing":
            self._observe_vision(refresh=True)
            def check_selection(body, now):
                if self.vision is None:
                    raise RuntimeError("Vision unavailable")
                self.vision.check_selection(self, body, now)
            return {"control": self.control.framing_request(values, link=self.link,
                    now=self.clock(), selection_check=check_selection)}
        expected = {"claim": set(), "input": {"token", "seq", "axes"},
                    "action": {"token", "action"}}[operation]
        optional = {"claim": set(), "input": {"throttle", "mode_generation"},
                    "action": {"mode", "mode_generation", "input_seq"}}[operation]
        if not expected <= set(values) or not set(values) <= expected | optional:
            raise ValueError("Invalid command fields")
        self._observe_vision(refresh=True)
        now = self.clock()
        if operation == "claim":
            return self.control.claim(self.link, now)
        if operation == "input":
            result = self.control.input(values["token"], values["seq"], values["axes"],
                                        link=self.link, now=now,
                                        **{key: values[key] for key in ("throttle", "mode_generation")
                                           if key in values})
        else:
            result = self.control.action(values["token"], values["action"],
                                         link=self.link, now=now,
                                         **{key: values[key] for key in ("mode", "mode_generation", "input_seq")
                                            if key in values})
        return {"control": result}

    def close(self):
        if not self._closed:
            self.control.close(self.link, self.clock())
            self._closed = True
            self.video.stop()
            try:
                if self.recorder.active:
                    self.recorder.stop(self.clock(), reason="shutdown", detail="Console shutdown.")
                if self.camera is not None:
                    self.camera.close()
            finally:
                if self.link is not None:
                    self.link.close()

    async def aclose(self):
        """Retire first, then drain the owned worker before its loop disappears."""
        self.close()
        task = self._reconnect_task
        if task is not None:
            await asyncio.shield(task)
