"""Headless ingest entrypoint: index + analyse images without the GUI.

Usage:
    uv run imagelib-scan
"""

import argparse
from tqdm import tqdm


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imagelib-scan", description=__doc__)
    parser.add_argument("--scan", action="store_true", help="run stage-1 ingest (walk, hash, EXIF, thumbnails)")
    parser.add_argument("--analyse", action="store_true", help="run stage-2 face analysis")
    args = parser.parse_args(argv)

    if args.scan:
        from imagelib.services.scanner import scan_watched_dirs

        report = scan_watched_dirs(progress=lambda path: None)
        print(
            f"scan: discovered={report.discovered} indexed={report.indexed} "
            f"unchanged={report.unchanged} errors={report.errors}"
        )
        return 0
    if args.analyse:
        from imagelib.services.analyser import analyse_images

        with tqdm(desc="analysis", unit="image") as bar:
            report = analyse_images(progress=lambda image: bar.update(1))
        print(
            f"analysis: discovered={report.discovered} analysed={report.analysed} "
            f"faces={report.faces} errors={report.errors}"
        )
        return 0 if report.errors == 0 else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
