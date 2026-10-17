/* views/parameters.js (H-403 Parameter space): the Parameters tab, the leaderboard and the delta heatmap.

   Reads data.parameters (report.parameter_space): question, days[], floors{day: {floor_*, tie{component}}}, tie_factor,
   components[{key, label, unit, floor_key, insulin}], extras[{key, label, unit}], params[{key, type, phone, ...}],
   base{vhash, variant, values, days}, variants[{vhash, variant, values, per_day{day: {metric}}, mean, verdict, why,
   rank, position, feasible, reasons{day: []}, envelope_days{day: {env_3h_U, env_day_pct_tdd}}, step, floor_multiple, floor_multiples{component}, review_score (not shown),
   review_component, tie_with_base, flags, validation{line, caveat, ...}}],
   The rows come in the review order (H-715): the floor multiple on the review component, descending - the 'x floor'
   column, the same number round.json's why and the cards print.
   rule_lines[], validation_lines[] (the 'Hold-out validation' panel; empty without a validate round),
   pareto (question earlier / gentler, else null: report.pareto_view {axes[3], weights, scalar_text, points[{vhash,
   variant, x, y, z, front, feasible, scalar, rank}], base{x, y, z}, order[], order_text, sensitivity{available, note,
   declared_top[], weights[{weight, value, '0.5': [], '1.5': [], tau{factor}, same_top}]}}) - the 'Pareto front' block,
   envelope (null unless some variant is envelope-limited: report.envelope_view {lead, items[{variant, days_text,
   inside_days, days[]}]}) - the yellow box above the leaderboard,
   coverage (null unless the metrics carry the manual-bolus coverage: {columns[{key, head, label, unit}], caveat}; the
   variant's numbers are v.coverage{key}).
   Tier 2 is not needed. Styles are scoped under #view-parameters and injected once (report.py inlines page.css only);
   every colour is a page.css token.

   Hooks for later views (H-407 parcoords / Pareto, H-405 drill-down, H-409 live rows):
     HC.parameters.model(data)            -> the derived model (points, cells, floors) the charts here use
     HC.parameters.setFilter(set|null, label)  restrict the leaderboard to these vhashes (a brush), null clears
     HC.parameters.onSelect(cb)           cb(vhash, variantRow) when a leaderboard row is activated (click / Enter)
     HC.parameters.refresh()              re-draw after data.parameters changed (live rows merged in)
     HC.parameters.state                  {comp, day, hx, hy}: the current control values */
