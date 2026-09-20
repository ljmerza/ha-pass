# HomePass

Shareable guest links for controlling Home Assistant devices.

## What it does

HomePass lets you create time-limited guest links that expose specific Home Assistant entities (lights, locks, switches, cameras, etc.) to visitors. No HA account required. Guests get a mobile-friendly PWA with real-time state updates.

## Accessing the admin UI

After installing, HomePass appears in the Home Assistant side panel. Click it to open the admin dashboard. No separate login needed, HA handles authentication automatically.

For direct port access (e.g., `http://<your-ha-ip>:5880/admin/dashboard`), set **Admin Username** and **Admin Password** in the configuration below.

## How guest links work

1. In the admin dashboard, create an **access token** with selected entities and an expiration time.
2. Share the generated link (`http://<your-ha-ip>:5880/g/{slug}`) with your guest.
3. The guest opens the link on their phone. No app install or HA account needed.
4. When the token expires, the guest sees the contact message and can no longer control devices.

The slug in the link is the credential: anyone holding the link has the access. Share it the way you would share a password, and use **Revoke** or **Rotate Link** if it ends up somewhere it should not be.

## Configuration

Set these options in the add-on Configuration tab:

| Option | Description |
|--------|-------------|
| **Admin Username** | Username for direct port access. Not needed when using the HA side panel. |
| **Admin Password** | Password for direct port access (min 8 characters). Not needed when using the HA side panel. |
| **App Name** | Display name shown to guests (default: "Home Access") |
| **Contact Message** | Message shown when a guest link expires |
| **Background Color** | Hex color for page background (e.g., `#F2F0E9`) |
| **Primary Color** | Hex color for accents and buttons (e.g., `#D9523C`) |
| **Guest URL** | External base URL for guest links (e.g., `https://guest.myhouse.com`). Leave empty for local network. |

These are the only add-on options. Everything else is set per link, in the dashboard.

## Choosing entities

The picker lists every entity in a domain HomePass supports: lights, switches, groups, climate, locks, alarm panels, media players, covers, fans, buttons, counters, timers, the `input_*` helpers, time and date helpers, and the read-only sensors, cameras and schedules. Scripts, scenes and automations are deliberately absent, because running one would take a guest outside the entities you picked.

Three ways to narrow the list, and they combine:

- **Domain chips** — Lights, Switches, Locks, Cameras, and so on.
- **Search** — matches the entity name, including a display name you have overridden.
- **Label** — filter by a Home Assistant label. Once a label or a search has narrowed the list, an **Add all** button appears and takes every match, not just the rows on screen.

If the label row is missing, HomePass could not read the label registry from Home Assistant. Everything else still works.

**Templates** save the current selection under a name so a later link can start from it. Loading a template adds to whatever is already selected, so two templates can be stacked; entities Home Assistant no longer has are dropped quietly.

### Per-entity options

Click a selected entity to open its options:

- **Display name** — what the guest sees instead of the Home Assistant name, up to 64 characters. Leave blank to use the HA name.
- **Show brightness slider** (lights only) — off by default, so a light is on/off unless you turn this on.
- **Show colour controls** (lights only) — also off by default. Gives the guest a colour wheel, a warm–cool temperature slider, or both, depending on what the bulb supports.
- **Require the guest to be at the property** — see below.

These belong to the link, not the entity. The same light can be on/off for the cleaner and fully adjustable for a house guest.

## Expiry and scheduled start

Pick a preset duration (1 hour through 1 year), enter a custom one, or choose **Never expires**. You can extend an expiry later from the token's card.

**Valid From** is optional. Set a future date and time and the link can be sent immediately, but until then a guest who opens it sees a countdown and a greyed-out preview instead of working controls — no Home Assistant state reaches the page. The duration runs from the start, not from when you created the link: a 3-day link starting next Friday is three days of access beginning next Friday.

**Activate Now** opens a scheduled link straight away. Its expiry does not move, and a guest already waiting on the link is let in without reloading.

## PIN protection

