// Settings window for xDrip+ Top Bar (GNOME 46, libadwaita)

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import Gdk from 'gi://Gdk';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

import {dataDir, syncKeyFromQrText, fmtGlucose, MGDL_PER_MMOL} from './common.js';
import {lookupKey, storeKey, clearKey, lookupNsSecret, storeNsSecret, clearNsSecret} from './secret.js';

function rgbaToHex(c) {
    const h = v => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, '0');
    return `#${h(c.red)}${h(c.green)}${h(c.blue)}`;
}

function colorButton(settings, key) {
    const btn = new Gtk.ColorDialogButton({
        dialog: new Gtk.ColorDialog({with_alpha: false, modal: true}),
        valign: Gtk.Align.CENTER,
    });
    const load = () => {
        const c = new Gdk.RGBA();
        if (c.parse(settings.get_string(key)))
            btn.set_rgba(c);
    };
    load();
    btn.connect('notify::rgba', () => {
        const hex = rgbaToHex(btn.get_rgba());
        if (hex !== settings.get_string(key))
            settings.set_string(key, hex);
    });
    settings.connect(`changed::${key}`, load);
    return btn;
}

function smallLabel(text) {
    return new Gtk.Label({label: text, valign: Gtk.Align.CENTER, css_classes: ['dim-label']});
}

function spinRow(settings, key, title, subtitle, min, max, step, digits) {
    const row = Adw.SpinRow.new_with_range(min, max, step);
    row.title = title;
    if (subtitle)
        row.subtitle = subtitle;
    row.digits = digits;
    settings.bind(key, row, 'value', Gio.SettingsBindFlags.DEFAULT);
    return row;
}

/** EntryRow that only saves when the apply (✓) button or Enter is pressed. */
function applyEntryRow(settings, key, title) {
    const row = new Adw.EntryRow({title, show_apply_button: true, text: settings.get_string(key)});
    row.connect('apply', () => settings.set_string(key, row.text.trim()));
    settings.connect(`changed::${key}`, () => {
        if (row.text.trim() !== settings.get_string(key))
            row.text = settings.get_string(key);
    });
    return row;
}

