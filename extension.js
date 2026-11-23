// xDrip+ Top Bar — GNOME Shell 46
//
// Top bar:  "6.4 → 3m"  (mmol/L, trend arrow, minutes since the reading), colours from settings.
// Click:    48 h history graph, as a resizable dropdown or (setting) a separate window.
//           In window mode: left click toggles the window, right click opens the small menu.
// Collector: starts collector/xdrip_gnome_collector.py while the extension is enabled
//           (also kept running while the screen is locked) and restarts it if it stops.

import GObject from 'gi://GObject';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {
    arrowFor, mmolOf, rangeOf, dataDir, graphOptions, readJsonAsync, drawGraph, fmtMinutesAgo, fmtTime12,
    zoomView, panView, fmtGlucose, panViewPixels, hitTest,
} from './common.js';
import {lookupKey, lookupNsSecret} from './secret.js';

const RESTART_DELAY_S = 30;
const MIN_W = 320, MIN_H = 160, MAX_W = 1800, MAX_H = 1000;

// ============================================================================ collector process
class Collector {
    constructor(ext, settings, onState) {
        this._ext = ext;
        this._settings = settings;
        this._onState = onState;       // (text) -> void, for the menu status line
        this._proc = null;
        this._starting = false;
        this._wanted = false;
        this._restartAfterExit = false;
        this._retryId = 0;
        this.problem = null;
    }

    start() {
        this._wanted = true;
        if (this._proc || this._starting)
            return;
        this._starting = true;
        const nightscout = this._settings.get_string('data-source') === 'nightscout';
        if (nightscout && !this._settings.get_string('nightscout-url').trim()) {
            this._starting = false;
            this._setProblem('No Nightscout address set — open Settings');
            return;
        }
        (nightscout ? lookupNsSecret : lookupKey)(key => {
            this._starting = false;
            if (!this._wanted || this._proc)
                return;
            if (!key && !nightscout) {
                this._setProblem('No sync key saved — open Settings');
                return;
            }
            this._spawn(key ?? '');          // Nightscout sites that are readable without a token: empty
        });
    }

    _spawn(key) {
        const s = this._settings;
        const dir = dataDir(s);
        try {
            GLib.mkdir_with_parents(dir, 0o700);
            const script = GLib.build_filenamev([this._ext.path, 'collector', 'xdrip_gnome_collector.py']);
            const argv = [s.get_string('python') || 'python3', '-u', script, '--data-dir', dir, '--key-stdin'];
            if (s.get_string('data-source') === 'nightscout')
                argv.push('--source', 'nightscout', '--ns-url', s.get_string('nightscout-url').trim());
            else if (s.get_boolean('active'))
                argv.push('--active', '--name', s.get_string('rollcall-name') || 'gnome_info_collector');

            const launcher = new Gio.SubprocessLauncher({
                flags: Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDERR_MERGE,
            });
            launcher.set_stdout_file_path(GLib.build_filenamev([dir, 'collector.log']));
            const proc = launcher.spawnv(argv);
            this._proc = proc;
            this._setProblem(null);
            // writes the key to stdin, closes it, and calls back when the collector exits
            proc.communicate_utf8_async(`${key}\n`, null, (p, res) => {
                try {
                    p.communicate_utf8_finish(res);
                } catch (e) { /* ignore */ }
                if (this._proc !== p)
                    return;
                this._proc = null;
                if (this._restartAfterExit) {
                    this._restartAfterExit = false;
                    this.start();
                } else if (this._wanted) {
                    this._setProblem(`Collector stopped (exit ${p.get_exit_status()}) — see collector.log; retrying`);
                    this._scheduleRetry();
                }
            });
        } catch (e) {
            this._setProblem(`Could not start the collector: ${e.message}`);
            this._scheduleRetry();
        }
    }

    _scheduleRetry() {
        if (this._retryId)
            return;
        this._retryId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, RESTART_DELAY_S, () => {
            this._retryId = 0;
            if (this._wanted)
                this.start();
            return GLib.SOURCE_REMOVE;
        });
    }

    restart() {
        if (this._proc) {
            this._restartAfterExit = true;
            this._proc.send_signal(15);
        } else {
            this.start();
        }
    }

    stop() {
        this._wanted = false;
        this._restartAfterExit = false;
        if (this._retryId) {
            GLib.source_remove(this._retryId);
            this._retryId = 0;
        }
        if (this._proc) {
            const p = this._proc;
            this._proc = null;
            p.send_signal(15);
        }
    }

    get running() {
        return this._proc !== null;
    }

    _setProblem(text) {
        this.problem = text;
        this._onState();
    }
}

