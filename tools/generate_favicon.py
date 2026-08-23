from __future__ import annotations

from pathlib import Path

from PIL import Image


def main() -> int:
    source = Path("frontend/src/assets/tsn_dss_icon.png")
    public_dir = Path("frontend/public")
    public_dir.mkdir(parents=True, exist_ok=True)
    target = public_dir / "favicon.ico"

    with Image.open(source) as image:
        image = image.convert("RGBA")
        sizes = [(16, 16), (32, 32), (48, 48)]
        image.save(target, format="ICO", sizes=sizes)

    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
