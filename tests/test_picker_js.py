"""The entity picker's browser-side filtering, bulk add and template loading.

The picker lives in an inline <script> in templates/admin_dashboard.html, so
there is nothing importable to unit-test. Instead the page is rendered through
the real admin route, its script is dropped into Node alongside the same static
files the browser loads, and the functions are called against a stub DOM.

That keeps the assertions on the shipped code rather than on a copy of it: if
renderPicker stops gating the label row on labels_available, or a bulk add
starts honouring the 100-row render cap, these fail.

Skipped when node is not installed — the rest of the suite is pure Python and
must not start requiring a JS runtime.
"""
import json
import re
import shutil
import subprocess
import textwrap

import pytest

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")

REPO_SCRIPTS = ("static/util.js", "static/domains.js", "static/theme.js")

# Enough of a DOM for the dashboard script to reach the end of its top-level
# without throwing. Elements are cached per id so a render can be read back.
DOM_STUB = """
const _els = {};
function fakeEl(id) {
  return {
    id, style: {}, dataset: {}, value: '', textContent: '', innerHTML: '',
    offsetWidth: 0, offsetLeft: 0, children: [],
    classList: { add() {}, remove() {}, contains() { return true; }, toggle() {} },
    addEventListener() {}, removeEventListener() {}, appendChild() {}, remove() {},
    setAttribute() {}, getAttribute() { return null; }, focus() {}, blur() {},
    setSelectionRange() {}, click() {}, closest() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    getContext() { return null; },
  };
}
globalThis.document = {
  body: fakeEl('body'),
  documentElement: fakeEl('html'),
  activeElement: null,
  visibilityState: 'hidden',
  getElementById(id) { return (_els[id] ||= fakeEl(id)); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { return fakeEl(tag); },
  addEventListener() {},
};
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.window = {
  matchMedia() { return { matches: false, addEventListener() {} }; },
};
globalThis.matchMedia = globalThis.window.matchMedia;
globalThis.location = { origin: 'http://testserver' };
// navigator is getter-only on modern Node, so it is defined rather than assigned.
Object.defineProperty(globalThis, 'navigator', { value: {}, configurable: true });
globalThis.requestAnimationFrame = () => 0;
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.setTimeout = () => 0;
globalThis.fetch = () => Promise.reject(new Error('no network in the harness'));
"""


def _dashboard_script(html: str) -> str:
    """The dashboard's own inline script — the last nonce'd block on the page."""
    blocks = re.findall(r'<script nonce="[^"]*">(.*?)</script>', html, re.S)
    assert blocks, "no inline script found in the rendered dashboard"
    return blocks[-1]