export default class XdripTopbarPrefs extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const s = this.getSettings();
        window._settings = s;
        window.set_default_size(640, 760);

        window.add(this._collectorPage(window, s));
        window.add(this._appearancePage(s));
        window.add(this._historyPage(s));
    }

    // ------------------------------------------------------------------ collector
    _collectorPage(window, s) {
        const page = new Adw.PreferencesPage({title: 'Collector', icon_name: 'network-receive-symbolic'});

        // --- where the data comes from
        const srcGroup = new Adw.PreferencesGroup({title: 'Data source'});
        const sources = ['xdrip-cloud', 'nightscout'];
        const src = new Adw.ComboRow({
            title: 'Read readings from',
            subtitle: 'xDrip+ Cloud Sync: follows your master phone directly. ' +
                'Nightscout: for setups that upload everything to a Nightscout site.',
            model: Gtk.StringList.new(['xDrip+ Cloud Sync', 'Nightscout']),
        });
        src.selected = Math.max(0, sources.indexOf(s.get_string('data-source')));
        src.connect('notify::selected', () => s.set_string('data-source', sources[src.selected] ?? 'xdrip-cloud'));
        srcGroup.add(src);
        page.add(srcGroup);

        // --- Nightscout
        const nsGroup = new Adw.PreferencesGroup({
            title: 'Nightscout',
            description: 'Your Nightscout address, plus an access token. To make one, open your Nightscout site › ' +
                'Admin Tools › add a subject with the role “readable” and copy its token. Your API secret ' +
                'works too, but a read-only token is safer. Leave the token empty if your site can be read ' +
                'without one. Readings are checked every minute; insulin and carbs every 5 minutes.',
        });
        const nsUrl = applyEntryRow(s, 'nightscout-url', 'Nightscout address (e.g. https://mysite.example.com)');
        nsGroup.add(nsUrl);
        const nsStatus = new Adw.ActionRow({title: 'Saved token / API secret', subtitle: 'Checking…'});
        const nsRemove = new Gtk.Button({label: 'Remove', valign: Gtk.Align.CENTER, css_classes: ['destructive-action']});
        nsStatus.add_suffix(nsRemove);
        const nsRefresh = () => lookupNsSecret(k => {
            nsStatus.subtitle = k ? 'Saved in the keyring' : 'None saved (fine if your site is readable without one)';
            nsRemove.sensitive = !!k;
        });
        nsRefresh();
        const nsEntry = new Adw.PasswordEntryRow({title: 'Access token or API secret', show_apply_button: true});
        nsEntry.connect('apply', () => {
            const v = nsEntry.text.trim();
            nsEntry.text = '';
            if (!v)
                return;
            storeNsSecret(v, ok => {
                if (ok)
                    s.set_int('key-stamp', s.get_int('key-stamp') + 1);
                window.add_toast(new Adw.Toast({title: ok ? 'Saved' : 'Could not save to the keyring'}));
                nsRefresh();
            });
        });
        nsRemove.connect('clicked', () => clearNsSecret(() => {
            s.set_int('key-stamp', s.get_int('key-stamp') + 1);
            nsRefresh();
        }));
        nsGroup.add(nsEntry);
        nsGroup.add(nsStatus);
        page.add(nsGroup);

        // --- sync key
        const keyGroup = new Adw.PreferencesGroup({
            title: 'xDrip+ Cloud Sync',
            description: 'Make sure “Use xDrip+ Cloud” is on in xDrip+ › Settings › xDrip+ Sync Settings on your ' +
                'master phone. The sync key is stored in your GNOME Keyring. It is never shown here, ' +
                'never saved in plain text and never put on the collector’s command line.',
        });
        const status = new Adw.ActionRow({title: 'Saved sync key', subtitle: 'Checking…'});
        const removeBtn = new Gtk.Button({label: 'Remove', valign: Gtk.Align.CENTER, css_classes: ['destructive-action']});
        status.add_suffix(removeBtn);
        const refresh = () => lookupKey(k => {
            status.subtitle = k ? 'A key is saved in the keyring' : 'No key saved — the collector cannot start';
            removeBtn.sensitive = !!k;
        });
        refresh();

        const saveKey = key => {
            storeKey(key, ok => {
                if (ok) {
                    s.set_int('key-stamp', s.get_int('key-stamp') + 1);     // tells the extension to restart the collector
                    const warn = key.length <= 16 ? ' (warning: xDrip+ keys are usually longer than 16 characters)' : '';
                    window.add_toast(new Adw.Toast({title: `Sync key saved${warn}`}));
                } else {
                    window.add_toast(new Adw.Toast({title: 'Could not save to the keyring'}));
                }
                refresh();
            });
        };
        // accepts the key itself, or the text of the xDrip+ settings QR code (xdpref:… / xdp2:…)
        const useText = (text, emptyMsg) => {
            let key = (text || '').trim();
            try {
                key = syncKeyFromQrText(key) ?? key;
            } catch (e) {
                window.add_toast(new Adw.Toast({title: `QR code: ${e.message}`, timeout: 6}));
                return;
            }
            if (!key) {
                window.add_toast(new Adw.Toast({title: emptyMsg}));
                return;
            }
            saveKey(key);
        };

        const entry = new Adw.PasswordEntryRow({
            title: 'Paste the sync key, or the scanned QR text (xdpref:…)',
            show_apply_button: true,
        });
        entry.connect('apply', () => {
            const text = entry.text;
            entry.text = '';
            if (text.trim())
                useText(text, 'Nothing to save');
        });

        // a screenshot / photo of the QR code, read with zbarimg (package zbar-tools)
        const qrRow = new Adw.ActionRow({
            title: 'Read the QR code from a screenshot',
            subtitle: 'xDrip+ › Settings › Show Settings QR codes › xDrip+ security key settings only. ' +
                'Needs zbar-tools (sudo apt install zbar-tools). Delete the screenshot afterwards.',
        });
        const qrBtn = new Gtk.Button({label: 'Choose image…', valign: Gtk.Align.CENTER});
        qrRow.add_suffix(qrBtn);
        qrBtn.connect('clicked', () => {
            const filter = new Gtk.FileFilter({name: 'Images'});
            filter.add_mime_type('image/*');
            const filters = new Gio.ListStore({item_type: Gtk.FileFilter});
            filters.append(filter);
            const dlg = new Gtk.FileDialog({title: 'Screenshot of the xDrip+ QR code', modal: true, filters});
            dlg.open(window, null, (d, res) => {
                let path;
                try {
                    path = d.open_finish(res)?.get_path();
                } catch (e) {
                    return;                                          // cancelled
                }
                if (!path)
                    return;
                let proc;
                try {
                    proc = Gio.Subprocess.new(['zbarimg', '--raw', '-q', path],
                        Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
                } catch (e) {
                    window.add_toast(new Adw.Toast({title: 'zbarimg not found — sudo apt install zbar-tools', timeout: 6}));
                    return;
                }
                proc.communicate_utf8_async(null, null, (p, r) => {
                    let out = '';
                    try {
                        [, out] = p.communicate_utf8_finish(r);
                    } catch (e) { /* ignore */ }
                    const line = (out || '').split('\n').find(l => l.startsWith('xdpref:') || l.startsWith('xdp2:'));
                    if (!line) {
                        window.add_toast(new Adw.Toast({title: 'No xDrip+ settings QR code found in that image', timeout: 6}));
                        return;
                    }
                    useText(line, 'No sync key found');
                });
            });
        });
        removeBtn.connect('clicked', () => clearKey(() => {
            s.set_int('key-stamp', s.get_int('key-stamp') + 1);
            window.add_toast(new Adw.Toast({title: 'Sync key removed'}));
            refresh();
        }));
        keyGroup.add(entry);
        keyGroup.add(qrRow);
        keyGroup.add(status);
        page.add(keyGroup);

        // --- active / passive
        const modeGroup = new Adw.PreferencesGroup({
            title: 'Visibility',
            description: 'Passive (default): only listens, sends nothing, does not appear on your phone. ' +
                'Active: sends the battery and roll-call requests a follower phone sends and appears in ' +
                'xDrip+ System status › Followers under the name below.',
        });
        const active = new Adw.SwitchRow({title: 'Active', subtitle: 'Announce this computer to the xDrip+ group'});
        s.bind('active', active, 'active', Gio.SettingsBindFlags.DEFAULT);
        modeGroup.add(active);
        const name = applyEntryRow(s, 'rollcall-name', 'Name shown in xDrip+ when active');
        s.bind('active', name, 'sensitive', Gio.SettingsBindFlags.GET);
        modeGroup.add(name);
        page.add(modeGroup);

        // show only the settings for the chosen source
        const syncSource = () => {
            const ns = s.get_string('data-source') === 'nightscout';
            nsGroup.visible = ns;
            keyGroup.visible = !ns;
            modeGroup.visible = !ns;
            src.selected = Math.max(0, sources.indexOf(s.get_string('data-source')));
        };
        s.connect('changed::data-source', syncSource);
        syncSource();

        // --- data folder
        const dirGroup = new Adw.PreferencesGroup({
            title: 'Data folder',
            description: 'latest.json (newest reading), history.json (rolling 48 hours) and collector.log are written here.',
        });
        const dirRow = new Adw.ActionRow({title: 'Folder', subtitle: dataDir(s), subtitle_selectable: true});
        const choose = new Gtk.Button({label: 'Choose…', valign: Gtk.Align.CENTER});
        const reset = new Gtk.Button({icon_name: 'edit-undo-symbolic', valign: Gtk.Align.CENTER,
            tooltip_text: 'Use the default folder', css_classes: ['flat']});
        dirRow.add_suffix(choose);
        dirRow.add_suffix(reset);
        choose.connect('clicked', () => {
            const dlg = new Gtk.FileDialog({title: 'Folder for xDrip+ data', modal: true});
            try {
                dlg.set_initial_folder(Gio.File.new_for_path(dataDir(s)));
            } catch (e) { /* folder may not exist yet */ }
            dlg.select_folder(window, null, (d, res) => {
                try {
                    const f = d.select_folder_finish(res);
                    if (f?.get_path())
                        s.set_string('data-dir', f.get_path());
                } catch (e) { /* cancelled */ }
            });
        });
        reset.connect('clicked', () => s.set_string('data-dir', ''));
        s.connect('changed::data-dir', () => {
            dirRow.subtitle = dataDir(s);
        });
        dirGroup.add(dirRow);
        page.add(dirGroup);

        // --- advanced
        const adv = new Adw.PreferencesGroup({title: 'Advanced'});
        adv.add(applyEntryRow(s, 'python', 'Python interpreter (needs the “cryptography” package)'));
        page.add(adv);
        return page;
    }

    // ------------------------------------------------------------------ appearance
    _appearancePage(s) {
        const page = new Adw.PreferencesPage({title: 'Top bar', icon_name: 'preferences-color-symbolic'});

        const posGroup = new Adw.PreferencesGroup({title: 'Position'});
        const positions = ['left', 'clock-left', 'clock-right', 'right'];
        const pos = new Adw.ComboRow({
            title: 'Place the reading',
            model: Gtk.StringList.new(['Left side', 'Left of the clock', 'Right of the clock',
                'Right side (before the system icons)']),
        });
        pos.selected = Math.max(0, positions.indexOf(s.get_string('panel-position')));
        pos.connect('notify::selected', () => s.set_string('panel-position', positions[pos.selected] ?? 'clock-left'));
        s.connect('changed::panel-position', () => {
            pos.selected = Math.max(0, positions.indexOf(s.get_string('panel-position')));
        });
        posGroup.add(pos);
        page.add(posGroup);

        const range = new Adw.PreferencesGroup({title: 'Units and target range'});
        const unitIds = ['mmol', 'mgdl'];
        const units = new Adw.ComboRow({
            title: 'Glucose units',
            model: Gtk.StringList.new(['mmol/L', 'mg/dL']),
        });
        units.selected = Math.max(0, unitIds.indexOf(s.get_string('units')));
        units.connect('notify::selected', () => s.set_string('units', unitIds[units.selected] ?? 'mmol'));
        range.add(units);
        // the limits are stored in mmol/L; these rows show and edit them in the chosen units
        const limitRow = (key, title, minMmol, maxMmol) => {
            const row = new Adw.SpinRow({title, adjustment: new Gtk.Adjustment()});
            let busy = false;
            const load = () => {
                busy = true;
                const mg = s.get_string('units') === 'mgdl';
                const k = mg ? MGDL_PER_MMOL : 1;
                row.digits = mg ? 0 : 1;
                row.adjustment.configure(s.get_double(key) * k, Math.round(minMmol * k), Math.round(maxMmol * k),
                    mg ? 1 : 0.1, mg ? 10 : 1, 0);
                row.subtitle = mg ? 'mg/dL' : 'mmol/L';
                busy = false;
            };
            row.connect('notify::value', () => {
                if (busy)
                    return;
                const k = s.get_string('units') === 'mgdl' ? MGDL_PER_MMOL : 1;
                const mmol = Math.round(row.value / k * 100) / 100;
                if (Math.abs(mmol - s.get_double(key)) > 0.004)
                    s.set_double(key, mmol);
            });
            s.connect(`changed::${key}`, load);
            s.connect('changed::units', load);
            load();
            return row;
        };
        range.add(limitRow('low-mmol', 'Low below', 2.0, 15.0));
        range.add(limitRow('high-mmol', 'High above', 5.0, 30.0));
        s.connect('changed::units', () => {
            units.selected = Math.max(0, unitIds.indexOf(s.get_string('units')));
        });
        page.add(range);

        const colors = new Adw.PreferencesGroup({
            title: 'Colours',
            description: 'The glucose value and the trend arrow each have their own colour per range. ' +
                'Turn on “Same colour for arrow” to combine them.',
        });
        const same = new Adw.SwitchRow({title: 'Same colour for arrow', subtitle: 'The arrow uses the value colours'});
        s.bind('arrow-same-as-value', same, 'active', Gio.SettingsBindFlags.DEFAULT);
        colors.add(same);

        for (const [id, title] of [['low', 'Low'], ['in', 'In range'], ['high', 'High']]) {
            const row = new Adw.ActionRow({title});
            row.add_suffix(smallLabel('Value'));
            row.add_suffix(colorButton(s, `value-color-${id}`));
            const arrowLabel = smallLabel('Arrow');
            const arrowBtn = colorButton(s, `arrow-color-${id}`);
            for (const w of [arrowLabel, arrowBtn])
                s.bind('arrow-same-as-value', w, 'sensitive', Gio.SettingsBindFlags.GET | Gio.SettingsBindFlags.INVERT_BOOLEAN);
            row.add_suffix(arrowLabel);
            row.add_suffix(arrowBtn);
            colors.add(row);
        }

        // live preview
        const preview = new Adw.ActionRow({title: 'Preview'});
        const sample = new Gtk.Label({use_markup: true, valign: Gtk.Align.CENTER});
        preview.add_suffix(sample);
        const update = () => {
            const part = (id, v, arrow) => {
                const vc = s.get_string(`value-color-${id}`);
                const ac = s.get_boolean('arrow-same-as-value') ? vc : s.get_string(`arrow-color-${id}`);
                return `<b><span foreground="${vc}">${v}</span> <span foreground="${ac}">${arrow}</span></b> <small>3m</small>`;
            };
            const st = s.get_string('stale-color');
            const u = s.get_string('units');
            sample.label = [part('low', fmtGlucose(3.8, u), '↘'), part('in', fmtGlucose(6.4, u), '→'),
                part('high', fmtGlucose(12.1, u), '↗'),
                `<b><span foreground="${st}">${fmtGlucose(7.2, u)} →</span></b> <small><s><span foreground="${st}">${s.get_int('stale-minutes')}m</span></s></small>`]
                .join('      ');
        };
        s.connect('changed', update);
        update();
        colors.add(preview);
        page.add(colors);

        const stale = new Adw.PreferencesGroup({
            title: 'Stale data',
            description: 'When no new reading has arrived for this long, the top bar is shown in the stale colour.',
        });
        stale.add(spinRow(s, 'stale-minutes', 'Stale after (minutes)', null, 5, 120, 1, 0));
        const staleColor = new Adw.ActionRow({title: 'Stale colour'});
        staleColor.add_suffix(colorButton(s, 'stale-color'));
        stale.add(staleColor);
        page.add(stale);
        return page;
    }

    // ------------------------------------------------------------------ history
    _historyPage(s) {
        const page = new Adw.PreferencesPage({title: 'History', icon_name: 'view-continuous-symbolic'});
        const group = new Adw.PreferencesGroup({
            title: '48-hour graph',
            description: 'Click the reading in the top bar to see it. In window mode a left click opens or closes ' +
                'the window and a right click opens the small menu. Both are resizable and remember their size. ' +
                'On the graph: mouse wheel zooms (remembered), Shift + wheel moves back and forward in time, ' +
                'middle click shows all 48 hours again.',
        });

        const modes = ['dropdown', 'window'];
        const combo = new Adw.ComboRow({
            title: 'Show history as',
            model: Gtk.StringList.new(['Dropdown under the top bar', 'Separate window']),
        });
        combo.selected = Math.max(0, modes.indexOf(s.get_string('history-mode')));
        combo.connect('notify::selected', () => s.set_string('history-mode', modes[combo.selected] ?? 'dropdown'));
        s.connect('changed::history-mode', () => {
            combo.selected = Math.max(0, modes.indexOf(s.get_string('history-mode')));
        });
        group.add(combo);

        const resetRow = new Adw.ActionRow({title: 'Reset size and zoom', subtitle: 'Back to the default dropdown and window size, showing all 48 hours'});
        const resetBtn = new Gtk.Button({label: 'Reset', valign: Gtk.Align.CENTER});
        resetBtn.connect('clicked', () => {
            for (const k of ['dropdown-width', 'dropdown-height', 'window-width', 'window-height', 'graph-hours'])
                s.reset(k);
        });
        resetRow.add_suffix(resetBtn);
        group.add(resetRow);
        page.add(group);

        // --- how the readings are drawn
        const look = new Adw.PreferencesGroup({
            title: 'Graph style',
            description: 'The smooth curve never goes above or below the actual readings, so it can’t show ' +
                'a peak or dip that wasn’t measured. The line breaks where readings are missing.',
        });
        const line = new Adw.SwitchRow({title: 'Draw a line through the readings'});
        s.bind('graph-show-line', line, 'active', Gio.SettingsBindFlags.DEFAULT);
        look.add(line);

        const lineOnly = [];
        const smooth = new Adw.SwitchRow({title: 'Smooth curve', subtitle: 'Off: straight lines between readings'});
        s.bind('graph-smooth', smooth, 'active', Gio.SettingsBindFlags.DEFAULT);
        lineOnly.push(smooth);
        lineOnly.push(spinRow(s, 'graph-line-width', 'Line width', null, 1.0, 6.0, 0.5, 1));

        const colorModes = ['range', 'single'];
        const colorMode = new Adw.ComboRow({
            title: 'Line colour',
            model: Gtk.StringList.new(['By range (low / in range / high colours)', 'One colour']),
        });
        colorMode.selected = Math.max(0, colorModes.indexOf(s.get_string('graph-line-color-mode')));
        colorMode.connect('notify::selected', () =>
            s.set_string('graph-line-color-mode', colorModes[colorMode.selected] ?? 'range'));
        lineOnly.push(colorMode);

        const single = new Adw.ActionRow({title: 'Single line colour'});
        single.add_suffix(colorButton(s, 'graph-line-color'));
        lineOnly.push(single);
        lineOnly.push(spinRow(s, 'graph-gap-minutes', 'Break the line after a gap of (minutes)', null, 6, 120, 1, 0));

        const syncSensitive = () => {
            const on = s.get_boolean('graph-show-line');
            for (const w of lineOnly)
                w.sensitive = on;
            single.sensitive = on && s.get_string('graph-line-color-mode') === 'single';
        };
        s.connect('changed::graph-show-line', syncSensitive);
        s.connect('changed::graph-line-color-mode', syncSensitive);
        syncSensitive();
        for (const w of lineOnly)
            look.add(w);

        const dots = new Adw.SwitchRow({
            title: 'Show a dot for every reading',
            subtitle: 'Dots are always shown when the line is off',
        });
        s.bind('graph-show-dots', dots, 'active', Gio.SettingsBindFlags.DEFAULT);
        look.add(dots);
        page.add(look);
        return page;
    }
}
