#!/usr/bin/env python3
"""
xDrip+ Tray for Windows: the Windows version of the xDrip+ GNOME top-bar extension.

  * Tray icon showing the latest reading (number in the range colour); hover for arrow and minutes
  * Optional small always-on-top widget "6.4 → 3m" you can drag anywhere
  * Click either one for the zoomable 48-hour graph (mouse wheel zoom, Shift+wheel moves in time,
    middle click shows all 48 hours)
  * Built-in collector following xDrip+ Cloud Sync, passive by default
  * Sync key kept in Windows Credential Manager; paste the key or the scanned QR text, or load a
    screenshot of the xDrip+ QR code

Settings:  %APPDATA%\\xdrip-topbar\\settings.json      Data:  %LOCALAPPDATA%\\xdrip-topbar (changeable)

Not a medical device. Rely on xDrip+ for alarms.
"""

import base64
import gzip
import json
import os
import queue
import struct
import sys
import threading
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "..", "collector")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import xdrip_gnome_collector as col      # noqa: E402  (shared with the GNOME extension)
import graph                              # noqa: E402

APP_NAME = "xDrip+ Tray"
VERSION = "1.0.0"
KEYRING_SERVICE = "xdrip-topbar"
KEYRING_USER = "sync-key"
IS_WINDOWS = sys.platform == "win32"


# ============================================================================ settings
def _appdata():
    return os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")


def _localdata():
    return os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")


SETTINGS_PATH = os.path.join(_appdata(), "xdrip-topbar", "settings.json")

# Same meaning and defaults as the GNOME schema.  Only ever ADD keys (see CLAUDE.md).
DEFAULTS = {
    "data_dir": "",
    "active": False,
    "rollcall_name": "gnome_info_collector",
    "low_mmol": 5.0,
    "high_mmol": 10.0,
    "value_color_low": "#ff5c5c", "value_color_in": "#7ddc7d", "value_color_high": "#ffb84d",
    "arrow_color_low": "#ff5c5c", "arrow_color_in": "#7ddc7d", "arrow_color_high": "#ffb84d",
    "arrow_same_as_value": True,
    "stale_minutes": 15,
    "stale_color": "#8a8a8a",
    "graph_show_line": True,
    "graph_smooth": True,
    "graph_line_width": 2.0,
    "graph_line_color_mode": "range",
    "graph_line_color": "#e0e0e0",
    "graph_show_dots": False,
    "graph_gap_minutes": 15,
    "graph_hours": 48.0,
    # Windows only
    "show_tray": True,
    "show_widget": True,
    "widget_x": -1, "widget_y": -1,
    "widget_font_size": 14,
    "widget_opacity": 0.92,
    "graph_geometry": "760x330",
    "start_with_windows": False,
}


class Settings:
    def __init__(self, path=SETTINGS_PATH):
        self.path = path
        self.data = dict(DEFAULTS)
        try:
            with open(path, encoding="utf-8") as fh:
                stored = json.load(fh)
            for k, v in stored.items():          # keep unknown keys too (written by newer versions)
                self.data[k] = v
        except (OSError, ValueError):
            pass

    def __getitem__(self, k):
        return self.data.get(k, DEFAULTS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
        os.replace(tmp, self.path)

    def data_dir(self):
        return os.path.expanduser(self["data_dir"]) or os.path.join(_localdata(), "xdrip-topbar")

    def graph_opts(self):
        return {"low": float(self["low_mmol"]), "high": float(self["high_mmol"]),
                "colors": {"low": self["value_color_low"], "in": self["value_color_in"],
                           "high": self["value_color_high"]},
                "line": bool(self["graph_show_line"]), "smooth": bool(self["graph_smooth"]),
                "line_width": float(self["graph_line_width"]),
                "line_by_range": self["graph_line_color_mode"] == "range",
                "line_color": self["graph_line_color"], "dots": bool(self["graph_show_dots"]),
                "gap_ms": int(self["graph_gap_minutes"]) * 60000, "span_hours": float(self["graph_hours"])}


# ============================================================================ sync key
def get_key():
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        return None


def set_key(key):
    import keyring
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)


def clear_key():
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        pass


