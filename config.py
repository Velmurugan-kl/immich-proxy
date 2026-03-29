"""
config.py
---------
Loads, validates, and saves config.yaml.
Used by every layer: processor, CLI, web UI, and future proxy.

Usage:
    from config import load_config, save_config, AppConfig
    cfg = load_config()          # load from default path
    cfg.output.format            # → "png"
    save_config(cfg)             # write back to disk
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from pathlib import Path
import multiprocessing
import yaml

# Default config file location — same directory as this script
DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"

VALID_FORMATS  = {"jpeg", "png", "webp", "tiff", "original"}
KNOWN_TOKENS   = {
    "year", "month", "day", "hour", "minute", "second",
    "make", "model", "filename", "counter", "city", "country",
}


# ---------------------------------------------------------------------------
# Dataclasses — typed, IDE-friendly config model
# ---------------------------------------------------------------------------

@dataclass
class PathsConfig:
    input_dir:  str = "input"
    output_dir: str = "output"
    tmp_dir:    str = "tmp"


@dataclass
class OutputConfig:
    format:          str  = "png"
    jpeg_quality:    int  = 95
    png_compression: int  = 1
    webp_quality:    int  = 90
    webp_lossless:   bool = False


@dataclass
class FilenameConfig:
    template: str = "{year}-{month}-{day}_{make}_{model}_{filename}"
    fallback: str = "unknown"


@dataclass
class ProcessingConfig:
    workers:          int        = 0
    convert_suffixes: list[str]  = field(
        default_factory=lambda: [".heic", ".heif"]
    )


@dataclass
class AppConfig:
    paths:      PathsConfig      = field(default_factory=PathsConfig)
    output:     OutputConfig     = field(default_factory=OutputConfig)
    filename:   FilenameConfig   = field(default_factory=FilenameConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    # Proxy upstream — ignored when running as standalone CLI
    upstream:   str              = "http://immich-server:2283"

    # ------------------------------------------------------------------
    # Convenience properties consumed by processor / proxy
    # ------------------------------------------------------------------

    @property
    def input_path(self) -> Path:
        return Path(self.paths.input_dir)

    @property
    def output_path(self) -> Path:
        return Path(self.paths.output_dir)

    @property
    def tmp_path(self) -> Path:
        return Path(self.paths.tmp_dir)

    @property
    def effective_workers(self) -> int:
        """Resolve 0 → auto."""
        return self.processing.workers or min(multiprocessing.cpu_count(), 8)

    @property
    def convert_suffixes_set(self) -> set[str]:
        return {s.lower() for s in self.processing.convert_suffixes}

    @property
    def output_extension(self) -> str:
        fmt = self.output.format.lower()
        return {
            "jpeg": ".jpg",
            "png":  ".png",
            "webp": ".webp",
            "tiff": ".tiff",
            "original": "",   # resolved per-file by processor
        }.get(fmt, ".jpg")

    @property
    def pil_save_kwargs(self) -> dict:
        """PIL Image.save() keyword args for the configured format."""
        fmt = self.output.format.lower()
        if fmt == "jpeg":
            return {"format": "JPEG", "quality": self.output.jpeg_quality,
                    "subsampling": 0, "optimize": True}
        if fmt == "png":
            return {"format": "PNG",
                    "compress_level": self.output.png_compression}
        if fmt == "webp":
            if self.output.webp_lossless:
                return {"format": "WEBP", "lossless": True}
            return {"format": "WEBP", "quality": self.output.webp_quality}
        if fmt == "tiff":
            return {"format": "TIFF", "compression": "tiff_lzw"}
        return {}   # original passthrough — caller handles this


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def _dict_to_config(d: dict) -> AppConfig:
    """Recursively map a raw dict (from YAML) onto the dataclass tree."""
    paths = PathsConfig(**{
        k: v for k, v in d.get("paths", {}).items()
        if k in PathsConfig.__dataclass_fields__
    })
    output = OutputConfig(**{
        k: v for k, v in d.get("output", {}).items()
        if k in OutputConfig.__dataclass_fields__
    })
    filename = FilenameConfig(**{
        k: v for k, v in d.get("filename", {}).items()
        if k in FilenameConfig.__dataclass_fields__
    })
    proc_raw = d.get("processing", {})
    processing = ProcessingConfig(
        workers=proc_raw.get("workers", 0),
        convert_suffixes=proc_raw.get("convert_suffixes", [".heic", ".heif"]),
    )
    return AppConfig(paths=paths, output=output,
                     filename=filename, processing=processing,
                     upstream=d.get("upstream", "http://immich-server:2283"))


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """
    Load config from YAML file. Falls back to defaults if file missing.
    Validates critical fields and raises ValueError on bad values.
    """
    if not path.exists():
        return AppConfig()   # all defaults

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = _dict_to_config(raw)
    _validate(cfg)
    return cfg


def save_config(cfg: AppConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Serialise AppConfig back to YAML, preserving structure."""
    _validate(cfg)
    data = {
        "paths": asdict(cfg.paths),
        "output": asdict(cfg.output),
        "filename": asdict(cfg.filename),
        "processing": asdict(cfg.processing),
        "upstream": cfg.upstream,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(cfg: AppConfig) -> None:
    if cfg.output.format.lower() not in VALID_FORMATS:
        raise ValueError(
            f"Invalid output format '{cfg.output.format}'. "
            f"Must be one of: {', '.join(sorted(VALID_FORMATS))}"
        )
    if not (1 <= cfg.output.jpeg_quality <= 100):
        raise ValueError("jpeg_quality must be 1–100")
    if not (0 <= cfg.output.png_compression <= 9):
        raise ValueError("png_compression must be 0–9")
    if not (1 <= cfg.output.webp_quality <= 100):
        raise ValueError("webp_quality must be 1–100")
    if cfg.processing.workers < 0:
        raise ValueError("workers must be >= 0")


def extract_template_tokens(template: str) -> set[str]:
    """Return the set of {token} names found in a template string."""
    import re
    return set(re.findall(r"\{(\w+)\}", template))


def validate_template(template: str) -> list[str]:
    """
    Return a list of warning strings for unknown tokens.
    Empty list means the template is clean.
    """
    found   = extract_template_tokens(template)
    unknown = found - KNOWN_TOKENS
    return [f"Unknown token: {{{t}}}" for t in sorted(unknown)]
