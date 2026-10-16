"""Create a staff token for ticket operations (Stage 10).

Usage (from the project root, virtual environment active):

    python -m scripts.create_staff_token maria

Prints a new token for the staff member and the entry to add to ``STAFF_TOKEN_HASHES``
in ``.env``. Give the token to that person only (it is shown once and cannot be
recovered); only the hash goes into the configuration. To revoke a token, remove its
entry and restart the app. Nothing is written anywhere by this script.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/create_staff_token.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.staff_auth import generate_staff_token, hash_staff_token  # noqa: E402
from app.models.support import STAFF_ID_PATTERN  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a staff token.")
    parser.add_argument("staff_id", help="Who the token is for, e.g. maria or agent-07.")
    args = parser.parse_args(argv)
    if not re.fullmatch(STAFF_ID_PATTERN, args.staff_id):
        parser.error("staff_id may contain letters, digits, '.', '_', '@' and '-' (1-64)")
    token = generate_staff_token()
    print(f"Token for {args.staff_id} (shown once; give it only to them):")
    print(f"  {token}")
    print("Add this entry to STAFF_TOKEN_HASHES in .env (a JSON list), then restart:")
    print(f'  "{args.staff_id}:{hash_staff_token(token)}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