// ============================================================================ top bar button
const Indicator = GObject.registerClass(
class XdripIndicator extends PanelMenu.Button {
    _init(ext) {
        super._init(0.5, 'xDrip+ Top Bar', false);
        this._ext = ext;
        this._settings = ext.getSettings();
        this._latest = null;
        this._history = null;
        this._winProc = null;

        const box = new St.BoxLayout({y_align: Clutter.ActorAlign.CENTER});
        this._value = new St.Label({text: '--', y_align: Clutter.ActorAlign.CENTER});
        this._arrow = new St.Label({text: '', y_align: Clutter.ActorAlign.CENTER});
        this._mins = new St.Label({text: '', y_align: Clutter.ActorAlign.CENTER});
        box.add_child(this._value);
        box.add_child(this._arrow);
        box.add_child(this._mins);
        this.add_child(box);

        this._buildMenu();

        this._collector = new Collector(ext, this._settings, () => this._updateStatus());

        // settings
        this._settingIds = [];
        const on = (keys, fn) => {
            for (const k of keys)
                this._settingIds.push(this._settings.connect(`changed::${k}`, fn));
        };
        on(['data-dir'], () => {
            this._watchDir();
            this._loadLatest();
            this._collector.restart();
        });
        on(['active', 'rollcall-name', 'key-stamp', 'python', 'data-source', 'nightscout-url'],
            () => this._collector.restart());
        on(['low-mmol', 'high-mmol', 'value-color-low', 'value-color-in', 'value-color-high',
            'arrow-color-low', 'arrow-color-in', 'arrow-color-high', 'arrow-same-as-value',
            'stale-minutes', 'stale-color', 'graph-show-line', 'graph-smooth', 'graph-line-width',
            'graph-line-color-mode', 'graph-line-color', 'graph-show-dots', 'graph-gap-minutes', 'units',
            'graph-event-info'], () => {
            this._showEvent();
            this._updateLabels();
            this._area.queue_repaint();
        });
        on(['graph-hours'], () => {
            const h = this._settings.get_double('graph-hours');
            if (Math.abs(h - this._view.spanHours) > 0.01) {
                this._view = {spanHours: h, offsetMs: 0};
                this._area.queue_repaint();
            }
        });
        on(['dropdown-width', 'dropdown-height'], () => {
            if (!this._drag)
                this._area.set_size(this._settings.get_int('dropdown-width'), this._settings.get_int('dropdown-height'));
        });
        on(['history-mode'], () => {
            this.menu.close();
            this._closeWindow();
        });

        // data folder watching + a tick for "minutes ago"
        this._watchDir();
        // re-read latest.json every 20 s as well, in case a file-change event is missed
        this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 20, () => {
            this._loadLatest();
            return GLib.SOURCE_CONTINUE;
        });

        this._collector.start();
        this._loadLatest();
    }

    // ------------------------------------------------------------- menu (dropdown graph)
    _buildMenu() {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const col = new St.BoxLayout({vertical: true, x_expand: true});

        this._view = {spanHours: this._settings.get_double('graph-hours'), offsetMs: 0};
        this._area = new St.DrawingArea({
            width: this._settings.get_int('dropdown-width'),
            height: this._settings.get_int('dropdown-height'),
            reactive: true,
        });
        this._area.connect('repaint', area => {
            const cr = area.get_context();
            const [w, h] = area.get_surface_size();
            try {
                const res = drawGraph(cr, w, h, this._history,
                    {...graphOptions(this._settings), selected: this._selected}, Date.now(), this._view);
                this._targets = res.targets;
            } catch (e) {
                logError(e, 'xdrip-topbar: graph');
            }
            cr.$dispose();
        });
        // mouse wheel: zoom around the pointer (remembered); Shift+wheel or sideways scroll: move in time;
        // middle click: back to the whole 48 hours
        this._area.connect('scroll-event', (a, ev) => this._onScroll(ev));
        // left button: drag to move through time, or click an item to see its exact time
        this._area.connect('button-press-event', (a, ev) => {
            if (ev.get_button() === 2) {
                this._setView({spanHours: 48, offsetMs: 0});
                return Clutter.EVENT_STOP;
            }
            if (ev.get_button() !== 1)
                return Clutter.EVENT_PROPAGATE;
            const [x] = ev.get_coords();
            this._pan = {x, view: this._view, moved: false};
            this._panGrab = global.stage.grab(this._area);
            return Clutter.EVENT_STOP;
        });
        this._area.connect('motion-event', (a, ev) => {
            if (!this._pan)
                return Clutter.EVENT_PROPAGATE;
            const [x] = ev.get_coords();
            const dx = x - this._pan.x;
            if (Math.abs(dx) > 4)
                this._pan.moved = true;
            if (this._pan.moved)
                this._setView(panViewPixels(this._pan.view, this._area.width, this._area.height, dx));
            return Clutter.EVENT_STOP;
        });
        this._area.connect('button-release-event', (a, ev) => {
            if (!this._pan)
                return Clutter.EVENT_PROPAGATE;
            const moved = this._pan.moved;
            this._pan = null;
            this._panGrab?.dismiss();
            this._panGrab = null;
            if (!moved) {
                const [sx, sy] = ev.get_coords();
                const [ax, ay] = this._area.get_transformed_position();
                this._select(hitTest(this._targets, sx - ax, sy - ay));
            }
            return Clutter.EVENT_STOP;
        });
        col.add_child(this._area);

        const row = new St.BoxLayout({x_expand: true});
        this._eventLabel = new St.Label({
            text: '', y_align: Clutter.ActorAlign.CENTER, visible: false,
            style: 'font-size: 0.85em; font-weight: bold; padding-top: 4px; padding-right: 12px;',
        });
        row.add_child(this._eventLabel);
        this._statusLabel = new St.Label({
            text: '', x_expand: true, y_align: Clutter.ActorAlign.CENTER,
            style: 'font-size: 0.85em; color: #aaaaaa; padding-top: 4px;',
        });
        row.add_child(this._statusLabel);

        // resize grip (bottom-right corner): drag to resize, size is saved
        this._grip = new St.Label({
            text: '◢', reactive: true, track_hover: true,
            style: 'font-size: 1.1em; color: #888888; padding: 2px 0 0 8px;',
        });
        this._grip.connect('button-press-event', (a, ev) => this._gripPress(ev));
        this._grip.connect('motion-event', (a, ev) => this._gripMotion(ev));
        this._grip.connect('button-release-event', () => this._gripRelease());
        row.add_child(this._grip);
        col.add_child(row);
        item.add_child(col);
        this.menu.addMenuItem(item);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Open history in a window', () => this._openWindow());
        this.menu.addAction('Open data folder', () => {
            try {
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(dataDir(this._settings)).get_uri(), null);
            } catch (e) {
                logError(e, 'xdrip-topbar');
            }
        });
        this.menu.addAction('Settings', () => this._ext.openPreferences());

        this._menuOpenId = this.menu.connect('open-state-changed', (m, open) => {
            if (open) {
                this._view = {spanHours: this._settings.get_double('graph-hours'), offsetMs: 0};
                this._select(null);
                this._loadLatest();
                this._loadHistory();
                this._updateStatus();
            }
        });
    }

    _onScroll(ev) {
        let dx = 0, dy = 0;
        switch (ev.get_scroll_direction()) {
        case Clutter.ScrollDirection.UP: dy = -1; break;
        case Clutter.ScrollDirection.DOWN: dy = 1; break;
        case Clutter.ScrollDirection.LEFT: dx = -1; break;
        case Clutter.ScrollDirection.RIGHT: dx = 1; break;
        case Clutter.ScrollDirection.SMOOTH:
            [dx, dy] = ev.get_scroll_delta();
            break;
        }
        if (ev.get_state() & Clutter.ModifierType.SHIFT_MASK) {
            dx += dy;
            dy = 0;
        }
        let view = this._view;
        if (dy) {
            const [sx] = ev.get_coords();
            const [ax] = this._area.get_transformed_position();
            view = zoomView(view, this._area.width, this._area.height, sx - ax, Math.pow(1.2, dy));
        }
        if (dx)
            view = panView(view, -dx * 0.15);         // scroll left = further back in time
        this._setView(view);
        return Clutter.EVENT_STOP;
    }

    /** Remember the clicked graph item (or none) and show its details. */
    _select(target) {
        this._selected = target?.key ?? null;
        this._selectedLabel = target?.label ?? '';
        this._showEvent();
        this._area.queue_repaint();
    }

    _showEvent() {
        const inStatus = this._settings.get_string('graph-event-info') === 'status';
        this._eventLabel.text = this._selectedLabel ?? '';
        this._eventLabel.visible = inStatus && !!this._selectedLabel;
    }

    _setView(view) {
        this._view = view;
        this._area.queue_repaint();
        // remember the zoom (not the position in time), written once scrolling stops
        if (this._zoomSaveId)
            GLib.source_remove(this._zoomSaveId);
        this._zoomSaveId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            this._zoomSaveId = 0;
            if (Math.abs(this._settings.get_double('graph-hours') - this._view.spanHours) > 0.01)
                this._settings.set_double('graph-hours', this._view.spanHours);
            return GLib.SOURCE_REMOVE;
        });
    }

    _gripPress(ev) {
        if (ev.get_button() !== 1)
            return Clutter.EVENT_PROPAGATE;
        const [x, y] = ev.get_coords();
        this._drag = {x, y, w: this._area.width, h: this._area.height};
        this._grab = global.stage.grab(this._grip);
        return Clutter.EVENT_STOP;
    }

    _gripMotion(ev) {
        if (!this._drag)
            return Clutter.EVENT_PROPAGATE;
        const [x, y] = ev.get_coords();
        // the menu is centred under the button, so a width change grows both sides: double dx
        const w = Math.round(Math.max(MIN_W, Math.min(MAX_W, this._drag.w + 2 * (x - this._drag.x))));
        const h = Math.round(Math.max(MIN_H, Math.min(MAX_H, this._drag.h + (y - this._drag.y))));
        this._area.set_size(w, h);
        return Clutter.EVENT_STOP;
    }

    _gripRelease() {
        if (!this._drag)
            return Clutter.EVENT_PROPAGATE;
        this._drag = null;
        if (this._grab) {
            this._grab.dismiss();
            this._grab = null;
        }
        this._panGrab?.dismiss();
        this._panGrab = null;
        this._settings.set_int('dropdown-width', this._area.width);
        this._settings.set_int('dropdown-height', this._area.height);
        return Clutter.EVENT_STOP;
    }

    // In window mode a left click toggles the separate window; right click still opens the menu.
    vfunc_event(event) {
        if (this._settings.get_string('history-mode') === 'window' &&
            (event.type() === Clutter.EventType.BUTTON_PRESS || event.type() === Clutter.EventType.TOUCH_BEGIN) &&
            (event.type() === Clutter.EventType.TOUCH_BEGIN || event.get_button() === 1)) {
            this.menu.close();
            if (this._winProc)
                this._closeWindow();
            else
                this._openWindow();
            return Clutter.EVENT_STOP;
        }
        return super.vfunc_event(event);
    }

    // ------------------------------------------------------------- separate window
    _openWindow() {
        if (this._winProc)
            return;
        try {
            const script = GLib.build_filenamev([this._ext.path, 'historywindow.js']);
            const proc = Gio.Subprocess.new(['gjs', '-m', script], Gio.SubprocessFlags.NONE);
            this._winProc = proc;
            proc.wait_async(null, p => {
                if (this._winProc === p)
                    this._winProc = null;
            });
        } catch (e) {
            Main.notifyError('xDrip+ Top Bar', `Could not open the history window: ${e.message}`);
        }
    }

    _closeWindow() {
        if (this._winProc) {
            this._winProc.send_signal(15);
            this._winProc = null;
        }
    }

    // ------------------------------------------------------------- data
    _watchDir() {
        if (this._monitor) {
            this._monitor.cancel();
            this._monitor = null;
        }
        const dir = dataDir(this._settings);
        try {
            GLib.mkdir_with_parents(dir, 0o700);
            this._monitor = Gio.File.new_for_path(dir).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
            this._monitor.connect('changed', (m, file, other) => {
                const name = (other ?? file)?.get_basename();
                if (name === 'latest.json')
                    this._loadLatest();
                else if (name === 'history.json' && this.menu.isOpen)
                    this._loadHistory();
            });
        } catch (e) {
            logError(e, 'xdrip-topbar: watching data folder');
        }
    }

    _loadLatest() {
        readJsonAsync(GLib.build_filenamev([dataDir(this._settings), 'latest.json']), doc => {
            if (this._destroyed)
                return;
            this._latest = doc;
            this._updateLabels();
            this._updateStatus();
        });
    }

    _loadHistory() {
        readJsonAsync(GLib.build_filenamev([dataDir(this._settings), 'history.json']), doc => {
            if (this._destroyed)
                return;
            this._history = doc;
            this._area.queue_repaint();
        });
    }

    // ------------------------------------------------------------- display
    _updateLabels() {
        const s = this._settings;
        const r = this._latest?.reading;
        if (!r) {
            this._value.text = '--';
            this._arrow.text = '';
            this._mins.text = '';
            this._value.style = 'color: #8a8a8a;';
            return;
        }
        const now = Date.now();
        const v = mmolOf(r);
        const range = rangeOf(v, s.get_double('low-mmol'), s.get_double('high-mmol'));
        const stale = now - r.t >= s.get_int('stale-minutes') * 60000;

        const valueColor = stale ? s.get_string('stale-color') : s.get_string(`value-color-${range}`);
        let arrowColor = valueColor;
        if (!stale && !s.get_boolean('arrow-same-as-value'))
            arrowColor = s.get_string(`arrow-color-${range}`);

        this._value.text = fmtGlucose(v, s.get_string('units'));
        this._arrow.text = ` ${arrowFor(r)}`;
        this._mins.text = ` ${fmtMinutesAgo(r.t, now)}`;
        this._value.style = `color: ${valueColor}; font-weight: bold;`;
        this._arrow.style = `color: ${arrowColor}; font-weight: bold;`;
        this._mins.style = `font-size: 0.85em;${stale ? ` color: ${s.get_string('stale-color')};` : ''}`;
        if (stale)
            this._mins.style += ' text-decoration: line-through;';
    }

    _updateStatus() {
        if (!this._statusLabel)
            return;
        const parts = [];
        const c = this._collector;
        const st = this._latest?.status;
        if (c?.problem) {
            parts.push(c.problem);
        } else if (!c?.running) {
            parts.push('Collector starting…');
        } else if (st && st.source === 'nightscout') {
            parts.push(st.connected ? 'Nightscout: connected' : `Nightscout: connecting… ${st.problem ? `(${st.problem})` : ''}`);
            if (st.last_message_ms)
                parts.push(`last checked ${fmtTime12(st.last_message_ms)}`);
        } else if (st) {
            parts.push(st.connected ? 'Connected' : `Connecting… ${st.problem ? `(${st.problem})` : ''}`);
            parts.push(st.active ? `active as ${st.name}` : 'passive');
            if (st.last_message_ms)
                parts.push(`last message ${fmtTime12(st.last_message_ms)}`);
        }
        this._statusLabel.text = parts.join(' · ');
    }

    destroy() {
        this._destroyed = true;
        if (this._grab) {
            this._grab.dismiss();
            this._grab = null;
        }
        if (this._zoomSaveId)
            GLib.source_remove(this._zoomSaveId);
        this._zoomSaveId = 0;
        if (this._tickId)
            GLib.source_remove(this._tickId);
        this._tickId = 0;
        for (const id of this._settingIds)
            this._settings.disconnect(id);
        this._settingIds = [];
        if (this._menuOpenId)
            this.menu.disconnect(this._menuOpenId);
        if (this._monitor)
            this._monitor.cancel();
        this._monitor = null;
        this._collector.stop();
        this._closeWindow();
        super.destroy();
    }
});

