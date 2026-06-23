#!/usr/bin/env python3
"""Entry point. See `python book.py --help`.

Examples:
  python book.py inspect                      # capture your portal's selectors
  python book.py book --date 2026-07-07 --dry-run
  python book.py schedule --date 2026-07-07   # wait for 12:01am release, then book
"""

from tee_booker.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
