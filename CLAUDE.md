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

## Each release
- Increase `"version"` in `metadata.json`.
- Run `python3 collector/xdrip_gnome_collector.py --selftest`.
- Run `glib-compile-schemas --strict --dry-run schemas`.

## Never commit
Sync keys, `latest.json`, `history.json`, `collector.log`, `.collector_token`, or any user data.
