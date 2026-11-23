# Rules for changing this project

## Users' settings and data must survive every update
- **Never rename or remove** a GSettings key in `schemas/…gschema.xml`. Don't change its type either, and don't narrow its `<range>` or `<choices>` so that an existing value becomes invalid. Only **add** new keys, with defaults that keep the old behaviour.
- If a setting truly has to change shape, add a new key, copy the old value across once in `enable()`, and leave the old key in the schema.
- **Never change** these:
  - the extension UUID (`xdrip-topbar@local`)
  - the schema id (`org.gnome.shell.extensions.xdrip-topbar`)
  - its path (`/org/gnome/shell/extensions/xdrip-topbar/`)
  - the libsecret schema and attributes in `secret.js`
  - the default data folder
- `history.json` and `latest.json` must stay readable by both older and newer versions. Only add fields, and keep `version: 1` unless a migration is written.
- `install.sh` must never delete the data folder, the settings or the keyring entry.

## Windows app (`windows/`, branch `windows`)
- `settings.json` keys follow the same rule as the GSettings keys: only add keys, never rename, retype or remove them.
- Never change the Credential Manager entry: service `xdrip-topbar`, user `sync-key`.
- Never change the default folders: `%APPDATA%\xdrip-topbar` and `%LOCALAPPDATA%\xdrip-topbar`.
- Increase `VERSION` in `windows/xdrip_tray.py` for each Windows release. The workflow publishes it as the GitHub Release `windows-v<VERSION>`. Don't upload Actions artifacts. The only exception is the temporary unsigned .exe for SignPath signing (1-day retention, deleted straight after signing).
- Keep `CODE_SIGNING_POLICY.md` accurate, because SignPath requires it. Only `.github/workflows/windows.yml` on GitHub-hosted runners may produce signed files.

## Each release
- Increase `"version"` in `metadata.json`.
- Run `python3 collector/xdrip_gnome_collector.py --selftest`.
- Run `glib-compile-schemas --strict --dry-run schemas`.

## Never commit
Sync keys, `latest.json`, `history.json`, `collector.log`, `.collector_token`, or any user data.
