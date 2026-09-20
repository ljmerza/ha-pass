"""Where the service worker is served from, and what that path scopes it to.

A worker controls only pages at or below the directory its script was served
from, so the registration at /static/sw.js scoped the worker to /static/ — a
directory with no pages in it. It installed, precached the shell, showed up
activated in DevTools, and controlled nothing: the guest pages live at
/g/<slug>. The worker is now served from /g/sw.js, whose directory is exactly
the guest pages.

The browser is what enforces scope, so none of this can be caught by making a
request. What is checked instead is the relationship between three strings —
the path the server serves the script from, the scope the page asks for, and
the guest URLs that have to fall inside it.
"""
import json
import re
import shutil
import subprocess
import textwrap
from unittest.mock import patch

import pytest

from app.routers import guest

# register('<url>', { scope: '<scope>' }) as the page emits it.
REGISTER_RE = re.compile(
    r"navigator\.serviceWorker\.register\('([^']+)',\s*\{\s*scope:\s*'([^']+)'\s*\}\)"
)

# The whole guarded block, for running in node below.
BLOCK_RE = re.compile(r"if \('serviceWorker' in navigator\) \{.*?\n\}", re.S)

JS_CONTENT_TYPES = ("text/javascript", "application/javascript")

INGRESS_PREFIX = "/api/hassio_ingress/abc123"


def _registration(html: str) -> tuple[str, str]:
    match = REGISTER_RE.search(html)
    assert match, "the guest page emits no service worker registration"
    return match.group(1), match.group(2)


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

async def test_the_worker_is_served_from_the_guest_path(client, mock_ha_client, test_db):
    resp = await client.get("/g/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].split(";")[0] in JS_CONTENT_TYPES
    assert "addEventListener('fetch'" in resp.text


async def test_the_worker_is_the_file_the_build_stamps(client, mock_ha_client, test_db):
    """Served from static/sw.js on disk, verbatim — that file is the one the
    Dockerfile seds CACHE_VERSION into, and templating it here instead would
    leave the stamp with nothing to write to."""
    resp = await client.get("/g/sw.js")
    assert resp.text == guest.SW_PATH.read_text()


async def test_the_served_worker_carries_a_stamped_cache_version(
    client, mock_ha_client, test_db, monkeypatch, tmp_path
):
    """What the built image serves: the Dockerfile's sed has already replaced
    the placeholder, and the route hands that through untouched."""
    stamped = tmp_path / "sw.js"
    stamped.write_text(
        guest.SW_PATH.read_text().replace(
            "CACHE_VERSION_PLACEHOLDER", "homepass-0123456789ab"
        )
    )
    monkeypatch.setattr(guest, "SW_PATH", stamped)

    resp = await client.get("/g/sw.js")
    assert resp.status_code == 200
    assert "const CACHE_VERSION = 'homepass-0123456789ab';" in resp.text
    assert "CACHE_VERSION_PLACEHOLDER" not in resp.text


async def test_an_unstamped_checkout_still_serves_a_usable_worker(
    client, mock_ha_client, test_db
):
    """Running from a checkout there is no Docker build and so no sed. The
    placeholder is a valid cache name, so the worker installs and caches under
    it — one shared cache across dev edits, which is the dev story anyway."""
    resp = await client.get("/g/sw.js")
    version = re.search(r"const CACHE_VERSION = '([^']*)';", resp.text)
    assert version, "the worker declares no cache name"
    assert version.group(1), "an empty cache name would break caches.open()"


async def test_the_slug_route_does_not_answer_for_the_worker(
    client, mock_ha_client, sample_token
):
    """/g/{slug} matches 'sw.js' happily and would return the expired page —
    HTML where a worker script is expected, which fails registration silently.
    The worker route is declared first; this is what holds that in place."""
    resp = await client.get("/g/sw.js")
    assert resp.headers["content-type"].split(";")[0] in JS_CONTENT_TYPES
    assert "<html" not in resp.text.lower()


def test_no_token_slug_can_shadow_the_worker_path():
    """The other half of the collision: a slug of 'sw.js' would be unreachable
    behind the worker route. It cannot be minted — generated slugs are hex and
    a custom one has no dot in its character class."""
    from pydantic import ValidationError

    from app.models import TokenCreateRequest
    from app.routers.admin import _generate_slug

    with pytest.raises(ValidationError):
        TokenCreateRequest(
            label="x", slug="sw.js", entity_ids=["light.a"], expires_in_seconds=60
        )
    assert re.fullmatch(r"[0-9a-f]{32}", _generate_slug())


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
# The max-scope rule: a registration is rejected unless the requested scope is
# at or below the directory the worker script was served from, and a server can
# only widen that by sending Service-Worker-Allowed on the script response. No
# header is sent here, so the scope has to be exactly the script's directory or
# narrower — and it has to still cover /g/<slug>, or the guest page it was
# written for is not controlled. Only the browser enforces this, so the three
# strings are compared directly.

async def test_the_scope_is_within_the_served_script_directory(
    client, mock_ha_client, sample_token
):
    url, scope = _registration((await client.get("/g/test-token")).text)

    script_dir = url.rsplit("/", 1)[0] + "/"
    assert scope.startswith(script_dir), (
        f"scope {scope} is above {script_dir}; without a Service-Worker-Allowed "
        "header on the worker response the browser rejects this registration"
    )
    assert "service-worker-allowed" not in (await client.get(url)).headers


async def test_the_scope_covers_the_guest_page_and_nothing_else(
    client, mock_ha_client, sample_token
):
    """The original bug in one assertion: /static/ does not prefix /g/<slug>.
    The scope is not widened to / either — the admin UI has no worker and does
    not want one in front of it."""
    _, scope = _registration((await client.get("/g/test-token")).text)

    assert "/g/test-token".startswith(scope)
    assert not "/admin/dashboard".startswith(scope)
    assert not "/health".startswith(scope)


async def test_the_registered_url_is_one_the_server_serves(
    client, mock_ha_client, sample_token
):
    """A 404 here registers nothing at all, which looks identical from the page:
    the .catch() swallows it."""
    url, _ = _registration((await client.get("/g/test-token")).text)
    resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.headers["content-type"].split(";")[0] in JS_CONTENT_TYPES


async def test_the_worker_scope_covers_the_manifest_scope(
    client, mock_ha_client, sample_token
):
    """An installed PWA navigates inside the manifest's scope. A worker scope
    that did not contain it would leave the installed app uncontrolled while the
    browser tab was fine."""
    _, scope = _registration((await client.get("/g/test-token")).text)
    manifest = (await client.get("/g/test-token/manifest.json")).json()
    assert manifest["scope"].startswith(scope)


# ---------------------------------------------------------------------------
# Ingress
# ---------------------------------------------------------------------------

async def test_no_worker_is_registered_under_ingress(client, mock_ha_client, sample_token):
    """Under ingress the app is served from /api/hassio_ingress/<token>/, and a
    scope above that prefix is not something the browser would allow — so the
    page skips registration entirely. The guard is the rendered base_path."""
    import app.ingress

    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "fake-supervisor-token"):
        html = (await client.get(
            "/g/test-token", headers={"X-Ingress-Path": INGRESS_PREFIX}
        )).text

    assert f"if (!('{INGRESS_PREFIX}'))" in html