async def _run(client, admin_session, probe: str):
    """Render the dashboard, run `probe` against its script in node, return JSON."""
    resp = await client.get("/admin/dashboard", cookies=admin_session)
    assert resp.status_code == 200
    parts = [DOM_STUB]
    for rel in REPO_SCRIPTS:
        with open(rel) as fh:
            parts.append(fh.read())
    parts.append(_dashboard_script(resp.text))
    parts.append(textwrap.dedent(probe))
    proc = subprocess.run(
        [node, "--input-type=module", "-e", "\n".join(parts)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# A small HA that exercises every axis: two domains, two labels, overlapping
# names. guest_bulb wears both labels; hall_switch wears neither.
ENTITIES = [
    {"entity_id": "light.guest_bulb", "friendly_name": "Guest Bulb", "domain": "light",
     "state": "on", "labels": ["lbl_guest", "lbl_upstairs"]},
    {"entity_id": "light.hall_lamp", "friendly_name": "Hall Lamp", "domain": "light",
     "state": "off", "labels": ["lbl_guest"]},
    {"entity_id": "switch.hall_switch", "friendly_name": "Hall Switch", "domain": "switch",
     "state": "off", "labels": []},
    {"entity_id": "switch.guest_fan", "friendly_name": "Guest Fan", "domain": "switch",
     "state": "off", "labels": ["lbl_guest"]},
]
LABELS = [
    {"label_id": "lbl_guest", "name": "Guest", "color": "#aabbcc", "icon": "mdi:account"},
    {"label_id": "lbl_upstairs", "name": "Upstairs", "color": "purple", "icon": None},
    {"label_id": "lbl_unused", "name": "Unused", "color": "#112233", "icon": None},
]

_SEED = f"""
const ENTITIES = {json.dumps(ENTITIES)};
const LABELS = {json.dumps(LABELS)};
"""


# ---------------------------------------------------------------------------
# loadEntities — both response shapes
# ---------------------------------------------------------------------------

async def test_load_entities_unpacks_the_label_envelope(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, _SEED + """
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({ entities: ENTITIES, labels: LABELS, labels_available: true }),
    });
    await loadEntities();
    console.log(JSON.stringify({
      entities: allEntities.map(e => e.entity_id),
      labels: allLabels.map(l => l.label_id),
      available: labelsAvailable,
    }));
    """)
    assert out["entities"] == [e["entity_id"] for e in ENTITIES]
    assert out["available"] is True
    # lbl_unused is in the catalogue but on no entity, so it gets no chip.
    assert out["labels"] == ["lbl_guest", "lbl_upstairs"]


async def test_load_entities_still_accepts_the_bare_list(client, admin_session, mock_ha_client):
    """The pre-envelope response shape must not blank the picker."""
    out = await _run(client, admin_session, _SEED + """
    globalThis.fetch = async () => ({ ok: true, json: async () => ENTITIES });
    await loadEntities();
    console.log(JSON.stringify({
      entities: allEntities.map(e => e.entity_id),
      labels: allLabels,
      available: labelsAvailable,
    }));
    """)
    assert out["entities"] == [e["entity_id"] for e in ENTITIES]
    assert out["labels"] == []
    assert out["available"] is False


# ---------------------------------------------------------------------------
# Label filter, and how it composes with the domain chips and the search box
# ---------------------------------------------------------------------------

async def test_label_domain_and_search_intersect(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, _SEED + """
    allEntities = ENTITIES;
    const ids = s => pickerFiltered(Object.assign(
      { selected: new Set(), filter: 'all', label: '', search: '', meta: {} }, s
    )).map(e => e.entity_id);
    console.log(JSON.stringify({
      none:          ids({}),
      label:         ids({ label: 'lbl_guest' }),
      domain:        ids({ filter: 'light' }),
      search:        ids({ search: 'hall' }),
      label_domain:  ids({ label: 'lbl_guest', filter: 'light' }),
      label_search:  ids({ label: 'lbl_guest', search: 'hall' }),
      all_three:     ids({ label: 'lbl_guest', filter: 'light', search: 'guest' }),
      empty_overlap: ids({ label: 'lbl_upstairs', filter: 'switch' }),
    }));
    """)
    assert out["none"] == [e["entity_id"] for e in ENTITIES]
    assert out["label"] == ["light.guest_bulb", "light.hall_lamp", "switch.guest_fan"]
    assert out["domain"] == ["light.guest_bulb", "light.hall_lamp"]
    assert out["search"] == ["light.hall_lamp", "switch.hall_switch"]
    # Each pair is the intersection, not the union or a replacement.
    assert out["label_domain"] == ["light.guest_bulb", "light.hall_lamp"]
    assert out["label_search"] == ["light.hall_lamp"]
    assert out["all_three"] == ["light.guest_bulb"]
    assert out["empty_overlap"] == []


async def test_clicking_the_active_label_chip_clears_it(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, _SEED + """
    allEntities = ENTITIES;
    const seen = [];
    setPickerLabel('create-picker', 'lbl_guest'); seen.push(createPicker.label);
    setPickerLabel('create-picker', 'lbl_upstairs'); seen.push(createPicker.label);
    setPickerLabel('create-picker', 'lbl_upstairs'); seen.push(createPicker.label);
    console.log(JSON.stringify({ seen }));
    """)
    assert out["seen"] == ["lbl_guest", "lbl_upstairs", ""]


async def test_label_row_is_hidden_when_labels_are_unavailable(client, admin_session, mock_ha_client):
    """labels_available false means no filter at all, not an empty one."""
    out = await _run(client, admin_session, _SEED + """
    allEntities = ENTITIES;
    const render = () => {
      renderPicker('create-picker', createPicker);
      return document.getElementById('create-picker').innerHTML;
    };
    allLabels = []; labelsAvailable = false;
    const off = render();
    allLabels = LABELS.slice(0, 2); labelsAvailable = true;
    const on = render();
    console.log(JSON.stringify({
      off_has_row: off.includes('picker-label'),
      on_has_row: on.includes('picker-label'),
      on_has_name: on.includes('Upstairs'),
      // A hex colour rides in an inline style; Tailwind cannot compile a class
      // for a value that only exists at runtime.
      on_has_hex: on.includes('background-color:#aabbcc'),
      on_has_bare_name: on.includes('background-color:purple'),
    }));
    """)
    assert out["off_has_row"] is False
    assert out["on_has_row"] is True
    assert out["on_has_name"] is True
    assert out["on_has_hex"] is True
    # "purple" is not a hex value, so nothing is interpolated into the style.
    assert out["on_has_bare_name"] is False


# ---------------------------------------------------------------------------
# Bulk add
# ---------------------------------------------------------------------------

async def test_add_all_takes_every_match_not_just_the_rendered_hundred(
    client, admin_session, mock_ha_client
):
    out = await _run(client, admin_session, """
    allEntities = Array.from({ length: 150 }, (_, i) => ({
      entity_id: `light.bulb_${i}`, friendly_name: `Bulb ${i}`, domain: 'light',
      state: 'off', labels: i % 2 ? ['lbl_guest'] : ['lbl_other'],
    }));
    createPicker.label = 'lbl_guest';
    pickerAddAll('create-picker');
    console.log(JSON.stringify({
      count: createPicker.selected.size,
      all_tagged: [...createPicker.selected].every(id => Number(id.split('_')[1]) % 2 === 1),
    }));
    """)
    assert out["count"] == 75
    assert out["all_tagged"] is True


async def test_add_all_is_offered_only_once_something_narrows_the_list(
    client, admin_session, mock_ha_client
):
    out = await _run(client, admin_session, _SEED + """
    allEntities = ENTITIES; allLabels = LABELS.slice(0, 2); labelsAvailable = true;
    const render = () => {
      renderPicker('create-picker', createPicker);
      return document.getElementById('create-picker').innerHTML.includes('picker-add-all');
    };
    const unfiltered = render();
    createPicker.label = 'lbl_guest';
    const byLabel = render();
    createPicker.label = ''; createPicker.search = 'hall';
    const bySearch = render();
    console.log(JSON.stringify({ unfiltered, byLabel, bySearch }));
    """)
    # Unfiltered it would mean "every entity in Home Assistant", so it is absent.
    assert out["unfiltered"] is False
    assert out["byLabel"] is True
    assert out["bySearch"] is True


# ---------------------------------------------------------------------------
# Loading a template into the picker
# ---------------------------------------------------------------------------

async def test_loading_a_template_adds_to_the_selection(client, admin_session, mock_ha_client):
    """Additive, not replace — an in-progress selection survives, and two
    templates can be stacked."""
    out = await _run(client, admin_session, _SEED + """
    allEntities = ENTITIES;
    entityTemplates = [
      { id: 't1', name: 'Lights', entity_ids: ['light.guest_bulb', 'light.hall_lamp'], created_at: 0 },
      { id: 't2', name: 'Switches', entity_ids: ['switch.guest_fan'], created_at: 0 },
    ];
    createPicker.selected.add('switch.hall_switch');
    loadTemplateInto('create-picker', 't1');
    const afterFirst = [...createPicker.selected].sort();
    loadTemplateInto('create-picker', 't2');
    console.log(JSON.stringify({ afterFirst, afterSecond: [...createPicker.selected].sort() }));
    """)
    assert out["afterFirst"] == ["light.guest_bulb", "light.hall_lamp", "switch.hall_switch"]
    assert out["afterSecond"] == [
        "light.guest_bulb", "light.hall_lamp", "switch.guest_fan", "switch.hall_switch",
    ]


async def test_loading_a_template_drops_entities_ha_no_longer_has(
    client, admin_session, mock_ha_client
):
    out = await _run(client, admin_session, _SEED + """
    allEntities = ENTITIES;
    entityTemplates = [
      { id: 't1', name: 'Stale', entity_ids: ['light.guest_bulb', 'light.deleted'], created_at: 0 },
    ];
    loadTemplateInto('create-picker', 't1');
    console.log(JSON.stringify({ selected: [...createPicker.selected] }));
    """)
    assert out["selected"] == ["light.guest_bulb"]
