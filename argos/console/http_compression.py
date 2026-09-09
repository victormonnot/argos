"""Small negotiated replies for the frequently polled state/control routes."""
import re

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware


def _accepts_gzip(value):
    """Honor explicit exclusions/preferences before selecting an encoding.

    Starlette's middleware uses a substring check. Normalize its input only
    after parsing exact coding tokens, including q=0 and identity preferences.
    The most restrictive duplicate weight wins; malformed weights forbid a coding.
    """
    qualities = {}
    for item in value.split(","):
        coding, *parameters = (part.strip().lower() for part in item.split(";"))
        if not coding:
            continue
        quality = 1.
        if parameters:
            if (len(parameters) != 1 or not parameters[0].startswith("q=")
                    or not re.fullmatch(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", parameters[0][2:])):
                quality = 0.
            else:
                quality = float(parameters[0][2:])
        qualities[coding] = min(qualities.get(coding, 1.), quality)
    gzip_quality = qualities.get("gzip", qualities.get("*", 0.))
    return gzip_quality > 0. and qualities.get("identity", -1.) < gzip_quality


class ConsoleJSONCompression:
    """Use standard ASGI compression; never buffer/recompress images or files."""

    def __init__(self, app):
        self.app = app
        # These constructor arguments also exist in the declared dependency's
        # minimum Starlette version. No new optional compression dependency.
        self.compressed = GZipMiddleware(app, minimum_size=500, compresslevel=1)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api/state" or path.startswith("/api/control/")):
            await self.app(scope, receive, send)
            return
        headers = Headers(raw=scope.get("headers", []))
        encoding = b"gzip" if _accepts_gzip(",".join(headers.getlist("accept-encoding"))) else b"identity"
        selected = {**scope, "headers": [
            (key, value) for key, value in scope.get("headers", []) if key.lower() != b"accept-encoding"
        ] + [(b"accept-encoding", encoding)]}
        # Both paths use Starlette so Vary/Content-Length and streaming handling
        # stay its responsibility. The payload schema and command timing stay put.
        await self.compressed(selected, receive, send)
