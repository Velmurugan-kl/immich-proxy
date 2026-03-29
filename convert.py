"""
convert.py
----------
CLI entrypoint for batch processing.
Reads settings from config.yaml — run web_ui.py first to configure,
or edit config.yaml directly.

Usage:
    python convert.py                 # use config.yaml defaults
    python convert.py --workers 4     # override worker count for this run
    python convert.py --format jpeg   # override output format for this run
"""

import argparse
from config import load_config
from processor import batch_process


def main() -> None:
    parser = argparse.ArgumentParser(description="HEIC → image batch converter")
    parser.add_argument("--workers", type=int, default=None,
                        help="Override parallel worker count (0=auto)")
    parser.add_argument("--format",  type=str, default=None,
                        choices=["jpeg", "png", "webp", "tiff", "original"],
                        help="Override output format for this run")
    parser.add_argument("--input",   type=str, default=None,
                        help="Override input directory for this run")
    parser.add_argument("--output",  type=str, default=None,
                        help="Override output directory for this run")
    args = parser.parse_args()

    cfg = load_config()

    # Apply CLI overrides without touching config.yaml
    if args.workers is not None:
        cfg.processing.workers = args.workers
    if args.format is not None:
        cfg.output.format = args.format
    if args.input is not None:
        cfg.paths.input_dir = args.input
    if args.output is not None:
        cfg.paths.output_dir = args.output

    batch_process(cfg)


if __name__ == "__main__":
    main()
