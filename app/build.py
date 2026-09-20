"""The build identifier stamped onto every static asset URL.

Static files live at fixed paths, so a browser — or a caching reverse proxy in
front of the add-on — that already holds ``/static/dist.css`` has no reason to
ask for it again after an upgrade. Stamping the service worker's cache name
alone does not help: the admin dashboard registers no service worker at all, and
under ingress the guest page does not either. So every asset URL the app emits
carries ``?v=<BUILD_VERSION>``, which makes a new build a new URL.

Two sources, in order:

``GIT_SHA`` is what CI passes to the image build and what already stamps the
service worker's cache name, so reusing it keeps the cache key and the asset
URLs telling the same story. It is constant for the lifetime of an image and
different in the next one, which is exactly the property needed.

Outside a CI build there is no sha — a local ``docker build`` gets the
Dockerfile's ``dev`` default, and running from a checkout has none at all. Both
fall back to a digest of the static directory itself, which is stable for as
long as the files are and changes the moment one is edited. That is the right
behaviour while developing, where assets change without a rebuild.
"""
import hashlib
import os
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# The Dockerfile's ARG default. It is a placeholder, not a build identity, so it
# falls through to the fingerprint rather than pinning every local image to the
# same version.
PLACEHOLDER_SHA = "dev"

# Long enough that two builds cannot collide in practice, short enough to keep
# the query string readable in a network log.
VERSION_LENGTH = 12


def _static_fingerprint() -> str:
    """Digest of every file under ``static/``, by path, size and mtime.

    Contents are deliberately not read: the icons and the compiled CSS are the
    bulk of that directory and this runs once at import. Path, size and mtime
    change on every edit and every rebuild, which is all the URL has to say.
    """
    digest = hashlib.sha256()
    try:
        for path in sorted(STATIC_DIR.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            digest.update(
                f"{path.relative_to(STATIC_DIR)}|{stat.st_size}|{stat.st_mtime_ns}".encode()
            )
    except OSError:
        # A missing or unreadable static dir is a deployment problem, not a
        # reason to fail import. The digest of nothing is still a usable
        # constant, and the assets it would have versioned are not there either.
        pass
    return digest.hexdigest()[:VERSION_LENGTH]


def resolve_build_version() -> str:
    """The value stamped on asset URLs. Never empty."""
    sha = os.environ.get("GIT_SHA", "").strip()
    if sha and sha != PLACEHOLDER_SHA:
        return sha[:VERSION_LENGTH]
    return _static_fingerprint()


BUILD_VERSION = resolve_build_version()
