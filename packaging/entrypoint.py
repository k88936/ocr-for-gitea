"""PyInstaller entry point.

Absolute import (not ``python -m``) so the frozen bundle starts cleanly.
"""

from gitea_ocr_review.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
