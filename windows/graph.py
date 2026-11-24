"""
48-hour graph for the Windows app, drawn with Pillow.  Same look and behaviour as common.js in
the GNOME extension: shaded target band, line coloured by range (smooth monotone curve that never
overshoots the readings), dots optional, insulin (blue) and carbs (orange) markers, 12-hour clock,
mouse-wheel zoom and moving back in time.
"""

import datetime
import math
import time

from PIL import Image, ImageDraw, ImageFont

HISTORY_HOURS = 48
MIN_SPAN_HOURS = 1
MGDL_PER_MMOL = 18.0182
SS = 2                                   # supersampling factor (smooth lines without antialias support)

ARROWS = {"DoubleUp": "⇈", "SingleUp": "↑", "FortyFiveUp": "↗", "Flat": "→",
          "FortyFiveDown": "↘", "SingleDown": "↓", "DoubleDown": "⇊", "NotComputable": "?"}

INSULIN_COLOR = "#4da3ff"
CARBS_COLOR = "#ffa53d"
FINGER_COLOR = "#ff4040"

_font_cache = {}


def font(size):
    size = int(round(size))
    if size not in _font_cache:
        f = None
        for name in ("segoeui.ttf", "seguisym.ttf", "arial.ttf", "DejaVuSans.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        if f is None:
            try:
                f = ImageFont.load_default(size)
            except TypeError:                       # Pillow < 10.1
                f = ImageFont.load_default()
        _font_cache[size] = f
    return _font_cache[size]


_sym_cache = {}
ARROW_CHARS = set("⇈↑↗→↘↓⇊")


def symbol_font(size):
    """A font that has all the trend arrows (the normal Windows font lacks some, e.g. ⇈ ⇊)."""
    size = int(round(size))
    if size not in _sym_cache:
        f = None
        for name in ("seguisym.ttf", "DejaVuSans.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        _sym_cache[size] = f or font(size)
    return _sym_cache[size]


def _runs_of_text(text):
    """Splits text into (is_arrow, part) pieces."""
    out = []
    for ch in text:
        a = ch in ARROW_CHARS
        if out and out[-1][0] == a:
            out[-1][1] += ch
        else:
            out.append([a, ch])
    return out


def text_width(dr, text, size):
    return sum(dr.textlength(part, font=symbol_font(size) if a else font(size)) for a, part in _runs_of_text(text))


def draw_text_lm(dr, x, y, text, size, fill):
    """Left-middle anchored text; arrows are drawn with the symbol font."""
    for a, part in _runs_of_text(text):
        f = symbol_font(size) if a else font(size)
        dr.text((x, y), part, font=f, fill=fill, anchor="lm")
        x += dr.textlength(part, font=f)


# ---------------------------------------------------------------- helpers shared with the app
def mmol_of(r):
    if r is None:
        return None
    return r["mgdl"] / MGDL_PER_MMOL if "mgdl" in r else r.get("mmol")


def trend_from_slope(s):
    if s is None:
        return "NotComputable"
    if s <= -3.5: return "DoubleDown"
    if s <= -2: return "SingleDown"
    if s <= -1: return "FortyFiveDown"
    if s <= 1: return "Flat"
    if s <= 2: return "FortyFiveUp"
    if s <= 3.5: return "SingleUp"
    return "DoubleUp"


def arrow_for(r):
    if not r:
        return ""
    return ARROWS.get(r.get("trend")) or ARROWS[trend_from_slope(r.get("slope"))]


def fmt_glucose(mmol, units):
    """'6.4' (mmol/L) or '115' (mg/dL)."""
    return "%d" % round(mmol * MGDL_PER_MMOL) if units == "mgdl" else "%.1f" % mmol


def unit_label(units):
    return "mg/dL" if units == "mgdl" else "mmol/L"


def range_of(v, low, high):
    return "low" if v < low else "high" if v > high else "in"


def fmt_time12(ms, with_minutes=True):
    d = datetime.datetime.fromtimestamp(ms / 1000)
    h = d.hour % 12 or 12
    ap = "AM" if d.hour < 12 else "PM"
    return "%d:%02d %s" % (h, d.minute, ap) if with_minutes else "%d %s" % (h, ap)


def fmt_minutes_ago(ms, now_ms):
    m = max(0, int((now_ms - ms) // 60000))
    if m < 1:
        return "now"
    if m < 60:
        return "%dm" % m
    h = m // 60
    return "%dh" % h if h < 24 else "%dd" % (h // 24)


def hex_rgb(h, alpha=255):
    h = (h or "#ffffff").lstrip("#")[:6]
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
    except ValueError:
        return (255, 255, 255, alpha)


# ---------------------------------------------------------------- zoom / pan
def _layout(w, h):
    fs = max(9, min(12, h / 22)) * 1.15            # Windows fonts look smaller than GNOME's
    pad_l, pad_r, pad_t, pad_b = math.ceil(fs * 2.4), 8, math.ceil(fs * 2.0), math.ceil(fs * 1.9)
    return fs, pad_l, pad_r, pad_t, pad_b, max(10, w - pad_l - pad_r), max(10, h - pad_t - pad_b)


def clamp_view(span_hours, offset_ms):
    span = max(MIN_SPAN_HOURS, min(HISTORY_HOURS, span_hours))
    max_off = (HISTORY_HOURS - span) * 3600000
    return {"span_hours": span, "offset_ms": max(0, min(max_off, offset_ms))}


def zoom_view(view, w, h, mouse_x, factor):
    """factor < 1 zooms in. Showing 'now' stays live; otherwise zooms around the pointer."""
    if view["offset_ms"] < 1000:
        return clamp_view(view["span_hours"] * factor, 0)
    _, pad_l, _, _, _, pw, _ = _layout(w, h)
    fx = max(0.0, min(1.0, (mouse_x - pad_l) / pw))
    old = view["span_hours"] * 3600000
    t_at = -view["offset_ms"] - old * (1 - fx)
    span = clamp_view(view["span_hours"] * factor, 0)["span_hours"]
    new_end = t_at + span * 3600000 * (1 - fx)
    return clamp_view(span, -new_end)


def pan_view(view, fraction):
    """+ = further back in time, as a fraction of the visible span."""
    return clamp_view(view["span_hours"], view["offset_ms"] + fraction * view["span_hours"] * 3600000)


def pan_view_pixels(view, w, h, dx_px):
    """Drag to scroll: dx_px pixels to the right shows earlier readings."""
    pw = _layout(w, h)[5]
    return clamp_view(view["span_hours"], view["offset_ms"] + dx_px / pw * view["span_hours"] * 3600000)


def fmt_when(ms, now_ms=None):
    """'3:42 PM' for today, 'Sun 3:42 PM' for other days."""
    d = datetime.datetime.fromtimestamp(ms / 1000)
    n = datetime.datetime.fromtimestamp((now_ms or time.time() * 1000) / 1000)
    t = fmt_time12(ms)
    return t if d.date() == n.date() else "%s %s" % (d.strftime("%a"), t)


def hit_test(targets, x, y):
    """The item under (x, y) in a graph drawn by draw_graph(out=...), or None."""
    best, best_d = None, float("inf")
    for t in targets or []:
        d = math.hypot(t["x"] - x, t["y"] - y) - (0 if t["kind"] == "reading" else 6)   # markers win
        if d < (14 if t["kind"] == "reading" else 16) and d < best_d:
            best, best_d = t, d
    return best


def _fmt_span(hours):
    return "%d h" % round(hours) if hours >= 1.95 else "%d min" % round(hours * 60)


# ---------------------------------------------------------------- curve
def _runs(points, gap_ms):
    runs, cur = [], []
    for p in points:
        if cur and p["t"] - cur[-1]["t"] > gap_ms:
            runs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        runs.append(cur)
    return runs


def _smooth(pts, steps=8):
    """Monotone cubic (Fritsch–Carlson) through pts [(x, y)], sampled into a polyline."""
    n = len(pts)
    if n < 3:
        return pts
    d = []
    for i in range(n - 1):
        h = pts[i + 1][0] - pts[i][0]
        d.append((pts[i + 1][1] - pts[i][1]) / h if h > 0 else 0.0)
    m = [0.0] * n
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, n - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        s = a * a + b * b
        if s > 9:
            t = 3 / math.sqrt(s)
            m[i], m[i + 1] = t * a * d[i], t * b * d[i]
    out = [pts[0]]
    for i in range(n - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        h = x1 - x0
        for k in range(1, steps + 1):
            u = k / steps
            h00, h10 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u
            h01, h11 = -2 * u ** 3 + 3 * u ** 2, u ** 3 - u ** 2
            out.append((x0 + u * h, h00 * y0 + h10 * h * m[i] + h01 * y1 + h11 * h * m[i + 1]))
    return out


def _interp(readings, t):
    if not readings:
        return None
    if t <= readings[0]["t"]:
        return mmol_of(readings[0])
    if t >= readings[-1]["t"]:
        return mmol_of(readings[-1])
    for a, b in zip(readings, readings[1:]):
        if a["t"] <= t <= b["t"]:
            if b["t"] - a["t"] > 30 * 60000:
                return None
            f = (t - a["t"]) / (b["t"] - a["t"])
            return mmol_of(a) + f * (mmol_of(b) - mmol_of(a))
    return None


def _units(n):
    return str(int(n)) if float(n).is_integer() else "%.1f" % n


# ---------------------------------------------------------------- the graph
def draw_graph(w, h, history, opts, now_ms, view=None, out=None):
    """Returns a w x h RGB image.  opts: dict made by the app from its settings.
    out (optional dict) receives "targets": the clickable items, in image pixels."""
    v0 = clamp_view((view or {}).get("span_hours", opts.get("span_hours", HISTORY_HOURS)),
                    (view or {}).get("offset_ms", 0))
    span = v0["span_hours"] * 3600000
    t_end = now_ms - v0["offset_ms"]
    t0 = t_end - span
    allr = [r for r in (history or {}).get("readings", [])
            if now_ms - HISTORY_HOURS * 3600000 <= r["t"] <= now_ms + 600000]
    i_first = next((i for i, r in enumerate(allr) if r["t"] >= t0), len(allr))
    i_last = len(allr) - 1
    while i_last >= 0 and allr[i_last]["t"] > t_end:
        i_last -= 1
    readings = allr[max(0, i_first - 1):min(len(allr), i_last + 2)]
    visible = allr[i_first:i_last + 1]
    treatments = [t for t in (history or {}).get("treatments", [])
                  if t0 <= t["t"] <= t_end + (0 if v0["offset_ms"] else 3600000)]
    finger = [b for b in (history or {}).get("bloodtests", []) if t0 <= b["t"] <= t_end]

    W, H = w * SS, h * SS
    img = Image.new("RGB", (W, H), (28, 28, 31))
    dr = ImageDraw.Draw(img, "RGBA")
    fs, pad_l, pad_r, pad_t, pad_b, pw, ph = _layout(w, h)
    S = lambda v: v * SS
    f_main, f_small = font(S(fs)), font(S(fs * 0.9))

    # internally mmol/L; only the labels use the chosen units
    units = opts.get("units", "mmol")
    mgdl = units == "mgdl"
    k = MGDL_PER_MMOL if mgdl else 1.0
    lo, hi = 2.0, 15.0
    for r in visible:
        v = mmol_of(r)
        lo, hi = min(lo, math.floor(v)), max(hi, v + 1)
    lo_d = math.floor(lo * k / 10) * 10 if mgdl else lo
    y_step = (100 if hi * k - lo_d > 360 else 50) if mgdl else (5 if hi - lo > 20 else 3)
    v_min, v_max = lo_d / k, math.ceil(hi * k / y_step) * y_step / k
    X = lambda t: S(pad_l + (t - t0) / span * pw)
    Y = lambda v: S(pad_t + ph - (v - v_min) / (v_max - v_min) * ph)
    top, bottom = S(pad_t), S(pad_t + ph)
    left, right = S(pad_l), S(pad_l + pw)
    low, high, colors = opts["low"], opts["high"], opts["colors"]

    # target band, dashed limits
    dr.rectangle([left, Y(high), right, Y(low)], fill=(115, 217, 115, 23))
    for v, c in ((low, colors["low"]), (high, colors["high"])):
        y = Y(v)
        x = left
        while x < right:
            dr.line([x, y, min(right, x + S(4)), y], fill=hex_rgb(c, 140), width=SS)
            x += S(7)

    # y grid
    v_d = math.ceil(v_min * k / y_step - 1e-9) * y_step
    while v_d <= v_max * k + 1e-9:
        y = Y(v_d / k)
        dr.line([left, y, right, y], fill=(255, 255, 255, 18), width=SS)
        dr.text((left - S(5), y), str(int(round(v_d))), font=f_main, fill=(255, 255, 255, 140), anchor="rm")
        v_d += y_step

    # x grid: 12 h clock, day name at midnight
    px_per_min = pw / (span / 60000)
    step = next((m for m in (15, 30, 60, 120, 180, 360, 720, 1440) if m * px_per_min >= fs * 5.5), 1440)
    d = datetime.datetime.fromtimestamp(t0 / 1000).replace(second=0, microsecond=0)
    d = d.replace(minute=d.minute - d.minute % 15)
    while (d.hour * 60 + d.minute) % step or d.timestamp() * 1000 < t0:
        d += datetime.timedelta(minutes=15)
    while d.timestamp() * 1000 <= t_end:
        x = X(d.timestamp() * 1000)
        midnight = d.hour == 0 and d.minute == 0
        dr.line([x, top, x, bottom], fill=(255, 255, 255, 46 if midnight else 18), width=SS)
        label = d.strftime("%a") if midnight else fmt_time12(d.timestamp() * 1000, step < 60)
        if x - left > S(fs * 1.2) and right - x > S(fs * 1.2):
            dr.text((x, bottom + S(fs * 0.9)), label, font=f_main,
                    fill=(255, 255, 255, 204 if midnight else 140), anchor="mm")
        d += datetime.timedelta(minutes=step)

    # readings
    px_per_reading = pw / (span / 300000)
    rad = max(1.3, min(3.5, px_per_reading * 0.35))
    runs = [[(X(r["t"]), Y(mmol_of(r)), mmol_of(r)) for r in run]
            for run in _runs(readings, opts.get("gap_ms", 15 * 60000))]
    show_line = opts.get("line", True)
    targets = []
    when = lambda ms: fmt_when(ms, now_ms)
    for r in visible:
        v = mmol_of(r)
        targets.append({"kind": "reading", "key": "r%d" % r["t"], "x": X(r["t"]) / SS, "y": Y(v) / SS,
                        "label": "%s %s %s at %s" % (fmt_glucose(v, units), unit_label(units), arrow_for(r), when(r["t"]))})
    if show_line:
        lw = max(1, int(round(S(opts.get("line_width", 2)))))
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        for run in runs:
            if len(run) == 1:
                x, y, _ = run[0]
                r_ = max(S(rad), lw / 2)
                md.ellipse([x - r_, y - r_, x + r_, y + r_], fill=255)
            else:
                pts = [(x, y) for x, y, _ in run]
                if opts.get("smooth", True):
                    pts = _smooth(pts)
                md.line(pts, fill=255, width=lw, joint="curve")
                for x, y in (pts[0], pts[-1]):
                    md.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=255)
        if opts.get("line_by_range", True):
            bands = [(0, Y(high), colors["high"]), (Y(high), Y(low), colors["in"]), (Y(low), H, colors["low"])]
        else:
            bands = [(0, H, opts.get("line_color", "#e0e0e0"))]
        for y_a, y_b, c in bands:
            box = (int(left), int(max(0, y_a)), int(right), int(min(H, y_b)))
            if box[3] > box[1]:
                img.paste(hex_rgb(c)[:3], box, mask.crop(box))
    if opts.get("dots", False) or not show_line:
        dot = S(rad * (0.8 if show_line else 1))
        for run in runs:
            for x, y, v in run:
                if left - dot <= x <= right + dot:
                    dr.ellipse([x - dot, y - dot, x + dot, y + dot], fill=hex_rgb(colors[range_of(v, low, high)]))

    for b in finger:
        v = mmol_of(b)
        x, y, r_ = X(b["t"]), Y(v), S(rad + 2.5)
        dr.ellipse([x - r_, y - r_, x + r_, y + r_], fill=(255, 255, 255, 255))
        r_ = S(rad + 1.5)
        dr.ellipse([x - r_, y - r_, x + r_, y + r_], fill=hex_rgb(FINGER_COLOR))
        targets.append({"kind": "finger", "key": "f%d" % b["t"], "x": x / SS, "y": y / SS,
                        "label": "Finger-stick %s %s at %s" % (fmt_glucose(v, units), unit_label(units), when(b["t"]))})

    # treatments on the glucose line, amount written next to them (like xDrip+)
    clamp_y = lambda y: max(top + S(2), min(bottom - S(2), y))
    for t in treatments:
        x = X(min(t["t"], now_ms))
        lvl = _interp(allr, t["t"])
        y = clamp_y(Y((low + high) / 2) if lvl is None else Y(lvl))
        if t.get("insulin", 0) > 0:
            s = S(5)
            ty = max(top + 2 * s + S(fs), y - S(6))
            dr.polygon([(x - s, ty - 2 * s), (x + s, ty - 2 * s), (x, ty)], fill=hex_rgb(INSULIN_COLOR))
            dr.text((x, ty - 2 * s - S(3)), "%sU" % _units(t["insulin"]), font=f_small,
                    fill=hex_rgb(INSULIN_COLOR), anchor="mb")
            targets.append({"kind": "insulin", "key": "i%s" % t.get("id", t["t"]), "x": x / SS, "y": (ty - s) / SS,
                            "label": "Insulin %sU at %s" % (_units(t["insulin"]), when(t["t"]))})
        if t.get("carbs", 0) > 0:
            cy = min(bottom - S(5) - S(fs), y + S(10))
            r_ = S(4.5)
            dr.ellipse([x - r_, cy - r_, x + r_, cy + r_], fill=hex_rgb(CARBS_COLOR))
            dr.text((x, cy + r_ + S(2)), "%sg" % _units(t["carbs"]), font=f_small,
                    fill=hex_rgb(CARBS_COLOR), anchor="mt")
            targets.append({"kind": "carbs", "key": "c%s" % t.get("id", t["t"]), "x": x / SS, "y": cy / SS,
                            "label": "Carbs %sg at %s" % (_units(t["carbs"]), when(t["t"]))})

    # header
    head_y = S(fs * 1.0)
    if allr:
        last = allr[-1]
        v = mmol_of(last)
        head = "%s %s %s" % (fmt_glucose(v, units), unit_label(units), arrow_for(last))
        if len(allr) > 1 and last["t"] - allr[-2]["t"] < 16 * 60000:
            dlt = v - mmol_of(allr[-2])
            head += "  %s%s" % ("+" if dlt >= 0 else "−", fmt_glucose(abs(dlt), units))
        draw_text_lm(dr, left, head_y, head, S(fs), hex_rgb(colors[range_of(v, low, high)]))
        x2 = left + text_width(dr, head, S(fs)) + S(fs)
        dr.text((x2, head_y), "at %s" % fmt_time12(last["t"]), font=f_main, fill=(255, 255, 255, 153), anchor="lm")
    else:
        dr.text(((left + right) / 2, (top + bottom) / 2), "No readings in the last 48 hours yet",
                font=f_main, fill=(255, 255, 255, 153), anchor="mm")
    where = " to %s" % fmt_time12(t_end) if v0["offset_ms"] > 60000 else ""
    dr.text((S(w - pad_r), head_y), "%s%s · %s" % (_fmt_span(v0["span_hours"]), where, unit_label(units)),
            font=f_main, fill=(255, 255, 255, 115), anchor="rm")

    # the item that was clicked: a ring around it, and its details in a small popup next to it
    sel = next((t for t in targets if t["key"] == opts.get("selected")), None)
    if sel:
        sx, sy = S(sel["x"]), S(sel["y"])
        dr.ellipse([sx - S(7), sy - S(7), sx + S(7), sy + S(7)], outline=(255, 255, 255, 230), width=S(2))
        if opts.get("event_popup", True):
            tw = text_width(dr, sel["label"], S(fs))
            bw, bh = tw + S(14), S(fs) + S(12)
            bx, by = sx + S(12), sy - bh - S(10)
            if bx + bw > W - S(2):
                bx = sx - S(12) - bw
            if by < S(2):
                by = sy + S(12)
            bx = max(S(2), bx)
            dr.rectangle([bx, by, bx + bw, by + bh], fill=(13, 13, 15, 235), outline=(255, 255, 255, 90), width=SS)
            draw_text_lm(dr, bx + S(7), by + bh / 2, sel["label"], S(fs), (255, 255, 255, 242))
    if out is not None:
        out["targets"] = targets
        out["selected"] = sel
    return img.resize((w, h), Image.LANCZOS)
