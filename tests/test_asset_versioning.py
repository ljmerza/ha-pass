"""Static assets are served from fixed paths, so an upgraded add-on kept serving
the previous build's CSS and JS out of the browser's cache. Every asset URL the
app emits now carries ?v=<build>.

The property these hold down is "every", not "some": a template that missed the
stamp keeps its assets stale while the versioned pages around it look fixed, so
each page is scanned for asset URLs and every one of them has to carry it.
"""
import json
import re
import shutil
import subprocess
import textwrap
import time
from unittest.mock import patch

import pytest

from app import build, database as db, guest_pin

# href="..." / src="..." values that point into /static/. The service worker
# registration URL is deliberately excluded here: it is a bare JS string, not an
# attribute, and is unversioned on purpose (see the comment in guest_pwa.html).
ASSET_ATTR_RE = re.compile(r'(?:href|src)="([^"]*/static/[^"]*)"')

SUFFIX = f"?v={build.BUILD_VERSION}"


def _assets(html: str) -> list[str]:
    urls = ASSET_ATTR_RE.findall(html)
    assert urls, "page emits no static asset URLs — the scan proves nothing"
    return urls


def _assert_all_versioned(html: str) -> list[str]:
    urls = _assets(html)
    unversioned = [u for u in urls if not u.endswith(SUFFIX)]
    assert not unversioned, f"unversioned static assets: {unversioned}"
    return urls


# ---------------------------------------------------------------------------
# Every page that emits an asset URL
# ---------------------------------------------------------------------------

async def test_guest_page_assets_are_versioned(client, mock_ha_client, sample_token):
    resp = await client.get("/g/test-token")
    assert resp.status_code == 200
    urls = _assert_all_versioned(resp.text)
    # The shared head from base.html plus the guest page's own scripts.
    assert any("dist.css" in u for u in urls)
    assert any("util.js" in u for u in urls)


async def test_admin_dashboard_assets_are_versioned(client, admin_session, mock_ha_client):
    """The dashboard registers no service worker at all, so the URL stamp is the
    only thing standing between an upgrade and a stale picker."""
    resp = await client.get("/admin/dashboard", cookies=admin_session)
    assert resp.status_code == 200
    urls = _assert_all_versioned(resp.text)
    assert any("qrcode.min.js" in u for u in urls)


async def test_pin_entry_page_assets_are_versioned(client, mock_ha_client, test_db):
    await db.create_token(
        label="Locked",
        slug="locked-assets",
        entity_ids=["light.living_room"],
        expires_at=int(time.time()) + 3600,
        ip_allowlist=None,
        pin_hash=await guest_pin.hash_pin("4821"),
    )
    resp = await client.get("/g/locked-assets")
    assert resp.status_code == 200
    _assert_all_versioned(resp.text)


async def test_expired_page_assets_are_versioned(client, mock_ha_client, test_db):
    resp = await client.get("/g/no-such-token")
    assert resp.status_code == 410
    _assert_all_versioned(resp.text)


async def test_ingress_prefix_and_version_ride_together(
    client, mock_ha_client, sample_token
):
    """Under ingress the asset path is prefixed; the stamp still lands last."""
    import app.ingress

    prefix = "/api/hassio_ingress/abc123"
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "fake-supervisor-token"):
        resp = await client.get("/g/test-token", headers={"X-Ingress-Path": prefix})

    assert resp.status_code == 200
    urls = _assert_all_versioned(resp.text)
    assert all(u.startswith(f"{prefix}/static/") for u in urls)


# ---------------------------------------------------------------------------
# The generated PWA manifest
# ---------------------------------------------------------------------------

async def test_manifest_icon_urls_are_versioned(client, mock_ha_client, sample_token):
    resp = await client.get("/g/test-token/manifest.json")
    assert resp.status_code == 200
    icons = resp.json()["icons"]
    assert len(icons) == 4
    for icon in icons:
        assert icon["src"].endswith(SUFFIX), icon["src"]


# ---------------------------------------------------------------------------
# The value itself
# ---------------------------------------------------------------------------

async def test_the_version_is_stable_within_one_build(
    client, mock_ha_client, sample_token
):
    """Two renders of two different pages agree, and so does the manifest — a
    version that moved between requests would bust the cache on every load."""
    first = _assets((await client.get("/g/test-token")).text)
    second = _assets((await client.get("/g/test-token")).text)
    manifest = (await client.get("/g/test-token/manifest.json")).json()

    versions = {u.split("?v=")[1] for u in first + second}
    versions |= {i["src"].split("?v=")[1] for i in manifest["icons"]}
    assert len(versions) == 1
    assert versions == {build.BUILD_VERSION}


def test_a_checkout_with_no_git_sha_still_gets_a_usable_version(monkeypatch):
    """Local dev has no Docker build and so no GIT_SHA. It must not render
    `?v=None`, `?v=` or anything else a cache would key on oddly."""
    monkeypatch.delenv("GIT_SHA", raising=False)
    version = build.resolve_build_version()
    assert re.fullmatch(r"[0-9a-f]{12}", version), version