(function () {
  'use strict';
  var HC = window.HC;
  if (!HC || !HC.registerView) return;
  var h = HC.h, fmt = HC.fmt;

  var CSS = [
    '#view-parameters .pv-controls { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: end; }',
    '#view-parameters .pv-controls label { display: grid; gap: 2px; font-size: 0.8rem; color: var(--ink-2); min-width: 0; max-width: 100%; }',
    '#view-parameters select { font: inherit; font-size: 0.88rem; padding: 4px 8px; border: 1px solid var(--axis); border-radius: var(--radius); background: var(--surface); color: var(--ink); max-width: 100%; }',
    '#view-parameters .pv-legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 0.82rem; color: var(--ink-2); }',
    '#view-parameters .sw { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }',
    '#view-parameters .sw.var { background: var(--series-1); }',
    '#view-parameters .sw.tie { background: var(--phone); }',
    '#view-parameters .sw.base { background: var(--surface); box-shadow: inset 0 0 0 2px var(--ink); }',
    '#view-parameters .sw.mean { width: 16px; height: 2px; border-radius: 0; background: var(--series-1); vertical-align: 3px; }',
    '#view-parameters .sw.band { border-radius: 2px; width: 16px; background: var(--grid); }',
    '#view-parameters .pv-panels { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); gap: 12px; }',
    '#view-parameters .pv-panels figure.chart .plot { height: 220px; }',
    '#view-parameters .pv-scale { display: grid; grid-template-columns: auto minmax(80px, 260px) auto; gap: 4px 8px; align-items: center; font-size: 0.8rem; color: var(--ink-2); }',
    '#view-parameters .pv-scale .bar { height: 10px; border-radius: 2px; }',
    '#view-parameters .pv-scale .bar.div { background: linear-gradient(90deg, var(--div-neg), var(--div-mid) 38%, var(--div-mid) 62%, var(--div-pos)); }',
    '#view-parameters .pv-scale .bar.seq { background: linear-gradient(90deg, var(--div-mid), var(--div-mid) 24%, var(--seq-2), var(--seq-4)); }',
    '#view-parameters .pv-scale .base-key { grid-column: 1 / -1; }',
    '#view-parameters .pv-scale .base-key i { display: inline-block; width: 14px; height: 10px; margin-right: 6px; vertical-align: -1px; box-shadow: inset 0 0 0 2px var(--ink); }',
    '#view-parameters .lb-box { background: var(--surface); border: 1px solid var(--grid); border-radius: var(--radius); padding: 6px 0 2px 12px; min-width: 0; }',
    '#view-parameters .lb-rule { display: grid; gap: 4px; font-size: 0.86rem; color: var(--ink-2); max-width: 92ch; margin: 0; padding-left: 1.2em; }',
    '#view-parameters .lb-validation { background: var(--surface-2); border: 1px solid var(--grid); border-radius: var(--radius); padding: 8px 12px; display: grid; gap: 4px; max-width: 92ch; min-width: 0; }',
    '#view-parameters .lb-validation .lead { font-weight: 600; margin: 0; overflow-wrap: anywhere; }',
    '#view-parameters .lb-validation p { font-size: 0.86rem; color: var(--ink-2); margin: 0; overflow-wrap: anywhere; }',
    '#view-parameters .lb .vline { display: block; font-size: 0.8rem; color: var(--ink-2); margin-top: 2px; }',
    '#view-parameters .lb-head { display: grid; gap: 6px; }',
    '#view-parameters .lb-head .order { font-size: 1rem; font-weight: 600; color: var(--ink); }',
    '#view-parameters table.lb th { vertical-align: bottom; white-space: normal; min-width: 84px; max-width: 120px; line-height: 1.25; }',
    '#view-parameters table.lb th.short { min-width: 0; }',
    '#view-parameters table.lb th.n { text-align: right; }',
    '#view-parameters table.lb td.wrap { white-space: normal; min-width: 240px; max-width: 420px; }',
    '#view-parameters table.lb .sticky { position: sticky; left: 0; z-index: 1; background: var(--surface); }',
    '#view-parameters table.lb tr.base td { background: var(--surface-2); }',
    '#view-parameters table.lb tr.infeasible td { color: var(--muted); }',
    '#view-parameters table.lb tr.selectable { cursor: pointer; }',
    '#view-parameters table.lb tr.selectable:hover td { background: var(--surface-2); }',
    '#view-parameters table.lb .vlabel { display: grid; gap: 1px; }',
    '#view-parameters table.lb .vlabel code { font-size: 0.8rem; }',
    '#view-parameters table.lb .vlabel .sha { color: var(--muted); font-size: 0.74rem; }',
    '#view-parameters table.lb td.param { font-family: var(--mono); font-size: 0.82rem; }',
    '#view-parameters table.lb td.param.moved { font-weight: 700; }',
    '#view-parameters td.dcell { white-space: nowrap; }',
    '#view-parameters .dv.tie { color: var(--muted); }',
    '#view-parameters .fb { position: relative; display: inline-block; width: 46px; height: 10px; margin-left: 6px; vertical-align: middle; }',
    '#view-parameters .fb::before { content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 1px; background: var(--grid); }',
    '#view-parameters .fb .band { position: absolute; top: 1px; bottom: 1px; background: var(--grid); border-radius: 2px; }',
    '#view-parameters .fb .zero { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: var(--axis); }',
    '#view-parameters .fb .mk { position: absolute; top: 50%; width: 8px; height: 8px; margin: -4px 0 0 -4px; border-radius: 50%; background: var(--muted); }',
    '#view-parameters .fb .mk.neg { background: var(--div-neg); }',
    '#view-parameters .fb .mk.pos { background: var(--div-pos); }',
    '#view-parameters .fb .mk.loops { background: var(--ink-2); }',
    '#view-parameters .filter-chip { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; font-size: 0.86rem; color: var(--ink-2); }',
    '#view-parameters button.link { font: inherit; color: var(--series-1); background: none; border: 0; padding: 0; cursor: pointer; text-decoration: underline; }',
    '#view-parameters .pf-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 380px), 1fr)); gap: 12px; align-items: start; }',
    '#view-parameters .pf-grid figure.chart .plot { height: 340px; }',
    '#view-parameters .pf-side { display: grid; gap: 10px; min-width: 0; }',
    '#view-parameters .pf-order { margin: 0; padding-left: 1.6em; display: grid; gap: 3px; font-size: 0.86rem; }',
    '#view-parameters .pf-order code { font-size: 0.82rem; overflow-wrap: anywhere; }',
    '#view-parameters .pf-order .sc { color: var(--ink-2); font-family: var(--mono); font-size: 0.8rem; margin-left: 6px; }',
    '#view-parameters .pf-note { font-size: 0.86rem; color: var(--ink-2); margin: 0; max-width: 92ch; overflow-wrap: anywhere; }',
    '#view-parameters .pf-weights { font-family: var(--mono); font-size: 0.82rem; color: var(--ink); overflow-wrap: anywhere; }',
    '#view-parameters .sw.front { background: var(--series-1); box-shadow: inset 0 0 0 2px var(--ink); }',
    '#view-parameters .sw.other { background: var(--surface); box-shadow: inset 0 0 0 2px var(--series-1); }',
    '#view-parameters .sw.off { background: var(--muted); opacity: 0.5; }',
    /* weights sensitivity: one row per weight; inside a row the cells wrap onto further lines at narrow widths and
       never squeeze below ~16 characters (no clipping at 1200 px, no horizontal scroll at 400 px) */
    '#view-parameters .sens-rows { display: grid; gap: 8px; min-width: 0; }',
    '#view-parameters .sens-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 16ch), 1fr)); gap: 6px 12px; padding: 6px 0 0; border-top: 1px solid var(--grid); min-width: 0; }',
    '#view-parameters .sens-row .cell { min-width: 0; display: grid; gap: 2px; align-content: start; }',
    '#view-parameters .sens-row .cell .k { font-size: 0.74rem; color: var(--muted); }',
    '#view-parameters .sens-row .cell .v { font-size: 0.82rem; color: var(--ink); overflow-wrap: anywhere; }',
    '#view-parameters .sens-row ol { margin: 0; padding-left: 1.4em; font-family: var(--mono); font-size: 0.78rem; overflow-wrap: anywhere; }',
    '#view-parameters table.lb tr.envelope td { color: var(--ink-2); }',
    '#view-parameters table.lb tr.envelope td.sticky { box-shadow: inset 3px 0 0 var(--status-warning); }',
    '#view-parameters .lb-envelope { border-left: 3px solid var(--status-warning); }',
    '#view-parameters .lb-envelope ul { margin: 0; padding-left: 1.2em; display: grid; gap: 3px; font-size: 0.84rem; color: var(--ink-2); }',
    '#view-parameters .lb-envelope code { font-size: 0.8rem; overflow-wrap: anywhere; }',
    '#view-parameters .sw.env { background: var(--status-warning); }',
    '#view-parameters .lb-cov { font-size: 0.82rem; color: var(--ink-2); margin: 0; max-width: 92ch; }',
    '@media (max-width: 480px) { #view-parameters .pv-controls label, #view-parameters select { width: 100%; } #view-parameters .pv-panels figure.chart .plot { height: 200px; } }'
  ].join('\n');

  function injectCss() {
    if (document.getElementById('hc-view-parameters-css')) return;
    var s = document.createElement('style');
    s.id = 'hc-view-parameters-css';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  // ------------------------------------------------------------------------------------------------ model
  var state = { comp: null, day: 'all', hx: null, hy: null };
  var filter = { set: null, label: '' };
  var selectListeners = [];
  var hooks = { redraw: null };

  function num(v) { return typeof v === 'number' && v === v; }
  function valueText(v) {
    if (v === null || v === undefined) return '-';
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    return String(v);
  }
  function shortKey(k) { return String(k).replace(/^(profile|new_parameter|state)\s+/, ''); }

  function compOf(ps, key) {
    var cs = ps.components || [];
    for (var i = 0; i < cs.length; i++) if (cs[i].key === key) return cs[i];
    return cs[0];
  }
  function tieOn(ps, day, key) {
    var f = (ps.floors || {})[day];
    return f && f.tie && num(f.tie[key]) ? f.tie[key] : null;
  }
  function shownDays(ps) {
    var floored = (ps.days || []).filter(function (d) { return (ps.floors || {})[d]; });
    return state.day === 'all' ? floored : floored.filter(function (d) { return d === state.day; });
  }
  function bandOf(ps, days, key) {
    var m = 0;
    days.forEach(function (d) { var t = tieOn(ps, d, key); if (num(t) && t > m) m = t; });
    return m;
  }
  function decimals(c) { return c && c.unit === 'loops' ? 0 : 2; }
  function fmtComp(c, v) { return fmt.signed(v, decimals(c)) + (c && c.unit === 'loops' ? ' loops' : ' U'); }

  /* Every (variant, day) point of the shown days on component `key`, plus base at the phone's values (0 by
     definition); `inside` = inside that day's tie band (2 x floor). */
  function points(ps, key) {
    var days = shownDays(ps);
    var out = [];
    var base = ps.base || {};
    days.forEach(function (d) {
      if ((base.days || []).indexOf(d) >= 0) out.push({ vhash: base.vhash, variant: base.variant, values: base.values || {}, day: d, y: 0, base: true, inside: true, tie: tieOn(ps, d, key) });
    });
    (ps.variants || []).forEach(function (v) {
      days.forEach(function (d) {
        var pd = (v.per_day || {})[d];
        if (!pd || !num(pd[key])) return;
        var t = tieOn(ps, d, key);
        out.push({ vhash: v.vhash, variant: v.variant, values: v.values || {}, day: d, y: pd[key], base: false,
                   inside: num(t) ? Math.abs(pd[key]) < t : false, tie: t, feasible: v.feasible });
      });
    });
    return out;
  }

  function triedValues(ps, p) {
    var seen = {}, out = [];
    function add(v) { var k = valueText(v); if (!seen[k]) { seen[k] = true; out.push(v); } }
    add(p.phone);
    (ps.variants || []).forEach(function (v) { add((v.values || {})[p.key]); });
    var numeric = out.every(num);
    if (numeric) out.sort(function (a, b) { return a - b; });
    else if (out.every(function (v) { return typeof v === 'boolean'; })) out.sort();
    return { values: out, numeric: numeric };
  }

  function cells(ps, px, py, key) {
    var tx = triedValues(ps, px), ty = triedValues(ps, py);
    var map = {};
    points(ps, key).forEach(function (pt) {
      var k = valueText(pt.values[px.key]) + '\u0001' + valueText(pt.values[py.key]);
      var c = map[k] || (map[k] = { sum: 0, n: 0, variants: {}, inside: 0, days: {} });
      c.sum += pt.y; c.n += 1; c.variants[pt.variant] = true; c.days[pt.day] = true;
      if (pt.inside) c.inside += 1;
    });
    var out = [];
    tx.values.forEach(function (xv, xi) {
      ty.values.forEach(function (yv, yi) {
        var c = map[valueText(xv) + '\u0001' + valueText(yv)];
        if (!c) return;
        out.push({ xi: xi, yi: yi, x: xv, y: yv, mean: c.sum / c.n, n: c.n, variants: Object.keys(c.variants),
                   days: Object.keys(c.days).length,
                   base: valueText(xv) === valueText(px.phone) && valueText(yv) === valueText(py.phone) });
      });
    });
    return { xs: tx.values, ys: ty.values, cells: out };
  }

  // ------------------------------------------------------------------------------------------------ colour
  function hexRgb(s) {
    var m = /^#?([0-9a-f]{6})$/i.exec(String(s || '').trim());
    if (!m) { m = /^#?([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(String(s || '').trim()); if (!m) return null; return [parseInt(m[1] + m[1], 16), parseInt(m[2] + m[2], 16), parseInt(m[3] + m[3], 16)]; }
    var n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function mix(a, b, f) {
    var x = hexRgb(a), y = hexRgb(b);
    if (!x || !y) return f < 0.5 ? a : b;
    f = Math.max(0, Math.min(1, f));
    return 'rgb(' + [0, 1, 2].map(function (i) { return Math.round(x[i] + (y[i] - x[i]) * f); }).join(',') + ')';
  }
  /* diverging blue (less insulin than base) - grey - red (more); grey inside the tie band. Loops: sequential blue. */
  function cellColor(t, c, v, band, M) {
    if (!num(v) || Math.abs(v) < band) return t['div-mid'];
    var f = M > band ? (Math.abs(v) - band) / (M - band) : 1;
    f = 0.35 + 0.65 * f;
    if (!c.insulin) return f < 0.6 ? mix(t['div-mid'], t['seq-3'], f / 0.6) : mix(t['seq-3'], t['seq-5'], (f - 0.6) / 0.4);
    return mix(t['div-mid'], v < 0 ? t['div-neg'] : t['div-pos'], f);
  }

  // ------------------------------------------------------------------------------------------------ chart options
  function tooltipBox(lines) {
    return lines.map(function (l, i) { return i === 0 ? '<b>' + esc(l) + '</b>' : esc(l); }).join('<br>');
  }
  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

  function panelOption(ps, p, t) {
    var c = compOf(ps, state.comp);
    var pts = points(ps, c.key);
    var tv = triedValues(ps, p);
    var days = shownDays(ps);
    var band = bandOf(ps, days, c.key);
    var xOf = function (v) { return tv.numeric ? v : valueText(v); };
    var means = tv.values.map(function (xv) {
      var ys = pts.filter(function (pt) { return valueText(pt.values[p.key]) === valueText(xv); }).map(function (pt) { return pt.y; });
      return ys.length ? [xOf(xv), ys.reduce(function (a, b) { return a + b; }, 0) / ys.length] : null;
    }).filter(Boolean);
    function sdata(filterFn) {
      return pts.filter(filterFn).map(function (pt) { return { value: [xOf(pt.values[p.key]), pt.y], pt: pt }; });
    }
    var span = tv.numeric && tv.values.length ? (tv.values[tv.values.length - 1] - tv.values[0]) : 0;
    var pad = tv.numeric ? (span > 0 ? span * 0.12 : Math.max(Math.abs(tv.values[0] || 1) * 0.1, 0.1)) : 0;
    var ext = Math.max(band * 1.2, 0);
    pts.forEach(function (pt) { ext = Math.max(ext, Math.abs(pt.y) * 1.1); });
    if (!(ext > 0)) ext = c.unit === 'loops' ? 2 : 0.1;
    var dp = decimals(c);
    return {
      grid: { left: 52, right: 14, top: 14, bottom: 34 },
      tooltip: { trigger: 'item', confine: true, formatter: function (it) {
        if (it.seriesName === 'mean per value') return tooltipBox([shortKey(p.key) + ' ' + valueText(it.value[0]), 'mean of the shown points ' + fmtComp(c, it.value[1])]);
        var pt = it.data && it.data.pt;
        if (!pt) return '';
        return tooltipBox([pt.base ? 'base (the phone\'s settings)' : pt.variant, pt.day + ', ' + shortKey(p.key) + ' ' + valueText(pt.values[p.key]),
          c.label + ' ' + fmtComp(c, pt.y),
          num(pt.tie) ? (pt.inside ? 'inside the floor band (tie below ' + fmt.num(pt.tie, dp) + ')' : 'outside the floor band (' + fmt.num(Math.abs(pt.y) / (pt.tie / ps.tie_factor), 1) + ' x floor)') : 'no floor on this day']);
      } },
      xAxis: tv.numeric
        ? { type: 'value', min: tv.values[0] - pad, max: tv.values[tv.values.length - 1] + pad, splitLine: { show: false },
            axisLabel: { formatter: function (v) { return tv.values.some(function (x) { return Math.abs(x - v) < 1e-9; }) ? valueText(v) : ''; }, showMinLabel: false, showMaxLabel: false } }
        : { type: 'category', data: tv.values.map(valueText), boundaryGap: true },
      yAxis: { type: 'value', min: -ext, max: ext, name: c.unit, nameGap: 8, nameTextStyle: { align: 'right' },
               axisLabel: { formatter: function (v) { return fmt.num(v, dp === 0 ? 0 : (ext < 0.5 ? 2 : 1)); } }, splitNumber: 4 },
      series: [
        { name: 'mean per value', type: 'line', data: means, symbol: 'none', z: 2,
          lineStyle: { color: t['series-1'], width: 2 }, itemStyle: { color: t['series-1'] },
          markArea: band > 0 ? { silent: true, itemStyle: { color: t.grid, opacity: 0.75 }, label: { show: false },
                                 data: [[{ yAxis: -band }, { yAxis: band }]] } : undefined,
          markLine: { silent: true, symbol: 'none', animation: false,
                      lineStyle: { color: t.muted, width: 1, type: 'solid' },
                      label: { color: t['ink-2'], fontSize: 10, formatter: 'phone', position: 'insideEndTop' },
                      data: [{ xAxis: xOf(p.phone) }] } },
        { name: 'inside the floor band', type: 'scatter', data: sdata(function (pt) { return !pt.base && pt.inside; }), symbolSize: 8, z: 3,
          itemStyle: { color: t.phone, borderColor: t.surface, borderWidth: 1 } },
        { name: 'outside the floor band', type: 'scatter', data: sdata(function (pt) { return !pt.base && !pt.inside; }), symbolSize: 9, z: 4,
          itemStyle: { color: t['series-1'], borderColor: t.surface, borderWidth: 1 } },
        { name: 'base', type: 'scatter', data: sdata(function (pt) { return pt.base; }), symbolSize: 11, z: 5,
          itemStyle: { color: t.surface, borderColor: t.ink, borderWidth: 2 } }
      ]
    };
  }

  function heatOption(ps, model, t, plot) {
    var c = compOf(ps, state.comp);
    var px = paramByKey(ps, state.hx), py = paramByKey(ps, state.hy);
    var band = bandOf(ps, shownDays(ps), c.key);
    var M = band * 1.5;
    model.cells.forEach(function (cl) { M = Math.max(M, Math.abs(cl.mean)); });
    if (!(M > 0)) M = 1;
    var w = (plot && plot.clientWidth) || 600;
    var ph = (plot && plot.clientHeight) || 260;
    var cellW = (w - 76 - 16) / Math.max(1, model.xs.length);
    var cellH = (ph - 18 - 40) / Math.max(1, model.ys.length);
    var wide = cellW >= 58 && cellH >= 24;
    var dp = decimals(c);
    return {
      grid: { left: 76, right: 16, top: 18, bottom: 40 },
      tooltip: { trigger: 'item', confine: true, formatter: function (it) {
        var cl = it.data && it.data.cell;
        if (!cl) return '';
        var lines = [shortKey(px.key) + ' ' + valueText(cl.x) + ', ' + shortKey(py.key) + ' ' + valueText(cl.y),
          c.label + ' ' + fmtComp(c, cl.mean) + (cl.n > 1 ? ' (mean of ' + cl.n + ' points)' : ''),
          Math.abs(cl.mean) < band ? 'inside the floor band: a tie' : 'outside the floor band',
          cl.variants.length + ' variant(s) on ' + cl.days + ' day(s)'];
        if (cl.base) lines.push('the phone\'s values');
        return tooltipBox(lines);
      } },
      xAxis: { type: 'category', data: model.xs.map(valueText), splitArea: { show: false }, axisLine: { show: false },
               name: shortKey(px.key), nameLocation: 'middle', nameGap: 26 },
      yAxis: { type: 'category', data: model.ys.map(valueText), splitArea: { show: false }, axisLine: { show: false },
               name: shortKey(py.key), nameLocation: 'end', nameGap: 6, nameTextStyle: { align: 'left' } },
      series: [{
        type: 'heatmap', progressive: 0,
        data: model.cells.map(function (cl) {
          return { value: [cl.xi, cl.yi, cl.mean], cell: cl,
                   itemStyle: { color: cellColor(t, c, cl.mean, band, M), borderColor: cl.base ? t.ink : t.surface, borderWidth: 2, borderRadius: 0 } };
        }),
        label: { show: wide, color: t.ink, fontFamily: t.font, fontSize: 11,
                 formatter: function (it) { return fmt.signed(it.value[2], dp); } },
        emphasis: { itemStyle: { borderColor: t.ink, borderWidth: 2 } }
      }]
    };
  }

  function paramByKey(ps, key) {
    var a = ps.params || [];
    for (var i = 0; i < a.length; i++) if (a[i].key === key) return a[i];
    return a[0];
  }

  // ------------------------------------------------------------------------------------------------ sections
  function controls(ps, onChange) {
    var comps = ps.components || [];
    var cSel = h('select', { id: 'pv-component', onchange: function () { state.comp = cSel.value; onChange(); } },
      comps.map(function (c) { return h('option', { value: c.key, selected: c.key === state.comp || null, text: c.label + ' (' + c.unit + ')' }); }));
    var days = (ps.days || []).filter(function (d) { return (ps.floors || {})[d]; });
    var dSel = h('select', { id: 'pv-day', onchange: function () { state.day = dSel.value; onChange(); } },
      h('option', { value: 'all', text: days.length > 1 ? 'every search day (one point per day)' : 'every search day' }),
      days.map(function (d) { return h('option', { value: d, selected: d === state.day || null, text: d }); }));
    return h('div', { class: 'pv-controls', role: 'group', 'aria-label': 'what the charts show' },
      h('label', { for: 'pv-component' }, 'Effect vs base', cSel),
      h('label', { for: 'pv-day' }, 'Days', dSel));
  }

  function legendRow() {
    return h('div', { class: 'pv-legend' },
      h('span', null, h('i', { class: 'sw var' }), 'variant on a day, outside the floor band'),
      h('span', null, h('i', { class: 'sw tie' }), 'inside the floor band (a tie)'),
      h('span', null, h('i', { class: 'sw base' }), 'base: the phone\'s settings, 0 by definition'),
      h('span', null, h('i', { class: 'sw mean' }), 'mean per value'),
      h('span', null, h('i', { class: 'sw band' }), 'floor band: twice the base-vs-phone floor'));
  }

  function panelTwin(ps, p) {
    var c = compOf(ps, state.comp);
    var dp = decimals(c);
    var rows = points(ps, c.key).map(function (pt) {
      return { variant: pt.base ? 'base' : pt.variant, day: pt.day, value: valueText(pt.values[p.key]), y: pt.y, tie: pt.tie,
               reading: pt.base ? 'reference' : (pt.inside ? 'inside the floor band (tie)' : 'outside the floor band') };
    });
    return {
      summary: 'Table of the points in this panel',
      columns: [
        { key: 'value', label: shortKey(p.key), numeric: true },
        { key: 'variant', label: 'variant' },
        { key: 'day', label: 'day' },
        { key: 'y', label: c.label + ' (' + c.unit + ')', numeric: true, format: function (v) { return fmt.signed(v, dp); } },
        { key: 'tie', label: 'tie below (' + c.unit + ')', numeric: true, format: function (v) { return fmt.num(v, dp); } },
        { key: 'reading', label: 'reading' }
      ],
      rows: rows
    };
  }

  function renderPanels(el, ps) {
    var params = ps.params || [];
    var block = h('div', { class: 'block' },
      h('header', null, h('h2', { text: 'Parameters' }),
        h('p', { text: 'One panel per searched parameter. x is the value tried, y the effect vs base on each day. Points inside the shaded floor band are ties with base; a panel with every point inside the band is a parameter not testable on these days.' })),
      legendRow());
    var grid = h('div', { class: 'pv-panels' });
    block.appendChild(grid);
    el.appendChild(block);
    if (!params.length) { grid.appendChild(h('p', { class: 'placeholder', text: 'The study searches no parameters.' })); return []; }
    var recs = [];
    params.forEach(function (p) {
      var tv = triedValues(ps, p);
      var fig = HC.figure(p.key, 'phone ' + valueText(p.phone) + '; tried ' + tv.values.map(valueText).join(', '));
      grid.appendChild(fig);
      var rec = HC.chart(fig, function (t) { return panelOption(ps, p, t); });
      HC.tableTwin(fig, panelTwin(ps, p));
      recs.push({ rec: rec, fig: fig, twin: function () { return panelTwin(ps, p); } });
    });
    return recs;
  }

  function heatTwin(ps, model) {
    var c = compOf(ps, state.comp), px = paramByKey(ps, state.hx), py = paramByKey(ps, state.hy);
    var band = bandOf(ps, shownDays(ps), c.key);
    var dp = decimals(c);
    return {
      summary: 'Table of the cells in this heatmap',
      columns: [
        { key: 'x', label: shortKey(px.key), numeric: true },
        { key: 'y', label: shortKey(py.key), numeric: true },
        { key: 'mean', label: c.label + ' (' + c.unit + ')', numeric: true, format: function (v) { return fmt.signed(v, dp); } },
        { key: 'n', label: 'points', numeric: true, format: fmt.int },
        { key: 'reading', label: 'reading' }
      ],
      rows: model.cells.map(function (cl) {
        return { x: valueText(cl.x), y: valueText(cl.y), mean: cl.mean, n: cl.n,
                 reading: (cl.base ? 'the phone\'s values; ' : '') + (Math.abs(cl.mean) < band ? 'inside the floor band (tie)' : 'outside the floor band') };
      })
    };
  }

  function scaleKey(ps, model) {
    var c = compOf(ps, state.comp);
    var band = bandOf(ps, shownDays(ps), c.key);
    var M = band * 1.5;
    model.cells.forEach(function (cl) { M = Math.max(M, Math.abs(cl.mean)); });
    var dp = decimals(c);
    var u = c.unit === 'loops' ? ' loops' : ' U';
    if (!c.insulin) {
      return h('div', { class: 'pv-scale', 'aria-hidden': 'true' },
        h('span', { text: 'tie, below ' + fmt.num(band, dp) + u }), h('span', { class: 'bar seq' }), h('span', { text: fmt.num(M, dp) + u + ' apart from base' }),
        h('span', { class: 'base-key' }, h('i'), 'outlined cell: the phone\'s values (base)'));
    }
    return h('div', { class: 'pv-scale', 'aria-hidden': 'true' },
      h('span', { text: '−' + fmt.num(M, dp) + u + ' less than base' }), h('span', { class: 'bar div' }),
      h('span', { text: '+' + fmt.num(M, dp) + u + ' more' }),
      h('span', { class: 'base-key' }, h('i'), 'outlined cell: the phone\'s values (base); grey: inside the floor band, ±' + fmt.num(band, dp) + u));
  }

  function renderHeat(el, ps) {
    var params = ps.params || [];
    var block = h('div', { class: 'block' },
      h('header', null, h('h2', { text: 'Delta heatmap' }),
        h('p', { text: 'The mean effect vs base for each pair of values of two parameters. Blue: the loop would have asked for less insulin than base; red: more; grey: inside the floor band.' })));
    el.appendChild(block);
    if (params.length < 2) {
      block.appendChild(h('p', { class: 'placeholder', text: 'The heatmap needs two searched parameters; this study searches ' + params.length + '.' }));
      return null;
    }
    if (!state.hx || !paramByKey(ps, state.hx) || paramByKey(ps, state.hx).key !== state.hx) state.hx = params[0].key;
    if (!state.hy || state.hy === state.hx) state.hy = (params[1].key === state.hx ? params[0] : params[1]).key;
    var fig = h('figure', { class: 'chart' });
    var api = {};
    function pick(id, label, which) {
      var sel = h('select', { id: id, onchange: function () {
        state[which] = sel.value;
        var other = which === 'hx' ? 'hy' : 'hx';
        if (state[other] === state[which]) {
          state[other] = params.filter(function (p) { return p.key !== state[which]; })[0].key;
          document.getElementById(other === 'hx' ? 'pv-hx' : 'pv-hy').value = state[other];
        }
        api.redraw();
      } }, params.map(function (p) { return h('option', { value: p.key, selected: p.key === state[which] || null, text: p.key }); }));
      return h('label', { for: id }, label, sel);
    }
    var note = h('span', { class: 'note' });
    fig.appendChild(h('figcaption', null, h('span', { class: 'title', text: 'Two parameters' }), note));
    fig.appendChild(h('div', { class: 'pv-controls' }, pick('pv-hx', 'Columns (x)', 'hx'), pick('pv-hy', 'Rows (y)', 'hy')));
    var plot = h('div', { class: 'plot' });
    fig.appendChild(plot);
    var keyHolder = h('div');
    fig.appendChild(keyHolder);
    block.appendChild(fig);
    var model = null;
    function sizePlot() {
      var ny = model ? model.ys.length : 2;
      plot.style.height = Math.max(180, Math.min(440, 64 + ny * 46)) + 'px';
    }
    function rebuildModel() {
      model = cells(ps, paramByKey(ps, state.hx), paramByKey(ps, state.hy), compOf(ps, state.comp).key);
      note.textContent = params.length > 2 ? 'a cell averages every point with these two values over the other parameters' : 'one cell per pair of values tried';
      HC.clear(keyHolder).appendChild(scaleKey(ps, model));
      sizePlot();
    }
    rebuildModel();
    var rec = HC.chart(fig, function (t) { return heatOption(ps, model, t, plot); });
    HC.tableTwin(fig, heatTwin(ps, model));
    var lastWide = null;
    if (window.ResizeObserver) {                        // cell labels only when the cells are wide enough
      new ResizeObserver(function () {
        var w = plot.clientWidth;
        if (!model || !w) return;
        var wide = (w - 92) / Math.max(1, model.xs.length) >= 58;
        if (lastWide !== null && wide !== lastWide && rec.chart) rec.chart.setOption(rec.build(HC.theme.current(), window.echarts), { notMerge: true });
        lastWide = wide;
      }).observe(plot);
    }
    api.redraw = function () {
      rebuildModel();
      if (rec.chart) { rec.chart.resize(); rec.chart.setOption(rec.build(HC.theme.current(), window.echarts), { notMerge: true }); }
      HC.tableTwin(fig, heatTwin(ps, model));
    };
    return api;
  }

  // ------------------------------------------------------------------------------------------------ Pareto (earlier / gentler)
  /* DESIGN.md 5.3 rule 3: the front of the three objectives, the review order under the declared weights and the
     weights-sensitivity panel. x / y are the first two objectives, the third is in the tooltip, the label and the table. */
  function directionText(a) { return a.direction === 'up' ? 'larger is what the question asks for' : 'smaller is what the question asks for'; }

  function paretoOption(pa, t) {
    var ax = pa.axes || [];
    var pts = pa.points || [];
    function data(fn) { return pts.filter(fn).map(function (p) { return { value: [p.x, p.y], p: p }; }); }
    var xs = pts.map(function (p) { return p.x; }).concat([0]), ys = pts.map(function (p) { return p.y; }).concat([0]);
    var xr = Math.max.apply(null, xs) - Math.min.apply(null, xs), yr = Math.max.apply(null, ys) - Math.min.apply(null, ys);
    var xpad = (xr || 0.1) * 0.08, ypad = (yr || 0.1) * 0.08;
    function tip(it) {
      var p = it.data && it.data.p;
      if (!p) return it.seriesName === 'base' ? tooltipBox(['base (the phone\'s settings, replayed)', 'the origin of every axis']) : '';
      return tooltipBox([p.variant + (p.front ? ' (front)' : ''),
        (ax[0] ? ax[0].label : 'x') + ' ' + fmt.signed(p.x, 2),
        (ax[1] ? ax[1].label : 'y') + ' ' + fmt.num(p.y, 2),
        (ax[2] ? ax[2].label : 'z') + ' ' + fmt.num(p.z, 1),
        p.feasible ? 'review order ' + (p.rank || '-') + ', scalar ' + fmt.num(p.scalar, 2) + (p.step === false ? ' (no step: a tie for this question)' : '')
          : p.envelope_limited ? 'envelope-limited: beyond what the replay can judge on some days, never ranked' : 'infeasible: never ranked']);
    }
    return {
      grid: { left: 56, right: 18, top: 18, bottom: 46 },
      tooltip: { trigger: 'item', confine: true, formatter: tip },
      xAxis: { type: 'value', name: ax[0] ? ax[0].short || ax[0].label : '', nameLocation: 'middle', nameGap: 28, min: function (v) { return +(Math.min(v.min, 0) - xpad).toFixed(3); }, max: function (v) { return +(Math.max(v.max, 0) + xpad).toFixed(3); },
               splitLine: { lineStyle: { color: t.grid } }, axisLabel: { showMinLabel: false, showMaxLabel: false, formatter: function (v) { return fmt.num(v, xr < 1 ? 2 : 1); } } },
      yAxis: { type: 'value', name: ax[1] ? ax[1].short || ax[1].label : '', nameLocation: 'end', nameGap: 8, nameTextStyle: { align: 'left' },
               min: function (v) { return +(Math.min(v.min, 0) - ypad).toFixed(3); }, max: function (v) { return +(Math.max(v.max, 0) + ypad).toFixed(3); },
               splitLine: { lineStyle: { color: t.grid } }, axisLabel: { showMinLabel: false, showMaxLabel: false, formatter: function (v) { return fmt.num(v, yr < 1 ? 2 : 1); } } },
      series: [
        { name: 'infeasible', type: 'scatter', data: data(function (p) { return !p.feasible && !p.envelope_limited; }), symbolSize: 7, z: 2,
          itemStyle: { color: t.muted, opacity: 0.5 } },
        { name: 'envelope-limited', type: 'scatter', data: data(function (p) { return p.envelope_limited; }), symbolSize: 8, z: 2,
          itemStyle: { color: t['status-warning'], opacity: 0.85 } },
        { name: 'feasible, behind the front', type: 'scatter', data: data(function (p) { return p.feasible && !p.front; }), symbolSize: 9, z: 3,
          itemStyle: { color: t.surface, borderColor: t['series-1'], borderWidth: 2 } },
        { name: 'front', type: 'scatter', data: data(function (p) { return p.front; }), symbolSize: 12, z: 4,
          itemStyle: { color: t['series-1'], borderColor: t.ink, borderWidth: 1.5 },
          label: { show: true, position: 'right', color: t['ink-2'], fontSize: 10, formatter: function (it) { return it.data.p.rank ? '#' + it.data.p.rank : ''; } } },
        { name: 'base', type: 'scatter', data: [{ value: [0, 0] }], symbolSize: 13, z: 5,
          itemStyle: { color: t.surface, borderColor: t.ink, borderWidth: 2 },
          label: { show: true, position: 'bottom', color: t['ink-2'], fontSize: 10, formatter: 'phone' },
          markLine: { silent: true, symbol: 'none', animation: false, lineStyle: { color: t.axis, width: 1, type: 'solid' }, label: { show: false },
                      data: [{ xAxis: 0 }, { yAxis: 0 }] } }
      ]
    };
  }

  function paretoTwin(pa) {
    var ax = pa.axes || [];
    return {
      summary: 'Table of the points in this chart',
      columns: [
        { key: 'rank', label: 'review order', numeric: true, format: function (v) { return v ? String(v) : '-'; } },
        { key: 'variant', label: 'variant' },
        { key: 'x', label: ax[0] ? ax[0].label : 'x', numeric: true, format: function (v) { return fmt.signed(v, 2); } },
        { key: 'y', label: ax[1] ? ax[1].label : 'y', numeric: true, format: function (v) { return fmt.num(v, 2); } },
        { key: 'z', label: ax[2] ? ax[2].label : 'z', numeric: true, format: function (v) { return fmt.num(v, 1); } },
        { key: 'scalar', label: 'scalar', numeric: true, format: function (v) { return fmt.num(v, 2); } },
        { key: 'reading', label: 'reading' }
      ],
      rows: [{ rank: null, variant: 'base', x: 0, y: 0, z: 0, scalar: null, reading: 'the phone\'s settings, replayed: the origin' }].concat(
        (pa.points || []).map(function (p) {
          return { rank: p.rank, variant: p.variant, x: p.x, y: p.y, z: p.z, scalar: p.scalar,
                   reading: p.envelope_limited ? 'envelope-limited, never ranked' : !p.feasible ? 'infeasible, never ranked' : p.front ? 'on the front' : p.step === false ? 'no step: a tie for this question' : 'feasible step, behind the front' };
        }))
    };
  }

  function weightsText(pa) {
    var w = pa.weights || {};
    return Object.keys(w).map(function (k) { return k + ' ' + fmt.num(w[k], 1); }).join(', ');
  }

  function sensitivityPanel(pa) {
    var se = pa.sensitivity || {};
    var box = h('div', { class: 'lb-validation', role: 'note', 'aria-label': 'weights sensitivity' },
      h('p', { class: 'lead', text: 'Weights sensitivity' }),
      h('p', { text: se.note || '' }));
    if (!se.available) return box;
    var n = (se.declared_top || []).length;
    function cell(key, body) { return h('div', { class: 'cell' }, h('span', { class: 'k', text: key }), body); }
    function list(l) {
      if (!l) return h('span', { class: 'v', text: 'not recorded' });
      if (!l.length) return h('span', { class: 'v', text: 'none feasible' });
      return h('ol', null, l.map(function (x) { return h('li', { text: x }); }));
    }
    // one row per weight (a list, not a table): the cells wrap at narrow widths instead of squeezing the columns
    var rows = h('div', { class: 'sens-rows', role: 'list' });
    (se.weights || []).forEach(function (r) {
      rows.appendChild(h('div', { class: 'sens-row', role: 'listitem', 'aria-label': 'weight ' + r.weight },
        cell('weight', h('span', { class: 'v' }, h('code', { text: r.weight }), ' ' + fmt.num(r.value, 1) + ' declared')),
        cell('top ' + n + ' unchanged', h('span', { class: 'v' }, h('span', { class: 'pill', text: r.same_top ? 'yes' : 'no, it moves' }),
          r.tau && Object.keys(r.tau).length ? h('span', { class: 'vline', text: 'Kendall tau ' + Object.keys(r.tau).map(function (f) { return f + ' x ' + fmt.num(r.tau[f], 2); }).join(', ') }) : null)),
        cell('top ' + n + ' at 0.5 x', list(r['0.5'])),
        cell('top ' + n + ' at 1.5 x', list(r['1.5']))));
    });
    box.appendChild(h('p', { text: 'Scalar order at the declared weights, top ' + n + ': ' + (se.declared_top || []).join(', ') }));
    box.appendChild(rows);
    return box;
  }

  function renderPareto(el, ps) {
    var pa = ps.pareto;
    if (!pa) return null;
    var ax = pa.axes || [];
    var nFront = (pa.points || []).filter(function (p) { return p.front; }).length;
    var block = h('div', { class: 'block', id: 'pv-pareto' },
      h('header', null, h('h2', { text: 'Pareto front' }),
        h('p', { text: 'The question ' + pa.question + ' is a declared study setting, not a finding. Every feasible variant as the mean over the search days vs base; the phone\'s settings, replayed, sit at the origin. ' +
          ax.map(function (a) { return a.label + ': ' + directionText(a); }).join('; ') + '. ' + nFront + ' variant(s) on the front, taken among the steps (rise beyond the floor in the question\'s direction): no other step asks for at least as much on all three at once.' })),
      h('div', { class: 'pv-legend' },
        h('span', null, h('i', { class: 'sw front' }), 'front member, labelled with its review order'),
        h('span', null, h('i', { class: 'sw other' }), 'feasible, behind the front'),
        h('span', null, h('i', { class: 'sw env' }), 'envelope-limited, never ranked'),
        h('span', null, h('i', { class: 'sw off' }), 'infeasible, never ranked'),
        h('span', null, h('i', { class: 'sw base' }), 'phone: base at the origin')));
    var grid = h('div', { class: 'pf-grid' });
    block.appendChild(grid);
    var fig = HC.figure('Front of the declared question', ax[0] && ax[1] && ax[2] ? 'x: ' + ax[0].label + '; y: ' + ax[1].label + '; ' + ax[2].label + ' in the tooltip and the table' : '');
    grid.appendChild(fig);
    var side = h('div', { class: 'pf-side' });
    var order = pa.order || [];
    side.appendChild(h('div', { class: 'lb-validation', role: 'note', 'aria-label': 'review order' },
      h('p', { class: 'lead', text: 'Review order under the declared weights' }),
      h('p', { class: 'pf-weights', text: pa.scalar_text + '   (weights: ' + weightsText(pa) + ')' }),
      h('p', { text: pa.order_text || '' }),
      order.length ? h('ol', { class: 'pf-order' }, order.slice(0, 10).map(function (o) {
        return h('li', null, h('code', { text: o.variant }), o.front ? ' ' : null, o.front ? h('span', { class: 'pill', text: 'front' }) : null,
          h('span', { class: 'sc', text: 'scalar ' + fmt.num(o.scalar, 2) }));
      })) : h('p', { text: 'No feasible variant yet.' }),
      order.length > 10 ? h('p', { text: (order.length - 10) + ' more in the leaderboard below.' }) : null));
    side.appendChild(sensitivityPanel(pa));
    grid.appendChild(side);
    el.appendChild(block);
    var rec = HC.chart(fig, function (t) { return paretoOption(pa, t); });
    HC.tableTwin(fig, paretoTwin(pa));
    return rec;
  }

  // ------------------------------------------------------------------------------------------------ leaderboard
  /* short column heads; the full label is the header's title */
  var SHORT = {
    rise_vs_base_U: 'rise vs base', rest_vs_base_U: 'rest vs base', delta_vs_base_U: 'total vs base',
    smb_vs_base_U: 'SMB vs base', tbr_vs_base_U: 'TBR vs base', changed_vs_base: 'loops changed vs base',
    smb_more_vs_base: 'larger SMB, loops', smb_less_vs_base: 'smaller SMB, loops', lowside_flags: 'low-side flags',
    prehypo_extra_U: '2 h before a low', env_3h_max_U: '3 h envelope', env_day_pct_tdd: 'day, % TDD',
    zero_temp_vs_base: 'zero temps vs base', cap_binding_pct: 'on a cap', max_single_smb_U: 'largest SMB'
  };
  function colHead(x) { return (SHORT[x.key] || x.label) + ' (' + x.unit + ')'; }

  function bandCell(c, v, tie, M) {
    var dp = decimals(c);
    var inside = !num(v) || (num(tie) && Math.abs(v) < tie);
    var td = h('td', { class: 'n dcell', title: num(tie) ? 'floor band ±' + fmt.num(tie, dp) + ' ' + c.unit + (inside ? ': a tie' : ': outside the band') : null });
    td.appendChild(h('span', { class: 'dv num' + (inside ? ' tie' : ''), text: fmt.signed(v, dp) }));
    if (num(v) && M > 0) {
      var bw = num(tie) ? Math.min(100, tie / M * 100) : 0;
      var pos = 50 + Math.max(-1, Math.min(1, v / M)) * 50;
      var cls = inside ? 'mk' : !c.insulin ? 'mk loops' : v < 0 ? 'mk neg' : 'mk pos';
      td.appendChild(h('span', { class: 'fb', 'aria-hidden': 'true' },
        h('span', { class: 'band', style: 'left:' + (50 - bw / 2).toFixed(1) + '%;width:' + bw.toFixed(1) + '%' }),
        h('span', { class: 'zero' }),
        h('span', { class: cls, style: 'left:' + pos.toFixed(1) + '%' })));
    }
    return td;
  }

  function verdictPill(v) {
    var word = v.verdict === 'recommended' ? 'for review' : v.verdict;
    // status colours are for feasibility only (DESIGN 6): a verdict is a review order, so it stays a neutral pill
    // envelope-limited carries its status colour in the feasibility column and the row marker, not here
    var cls = v.verdict === 'infeasible' ? 'pill serious' : 'pill';
    return h('span', { class: cls, text: word });
  }

  function envelopeDaysText(ed) {
    return Object.keys(ed || {}).sort(function (a, b) {
      return Math.abs((ed[b] || {}).env_3h_U || 0) - Math.abs((ed[a] || {}).env_3h_U || 0) || (a < b ? -1 : 1);
    }).map(function (d) {
      var r = ed[d] || {};
      return d + (num(r.env_3h_U) ? ' (' + fmt.signed(r.env_3h_U, 1) + ' U in 3 h)' : num(r.env_day_pct_tdd) ? ' (' + fmt.num(r.env_day_pct_tdd, 0) + ' % of TDD)' : '');
    }).join(', ');
  }

  function feasibilityText(v) {
    if (v.feasible === true) return h('span', { class: 'pill good', text: 'feasible' });
    if (v.verdict === 'envelope-limited') {
      var n = Object.keys(v.envelope_days || {}).length;
      return h('span', null, h('span', { class: 'pill warning', text: 'beyond the envelope on ' + n + (n === 1 ? ' day' : ' days') }), ' ',
        'beyond what the replay can judge on: ' + envelopeDaysText(v.envelope_days) + '; every hard check passes on every day');
    }
    var reasons = v.reasons || {};
    var days = Object.keys(reasons).sort();
    if (v.feasible === false && days.length) {
      return h('span', null, h('span', { class: 'pill serious', text: 'infeasible' }), ' ',
        days.map(function (d) { return d + ': ' + reasons[d].join(', '); }).join('; '));
    }
    return h('span', { class: 'muted', text: v.feasible === false ? 'infeasible' : 'not assessed' });
  }

  function renderLeaderboard(el, ps) {
    var explore = ps.question === 'explore';
    var params = ps.params || [];
    var comps = ps.components || [];
    var extras = ps.extras || [];
    var days = (ps.days || []).filter(function (d) { return (ps.floors || {})[d]; });
    var head = h('div', { class: 'lb-head' },
      h('div', { class: 'eyebrow', text: 'Study question: ' + ps.question + (explore ? '' : ' (a declared study setting, not a finding)') }),
      h('p', { class: 'order', text: explore ? 'Review order, not a ranking by outcome' : 'Ranked by the declared question; ties within the floor are marked' }),
      h('ul', { class: 'lb-rule' }, (ps.rule_lines || []).filter(function (l, i) {
        return !(i === 0 && explore && /^Review order, not a ranking by outcome\.?$/.test(l));      // printed as the header
      }).map(function (l) { return h('li', { text: l }); })));
    var vlines = ps.validation_lines || [];
    if (vlines.length) {                                  // report.validation_lines: the validate round, a caveat with its day count
      head.appendChild(h('div', { class: 'lb-validation', role: 'note', 'aria-label': 'hold-out validation' },
        h('p', { class: 'lead', text: vlines[0] }),
        vlines.slice(1).map(function (l) { return h('p', { text: l }); })));
    }
    var env = ps.envelope || null;
    if (env && (env.items || []).length) {                // after the candidates, before the infeasible ones: the days
      head.appendChild(h('div', { class: 'lb-validation lb-envelope', role: 'note', 'aria-label': 'envelope-limited variants' },
        h('p', { class: 'lead', text: (env.items || []).length + ' envelope-limited variant(s)' }),
        h('p', { text: env.lead || '' }),
        h('ul', null, (env.items || []).slice(0, 12).map(function (it) {
          return h('li', null, h('code', { text: it.variant }), ': ' + it.days_text + (it.inside_days ? '; inside on ' + it.inside_days + ' other day(s)' : ''));
        })),
        (env.items || []).length > 12 ? h('p', { text: ((env.items || []).length - 12) + ' more in the leaderboard below.' }) : null));
    }
    var cov = ps.coverage || null;
    var covCols = cov ? (cov.columns || []) : [];
    if (cov) head.appendChild(h('p', { class: 'lb-cov', text: 'The columns "' + covCols.map(function (x) { return x.head; }).join('" and "') + '" read the manual boluses of the search days: ' + cov.caveat }));
    var chip = h('div', { class: 'filter-chip', hidden: true });
    var box = h('div', { class: 'lb-box' });
    var block = h('div', { class: 'block' },
      h('header', null, h('h2', { text: 'Leaderboard' }),
        h('p', { text: 'Every component as the mean over ' + (days.length === 1 ? 'the search day' : 'the ' + days.length + ' search days') + ' vs base. The small track in a delta cell shows the value against the shaded floor band; grey numbers are ties. Envelope-limited variants (after the ranked ones, marked in yellow) pass every hard check but leave the validity envelope on some days, listed with them; infeasible variants are greyed with the reason. Neither is ranked.' })),
      head, chip, box);
    el.appendChild(block);

    function draw() {
      var rows = ps.variants || [];
      if (filter.set) rows = rows.filter(function (v) { return filter.set.has ? filter.set.has(v.vhash) : filter.set.indexOf(v.vhash) >= 0; });
      HC.clear(chip);
      chip.hidden = !filter.set;
      if (filter.set) {
        chip.appendChild(h('span', { text: 'showing ' + rows.length + ' of ' + (ps.variants || []).length + ' variants' + (filter.label ? ' (' + filter.label + ')' : '') }));
        chip.appendChild(h('button', { type: 'button', class: 'link', text: 'show all', onclick: function () { api.setFilter(null); } }));
      }
      var ties = {}, scale = {};
      comps.forEach(function (c) {
        ties[c.key] = bandOf(ps, days, c.key);
        var m = ties[c.key] * 1.5;
        (ps.variants || []).forEach(function (v) { var x = (v.mean || {})[c.key]; if (num(x)) m = Math.max(m, Math.abs(x)); });
        scale[c.key] = m > 0 ? m : 1;
      });
      var tr = h('tr');
      tr.appendChild(h('th', { class: 'n short', text: explore ? 'review order' : 'rank' }));
      tr.appendChild(h('th', { class: 'sticky', text: 'variant' }));
      tr.appendChild(h('th', { text: 'verdict' }));
      params.forEach(function (p) { tr.appendChild(h('th', { class: 'n', title: p.key, text: shortKey(p.key) })); });
      tr.appendChild(h('th', { class: 'n short', text: 'days' }));
      comps.forEach(function (c) {
        tr.appendChild(h('th', { class: 'n', title: c.label + '; floor band ±' + fmt.num(ties[c.key], decimals(c)) + ' ' + c.unit + ' (widest search day)', text: colHead(c) }));
      });
      tr.appendChild(h('th', { class: 'n', title: '|effect| / floor on the review component: the mean over the days of each day\'s multiple (the review order key)', text: 'x floor' }));
      extras.forEach(function (x) { tr.appendChild(h('th', { class: 'n', title: x.label, text: colHead(x) })); });
      covCols.forEach(function (x) { tr.appendChild(h('th', { class: 'n', title: x.label + '. ' + cov.caveat, text: x.head })); });
      tr.appendChild(h('th', { text: 'feasibility' }));
      tr.appendChild(h('th', { text: 'why' }));
      var tbody = h('tbody');
      var base = ps.base || {};
      var btr = h('tr', { class: 'base' });
      btr.appendChild(h('td', { class: 'n', text: '-' }));
      btr.appendChild(h('td', { class: 'sticky' }, h('div', { class: 'vlabel' }, h('b', { text: 'base' }), h('span', { class: 'sha', text: 'the phone\'s settings, replayed' }))));
      btr.appendChild(h('td', null, h('span', { class: 'pill', text: 'reference' })));
      params.forEach(function (p) { btr.appendChild(h('td', { class: 'n param', text: valueText(p.phone) })); });
      btr.appendChild(h('td', { class: 'n', text: fmt.int((base.days || []).length) }));
      comps.forEach(function (c) { btr.appendChild(bandCell(c, (base.days || []).length ? 0 : null, ties[c.key], scale[c.key])); });
      btr.appendChild(h('td', { class: 'n', text: '-' }));
      extras.forEach(function () { btr.appendChild(h('td', { class: 'n muted', text: '-' })); });
      covCols.forEach(function () { btr.appendChild(h('td', { class: 'n muted', text: '-' })); });
      btr.appendChild(h('td', { class: 'muted', text: 'the floor is measured here' }));
      btr.appendChild(h('td', { class: 'wrap muted', text: 'its distance to the phone\'s decisions is the floor of each day' }));
      tbody.appendChild(btr);
      rows.forEach(function (v) {
        var selectable = selectListeners.length > 0;
        var rowCls = v.verdict === 'envelope-limited' ? 'envelope' : v.feasible === false ? 'infeasible' : '';
        var row = h('tr', { class: rowCls + (selectable ? ' selectable' : ''), tabindex: selectable ? '0' : null,
          'data-vhash': v.vhash,
          onclick: selectable ? function () { emitSelect(v); } : null,
          onkeydown: selectable ? function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); emitSelect(v); } } : null });
        row.appendChild(h('td', { class: 'n num', text: v.feasible === false ? '-' : (explore ? v.position : (v.rank || '-')) }));
        row.appendChild(h('td', { class: 'sticky' }, h('div', { class: 'vlabel' }, h('code', { text: v.variant }), h('span', { class: 'sha mono', text: fmt.sha(v.vhash) }))));
        row.appendChild(h('td', null, verdictPill(v)));
        params.forEach(function (p) {
          var val = (v.values || {})[p.key];
          var moved = valueText(val) !== valueText(p.phone);
          row.appendChild(h('td', { class: 'n param' + (moved ? ' moved' : ''), title: moved ? 'phone ' + valueText(p.phone) : 'the phone\'s value', text: valueText(val) }));
        });
        row.appendChild(h('td', { class: 'n num', text: fmt.int((v.days || []).length) }));
        comps.forEach(function (c) { row.appendChild(bandCell(c, (v.mean || {})[c.key], ties[c.key], scale[c.key])); });
        row.appendChild(h('td', { class: 'n num', title: v.review_component ? 'on ' + v.review_component : null, text: num(v.floor_multiple) ? fmt.num(v.floor_multiple, 1) + ' x' : '-' }));
        extras.forEach(function (x) {
          var val = (v.mean || {})[x.key];
          row.appendChild(h('td', { class: 'n num', text: !num(val) ? '-' : x.key.slice(-4) === 'base' ? fmt.signed(val, x.unit === 'loops' ? 0 : 2) : fmt.num(val, x.unit === 'loops' ? 0 : x.unit === '%' ? 1 : 2) }));
        });
        covCols.forEach(function (x) {
          var val = (v.coverage || {})[x.key];
          row.appendChild(h('td', { class: 'n num', title: cov.caveat, text: !num(val) ? '-' : x.unit === 'U' ? fmt.signed(val, 2) : fmt.num(val, 1) }));
        });
        row.appendChild(h('td', { class: 'wrap' }, feasibilityText(v)));
        var vline = v.validation && v.validation.line;
        var vcav = v.validation && v.validation.recommended_for_review && v.validation.caveat;   // metrics.validation_caveat
        row.appendChild(h('td', { class: 'wrap' }, v.why || '', vline ? h('span', { class: 'vline', text: vline + (v.validation.recommended_for_review ? '; recommended for review' : '') }) : null,
          vcav ? h('span', { class: 'vline vcaveat', text: vcav }) : null));
        tbody.appendChild(row);
      });
      HC.clear(box).appendChild(h('div', { class: 'table-scroll' }, h('table', { class: 'twin lb' }, h('thead', null, tr), tbody)));
    }
    draw();
    return { draw: draw };
  }

  function emitSelect(v) { selectListeners.forEach(function (cb) { try { cb(v.vhash, v); } catch (e) { console.error('parameters select', e); } }); }

  // ------------------------------------------------------------------------------------------------ view
  var api = HC.parameters = {
    state: state,
    model: function (data) {
      var ps = (data || HC.data || {}).parameters || {};
      var c = compOf(ps, state.comp || (ps.components && ps.components[0] && ps.components[0].key));
      return { ps: ps, component: c, points: c ? points(ps, c.key) : [], band: c ? bandOf(ps, shownDays(ps), c.key) : 0,
               cells: (ps.params || []).length >= 2 && c ? cells(ps, paramByKey(ps, state.hx || ps.params[0].key), paramByKey(ps, state.hy || ps.params[1].key), c.key) : null };
    },
    setFilter: function (set, label) {
      filter.set = set ? (set instanceof Set ? set : new Set(set)) : null;
      filter.label = label || '';
      if (hooks.board) hooks.board.draw();
    },
    onSelect: function (cb) { if (typeof cb === 'function') selectListeners.push(cb); },
    refresh: function () { if (hooks.redraw) hooks.redraw(); }
  };

  HC.registerView('parameters', { title: 'Parameters', issue: 'H-403', render: function (el, data) {
    injectCss();
    var ps = data && data.parameters;
    if (!ps) { el.appendChild(h('p', { class: 'placeholder', text: 'This page carries no parameter-space data (render it again with the current hc report).' })); return; }
    var comps = ps.components || [];
    if (!state.comp) state.comp = comps.length ? comps[0].key : null;
    var floored = (ps.days || []).filter(function (d) { return (ps.floors || {})[d]; });
    if (!floored.length) {
      el.appendChild(h('p', { class: 'placeholder', text: 'No base replay on a search day yet: without the floor nothing here can be read against the noise of the replay.' }));
    }
    (ps.warnings || []).forEach(function (w) { el.appendChild(h('p', null, h('span', { class: 'pill warning', text: w }))); });
    var panels = [], heat = null;
    function redrawCharts() {
      var t = HC.theme.current();
      panels.forEach(function (p) {
        if (p.rec.chart) p.rec.chart.setOption(p.rec.build(t, window.echarts), { notMerge: true });
        HC.tableTwin(p.fig, p.twin());
      });
      if (heat) heat.redraw();
      if (pareto && pareto.chart) pareto.chart.setOption(pareto.build(t, window.echarts), { notMerge: true });
    }
    el.appendChild(controls(ps, redrawCharts));
    var pareto = renderPareto(el, ps);
    panels = renderPanels(el, ps);
    hooks.board = renderLeaderboard(el, ps);
    heat = renderHeat(el, ps);
    hooks.redraw = function () { redrawCharts(); hooks.board.draw(); };
  } });
})();
