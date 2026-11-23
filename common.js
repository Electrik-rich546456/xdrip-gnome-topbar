// Shared by the extension (GNOME Shell), the settings window and the separate history window.
// Only GLib / Gio / Cairo here: no St, no Gtk.

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

export const SCHEMA_ID = 'org.gnome.shell.extensions.xdrip-topbar';
export const HISTORY_HOURS = 48;

export const ARROWS = {
    DoubleUp: '⇈', SingleUp: '↑', FortyFiveUp: '↗', Flat: '→',
    FortyFiveDown: '↘', SingleDown: '↓', DoubleDown: '⇊', NotComputable: '?',
};

// Same thresholds as xDrip+ BgReading.getSlopeOrdinal(), used when a reading has no trend name.
export function trendFromSlope(slopePerMin) {
    if (slopePerMin === undefined || slopePerMin === null) return 'NotComputable';
    if (slopePerMin <= -3.5) return 'DoubleDown';
    if (slopePerMin <= -2) return 'SingleDown';
    if (slopePerMin <= -1) return 'FortyFiveDown';
    if (slopePerMin <= 1) return 'Flat';
    if (slopePerMin <= 2) return 'FortyFiveUp';
    if (slopePerMin <= 3.5) return 'SingleUp';
    return 'DoubleUp';
}

export function arrowFor(reading) {
    if (!reading) return '';
    return ARROWS[reading.trend] ?? ARROWS[trendFromSlope(reading.slope)];
}

export function mmolOf(reading) {
    if (!reading) return null;
    return reading.mgdl !== undefined ? reading.mgdl / 18.0182 : reading.mmol;
}

export const MGDL_PER_MMOL = 18.0182;

/** Glucose (given in mmol/L) as text in the chosen units: '6.4' or '115'. */
export function fmtGlucose(mmol, units) {
    return units === 'mgdl' ? String(Math.round(mmol * MGDL_PER_MMOL)) : mmol.toFixed(1);
}

export function unitLabel(units) {
    return units === 'mgdl' ? 'mg/dL' : 'mmol/L';
}

/** 'low' | 'in' | 'high' */
export function rangeOf(mmol, low, high) {
    if (mmol < low) return 'low';
    if (mmol > high) return 'high';
    return 'in';
}

export function dataDir(settings) {
    let d = settings.get_string('data-dir').trim();
    if (!d)
        return GLib.build_filenamev([GLib.get_user_data_dir(), 'xdrip-topbar']);
    if (d === '~' || d.startsWith('~/'))
        d = GLib.get_home_dir() + d.slice(1);
    return d;
}

/** Colours and limits the graph needs, read from settings. */
export function graphOptions(settings) {
    return {
        low: settings.get_double('low-mmol'),
        high: settings.get_double('high-mmol'),
        colors: {
            low: settings.get_string('value-color-low'),
            in: settings.get_string('value-color-in'),
            high: settings.get_string('value-color-high'),
        },
        line: settings.get_boolean('graph-show-line'),
        smooth: settings.get_boolean('graph-smooth'),
        lineWidth: settings.get_double('graph-line-width'),
        lineByRange: settings.get_string('graph-line-color-mode') === 'range',
        lineColor: settings.get_string('graph-line-color'),
        dots: settings.get_boolean('graph-show-dots'),
        gapMs: settings.get_int('graph-gap-minutes') * 60000,
        spanHours: settings.get_double('graph-hours'),
        units: settings.get_string('units'),
        eventPopup: settings.get_string('graph-event-info') !== 'status',
    };
}

/** Splits readings into runs with no gap longer than gapMs (the line is broken between runs). */
function runsOf(points, gapMs) {
    const runs = [];
    let cur = [];
    for (const p of points) {
        if (cur.length && p.t - cur[cur.length - 1].t > gapMs) {
            runs.push(cur);
            cur = [];
        }
        cur.push(p);
    }
    if (cur.length)
        runs.push(cur);
    return runs;
}

