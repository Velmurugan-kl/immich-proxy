"""
template.py
-----------
Renders output filenames from a template string and file metadata.

Completely stateless — all functions are pure and take explicit arguments.
Used by processor.py and web_ui.py (for live preview).

Supported tokens
----------------
{year}      4-digit year                    2026
{month}     2-digit month (zero-padded)     03
{day}       2-digit day (zero-padded)       21
{hour}      2-digit hour 24h (zero-padded)  16
{minute}    2-digit minute (zero-padded)    01
{second}    2-digit second (zero-padded)    06
{make}      Camera make, spaces→underscores Apple
{model}     Camera model, spaces→underscores iPhone_15
{filename}  Original file stem (no ext)     IMG_0035
{counter}   Collision counter (omitted=0)   1
{city}      GPS city  [reserved, not impl]  —
{country}   GPS country [reserved, not impl]—
"""

from __future__ import annotations
import re
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------

def _safe(value: str) -> str:
    """Replace spaces with underscores; strip characters unsafe in filenames."""
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^\w\.\-]", "", value)
    return value


def _safe_or_fallback(value: str | None, fallback: str) -> str:
    if not value:
        return _safe(fallback)
    cleaned = _safe(value)
    return cleaned if cleaned else _safe(fallback)


# ---------------------------------------------------------------------------
# Token extraction from metadata
# ---------------------------------------------------------------------------

def build_token_map(
    *,
    dt:       datetime | None,
    make:     str | None,
    model:    str | None,
    filename: str,          # original stem, no extension
    counter:  int = 0,
    city:     str | None = None,
    country:  str | None = None,
    fallback: str = "unknown",
) -> dict[str, str]:
    """
    Build the complete {token: value} mapping for a single file.

    All values are already filesystem-safe (spaces→underscores, bad chars stripped).
    Missing values are replaced with `fallback`.
    counter=0 means no collision — {counter} renders as empty string in that case.
    """
    def d(fmt: str) -> str:
        """Format datetime or return fallback."""
        return dt.strftime(fmt) if dt else _safe(fallback)

    return {
        "year":     d("%Y"),
        "month":    d("%m"),
        "day":      d("%d"),
        "hour":     d("%H"),
        "minute":   d("%M"),
        "second":   d("%S"),
        "make":     _safe_or_fallback(make,    fallback),
        "model":    _safe_or_fallback(model,   fallback),
        "filename": _safe(filename) or _safe(fallback),
        "counter":  str(counter) if counter > 0 else "",
        "city":     _safe_or_fallback(city,    fallback),
        "country":  _safe_or_fallback(country, fallback),
    }


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

def render_template(template: str, tokens: dict[str, str]) -> str:
    """
    Substitute {token} placeholders in template with values from tokens dict.

    Rules:
    - Unknown tokens are left as-is (not substituted) so user can see the typo.
    - Empty token values (e.g. counter=0) are removed along with any adjacent
      separator character that would leave a double-separator artefact.
      e.g. "2026-03-21__IMG_0035" → "2026-03-21_IMG_0035"
    """
    result = template

    for token, value in tokens.items():
        placeholder = "{" + token + "}"
        if value == "":
            # Remove the placeholder AND one leading separator if present
            result = re.sub(
                r"[_\-\s]?" + re.escape(placeholder) + r"[_\-\s]?",
                lambda m: "",
                result,
            )
        else:
            result = result.replace(placeholder, value)

    # Collapse any double-separators left behind
    result = re.sub(r"([_\-])\1+", r"\1", result)
    result = result.strip("_- ")

    return result


def build_output_stem(
    template:   str,
    dt:         datetime | None,
    make:       str | None,
    model:      str | None,
    src_path:   Path,
    counter:    int = 0,
    fallback:   str = "unknown",
) -> str:
    """
    High-level helper: given a source file path and metadata, return the
    output filename stem (no extension, no directory).

    Used by processor.py for every converted file.
    """
    tokens = build_token_map(
        dt=dt,
        make=make,
        model=model,
        filename=src_path.stem,
        counter=counter,
        fallback=fallback,
    )
    return render_template(template, tokens)


# ---------------------------------------------------------------------------
# Live preview (used by web UI)
# ---------------------------------------------------------------------------

# Sample values shown in the web UI template preview
PREVIEW_METADATA = {
    "dt":       datetime(2026, 3, 21, 16, 1, 6),
    "make":     "Apple",
    "model":    "iPhone 15",
    "filename": "IMG_0035",
    "counter":  0,
}


def preview_template(template: str, fallback: str = "unknown") -> str:
    """
    Render the template with fixed sample metadata for UI preview.
    Returns the rendered stem (no extension).
    """
    tokens = build_token_map(fallback=fallback, **PREVIEW_METADATA)
    return render_template(template, tokens)