def sync_key_from_qr_text(text):
    """The text of xDrip+'s 'xDrip+ security key settings only' QR code -> the sync key.
    Returns None if the text is not a QR export (then it is the key itself)."""
    text = (text or "").strip()
    if text.startswith("xdpref:"):
        raw = text[7:].replace("-", "+").replace("_", "/")
        raw += "=" * (-len(raw) % 4)
        prefs = json.loads(zlib.decompress(base64.b64decode(raw)).decode("utf-8"))
    elif text.startswith("xdp2:"):
        raw = text[5:].replace("-", "+").replace("_", "/")
        raw += "=" * (-len(raw) % 4)
        b = gzip.decompress(base64.b64decode(raw))
        n = struct.unpack(">h", b[:2])[0]
        i, prefs = 2, {}
        for _ in range(n):
            kl = struct.unpack(">h", b[i:i + 2])[0]; i += 2
            k = b[i:i + kl].decode(); i += kl
            vl = struct.unpack(">h", b[i:i + 2])[0]; i += 2
            prefs[k] = b[i:i + vl].decode("utf-8", "replace"); i += vl
    else:
        return None
    key = prefs.get("custom_sync_key")
    if not key:
        raise ValueError("this QR code has no sync key in it - use 'xDrip+ security key settings only'")
    return str(key).strip()


def qr_text_from_image(path):
    """Reads an xDrip+ QR code from a screenshot (needs the zxing-cpp package)."""
    import zxingcpp
    from PIL import Image
    for r in zxingcpp.read_barcodes(Image.open(path).convert("RGB")):
        if r.text.startswith(("xdpref:", "xdp2:")):
            return r.text
    return None


# ============================================================================ collector (in-process)
class Collector:
    def __init__(self, settings, on_change):
        self.settings = settings
        self.on_change = on_change
        self.lock = threading.Lock()
        self.store = None
        self.follower = None
        self.thread = None
        self.lock_fd = None
        self.problem = None

    def start(self):
        key = get_key()
        if not key:
            self.problem = "No sync key saved - open Settings"
            self.on_change()
            return
        folder = self.settings.data_dir()
        os.makedirs(folder, exist_ok=True)
        if self.lock_fd is None:
            self.lock_fd = col.take_lock(folder, takeover=False)
            if self.lock_fd is None:
                self.problem = "Another copy is already collecting into %s" % folder
                self.on_change()
                return
            self._lock_folder = folder
        active = bool(self.settings["active"])
        name = (self.settings["rollcall_name"] or col.DEFAULT_NAME).strip() or col.DEFAULT_NAME
        topic, aes_key = col.derive_keys(key)
        del key
        with self.lock:
            self.store = col.Store(folder, active, name)
        self.problem = None
        self.follower = col.Follower(col.SERVER, col.PORT, col.get_token(folder), topic, aes_key,
                                     self._event, self._status, passive=not active, name=name)
        self.thread = threading.Thread(target=self.follower.run, daemon=True)
        self.thread.start()
        self.on_change()

    def stop(self):
        f, th = self.follower, self.thread
        self.follower = self.thread = None
        if f:
            f.shutdown()
        if th:
            th.join(timeout=5)
        with self.lock:
            if self.store:
                try:
                    self.store.set_status(connected=False, problem="stopped")
                except Exception:
                    pass

    def restart(self):
        self.stop()
        if self.lock_fd is not None and getattr(self, "_lock_folder", None) != self.settings.data_dir():
            os.close(self.lock_fd)                        # data folder changed: release the old one
            self.lock_fd = None
        self.start()

    def _event(self, ev):
        with self.lock:
            try:
                self.store.handle(ev)
            except Exception as e:
                col.log("could not store message: %s" % e)
        self.on_change()

    def _status(self, **kw):
        with self.lock:
            try:
                self.store.set_status(**kw)
            except Exception as e:
                col.log("could not write status: %s" % e)
        self.on_change()

    def snapshot(self):
        """(latest reading, history dict, status dict) — copies, safe to use from the GUI."""
        with self.lock:
            if not self.store:
                return None, {"readings": [], "treatments": [], "bloodtests": []}, {}
            st = self.store
            readings = sorted(st.readings.values(), key=lambda r: r["t"])
            hist = {"readings": readings,
                    "treatments": sorted(st.treatments.values(), key=lambda t: t["t"]),
                    "bloodtests": sorted(st.bloodtests.values(), key=lambda b: b["t"])}
            return (readings[-1] if readings else None), hist, dict(st.status)


# ============================================================================ start with Windows
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_start_with_windows(on):
    if not IS_WINDOWS:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            exe = sys.executable if getattr(sys, "frozen", False) else '%s" "%s' % (sys.executable, os.path.abspath(__file__))
            winreg.SetValueEx(k, "xDripTray", 0, winreg.REG_SZ, '"%s"' % exe)
        else:
            try:
                winreg.DeleteValue(k, "xDripTray")
            except OSError:
                pass