You can put an optional 4–8 digit PIN on a link, either when creating it or later with the **Add PIN** button on its card. The guest is asked for the PIN before they see anything, and everything behind it — the page, live state, commands and camera views — stays closed until they enter it. One correct entry lasts 24 hours, or until the link expires if that is sooner.

Two things to know:

- **A forgotten PIN cannot be looked up.** PINs are stored hashed. The dashboard can tell you that a link has one; it can never tell you what it is. If a guest forgets it, set a new PIN and tell them the new one.
- **Changing or removing a PIN signs out everyone who had entered the old one.** They are asked again on their next action.

Guessing is rate-limited, so a PIN entered wrongly several times in a row starts being refused for a minute at a time. Wait and try again.

## Requiring the guest to be at the property

Any controllable entity can be marked **Require the guest to be at the property**. When the guest presses that one control, their browser is asked where it is, and the command only goes through if the position falls inside Home Assistant's `zone.home`. It is per entity — you can gate the gate release and leave the living-room lamp alone.

- **The guest link has to be served over HTTPS.** Set **Guest URL** to an `https://` address, behind a reverse proxy or Nabu Casa. Browsers refuse to report a location over plain HTTP, and the control refuses with them.
- **It is a deterrent, not proof.** The position comes from the guest's own browser, so someone determined can claim to be anywhere. It is the same kind of protection the IP allowlist gives: good against a guest casually using the link from their own house, not against someone trying to get around it.
- **It fails closed.** No location, a location more than two minutes old, or a `zone.home` that cannot be read all refuse the command.

`zone.home` is the Home zone under **Settings → Areas, labels & zones**. Its radius is what HomePass compares against, so widen it there if guests are being refused at the door.

## Cameras

Add a camera entity to a link and the guest gets a live view on their page. It is read-only: no camera service can be called through a guest link. The view stops when the guest closes or backgrounds the tab, and one link is limited to 8 live views at a time across all of the guest's devices.

## Managing a live link

Each token's card offers:

- **Extend** — push the expiry out. It reads **Renew** on a link that has already expired or been revoked.
- **Edit Entities** — change what is on the link, including the per-entity options.
- **Add PIN / PIN Protected** — set, change or remove the PIN.
- **Rotate Link** — generate a new link and kill the old one immediately. Entities, options, expiry, PIN and history are kept, so use this instead of rebuilding a token when a link has reached the wrong person. A guest who had entered the PIN is asked for it again.
- **Duplicate** — start a new link pre-filled with this one's entities and IP allowlist.
- **Revoke** — stop the link working, keeping its history.
- **Delete** — remove the token and its history.

**IP Allowlist** is set when the link is created and is a comma-separated list of CIDRs (`192.168.1.0/24`). It only means anything if HomePass sits behind a reverse proxy that overwrites the client address; on a bare LAN setup it is easy to bypass. To change it later, duplicate the link and revoke the old one.

**Recent activity** in the dashboard shows link opens and commands. Access logs are kept for 90 days.

## Notifications

HomePass fires a `homepass_activity` event on Home Assistant's event bus when a guest link is opened and when a guest command succeeds, and writes a matching Logbook entry. Trigger an automation on that event to get a phone notification when the cleaner unlocks the door. The event carries the link's label, the entity and the service — never the slug or the guest's IP address. The README has a worked automation.

## Troubleshooting

**A guest link looks broken in the HA sidebar.** Guest links are meant to be opened outside Home Assistant, on the direct port or your **Guest URL**. The sidebar panel is the admin dashboard.

**The guest sees "This link is not active yet".** The link has a **Valid From** time that has not arrived. Use **Activate Now** if that was a mistake.

**A location-gated control says the link needs to be secure.** The guest is on `http://`. Set **Guest URL** to an HTTPS address.

**The guest gets "too many requests".** The per-link rate limits kicked in. They clear on their own within a minute.

**Nothing works and the add-on log says Home Assistant is unreachable.** HomePass proxies everything to HA and will not serve if it cannot reach it.