// ============================================================================ extension
export default class XdripTopbarExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._indicator = new Indicator(this);
        const [box, index] = this._placement();
        Main.panel.addToStatusArea(this.uuid, this._indicator, index, box);
        this._posId = this._settings.connect('changed::panel-position', () => this._move());
        // keep collecting while the screen is locked, but don't show the reading on the lock screen
        this._sessionId = Main.sessionMode.connect('updated', () => this._syncVisible());
        this._syncVisible();
    }

    // [box name, index] for the chosen position. The clock is in the centre box.
    _placement() {
        switch (this._settings.get_string('panel-position')) {
        case 'left': return ['left', -1];             // end of the left side (after Activities)
        case 'clock-right': return ['center', -1];    // just right of the clock
        case 'right': return ['right', 0];            // start of the right side, before the system icons
        default: return ['center', 0];                // just left of the clock
        }
    }

    _move() {
        const container = this._indicator?.container;
        if (!container)
            return;
        const [name, index] = this._placement();
        const box = {left: Main.panel._leftBox, center: Main.panel._centerBox, right: Main.panel._rightBox}[name];
        container.get_parent()?.remove_child(container);
        box.insert_child_at_index(container, index < 0 ? box.get_n_children() : index);
    }

    _syncVisible() {
        if (this._indicator) {
            this._indicator.visible = !Main.sessionMode.isLocked;
            if (Main.sessionMode.isLocked)
                this._indicator.menu.close();
        }
    }

    disable() {
        if (this._posId) {
            this._settings.disconnect(this._posId);
            this._posId = 0;
        }
        this._settings = null;
        if (this._sessionId) {
            Main.sessionMode.disconnect(this._sessionId);
            this._sessionId = 0;
        }
        this._indicator?.destroy();
        this._indicator = null;
    }
}