# ============================================================================ GUI
class App:
    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.settings = Settings()
        self.q = queue.Queue()                       # work handed to the Tk thread
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.collector = Collector(self.settings, lambda: self.q.put(self.refresh))
        self.widget = None
        self.graph_win = None
        self.settings_win = None
        self.tray = None
        self.view = {"span_hours": float(self.settings["graph_hours"]), "offset_ms": 0}
        self._zoom_save = None

    # --------------------------------------------------------------- run / quit
    def run(self):
        self.collector.start()
        if self.settings["show_tray"]:
            self.start_tray()
        if self.settings["show_widget"] or not self.settings["show_tray"]:
            self.show_widget()
        if not get_key():
            self.root.after(300, self.open_settings)
        self.root.after(200, self._pump)
        self.root.after(20000, self._tick)
        self.root.mainloop()

    def _pump(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(200, self._pump)

    def _tick(self):
        self.refresh()
        self.root.after(20000, self._tick)

    def quit(self):
        try:
            self.settings.save()
        except OSError:
            pass
        self.collector.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    # --------------------------------------------------------------- what to show
    def current(self):
        """(text parts, colours, stale) for the latest reading."""
        r, _, _ = self.collector.snapshot()
        s = self.settings
        if not r:
            return None
        now = time.time() * 1000
        v = graph.mmol_of(r)
        rng = graph.range_of(v, float(s["low_mmol"]), float(s["high_mmol"]))
        stale = now - r["t"] >= int(s["stale_minutes"]) * 60000
        vc = s["stale_color"] if stale else s["value_color_" + rng]
        ac = vc if (stale or s["arrow_same_as_value"]) else s["arrow_color_" + rng]
        return {"value": "%.1f" % v, "arrow": graph.arrow_for(r), "mins": graph.fmt_minutes_ago(r["t"], now),
                "value_color": vc, "arrow_color": ac, "stale": stale, "t": r["t"]}

    def status_text(self):
        _, _, st = self.collector.snapshot()
        if self.collector.problem:
            return self.collector.problem
        if not st:
            return "Starting…"
        parts = ["Connected" if st.get("connected") else "Connecting…%s" % (" (%s)" % st["problem"] if st.get("problem") else "")]
        parts.append("active as %s" % st.get("name") if st.get("active") else "passive")
        if st.get("last_message_ms"):
            parts.append("last message %s" % graph.fmt_time12(st["last_message_ms"]))
        return " · ".join(parts)

    def refresh(self):
        c = self.current()
        if self.widget:
            self.widget.update(c)
        if self.tray:
            self.update_tray(c)
        if self.graph_win:
            self.graph_win.redraw()

    # --------------------------------------------------------------- tray
    def start_tray(self):
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("Show graph", lambda: self.q.put(self.toggle_graph), default=True),
            pystray.MenuItem("Show widget", lambda: self.q.put(self.toggle_widget),
                             checked=lambda item: self.widget is not None),
            pystray.MenuItem("Settings", lambda: self.q.put(self.open_settings)),
            pystray.MenuItem("Open data folder", lambda: self.q.put(self.open_data_folder)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.q.put(self.quit)),
        )
        self.tray = pystray.Icon("xdrip-tray", self.tray_image(None), APP_NAME, menu)
        self.tray.run_detached()
        self.update_tray(self.current())

    def stop_tray(self):
        if self.tray:
            self.tray.stop()
            self.tray = None

    def tray_image(self, c):
        from PIL import Image, ImageDraw
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if not c:
            d.ellipse([8, 8, 56, 56], outline=(160, 160, 160, 255), width=6)
            return img
        text = c["value"] if len(c["value"]) <= 3 else c["value"].split(".")[0]   # "12.4" -> "12"
        fsize = 40 if len(text) <= 3 else 34
        f = graph.font(fsize)
        d.rounded_rectangle([0, 6, 63, 58], radius=10, fill=(20, 20, 22, 230))
        d.text((32, 33), text, font=f, fill=graph.hex_rgb(c["value_color"]), anchor="mm")
        return img

    def update_tray(self, c):
        if not self.tray:
            return
        try:
            self.tray.icon = self.tray_image(c)
            if c:
                tip = "%s mmol/L %s  %s ago%s" % (c["value"], c["arrow"], c["mins"], "  (stale)" if c["stale"] else "")
                tip = tip.replace("now ago", "just now")
            else:
                tip = "%s - %s" % (APP_NAME, self.status_text())
            self.tray.title = tip[:127]
        except Exception:
            pass

    # --------------------------------------------------------------- widget
    def show_widget(self):
        if not self.widget:
            self.widget = Widget(self)
            self.widget.update(self.current())

    def toggle_widget(self):
        if self.widget:
            self.widget.destroy()
            self.widget = None
            self.settings["show_widget"] = False
        else:
            self.settings["show_widget"] = True
            self.show_widget()
        self.settings.save()
        if self.tray:
            self.tray.update_menu()

    # --------------------------------------------------------------- graph
    def toggle_graph(self):
        if self.graph_win:
            self.graph_win.close()
        else:
            self.graph_win = GraphWindow(self)

    def set_view(self, view):
        self.view = view
        if self.graph_win:
            self.graph_win.redraw()
        if self._zoom_save:
            self.root.after_cancel(self._zoom_save)

        def save():
            self._zoom_save = None
            if abs(float(self.settings["graph_hours"]) - view["span_hours"]) > 0.01:
                self.settings["graph_hours"] = view["span_hours"]
                self.settings.save()
        self._zoom_save = self.root.after(500, save)

    # --------------------------------------------------------------- misc
    def open_data_folder(self):
        folder = self.settings.data_dir()
        os.makedirs(folder, exist_ok=True)
        if IS_WINDOWS:
            os.startfile(folder)

    def open_settings(self):
        if self.settings_win:
            self.settings_win.top.deiconify()
            self.settings_win.top.lift()
            self.settings_win.top.focus_force()
        else:
            self.settings_win = SettingsWindow(self)


