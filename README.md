# xDrip Top Bar (GNOME 46)

A GNOME Shell extension that shows your latest [xDrip](https://github.com/NightscoutFoundation/xDrip) glucose reading (mmol/L or mg/dL) in the top bar, with a zoomable 48-hour graph. It brings its own background collector that gets the readings from either of two places:
- **xDrip Cloud Sync**, as a passive follower of your master phone. No Nightscout server is needed.
- **A Nightscout site**, for setups that upload everything to Nightscout.

*xDrip used to be called xDrip+. Depending on your version of the app, the menu names below may still show the +, e.g. "xDrip+ Sync Settings".*

![Graph preview (sample data)](docs/graph-preview.svg)

*The preview above is drawn from made-up readings.*

**Windows users:** see [Windows](#windows) below.

> **Not a medical device.** This is a convenience display. Don't base treatment decisions on it, and rely on xDrip itself for alarms. Readings can be late, missing or wrong.

Shows your latest xDrip reading in the top bar, as `6.4 → 3m` or `115 → 3m` (mmol/L or mg/dL, trend arrow, minutes since the reading). Click it to see a 48-hour graph styled like the xDrip main screen: dots coloured by range, the target band shaded, insulin as blue ▼ and carbs as orange ●, finger-sticks as red dots, and all times on a 12-hour clock.

## Install

```sh
./install.sh            # checks requirements, copies, compiles the schema, runs the collector self-test
# log out and back in once (Wayland)
gnome-extensions enable xdrip-topbar@local
gnome-extensions prefs  xdrip-topbar@local
```

In Settings, enter your sync key and choose the data folder.

You need: `python3-cryptography`, `gjs` and `gir1.2-secret-1`. The installer tells you if any of them are missing. To read the QR code from a screenshot you also need `zbar-tools`, which is optional.

## Windows

There's also a **Windows version**. It shows the reading in the taskbar tray and an optional floating widget, with the same 48-hour graph.
- **Download:** `xDripTray.exe` from [Releases](https://github.com/Electrik-rich546456/xdrip-gnome-topbar/releases)
- **Instructions:** the [Windows guide](https://github.com/Electrik-rich546456/xdrip-gnome-topbar/blob/windows/windows/README.md)
- **Source:** the [`windows` branch](https://github.com/Electrik-rich546456/xdrip-gnome-topbar/tree/windows)

Windows releases are built only by GitHub Actions from this repository, and each one comes with a GitHub build attestation. See the [code signing policy](https://github.com/Electrik-rich546456/xdrip-gnome-topbar/blob/windows/CODE_SIGNING_POLICY.md).

The Windows version hasn't been tested on a real Windows PC yet, so feedback and bug reports are very welcome. Please open an [issue](https://github.com/Electrik-rich546456/xdrip-gnome-topbar/issues).

**Linux requirements:** GNOME Shell 46, which is the default on Ubuntu 24.04.

## Using Nightscout instead

Use this if your phone isn't an xDrip master, or if everything is uploaded to a Nightscout site anyway.
1. **Make a read-only token** (recommended): open your Nightscout site › **Admin Tools** › **Add new Subject**. Give it the role **readable**, save it, and copy the token shown next to it. It looks like `topbar-1a2b3c4d5e6f7a8b`. Your API secret works too, but a read-only token can't change anything and is easy to revoke.
2. **Enter it in the extension's settings:** under **Collector › Data source**, choose **Nightscout**. Enter your site address, e.g. `https://mysite.example.com`, and press ✓. Then paste the token into **Access token or API secret** and press ✓. If your site can be read without a token, leave that box empty.

The collector checks for new readings every minute, and for insulin, carbs and finger-sticks every 5 minutes. The token is kept in GNOME Keyring like the sync key. The top bar, graph and data files work exactly the same as with xDrip Cloud Sync.

## Getting your sync key (xDrip Cloud Sync)

The collector joins your xDrip sync group using your **Handset Group Security Sync Key**. Do everything below on your **master** phone, the one connected to the sensor.

**First, check sync is on:** in xDrip, go to **Settings › xDrip Sync Settings**. Make sure **Be master for followers** and **Use xDrip Cloud** are both ticked. If you can't find xDrip Sync Settings, look under **Less Common Settings**.

Then use any one of these three ways to get the key into the extension. In each case, open the extension's settings first with `gnome-extensions prefs xdrip-topbar@local`, or from the top-bar menu › Settings.

### A. Screenshot of the QR code (easiest)
1. In xDrip, go to **Settings › Show Settings QR codes**, near the bottom, and choose **xDrip security key settings only**.
2. Take a screenshot and copy it to your PC.
3. In the extension's settings, under **Collector › xDrip Cloud Sync**, click **Read the QR code from a screenshot › Choose image…**. This needs `sudo apt install zbar-tools`.
4. **Delete the screenshot afterwards.** It contains your key.

### B. Paste the scanned QR text
1. Show the same QR code as in A.
2. Scan it with any QR scanner app, such as your phone camera or Google Lens on another phone.
3. Copy the text. It starts with `xdpref:` or `xdp2:`.
4. Paste it into **Paste the sync key, or the scanned QR text** and press ✓. The extension takes the key out of the text by itself.

### C. Type the key
1. In xDrip, go to **Settings › xDrip Sync Settings › Handset Group Security Sync Key**, and copy or write down the value.
2. Paste or type it into **Paste the sync key, or the scanned QR text** and press ✓.

The key is saved in GNOME Keyring and is never shown again. Anyone with your key can read your glucose data, so treat it like a password. Within a few minutes the top bar shows your reading. If it doesn't, the menu's status line and `collector.log` in your data folder will say why.

## How it works

- **Collector.** When the extension is turned on, it starts `collector/xdrip_gnome_collector.py`, and stops it when the extension is turned off.
  - It keeps running while the screen is locked. Your reading is hidden on the lock screen itself.
  - If the collector stops unexpectedly, it is restarted 30 seconds later.
  - It also exits on its own if GNOME Shell goes away.
- **Passive by default.** The collector only listens. Switch on *Active* if you want it to appear in xDrip's followers list. It uses the name you set, `gnome_info_collector` by default.
- **Sync key.** It is stored in GNOME Keyring. The extension passes it to the collector through stdin, so it never appears on a command line or in a file.
- **Data folder** (chosen in Settings, default `~/.local/share/xdrip-topbar`):
  - `latest.json`: newest reading plus collector status
  - `history.json`: rolling 48 hours of readings, treatments and finger-sticks
  - `collector.log`: output of the current run
  - `.collector_token`, `.collector.lock`: internal files the collector uses
- **Treatments.** Insulin and carbs only arrive while the collector is running, at the moment you enter or edit them on the phone. Anything entered while it was off won't be in the graph.

## Updating keeps your settings

Running `./install.sh` again only replaces the program files.
- **Your settings** (colours, range, position, zoom, sizes, data folder…) are stored in GNOME's settings database (dconf), not in the extension folder, so updates don't touch them.
- **Your sync key** stays in GNOME Keyring.
- **Your collected data** stays in your data folder.

As a safety net, the installer saves a copy of your settings to `~/.local/share/xdrip-topbar-settings-backup.ini` before every update. To restore it:

```sh
dconf load /org/gnome/shell/extensions/xdrip-topbar/ < ~/.local/share/xdrip-topbar-settings-backup.ini
```

## Units

**Settings › Top bar › Units and target range** switches between **mmol/L** (the default) and **mg/dL**. The low and high limits are shown in whichever unit you pick, and the top bar, graph labels and preview follow it.

## Graph controls

| Action | What it does |
|---|---|
| Mouse wheel | Zooms between 1 and 48 hours. The zoom level is remembered. |
| Shift + wheel, or scrolling sideways | Moves back and forward through the 48 hours. |
| Middle click | Shows all 48 hours again. |
| ◢ corner (dropdown) | Drag to resize. The size is remembered. |

## Privacy

- Your sync key stays in GNOME Keyring on your machine.
- Glucose and treatment data are only written to the data folder you choose.
- With Nightscout, the collector only reads from your site and never writes to it. A token with the **readable** role is enough.
- In passive mode, nothing is ever sent to the xDrip group.
- Don't commit your data folder or sync key anywhere public.

## Credits and licence

The Cloud Sync protocol details (server, framing, encryption) come from reading the open-source xDrip app (GPL-3.0). This project is also GPL-3.0, so see `LICENSE`. It isn't affiliated with the xDrip or Nightscout projects.

## Testing without GNOME

```sh
python3 collector/xdrip_gnome_collector.py --selftest
# a manual run against the real server (key from the environment):
XDRIP_SYNC_KEY='…' python3 collector/xdrip_gnome_collector.py --data-dir /tmp/xd --no-parent-watch
```

To see extension errors: `journalctl -f -o cat /usr/bin/gnome-shell`. For the settings and history windows: `journalctl -f -o cat /usr/bin/gjs`.

This is a convenience display, not a medical device. Rely on xDrip for alarms.
