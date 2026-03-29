# =============================================================================
# Dockerfile — Immich HEIC proxy / converter
# =============================================================================
# Multi-stage build:
#   builder  — installs Python deps into a venv
#   runtime  — lean final image with ExifTool + venv copied in
# =============================================================================

# --- Stage 1: builder --------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools needed by some Python wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt


# --- Stage 2: runtime --------------------------------------------------------
FROM python:3.11-slim AS runtime

# ExifTool is a Perl script — install perl + exiftool from apt
# libimage-exiftool-perl gives us the `exiftool` binary
RUN apt-get update && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

# App source
WORKDIR /app
COPY config.py      .
COPY config.yaml    .
COPY template.py    .
COPY processor.py   .
COPY proxy.py       .

# Tmp dir for in-flight conversions
RUN mkdir -p /tmp/proxy

# Expose the port the proxy listens on (same as Immich default)
EXPOSE 2283

# Healthcheck — polls the /admin/health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:2283/admin/health')" \
    || exit 1

CMD ["python", "proxy.py"]
