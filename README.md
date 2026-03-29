# Immich HEIC Proxy

> Automatic HEIC → PNG / JPEG / WebP / TIFF conversion for Immich uploads

A lightweight async reverse proxy that sits between your Immich clients and the Immich server. It intercepts photo upload requests, converts HEIC/HEIF files to your configured output format, and forwards the converted file upstream — completely transparently. No changes to your Immich installation or your clients.

Everything else (login, albums, thumbnails, live photos, sidecar files) passes through untouched with zero buffering.

---

## Request Flow

```
Immich app / browser
        ↓  port 2283  (same port as stock Immich)
immich-heic-proxy
        ↓
  POST /api/assets?
  ├── HEIC/HEIF file  →  convert  →  rebuild multipart  →  forward
  └── anything else   →  transparent stream passthrough
        ↓
immich-server  (internal Docker network)
```

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Quick Start](#2-quick-start)
3. [Port Reference](#3-port-reference)
4. [Configuration](#4-configuration)
5. [Filename Templates](#5-filename-templates)
6. [Web UI](#6-web-ui)
7. [Admin API](#7-admin-api)
8. [Upgrading Immich](#8-upgrading-immich)
9. [Troubleshooting](#9-troubleshooting)
10. [Project File Layout](#10-project-file-layout)

---

## 1. Prerequisites

- Docker Engine 24+ and Docker Compose v2 (plugin syntax)
- An existing or new Immich deployment
- The following files in the same directory:
  - `docker-compose.yml`
  - `Dockerfile`
  - `config.yaml`
  - `stack.env` (copied from `stack.env.example`)
  - All Python source files: `proxy.py`, `processor.py`, `config.py`, `template.py`, `web_ui.py`, `convert.py`

---

## 2. Quick Start

**Step 1** — Copy the example env file and fill in your values:

```bash
cp stack.env.example stack.env
nano stack.env
```

The three fields you must set:

| Variable | What to set |
|---|---|
| `UPLOAD_LOCATION` | Absolute host path where Immich stores photos — e.g. `/mnt/photos` |
| `DB_DATA_LOCATION` | Absolute host path for PostgreSQL data — e.g. `/mnt/postgres` |
| `DB_PASSWORD` | A strong password — change from the default |

**Step 2** — Build and start all services:

```bash
docker compose up -d --build
```

**Step 3** — Point your Immich mobile app and browser to `http://<your-host>:2283` — the same port as stock Immich. No client reconfiguration needed.

> **Note:** The first build takes 3–5 minutes to install Python dependencies and ExifTool. Subsequent restarts are fast because Docker caches the image layers.

---

## 3. Port Reference

| Port | Service | Purpose |
|---|---|---|
| `2283` | immich-heic-proxy | **Main client entry point.** All clients connect here. |
| `2284` | immich-server (direct) | Bypass the proxy. Use only for debugging. |
| `2285` | Proxy admin API | Config reload and health checks (see [Section 7](#7-admin-api)). |
| `5000` | Web UI | Browser settings UI — must be enabled (see [Section 6](#6-web-ui)). |

> **Warning:** Do not expose port `2284` to untrusted networks. Files uploaded directly to it bypass HEIC conversion entirely.

---

## 4. Configuration

Settings can be provided in two ways. Environment variables in `docker-compose.yml` take priority over `config.yaml`.

| Method | When to use |
|---|---|
| `docker-compose.yml` env vars | Simple overrides — change a value and `docker compose restart` |
| `config.yaml` (mounted volume) | Full control — edit the file, then `POST /admin/reload` (no restart needed) |
| Web UI (port 5000) | Visual editing of `config.yaml` with live filename preview |

### 4.1 Environment Variables

Set these under the `immich-heic-proxy` service's `environment` block in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `IMMICH_UPSTREAM` | `http://immich-server:2283` | Internal address of the Immich server |
| `IMMICH_FORMAT` | `png` | Output format: `jpeg` \| `png` \| `webp` \| `tiff` \| `original` |
| `IMMICH_JPEG_QUALITY` | `95` | JPEG quality 1–100 (`95` = visually lossless) |
| `IMMICH_PNG_COMPRESSION` | `1` | PNG compression 0–9 (lossless at all levels — only affects speed/size) |
| `IMMICH_WEBP_QUALITY` | `90` | WebP lossy quality 1–100 |
| `IMMICH_WEBP_LOSSLESS` | `false` | Set to `true` for lossless WebP (ignores quality) |
| `IMMICH_TEMPLATE` | `{year}-{month}-{day}_{make}_{model}_{filename}` | Filename template (see [Section 5](#5-filename-templates)) |
| `IMMICH_FALLBACK` | `unknown` | Value used when a template token has no metadata |
| `IMMICH_WORKERS` | `0` | Parallel workers (`0` = auto, capped at CPU count) |
| `IMMICH_TMP_DIR` | `/tmp/proxy` | Temp directory for in-flight conversions |
| `PROXY_PORT` | `2283` | Port the proxy listens on inside the container |

### 4.2 config.yaml

The mounted `config.yaml` gives full control including fields not exposed as env vars. It is read once at startup. After editing, reload without a restart:

```bash
curl -X POST http://<host>:2285/admin/reload
```

> **Note:** If you remove the `config.yaml` volume mount from `docker-compose.yml`, the file baked into the image at build time is used. Useful for immutable deployments.

### 4.3 Output Formats

| Format | Quality setting | Notes |
|---|---|---|
| `jpeg` | `IMMICH_JPEG_QUALITY` (1–100) | Lossy. Smallest files. Universal support. |
| `png` | `IMMICH_PNG_COMPRESSION` (0–9) | **Lossless.** Large files. Universal support. |
| `webp` | `IMMICH_WEBP_QUALITY` (1–100) | Lossy or lossless. Excellent compression. |
| `tiff` | — | Lossless LZW. Very large. Archival use. |
| `original` | — | No conversion — file copied as-is. |

---

## 5. Filename Templates

The `IMMICH_TEMPLATE` variable (or `filename.template` in `config.yaml`) controls how converted files are named. Use curly-brace tokens replaced with values from each file's EXIF metadata.

### Available Tokens

| Token | Example output | Description |
|---|---|---|
| `{year}` | `2026` | 4-digit capture year |
| `{month}` | `03` | 2-digit capture month (zero-padded) |
| `{day}` | `21` | 2-digit capture day (zero-padded) |
| `{hour}` | `16` | 2-digit capture hour, 24h (zero-padded) |
| `{minute}` | `01` | 2-digit capture minute (zero-padded) |
| `{second}` | `06` | 2-digit capture second (zero-padded) |
| `{make}` | `Apple` | Camera make from EXIF (spaces → underscores) |
| `{model}` | `iPhone_15` | Camera model from EXIF (spaces → underscores) |
| `{filename}` | `IMG_0035` | Original filename stem, no extension |
| `{counter}` | `1` | Collision counter — omitted when filename is unique |
| `{city}` | *(coming soon)* | GPS city via reverse geocoding |
| `{country}` | *(coming soon)* | GPS country via reverse geocoding |

### Template Examples

| Template | Output filename |
|---|---|
| `{year}-{month}-{day}_{make}_{model}_{filename}` | `2026-03-21_Apple_iPhone_15_IMG_0035.png` |
| `{year}/{month}/{day}/{filename}` | `2026/03/21/IMG_0035.png` |
| `{filename}_{year}{month}{day}` | `IMG_0035_20260321.png` |
| `{year}-{month}-{day}_{hour}-{minute}-{second}` | `2026-03-21_16-01-06.png` |

> **Note:** Missing tokens are replaced with the `IMMICH_FALLBACK` value. If a photo has no EXIF `Make`, `{make}` becomes `unknown` (or whatever fallback you configured).

---

## 6. Web UI

The proxy includes a browser-based settings UI for editing `config.yaml` visually, with a live filename template preview. It is **disabled by default** and runs as a separate service.

### 6.1 Enabling the Web UI

Add the following service to your `docker-compose.yml`, alongside the proxy service:

```yaml
  immich-web-ui:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: immich_web_ui
    command: python web_ui.py
    ports:
      - "5000:5000"
    volumes:
      - ./config.yaml:/app/config.yaml   # shared with the proxy service
    restart: unless-stopped
```

Then apply the change:

```bash
docker compose up -d --build
```

Open `http://<your-host>:5000` in your browser.

### 6.2 Features

- Output format selector — JPEG, PNG, WebP, TIFF, or original passthrough
- Per-format quality sliders — JPEG quality, PNG compression level, WebP quality and lossless toggle
- Filename template editor with clickable token insertion buttons
- Live filename preview — updates as you type, using sample metadata
- Parallel workers slider
- **Save** — writes `config.yaml` to disk immediately
- **Run batch** — triggers batch processing and streams results live to the browser

### 6.3 Applying Web UI Changes to the Proxy

The Web UI writes `config.yaml`. The proxy does not watch the file — after saving in the UI, reload the running proxy:

```bash
curl -X POST http://<host>:2285/admin/reload
```

> **Warning:** Do not expose port `5000` to the internet. The Web UI has no authentication — restrict it to your local network or access it through a VPN.

---

## 7. Admin API

The proxy exposes a lightweight admin API on port `2285`.

| Endpoint | Method | Description |
|---|---|---|
| `/admin/health` | `GET` | Returns upstream URL, active format, and `"status": "ok"`. Used by Docker healthcheck. |
| `/admin/reload` | `POST` | Reloads `config.yaml` without restarting the container. |

### Examples

```bash
# Check proxy health
curl http://<host>:2285/admin/health

# Reload config after editing config.yaml or saving in the Web UI
curl -X POST http://<host>:2285/admin/reload
```

---

## 8. Upgrading Immich

The proxy is fully decoupled from the Immich version. To upgrade Immich:

```bash
# Update IMMICH_VERSION in stack.env, then:
docker compose pull immich-server immich-machine-learning
docker compose up -d
```

The proxy container does not need to be rebuilt for Immich upgrades.

> **Note:** If a future Immich release changes the upload endpoint path (`/api/assets`) or the multipart field names, update `UPLOAD_PATH` and the field-detection logic in `proxy.py`.

---

## 9. Troubleshooting

### Photos not converting

- Check proxy logs:
  ```bash
  docker compose logs immich-heic-proxy -f
  ```
- Confirm clients connect to port `2283` (the proxy), not `2284` (direct server).
- Verify the proxy can reach Immich:
  ```bash
  curl http://<host>:2285/admin/health
  ```

### charmap / encoding errors

These should no longer occur — the proxy uses ExifTool with explicit UTF-8 pipe encoding. If they reappear, check the ExifTool version inside the container:

```bash
docker exec immich_heic_proxy exiftool -ver
```

It should be version 12 or higher.

### Large uploads timing out

The proxy buffers HEIC uploads in memory during conversion. Videos and non-HEIC files are streamed without buffering and are not affected. If memory is a concern, lower `IMMICH_WORKERS` and increase Docker container memory limits.

### Web UI changes not taking effect

After saving in the UI you must explicitly reload the proxy — it does not watch `config.yaml` automatically:

```bash
curl -X POST http://<host>:2285/admin/reload
```

If the reload returns an error, check `config.yaml` for YAML syntax issues.

---

## 10. Project File Layout

```
.
├── docker-compose.yml     Docker Compose stack definition
├── Dockerfile             Multi-stage image build (builder + slim runtime)
├── stack.env              Your secrets and paths  ← do not commit to git
├── stack.env.example      Template — copy to stack.env and fill in values
├── config.yaml            All converter settings (mounted into proxy container)
├── config.py              Typed config model — load / validate / save
├── template.py            Filename token engine (pure, stateless functions)
├── processor.py           HEIC conversion engine (PIL + ExifTool)
├── proxy.py               Async reverse proxy (aiohttp)
├── web_ui.py              Flask browser settings UI
├── convert.py             CLI batch runner (standalone, no Docker needed)
└── requirements.txt       Python dependencies
```
