# xDrip+ Tray for Windows

Shows your latest [xDrip+](https://github.com/NightscoutFoundation/xDrip) glucose reading on Windows in two places:
- **Tray icon:** the reading appears as a number in your range colour. Hover over it to see the trend arrow and how many minutes ago the reading was.
- **Floating widget:** a small always-on-top box showing `6.4 → 3m` that you can drag anywhere.

Click either one to open a **zoomable 48-hour graph** showing your readings, insulin and carbs.

The app follows **xDrip+ Cloud Sync** by itself, like a follower phone. You don't need Nightscout, Python or anything else installed.

> **Not a medical device.** This is a convenience display. Don't base treatment decisions on it, and rely on xDrip+ itself for alarms. Readings can be late, missing or wrong.

## 1. Download

1. Go to the [Releases page](https://github.com/Electrik-rich546456/xdrip-gnome-topbar/releases) and open the newest **xDrip+ Tray for Windows** release.
2. Download **`xDripTray.exe`**.
3. Put it somewhere permanent, for example a new folder `C:\Users\<you>\Apps\xDripTray\`. It's a single file, so there's no installer.

**"Windows protected your PC" warning:** the .exe isn't code-signed, so Windows SmartScreen will warn you the first time you run it. Click **More info › Run anyway**. You can check the download against `xDripTray.exe.sha256` from the same release:
```powershell
Get-FileHash .\xDripTray.exe -Algorithm SHA256
```

## 2. Get your sync key

Do all of this on your **master** phone, the one connected to the sensor.

**First, check sync is on:** in xDrip+, go to **Settings › xDrip+ Sync Settings**. Make sure **Be master for followers** and **Use xDrip Cloud** are both ticked. If you can't find xDrip+ Sync Settings, look under **Less Common Settings**.

Run `xDripTray.exe`. The Settings window opens by itself the first time. On the **Collector** tab, use any one of these three ways:

### A. QR code screenshot (easiest)
1. In xDrip+, go to **Settings › Show Settings QR codes**, near the bottom, and choose **xDrip+ security key settings only**.
2. Take a screenshot and copy it to your PC.
3. Click **QR screenshot…** and choose the image.
4. **Delete the screenshot afterwards.** It contains your key.

### B. Scanned QR text
1. Show the same QR code as in A.
2. Scan it with any QR scanner app, such as your phone camera or Google Lens on another phone.
3. Copy the text. It starts with `xdpref:` or `xdp2:`.
4. Paste it into the key box and click **Save key**. The app takes the key out of the text by itself.

### C. Type the key
1. In xDrip+, go to **Settings › xDrip+ Sync Settings › Handset Group Security Sync Key**.
2. Copy it into the key box and click **Save key**.

The key is stored in **Windows Credential Manager**, under the name `xdrip-topbar`. It's never shown again and never saved in a plain file. Anyone with your key can read your glucose data, so treat it like a password.

Within a few minutes, the tray icon and widget show your reading.

## 3. Using it

| Where | What to do |
|---|---|
| Tray icon | Hover for the reading, arrow and minutes ago. **Left click** for the graph. **Right click** for the menu: graph, widget on/off, settings, data folder, quit. |
| Widget | **Drag** to move it, and its position is remembered. **Click** for the graph. **Right click** for the menu. |
| Graph | **Mouse wheel** zooms between 1 and 48 hours, and the zoom is remembered. **Shift + wheel** moves back and forward in time. **Middle click** shows all 48 hours again. The window remembers its size and position. |

To keep the tray icon visible all the time, go to **Windows Settings › Personalization › Taskbar › Other system tray icons** and turn on **xDripTray**. On Windows 10 you can drag the icon out of the `^` overflow area instead.

## 4. Settings

The Settings window is available from the tray or widget menu.

**Collector tab**
- **Sync key:** paste the key, load a QR screenshot, or remove the saved key.
- **Active:** off by default (passive), so the app only listens and sends nothing. When on, the app shows up in xDrip+'s **System status › Followers** list under the name you set, `gnome_info_collector` by default.
- **Data folder:** where your readings are kept.
- **Start with Windows:** starts the app when you log in.

**Display tab**
- **Tray icon and widget:** turn each one on or off. At least one stays on.
- **Widget appearance:** text size and opacity.
- **Target range:** low and high limits. The defaults are 5.0 and 10.0 mmol/L.
- **Colours:**
  - The value and the arrow each have their own colour for low, in range and high. Tick **Same colour for arrow** to combine them.
  - The stale colour is used once no reading has arrived for longer than the time you set.

**Graph tab**
- Line on or off, smooth or straight, line width, and line colour (by range or one colour).
- A dot for every reading.
- Where the line breaks at a gap.
- Reset zoom.

## Where things are stored

| What | Where |
|---|---|
| Settings | `%APPDATA%\xdrip-topbar\settings.json` |
| Readings (rolling 48 h), latest reading, log | `%LOCALAPPDATA%\xdrip-topbar\`, or the folder you chose. Contains `history.json`, `latest.json` and `collector.log` |
| Sync key | Windows Credential Manager › Windows Credentials › `xdrip-topbar` |

**Updating:** quit the app from the tray menu, replace `xDripTray.exe` with the new one, and start it again. Your settings, key and readings are kept.

**Uninstalling:**
1. Quit the app and untick **Start with Windows** first.
2. Delete the .exe and the two folders listed in "Where things are stored".
3. Remove the `xdrip-topbar` entry from Credential Manager.

## Troubleshooting

| Problem | What to check |
|---|---|
| Shows `--` and never a reading | **Use xDrip Cloud** must be on, on the master phone. Re-save the key, and use option A or B if unsure. Then look at `collector.log` in the data folder, which is reachable from the tray menu › Open data folder. |
| "could not decode … sync key mismatch?" in the log | Wrong key. Copy it again from the master phone. |
| "Another copy is already collecting" | The app is already running. Check the tray, including the `^` overflow area. |
| Readings stop after the PC sleeps | The app reconnects by itself within a few minutes of waking. Meanwhile the reading turns your stale colour. |
| Insulin or carbs missing from the graph | Treatments only arrive while the app is running, at the moment they're entered or edited on the phone. |

## Privacy

- **Passive mode (the default):** the app sends nothing to your xDrip+ group. It only makes the connection and sends the acknowledgements the Cloud Sync protocol needs.
- **Your data:** readings stay in your data folder. Nothing is uploaded anywhere else.
- **Your key:** it's only in Windows Credential Manager.

## Building it yourself

The `.github/workflows/windows.yml` workflow builds the .exe with PyInstaller on GitHub's Windows machines, runs the self-tests, and publishes it as a Release. To run the app from source instead:
```powershell
pip install pystray pillow keyring cryptography zxing-cpp
python windows\xdrip_tray.py
```