# ---------------------------------------------------------------------------
# The emitted block, run
# ---------------------------------------------------------------------------
# The regex assertions above read the page as text. Running the same block in
# node proves the guard actually evaluates the way the interpolated string
# reads, and that register() is called once with the url and scope claimed.
# It proves nothing about scope itself: there is no service worker
# implementation here, only a recording stub.

node = shutil.which("node")

HARNESS = """
const calls = [];
// node ships a read-only navigator of its own, so the stub is defined over it.
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: {
    serviceWorker: {
      register(url, opts) { calls.push({ url, opts }); return { catch() {} }; },
    },
  },
});
"""


def _run_block(html: str) -> list[dict]:
    match = BLOCK_RE.search(html)
    assert match, "the registration block is not in the rendered page"
    proc = subprocess.run(
        [node, "--input-type=module", "-e",
         "\n".join([textwrap.dedent(HARNESS), match.group(0),
                    "console.log(JSON.stringify(calls));"])],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(node is None, reason="node is not installed")
async def test_the_page_registers_the_worker_it_says_it_does(
    client, mock_ha_client, sample_token
):
    calls = _run_block((await client.get("/g/test-token")).text)
    assert calls == [{"url": "/g/sw.js", "opts": {"scope": "/g/"}}]


@pytest.mark.skipif(node is None, reason="node is not installed")
async def test_the_ingress_guard_really_skips_registration(
    client, mock_ha_client, sample_token
):
    import app.ingress

    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "fake-supervisor-token"):
        html = (await client.get(
            "/g/test-token", headers={"X-Ingress-Path": INGRESS_PREFIX}
        )).text

    assert _run_block(html) == []