/**
 * Adds one run to the current path. Smooth = monotone cubic (Fritsch–Carlson): the curve never
 * overshoots the readings, so it can't show a dip or a peak that wasn't measured.
 */
function pathRun(cr, pts, smooth) {
    cr.moveTo(pts[0].x, pts[0].y);
    if (!smooth || pts.length < 3) {
        for (let i = 1; i < pts.length; i++)
            cr.lineTo(pts[i].x, pts[i].y);
        return;
    }
    const n = pts.length;
    const d = [];                                   // secant slopes
    for (let i = 0; i < n - 1; i++) {
        const h = pts[i + 1].x - pts[i].x;
        d.push(h > 0 ? (pts[i + 1].y - pts[i].y) / h : 0);
    }
    const m = new Array(n);                         // tangents
    m[0] = d[0];
    m[n - 1] = d[n - 2];
    for (let i = 1; i < n - 1; i++)
        m[i] = d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2;
    for (let i = 0; i < n - 1; i++) {
        if (d[i] === 0) {
            m[i] = m[i + 1] = 0;
            continue;
        }
        const a = m[i] / d[i], b = m[i + 1] / d[i], s = a * a + b * b;
        if (s > 9) {
            const t = 3 / Math.sqrt(s);
            m[i] = t * a * d[i];
            m[i + 1] = t * b * d[i];
        }
    }
    for (let i = 0; i < n - 1; i++) {
        const h = (pts[i + 1].x - pts[i].x) / 3;
        cr.curveTo(pts[i].x + h, pts[i].y + m[i] * h,
            pts[i + 1].x - h, pts[i + 1].y - m[i + 1] * h,
            pts[i + 1].x, pts[i + 1].y);
    }
}

export function readJsonAsync(path, callback) {
    const file = Gio.File.new_for_path(path);
    file.load_contents_async(null, (f, res) => {
        let doc = null;
        try {
            const [, bytes] = f.load_contents_finish(res);
            doc = JSON.parse(new TextDecoder().decode(bytes));
        } catch (e) {
            doc = null;
        }
        callback(doc);
    });
}

// ---------------------------------------------------------------- time, 12 h clock
function localTime(ms) {
    return GLib.DateTime.new_from_unix_local(Math.floor(ms / 1000));
}

export function fmtTime12(ms) {
    return localTime(ms).format('%-I:%M %p');
}

