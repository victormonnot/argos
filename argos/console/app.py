"""Local observation HTTP surface, with source settings and journal controls."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ConsoleConfig
from .context import capture_context
from .archive import ArchiveError, RecordingArchive
from .session import ConsoleSession

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


def create_app(config: ConsoleConfig | None = None, *, session=None):
    session = session or ConsoleSession(config or ConsoleConfig())
    archive = RecordingArchive(session.recorder.directory)

    @asynccontextmanager
    async def lifespan(app):
        session.start()
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
                await session.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session = session
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
            raise HTTPException(404, "Police introuvable")
        return FileResponse(STATIC / "fonts" / filename, media_type="font/woff2")

    @app.get("/api/state")
    async def state():
        return JSONResponse(session.state())

    @app.get("/api/mavlink/messages")
    async def live_messages():
        return JSONResponse(session.live_messages())

    @app.get("/api/frame.jpg")
    async def frame():
        sample = session.video.latest(session.clock())
        if sample is None:
            return JSONResponse({"detail": "Aucune image récente disponible"}, status_code=503)
        return Response(sample.jpeg, media_type="image/jpeg", headers={
            "X-Frame-Sequence": str(sample.sequence),
            "X-Frame-Received-At": str(sample.received_at),
            "X-Run-Id": session.run_id,
            "X-Video-Id": session.video_source_id,
        })

    async def mutation_body(request):
        # Browser write requests must originate from this local console. No CORS,
        # cross-site form or optional Origin bypass for socket/file operations.
        if request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cette action doit venir de la console locale")
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(415, "Un corps JSON est requis")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413, "Configuration trop volumineuse")
        import json
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise HTTPException(422, "JSON invalide") from exc

    @app.post("/api/sources")
    async def sources(request: Request):
        nonlocal session
        values = await mutation_body(request)
        if session.reconnecting or session.replacing:
            raise HTTPException(409, "Une réouverture de source est déjà en cours")
        if session.recorder.active:
            raise HTTPException(409, "Arrêtez le journal avant de changer les sources")
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
        app.state.session = replacement
        session.start()
        return JSONResponse(session.state())

    @app.post("/api/control/{operation}")
    async def flight_control(operation: str, request: Request):
        if operation not in {"claim", "input", "action"}:
            raise HTTPException(404, "Action de pilotage inconnue")
        values = await mutation_body(request)
        try:
            return JSONResponse(session.control_request(operation, values))
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/sources/{source}/reconnect")
    async def reconnect(source: str, request: Request):
        if await mutation_body(request) != {}:
            raise HTTPException(422, "Cette action attend un objet vide")
        if source not in {"video", "mavlink"}:
            raise HTTPException(404, "Source inconnue")
        try:
            return JSONResponse(await session.reconnect(source))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/recordings/start")
    async def start_recording(request: Request):
        if await mutation_body(request) != {}:
            raise HTTPException(422, "Cette action attend un objet vide")
        if (not session.config.has_telemetry or session.link is None or session._error
                or session._closed or session.recorder.active or session.replacing
                or session.reconnecting == "mavlink"):
            raise HTTPException(409, "Une liaison ouverte sans journal actif est requise")
        context = capture_context(session.config, session.run_id, session._endpoint)
        result = session.recorder.start(session.clock(), context=context)
        return JSONResponse(result, status_code=500 if result["state"] == "error" else 200)

    @app.post("/api/recordings/stop")
    async def stop_recording(request: Request):
        if await mutation_body(request) != {}:
            raise HTTPException(422, "Cette action attend un objet vide")
        if not session.recorder.active:
            raise HTTPException(409, "Aucun enregistrement en cours")
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
            raise HTTPException(404, "Journal terminé indisponible")

    @app.get("/api/recordings")
    async def recordings():
        return await archive_call(archive.catalog, session.recorder.snapshot())

    @app.get("/api/recordings/{identifier}")
    async def recording(identifier: str):
        require_closed(identifier)
        return await archive_call(archive.metadata, identifier)

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
