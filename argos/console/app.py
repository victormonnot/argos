"""Local observation HTTP surface, with source settings and journal controls."""
import asyncio
import json
import math
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.background import BackgroundTask

from .config import ConsoleConfig
from .context import capture_context
from .control import ModeGenerationConflict
from .http_compression import ConsoleJSONCompression
from .archive import ArchiveError, RecordingArchive
from .session import ConsoleSession
from .vision import VisionService
from .visual_capture import capture_visual
from .visual_recording import MAX_SAMPLES as VISUAL_MAX_SAMPLES, VisualArchive

STATIC = Path(__file__).with_name("static")
FONT_FILES = frozenset({
    "ibm-plex-sans-400-500-latin.woff2",
    "ibm-plex-sans-400-500-latin-ext.woff2",
    "ibm-plex-mono-400-latin.woff2",
    "ibm-plex-mono-400-latin-ext.woff2",
    "ibm-plex-mono-500-latin.woff2",
    "ibm-plex-mono-500-latin-ext.woff2",
    "marcellus-400-latin.woff2",
    "marcellus-400-latin-ext.woff2",
})


def create_app(config: ConsoleConfig | None = None, *, session=None, vision=None):
    session = session or ConsoleSession(config or ConsoleConfig())
    archive = RecordingArchive(session.recorder.directory)
    visual_archive = VisualArchive(session.recorder.directory)
    vision = vision or VisionService(session.config.vision_model, variant=session.config.vision_variant,
                                     threads=session.config.vision_threads)
    session.vision = vision

    def snapshot():
        result = session.state()
        result["vision"] = vision.state(session)
        return result

    @asynccontextmanager
    async def lifespan(app):
        session.start()
        vision.start()
        stop = asyncio.Event()

        async def receive():
            while not stop.is_set():
                session.tick()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=.05)
                except asyncio.TimeoutError:
                    pass

        receiver = asyncio.create_task(receive())
        try:
            yield
        finally:
            stop.set()
            try:
                await receiver
            finally:
                try:
                    await session.aclose()
                finally:
                    await vision.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session = session
    app.state.vision = vision
    app.add_middleware(ConsoleJSONCompression)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/app.css")
    async def stylesheet():
        return FileResponse(STATIC / "app.css", media_type="text/css")

    @app.get("/app.js")
    async def script():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/control.js")
    async def control_script():
        return FileResponse(STATIC / "control.js", media_type="text/javascript")

    @app.get("/control.css")
    async def control_stylesheet():
        return FileResponse(STATIC / "control.css", media_type="text/css")

    @app.get("/sessions.js")
    async def sessions_script():
        return FileResponse(STATIC / "sessions.js", media_type="text/javascript")

    @app.get("/sessions.css")
    async def sessions_style():
        return FileResponse(STATIC / "sessions.css", media_type="text/css")

    @app.get("/live.js")
    async def live_script():
        return FileResponse(STATIC / "live.js", media_type="text/javascript")

    @app.get("/live.css")
    async def live_style():
        return FileResponse(STATIC / "live.css", media_type="text/css")

    @app.get("/analysis.js")
    async def analysis_script():
        return FileResponse(STATIC / "analysis.js", media_type="text/javascript")

    @app.get("/analysis.css")
    async def analysis_style():
        return FileResponse(STATIC / "analysis.css", media_type="text/css")

    @app.get("/fonts/{filename}")
    async def font(filename: str):
        if filename not in FONT_FILES:
            raise HTTPException(404, "Font not found")
        return FileResponse(STATIC / "fonts" / filename, media_type="font/woff2")

    @app.get("/api/state")
    async def state():
        return JSONResponse(snapshot())

    @app.get("/api/mavlink/messages")
    async def live_messages():
        return JSONResponse(session.live_messages())

    @app.get("/api/frame.jpg")
    async def frame():
        sample = session.video.latest(session.clock())
        if sample is None:
            return JSONResponse({"detail": "No recent image available"}, status_code=503)
        return Response(sample.jpeg, media_type="image/jpeg", headers={
            "X-Frame-Sequence": str(sample.sequence),
            "X-Frame-Received-At": str(sample.received_at),
            "X-Run-Id": session.run_id,
            "X-Video-Id": session.video_source_id,
        })

    @app.get("/api/vision/frame.jpg")
    async def vision_frame():
        candidate = vision.frame(session)
        if candidate is None:
            return JSONResponse({"detail": vision.state(session)["detail"]}, status_code=503)
        return Response(candidate.sample.jpeg, media_type="image/jpeg", headers={
            "X-Frame-Sequence": str(candidate.sample.sequence),
            "X-Frame-Received-At": str(candidate.sample.received_at),
            "X-Run-Id": candidate.context[0],
            "X-Video-Id": candidate.context[1],
            "X-Vision-Result": json.dumps(candidate.result, separators=(",", ":"), allow_nan=False),
        })

    async def mutation_body(request):
        # Browser write requests must originate from this local console. No CORS,
        # cross-site form or optional Origin bypass for socket/file operations.
        if request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "This action must come from the local console")
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(415, "A JSON body is required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413, "Configuration too large")
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise HTTPException(422, "Invalid JSON") from exc

    @app.post("/api/sources")
    async def sources(request: Request):
        nonlocal session
        values = await mutation_body(request)
        if session.reconnecting or session.replacing:
            raise HTTPException(409, "A source is already reopening")
        if session.recorder.active:
            raise HTTPException(409, "Stop recording before changing sources")
        try:
            session.control.check_reconfigure()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        try:
            config = session.config.with_sources(values)
            replacement = ConsoleSession(config, recorder=session.recorder)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            session.replacing = True
            await session.prepare_replacement()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            session.replacing = False
        # No await during this transition: the event-loop owner cannot poll or
        # inspect the retiring session midway through closing/replacing it.
        session.close()
        session = replacement
        session.vision = vision
        app.state.session = replacement
        session.start()
        return JSONResponse(snapshot())

    @app.post("/api/control/{operation}")
    async def flight_control(operation: str, request: Request):
        if operation not in {"claim", "input", "action", "framing"}:
            raise HTTPException(404, "Unknown flight-control action")
        values = await mutation_body(request)
        try:
            return JSONResponse(session.control_request(operation, values))
        except ModeGenerationConflict as exc:
            return JSONResponse({"detail": str(exc), "code": "stale_mode_generation",
                                 "control": exc.control}, status_code=409)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/sources/{source}/reconnect")
    async def reconnect(source: str, request: Request):
        if await mutation_body(request) != {}:
            raise HTTPException(422, "This action requires an empty object")
        if source not in {"video", "mavlink"}:
            raise HTTPException(404, "Unknown source")
        try:
            return JSONResponse(await session.reconnect(source))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/recordings/start")
    async def start_recording(request: Request):
        values = await mutation_body(request)
        if (not isinstance(values, dict) or set(values) - {"include_visual"}
                or type(values.get("include_visual", False)) is not bool):
            raise HTTPException(422, "Use an optional include_visual boolean")
        include_visual = values.get("include_visual", False)
        telemetry_ready = session.config.has_telemetry and session.link is not None and not session._error
        video_ready = include_visual and session.video.latest(session.clock()) is not None
        if (not (telemetry_ready or video_ready) or session._closed
                or session.recorder.active or session.replacing or session.reconnecting == "mavlink"
                or session.recorder.visual.snapshot()["state"] == "finalizing"):
            raise HTTPException(409, "A recent video or open telemetry link without an active recording is required")
        context = capture_context(session.config, session.run_id, session._endpoint)
        result = session.recorder.start(session.clock(), context=context, include_visual=include_visual)
        if result["state"] != "error":
            capture_visual(session, session.clock())
            result = session.recorder.snapshot()
        return JSONResponse(result, status_code=500 if result["state"] == "error" else 200)

    @app.post("/api/recordings/stop")
    async def stop_recording(request: Request):
        if await mutation_body(request) != {}:
            raise HTTPException(422, "This action requires an empty object")
        if not session.recorder.active:
            raise HTTPException(409, "No recording in progress")
        capture_visual(session, session.clock())
        result = session.recorder.stop(session.clock())
        return JSONResponse(result, status_code=500 if result["state"] == "error" else 200)

    async def archive_call(method, *args):
        try:
            # Disk reads, checksum validation and indexing must not run on the
            # event-loop owner that polls the live receiver every 50 ms.
            return await asyncio.to_thread(method, *args)
        except ArchiveError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    def require_closed(identifier):
        status = session.recorder.snapshot()
        if status["id"] == identifier and status["state"] in ("recording", "error"):
            raise HTTPException(404, "Completed recording unavailable")

    def visual_binding(metadata):
        context = metadata.get("context")
        if not isinstance(context, dict) or not context.get("run_id"):
            return None
        return {"started_at": metadata["started_at"], "run_id": context["run_id"]}

    async def visual_metadata(identifier, metadata):
        binding = visual_binding(metadata)
        if binding is None:
            return {"state": "missing", "detail": "This journal has no visual capture"}
        current = session.recorder.snapshot()
        if current["id"] == identifier and current.get("visual", {}).get("state") == "finalizing":
            return {**current["visual"], "detail": "Visual recording is finishing; reopen the session shortly"}
        try:
            return await asyncio.to_thread(visual_archive.metadata, identifier, **binding)
        except ArchiveError as exc:
            return {"state": "invalid", "detail": str(exc)}

    async def checked_visual(identifier, revision):
        require_closed(identifier)
        metadata = await archive_call(archive.metadata, identifier)
        if metadata["revision"] != revision:
            raise HTTPException(409, "The file changed; reopen the recording")
        binding = visual_binding(metadata)
        if binding is None:
            raise HTTPException(404, "This journal has no visual capture")
        current = session.recorder.snapshot()
        if current["id"] == identifier and current.get("visual", {}).get("state") == "finalizing":
            raise HTTPException(409, "Visual recording is still finishing")
        return metadata, binding

    @app.get("/api/recordings")
    async def recordings():
        return await archive_call(archive.catalog, session.recorder.snapshot())

    @app.get("/api/recordings/{identifier}")
    async def recording(identifier: str):
        require_closed(identifier)
        metadata = await archive_call(archive.metadata, identifier)
        metadata["visual"] = await visual_metadata(identifier, metadata)
        return metadata

    @app.get("/api/recordings/{identifier}/visual")
    async def visual_replay(identifier: str, revision: str, visual_revision: str, at: float = 0.):
        metadata, binding = await checked_visual(identifier, revision)
        if not math.isfinite(at) or not 0 <= at <= metadata["duration_s"]:
            raise HTTPException(422, "The cursor must be within the recording")
        result = await archive_call(lambda: visual_archive.replay(
            identifier, at, revision=visual_revision, **binding))
        if result.get("state") == "missing":
            raise HTTPException(404, "Visual recording not found; reopen this session")
        result.update(id=identifier, revision=revision, visual_revision=visual_revision, at_s=at)
        result["control"] = result.get("sample", {}).get("control") if result.get("sample") else None
        frame = result.get("frame")
        if frame is not None:
            frame["sequence"] = frame.get("source_sequence")
            frame["url"] = (f"/api/recordings/{identifier}/visual/frames/{frame['index']}.jpg"
                            f"?revision={revision}&visual_revision={visual_revision}")
        return JSONResponse(result)

    @app.get("/api/recordings/{identifier}/visual/frames/{index}.jpg")
    async def visual_frame(identifier: str, index: int, revision: str, visual_revision: str):
        if not 0 <= index < VISUAL_MAX_SAMPLES:
            raise HTTPException(422, "Invalid archived image index")
        _, binding = await checked_visual(identifier, revision)
        data = await archive_call(lambda: visual_archive.frame(
            identifier, index, revision=visual_revision, **binding))
        return Response(data, media_type="image/jpeg")

    @app.get("/api/recordings/{identifier}/visual/download")
    async def visual_download(identifier: str, revision: str, visual_revision: str):
        _, binding = await checked_visual(identifier, revision)
        stream = await archive_call(lambda: visual_archive.open_download(
            identifier, revision=visual_revision, **binding))
        expected = stream.visual_signature
        size_bytes = stream.visual_size_bytes

        def chunks():
            try:
                remaining = size_bytes
                while remaining:
                    current = os.fstat(stream.fileno())
                    actual = (current.st_dev, current.st_ino, current.st_size,
                              current.st_mtime_ns, current.st_ctime_ns)
                    if not stat.S_ISREG(current.st_mode) or actual != expected:
                        raise RuntimeError("Visual recording changed during download")
                    data = stream.read(min(128 * 1024, remaining))
                    if not data:
                        raise RuntimeError("Visual recording ended during download")
                    after = os.fstat(stream.fileno())
                    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != expected:
                        raise RuntimeError("Visual recording changed during download")
                    remaining -= len(data)
                    yield data
            finally:
                stream.close()

        return StreamingResponse(chunks(), media_type="application/vnd.sqlite3", background=BackgroundTask(stream.close), headers={
            "Content-Length": str(size_bytes),
            "Content-Disposition": f'attachment; filename="{identifier}.visual.sqlite3"'})

    @app.get("/api/recordings/{identifier}/replay")
    async def replay(identifier: str, revision: str, at: float = 0., system: int = 1, component: int = 1):
        require_closed(identifier)
        return await archive_call(archive.replay, identifier, at, system, component, revision)

    @app.get("/api/recordings/{identifier}/messages")
    async def messages(identifier: str, revision: str, offset: int = Query(0, ge=0),
                       limit: int = Query(50, ge=1, le=100), system: int | None = Query(None, ge=0, le=255),
                       component: int | None = Query(None, ge=0, le=255),
                       message_id: int | None = Query(None, ge=0, le=16777215)):
        require_closed(identifier)
        return await archive_call(archive.messages, identifier, revision, offset, limit, system, component, message_id)

    @app.get("/api/recordings/{identifier}/analysis")
    async def analysis(identifier: str, revision: str, bins: int = Query(160, ge=1, le=400),
                       gap_threshold_s: float = Query(1., gt=0, le=86400),
                       system: int | None = Query(None, ge=0, le=255),
                       component: int | None = Query(None, ge=0, le=255),
                       message_id: int | None = Query(None, ge=0, le=16777215)):
        require_closed(identifier)
        return await archive_call(archive.analysis, identifier, revision, system, component,
                                  message_id, bins, gap_threshold_s)

    @app.get("/api/recordings/{identifier}/download")
    async def download(identifier: str, revision: str | None = None):
        require_closed(identifier)
        data = await archive_call(archive.download, identifier, revision)
        return Response(data, media_type="application/x-ndjson", headers={
            "Content-Disposition": f'attachment; filename="{identifier}.jsonl"'})

    return app
