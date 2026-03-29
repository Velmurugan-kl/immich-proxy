"""
processor.py
------------
Conversion engine. Reads all settings from AppConfig.
Imports template.py for filename rendering.

Public API (used by CLI, web UI, and future proxy):
    process_file(file, output_dir, et, cfg)      → result string
    batch_process(cfg)                           → None (prints results)
    convert_bytes(data, original_filename, et, cfg, tmp_dir) → (bytes, str)
"""

from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageOps
import pillow_heif
from datetime import datetime, timezone
import exiftool
import shutil
import os
import multiprocessing
import re
from concurrent.futures import ProcessPoolExecutor, as_completed

from config import AppConfig, load_config
from template import build_output_stem

pillow_heif.register_heif_opener()


# ---------------------------------------------------------------------------
# Windows ctime
# ---------------------------------------------------------------------------

def _set_windows_ctime(path: Path, dt: datetime) -> None:
    """Set Windows 'Date Created' via Win32 API. No-op on non-Windows."""
    try:
        import pywintypes, win32file, win32con
        win_time = pywintypes.Time(dt.replace(tzinfo=timezone.utc).timestamp())
        handle = win32file.CreateFile(
            str(path), win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            None, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None,
        )
        try:
            win32file.SetFileTime(handle, win_time, None, None)
        finally:
            handle.Close()
    except Exception:
        pass


def sync_all_timestamps(path: Path, dt: datetime) -> None:
    ts = dt.replace(tzinfo=timezone.utc).timestamp()
    os.utime(path, (ts, ts))
    _set_windows_ctime(path, dt)


# ---------------------------------------------------------------------------
# ExifTool
# ---------------------------------------------------------------------------

def make_exiftool() -> exiftool.ExifTool:
    """UTF-8 ExifTool instance (pyexiftool 0.5.x). Fixes Windows charmap crash."""
    return exiftool.ExifTool(encoding="utf-8")


def get_file_info(
    et: exiftool.ExifTool,
    file: Path,
) -> tuple[datetime | None, str | None, str | None]:
    """
    Single ExifTool read → (datetime, make, model).
    Returns None for any field not present in the file's metadata.
    """
    metadata = et.execute_json(str(file))[0]

    # --- datetime ---
    dt_str = (
        metadata.get("EXIF:DateTimeOriginal")
        or metadata.get("QuickTime:CreateDate")
        or metadata.get("CreateDate")
        or metadata.get("File:FileModifyDate")
    )
    dt = None
    if dt_str:
        dt_str = dt_str.split("+")[0].strip()
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(dt_str, fmt)
                break
            except ValueError:
                continue

    # --- make / model (kept separate for template engine) ---
    make  = metadata.get("EXIF:Make",  "").strip() or None
    model = metadata.get("EXIF:Model", "").strip() or None

    return dt, make, model


def copy_metadata_and_fix_dates(
    et: exiftool.ExifTool,
    src: Path,
    dst: Path,
    dt: datetime | None,
) -> None:
    """
    One ExifTool call: copy all tags src→dst, clear Orientation,
    overwrite date fields. dst must already exist.
    """
    args = [
        "-TagsFromFile", str(src),
        "-all:all>all:all",
        "-EXIF:Orientation=",
        "-overwrite_original",
    ]
    if dt:
        ts = dt.strftime("%Y:%m:%d %H:%M:%S")
        args += [
            f"-EXIF:DateTimeOriginal={ts}",
            f"-EXIF:CreateDate={ts}",
            f"-EXIF:ModifyDate={ts}",
            f"-XMP:DateTimeOriginal={ts}",
            f"-XMP:CreateDate={ts}",
        ]
    args.append(str(dst))
    et.execute(*args)


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def _save_image(img: Image.Image, output: Path, cfg: AppConfig) -> None:
    """Save a PIL image to output path using format settings from cfg."""
    kwargs = cfg.pil_save_kwargs.copy()
    fmt    = kwargs.pop("format")

    # JPEG doesn't support alpha — ensure RGB
    if fmt == "JPEG" and img.mode in ("RGBA", "PA", "P"):
        img = img.convert("RGB")
    elif fmt in ("PNG", "TIFF", "WEBP"):
        mode = "RGBA" if img.mode in ("RGBA", "PA") else "RGB"
        img  = img.convert(mode)

    img.save(output, fmt, **kwargs)


def _collision_safe_path(directory: Path, stem: str, ext: str) -> Path:
    """Return directory/stem.ext, appending _1, _2 … if the path exists."""
    candidate = directory / f"{stem}{ext}"
    counter   = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{ext}"
        counter  += 1
    return candidate