class Widget:
    """Small always-on-top box: '6.4 → 3m'. Drag to move, click for the graph, right click for a menu."""

    def __init__(self, app):
        tk = app.tk
        self.app = app
        s = app.settings
        self.top = tk.Toplevel(app.root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", float(s["widget_opacity"]))
        except tk.TclError:
            pass
        bg = "#1c1c1f"
        self.top.configure(bg=bg)
        size = int(s["widget_font_size"])
        frame = tk.Frame(self.top, bg=bg, padx=10, pady=4)
        frame.pack()
        self.value = tk.Label(frame, text="--", bg=bg, fg="#8a8a8a", font=("Segoe UI", size, "bold"))
        self.arrow = tk.Label(frame, text="", bg=bg, fg="#8a8a8a", font=("Segoe UI Symbol", size, "bold"))
        self.mins = tk.Label(frame, text="", bg=bg, fg="#bbbbbb", font=("Segoe UI", max(8, size - 4)))
        for w in (self.value, self.arrow, self.mins):
            w.pack(side="left", padx=(0, 4))
        x, y = int(s["widget_x"]), int(s["widget_y"])
        if x < 0 or y < 0:
            self.top.update_idletasks()
            x = self.top.winfo_screenwidth() - 220
            y = 60
        self.top.geometry("+%d+%d" % (x, y))

        self.menu = tk.Menu(self.top, tearoff=0)
        self.menu.add_command(label="Show graph", command=app.toggle_graph)
        self.menu.add_command(label="Settings", command=app.open_settings)
        self.menu.add_command(label="Hide widget", command=app.toggle_widget)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=app.quit)

        for w in (self.top, frame, self.value, self.arrow, self.mins):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._release)
            w.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self._start = None

    def _press(self, e):
        self._start = (e.x_root, e.y_root, self.top.winfo_x(), self.top.winfo_y())
        self._moved = False

    def _drag(self, e):
        if not self._start:
            return
        dx, dy = e.x_root - self._start[0], e.y_root - self._start[1]
        if abs(dx) + abs(dy) > 3:
            self._moved = True
        self.top.geometry("+%d+%d" % (self._start[2] + dx, self._start[3] + dy))

    def _release(self, e):
        if self._start and not self._moved:
            self.app.toggle_graph()
        elif self._start:
            self.app.settings["widget_x"] = self.top.winfo_x()
            self.app.settings["widget_y"] = self.top.winfo_y()
            self.app.settings.save()
        self._start = None

    def update(self, c):
        if not c:
            self.value.configure(text="--", fg="#8a8a8a")
            self.arrow.configure(text="")
            self.mins.configure(text="")
            return
        self.value.configure(text=c["value"], fg=c["value_color"])
        self.arrow.configure(text=c["arrow"], fg=c["arrow_color"])
        self.mins.configure(text=c["mins"], fg=self.app.settings["stale_color"] if c["stale"] else "#bbbbbb",
                            font=("Segoe UI", max(8, int(self.app.settings["widget_font_size"]) - 4),
                                  "overstrike" if c["stale"] else "normal"))

    def destroy(self):
        self.top.destroy()


