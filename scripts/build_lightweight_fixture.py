"""Build the deterministic lightweight data-room ZIP used by local tests."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "lightweight_dataroom"
OUTPUT = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "lightweight_dataroom.zip"


def main() -> None:
    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(SOURCE_ROOT.rglob("*")):
            if not path.is_file():
                continue
            info = ZipInfo(path.relative_to(SOURCE_ROOT).as_posix())
            info.date_time = (2026, 6, 30, 12, 0, 0)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    print(f"Wrote {OUTPUT.relative_to(PROJECT_ROOT)} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