def test_the_dockerfile_arg_default_is_not_taken_as_a_build_identity(monkeypatch):
    """`dev` is the Dockerfile's placeholder, so every local image would share
    one version. It falls through to the static fingerprint instead."""
    monkeypatch.setenv("GIT_SHA", build.PLACEHOLDER_SHA)
    with_placeholder = build.resolve_build_version()
    monkeypatch.delenv("GIT_SHA")
    assert with_placeholder == build.resolve_build_version()


def test_a_ci_sha_is_used_verbatim(monkeypatch):
    """CI passes GIT_SHA as a build arg, and the Dockerfile puts it in the
    runtime env — reusing it keeps the asset URLs and the service worker's
    cache name naming the same build."""
    monkeypatch.setenv("GIT_SHA", "0123456789abcdef0123456789abcdef01234567")
    assert build.resolve_build_version() == "0123456789ab"


def test_the_fingerprint_moves_when_a_static_file_does(monkeypatch, tmp_path):
    """The dev fallback has to change when an asset is edited, or editing CSS
    during development would never reach the browser."""
    monkeypatch.delenv("GIT_SHA", raising=False)
    asset = tmp_path / "dist.css"
    asset.write_text("body{}")
    monkeypatch.setattr(build, "STATIC_DIR", tmp_path)

    before = build.resolve_build_version()
    asset.write_text("body{color:red}")
    assert build.resolve_build_version() != before


def test_the_version_is_importable_without_a_static_directory(monkeypatch, tmp_path):
    """A missing static dir is a deployment fault, not a reason to fail import."""
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.setattr(build, "STATIC_DIR", tmp_path / "gone")
    assert re.fullmatch(r"[0-9a-f]{12}", build.resolve_build_version())


# ---------------------------------------------------------------------------
# The service worker's cache lookup
# ---------------------------------------------------------------------------
# Versioning the URLs without this is worse than not versioning them: the shell
# is precached under plain paths at install, pages ask for ?v=<build>, and an
# exact-match lookup misses every one of them. Run in node against the shipped
# static/sw.js, the same way test_picker_js.py runs the dashboard's script.

node = shutil.which("node")

SW_HARNESS = """
const handlers = {};
const store = new Map();
const abs = u => new URL(typeof u === 'string' ? u : u.url, 'http://testserver').href;
let networkHits = 0;

const cache = {
  addAll: async urls => { for (const u of urls) store.set(abs(u), 'precached:' + u); },
  put: async (req, res) => { store.set(abs(req), res); },
};
globalThis.caches = {
  open: async () => cache,
  keys: async () => [],
  delete: async () => true,
  match: async (req, opts) => {
    const url = abs(req);
    if (store.has(url)) return store.get(url);
    if (opts && opts.ignoreSearch) {
      const bare = url.split('?')[0];
      for (const [k, v] of store) if (k.split('?')[0] === bare) return v;
    }
    return undefined;
  },
};
globalThis.self = {
  addEventListener: (type, fn) => { handlers[type] = fn; },
  skipWaiting() {},
  clients: { claim() {} },
};
globalThis.fetch = async req => {
  networkHits++;
  const tag = 'network:' + abs(req);
  return { tag, clone: () => tag };
};

async function request(path) {
  let answered;
  handlers.fetch({
    request: { url: abs(path) },
    respondWith: p => { answered = p; },
  });
  return answered === undefined ? 'passthrough' : await answered;
}
"""


def _sw(probe: str) -> dict:
    with open("static/sw.js") as fh:
        sw = fh.read()
    proc = subprocess.run(
        [node, "--input-type=module", "-e",
         "\n".join([SW_HARNESS, sw, textwrap.dedent(probe)])],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_precached_assets_are_served_for_a_versioned_url():
    """The exact bug the sibling fork shipped and had to fix: plain precache
    keys, versioned requests, and a lookup that matched neither."""
    out = _sw("""
    let installed;
    handlers.install({ waitUntil: p => { installed = p; } });
    await installed;
    const hit = await request('/static/dist.css?v=deadbeef1234');
    console.log(JSON.stringify({ hit, network: networkHits }));
    """)
    assert out["hit"] == "precached:/static/dist.css"
    # Served from cache, with the revalidation fetch still in flight behind it.
    assert out["network"] == 1


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_an_asset_that_was_never_precached_still_falls_through_to_the_network():
    out = _sw("""
    let installed;
    handlers.install({ waitUntil: p => { installed = p; } });
    await installed;
    const hit = await request('/static/unknown.js?v=deadbeef1234');
    console.log(JSON.stringify({ hit: hit.tag }));
    """)
    assert out["hit"] == "network:http://testserver/static/unknown.js?v=deadbeef1234"


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_the_shell_list_matches_the_assets_the_templates_emit():
    """A precached path the pages never request is dead weight in the install,
    and it is the ignoreSearch match that lets the two lists be written
    differently — so they are checked against each other here."""
    out = _sw("console.log(JSON.stringify({ shell: SHELL_ASSETS }));")
    shell = set(out["shell"])
    emitted = set()
    for name in ("base.html", "guest_pwa.html", "pin_entry.html"):
        with open(f"templates/{name}") as fh:
            for url in ASSET_ATTR_RE.findall(fh.read()):
                emitted.add("/static/" + url.split("/static/", 1)[1].split("?")[0])
    assert emitted <= shell, f"guest pages request assets the shell never caches: {emitted - shell}"
