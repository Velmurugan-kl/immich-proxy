"""
heic_processor.py
-----------------
Converts HEIC/HEIF images to lossless PNG while preserving all metadata.
Designed to be used standalone OR imported by the proxy layer.

Design decisions & fixes:
  - pyexiftool 0.5.6: encoding="utf-8" fixes Windows charmap crash (0x81 etc.)
  - PNG output: truly lossless, universally supported (Win/iOS/Android/Immich)
  - Orientation baked into pixels; EXIF Orientation tag stripped after
  - ALL three Windows timestamps synced (mtime via os.utime, ctime via pywin32)
  - Parallel processing via ProcessPoolExecutor — each worker gets its own
    ExifTool instance (single ExifTool is not thread/process-safe to share)
  - Non-HEIC files passed through unchanged
"""

from pathlib import Path
from PIL import Image, ImageOps
import pillow_heif
from datetime import datetime, timezone
import exiftool
import shutil
import os
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed

pillow_heif.register_heif_opener()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEIC_SUFFIXES = {".heic", ".heif"}

# PNG compression 0–9. Lossless at every level — only speed/size tradeoff.
# 1 = fast encode, reasonable size. Raise to 6–9 for smaller files.
PNG_COMPRESS_LEVEL = 1


# ---------------------------------------------------------------------------
# Windows ctime helper
# ---------------------------------------------------------------------------

def _set_windows_ctime(path: Path, dt: datetime) -> None:
    """
    Set the Windows 'Date Created' (ctime) on a file.

    os.utime() can only set mtime/atime. The only way to set ctime on Windows
    is via the Win32 API. This function is a no-op on non-Windows platforms.
    Requires: pip install pywin32
    """
    try:
        import pywintypes
        import win32file
        import win32con

        # Convert datetime → Windows FILETIME (100-nanosecond intervals since 1601)
        win_time = pywintypes.Time(dt.replace(tzinfo=timezone.utc).timestamp())

        handle = win32file.CreateFile(
            str(path),
            win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_ATTRIBUTE_NORMAL,
            None,
        )
        try:
            win32file.SetFileTime(handle, win_time, None, None)  # set ctime only
        finally:
            handle.Close()

    except ImportError:
        pass   # pywin32 not installed — silently skip, mtime is still correct
    except Exception:
        pass   # non-Windows or access denied — silently skip


def sync_all_timestamps(path: Path, dt: datetime) -> None:
    """
    Sync all three Windows timestamps to the photo's EXIF datetime:
      - mtime  (Modified)  → os.utime()
      - atime  (Accessed)  → os.utime()
      - ctime  (Created)   → win32file.SetFileTime()  [Windows only]
    """
    ts = dt.replace(tzinfo=timezone.utc).timestamp()
    os.utime(path, (ts, ts))       # sets mtime + atime on all platforms
    _set_windows_ctime(path, dt)   # sets ctime on Windows via Win32 API


# ---------------------------------------------------------------------------
# ExifTool helpers
# ---------------------------------------------------------------------------

def make_exiftool() -> exiftool.ExifTool:
    """
    UTF-8 ExifTool instance for pyexiftool 0.5.x.
    The encoding parameter sets the pipe decode codec — this is the correct
    fix for the Windows charmap crash, not -charset UTF8 (which only affects
    internal tag value interpretation, not the stdout pipe).
    """
    return exiftool.ExifTool(encoding="utf-8")




def get_device_name(metadata: dict) -> str | None:
    """
    Extract a filesystem-safe device name from EXIF metadata.

    Tries EXIF Make + Model (e.g. "Apple iPhone_15"), then Model alone,
    then Make alone. Spaces replaced with underscores, unsafe chars stripped.

    iPhone:  Make="Apple"  Model="iPhone 15"   → "Apple_iPhone_15"
    Android: Make="samsung" Model="SM-S918B"   → "samsung_SM-S918B"
    """
    import re
    make  = metadata.get("EXIF:Make",  "").strip()
    model = metadata.get("EXIF:Model", "").strip()

    # Model often already contains make (e.g. "Apple iPhone 15") — don't double it
    if make and model:
        device = model if model.lower().startswith(make.lower()) else f"{make} {model}"
    elif model:
        device = model
    elif make:
        device = make
    else:
        return None

    device = re.sub(r"\s+", "_", device)          # spaces → underscores
    device = re.sub(r"[^\w\-]", "", device)       # strip unsafe chars
    return device or None


def get_file_info(et: exiftool.ExifTool, file: Path) -> tuple[datetime | None, str | None]:
    """
    Single ExifTool read that returns both:
      - best available capture datetime
      - filesystem-safe device name (Make + Model)

    Combining both into one call avoids a second ExifTool round-trip per file.
    Priority for datetime: DateTimeOriginal → QuickTime CreateDate → CreateDate → FileModifyDate
    """
    metadata = et.execute_json(str(file))[0]

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

    device = get_device_name(metadata)
    return dt, device



