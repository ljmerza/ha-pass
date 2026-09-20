# HomePass

Shareable guest links for controlling Home Assistant devices.

## What it does

HomePass lets you create time-limited guest links that expose specific Home Assistant entities (lights, locks, switches, etc.) to visitors. No HA account required. Guests get a mobile-friendly PWA with real-time state updates.

## Accessing the admin UI

After installing, HomePass appears in the Home Assistant side panel. Click it to open the admin dashboard. No separate login needed, HA handles authentication automatically.

For direct port access (e.g., `http://<your-ha-ip>:5880/admin/dashboard`), set **Admin Username** and **Admin Password** in the configuration below.

## How guest links work

1. In the admin dashboard, create an **access token** with selected entities and an expiration time.
2. Share the generated link (`http://<your-ha-ip>:5880/g/{slug}`) with your guest.
3. The guest opens the link on their phone. No app install or HA account needed.
4. When the token expires, the guest sees the contact message and can no longer control devices.

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