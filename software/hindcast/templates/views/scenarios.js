/* views/scenarios.js (H-404 Scenario comparison): small multiples on one shared time axis for up to three entities
   (an entity is a variant on one day) plus the phone, or one variant across days (one column per day).

   Reads data.scenarios (report.scenario_index): days[{day, role, tags, confounders, orig}], entities[{vhash,
   variant, label, base, params, position, verdict, caps{autoISF_max, autoISF_min}, series{day: path}}],
   phone{caps}, defaults[{vhash, day}], slots (3), smb_tol_U, iob_tolerance, units; data.page.units for glucose.
   Per-loop numbers come from the tier-2 documents through HC.loadTier2 (series/<day>/<vhash>.json and
   series/<day>/orig.json); when they cannot be loaded the figure says why (the 'not available from disk' text).
   Panels, top to bottom, one ECharts grid each, one x axis per grid linked by axisPointer.link and one dataZoom:
   recorded glucose (target band, carb markers, target / profile hairlines, recorded lows shaded as positions),
   final ISF factor with the cap hairline, insulinReq, SMB stems, the SMB difference strip per entity (entity minus
   the phone, diverging, grey inside the per-loop tolerance), temp basal rate in force, recorded IOB with each
   entity's iobTH and its tolerance. Never a dual axis; never more than three variant colours.
   Styles are scoped under #view-scenarios and injected once; every colour is a page.css token.

   Hooks for later views (H-405 drill-down, H-403 leaderboard, H-409 live rows):
     HC.scenarios.add(vhash, day)       put an entity into the first free slot; refused (with replace buttons) when
                                        three are shown; returns true when it was added
     HC.scenarios.replace(slot, vhash, day), HC.scenarios.remove(slot)   slot = 1..3
     HC.scenarios.setMode('compare' | 'days', vhash)                       'days' = one variant, a column per day
     HC.scenarios.onLoop(cb)            cb({day, t, index, mode, entities[{vhash, day, slot}]}) when a loop is
                                        clicked in a chart or Enter is pressed on a focused chart
     HC.scenarios.loopAt(chart, model, px, py)  the loop index under a click (-1 outside every panel)
     Table twin rows carry data-day, data-t (s since 00:00 UTC), data-index and, when one variant owns the row (the
     day's lowest slot, or the day variant in 'days' mode), data-vhash and data-slot (0 = no slot).
     HC.scenarios.focusLoop(day, t)     move the crosshair to the loop nearest t (s since 00:00 UTC) of that day
     HC.scenarios.refresh()             redraw after data.scenarios changed
     HC.scenarios.state                 {mode, slots[3], dayVariant}
   A leaderboard row of the Parameters view (HC.parameters.onSelect) adds that variant here and opens this tab. */