def convert_file(
    file: Path,
    output_dir: Path,
    et: exiftool.ExifTool,
    cfg: AppConfig,
) -> Path:
    """
    Convert a single HEIC/HEIF file to the format specified in cfg.
    Returns the output Path.
    """
    dt, make, model = get_file_info(et, file)

    stem = build_output_stem(
        template=cfg.filename.template,
        dt=dt,
        make=make,
        model=model,
        src_path=file,
        counter=0,
        fallback=cfg.filename.fallback,
    )

    ext    = cfg.output_extension
    output = _collision_safe_path(output_dir, stem, ext)

    # If stem changed because of collision, re-render with counter token
    if output.stem != stem:
        counter = int(re.search(r"_(\d+)$", output.stem).group(1))
        stem    = build_output_stem(
            template=cfg.filename.template,
            dt=dt, make=make, model=model,
            src_path=file, counter=counter,
            fallback=cfg.filename.fallback,
        )
        output = output_dir / f"{stem}{ext}"

    # Step 1: decode pixels → fix orientation → save
    with Image.open(file) as img:
        img = ImageOps.exif_transpose(img)
        _save_image(img, output, cfg)

    # Step 2: copy all metadata into the saved file
    copy_metadata_and_fix_dates(et, file, output, dt)

    # Step 3: sync filesystem timestamps
    if dt:
        sync_all_timestamps(output, dt)

    return output


# ---------------------------------------------------------------------------
# Routing (convert vs passthrough)
# ---------------------------------------------------------------------------

def process_file(
    file: Path,
    output_dir: Path,
    et: exiftool.ExifTool,
    cfg: AppConfig,
) -> str:
    """
    Route one file:
      - Source suffix in convert_suffixes → convert to cfg.output.format
      - Everything else → copy unchanged (unless format is 'original')
    Returns a human-readable log line.
    """
    try:
        if file.suffix.lower() in cfg.convert_suffixes_set:
            if cfg.output.format == "original":
                # passthrough even for HEIC — user explicitly wants no conversion
                shutil.copy2(file, output_dir / file.name)
                return f"[PASSTHROUGH] {file.name}  (format=original)"
            out = convert_file(file, output_dir, et, cfg)
            return f"[CONVERTED]   {file.name}  →  {out.name}"
        else:
            shutil.copy2(file, output_dir / file.name)
            return f"[PASSTHROUGH] {file.name}"
    except Exception as exc:
        return f"[ERROR]       {file.name}  |  {exc}"


# ---------------------------------------------------------------------------
# Parallel worker (top-level for Windows multiprocessing spawn)
# ---------------------------------------------------------------------------

def _worker(args: tuple[Path, Path, AppConfig]) -> str:
    file, output_dir, cfg = args
    with make_exiftool() as et:
        return process_file(file, output_dir, et, cfg)


# ---------------------------------------------------------------------------
# Batch entry point
# ---------------------------------------------------------------------------

def batch_process(cfg: AppConfig) -> None:
    """
    Process all files in cfg.input_path → cfg.output_path in parallel.
    Number of workers from cfg.effective_workers.
    """
    cfg.output_path.mkdir(parents=True, exist_ok=True)
    cfg.tmp_path.mkdir(parents=True, exist_ok=True)

    files = [f for f in cfg.input_path.iterdir() if f.is_file()]
    if not files:
        print("No files found.")
        return

    n = cfg.effective_workers
    print(f"Processing {len(files)} file(s) with {n} worker(s)...")
    print(f"  Format   : {cfg.output.format.upper()}")
    print(f"  Template : {cfg.filename.template}\n")

    work = [(f, cfg.output_path, cfg) for f in files]

    with ProcessPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(_worker, item): item[0] for item in work}
        for future in as_completed(futures):
            print(future.result())


# ---------------------------------------------------------------------------
# In-memory conversion (proxy layer)
# ---------------------------------------------------------------------------

def convert_bytes(
    data: bytes,
    original_filename: str,
    et: exiftool.ExifTool,
    cfg: AppConfig,
    tmp_dir: Path,
) -> tuple[bytes, str]:
    """
    Accepts raw file bytes → returns (output_bytes, new_filename).
    ExifTool needs real paths so tmp_dir holds intermediates.
    Called by the proxy per intercepted upload.
    """
    import tempfile

    suffix = Path(original_filename).suffix.lower()
    stem   = Path(original_filename).stem

    with tempfile.NamedTemporaryFile(
        dir=tmp_dir, suffix=suffix, delete=False
    ) as src_tmp:
        src_tmp.write(data)
        src_path = Path(src_tmp.name)

    ext      = cfg.output_extension if cfg.output.format != "original" else suffix
    out_path = tmp_dir / f"{src_path.stem}_out{ext}"

    try:
        dt, make, model = get_file_info(et, src_path)

        out_stem = build_output_stem(
            template=cfg.filename.template,
            dt=dt, make=make, model=model,
            src_path=Path(original_filename),
            counter=0,
            fallback=cfg.filename.fallback,
        )

        with Image.open(src_path) as img:
            img = ImageOps.exif_transpose(img)
            _save_image(img, out_path, cfg)

        copy_metadata_and_fix_dates(et, src_path, out_path, dt)
        if dt:
            sync_all_timestamps(out_path, dt)

        return out_path.read_bytes(), out_stem + ext

    finally:
        src_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
