"""The release that is running: its version and revision (Stage 13).

- ``__version__`` (``app/__init__.py``): the application's version number.
- ``APP_REVISION``: set in the image at build time (``deploy/Dockerfile``, from compose's
  ``IMAGE_TAG``, normally the Git commit), and also an OCI label of the image
  (``org.opencontainers.image.revision``). Outside an image it is usually unset.

Both are logged at startup and recorded in the database's ``app_releases`` table
(``Database.record_release``), so the deployment history is kept with the data and in
every backup. docs/deployment-rollback.md.
"""

import os
import re

from app import __version__

REVISION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
UNKNOWN = ("", "unknown", "local")


def app_revision() -> str | None:
    """The image's ``APP_REVISION``, or None when unset or not a plausible revision."""
    value = os.environ.get("APP_REVISION", "").strip()
    if value in UNKNOWN or not REVISION_PATTERN.fullmatch(value):
        return None
    return value


def release_label() -> str:
    """``0.1.0 (revision 1a2b3c4)``, or ``0.1.0`` without a revision."""
    revision = app_revision()
    return f"{__version__} (revision {revision})" if revision else __version__
