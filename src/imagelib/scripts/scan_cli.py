"""Headless ingest entrypoint: index + analyze images without the GUI.

Usage:
    uv run imagelib-scan
"""

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imagelib-scan", description=__doc__)
    parser.add_argument("--scan", action="store_true", help="run stage-1 ingest (walk, hash, EXIF, thumbnails)")
    parser.add_argument("--analyze", action="store_true", help="run stage-2 face analysis")
    args = parser.parse_args(argv)

    if args.scan:
        from imagelib.services.scanner import scan_watched_dirs

        scan_watched_dirs()
        return 0
    if args.analyze:
        raise NotImplementedError("Stage-2 analyzer lands in a later pass.")
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())