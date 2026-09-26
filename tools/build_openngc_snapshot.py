from __future__ import annotations

import argparse
from pathlib import Path

from tsn_dss.engine.openngc import write_openngc_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the pinned TSN DSS OpenNGC snapshot artifact.")
    parser.add_argument(
        "source_root",
        type=Path,
        help="Directory containing database_files/NGC.csv and database_files/addendum.csv from the pinned release.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tsn_dss/data/catalogs/openngc"),
        help="Output directory for objects.json and manifest.json.",
    )
    args = parser.parse_args()
    manifest = write_openngc_snapshot(args.source_root, args.output_dir)
    print(f"Wrote {manifest['generated_artifact']['records']} OpenNGC records")
    print(f"Artifact SHA-256: {manifest['generated_artifact']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