export function fmtMinutesAgo(ms, nowMs) {
    const m = Math.max(0, Math.floor((nowMs - ms) / 60000));
    if (m < 1) return 'now';
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`;
}

// ---------------------------------------------------------------- colours
export function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{6})/i.exec(hex || '');
    if (!m) return [1, 1, 1];
    const n = parseInt(m[1], 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

function setColor(cr, hex, alpha = 1) {
    const [r, g, b] = hexToRgb(hex);
    cr.setSourceRGBA(r, g, b, alpha);
}

// ---------------------------------------------------------------- the graph
const INSULIN_COLOR = '#4da3ff';
const CARBS_COLOR = '#ffa53d';
const FINGER_COLOR = '#ff4040';

function interpMmol(readings, t) {
    if (!readings.length) return null;
    let lo = 0, hi = readings.length - 1;
    if (t <= readings[0].t) return mmolOf(readings[0]);
    if (t >= readings[hi].t) return mmolOf(readings[hi]);
    while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (readings[mid].t <= t) lo = mid; else hi = mid;
    }
    const a = readings[lo], b = readings[hi];
    if (b.t - a.t > 30 * 60000) return null;            // in a gap: don't invent a level
    const f = (t - a.t) / (b.t - a.t);
    return mmolOf(a) + f * (mmolOf(b) - mmolOf(a));
}

function text(cr, str, x, y, align = 'left') {
    const ext = cr.textExtents(str);
    let dx = 0;
    if (align === 'center') dx = -ext.width / 2 - ext.xBearing;
    else if (align === 'right') dx = -ext.width - ext.xBearing;
    cr.moveTo(x + dx, y);
    cr.showText(str);
    cr.newPath();          // don't leave the text position in the next path
}

function fmtUnits(n) {
    return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

// ---------------------------------------------------------------- zoom / pan
export const MIN_SPAN_HOURS = 1;

function layout(w, h) {
    const fs = Math.max(9, Math.min(12, h / 22));
    const padL = Math.ceil(fs * 2.4), padR = 8, padT = Math.ceil(fs * 2.0), padB = Math.ceil(fs * 1.9);
    return {fs, padL, padR, padT, padB, pw: Math.max(10, w - padL - padR), ph: Math.max(10, h - padT - padB)};
}

function clampView(spanHours, offsetMs) {
    const span = Math.max(MIN_SPAN_HOURS, Math.min(HISTORY_HOURS, spanHours));
    const maxOffset = (HISTORY_HOURS - span) * 3600000;
    return {spanHours: span, offsetMs: Math.max(0, Math.min(maxOffset, offsetMs))};
}

/**
 * Mouse-wheel zoom around the pointer. view = {spanHours, offsetMs} (offset = how far the right
 * edge is before now). factor < 1 zooms in. Returns the new view.
 */
export function zoomView(view, w, h, mouseX, factor) {
    if (view.offsetMs < 1000)                                       // showing "now": stay live
        return clampView(view.spanHours * factor, 0);
    const L = layout(w, h);
    const fx = Math.max(0, Math.min(1, (mouseX - L.padL) / L.pw));
    const oldSpan = view.spanHours * 3600000;
    const tAtMouse = -view.offsetMs - oldSpan * (1 - fx);           // relative to now
    const span = clampView(view.spanHours * factor, 0).spanHours;
    const newEnd = tAtMouse + span * 3600000 * (1 - fx);
    return clampView(span, -newEnd);
}

/** Shift+wheel / sideways scroll: move by a fraction of the visible span (+ = further back). */
export function panView(view, fraction) {
    return clampView(view.spanHours, view.offsetMs + fraction * view.spanHours * 3600000);
}

/** Drag-to-scroll: dxPx pixels to the right shows earlier readings. */
export function panViewPixels(view, w, h, dxPx) {
    const L = layout(w, h);
    return clampView(view.spanHours, view.offsetMs + dxPx / L.pw * view.spanHours * 3600000);
}

/** '3:42 PM' for today, 'Sun 3:42 PM' for other days. */
export function fmtWhen(ms, nowMs = Date.now()) {
    const d = GLib.DateTime.new_from_unix_local(Math.floor(ms / 1000));
    const n = GLib.DateTime.new_from_unix_local(Math.floor(nowMs / 1000));
    const sameDay = d.get_year() === n.get_year() && d.get_day_of_year() === n.get_day_of_year();
    return d.format(sameDay ? '%-I:%M %p' : '%a %-I:%M %p');
}

/** The item under (x, y) in a graph drawn by drawGraph(), or null. */
export function hitTest(targets, x, y) {
    let best = null, bestD = Infinity;
    for (const t of targets ?? []) {
        const d = Math.hypot(t.x - x, t.y - y) - (t.kind === 'reading' ? 0 : 6);   // markers win over the line
        if (d < (t.kind === 'reading' ? 14 : 16) && d < bestD) {
            best = t;
            bestD = d;
        }
    }
    return best;
}

function fmtSpan(hours) {
    return hours >= 1.95 ? `${Math.round(hours)} h` : `${Math.round(hours * 60)} min`;
}

/**
 * Draws the history in the style of the xDrip+ main graph: line and/or dots coloured by range,
 * shaded target band, insulin (blue ▼) and carbs (orange ●) markers.
 *   history: parsed history.json (or null)    opts: graphOptions(settings)
 *   view: {spanHours, offsetMs} (default: the whole 48 h ending now)
 */
export function drawGraph(cr, w, h, history, opts, nowMs = Date.now(), view = null) {
    const v0 = clampView(view?.spanHours ?? opts.spanHours ?? HISTORY_HOURS, view?.offsetMs ?? 0);
    const span = v0.spanHours * 3600000;
    const tEnd = nowMs - v0.offsetMs;
    const t0 = tEnd - span;
    const all = (history?.readings ?? []).filter(r => r.t >= nowMs - HISTORY_HOURS * 3600000 && r.t <= nowMs + 600000);
    // keep one reading beyond each edge so the line runs right up to the edge
    let iFirst = all.findIndex(r => r.t >= t0);
    if (iFirst < 0) iFirst = all.length;
    let iLast = all.length - 1;
    while (iLast >= 0 && all[iLast].t > tEnd) iLast--;
    const readings = all.slice(Math.max(0, iFirst - 1), Math.min(all.length, iLast + 2));
    const visible = all.slice(iFirst, iLast + 1);
    const treatments = (history?.treatments ?? []).filter(t => t.t >= t0 && t.t <= tEnd + (v0.offsetMs ? 0 : 3600000));
    const finger = (history?.bloodtests ?? []).filter(b => b.t >= t0 && b.t <= tEnd);

    // background
    cr.setSourceRGBA(0.11, 0.11, 0.12, 1);
    cr.rectangle(0, 0, w, h);
    cr.fill();

    const {fs, padL, padR, padT, pw, ph} = layout(w, h);
    cr.selectFontFace('Sans', 0 /* normal */, 0 /* normal */);
    cr.setFontSize(fs);

    // vertical scale: at least 2-15 mmol/L (36-270 mg/dL), grows to fit what is on screen, top rounded
    // to a label. Internally everything stays in mmol/L; only the labels use the chosen units.
    const mgdl = opts.units === 'mgdl';
    const k = mgdl ? MGDL_PER_MMOL : 1;
    let lo = 2, hi = 15;
    for (const r of visible) {
        const v = mmolOf(r);
        lo = Math.min(lo, Math.floor(v));
        hi = Math.max(hi, v + 1);
    }
    const loD = mgdl ? Math.floor(lo * k / 10) * 10 : lo;
    const yStep = mgdl ? (hi * k - loD > 360 ? 100 : 50) : (hi - lo > 20 ? 5 : 3);   // in display units
    const vMin = loD / k, vMax = Math.ceil(hi * k / yStep) * yStep / k;
    const X = t => padL + (t - t0) / span * pw;
    const Y = v => padT + ph - (v - vMin) / (vMax - vMin) * ph;
    const clampY = y => Math.max(padT + 2, Math.min(padT + ph - 2, y));

    // target band and limit lines
    cr.setSourceRGBA(0.45, 0.85, 0.45, 0.09);
    cr.rectangle(padL, Y(opts.high), pw, Y(opts.low) - Y(opts.high));
    cr.fill();
    cr.setLineWidth(1);
    cr.setDash([4, 3], 0);
    for (const [v, c] of [[opts.low, opts.colors.low], [opts.high, opts.colors.high]]) {
        setColor(cr, c, 0.55);
        cr.moveTo(padL, Math.round(Y(v)) + 0.5);
        cr.lineTo(padL + pw, Math.round(Y(v)) + 0.5);
        cr.stroke();
    }
    cr.setDash([], 0);

    // y grid + labels
    cr.setSourceRGBA(1, 1, 1, 0.55);
    for (let vD = Math.ceil(vMin * k / yStep - 1e-9) * yStep; vD <= vMax * k + 1e-9; vD += yStep) {
        const v = vD / k;
        const y = Math.round(Y(v)) + 0.5;
        cr.setSourceRGBA(1, 1, 1, 0.07);
        cr.moveTo(padL, y);
        cr.lineTo(padL + pw, y);
        cr.stroke();
        cr.setSourceRGBA(1, 1, 1, 0.55);
        text(cr, String(Math.round(vD)), padL - 5, y + fs * 0.35, 'right');
    }

    // x grid: local-time marks on a 12 h clock, day name at midnight
    const pxPerMin = pw / (span / 60000);
    const stepMin = [15, 30, 60, 120, 180, 360, 720, 1440].find(m => m * pxPerMin >= fs * 5.5) ?? 1440;
    let dt = GLib.DateTime.new_from_unix_local(Math.floor(t0 / 1000));
    dt = dt.add_seconds(-dt.get_seconds()).add_minutes(-(dt.get_minute() % 15));
    const minuteOfDay = d => d.get_hour() * 60 + d.get_minute();
    while (minuteOfDay(dt) % stepMin !== 0 || dt.to_unix() * 1000 < t0)
        dt = dt.add_minutes(15);
    for (; dt.to_unix() * 1000 <= tEnd; dt = dt.add_minutes(stepMin)) {
        const x = Math.round(X(dt.to_unix() * 1000)) + 0.5;
        const midnight = minuteOfDay(dt) === 0;
        cr.setSourceRGBA(1, 1, 1, midnight ? 0.18 : 0.07);
        cr.moveTo(x, padT);
        cr.lineTo(x, padT + ph);
        cr.stroke();
        cr.setSourceRGBA(1, 1, 1, midnight ? 0.8 : 0.55);
        const label = midnight ? dt.format('%a') : dt.format(stepMin < 60 ? '%-I:%M %p' : '%-I %p');
        if (x - padL > fs * 1.2 && padL + pw - x > fs * 1.2)
            text(cr, label, x, padT + ph + fs * 1.3, 'center');
    }

    // readings: line (coloured by range using clip bands, so the colour changes exactly at the
    // low / high lines) and/or dots
    const pxPerReading = pw / (span / 300000);                  // readings are ~5 min apart
    const rad = Math.max(1.3, Math.min(3.5, pxPerReading * 0.35));
    const showLine = opts.line ?? true;
    const targets = [];
    const units = opts.units;
    const when = ms => fmtWhen(ms, nowMs);
    for (const r of visible) {
        const v = mmolOf(r);
        targets.push({kind: 'reading', key: `r${r.t}`, x: X(r.t), y: Y(v),
            label: `${fmtGlucose(v, units)} ${unitLabel(units)} ${arrowFor(r)} at ${when(r.t)}`});
    }
    const runs = runsOf(readings, opts.gapMs ?? 15 * 60000)
        .map(run => run.map(r => ({x: X(r.t), y: Y(mmolOf(r)), v: mmolOf(r)})));
    if (showLine) {
        const lw = opts.lineWidth ?? 2;
        const bands = opts.lineByRange ?? true
            ? [[padT - 20, Y(opts.high), opts.colors.high], [Y(opts.high), Y(opts.low), opts.colors.in],
                [Y(opts.low), padT + ph + 20, opts.colors.low]]
            : [[padT - 20, padT + ph + 20, opts.lineColor ?? '#e0e0e0']];
        cr.setLineWidth(lw);
        cr.setLineCap(1 /* round */);
        cr.setLineJoin(1 /* round */);
        for (const [top, bottom, color] of bands) {
            cr.save();
            cr.rectangle(padL, top, pw, bottom - top);
            cr.clip();
            setColor(cr, color);
            for (const run of runs) {
                if (run.length === 1) {                    // a lone reading: show it as a dot
                    cr.arc(run[0].x, run[0].y, Math.max(rad, lw), 0, 2 * Math.PI);
                    cr.fill();
                } else {
                    pathRun(cr, run, opts.smooth ?? true);
                    cr.stroke();
                }
            }
            cr.restore();
        }
    }
    if (opts.dots || !showLine) {
        const dotRad = showLine ? rad * 0.8 : rad;
        cr.save();
        cr.rectangle(padL - dotRad, padT - 20, pw + 2 * dotRad, ph + 40);
        cr.clip();
        for (const run of runs) {
            for (const p of run) {
                setColor(cr, opts.colors[rangeOf(p.v, opts.low, opts.high)]);
                cr.arc(p.x, p.y, dotRad, 0, 2 * Math.PI);
                cr.fill();
            }
        }
        cr.restore();
    }

    // finger-sticks
    for (const b of finger) {
        const v = b.mgdl !== undefined ? b.mgdl / 18.0182 : b.mmol;
        cr.setSourceRGBA(1, 1, 1, 1);
        cr.arc(X(b.t), Y(v), rad + 2.5, 0, 2 * Math.PI);
        cr.fill();
        setColor(cr, FINGER_COLOR);
        cr.arc(X(b.t), Y(v), rad + 1.5, 0, 2 * Math.PI);
        cr.fill();
        targets.push({kind: 'finger', key: `f${b.t}`, x: X(b.t), y: Y(v),
            label: `Finger-stick ${fmtGlucose(v, units)} ${unitLabel(units)} at ${when(b.t)}`});
    }

    // treatments: like xDrip+, placed on the glucose line with the amount written next to it
    cr.setFontSize(fs * 0.9);
    for (const t of treatments) {
        const x = X(Math.min(t.t, nowMs));
        const level = interpMmol(readings, t.t);
        const y = clampY(level === null ? Y((opts.low + opts.high) / 2) : Y(level));
        if (t.insulin > 0) {
            const s = 5;
            const ty = Math.max(padT + 2 * s + fs, y - 6);
            setColor(cr, INSULIN_COLOR);
            cr.moveTo(x - s, ty - 2 * s);
            cr.lineTo(x + s, ty - 2 * s);
            cr.lineTo(x, ty);
            cr.closePath();
            cr.fill();
            text(cr, `${fmtUnits(t.insulin)}U`, x, ty - 2 * s - 3, 'center');
            targets.push({kind: 'insulin', key: `i${t.id ?? t.t}`, x, y: ty - s,
                label: `Insulin ${fmtUnits(t.insulin)}U at ${when(t.t)}`});
        }
        if (t.carbs > 0) {
            const cy = Math.min(padT + ph - 5 - fs, y + 10);
            setColor(cr, CARBS_COLOR);
            cr.arc(x, cy, 4.5, 0, 2 * Math.PI);
            cr.fill();
            text(cr, `${fmtUnits(t.carbs)}g`, x, cy + 5 + fs, 'center');
            targets.push({kind: 'carbs', key: `c${t.id ?? t.t}`, x, y: cy,
                label: `Carbs ${fmtUnits(t.carbs)}g at ${when(t.t)}`});
        }
    }

    // header: latest value, delta, time
    cr.setFontSize(fs * 1.05);
    const last = all[all.length - 1];
    if (last) {
        const v = mmolOf(last);
        const prev = all[all.length - 2];
        let head = `${fmtGlucose(v, opts.units)} ${unitLabel(opts.units)} ${arrowFor(last)}`;
        if (prev && last.t - prev.t < 16 * 60000) {
            const d = v - mmolOf(prev);
            head += `  ${d >= 0 ? '+' : '−'}${fmtGlucose(Math.abs(d), opts.units)}`;
        }
        setColor(cr, opts.colors[rangeOf(v, opts.low, opts.high)]);
        text(cr, head, padL, fs * 1.35);
        cr.setSourceRGBA(1, 1, 1, 0.6);
        text(cr, `at ${fmtTime12(last.t)}`, padL + cr.textExtents(head).xAdvance + fs, fs * 1.35);
    } else {
        cr.setSourceRGBA(1, 1, 1, 0.6);
        text(cr, 'No readings in the last 48 hours yet', padL + pw / 2, padT + ph / 2, 'center');
    }
    cr.setSourceRGBA(1, 1, 1, 0.45);
    const where = v0.offsetMs > 60000 ? ` to ${fmtTime12(tEnd)}` : '';
    text(cr, `${fmtSpan(v0.spanHours)}${where} · ${unitLabel(opts.units)}`, w - padR, fs * 1.35, 'right');

    // the item that was clicked: a ring around it, and its details in a small popup next to it
    const sel = opts.selected ? targets.find(t => t.key === opts.selected) : null;
    if (sel) {
        cr.setLineWidth(2);
        cr.setSourceRGBA(1, 1, 1, 0.9);
        cr.arc(sel.x, sel.y, 7, 0, 2 * Math.PI);
        cr.stroke();
        if (opts.eventPopup ?? true) {
            cr.setFontSize(fs);
            const ext = cr.textExtents(sel.label);
            const bw = ext.xAdvance + 14, bh = fs + 12;
            let bx = sel.x + 12, by = sel.y - bh - 10;
            if (bx + bw > w - 2)
                bx = sel.x - 12 - bw;
            if (by < 2)
                by = sel.y + 12;
            bx = Math.max(2, bx);
            cr.setSourceRGBA(0.05, 0.05, 0.06, 0.92);
            cr.rectangle(bx, by, bw, bh);
            cr.fill();
            cr.setSourceRGBA(1, 1, 1, 0.35);
            cr.setLineWidth(1);
            cr.rectangle(bx + 0.5, by + 0.5, bw - 1, bh - 1);
            cr.stroke();
            cr.setSourceRGBA(1, 1, 1, 0.95);
            text(cr, sel.label, bx + 7, by + bh / 2 + fs * 0.35);
        }
    }
    return {targets, selected: sel};
}

// ---------------------------------------------------------------- xDrip+ settings QR code
/**
 * Pulls the sync key out of the text of xDrip+'s "Show Settings QR codes" › "xDrip+ security key
 * settings only" code: 'xdpref:' + base64 of zlib-compressed JSON, or the binary 'xdp2:' variant.
 * Returns the key, or null if the text is not a QR export (then it is treated as the key itself).
 * Throws if it is a QR export without a sync key in it.
 */
export function syncKeyFromQrText(text) {
    text = (text || '').trim();
    let body, format;
    if (text.startsWith('xdpref:')) {
        body = text.slice(7);
        format = Gio.ZlibCompressorFormat.ZLIB;
    } else if (text.startsWith('xdp2:')) {
        body = text.slice(5);
        format = Gio.ZlibCompressorFormat.GZIP;
    } else {
        return null;
    }
    body = body.replace(/-/g, '+').replace(/_/g, '/').replace(/\s/g, '');
    body += '='.repeat((4 - body.length % 4) % 4);
    const packed = GLib.base64_decode(body);
    const input = new Gio.ConverterInputStream({
        base_stream: Gio.MemoryInputStream.new_from_bytes(new GLib.Bytes(packed)),
        converter: new Gio.ZlibDecompressor({format}),
    });
    const out = Gio.MemoryOutputStream.new_resizable();
    out.splice(input, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET, null);
    const data = out.steal_as_bytes().toArray();

    let prefs = {};
    if (format === Gio.ZlibCompressorFormat.ZLIB) {
        prefs = JSON.parse(new TextDecoder().decode(data));
    } else {                     // int16 count, then [int16 keylen, key, int16 vallen, value]*, big-endian
        const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
        let i = 2;
        for (let n = dv.getInt16(0); n > 0; n--) {
            const kl = dv.getInt16(i); i += 2;
            const k = new TextDecoder().decode(data.subarray(i, i + kl)); i += kl;
            const vl = dv.getInt16(i); i += 2;
            prefs[k] = new TextDecoder().decode(data.subarray(i, i + vl)); i += vl;
        }
    }
    const key = prefs.custom_sync_key;
    if (!key)
        throw new Error('this QR code has no sync key in it — use “xDrip+ security key settings only”');
    return String(key).trim();
}