class GraphWindow:
    def __init__(self, app):
        tk = app.tk
        self.app = app
        app.view = {"span_hours": float(app.settings["graph_hours"]), "offset_ms": 0}
        self.top = tk.Toplevel(app.root)
        self.top.title("xDrip+ — last 48 hours")
        self.top.configure(bg="#1c1c1f")
        self.top.minsize(320, 160)
        try:
            self.top.geometry(app.settings["graph_geometry"])
        except tk.TclError:
            self.top.geometry("760x330")
        self.canvas = tk.Canvas(self.top, bg="#1c1c1f", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.status = tk.Label(self.top, text="", bg="#1c1c1f", fg="#999999", anchor="w", font=("Segoe UI", 9))
        self.status.pack(fill="x", padx=6, pady=(0, 3))
        self.photo = None
        self._pending = None
        self._geo_save = None
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<MouseWheel>", self._wheel)                  # Windows / macOS
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self._wheel(e, shift=True))
        self.canvas.bind("<Button-4>", lambda e: self._wheel(e, delta=120))   # Linux (testing)
        self.canvas.bind("<Button-5>", lambda e: self._wheel(e, delta=-120))
        self.canvas.bind("<Button-2>", lambda e: app.set_view({"span_hours": 48.0, "offset_ms": 0}))
        self.top.bind("<Configure>", self._moved)
        self.top.protocol("WM_DELETE_WINDOW", self.close)

    def _wheel(self, e, shift=False, delta=None):
        d = delta if delta is not None else e.delta
        steps = -d / 120.0                                   # wheel up (positive delta) = zoom in
        if shift or (e.state & 0x0001):
            self.app.set_view(graph.pan_view(self.app.view, steps * 0.15 * -1))
        else:
            self.app.set_view(graph.zoom_view(self.app.view, self.canvas.winfo_width(),
                                              self.canvas.winfo_height(), e.x, 1.2 ** steps))

    def _moved(self, e):
        if e.widget is not self.top:
            return
        if self._geo_save:
            self.top.after_cancel(self._geo_save)

        def save():
            self._geo_save = None
            self.app.settings["graph_geometry"] = self.top.geometry()
            self.app.settings.save()
        self._geo_save = self.top.after(600, save)

    def redraw(self):
        if self._pending is None:
            self._pending = self.top.after(30, self._draw)

    def _draw(self):
        from PIL import ImageTk
        self._pending = None
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 20 or h < 20:
            return
        _, hist, _ = self.app.collector.snapshot()
        img = graph.draw_graph(w, h, hist, self.app.settings.graph_opts(), time.time() * 1000, self.app.view)
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.status.configure(text=self.app.status_text() +
                              "    ·  wheel: zoom   Shift+wheel: move in time   middle click: all 48 h")

    def close(self):
        try:
            self.app.settings["graph_geometry"] = self.top.geometry()
            self.app.settings.save()
        except Exception:
            pass
        self.top.destroy()
        self.app.graph_win = None