(function () {
  'use strict';
  var HC = window.HC;
  if (!HC || !HC.registerView) return;
  var h = HC.h, fmt = HC.fmt;
  var TAGS = ['A', 'B', 'C'];
  var LOW_MGDL = 70.2;                                     // 3.9 mmol/l, the positional "a low followed" marker
  var DAY_GROUP = 'hc-scenarios-days';
  var UNSLOTTED = 'variant';                               // tag of a days-mode variant that holds no slot (drawn in ink)
  function slotColor(t, slot) { return slot ? t['series-' + slot] : t.ink; }
  function slotClass(slot) { return slot ? 's' + slot : 'sx'; }

  var CSS = [
    '#view-scenarios .sc-controls { display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: end; }',
    '#view-scenarios .sc-controls label { display: grid; gap: 2px; font-size: 0.8rem; color: var(--ink-2); min-width: 0; max-width: 100%; }',
    '#view-scenarios select { font: inherit; font-size: 0.88rem; padding: 4px 8px; border: 1px solid var(--axis); border-radius: var(--radius); background: var(--surface); color: var(--ink); max-width: 100%; }',
    '#view-scenarios fieldset.sc-mode { border: 0; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 0; min-width: 0; }',
    '#view-scenarios fieldset.sc-mode legend { font-size: 0.8rem; color: var(--ink-2); padding: 0; margin-bottom: 2px; }',
    '#view-scenarios fieldset.sc-mode label { display: inline-flex; align-items: center; gap: 6px; font-size: 0.86rem; color: var(--ink); padding: 4px 10px; border: 1px solid var(--axis); background: var(--surface); cursor: pointer; }',
    '#view-scenarios fieldset.sc-mode label:first-of-type { border-radius: var(--radius) 0 0 var(--radius); }',
    '#view-scenarios fieldset.sc-mode label:last-of-type { border-radius: 0 var(--radius) var(--radius) 0; border-left: 0; }',
    '#view-scenarios fieldset.sc-mode label.on { background: var(--surface-2); border-color: var(--ink-2); font-weight: 600; }',
    '#view-scenarios fieldset.sc-mode input { margin: 0; accent-color: var(--series-1); }',
    '#view-scenarios button.sc-btn { font: inherit; font-size: 0.86rem; padding: 4px 12px; border-radius: var(--radius); border: 1px solid var(--ink-2); background: var(--surface); color: var(--ink); cursor: pointer; }',
    '#view-scenarios button.sc-btn:hover { background: var(--surface-2); }',
    '#view-scenarios button.sc-x { font: inherit; font-size: 0.8rem; border: 0; background: none; color: var(--ink-2); cursor: pointer; padding: 2px 4px; text-decoration: underline; }',
    '#view-scenarios .sc-slots { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr)); gap: 8px; }',
    '#view-scenarios .sc-slot { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 2px 10px; align-items: center; background: var(--surface); border: 1px solid var(--grid); border-radius: var(--radius); padding: 6px 10px; min-width: 0; }',
    '#view-scenarios .sc-slot.empty { border-style: dashed; color: var(--muted); grid-template-columns: auto minmax(0, 1fr); }',
    '#view-scenarios .sc-slot .who { min-width: 0; display: grid; gap: 1px; }',
    '#view-scenarios .sc-slot .who b { font-weight: 600; overflow-wrap: anywhere; }',
    '#view-scenarios .sc-slot .who span { font-size: 0.8rem; color: var(--ink-2); overflow-wrap: anywhere; }',
    '#view-scenarios .tag { display: inline-grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; font-size: 0.78rem; font-weight: 700; color: var(--surface); background: var(--muted); }',
    '#view-scenarios .tag.s1 { background: var(--series-1); }',
    '#view-scenarios .tag.s2 { background: var(--series-2); }',
    '#view-scenarios .tag.s3 { background: var(--series-3); }',
    '#view-scenarios .tag.free { background: none; color: var(--muted); box-shadow: inset 0 0 0 1px var(--axis); }',
    '#view-scenarios .sc-refusal { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; font-size: 0.88rem; color: var(--ink); background: var(--surface-2); border: 1px solid var(--axis); border-radius: var(--radius); padding: 8px 12px; }',
    '#view-scenarios .sc-legend { z-index: 2; background: var(--surface); display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.8rem; color: var(--ink-2); padding: 4px 0 6px; border-bottom: 1px solid var(--grid); }',
    '#view-scenarios .sc-legend span { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; min-width: 0; overflow-wrap: anywhere; }',
    '#view-scenarios .sw { display: inline-block; flex: 0 0 auto; width: 16px; height: 2px; background: var(--phone); }',
    '#view-scenarios .sw.s1 { background: var(--series-1); }',
    '#view-scenarios .sw.s2 { background: var(--series-2); }',
    '#view-scenarios .sw.s3 { background: var(--series-3); }',
    '#view-scenarios .sw.sx { background: var(--ink); }',
    '#view-scenarios .sw.bg { background: var(--ink-2); }',
    '#view-scenarios .sw.band { height: 10px; border-radius: 2px; background: var(--grid); box-shadow: inset 0 0 0 1px var(--axis); }',
    '#view-scenarios .sw.low { height: 10px; border-radius: 2px; background: var(--muted); opacity: 0.35; }',
    '#view-scenarios .sw.hair { width: 1px; height: 12px; background: var(--muted); }',
    '#view-scenarios .sw.cap { height: 1px; background: var(--muted); }',
    '#view-scenarios .sw.carb { width: 0; height: 0; background: none; border-left: 5px solid transparent; border-right: 5px solid transparent; border-bottom: 9px solid var(--ink); }',
    '#view-scenarios .sw.neg { width: 8px; height: 10px; border-radius: 1px; background: var(--div-neg); }',
    '#view-scenarios .sw.pos { width: 8px; height: 10px; border-radius: 1px; background: var(--div-pos); }',
    '#view-scenarios .sw.mid { width: 8px; height: 10px; border-radius: 1px; background: var(--muted); }',
    '#view-scenarios .sc-days { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr)); gap: 12px; }',
    '#view-scenarios .sc-days.one { grid-template-columns: minmax(0, 1fr); }',
    '#view-scenarios figure.chart .plot { height: auto; }',
    '#view-scenarios figure.chart .plot:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }',
    '#view-scenarios .sc-badges { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; font-size: 0.82rem; color: var(--ink-2); }',
    '#view-scenarios .sc-badge-day { display: inline-flex; flex-wrap: wrap; gap: 4px; align-items: center; min-width: 0; max-width: 100%; }',
    '#view-scenarios .sc-note { font-size: 0.84rem; color: var(--ink-2); max-width: 90ch; }',
    '#view-scenarios .sc-msg { font-size: 0.86rem; color: var(--ink-2); padding: 6px 0; overflow-wrap: anywhere; }',
    '#view-scenarios .sc-tip { font-size: 12px; line-height: 1.35; color: var(--ink); font-variant-numeric: tabular-nums; }',
    '#view-scenarios .sc-tip table { border-collapse: collapse; margin-top: 4px; }',
    '#view-scenarios .sc-tip th, #view-scenarios .sc-tip td { padding: 1px 6px 1px 0; text-align: right; white-space: nowrap; }',
    '#view-scenarios .sc-tip th { color: var(--ink-2); font-weight: 600; }',
    '#view-scenarios .sc-tip td.k { text-align: left; }',
    '#view-scenarios .sc-tip i { display: inline-block; width: 10px; height: 3px; margin-right: 4px; vertical-align: 2px; }',
    '@media (min-width: 700px) { #view-scenarios .sc-legend { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 41px); } }',
    '@media (max-width: 480px) { #view-scenarios .sc-controls label, #view-scenarios .sc-controls select { width: 100%; } }'
  ].join('\n');

  function injectCss() {
    if (document.getElementById('hc-view-scenarios-css')) return;
    var s = document.createElement('style');
    s.id = 'hc-view-scenarios-css';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  // ------------------------------------------------------------------------------------------------ state
  var state = { mode: 'compare', slots: [null, null, null], dayVariant: null };
  var loopListeners = [];
  var pending = null;                                      // a refused entity waiting for "replace slot n"
  var ui = null;                                           // the rendered view's elements, null before render
  var drawSeq = 0;

  function sc() { return (HC.data && HC.data.scenarios) || { days: [], entities: [] }; }
  function units() { return ((HC.data && HC.data.page) || {}).units === 'mmol' ? 'mmol' : 'mg/dl'; }
  function num(v) { return typeof v === 'number' && v === v; }
  function entityOf(vhash) {
    var es = sc().entities || [];
    for (var i = 0; i < es.length; i++) if (es[i].vhash === vhash) return es[i];
    return null;
  }
  function dayOf(day) {
    var ds = sc().days || [];
    for (var i = 0; i < ds.length; i++) if (ds[i].day === day) return ds[i];
    return null;
  }
  function seriesDays(vhash) { var e = entityOf(vhash); return e ? Object.keys(e.series || {}).sort() : []; }
  function slotOf(vhash, day) {
    for (var i = 0; i < state.slots.length; i++) {
      var s = state.slots[i];
      if (s && s.vhash === vhash && (day === undefined || s.day === day)) return i + 1;
    }
    return 0;
  }
  function filled() { return state.slots.filter(Boolean).length; }
  function shortDay(d) { return d ? d.slice(5) : ''; }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function hhmm(ms) {
    var s = Math.round(ms / 1000), plus = s >= 86400 ? ' +1 d' : '';
    s = ((s % 86400) + 86400) % 86400;
    var hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60);
    return (hh < 10 ? '0' : '') + hh + ':' + (mm < 10 ? '0' : '') + mm + plus;
  }
  function bgv(mgdl) { return num(mgdl) ? (units() === 'mmol' ? Math.round(mgdl / 18.0 * 10) / 10 : Math.round(mgdl)) : null; }
  function bgUnit() { return units() === 'mmol' ? 'mmol/l' : 'mg/dl'; }
  function nearest(ts, t) {                                 // index of the loop nearest t (ts ascending)
    if (!ts || !ts.length) return -1;
    var lo = 0, hi = ts.length - 1;
    while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (ts[mid] < t) lo = mid; else hi = mid; }
    return Math.abs(ts[lo] - t) <= Math.abs(ts[hi] - t) ? lo : hi;
  }
  function at(doc, key, i) { var a = doc && doc[key]; return a && i >= 0 && i < a.length ? a[i] : null; }

  // ------------------------------------------------------------------------------------------------ slot actions
  function validEntity(vhash, day) {
    var e = entityOf(vhash);
    return e && (e.series || {})[day] && (dayOf(day) || {}).orig ? e : null;
  }

  function add(vhash, day) {
    if (!validEntity(vhash, day)) { say('no per-loop data for that variant on ' + day); return false; }
    var have = slotOf(vhash, day);
    if (have) { pending = null; say('already shown as ' + TAGS[have - 1]); return false; }
    var free = state.slots.indexOf(null);
    if (free < 0) {                                        // the fourth entity: refused, the viewer picks a slot to replace
      pending = { vhash: vhash, day: day };
      redrawControls();
      return false;
    }
    pending = null;
    state.slots[free] = { vhash: vhash, day: day };
    state.mode = 'compare';
    redraw();
    return true;
  }
  function replace(slot, vhash, day) {
    if (slot < 1 || slot > state.slots.length || !validEntity(vhash, day)) return false;
    var have = slotOf(vhash, day);
    if (have && have !== slot) state.slots[have - 1] = null;
    state.slots[slot - 1] = { vhash: vhash, day: day };
    pending = null;
    state.mode = 'compare';
    redraw();
    return true;
  }
  function remove(slot) {
    if (slot < 1 || slot > state.slots.length) return;
    state.slots[slot - 1] = null;                          // the others keep their slot and colour
    pending = null;
    redraw();
  }
  function setMode(mode, vhash) {
    state.mode = mode === 'days' ? 'days' : 'compare';
    if (vhash && entityOf(vhash)) state.dayVariant = vhash;
    pending = null;
    redraw();
  }
  function say(text) { if (ui && ui.msg) { ui.msg.textContent = text || ''; ui.msg.hidden = !text; } }

  function initState() {
    var s = sc();
    state.slots = [null, null, null];
    (s.defaults || []).slice(0, 3).forEach(function (d, i) { if (validEntity(d.vhash, d.day)) state.slots[i] = { vhash: d.vhash, day: d.day }; });
    var first = state.slots.filter(Boolean)[0];
    state.dayVariant = first ? first.vhash : ((s.entities || [])[0] || {}).vhash || null;
  }

  // ------------------------------------------------------------------------------------------------ controls
  function option(value, text, selected) { return h('option', { value: value, selected: selected ? true : null, text: text }); }

  function entityText(e) { return (e.base ? '' : (e.position ? '#' + e.position + ' ' : '')) + e.label; }

  function renderControls(el) {
    var s = sc();
    var ents = s.entities || [];
    var bar = h('div', { class: 'sc-controls' });
    var mode = h('fieldset', { class: 'sc-mode' }, h('legend', { text: 'Show' }));
    [['compare', 'Up to three variants'], ['days', 'One variant, a column per day']].forEach(function (m) {
      var input = h('input', { type: 'radio', name: 'sc-mode', id: 'sc-mode-' + m[0], value: m[0], checked: state.mode === m[0] ? true : null,
        onchange: function () { setMode(m[0]); } });
      mode.appendChild(h('label', { class: state.mode === m[0] ? 'on' : null, for: 'sc-mode-' + m[0] }, input, m[1]));
    });
    bar.appendChild(mode);
    if (state.mode === 'compare') {
      var vsel = h('select', { id: 'sc-variant' });
      ents.forEach(function (e) { if (seriesDays(e.vhash).length) vsel.appendChild(option(e.vhash, entityText(e))); });
      var dsel = h('select', { id: 'sc-day' });
      function fillDays() {
        HC.clear(dsel);
        seriesDays(vsel.value).forEach(function (d) { var dd = dayOf(d) || {}; if (dd.orig) dsel.appendChild(option(d, d + (dd.role && dd.role !== 'search' ? ' (' + roleText(dd.role) + ')' : ''))); });
      }
      vsel.addEventListener('change', fillDays);
      fillDays();
      bar.appendChild(h('label', { for: 'sc-variant' }, 'Variant', vsel));
      bar.appendChild(h('label', { for: 'sc-day' }, 'Day', dsel));
      bar.appendChild(h('button', { type: 'button', class: 'sc-btn', id: 'sc-add', onclick: function () { if (vsel.value && dsel.value) add(vsel.value, dsel.value); } }, 'Add to the chart'));
    } else {
      var one = h('select', { id: 'sc-dvariant', onchange: function () { state.dayVariant = one.value; redraw(); } });
      ents.forEach(function (e) { if (seriesDays(e.vhash).length) one.appendChild(option(e.vhash, entityText(e), e.vhash === state.dayVariant)); });
      bar.appendChild(h('label', { for: 'sc-dvariant' }, 'Variant', one));
    }
    el.appendChild(bar);
    if (state.mode === 'compare') {
      var slots = h('div', { class: 'sc-slots', role: 'list', 'aria-label': 'shown variants' });
      state.slots.forEach(function (sl, i) {
        if (!sl) {
          slots.appendChild(h('div', { class: 'sc-slot empty', role: 'listitem' }, h('span', { class: 'tag free', text: TAGS[i] }),
            h('span', { text: 'free slot: pick a variant and a day above' })));
          return;
        }
        var e = entityOf(sl.vhash) || {};
        slots.appendChild(h('div', { class: 'sc-slot', role: 'listitem' }, h('span', { class: 'tag s' + (i + 1), text: TAGS[i] }),
          h('div', { class: 'who' }, h('b', { text: e.label || sl.vhash }), h('span', { text: sl.day + (e.base ? '' : ' · ' + (e.variant || '')) })),
          h('button', { type: 'button', class: 'sc-x', 'aria-label': 'remove ' + TAGS[i], onclick: function () { remove(i + 1); } }, 'Remove')));
      });
      el.appendChild(slots);
      if (pending) {
        var pe = entityOf(pending.vhash) || {};
        var box = h('div', { class: 'sc-refusal', role: 'status' },
          h('span', { text: 'Three is the most this chart compares, one colour each. To show ' + (pe.label || '') + ' on ' + pending.day + ', remove one or replace:' }));
        state.slots.forEach(function (sl, i) {
          box.appendChild(h('button', { type: 'button', class: 'sc-btn', onclick: function () { replace(i + 1, pending.vhash, pending.day); } }, 'Replace ' + TAGS[i]));
        });
        box.appendChild(h('button', { type: 'button', class: 'sc-x', onclick: function () { pending = null; redrawControls(); } }, 'Keep these three'));
        el.appendChild(box);
      }
    }
    ui.msg = h('p', { class: 'sc-msg', role: 'status', hidden: true });
    el.appendChild(ui.msg);
  }
  function roleText(r) { return r === 'holdout' ? 'hold-out' : r; }

  function redrawControls() {
    if (!ui) return;
    HC.clear(ui.controls);
    renderControls(ui.controls);
  }

  // ------------------------------------------------------------------------------------------------ data
  function load(path) { return HC.loadTier2(path).then(function (doc) { return { doc: doc }; }, function (e) { return { error: e && e.message ? e.message : String(e) }; }); }

  /* entities to draw: [{vhash, day, slot, tag, e, path}], for the current mode */
  function drawList() {
    if (state.mode === 'days') {
      var vh = state.dayVariant, e = entityOf(vh);
      if (!e) return [];
      var slot = slotOf(vh);                              // 0 when the variant holds no slot: drawn in ink, never in a slot's colour
      return seriesDays(vh).filter(function (d) { return (dayOf(d) || {}).orig; }).map(function (d) {
        return { vhash: vh, day: d, slot: slot, tag: slot ? TAGS[slot - 1] : UNSLOTTED, e: e, path: e.series[d] };
      });
    }
    var out = [];
    state.slots.forEach(function (sl, i) {
      if (!sl) return;
      var e = entityOf(sl.vhash);
      if (e) out.push({ vhash: sl.vhash, day: sl.day, slot: i + 1, tag: TAGS[i], e: e, path: e.series[sl.day] });
    });
    return out;
  }

  // ------------------------------------------------------------------------------------------------ option builder
  /* layout of the stacked grids (px): titles sit above each grid; the strips are one per entity */
  function layout(nStrips) {
    var parts = [
      { key: 'bg', h: 150 }, { key: 'factor', h: 84 }, { key: 'req', h: 84 }, { key: 'smb', h: 92 }
    ];
    for (var i = 0; i < nStrips; i++) parts.push({ key: 'strip', h: 30, strip: i });
    parts.push({ key: 'tbr', h: 72 }, { key: 'iob', h: 92 });
    var y = 6;
    parts.forEach(function (p) {
      var title = p.key === 'strip' ? 16 : 22;
      p.titleTop = y;
      p.top = y + title;
      y = p.top + p.h + (p.key === 'strip' ? 6 : 12);
    });
    return { parts: parts, height: y + 22 + 38 };      // x labels of the last grid + the slider
  }

  function lineSeries(name, g, data, color, opts) {
    opts = opts || {};
    return {
      name: name, type: 'line', xAxisIndex: g, yAxisIndex: g, data: data, showSymbol: false, symbol: 'circle', symbolSize: 8,
      step: opts.step || false, connectNulls: !!opts.connect, z: opts.z || 3, silent: !!opts.silent,
      lineStyle: { color: color, width: opts.width || 2, opacity: opts.opacity === undefined ? 1 : opts.opacity },
      itemStyle: { color: color },
      emphasis: { disabled: true },
      endLabel: opts.label ? { show: true, formatter: opts.label, color: opts.labelColor, fontSize: 11, distance: 4 } : undefined,
      labelLayout: opts.label ? { moveOverlap: 'shiftY' } : undefined,
      markLine: opts.markLine, markArea: opts.markArea, areaStyle: opts.areaStyle, stack: opts.stack
    };
  }

  function stems(name, g, data, color, offset, ring) {
    return {
      name: name, type: 'custom', xAxisIndex: g, yAxisIndex: g, data: data, clip: true, z: 4,
      encode: { x: 0, y: 1 },
      renderItem: function (params, api) {
        var v = api.value(1);
        if (!(v > 0)) return null;
        var p0 = api.coord([api.value(0), 0]), p1 = api.coord([api.value(0), v]);
        var x = p0[0] + offset;
        return { type: 'group', children: [
          { type: 'line', shape: { x1: x, y1: p0[1], x2: x, y2: p1[1] }, style: { stroke: color, lineWidth: 1.5 } },
          { type: 'circle', shape: { cx: x, cy: p1[1], r: 4 }, style: { fill: color, stroke: ring, lineWidth: 1.5 } }
        ] };
      }
    };
  }

  function diffBars(name, g, data, t, tol) {
    return {
      name: name, type: 'custom', xAxisIndex: g, yAxisIndex: g, data: data, clip: true, z: 4,
      encode: { x: 0, y: 1 },
      renderItem: function (params, api) {
        var v = api.value(1);
        if (!num(v) || v === 0) return null;
        var p0 = api.coord([api.value(0), 0]), p1 = api.coord([api.value(0), v]);
        var w = Math.max(1.5, Math.min(4, api.size([60000, 0])[0] - 0.5));
        var c = Math.abs(v) <= tol + 1e-9 ? t.muted : v < 0 ? t['div-neg'] : t['div-pos'];
        return { type: 'rect', shape: { x: p0[0] - w / 2, y: Math.min(p0[1], p1[1]), width: w, height: Math.max(1, Math.abs(p1[1] - p0[1])) },
                 style: { fill: c } };
      }
    };
  }

  function lowSpans(orig) {
    var out = [], ts = orig.t || [], bg = orig.bg || [], start = null;
    for (var i = 0; i < ts.length; i++) {
      var low = num(bg[i]) && bg[i] < LOW_MGDL;
      if (low && start === null) start = i;
      if ((!low || i === ts.length - 1) && start !== null) {
        var end = low ? i : i - 1;
        out.push([{ xAxis: ts[start] * 1000 - 30000, name: 'recorded low' }, { xAxis: ts[end] * 1000 + 30000 }]);
        start = null;
      }
    }
    (orig.events || []).forEach(function (ev) {         // a low event with no BG below the line in this window
      if (ev.kind === 'low' && num(ev.t) && !out.some(function (s) { return ev.t * 1000 >= s[0].xAxis && ev.t * 1000 <= s[1].xAxis; })) {
        out.push([{ xAxis: ev.t * 1000 - 30000, name: 'recorded low' }, { xAxis: ev.t * 1000 + 90000 }]);
      }
    });
    return out;
  }

  var HAIRLINE_KINDS = { target_change: 'target', tt: 'TT', temp_target: 'TT', profile_switch: 'profile' };
  var CARB_KINDS = { carbs: true, cob_rise: true };

  /* the whole option for one chart: `days` = the distinct days drawn, `ents` = [{tag, slot, day, e, doc}],
     `origs` = {day: orig doc} */
  /* the data range of each panel (for columns that must share their y scales) */
  function extents(days, ents, origs) {
    var tolF = num(sc().iob_tolerance) ? sc().iob_tolerance : 1.3;
    var ex = {};
    function put(k, v) { if (!num(v)) return; var e = ex[k] || (ex[k] = [v, v]); if (v < e[0]) e[0] = v; if (v > e[1]) e[1] = v; }
    days.forEach(function (d) {
      var o = origs[d];
      (o.bg || []).forEach(function (v) { put('bg', bgv(v)); });
      (o.tar_hi || []).forEach(function (v) { put('bg', bgv(v)); });
      ['isf_factor', 'ins_req', 'smb', 'tbr', 'iob'].forEach(function (k) { (o[k] || []).forEach(function (v) { put(KEYMAP[k], v); }); });
    });
    ents.forEach(function (en) {
      var s = en.doc, o = origs[en.day];
      ['isf_factor', 'ins_req', 'smb', 'tbr'].forEach(function (k) { (s[k] || []).forEach(function (v) { put(KEYMAP[k], v); }); });
      (s.iob_th || []).forEach(function (v) { put('iob', num(v) ? v * tolF : null); });
      (s.smb || []).forEach(function (v, i) { var p = at(o, 'smb', nearest(o.t, s.t[i])); if (num(v) && num(p)) put('diff', Math.abs(v - p)); });
    });
    return ex;
  }
  var KEYMAP = { isf_factor: 'factor', ins_req: 'req', smb: 'smb', tbr: 'tbr', iob: 'iob' };
  function nice(lo, hi) {
    var span = Math.max(hi - lo, 1e-6), step = Math.pow(10, Math.floor(Math.log(span) / Math.LN10)) / 2;
    return [Math.floor(lo / step) * step, Math.ceil(hi / step) * step];
  }

  function buildOption(t, days, ents, origs, narrow, shared) {
    var multi = days.length > 1;
    var L = layout(ents.length);
    var grids = [], xAxes = [], yAxes = [], titles = [], series = [];
    var tmin = Infinity, tmax = -Infinity;
    days.forEach(function (d) { var ts = origs[d].t || []; if (ts.length) { tmin = Math.min(tmin, ts[0]); tmax = Math.max(tmax, ts[ts.length - 1]); } });
    var col = function (slot) { return slotColor(t, slot); };
    var dayTag = function (d) { return multi ? ' ' + shortDay(d) : ''; };
    var maxDiff = 0;
    var tol = num(sc().smb_tol_U) ? sc().smb_tol_U : 0.05;
    var tolF = num(sc().iob_tolerance) ? sc().iob_tolerance : 1.3;
    ents.forEach(function (en) {
      var o = origs[en.day], s = en.doc;
      (s.smb || []).forEach(function (v, i) { var p = at(o, 'smb', nearest(o.t, s.t[i])); if (num(v) && num(p)) maxDiff = Math.max(maxDiff, Math.abs(v - p)); });
    });
    if (shared && shared.diff) maxDiff = Math.max(maxDiff, shared.diff[1]);
    maxDiff = Math.max(maxDiff, tol * 2);
    var titleText = narrow ? {
      bg: 'Recorded glucose (' + bgUnit() + ')', factor: 'Final ISF factor', req: 'insulinReq (U)', smb: 'SMB (U)',
      tbr: 'Temp basal in force (U/h)', iob: 'Recorded IOB, iobTH (U)'
    } : {
      bg: 'Recorded glucose (' + bgUnit() + '), the same for every variant',
      factor: 'Final ISF factor (ratio), hairline = cap',
      req: 'insulinReq (U)',
      smb: 'SMB (U), one stem per loop',
      tbr: 'Temp basal rate in force (U/h)',
      iob: 'Recorded IOB (U) with iobTH and its ' + Math.round(tolF * 100) + ' % tolerance'
    };
    L.parts.forEach(function (p, g) {
      var last = g === L.parts.length - 1;
      grids.push({ left: 46, right: narrow ? 58 : 76, top: p.top, height: p.h, containLabel: false });
      xAxes.push({ type: 'time', gridIndex: g, min: tmin * 1000, max: tmax * 1000, splitNumber: narrow ? 4 : 8,
                   axisLabel: { show: last, formatter: function (v) { return hhmm(v); }, hideOverlap: true },
                   axisLine: { show: p.key === 'strip' || last }, splitLine: { show: p.key !== 'strip' },
                   axisPointer: { label: { show: last, formatter: function (x) { return hhmm(x.value) + ' UTC'; } } } });
      var y = { type: 'value', gridIndex: g, splitNumber: p.h < 90 ? 2 : 3, axisLabel: { fontSize: 10, hideOverlap: true },
                axisPointer: { label: { show: false } } };
      if (p.key === 'bg') { y.scale = true; }
      if (p.key === 'factor') { y.scale = true; }
      if (shared && shared[p.key] && p.key !== 'strip') {      // day columns: one y scale per panel across columns
        var r = shared[p.key], lo = p.key === 'bg' || p.key === 'factor' ? r[0] : Math.min(0, r[0]);
        var nr = nice(lo, r[1]);
        y.min = nr[0]; y.max = nr[1]; y.scale = false;
      }
      if (p.key === 'strip') {
        y.min = -maxDiff; y.max = maxDiff; y.interval = maxDiff; y.splitLine = { show: false };
        y.axisLabel = { fontSize: 9, formatter: function (v) { return v === 0 ? '0' : fmt.signed(v, 2); } };
      }
      yAxes.push(y);
      var tt = p.key === 'strip' ? ents[p.strip].tag + dayTag(ents[p.strip].day) + (narrow ? ' SMB minus phone (U)' : ' SMB minus the phone (U); grey within ±' + fmt.num(tol, 2) + ' U')
                                 : titleText[p.key];
      titles.push({ text: tt, left: 46, top: p.titleTop, textStyle: { fontSize: p.key === 'strip' ? 11 : 12, fontWeight: p.key === 'strip' ? 500 : 600, color: p.key === 'strip' ? t['ink-2'] : t.ink } });
    });
    var G = {};
    L.parts.forEach(function (p, g) { if (p.key !== 'strip') G[p.key] = g; });

    // glucose: target band (phone), BG per day, lows, carbs, hairlines
    days.forEach(function (d) {
      var o = origs[d], ts = o.t || [];
      var lo = ts.map(function (x, i) { return [x * 1000, bgv(at(o, 'tar_lo', i))]; });
      var span = ts.map(function (x, i) { var a = bgv(at(o, 'tar_lo', i)), b = bgv(at(o, 'tar_hi', i)); return [x * 1000, num(a) && num(b) ? Math.max(0, Math.round((b - a) * 10) / 10) : null]; });
      series.push(lineSeries('target' + dayTag(d), G.bg, lo, t.axis, { step: 'end', width: 1, stack: 'tb' + d, silent: true, z: 1 }));
      series.push(lineSeries('target band' + dayTag(d), G.bg, span, t.axis, { step: 'end', width: 1, stack: 'tb' + d, silent: true, z: 1,
        areaStyle: { color: t.grid, opacity: 0.9 } }));
      var hair = [];
      (o.events || []).forEach(function (ev) {
        if (HAIRLINE_KINDS[ev.kind] && num(ev.t)) hair.push({ xAxis: ev.t * 1000, name: ev.label || HAIRLINE_KINDS[ev.kind], label: { formatter: HAIRLINE_KINDS[ev.kind] } });
      });
      series.push(lineSeries('BG' + dayTag(d), G.bg, ts.map(function (x, i) { return [x * 1000, bgv(at(o, 'bg', i))]; }), t['ink-2'], {
        label: multi ? 'BG ' + shortDay(d) : null, labelColor: t['ink-2'], z: 3,
        markArea: { silent: true, itemStyle: { color: t.muted, opacity: 0.22 }, label: { show: true, position: 'insideTop', color: t['ink-2'], fontSize: 10, formatter: 'low' }, data: lowSpans(o) },
        markLine: hair.length ? { silent: true, symbol: 'none', lineStyle: { color: t.muted, width: 1, type: 'solid' },
          label: { position: 'insideEndTop', color: t['ink-2'], fontSize: 10 }, data: hair } : undefined
      }));
      var carbs = [];
      (o.events || []).forEach(function (ev) {
        if (CARB_KINDS[ev.kind] && num(ev.t)) { var i = nearest(ts, ev.t); carbs.push({ value: [ev.t * 1000, bgv(at(o, 'bg', i))], name: ev.label || 'carbs' }); }
      });
      if (carbs.length) series.push({ name: 'carbs' + dayTag(d), type: 'scatter', xAxisIndex: G.bg, yAxisIndex: G.bg, data: carbs, symbol: 'triangle', symbolSize: 10,
        symbolOffset: [0, -10], itemStyle: { color: t.ink, borderColor: t.surface, borderWidth: 1.5 }, z: 5 });
    });

    // phone (grey) and each entity on factor / insulinReq / TBR; SMB stems
    var nStem = days.length + ents.length, k = 0;
    function off() { var o = (k - (nStem - 1) / 2) * 3; k++; return o; }
    days.forEach(function (d) {
      var o = origs[d], ts = o.t || [];
      var pick = function (key) { return ts.map(function (x, i) { return [x * 1000, at(o, key, i)]; }); };
      var lab = 'phone' + dayTag(d);
      series.push(lineSeries(lab, G.factor, pick('isf_factor'), t.phone, { label: lab, labelColor: t['ink-2'], z: 2 }));
      series.push(lineSeries(lab, G.req, pick('ins_req'), t.phone, { label: lab, labelColor: t['ink-2'], z: 2 }));
      series.push(lineSeries(lab, G.tbr, pick('tbr'), t.phone, { label: lab, labelColor: t['ink-2'], step: 'end', connect: true, z: 2 }));
      series.push(stems(lab, G.smb, pick('smb'), t.phone, off(), t.surface));
      series.push(lineSeries('IOB' + dayTag(d), G.iob, pick('iob'), t['ink-2'], { label: multi ? 'IOB ' + shortDay(d) : 'IOB', labelColor: t['ink-2'], z: 2 }));
    });
    ents.forEach(function (en, j) {
      var s = en.doc, ts = s.t || [], c = col(en.slot), o = origs[en.day];
      var pick = function (key) { return ts.map(function (x, i) { return [x * 1000, at(s, key, i)]; }); };
      var lab = en.tag + (state.mode === 'days' ? '' : dayTag(en.day));
      series.push(lineSeries(lab, G.factor, pick('isf_factor'), c, { label: lab, labelColor: t['ink-2'] }));
      series.push(lineSeries(lab, G.req, pick('ins_req'), c, { label: lab, labelColor: t['ink-2'] }));
      series.push(lineSeries(lab, G.tbr, pick('tbr'), c, { label: lab, labelColor: t['ink-2'], step: 'end', connect: true }));
      series.push(stems(lab, G.smb, pick('smb'), c, off(), t.surface));
      var g = G.smb + 1 + j;
      series.push(diffBars(lab + ' minus phone', g, ts.map(function (x, i) {
        var p = at(o, 'smb', nearest(o.t, x)), v = at(s, 'smb', i);
        return [x * 1000, num(v) && num(p) ? Math.round((v - p) * 1000) / 1000 : null];
      }), t, tol));
    });

    // iobTH and its tolerance: one line per entity, or one shared line when every entity has the same values
    var groups = [];
    ents.forEach(function (en) {
      var key = en.day + '|' + JSON.stringify(en.doc.iob_th || []);
      var gr = groups.filter(function (x) { return x.key === key; })[0];
      if (gr) gr.ents.push(en); else groups.push({ key: key, ents: [en] });
    });
    groups.forEach(function (gr, gi) {
      var en = gr.ents[0], s = en.doc, ts = s.t || [];
      if (!(s.iob_th || []).some(num)) return;
      var shared = gr.ents.length > 1;
      var c = shared ? t.ink : col(en.slot);
      var who = gr.ents.map(function (x) { return x.tag; }).join('=');
      series.push(lineSeries('iobTH ' + who, G.iob, ts.map(function (x, i) { return [x * 1000, at(s, 'iob_th', i)]; }), c,
        { step: 'end', width: 1.5, label: 'iobTH ' + who, labelColor: t['ink-2'], z: 3 }));
      series.push(lineSeries(Math.round(tolF * 100) + ' % ' + who, G.iob, ts.map(function (x, i) { var v = at(s, 'iob_th', i); return [x * 1000, num(v) ? Math.round(v * tolF * 100) / 100 : null]; }), c,
        { step: 'end', width: 1, opacity: 0.55, label: gi === 0 ? Math.round(tolF * 100) + ' %' : null, labelColor: t.muted, z: 3 }));
    });

    // cap hairlines on the factor panel: one per distinct autoISF_max among the phone and the entities
    var caps = {};
    var phoneCap = ((sc().phone || {}).caps || {}).autoISF_max;
    if (num(phoneCap)) caps[phoneCap] = ['phone'];
    ents.forEach(function (en) { var v = ((en.e || {}).caps || {}).autoISF_max; if (num(v)) (caps[v] = caps[v] || []).push(en.tag); });
    var capLines = Object.keys(caps).sort().map(function (v, i) {
      return { yAxis: +v, name: 'cap ' + v, label: { position: i % 2 ? 'insideStartBottom' : 'insideEndBottom', formatter: 'cap ' + v + ' · ' + caps[v].join(', ') } };
    });
    if (capLines.length) series.push({ name: 'cap', type: 'line', xAxisIndex: G.factor, yAxisIndex: G.factor, data: [], silent: true,
      markLine: { silent: true, symbol: 'none', lineStyle: { color: t.muted, width: 1, type: 'solid' }, label: { position: 'insideEndBottom', color: t['ink-2'], fontSize: 10 }, data: capLines } });
    if (capLines.length) {                                 // keep the cap inside the factor panel's range
      var ys = yAxes[G.factor], cmax = Math.max.apply(null, Object.keys(caps).map(Number));
      if (!num(ys.max)) ys.max = function (v) { return Math.max(v.max, cmax) + 0.05; };
      else ys.max = Math.max(ys.max, cmax + 0.05);
    }

    var all = xAxes.map(function (_, i) { return i; });
    var zoom = [{ type: 'slider', xAxisIndex: all, bottom: 8, height: 22, filterMode: 'none', labelFormatter: function (v) { return hhmm(v); },
                  brushSelect: false, showDetail: !narrow }];
    if (!narrow) zoom.push({ type: 'inside', xAxisIndex: all, filterMode: 'none', zoomOnMouseWheel: 'shift', moveOnMouseWheel: false, moveOnMouseMove: true });
    return {
      useUTC: true,
      animation: false,
      title: titles, grid: grids, xAxis: xAxes, yAxis: yAxes, series: series, dataZoom: zoom,
      axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: t.muted, width: 1 }, label: { backgroundColor: t['ink-2'], color: t.surface } },
      tooltip: { trigger: 'axis', confine: true, axisPointer: { type: 'line' }, transitionDuration: 0,
                 formatter: function (params) { var p = Array.isArray(params) ? params[0] : params; return p ? tipHtml(t, p.axisValue, days, ents, origs) : ''; } }
    };
  }

  function tipHtml(t, x, days, ents, origs) {
    var tsec = x / 1000, multi = days.length > 1;
    var out = '<div class="sc-tip"><b>' + esc(hhmm(x)) + ' UTC</b>';
    days.forEach(function (d) {
      var o = origs[d], i = nearest(o.t, tsec);
      out += '<div>' + (multi ? esc(d) + ': ' : '') + 'BG ' + esc(fmt.num(bgv(at(o, 'bg', i)), units() === 'mmol' ? 1 : 0)) + ' ' + bgUnit() +
             ', IOB ' + esc(fmt.num(at(o, 'iob', i), 2)) + ' U</div>';
    });
    out += '<table><tr><th class="k"></th><th>req</th><th>SMB</th><th>TBR</th><th>factor</th></tr>';
    function row(label, color, doc, i) {
      return '<tr><td class="k"><i style="background:' + esc(color) + '"></i>' + esc(label) + '</td><td>' + esc(fmt.num(at(doc, 'ins_req', i), 2)) + '</td><td>' +
             esc(fmt.num(at(doc, 'smb', i), 2)) + '</td><td>' + esc(fmt.num(at(doc, 'tbr', i), 2)) + '</td><td>' + esc(fmt.num(at(doc, 'isf_factor', i), 2)) + '</td></tr>';
    }
    days.forEach(function (d) { var o = origs[d]; out += row('phone' + (multi ? ' ' + shortDay(d) : ''), t.phone, o, nearest(o.t, tsec)); });
    ents.forEach(function (en) { out += row(en.tag + (multi && state.mode !== 'days' ? ' ' + shortDay(en.day) : ''), slotColor(t, en.slot), en.doc, nearest(en.doc.t, tsec)); });
    return out + '</table></div>';
  }

  // ------------------------------------------------------------------------------------------------ table twin
  function twin(fig, days, ents, origs) {
    var multi = days.length > 1;
    var cols = [{ key: 'day', label: 'day' }, { key: 'time', label: 'time (UTC)' },
                { key: 'bg', label: 'BG (' + bgUnit() + ')', numeric: true }, { key: 'target', label: 'target' },
                { key: 'p_req', label: 'phone insulinReq', numeric: true }, { key: 'p_smb', label: 'phone SMB', numeric: true },
                { key: 'p_tbr', label: 'phone TBR', numeric: true }, { key: 'p_f', label: 'phone factor', numeric: true },
                { key: 'iob', label: 'recorded IOB', numeric: true }];
    if (!multi) cols.shift();
    ents.forEach(function (en, j) {
      var p = 'e' + j + '_', tag = en.tag + (multi && state.mode !== 'days' ? ' ' + shortDay(en.day) : '');
      cols.push({ key: p + 'req', label: tag + ' insulinReq', numeric: true }, { key: p + 'smb', label: tag + ' SMB', numeric: true },
                { key: p + 'd', label: tag + ' SMB minus phone', numeric: true }, { key: p + 'tbr', label: tag + ' TBR', numeric: true },
                { key: p + 'f', label: tag + ' factor', numeric: true }, { key: p + 'th', label: tag + ' iobTH', numeric: true });
    });
    var rows = [], meta = [], n2 = function (v) { return num(v) ? fmt.num(v, 2) : '-'; };
    days.forEach(function (d) {
      var o = origs[d];
      var own = state.mode === 'days' ? { vhash: state.dayVariant, slot: slotOf(state.dayVariant) }
        : ents.filter(function (en) { return en.day === d; }).sort(function (a, b) { return a.slot - b.slot; })[0] || null;
      (o.t || []).forEach(function (x, i) {
        meta.push({ day: d, t: x, index: i, vhash: own ? own.vhash : '', slot: own ? own.slot : 0 });
        var lo = bgv(at(o, 'tar_lo', i)), hi = bgv(at(o, 'tar_hi', i));
        var r = { day: d, time: hhmm(x * 1000), bg: fmt.num(bgv(at(o, 'bg', i)), units() === 'mmol' ? 1 : 0),
                  target: num(lo) ? (lo === hi ? String(lo) : lo + '-' + hi) : '-',
                  p_req: n2(at(o, 'ins_req', i)), p_smb: n2(at(o, 'smb', i)), p_tbr: n2(at(o, 'tbr', i)), p_f: n2(at(o, 'isf_factor', i)), iob: n2(at(o, 'iob', i)) };
        ents.forEach(function (en, j) {
          if (en.day !== d) return;
          var k = nearest(en.doc.t, x), p = 'e' + j + '_', v = at(en.doc, 'smb', k), ps = at(o, 'smb', i);
          r[p + 'req'] = n2(at(en.doc, 'ins_req', k)); r[p + 'smb'] = n2(v); r[p + 'd'] = num(v) && num(ps) ? fmt.signed(v - ps, 2) : '-';
          r[p + 'tbr'] = n2(at(en.doc, 'tbr', k)); r[p + 'f'] = n2(at(en.doc, 'isf_factor', k)); r[p + 'th'] = n2(at(en.doc, 'iob_th', k));
        });
        rows.push(r);
      });
    });
    var det = HC.tableTwin(fig, { columns: cols, rows: rows, summary: 'Table: every loop of this chart (' + rows.length + ' rows)' });
    // each row names its own loop and variant, so the drill-down never has to read them back from the cells
    var trs = det && det.querySelectorAll ? det.querySelectorAll('tbody tr') : [];
    Array.prototype.forEach.call(trs, function (tr, k) {
      var mt = meta[k];
      if (!mt) return;
      tr.setAttribute('data-day', mt.day); tr.setAttribute('data-t', String(mt.t)); tr.setAttribute('data-index', String(mt.index));
      if (mt.vhash) { tr.setAttribute('data-vhash', mt.vhash); tr.setAttribute('data-slot', String(mt.slot || 0)); }
    });
  }

  // ------------------------------------------------------------------------------------------------ figures
  var figs = {};                                           // key -> {fig, rec, model}
  function narrowNow() { return (window.innerWidth || 1200) < 600; }

  function ensureFigure(key, parent) {
    var f = figs[key];
    if (!f) {
      var fig = HC.figure('', '');
      fig.querySelector('figcaption .title').id = 'sc-title-' + key.replace(/[^a-z0-9]/gi, '');
      var legend = h('div', { class: 'sc-legend', 'aria-label': 'legend' });
      var badges = h('div', { class: 'sc-badges' });
      var msg = h('p', { class: 'sc-msg', hidden: true });
      var plot = fig.querySelector('.plot');
      fig.insertBefore(badges, plot); fig.insertBefore(legend, plot); fig.insertBefore(msg, plot);
      plot.setAttribute('tabindex', '0');
      plot.setAttribute('role', 'img');
      f = figs[key] = { key: key, fig: fig, legend: legend, badges: badges, msg: msg, plot: plot, rec: null, model: null, index: -1 };
    }
    if (f.fig.parentNode !== parent) parent.appendChild(f.fig);
    f.fig.hidden = false;
    return f;
  }

  function wire(f) {                                      // events and the connect group; again after a theme rebuild
    var c = f.rec && f.rec.chart;
    if (!c || c.__hcWired) return;
    c.__hcWired = true;
    if (f.group) { c.group = f.group; if (window.echarts && window.echarts.connect) window.echarts.connect(f.group); }
    c.getZr().on('click', function (ev) {
      var i = loopAt(c, f.model, ev.offsetX, ev.offsetY);
      if (i < 0) return;
      f.index = i;
      fireLoop(f);
    });
  }

  /* index of the loop under a click at pixel (px, py), or -1 outside every panel (the zoom slider, the titles). An
     axis finder converts ONE number (an [x, y] pair yields NaN), so the panel is found with a grid finder and the
     time is read from that panel's own x axis; every panel shares one time scale. */
  function loopAt(c, m, px, py) {
    if (!c || !m || !num(px) || !num(py)) return -1;
    var o = m.origs && m.origs[m.days[0]];
    if (!o || !o.t || !o.t.length) return -1;
    var grids = ((c.getOption() || {}).grid || []).length;
    for (var g = 0; g < grids; g++) {
      if (!c.containPixel({ gridIndex: g }, [px, py])) continue;
      var xv = c.convertFromPixel({ xAxisIndex: g }, px);
      return num(xv) ? nearest(o.t, xv / 1000) : -1;
    }
    return -1;
  }

  function fireLoop(f) {
    var m = f.model;
    if (!m) return;
    var d = m.days[0], o = m.origs[d], i = f.index;
    if (i < 0 || !o.t || i >= o.t.length) return;
    var info = { day: d, t: o.t[i], index: i, mode: state.mode, entities: m.ents.map(function (en) { return { vhash: en.vhash, day: en.day, slot: en.slot }; }) };
    loopListeners.forEach(function (cb) { try { cb(info); } catch (e) { console.error('scenarios onLoop', e); } });
  }

  function showTip(f) {
    var c = f.rec && f.rec.chart, m = f.model;
    if (!c || !m) return;
    var o = m.origs[m.days[0]];
    if (f.index < 0 || f.index >= (o.t || []).length) return;
    var x = o.t[f.index] * 1000;
    var px = c.convertToPixel({ xAxisIndex: 0 }, x);
    var g = c.getOption().grid[0];
    if (num(px)) c.dispatchAction({ type: 'showTip', x: px, y: g.top + 20 });
  }

  function keyNav(f) {
    f.plot.addEventListener('keydown', function (ev) {
      var m = f.model;
      if (!m) return;
      var n = (m.origs[m.days[0]].t || []).length;
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        f.index = Math.max(0, Math.min(n - 1, (f.index < 0 ? 0 : f.index + (ev.key === 'ArrowRight' ? 1 : -1))));
        showTip(f); ev.preventDefault();
      } else if (ev.key === 'Home' || ev.key === 'End') {
        f.index = ev.key === 'Home' ? 0 : n - 1; showTip(f); ev.preventDefault();
      } else if (ev.key === 'Enter' || ev.key === ' ') {
        if (f.index >= 0) { fireLoop(f); ev.preventDefault(); }
      } else if (ev.key === 'Escape') {
        if (f.rec && f.rec.chart) f.rec.chart.dispatchAction({ type: 'hideTip' });
      }
    });
    f.plot.addEventListener('blur', function () { if (f.rec && f.rec.chart) f.rec.chart.dispatchAction({ type: 'hideTip' }); });
  }

  function legendFor(f, days, ents) {
    HC.clear(f.legend);
    var multi = days.length > 1;
    f.legend.appendChild(h('span', null, h('i', { class: 'sw' }), multi ? 'phone (per day, labelled at the line end)' : 'phone'));
    ents.forEach(function (en) {
      f.legend.appendChild(h('span', { class: 'long' }, h('i', { class: 'sw ' + slotClass(en.slot) }),
        h('b', { text: en.tag + (multi && state.mode !== 'days' ? ' ' + shortDay(en.day) : '') }), ' ' + en.e.label + (state.mode === 'days' ? '' : ' · ' + en.day)));
    });
    f.legend.appendChild(h('span', null, h('i', { class: 'sw bg' }), 'recorded BG / IOB'));
    f.legend.appendChild(h('span', null, h('i', { class: 'sw band' }), 'target'));
    f.legend.appendChild(h('span', null, h('i', { class: 'sw low' }), 'recorded low (position only)'));
    f.legend.appendChild(h('span', null, h('i', { class: 'sw carb' }), 'carbs / COB step'));
    f.legend.appendChild(h('span', null, h('i', { class: 'sw hair' }), 'target or profile change'));
    f.legend.appendChild(h('span', null, h('i', { class: 'sw cap' }), 'autoISF cap'));
    f.legend.appendChild(h('span', null, h('i', { class: 'sw neg' }), h('i', { class: 'sw pos' }), h('i', { class: 'sw mid' }), 'SMB less / more than the phone / within tolerance'));
  }

  function badgesFor(f, days) {
    HC.clear(f.badges);
    days.forEach(function (d) {
      var dd = dayOf(d) || {};
      var bits = [];
      // one shared pill style with the Parameters view's verdict pills (page.css .pill); colour only for a confounder
      if (dd.role && dd.role !== 'search') bits.push(h('span', { class: 'pill', text: roleText(dd.role) }));
      (dd.tags || []).forEach(function (x) { bits.push(h('span', { class: 'pill', text: x })); });
      (dd.confounders || []).forEach(function (c) { bits.push(h('span', { class: 'pill warning', text: c })); });
      var any = bits.length > 0;
      if (!any) bits.push(h('span', { class: 'pill', text: 'no confounders recorded' }));
      if (any || days.length > 1 || state.mode === 'days')
        f.badges.appendChild(h('span', { class: 'sc-badge-day' }, h('b', { text: d }), bits));
    });
    f.badges.hidden = !f.badges.firstChild;
  }

  /* draw one figure: loads its documents, then builds or updates the chart */
  function drawFigure(f, days, list, title, note, seq) {
    f.fig.querySelector('figcaption .title').textContent = title;
    var nt = f.fig.querySelector('figcaption .note');
    if (!nt) { nt = h('span', { class: 'note' }); f.fig.querySelector('figcaption').appendChild(nt); }
    nt.textContent = note;
    f.msg.hidden = false;
    f.msg.textContent = 'loading the per-loop data ...';
    var jobs = days.map(function (d) { return load((dayOf(d) || {}).orig); }).concat(list.map(function (en) { return load(en.path); }));
    return Promise.all(jobs).then(function (res) {
      if (seq !== drawSeq) return;
      var origs = {}, errors = [], okDays = [];
      days.forEach(function (d, i) { if (res[i].doc) { origs[d] = res[i].doc; okDays.push(d); } else errors.push(res[i].error); });
      var ents = [];
      list.forEach(function (en, j) {
        var r = res[days.length + j];
        if (r.doc && origs[en.day]) ents.push({ vhash: en.vhash, day: en.day, slot: en.slot, tag: en.tag, e: en.e, doc: r.doc });
        else if (r.error) errors.push(r.error);
      });
      var uniq = errors.filter(function (x, i) { return errors.indexOf(x) === i; });
      f.msg.textContent = uniq.length ? uniq[0] + (uniq.length > 1 ? ' (and ' + (uniq.length - 1) + ' more per-loop document' + (uniq.length > 2 ? 's' : '') + ')' : '') : '';
      f.msg.hidden = !uniq.length;
      legendFor(f, okDays, ents);
      badgesFor(f, okDays);
      if (!okDays.length) {
        f.plot.hidden = true;
        var old = f.fig.querySelector('details.twin'); if (old) old.remove();
        return;
      }
      f.plot.hidden = false;
      f.model = { days: okDays, ents: ents, origs: origs };
      f.index = -1;
      var L = layout(ents.length);
      f.plot.style.height = L.height + 'px';
      f.plot.setAttribute('aria-label', title + '. Arrow keys step through the loops, Enter opens the loop.');
      f.model.extents = extents(okDays, ents, origs);
      var build = function (t) { return f.model ? buildOption(t, f.model.days, f.model.ents, f.model.origs, narrowNow(), f.shared) : {}; };
      if (!f.rec) {
        f.rec = HC.chart(f.fig, build);
        keyNav(f);
      } else if (f.rec.chart) {
        f.rec.chart.resize();
        f.rec.chart.setOption(build(HC.theme.current()), { notMerge: true });
      }
      wire(f);
      twin(f.fig, okDays, ents, origs);
    });
  }

  function hideAll() { Object.keys(figs).forEach(function (k) { figs[k].fig.hidden = true; if (figs[k].rec && figs[k].rec.chart) figs[k].rec.chart.dispatchAction({ type: 'hideTip' }); }); }

  function redraw() {
    if (!ui) return;
    redrawControls();
    var seq = ++drawSeq;
    hideAll();
    var list = drawList();
    HC.clear(ui.charts);
    ui.charts.className = 'sc-days';
    if (!list.length) {
      ui.charts.appendChild(h('p', { class: 'placeholder', text: state.mode === 'days' ? 'This variant has no per-loop data on any day yet.' : 'No variant is shown. Pick a variant and a day above and add it.' }));
      return;
    }
    if (state.mode === 'compare') {
      ui.charts.className = 'sc-days one';
      var days = [];
      list.forEach(function (en) { if (days.indexOf(en.day) < 0) days.push(en.day); });
      days.sort();
      var f = ensureFigure('compare', ui.charts);
      f.group = null;
      drawFigure(f, days, list, days.length > 1 ? 'Decisions by time of day, ' + days.join(' / ') : 'Decisions on ' + days[0],
        'one stem or point per loop; the phone in grey, variants by slot colour', seq);
    } else {
      if (list.length === 1) ui.charts.className = 'sc-days one';
      var cols = list.map(function (en) {
        var f = ensureFigure('day:' + en.day, ui.charts);
        f.group = DAY_GROUP;
        f.shared = null;
        return { f: f, p: drawFigure(f, [en.day], [en], en.day + ': ' + en.e.label, 'columns share the time of day, the crosshair, the zoom and the y scales', seq) };
      });
      Promise.all(cols.map(function (c) { return c.p; })).then(function () {
        if (seq !== drawSeq) return;
        var merged = {};
        cols.forEach(function (c) {
          var ex = (c.f.model || {}).extents || {};
          Object.keys(ex).forEach(function (k) { var m = merged[k]; merged[k] = m ? [Math.min(m[0], ex[k][0]), Math.max(m[1], ex[k][1])] : ex[k].slice(); });
        });
        cols.forEach(function (c) {
          c.f.shared = merged;
          if (c.f.rec && c.f.rec.chart && c.f.model) c.f.rec.chart.setOption(c.f.rec.build(HC.theme.current()), { notMerge: true });
        });
      });
    }
  }

  // ------------------------------------------------------------------------------------------------ view
  function render(el, data) {
    injectCss();
    var s = (data && data.scenarios) || null;
    el.appendChild(h('div', { class: 'block' },
      h('header', null, h('h2', { text: 'Scenario comparison' }),
        h('p', { text: 'What the loop would have asked for, loop by loop, under each variant next to what the phone did. Glucose and IOB are the recorded history in every panel; a variant\'s own glucose or IOB is never computed.' }))));
    if (!s || !(s.entities || []).length) {
      el.appendChild(h('p', { class: 'placeholder', text: 'No variant has finished a replay yet, so there is nothing to compare.' }));
      return;
    }
    if (!state.slots.some(Boolean) && !state.dayVariant) initState();
    ui = { el: el, controls: h('div', { class: 'block' }), charts: h('div', { class: 'sc-days' }), msg: null };
    el.appendChild(ui.controls);
    el.appendChild(ui.charts);
    el.appendChild(h('p', { class: 'sc-note', text: 'Read a difference against the floor on the Overview tab: the base replay itself differs from the phone in some loops. A grey bar in the difference strip is within ±' + fmt.num(s.smb_tol_U, 2) + ' U of the phone, the per-loop tolerance the base replay is scored with. Click a loop, or focus a chart and use the arrow keys and Enter, to pick it for a closer look.' }));
    redraw();
  }

  HC.theme.onChange(function () {                          // page.js re-created the charts: wire the new instances
    Object.keys(figs).forEach(function (k) { wire(figs[k]); });
  });

  var api = HC.scenarios = {
    state: state,
    add: function (vhash, day) {
      if (!state.slots.some(Boolean) && !state.dayVariant) initState();
      day = day || seriesDays(vhash).filter(function (d) { return (dayOf(d) || {}).orig; })[0];
      return day ? add(vhash, day) : false;
    },
    replace: replace,
    remove: remove,
    setMode: setMode,
    onLoop: function (cb) { if (typeof cb === 'function') loopListeners.push(cb); },
    focusLoop: function (day, t) {
      Object.keys(figs).forEach(function (k) {
        var f = figs[k];
        if (f.fig.hidden || !f.model || f.model.days[0] !== day) return;
        f.index = nearest(f.model.origs[day].t, t);
        showTip(f);
      });
    },
    refresh: function () { redraw(); },
    /* the pure option builder (tokens, days[], ents[{tag, slot, day, e, doc}], origs{day: doc}, narrow, shared) and
       the panel ranges it can share; exposed for later views and the node smoke test */
    buildOption: buildOption,
    extents: extents,
    loopAt: loopAt,                                        // (chart, model, px, py) -> loop index under a click, or -1
    _wire: function (f) { wire(f); }                       // node smoke test: the click handler of one figure
  };

  if (HC.parameters && typeof HC.parameters.onSelect === 'function') {
    HC.parameters.onSelect(function (vhash) {
      api.add(vhash);
      if (location.hash !== '#scenarios') location.hash = '#scenarios'; else redraw();
    });
  }

  HC.registerView('scenarios', { title: 'Scenarios', issue: 'H-404', render: render });
})();
