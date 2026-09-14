from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.ebook_catalog_service import EbookCatalogService  # noqa: E402


def main() -> None:
    catalog = EbookCatalogService()
    print(f"ebook_catalog_valid count={len(catalog.all())}")


if __name__ == "__main__":
    main()
