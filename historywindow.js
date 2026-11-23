// Separate, resizable 48 h history window (used when Settings > History is set to "Separate window").
// Started by the extension with:  gjs -m historywindow.js
// GTK 4 cannot place windows itself, so GNOME decides the position; the size is remembered.

import Gtk from 'gi://Gtk?version=4.0';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import Gdk from 'gi://Gdk?version=4.0';

import {SCHEMA_ID, dataDir, graphOptions, readJsonAsync, drawGraph, zoomView, panView, panViewPixels, hitTest}
    from './common.js';

const HERE = GLib.path_get_dirname(GLib.filename_from_uri(import.meta.url)[0]);
const source = Gio.SettingsSchemaSource.new_from_directory(
    GLib.build_filenamev([HERE, 'schemas']), Gio.SettingsSchemaSource.get_default(), false);
const settings = new Gio.Settings({settings_schema: source.lookup(SCHEMA_ID, true)});

let history = null;
let saveId = 0;
let zoomSaveId = 0;
let view = {spanHours: settings.get_double('graph-hours'), offsetMs: 0};
let mouseX = 0;
let targets = [];
let selected = null;
let selectedLabel = '';

const app = new Gtk.Application({
    application_id: 'org.local.XdripTopbar.History',
    flags: Gio.ApplicationFlags.DEFAULT_FLAGS,
});

app.connect('activate', () => {
    if (app.active_window) {
        app.active_window.present();
        return;
    }
    Gtk.Settings.get_default().gtk_application_prefer_dark_theme = true;

    const win = new Gtk.ApplicationWindow({
        application: app,
        title: 'xDrip+ — last 48 hours',
        default_width: settings.get_int('window-width'),
        default_height: settings.get_int('window-height'),
    });
    win.set_size_request(320, 160);

    const area = new Gtk.DrawingArea({hexpand: true, vexpand: true});
    area.set_draw_func((a, cr, w, h) => {
        try {
            targets = drawGraph(cr, w, h, history, {...graphOptions(settings), selected}, Date.now(), view).targets;
        } catch (e) {
            logError(e);
        }
    });
    // bottom line: details of the clicked item, when "show in the status line" is chosen
    const eventLabel = new Gtk.Label({xalign: 0, margin_start: 8, margin_end: 8, margin_top: 3, margin_bottom: 4,
        css_classes: ['caption-heading'], visible: false});
    const box = new Gtk.Box({orientation: Gtk.Orientation.VERTICAL});
    box.append(area);
    box.append(eventLabel);
    win.set_child(box);
    const showEvent = () => {
        eventLabel.label = selectedLabel;
        eventLabel.visible = settings.get_string('graph-event-info') === 'status' && !!selectedLabel;
    };

    // mouse wheel: zoom around the pointer (remembered); Shift+wheel or sideways scroll: move in time;
    // middle click: back to the whole 48 hours
    const setView = v => {
        view = v;
        area.queue_draw();
        if (zoomSaveId)
            GLib.source_remove(zoomSaveId);
        zoomSaveId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            zoomSaveId = 0;
            if (Math.abs(settings.get_double('graph-hours') - view.spanHours) > 0.01) {
                settings.set_double('graph-hours', view.spanHours);
                Gio.Settings.sync();
            }
            return GLib.SOURCE_REMOVE;
        });
    };
    const motion = new Gtk.EventControllerMotion();
    motion.connect('motion', (c, x) => {
        mouseX = x;
    });
    area.add_controller(motion);
    const scroll = new Gtk.EventControllerScroll({flags: Gtk.EventControllerScrollFlags.BOTH_AXES});
    scroll.connect('scroll', (c, dx, dy) => {
        if (c.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK) {
            dx += dy;
            dy = 0;
        }
        let v = view;
        if (dy)
            v = zoomView(v, area.get_width(), area.get_height(), mouseX, Math.pow(1.2, dy));
        if (dx)
            v = panView(v, -dx * 0.15);
        setView(v);
        return true;
    });
    area.add_controller(scroll);
    const middle = new Gtk.GestureClick({button: 2});
    middle.connect('pressed', () => setView({spanHours: 48, offsetMs: 0}));
    area.add_controller(middle);

    // left button: drag to move through time, or click an item to see its exact time
    const drag = new Gtk.GestureDrag({button: 1});
    let dragStart = null;
    drag.connect('drag-begin', (g, x, y) => {
        dragStart = {x, y, view, moved: false};
    });
    drag.connect('drag-update', (g, dx) => {
        if (!dragStart)
            return;
        if (Math.abs(dx) > 4)
            dragStart.moved = true;
        if (dragStart.moved)
            setView(panViewPixels(dragStart.view, area.get_width(), area.get_height(), dx));
    });
    drag.connect('drag-end', () => {
        if (dragStart && !dragStart.moved) {
            const hit = hitTest(targets, dragStart.x, dragStart.y);
            selected = hit?.key ?? null;
            selectedLabel = hit?.label ?? '';
            showEvent();
            area.queue_draw();
        }
        dragStart = null;
    });
    area.add_controller(drag);

    const load = () => readJsonAsync(GLib.build_filenamev([dataDir(settings), 'history.json']), doc => {
        history = doc;
        area.queue_draw();
    });

    // reload when the collector rewrites history.json, redraw every minute so "now" moves on
    let monitor = null;
    const watch = () => {
        monitor?.cancel();
        try {
            monitor = Gio.File.new_for_path(dataDir(settings)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
            monitor.connect('changed', (m, file, other) => {
                if ((other ?? file)?.get_basename() === 'history.json')
                    load();
            });
        } catch (e) {
            logError(e);
        }
    };
    watch();
    load();
    settings.connect('changed', (s, key) => {
        if (key === 'data-dir') {
            watch();
            load();
        } else if (key === 'graph-event-info') {
            showEvent();
            area.queue_draw();
        } else if (key === 'graph-hours') {
            const h = settings.get_double('graph-hours');
            if (Math.abs(h - view.spanHours) > 0.01)
                view = {spanHours: h, offsetMs: 0};
            area.queue_draw();
        } else {
            area.queue_draw();
        }
    });
    GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 60, () => {
        area.queue_draw();
        return GLib.SOURCE_CONTINUE;
    });

    // remember the size (saved shortly after each resize, so it survives being closed from the top bar)
    const saveSize = () => {
        if (saveId)
            GLib.source_remove(saveId);
        saveId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => {
            saveId = 0;
            if (!win.maximized && !win.fullscreened) {
                const [w, h] = win.get_default_size();
                if (w > 0 && h > 0) {
                    settings.set_int('window-width', w);
                    settings.set_int('window-height', h);
                    Gio.Settings.sync();
                }
            }
            return GLib.SOURCE_REMOVE;
        });
    };
    win.connect('notify::default-width', saveSize);
    win.connect('notify::default-height', saveSize);
    win.connect('close-request', () => {
        Gio.Settings.sync();
        return false;
    });

    win.present();
});

app.run([]);
