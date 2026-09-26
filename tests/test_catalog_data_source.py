from __future__ import annotations

import unittest
from pathlib import Path


class CatalogDataSourceTests(unittest.TestCase):
    def test_full_messier_source_is_not_bundled_yet(self) -> None:
        root = Path(__file__).resolve().parent.parent
        candidates = [
            root / "data" / "catalogs" / "messier.json",
            root / "tsn_dss" / "data" / "catalogs" / "messier.json",
            root / "sqlite" / "catalogs" / "messier.json",
        ]
        self.assertFalse(any(path.exists() for path in candidates))


if __name__ == "__main__":
    unittest.main()