class SettingsWindow:
    def __init__(self, app):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.app = tk, ttk, app
        s = app.settings
        top = self.top = tk.Toplevel(app.root)
        top.title("%s settings" % APP_NAME)
        top.protocol("WM_DELETE_WINDOW", self.close)
        nb = ttk.Notebook(top)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # ---------------- collector
        c = ttk.Frame(nb, padding=10)
        nb.add(c, text="Collector")
        ttk.Label(c, text="xDrip+ sync key", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(c, wraplength=460, justify="left", text=(
            "On your master phone: xDrip+ › Settings › xDrip+ Sync Settings, tick \"Use xDrip Cloud\". "
            "Then paste the Handset Group Security Sync Key below, or the text from scanning "
            "Settings › Show Settings QR codes › xDrip+ security key settings only (starts with xdpref:), "
            "or load a screenshot of that QR code. The key is kept in Windows Credential Manager.")
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 6))
        self.key_var = tk.StringVar()
        ttk.Entry(c, textvariable=self.key_var, show="•", width=46).grid(row=2, column=0, sticky="we")
        ttk.Button(c, text="Save key", command=self.save_key).grid(row=2, column=1, padx=4)
        ttk.Button(c, text="QR screenshot…", command=self.load_qr).grid(row=2, column=2)
        self.key_status = ttk.Label(c, text="")
        self.key_status.grid(row=3, column=0, sticky="w", pady=4)
        ttk.Button(c, text="Remove key", command=self.remove_key).grid(row=3, column=1, columnspan=2, sticky="e")

        ttk.Separator(c).grid(row=4, column=0, columnspan=3, sticky="we", pady=8)
        self.active = self._check(c, "Active: appear in xDrip+ followers list (default: passive, sends nothing)",
                                  "active", 5, restart=True)
        ttk.Label(c, text="Name shown in xDrip+ when active").grid(row=6, column=0, sticky="w")
        self.name_var = tk.StringVar(value=s["rollcall_name"])
        ttk.Entry(c, textvariable=self.name_var, width=24).grid(row=6, column=1, columnspan=2, sticky="we")
        ttk.Button(c, text="Apply name", command=self.apply_name).grid(row=7, column=2, sticky="e")

        ttk.Separator(c).grid(row=8, column=0, columnspan=3, sticky="we", pady=8)
        ttk.Label(c, text="Data folder (latest.json, history.json for 48 h, collector.log)").grid(row=9, column=0, columnspan=3, sticky="w")
        self.dir_label = ttk.Label(c, text=s.data_dir(), foreground="#555")
        self.dir_label.grid(row=10, column=0, sticky="w")
        ttk.Button(c, text="Change…", command=self.choose_dir).grid(row=10, column=1)
        ttk.Button(c, text="Open", command=app.open_data_folder).grid(row=10, column=2)
        ttk.Separator(c).grid(row=11, column=0, columnspan=3, sticky="we", pady=8)
        self._check(c, "Start with Windows", "start_with_windows", 12,
                    after=lambda v: set_start_with_windows(v))
        self.status = ttk.Label(c, text=app.status_text(), foreground="#555", wraplength=460)
        self.status.grid(row=13, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # ---------------- display
        d = ttk.Frame(nb, padding=10)
        nb.add(d, text="Display")
        self._check(d, "Tray icon", "show_tray", 0, after=lambda v: self._tray(v))
        self._check(d, "Floating widget (drag it anywhere; right click for a menu)", "show_widget", 1,
                    after=lambda v: self._widget(v))
        self._spin(d, "Widget text size", "widget_font_size", 2, 8, 40, 1, rebuild_widget=True)
        self._spin(d, "Widget opacity (0.3–1)", "widget_opacity", 3, 0.3, 1.0, 0.05, rebuild_widget=True)
        ttk.Separator(d).grid(row=4, column=0, columnspan=4, sticky="we", pady=8)
        self._spin(d, "Low below (mmol/L)", "low_mmol", 5, 2.0, 15.0, 0.1)
        self._spin(d, "High above (mmol/L)", "high_mmol", 6, 5.0, 30.0, 0.1)
        ttk.Separator(d).grid(row=7, column=0, columnspan=4, sticky="we", pady=8)
        self._check(d, "Same colour for arrow (combine value and arrow colours)", "arrow_same_as_value", 8,
                    after=lambda v: self._arrow_state())
        ttk.Label(d, text="Value").grid(row=9, column=1)
        ttk.Label(d, text="Arrow").grid(row=9, column=2)
        self.arrow_buttons = []
        for i, (rid, title) in enumerate((("low", "Low"), ("in", "In range"), ("high", "High"))):
            ttk.Label(d, text=title).grid(row=10 + i, column=0, sticky="w")
            self._color(d, "value_color_" + rid, 10 + i, 1)
            self.arrow_buttons.append(self._color(d, "arrow_color_" + rid, 10 + i, 2))
        self._arrow_state()
        ttk.Separator(d).grid(row=13, column=0, columnspan=4, sticky="we", pady=8)
        self._spin(d, "Stale after (minutes)", "stale_minutes", 14, 5, 120, 1)
        ttk.Label(d, text="Stale colour").grid(row=15, column=0, sticky="w")
        self._color(d, "stale_color", 15, 1)

        # ---------------- graph
        g = ttk.Frame(nb, padding=10)
        nb.add(g, text="Graph")
        self._check(g, "Draw a line through the readings", "graph_show_line", 0)
        self._check(g, "Smooth curve (never goes above or below the actual readings)", "graph_smooth", 1)
        self._spin(g, "Line width", "graph_line_width", 2, 1.0, 6.0, 0.5)
        ttk.Label(g, text="Line colour").grid(row=3, column=0, sticky="w")
        mode = tk.StringVar(value="By range" if s["graph_line_color_mode"] == "range" else "One colour")
        cb = ttk.Combobox(g, textvariable=mode, values=["By range", "One colour"], state="readonly", width=12)
        cb.grid(row=3, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self._set("graph_line_color_mode",
                                                            "range" if mode.get() == "By range" else "single"))
        ttk.Label(g, text="Single line colour").grid(row=4, column=0, sticky="w")
        self._color(g, "graph_line_color", 4, 1)
        self._check(g, "Show a dot for every reading (always shown when the line is off)", "graph_show_dots", 5)
        self._spin(g, "Break the line after a gap of (minutes)", "graph_gap_minutes", 6, 6, 120, 1)
        ttk.Button(g, text="Reset zoom to 48 hours",
                   command=lambda: app.set_view({"span_hours": 48.0, "offset_ms": 0})).grid(row=7, column=0, sticky="w", pady=8)
        ttk.Label(g, text="On the graph: mouse wheel zooms (remembered), Shift + wheel moves back and forward in time, "
                          "middle click shows all 48 hours.", wraplength=460, foreground="#555").grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Label(top, text="%s %s · Not a medical device: rely on xDrip+ for alarms." % (APP_NAME, VERSION),
                  foreground="#777").pack(pady=(0, 8))
        self._key_status()

    # ---- helpers
    def _set(self, key, value):
        self.app.settings[key] = value
        self.app.settings.save()
        self.app.refresh()

    def _check(self, parent, text, key, row, restart=False, after=None):
        var = self.tk.BooleanVar(value=bool(self.app.settings[key]))

        def changed():
            self._set(key, var.get())
            if after:
                try:
                    after(var.get())
                except Exception as e:
                    from tkinter import messagebox
                    messagebox.showerror(APP_NAME, str(e), parent=self.top)
            if restart:
                self.app.collector.restart()
        self.ttk.Checkbutton(parent, text=text, variable=var, command=changed).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=1)
        return var

    def _spin(self, parent, text, key, row, lo, hi, inc, rebuild_widget=False):
        self.ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w")
        is_int = isinstance(DEFAULTS[key], int) and not isinstance(DEFAULTS[key], bool)
        var = self.tk.StringVar(value=str(self.app.settings[key]))

        def changed(*_):
            try:
                v = int(float(var.get())) if is_int else round(float(var.get()), 2)
            except ValueError:
                return
            v = max(lo, min(hi, v))
            self._set(key, v)
            if rebuild_widget and self.app.widget:
                self.app.widget.destroy()
                self.app.widget = None
                self.app.show_widget()
        sp = self.ttk.Spinbox(parent, from_=lo, to=hi, increment=inc, textvariable=var, width=8, command=changed)
        sp.grid(row=row, column=1, sticky="w")
        sp.bind("<FocusOut>", changed)
        sp.bind("<Return>", changed)

    def _color(self, parent, key, row, column):
        from tkinter import colorchooser
        btn = self.tk.Button(parent, width=4, bg=self.app.settings[key], activebackground=self.app.settings[key],
                             relief="groove")

        def pick():
            rgb, hx = colorchooser.askcolor(color=self.app.settings[key], parent=self.top)
            if hx:
                self._set(key, hx)
                btn.configure(bg=hx, activebackground=hx)
        btn.configure(command=pick)
        btn.grid(row=row, column=column, padx=4, pady=2)
        return btn

    def _arrow_state(self):
        state = "disabled" if self.app.settings["arrow_same_as_value"] else "normal"
        for b in getattr(self, "arrow_buttons", []):
            b.configure(state=state)

    def _tray(self, on):
        if on:
            self.app.start_tray()
        else:
            self.app.stop_tray()
            self.app.show_widget()                        # never leave the app with nothing on screen

    def _widget(self, on):
        if on and not self.app.widget:
            self.app.show_widget()
        elif not on and self.app.widget:
            if not self.app.tray:
                self.app.start_tray()
                self.app.settings["show_tray"] = True
            self.app.widget.destroy()
            self.app.widget = None

    def _key_status(self):
        self.key_status.configure(text="A key is saved" if get_key() else "No key saved yet")
        self.status.configure(text=self.app.status_text())

    def _use_text(self, text):
        from tkinter import messagebox
        text = (text or "").strip()
        try:
            key = sync_key_from_qr_text(text) or text
        except Exception as e:
            messagebox.showerror(APP_NAME, "QR code: %s" % e, parent=self.top)
            return
        if not key:
            return
        try:
            set_key(key)
        except Exception as e:
            messagebox.showerror(APP_NAME, "Could not save to Credential Manager: %s" % e, parent=self.top)
            return
        warn = "\n\nWarning: xDrip+ keys are usually longer than 16 characters." if len(key) <= 16 else ""
        messagebox.showinfo(APP_NAME, "Sync key saved.%s" % warn, parent=self.top)
        self.app.collector.restart()
        self._key_status()

    def save_key(self):
        text = self.key_var.get()
        self.key_var.set("")
        self._use_text(text)

    def load_qr(self):
        from tkinter import filedialog, messagebox
        path = filedialog.askopenfilename(parent=self.top, title="Screenshot of the xDrip+ QR code",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"), ("All", "*.*")])
        if not path:
            return
        try:
            text = qr_text_from_image(path)
        except ImportError:
            messagebox.showerror(APP_NAME, "QR reading is not available in this build.", parent=self.top)
            return
        except Exception as e:
            messagebox.showerror(APP_NAME, "Could not read the image: %s" % e, parent=self.top)
            return
        if not text:
            messagebox.showerror(APP_NAME, "No xDrip+ settings QR code found in that image.", parent=self.top)
            return
        self._use_text(text)
        messagebox.showinfo(APP_NAME, "Remember to delete the screenshot - it contains your key.", parent=self.top)

    def remove_key(self):
        clear_key()
        self.app.collector.restart()
        self._key_status()

    def apply_name(self):
        self._set("rollcall_name", self.name_var.get().strip() or col.DEFAULT_NAME)
        self.app.collector.restart()

    def choose_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(parent=self.top, initialdir=self.app.settings.data_dir())
        if d:
            self._set("data_dir", d)
            self.dir_label.configure(text=self.app.settings.data_dir())
            self.app.collector.restart()

    def close(self):
        self.top.destroy()
        self.app.settings_win = None


# ============================================================================ self test (used by CI)
def selftest(out_path=None):
    lines = []
    ok = True

    def check(cond, text):
        nonlocal ok
        lines.append(("ok   " if cond else "FAIL ") + text)
        ok = ok and bool(cond)

    for mod in ("tkinter", "PIL.ImageTk", "pystray", "keyring", "cryptography", "zxingcpp"):
        try:
            __import__(mod)
            check(True, "import %s" % mod)
        except Exception as e:
            check(False, "import %s (%s)" % (mod, e))

    key = "this-is-a-test-sync-key-1234567890"
    j = json.dumps({"custom_sync_key": key, "use_custom_sync_key": "true"})
    qr = "xdpref:" + base64.b64encode(zlib.compress(j.encode())).decode().rstrip("=")
    check(sync_key_from_qr_text(qr) == key, "sync key from QR text")
    check(sync_key_from_qr_text(key) is None, "plain key passes through")

    now = int(time.time() * 1000)
    hist = {"readings": [{"t": now - i * 300000, "mgdl": 110 + 40 * ((i % 24) / 24)} for i in range(575, -1, -1)],
            "treatments": [{"t": now - 3600000, "insulin": 4, "carbs": 30}], "bloodtests": []}
    img = graph.draw_graph(760, 330, hist, Settings(os.devnull).graph_opts(), now)
    check(img.size == (760, 330), "graph renders")
    if out_path:
        img.save(os.path.splitext(out_path)[0] + "_graph.png")

    folder = os.path.join(_localdata(), "xdrip-topbar-selftest")
    os.makedirs(folder, exist_ok=True)
    st = col.Store(folder, False, None)
    st.handle({"action": "bgs", "readings": [col.make_reading(now, 120.0, 0.0)], "bloodtests": [], "text": None, "json": None})
    check(os.path.exists(os.path.join(folder, "history.json")), "collector store writes history.json")
    check(col.take_lock(folder, takeover=False) is not None, "data folder lock")

    lines.append("SELF TEST " + ("PASSED" if ok else "FAILED"))
    text = "\n".join(lines)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else None
        sys.exit(selftest(out))
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)         # crisp text on high-DPI screens
        except Exception:
            pass
    # a windowed .exe has no console: send the collector's log to collector.log in the data folder
    if sys.stdout is None or getattr(sys, "frozen", False):
        try:
            folder = Settings().data_dir()
            os.makedirs(folder, exist_ok=True)
            fh = open(os.path.join(folder, "collector.log"), "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = fh
        except OSError:
            pass
    App().run()


if __name__ == "__main__":
    main()