def copy_metadata_and_fix_dates(
    et: exiftool.ExifTool,
    src: Path,
    dst: Path,
    dt: datetime | None,
) -> None:
    """
    Single ExifTool call:
      1. Copies ALL tags from src → dst
      2. Overwrites EXIF/XMP date fields with parsed datetime
      3. Clears EXIF Orientation tag (pixels already correctly rotated)

    dst MUST exist on disk before calling this.
    """
    args = [
        "-TagsFromFile", str(src),
        "-all:all>all:all",
        "-EXIF:Orientation=",     # strip — already baked into pixels
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
# Core conversion (single file)
# ---------------------------------------------------------------------------

def convert_heic_to_png(
    file: Path,
    output_dir: Path,
    et: exiftool.ExifTool,
) -> Path:
    """
    Convert a single HEIC/HEIF → lossless PNG.
    Returns the output Path.
    """
    dt, device = get_file_info(et, file)

    # Filename format: DATE_DEVICE_ORIGINALNAME  or  DATE_ORIGINALNAME
    date_part   = dt.strftime("%Y-%m-%d") if dt else "no_date"
    stem        = file.stem                              # e.g. IMG_0035
    if device:
        base = f"{date_part}_{device}_{stem}"
    else:
        base = f"{date_part}_{stem}"

    output = output_dir / f"{base}.png"
    counter = 1
    while output.exists():
        output = output_dir / f"{base}_{counter}.png"
        counter += 1

    # Step 1: decode → correct orientation → save PNG
    with Image.open(file) as img:
        img = ImageOps.exif_transpose(img)
        mode = "RGBA" if img.mode in ("RGBA", "PA") else "RGB"
        img.convert(mode).save(output, "PNG", compress_level=PNG_COMPRESS_LEVEL)

    # Step 2: copy metadata into the saved PNG (file must exist first)
    copy_metadata_and_fix_dates(et, file, output, dt)

    # Step 3: sync all filesystem timestamps
    if dt:
        sync_all_timestamps(output, dt)

    return output


def process_file(file: Path, output_dir: Path, et: exiftool.ExifTool) -> str:
    """
    Route one file: HEIC → PNG, everything else → copy unchanged.
    Returns a result string for logging.
    """
    try:
        if file.suffix.lower() in HEIC_SUFFIXES:
            out = convert_heic_to_png(file, output_dir, et)
            return f"[CONVERTED]   {file.name}  →  {out.name}"
        else:
            shutil.copy2(file, output_dir / file.name)
            return f"[PASSTHROUGH] {file.name}"
    except Exception as exc:
        return f"[ERROR]       {file.name}  |  {exc}"


# ---------------------------------------------------------------------------
# Worker entrypoint (runs in a subprocess — has its own ExifTool instance)
# ---------------------------------------------------------------------------

def _worker(args: tuple[Path, Path]) -> str:
    """
    Top-level function (must be importable for multiprocessing on Windows).
    Each worker process creates its own ExifTool instance — they are not
    safe to share across process boundaries.
    """
    file, output_dir = args
    with make_exiftool() as et:
        return process_file(file, output_dir, et)


# ---------------------------------------------------------------------------
# Batch entry point
# ---------------------------------------------------------------------------

def batch_process(
    input_dir: Path,
    output_dir: Path,
    workers: int | None = None,
) -> None:
    """
    Process all files in input_dir in parallel, writing to output_dir.

    workers: number of parallel processes.
             Defaults to min(cpu_count, 8) — ExifTool is CPU-bound so
             going beyond physical core count gives diminishing returns.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    files = [f for f in input_dir.iterdir() if f.is_file()]

    if not files:
        print("No files found.")
        return

    n_workers = workers or min(multiprocessing.cpu_count(), 8)
    print(f"Processing {len(files)} file(s) with {n_workers} worker(s)...\n")

    work = [(f, output_dir) for f in files]

    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_worker, item): item[0] for item in work}
        for future in as_completed(futures):
            print(future.result())


# ---------------------------------------------------------------------------
# In-memory conversion (proxy layer)
# ---------------------------------------------------------------------------

def convert_heic_bytes_to_png(
    data: bytes,
    original_filename: str,
    et: exiftool.ExifTool,
    tmp_dir: Path,
) -> tuple[bytes, str]:
    """
    Accepts raw HEIC bytes → returns (png_bytes, new_filename).
    ExifTool needs real file paths so tmp_dir is used for intermediates.
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

    out_path = tmp_dir / f"{src_path.stem}_out.png"

    try:
        dt, _device = get_file_info(et, src_path)

        with Image.open(src_path) as img:
            img = ImageOps.exif_transpose(img)
            mode = "RGBA" if img.mode in ("RGBA", "PA") else "RGB"
            img.convert(mode).save(out_path, "PNG", compress_level=PNG_COMPRESS_LEVEL)

        copy_metadata_and_fix_dates(et, src_path, out_path, dt)

        if dt:
            sync_all_timestamps(out_path, dt)

        return out_path.read_bytes(), stem + ".png"

    finally:
        src_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    INPUT_DIR  = Path(r"D:\HomeLab\HomelabAutomation\sample\input")
    OUTPUT_DIR = Path(r"D:\HomeLab\HomelabAutomation\sample\output")
    batch_process(INPUT_DIR, OUTPUT_DIR)