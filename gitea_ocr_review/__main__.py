"""Allow ``python -m gitea_ocr_review``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
