"""
proxy.py
--------
Async reverse proxy that sits between Immich clients and the Immich server.

Intercepts POST /api/assets (the upload endpoint), converts HEIC/HEIF files
to the configured output format, then forwards the modified request upstream.
All other requests are passed through transparently with zero modification.

Architecture:
    Client → proxy:2283 → [intercept if HEIC upload] → immich-server:2283

The proxy is stateless — config is loaded once at startup and can be reloaded
by restarting the container (or hitting POST /admin/reload).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from config import load_config, AppConfig
from processor import convert_bytes, make_exiftool

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("proxy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UPLOAD_PATH = "/api/assets"
HEIC_SUFFIXES = {".heic", ".heif"}
HEIC_MIME_TYPES = {"image/heic", "image/heif", "image/heic-sequence"}

# Headers that must not be forwarded to upstream (aiohttp sets them itself)
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}


def _filtered_response_header_pairs(
    headers: aiohttp.typedefs.LooseHeaders,
) -> list[tuple[str, str]]:
    """Filter hop-by-hop response headers while preserving duplicate keys."""
    return [(k, v) for k, v in headers.items() if k.lower() not in HOP_BY_HOP]


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _upstream_headers(
    request: web.Request,
    *,
    strip_content_length: bool = False,
    strip_content_type: bool = False,
) -> dict[str, str]:
    """
    Strip hop-by-hop headers before forwarding upstream.
    Also removes Accept-Encoding — we stream the response bytes verbatim
    so the upstream must not compress in a way that requires decoding here.
    The browser receives the raw compressed bytes directly and decompresses
    them itself, which is correct proxy behaviour.
    """
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() != "accept-encoding"  # let upstream respond uncompressed
    }

    if strip_content_length:
        headers.pop("Content-Length", None)
        headers.pop("content-length", None)

    # Only strip Content-Type when we rebuild the body format entirely.
    if strip_content_type:
        headers.pop("Content-Type", None)
        headers.pop("content-type", None)

    return headers


def _is_websocket_request(request: web.Request) -> bool:
    upgrade = request.headers.get("Upgrade", "").lower()
    connection = request.headers.get("Connection", "").lower()
    return upgrade == "websocket" and "upgrade" in connection


def _to_ws_url(http_url: str) -> str:
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://") :]
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://") :]
    return http_url


def _should_buffer_body(request: web.Request) -> bool:
    content_type = request.headers.get("Content-Type", "").lower()
    return request.method in {"DELETE", "PATCH", "PUT"} or content_type.startswith(
        "application/json"
    )


def _is_heic_field(field: aiohttp.BodyPartReader) -> bool:
    """
    Determine if a multipart field contains a HEIC/HEIF file.
    Checks both the filename extension and the Content-Type header.
    """
    filename = field.filename or ""
    if Path(filename).suffix.lower() in HEIC_SUFFIXES:
        return True
    ct = field.headers.get("Content-Type", "").lower().split(";")[0].strip()
    return ct in HEIC_MIME_TYPES


# ---------------------------------------------------------------------------
# Core intercept logic
# ---------------------------------------------------------------------------


async def _intercept_upload(
    request: web.Request,
    cfg: AppConfig,
    session: aiohttp.ClientSession,
) -> web.Response:
    """
    Read the multipart upload, convert any HEIC assetData field,
    rebuild the multipart, and forward to upstream Immich.
    """
    upstream_url = str(cfg.upstream).rstrip("/") + request.path_qs

    # Read all multipart fields into memory / temp files
    reader = await request.multipart()
    out_fields = []  # list of (name, value_bytes, original_headers)
    converted = False

    tmp_dir = cfg.tmp_path
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with make_exiftool() as et:
        async for field in reader:
            raw = await field.read()

            if field.name == "assetData" and _is_heic_field(field):
                original_name = field.filename or "asset.heic"
                log.info("Converting %s → %s", original_name, cfg.output.format.upper())

                # convert_bytes is synchronous (PIL + ExifTool); run in thread pool
                loop = asyncio.get_event_loop()
                out_bytes, new_name = await loop.run_in_executor(
                    None,
                    lambda: convert_bytes(raw, original_name, et, cfg, tmp_dir),
                )

                # Build replacement headers for this field
                ext_to_mime = {
                    ".jpg": "image/jpeg",
                    ".png": "image/png",
                    ".webp": "image/webp",
                    ".tiff": "image/tiff",
                }
                new_ext = Path(new_name).suffix.lower()
                new_mime = ext_to_mime.get(new_ext, "application/octet-stream")

                new_headers = {
                    "Content-Disposition": (
                        f'form-data; name="assetData"; filename="{new_name}"'
                    ),
                    "Content-Type": new_mime,
                }
                out_fields.append(("assetData", out_bytes, new_headers, new_name))
                converted = True

            else:
                # Pass all other fields (deviceAssetId, fileCreatedAt, etc.) unchanged
                orig_headers = dict(field.headers)
                out_fields.append((field.name, raw, orig_headers, field.filename))

    if not converted:
        # No HEIC file found — shouldn't normally reach here (we only intercept
        # when we detect HEIC) but handle gracefully by passing through raw.
        log.warning("No HEIC assetData found in upload — forwarding original")

    # Rebuild multipart body
    boundary = uuid.uuid4().hex
    body_parts = []

    for name, data, headers, filename in out_fields:
        part_lines = [f"--{boundary}".encode()]
        for k, v in headers.items():
            part_lines.append(f"{k}: {v}".encode())
        part_lines.append(b"")
        part_lines.append(data)
        body_parts.append(b"\r\n".join(part_lines))

    body = b"\r\n".join(body_parts) + f"\r\n--{boundary}--".encode()

    fwd_headers = _upstream_headers(
        request,
        strip_content_length=True,
        strip_content_type=True,
    )
    fwd_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    fwd_headers["Content-Length"] = str(len(body))

    async with session.post(upstream_url, data=body, headers=fwd_headers) as resp:
        resp_body = await resp.read()
        return web.Response(
            status=resp.status,
            headers=_filtered_response_header_pairs(resp.headers),
            body=resp_body,
        )


# ---------------------------------------------------------------------------
# Generic transparent passthrough
# ---------------------------------------------------------------------------

# Headers that carry body/encoding info — must not be forwarded on GET/HEAD
NO_BODY_METHODS = {"GET", "HEAD", "OPTIONS"}


async def _proxy_websocket(
    request: web.Request,
    cfg: AppConfig,
    session: aiohttp.ClientSession,
) -> web.WebSocketResponse:
    """Bridge client websocket <-> upstream websocket."""
    upstream_http = str(cfg.upstream).rstrip("/") + request.path_qs
    upstream_ws = _to_ws_url(upstream_http)

    client_ws = web.WebSocketResponse(autoping=False)
    await client_ws.prepare(request)

    ws_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in {
            "host",
            "connection",
            "upgrade",
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-extensions",
        }
    }

    async with session.ws_connect(
        upstream_ws,
        headers=ws_headers,
        autoping=False,
    ) as server_ws:

        async def client_to_server() -> None:
            async for msg in client_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await server_ws.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await server_ws.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.PING:
                    await server_ws.ping(msg.data)
                elif msg.type == aiohttp.WSMsgType.PONG:
                    await server_ws.pong(msg.data)
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    await server_ws.close()

        async def server_to_client() -> None:
            async for msg in server_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await client_ws.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await client_ws.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.PING:
                    await client_ws.ping(msg.data)
                elif msg.type == aiohttp.WSMsgType.PONG:
                    await client_ws.pong(msg.data)
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    await client_ws.close()

        relay_to_server = asyncio.create_task(client_to_server())
        relay_to_client = asyncio.create_task(server_to_client())

        done, pending = await asyncio.wait(
            {relay_to_server, relay_to_client},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        for task in done:
            task.result()

    return client_ws


async def _passthrough(
    request: web.Request,
    cfg: AppConfig,
    session: aiohttp.ClientSession,
) -> web.StreamResponse:
    """
    Forward any non-intercepted request to upstream verbatim.

    Key fixes:
    - GET/HEAD/OPTIONS requests send no body (passing request.content causes
      aiohttp to hang waiting for data that never comes)
    - Response headers are filtered but Content-Encoding is preserved so
      compressed static assets (gzip/br) are streamed correctly
    - WebSocket upgrade requests are passed through without body
    """
    upstream_url = str(cfg.upstream).rstrip("/") + request.path_qs

    if _is_websocket_request(request):
        return await _proxy_websocket(request, cfg, session)

    # Buffer JSON-style bodies so aiohttp sends the exact payload upstream.
    if request.method in NO_BODY_METHODS:
        body = None
        fwd_headers = _upstream_headers(request)
    elif _should_buffer_body(request):
        body = await request.read()
        fwd_headers = _upstream_headers(request, strip_content_length=True)
        fwd_headers["Content-Length"] = str(len(body))
    else:
        body = request.content
        fwd_headers = _upstream_headers(request)

    async with session.request(
        method=request.method,
        url=upstream_url,
        headers=fwd_headers,
        data=body,
        allow_redirects=False,
        compress=False,  # do not re-compress — stream upstream response as-is
    ) as resp:
        # Build response headers — keep Content-Encoding so browser can
        # decompress gzip/brotli assets correctly
        resp_headers = _filtered_response_header_pairs(resp.headers)

        proxy_resp = web.StreamResponse(
            status=resp.status,
            reason=resp.reason,
            headers=resp_headers,
        )
        await proxy_resp.prepare(request)

        async for chunk in resp.content.iter_chunked(64 * 1024):
            await proxy_resp.write(chunk)

        await proxy_resp.write_eof()
        return proxy_resp


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


async def handle(request: web.Request) -> web.StreamResponse:
    cfg: AppConfig = request.app["cfg"]
    session: aiohttp.ClientSession = request.app["session"]

    content_type = request.headers.get("Content-Type", "").lower()
    path = request.path.rstrip("/") or "/"

    is_heic_upload = (
        request.method == "POST"
        and path == UPLOAD_PATH
        and content_type.startswith("multipart/form-data")
    )

    if is_heic_upload:
        log.debug("Upload intercepted: %s", request.path)
        return await _intercept_upload(request, cfg, session)

    return await _passthrough(request, cfg, session)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


async def admin_reload(request: web.Request) -> web.Response:
    """POST /admin/reload — reload config.yaml without restarting the container."""
    try:
        request.app["cfg"] = load_config()
        log.info("Config reloaded")
        return web.json_response({"ok": True, "message": "Config reloaded"})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def admin_health(request: web.Request) -> web.Response:
    """GET /admin/health — liveness probe for Docker healthcheck."""
    cfg: AppConfig = request.app["cfg"]
    return web.json_response(
        {
            "status": "ok",
            "upstream": str(cfg.upstream),
            "format": cfg.output.format,
        }
    )


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


async def on_startup(app: web.Application) -> None:
    connector = aiohttp.TCPConnector(limit=100)
    # auto_decompress=False is critical — we are a proxy, not a client.
    # We must stream the upstream response bytes (gzip/brotli/etc.) directly
    # to the browser without touching them. If aiohttp decompresses them, it
    # corrupts the Content-Encoding header and the browser can't render the page.
    app["session"] = aiohttp.ClientSession(
        connector=connector,
        auto_decompress=False,
    )
    log.info("Proxy started")
    log.info("  Upstream : %s", app["cfg"].upstream)
    log.info("  Format   : %s", app["cfg"].output.format.upper())
    log.info("  Template : %s", app["cfg"].filename.template)


async def on_shutdown(app: web.Application) -> None:
    await app["session"].close()
    log.info("Proxy stopped")


def make_app(cfg: AppConfig) -> web.Application:
    app = web.Application(client_max_size=500 * 1024 * 1024)  # 500 MB max upload
    app["cfg"] = cfg

    app.router.add_post("/admin/reload", admin_reload)
    app.router.add_get("/admin/health", admin_health)
    app.router.add_route("*", "/{path_info:.*}", handle)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


# ---------------------------------------------------------------------------


def _apply_env_overrides(cfg: AppConfig) -> AppConfig:
    """
    Apply environment variable overrides onto the loaded config.
    This lets docker-compose.yml control every setting without editing config.yaml.

    ENV var             Config field
    ─────────────────── ──────────────────────────
    IMMICH_UPSTREAM     cfg.upstream
    IMMICH_FORMAT       cfg.output.format
    IMMICH_JPEG_QUALITY cfg.output.jpeg_quality
    IMMICH_PNG_COMPRESSION cfg.output.png_compression
    IMMICH_WEBP_QUALITY cfg.output.webp_quality
    IMMICH_WEBP_LOSSLESS cfg.output.webp_lossless
    IMMICH_TEMPLATE     cfg.filename.template
    IMMICH_FALLBACK     cfg.filename.fallback
    IMMICH_WORKERS      cfg.processing.workers
    IMMICH_TMP_DIR      cfg.paths.tmp_dir
    """

    def env(key: str) -> str | None:
        return os.environ.get(key) or None

    if v := env("IMMICH_UPSTREAM"):
        cfg.upstream = v
    if v := env("IMMICH_FORMAT"):
        cfg.output.format = v
    if v := env("IMMICH_JPEG_QUALITY"):
        cfg.output.jpeg_quality = int(v)
    if v := env("IMMICH_PNG_COMPRESSION"):
        cfg.output.png_compression = int(v)
    if v := env("IMMICH_WEBP_QUALITY"):
        cfg.output.webp_quality = int(v)
    if v := env("IMMICH_WEBP_LOSSLESS"):
        cfg.output.webp_lossless = v.lower() in ("1", "true", "yes")
    if v := env("IMMICH_TEMPLATE"):
        cfg.filename.template = v
    if v := env("IMMICH_FALLBACK"):
        cfg.filename.fallback = v
    if v := env("IMMICH_WORKERS"):
        cfg.processing.workers = int(v)
    if v := env("IMMICH_TMP_DIR"):
        cfg.paths.tmp_dir = v
    return cfg


if __name__ == "__main__":
    cfg = load_config()
    cfg = _apply_env_overrides(cfg)
    port = int(os.environ.get("PROXY_PORT", 2283))
    web.run_app(make_app(cfg), host="0.0.0.0", port=port)
