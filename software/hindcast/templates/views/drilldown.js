/* views/drilldown.js (H-405 Loop drill-down, minimal): one loop of one variant on one day, next to the phone and the
   base replay, in a side sheet (full screen below 760 px).

   Opened from three places: a loop clicked (or Enter on a focused chart) in the Scenarios view
   (HC.scenarios.onLoop), a row of the Scenarios table twin, and a row of this view's own loop table (the Loop tab).
   The sheet shows
     header       time (UTC), day, the variant against base, recorded BG / IOB / COB / target at that loop
     badge        'a low followed at HH:MM' when a recorded reading below 3.9 mmol/l lies within 2 h after the loop:
                  a position in the history, never a score and never a cause; '(after a manual bolus, not counted
                  for P)' is appended when the pre-hypo rule attributes that low to a manual bolus (the low event's
                  `manual` flag, else orig.manual_low_ahead)
     decisions    phone | base replay | variant for insulinReq, SMB, maxBolus, TBR, final ISF factor, eventualBG,
                  minPredBG, minGuardBG; a variant cell that differs from the reference column (base, or the phone
                  when the variant is base) carries a 3 px stripe in the variant's colour
     predictions  the phone's and the variant's prediction fans (IOB solid, ZT dashed, UAM dotted, COB thin solid) on
                  ONE axis with the 3.8 / 5.4 mmol/l guard hairlines; a table twin below
     text diff    word-level diff of the two reason texts and of the two consoleError transcripts (lines aligned
                  first, then words within an aligned pair)
   Tier 1: data.drilldown (report.drilldown_index): pred / pred_text {day: {vhash: path}}, pred_orig / pred_orig_text /
   orig {day: path}, series {day: {vhash: path}}, guards_mgdl, low_mgdl, low_follow_s, tol, match_s; plus
   data.scenarios.entities for labels. Tier 2, fetched only when the sheet opens (HC.loadTier2): series/<day>/orig.json,
   series/<day>/<vhash>.json (+ the base series), pred/<day>/<vhash>.json, pred/<day>/<vhash>.text.json,
   pred/<day>/orig.json, pred/<day>/orig.text.json. A prediction document the index does not name is 'not written
   for this variant' (finalists only), not a load failure.
   Styles are scoped under #hc-drilldown and #view-loop and injected once; every colour is a page.css token.

   Hooks for later views (H-406 full drill-down, H-409 live rows):
     HC.drilldown.open({day, t | index, vhash, opener})  -> Promise of the model; t = s since 00:00 UTC of day
     HC.drilldown.close()                                closes and returns focus to the opener
     HC.drilldown.onOpen(cb), onClose(cb)                cb(model) / cb()
     HC.drilldown.state                                  {open, spec, model}
     HC.drilldown.buildModel(spec, docs)                 pure: the model from loaded documents (node smoke test)
     HC.drilldown.fanOption(tokens, model, units)        pure: the ECharts option of the prediction fans
     HC.drilldown.wordDiff(a, b), lineDiff(a[], b[])     pure: [{op: '=' | '-' | '+', a, b}] diff runs
     HC.drilldown.parseReason(text, units)               {eventual, minPred, minGuard} in mg/dl from a reason text
     HC.drilldown.lowAfter(orig, i, lowMgdl, followS)    {t, bg, manual} of the first recorded low after loop i, or null */
(function () {
  'use strict';
  var HC = window.HC;
  if (!HC || !HC.registerView) return;
  var h = HC.h, fmt = HC.fmt;
  var MGDL = 18.0;
  var MANUAL_LOW = 'after a manual bolus, not counted for P';   // export.MANUAL_LOW_LABEL
  var DEFAULTS = { pred: {}, pred_text: {}, pred_orig: {}, pred_orig_text: {}, orig: {}, series: {},
                   guards_mgdl: [68.4, 97.2], guards_mmol: [3.8, 5.4], low_mgdl: 70.2, low_follow_s: 7200,
                   tol: { insulin: 0.05, tbr: 0.05, factor: 0.005 }, match_s: 90, base_vhash: null };
  // dashes are reserved for the ZT / UAM coding (DESIGN 6): COB is a thin solid line, IOB the full-width one
  var FAN_KINDS = [['iob', 'IOB', 'solid'], ['zt', 'ZT', 'dashed'], ['uam', 'UAM', 'dotted'], ['cob', 'COB', 'thin']];
  var DASH = { solid: 'solid', dashed: [6, 4], dotted: [1.5, 3.5], thin: 'solid' };
  var THIN = 0.5;                                          // COB width as a share of the side's IOB width
  var LINE_CAP = 4000000;                                  // LCS cells; above this the diff falls back to whole blocks

  var CSS = [
    '#hc-drilldown { position: fixed; inset: 0; z-index: 30; }',
    '#hc-drilldown .dd-scrim { position: absolute; inset: 0; background: var(--ink); opacity: 0.28; }',
    '#hc-drilldown .dd-sheet { position: absolute; top: 0; right: 0; bottom: 0; width: min(700px, 94vw); display: grid; grid-template-rows: auto minmax(0, 1fr); background: var(--page); color: var(--ink); border-left: 1px solid var(--axis); box-shadow: -10px 0 30px var(--border); }',
    '#hc-drilldown .dd-head { display: grid; gap: 6px; padding-top: calc(env(safe-area-inset-top, 0px) + 12px); padding-bottom: 10px; padding-left: 18px; padding-right: max(16px, env(safe-area-inset-right, 0px)); background: var(--surface); border-bottom: 1px solid var(--grid); }',
    '#hc-drilldown .dd-top { display: flex; gap: 10px; align-items: start; justify-content: space-between; }',
    '#hc-drilldown .dd-top > div { min-width: 0; display: grid; gap: 2px; }',
    '#hc-drilldown h2 { font-size: 1.15rem; font-variant-numeric: tabular-nums; }',
    '#hc-drilldown .dd-who { font-size: 0.9rem; color: var(--ink-2); overflow-wrap: anywhere; }',
    '#hc-drilldown .dd-who b { color: var(--ink); font-weight: 600; }',
    '#hc-drilldown button.dd-close { flex: 0 0 auto; font: inherit; font-size: 0.86rem; padding: 4px 12px; border-radius: var(--radius); border: 1px solid var(--ink-2); background: var(--surface); color: var(--ink); cursor: pointer; }',
    '#hc-drilldown button.dd-close:hover { background: var(--surface-2); }',
    '#hc-drilldown .dd-pick { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: end; }',
    '#hc-drilldown .dd-pick label { display: grid; gap: 2px; font-size: 0.78rem; color: var(--ink-2); min-width: 0; max-width: 100%; }',
    '#hc-drilldown select { font: inherit; font-size: 0.86rem; padding: 3px 8px; border: 1px solid var(--axis); border-radius: var(--radius); background: var(--surface); color: var(--ink); max-width: 100%; }',
    '#hc-drilldown .dd-body { overflow-y: auto; overscroll-behavior: contain; display: grid; grid-template-columns: minmax(0, 1fr); align-content: start; gap: 18px; padding-top: 14px; padding-bottom: calc(env(safe-area-inset-bottom, 0px) + 28px); padding-left: 18px; padding-right: max(16px, env(safe-area-inset-right, 0px)); }',
    '#hc-drilldown .dd-body > * { min-width: 0; }',
    '#hc-drilldown .dd-facts { margin: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); gap: 6px 14px; }',
    '#hc-drilldown .dd-facts div { display: grid; gap: 0; min-width: 0; }',
    '#hc-drilldown .dd-facts dt { font-size: 0.72rem; color: var(--muted); letter-spacing: 0.05em; text-transform: uppercase; font-weight: 600; }',
    '#hc-drilldown .dd-facts dd { margin: 0; font-variant-numeric: tabular-nums; font-weight: 600; overflow-wrap: anywhere; }',
    '#hc-drilldown .dd-badges { display: flex; flex-wrap: wrap; gap: 6px; }',
    '#hc-drilldown .dd-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 0.82rem; padding: 2px 10px; border-radius: 999px; border: 1px solid var(--ink-2); color: var(--ink); background: var(--surface); }',
    '#hc-drilldown .dd-badge::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--ink-2); flex: 0 0 8px; }',
    '#hc-drilldown .dd-badge.quiet { border-color: var(--axis); color: var(--ink-2); }',
    '#hc-drilldown .dd-badge.quiet::before { background: var(--muted); }',
    '#hc-drilldown section.dd-part { display: grid; gap: 8px; }',
    '#hc-drilldown section.dd-part > header { display: flex; flex-wrap: wrap; gap: 2px 12px; align-items: baseline; }',
    '#hc-drilldown section.dd-part > header p { font-size: 0.84rem; color: var(--ink-2); max-width: 72ch; }',
    '#hc-drilldown table.dd-pair { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; font-size: 0.9rem; }',
    '#hc-drilldown table.dd-pair th, #hc-drilldown table.dd-pair td { padding: 5px 10px; border-bottom: 1px solid var(--grid); text-align: right; white-space: nowrap; }',
    '#hc-drilldown table.dd-pair th:first-child, #hc-drilldown table.dd-pair td:first-child { text-align: left; padding-left: 0; }',
    '#hc-drilldown table.dd-pair thead th { font-size: 0.78rem; color: var(--ink-2); font-weight: 600; vertical-align: bottom; }',
    '#hc-drilldown table.dd-pair td.k { color: var(--ink-2); }',
    '#hc-drilldown table.dd-pair td.k small { color: var(--muted); font-size: 0.78rem; }',
    '#hc-drilldown table.dd-pair td.ref { color: var(--ink-2); }',
    '#hc-drilldown table.dd-pair td.v { font-weight: 600; }',
    '#hc-drilldown table.dd-pair td.diff { box-shadow: inset 3px 0 0 var(--ink); }',
    '#hc-drilldown table.dd-pair td.diff.s1 { box-shadow: inset 3px 0 0 var(--series-1); }',
    '#hc-drilldown table.dd-pair td.diff.s2 { box-shadow: inset 3px 0 0 var(--series-2); }',
    '#hc-drilldown table.dd-pair td.diff.s3 { box-shadow: inset 3px 0 0 var(--series-3); }',
    '#hc-drilldown table.dd-pair td.d { color: var(--ink-2); font-size: 0.84rem; }',
    '#hc-drilldown .sw { display: inline-block; width: 16px; height: 0; border-top: 2px solid var(--phone); vertical-align: 3px; margin-right: 5px; }',
    '#hc-drilldown .sw.s1 { border-top-color: var(--series-1); }',
    '#hc-drilldown .sw.s2 { border-top-color: var(--series-2); }',
    '#hc-drilldown .sw.s3 { border-top-color: var(--series-3); }',
    '#hc-drilldown .sw.sx { border-top-color: var(--ink); }',
    '#hc-drilldown .sw.base { border-top-color: var(--ink-2); }',
    '#hc-drilldown .sw.zt { border-top-style: dashed; }',
    '#hc-drilldown .sw.uam { border-top-style: dotted; }',
    '#hc-drilldown .sw.cob { border-top-style: solid; border-top-width: 1px; }',
    '#hc-drilldown .sw.hair { border-top: 1px solid var(--muted); }',
    '#hc-drilldown .sw.stripe { width: 3px; height: 14px; border: 0; background: var(--ink); vertical-align: -2px; }',
    '#hc-drilldown .sw.stripe.s1 { background: var(--series-1); }',
    '#hc-drilldown .sw.stripe.s2 { background: var(--series-2); }',
    '#hc-drilldown .sw.stripe.s3 { background: var(--series-3); }',
    '#hc-drilldown .dd-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.8rem; color: var(--ink-2); }',
    '#hc-drilldown .dd-legend span { white-space: nowrap; }',
    '#hc-drilldown figure.chart .plot { height: 260px; }',
    '#hc-drilldown .dd-msg { font-size: 0.86rem; color: var(--ink-2); overflow-wrap: anywhere; }',
    '#hc-drilldown .dd-text { font-family: var(--mono); font-size: 0.8rem; line-height: 1.55; background: var(--surface); border: 1px solid var(--grid); border-radius: var(--radius); padding: 8px 10px; overflow-wrap: anywhere; }',
    '#hc-drilldown .dd-text .ln { display: block; padding-left: 14px; text-indent: -14px; }',
    '#hc-drilldown .dd-text .ln.same { color: var(--muted); }',
    '#hc-drilldown .dd-text .ln.gone::before { content: "− "; color: var(--ink-2); }',
    '#hc-drilldown .dd-text .ln.new::before { content: "+ "; color: var(--ink-2); }',
    '#hc-drilldown .dd-text del { text-decoration: line-through; text-decoration-color: var(--ink-2); color: var(--ink-2); background: var(--surface-2); border-radius: 2px; padding: 0 2px; }',
    '#hc-drilldown .dd-text ins { text-decoration: none; color: var(--ink); font-weight: 600; border-bottom: 2px solid var(--ink); padding: 0 1px; }',
    '#hc-drilldown .dd-text ins.s1 { border-bottom-color: var(--series-1); }',
    '#hc-drilldown .dd-text ins.s2 { border-bottom-color: var(--series-2); }',
    '#hc-drilldown .dd-text ins.s3 { border-bottom-color: var(--series-3); }',
    '#hc-drilldown details.dd-fold summary { cursor: pointer; color: var(--ink-2); font-size: 0.86rem; }',
    '#hc-drilldown details.dd-fold > .dd-text { margin-top: 6px; }',
    '#hc-drilldown .dd-tip { font-size: 12px; line-height: 1.35; color: var(--ink); font-variant-numeric: tabular-nums; }',
    '#hc-drilldown .dd-tip table { border-collapse: collapse; margin-top: 3px; }',
    '#hc-drilldown .dd-tip td { padding: 0 6px 0 0; text-align: right; white-space: nowrap; }',
    '#hc-drilldown .dd-tip td.k { text-align: left; color: var(--ink-2); }',
    '@media (max-width: 759px) { #hc-drilldown .dd-sheet { width: 100%; border-left: 0; box-shadow: none; } #hc-drilldown .dd-head, #hc-drilldown .dd-body { padding-left: max(16px, env(safe-area-inset-left, 0px)); } }',
    '@media (max-width: 480px) { #hc-drilldown figure.chart .plot { height: 230px; } #hc-drilldown .dd-pick label, #hc-drilldown .dd-pick select { width: 100%; } }',
    'html.hc-dd-open { overflow: hidden; }',
    '#view-loop .dd-controls { display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: end; }',
    '#view-loop .dd-controls label { display: grid; gap: 2px; font-size: 0.8rem; color: var(--ink-2); min-width: 0; max-width: 100%; }',
    '#view-loop .dd-controls label.inline { display: inline-flex; align-items: center; gap: 6px; color: var(--ink); font-size: 0.86rem; }',
    '#view-loop select { font: inherit; font-size: 0.88rem; padding: 4px 8px; border: 1px solid var(--axis); border-radius: var(--radius); background: var(--surface); color: var(--ink); max-width: 100%; }',
    '#view-loop input[type="checkbox"] { accent-color: var(--series-1); margin: 0; }',
    '#view-loop .dd-list { background: var(--surface); border: 1px solid var(--grid); border-radius: var(--radius); padding: 6px 12px 8px; display: grid; gap: 6px; }',
    '#view-loop .dd-list p { font-size: 0.84rem; color: var(--ink-2); }',
    '#view-loop table.twin tbody tr { cursor: pointer; }',
    '#view-loop table.twin tbody tr:hover td { background: var(--surface-2); }',
    '#view-loop table.twin td.chg { box-shadow: inset 3px 0 0 var(--series-1); }',
    '#view-loop button.dd-open { font: inherit; font-variant-numeric: tabular-nums; border: 0; background: none; color: var(--series-1); text-decoration: underline; cursor: pointer; padding: 0; }',
    '#view-loop button.dd-more { font: inherit; font-size: 0.86rem; padding: 4px 12px; border-radius: var(--radius); border: 1px solid var(--ink-2); background: var(--surface); color: var(--ink); cursor: pointer; justify-self: start; }',
    '#view-loop .dd-msg { font-size: 0.86rem; color: var(--ink-2); overflow-wrap: anywhere; }',
    '@media (max-width: 480px) { #view-loop .dd-controls label, #view-loop .dd-controls select { width: 100%; } }',
    '#view-scenarios table.twin tbody tr[data-dd] { cursor: pointer; }',
    '#view-scenarios table.twin tbody tr[data-dd]:hover td { background: var(--surface-2); }'
  ].join('\n');

  function injectCss() {
    if (document.getElementById('hc-view-drilldown-css')) return;
    var s = document.createElement('style');
    s.id = 'hc-view-drilldown-css';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  // ------------------------------------------------------------------------------------------------ helpers
  function dd() {
    var d = (HC.data && HC.data.drilldown) || {};
    var out = {};
    Object.keys(DEFAULTS).forEach(function (k) { out[k] = d[k] !== undefined && d[k] !== null ? d[k] : DEFAULTS[k]; });
    return out;
  }
  function scn() { return (HC.data && HC.data.scenarios) || { days: [], entities: [] }; }
  function pageUnits() { return ((HC.data && HC.data.page) || {}).units === 'mmol' ? 'mmol' : 'mg/dl'; }
  function num(v) { return typeof v === 'number' && v === v; }
  function at(doc, key, i) { var a = doc && doc[key]; return a && i >= 0 && i < a.length ? a[i] : null; }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function nearest(ts, t) {
    if (!ts || !ts.length || !num(t)) return -1;
    var lo = 0, hi = ts.length - 1;
    while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (ts[mid] < t) lo = mid; else hi = mid; }
    return Math.abs(ts[lo] - t) <= Math.abs(ts[hi] - t) ? lo : hi;
  }
  function indexIn(doc, t, matchS) {                       // the same loop in another document, or -1
    if (!doc || !doc.t) return -1;
    var i = nearest(doc.t, t);
    return i >= 0 && Math.abs(doc.t[i] - t) <= matchS ? i : -1;
  }
  function hhmm(sec) {                                     // s since 00:00 UTC -> HH:MM (same rounding as the Scenarios view)
    var s = Math.round(sec), plus = s >= 86400 ? ' +1 d' : '';
    s = ((s % 86400) + 86400) % 86400;
    var hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60);
    return (hh < 10 ? '0' : '') + hh + ':' + (mm < 10 ? '0' : '') + mm + plus;
  }
  function bgv(mgdl, units) { return num(mgdl) ? (units === 'mmol' ? Math.round(mgdl / MGDL * 10) / 10 : Math.round(mgdl)) : null; }
  function bgUnit(units) { return units === 'mmol' ? 'mmol/l' : 'mg/dl'; }
  function bgText(mgdl, units) { var v = bgv(mgdl, units); return v === null ? '-' : fmt.num(v, units === 'mmol' ? 1 : 0); }
  function entityOf(vhash) {
    var es = scn().entities || [];
    for (var i = 0; i < es.length; i++) if (es[i].vhash === vhash) return es[i];
    return null;
  }
  function baseVhash() {
    var d = dd();
    if (d.base_vhash) return d.base_vhash;
    var es = scn().entities || [];
    for (var i = 0; i < es.length; i++) if (es[i].base) return es[i].vhash;
    return (HC.data && HC.data.base_vhash) || null;
  }
  function isBase(vhash) { var e = entityOf(vhash); return e ? !!e.base : vhash === baseVhash(); }
  function labelOf(vhash) {
    var e = entityOf(vhash);
    if (e) return e.label || e.variant;
    return vhash === baseVhash() ? 'base' : String(vhash || '').slice(0, 10);
  }
  /* the variant's colour: its slot in the Scenarios view when it has one there (colour follows the variant for the
     life of the page), otherwise ink - never a slot colour that belongs to another variant */
  function slotOf(vhash, day) {
    var S = HC.scenarios && HC.scenarios.state;
    if (!S || !S.slots) return 0;
    for (var i = 0; i < S.slots.length; i++) {
      var s = S.slots[i];
      if (s && s.vhash === vhash && (!day || s.day === day)) return i + 1;
    }
    for (var j = 0; j < S.slots.length; j++) if (S.slots[j] && S.slots[j].vhash === vhash) return j + 1;
    return 0;
  }
  function slotClass(slot) { return slot ? 's' + slot : 'sx'; }
  function seriesPath(day, vh) {
    var p = (dd().series[day] || {})[vh];
    if (p) return p;
    var e = entityOf(vh);
    return (e && e.series && e.series[day]) || null;
  }
  function origPath(day) {
    var p = dd().orig[day];
    if (p) return p;
    var ds = scn().days || [];
    for (var i = 0; i < ds.length; i++) if (ds[i].day === day && ds[i].orig) return ds[i].orig;
    return HC.tier2Path ? HC.tier2Path('orig', day) : null;
  }
  function variantsOn(day) {                               // vhashes with a series on that day, base first, then review order
    var have = {};
    Object.keys(dd().series[day] || {}).forEach(function (vh) { have[vh] = true; });
    (scn().entities || []).forEach(function (e) { if ((e.series || {})[day]) have[e.vhash] = true; });
    var order = (scn().entities || []).map(function (e) { return e.vhash; }).filter(function (vh) { return have[vh]; });
    Object.keys(have).forEach(function (vh) { if (order.indexOf(vh) < 0) order.push(vh); });
    return order;
  }
  function daysWithData() {
    var d = dd(), out = [];
    Object.keys(d.series).concat((scn().days || []).filter(function (x) { return x.orig; }).map(function (x) { return x.day; }))
      .forEach(function (day) { if (out.indexOf(day) < 0 && origPath(day)) out.push(day); });
    return out.sort();
  }
  function load(path) {
    if (!path) return Promise.resolve({ missing: true });
    return HC.loadTier2(path).then(function (doc) { return { doc: doc }; }, function (e) { return { error: e && e.message ? e.message : String(e) }; });
  }

  // ------------------------------------------------------------------------------------------------ pure parts
  var NUM = '(-?\\d+(?:\\.\\d+)?)';
  var REASON_RE = {
    eventual: [new RegExp('\\bEventual BG\\s+' + NUM), new RegExp('\\beventualBG\\s*(?:is)?\\s*:?\\s*' + NUM, 'i')],
    minPred: [new RegExp('\\bminPredBG:?\\s+' + NUM)],
    minGuard: [new RegExp('\\bminGuardBG:?\\s+' + NUM)]
  };
  /* numbers of a reason (or consoleError) text, read in the user's display units (the loop writes its reason in
     out_units, the same units as the page) and returned in mg/dl */
  function parseReason(text, units) {
    var out = { eventual: null, minPred: null, minGuard: null };
    if (!text) return out;
    Object.keys(REASON_RE).forEach(function (k) {
      for (var i = 0; i < REASON_RE[k].length && out[k] === null; i++) {
        var m = REASON_RE[k][i].exec(text);
        if (m) { var v = parseFloat(m[1]); out[k] = units === 'mmol' ? Math.round(v * MGDL * 10) / 10 : v; }
      }
    });
    return out;
  }

  function lowAfter(orig, i, lowMgdl, followS) {
    if (!orig || !orig.t || i < 0) return null;
    var t0 = orig.t[i], best = null;
    (orig.events || []).forEach(function (ev) {
      if (ev && ev.kind === 'low' && num(ev.t) && ev.t > t0 && ev.t - t0 <= followS && (!best || ev.t < best.t)) {
        best = { t: ev.t, bg: num(ev.value) ? ev.value : null, manual: !!ev.manual };
      }
    });
    if (best) return best;
    var bg = orig.bg || [];
    var manual = !!(orig.manual_low_ahead && orig.manual_low_ahead[i]);
    for (var j = i + 1; j < orig.t.length && orig.t[j] - t0 <= followS; j++) {
      if (num(bg[j]) && bg[j] < lowMgdl) return { t: orig.t[j], bg: bg[j], manual: manual };
    }
    return null;
  }

  /* LCS diff of two token arrays; eq(a, b) decides equality. -> [{op, a, b}] with op '=' (a and b), '-' (a only),
     '+' (b only). Above LINE_CAP cells it returns the two blocks whole. */
  function diffSeq(A, B, eq) {
    var n = A.length, m = B.length, out = [];
    if (!n && !m) return out;
    if ((n + 1) * (m + 1) > LINE_CAP) {
      A.forEach(function (x) { out.push({ op: '-', a: x }); });
      B.forEach(function (y) { out.push({ op: '+', b: y }); });
      return out;
    }
    var W = m + 1, L = new Uint32Array((n + 1) * W);
    for (var i = n - 1; i >= 0; i--) {
      for (var j = m - 1; j >= 0; j--) {
        L[i * W + j] = eq(A[i], B[j]) ? L[(i + 1) * W + j + 1] + 1 : Math.max(L[(i + 1) * W + j], L[i * W + j + 1]);
      }
    }
    var a = 0, b = 0;
    while (a < n && b < m) {
      if (eq(A[a], B[b])) { out.push({ op: '=', a: A[a], b: B[b] }); a++; b++; }
      else if (L[(a + 1) * W + b] >= L[a * W + b + 1]) { out.push({ op: '-', a: A[a] }); a++; }
      else { out.push({ op: '+', b: B[b] }); b++; }
    }
    for (; a < n; a++) out.push({ op: '-', a: A[a] });
    for (; b < m; b++) out.push({ op: '+', b: B[b] });
    return out;
  }
  function words(s) { return String(s === null || s === undefined ? '' : s).split(/\s+/).filter(function (w) { return w !== ''; }); }
  function wordDiff(a, b) { return diffSeq(words(a), words(b), function (x, y) { return x === y; }); }
  function lineKey(s) { return String(s).replace(/-?\d+(?:\.\d+)?/g, '#').replace(/\s+/g, ' ').trim(); }
  /* consoleError transcripts: lines are aligned on their wording with the numbers masked, then an aligned pair
     whose text differs gets a word diff (op '~' with `words`) */
  function lineDiff(A, B) {
    var runs = diffSeq(A || [], B || [], function (x, y) { return lineKey(x) === lineKey(y); });
    return runs.map(function (r) {
      if (r.op === '=' && r.a !== r.b) return { op: '~', a: r.a, b: r.b, words: wordDiff(r.a, r.b) };
      return r;
    });
  }
  function diffCount(runs) {
    var n = 0;
    runs.forEach(function (r) {
      if (r.op === '-' || r.op === '+') n++;
      else if (r.op === '~') r.words.forEach(function (w) { if (w.op !== '=') n++; });
    });
    return n;
  }

  /* the model of one loop from loaded documents:
     docs = {orig, series, base, pred, predText, predOrig, predOrigText} (each a tier-2 doc or null) */
  function buildModel(spec, docs, opts) {
    opts = opts || {};
    var cfg = opts.cfg || dd(), units = opts.units || pageUnits();
    var orig = docs.orig;
    var i = spec.index !== undefined && spec.index !== null ? spec.index : nearest(orig && orig.t, spec.t);
    if (!orig || i < 0 || i >= orig.t.length) return null;
    var t = orig.t[i], ms = cfg.match_s;
    var vIsBase = !!opts.isBase;
    var iv = indexIn(docs.series, t, ms), ib = indexIn(docs.base, t, ms);
    var ip = indexIn(docs.pred, t, ms), ipo = indexIn(docs.predOrig, t, ms);
    var ipt = indexIn(docs.predText, t, ms), ipot = indexIn(docs.predOrigText, t, ms);
    var vReason = at(docs.pred, 'reason', ip), pReason = at(docs.predOrig, 'reason', ipo);
    var bReason = vIsBase ? vReason : at(opts.basePred, 'reason', indexIn(opts.basePred, t, ms));
    var vText = parseReason(vReason, units), pText = parseReason(pReason, units), bText = parseReason(bReason, units);
    var levels = at(docs.pred, 'levels', ip) || {};
    function lines(doc, k) {
      var idx = at(doc, 'console', k);
      return idx ? idx.map(function (j) { return (doc.strings || [])[j]; }).filter(function (x) { return typeof x === 'string'; }) : null;
    }
    var pConsole = lines(docs.predOrigText, ipot), vConsole = lines(docs.predText, ipt);
    if (pConsole) { var pt = parseReason(pConsole.join('\n'), units); if (pText.eventual === null) pText.eventual = pt.eventual; }
    function pick() { for (var k = 0; k < arguments.length; k++) if (num(arguments[k])) return arguments[k]; return null; }
    var hasBase = !vIsBase && !!docs.base && ib >= 0;
    var ref = vIsBase || !hasBase ? 'phone' : 'base';
    var specRows = [
      ['ins_req', 'insulinReq', 'U', 'insulin'], ['smb', 'SMB', 'U', 'insulin'], ['max_bolus', 'maxBolus', 'U', 'insulin'],
      ['tbr', 'TBR', 'U/h', 'tbr'], ['isf_factor', 'final ISF factor', '', 'factor'],
      ['eventual_bg', 'eventualBG', bgUnit(units), 'bg'], ['min_pred_bg', 'minPredBG', bgUnit(units), 'bg'],
      ['min_guard', 'minGuardBG', bgUnit(units), 'bg']
    ];
    var textKey = { eventual_bg: 'eventual', min_pred_bg: 'minPred', min_guard: 'minGuard' };
    var levelKey = { eventual_bg: 'eventualBG', min_pred_bg: 'minPredBG', min_guard: 'minGuardBG1' };
    var fromText = [];
    var rows = specRows.map(function (r) {
      var key = r[0], kind = r[3], phone, base, variant;
      if (kind === 'bg') {
        var tk = textKey[key];
        phone = pText[tk];
        variant = key === 'min_guard' ? pick(vText[tk], levels[levelKey[key]]) : pick(at(docs.series, key, iv), levels[levelKey[key]], vText[tk]);
        base = vIsBase ? variant : (key === 'min_guard' ? pick(bText[tk]) : pick(at(docs.base, key, ib), bText[tk]));
        if (num(phone)) fromText.push(r[1]);
      } else {
        phone = at(orig, key, i);
        variant = at(docs.series, key, iv);
        base = vIsBase ? variant : at(docs.base, key, ib);
      }
      var refv = ref === 'base' ? base : phone;
      return { key: key, label: r[1], unit: r[2], kind: kind, phone: phone, base: hasBase ? base : null, variant: variant,
               differs: differs(kind, refv, variant, cfg, units), delta: num(refv) && num(variant) ? variant - refv : null };
    });
    var fans = { phone: null, variant: null };
    function fan(doc, k) {
      if (!doc || k < 0) return null;
      var f = {}, any = false;
      FAN_KINDS.forEach(function (fk) { var a = at(doc, fk[0], k); f[fk[0]] = Array.isArray(a) && a.length ? a : null; if (f[fk[0]]) any = true; });
      return any ? f : null;
    }
    fans.phone = fan(docs.predOrig, ipo);
    fans.variant = fan(docs.pred, ip);
    var lowT = lowAfter(orig, i, cfg.low_mgdl, cfg.low_follow_s);
    var tl = at(orig, 'tar_lo', i), th = at(orig, 'tar_hi', i);
    return {
      day: spec.day, vhash: spec.vhash, isBase: vIsBase, index: i, loops: orig.t.length, t: t, ref: ref, hasBase: hasBase,
      facts: { bg: at(orig, 'bg', i), iob: at(orig, 'iob', i), cob: at(orig, 'cob', i), tar_lo: tl, tar_hi: th },
      rows: rows, fromText: fromText, fans: fans, low: lowT,
      reason: { phone: pReason, variant: vReason, diff: pReason || vReason ? wordDiff(pReason, vReason) : null },
      console: { phone: pConsole, variant: vConsole, diff: pConsole || vConsole ? lineDiff(pConsole || [], vConsole || []) : null },
      found: { series: iv >= 0, pred: ip >= 0, predOrig: ipo >= 0, predText: ipt >= 0, predOrigText: ipot >= 0 }
    };
  }

  function differs(kind, a, b, cfg, units) {
    if (!num(a) || !num(b)) return false;
    if (kind === 'bg') return bgv(a, units) !== bgv(b, units);
    var tol = kind === 'factor' ? cfg.tol.factor : kind === 'tbr' ? cfg.tol.tbr : cfg.tol.insulin;
    return Math.abs(a - b) > tol + 1e-9;
  }

  /* the prediction fans of the phone and the variant on ONE value axis (BG in the page's units) over minutes ahead,
     the 3.8 / 5.4 mmol/l guard hairlines as solid markLines; the phone grey, the variant in its colour */
  function fanOption(t, model, units, slot, narrow) {
    var cfg = model.cfg || DEFAULTS;
    var vColor = slot ? t['series-' + slot] : t.ink;
    var series = [], lo = Infinity, hi = -Infinity, maxX = 0;
    [['phone', model.fans.phone, t.phone], ['variant', model.fans.variant, vColor]].forEach(function (side) {
      if (!side[1]) return;
      FAN_KINDS.forEach(function (fk) {
        var arr = side[1][fk[0]];
        if (!arr) return;
        var data = arr.map(function (v, k) {
          var y = bgv(v, units);
          if (num(y)) { lo = Math.min(lo, y); hi = Math.max(hi, y); }
          return [k * 5, y];
        });
        maxX = Math.max(maxX, (arr.length - 1) * 5);
        series.push({ name: (side[0] === 'phone' ? 'phone ' : 'variant ') + fk[1], type: 'line', data: data, showSymbol: false,
                      symbol: 'none', smooth: false, z: side[0] === 'phone' ? 2 : 3, connectNulls: false,
                      lineStyle: { color: side[2], width: (side[0] === 'phone' ? 1.75 : 2) * (fk[2] === 'thin' ? THIN : 1), type: DASH[fk[2]], cap: 'round' },
                      itemStyle: { color: side[2] }, emphasis: { disabled: true }, hcSide: side[0], hcKind: fk[1] });
      });
    });
    var guards = (cfg.guards_mgdl || DEFAULTS.guards_mgdl).map(function (g) { return bgv(g, units); });
    guards.forEach(function (g) { lo = Math.min(lo, g); hi = Math.max(hi, g); });
    var pad = units === 'mmol' ? 0.5 : 9;
    if (series.length) {
      series[0].markLine = { silent: true, symbol: 'none', animation: false,
        lineStyle: { color: t.muted, width: 1, type: 'solid' },
        label: { color: t['ink-2'], fontSize: 11, position: 'end', formatter: function (p) { return fmt.num(p.value, units === 'mmol' ? 1 : 0); } },
        data: guards.map(function (g) { return { yAxis: g }; }) };
    }
    return {
      animation: false,
      grid: { left: 44, right: 40, top: 14, bottom: 34 },
      xAxis: { type: 'value', min: 0, max: Math.max(30, Math.ceil(maxX / 30) * 30), interval: narrow ? 60 : 30,
               name: 'minutes ahead', nameLocation: 'middle', nameGap: 22,
               axisLabel: { formatter: function (v) { return String(v); } }, splitLine: { show: false } },
      yAxis: { type: 'value', name: bgUnit(units), nameGap: 6, nameTextStyle: { align: 'left' },
               min: Math.floor((lo - pad) * 2) / 2, max: Math.ceil((hi + pad) * 2) / 2, scale: true,
               axisLabel: { formatter: function (v) { return fmt.num(v, units === 'mmol' ? 1 : 0); } } },
      tooltip: { trigger: 'axis', confine: true, axisPointer: { type: 'line', lineStyle: { color: t.muted, width: 1 } },
                 formatter: function (ps) { return fanTip(ps, units, t, vColor); } },
      series: series
    };
  }
  function fanTip(ps, units, t, vColor) {
    if (!ps || !ps.length) return '';
    var x = ps[0].value ? ps[0].value[0] : ps[0].axisValue, rows = {};
    ps.forEach(function (p) {
      var s = p.seriesName.split(' '), side = s[0], kind = s[1];
      (rows[kind] = rows[kind] || {})[side] = p.value ? p.value[1] : null;
    });
    var out = '<div class="dd-tip"><b>+' + esc(x) + ' min</b><table><tr><td class="k"></td><td class="k">phone</td><td class="k">variant</td></tr>';
    FAN_KINDS.forEach(function (fk) {
      var r = rows[fk[1]];
      if (!r) return;
      out += '<tr><td class="k">' + esc(fk[1]) + '</td><td>' + esc(num(r.phone) ? fmt.num(r.phone, units === 'mmol' ? 1 : 0) : '-') +
             '</td><td>' + esc(num(r.variant) ? fmt.num(r.variant, units === 'mmol' ? 1 : 0) : '-') + '</td></tr>';
    });
    return out + '</table></div>';
  }

  // ------------------------------------------------------------------------------------------------ the sheet
  var state = { open: false, spec: null, model: null, seq: 0 };
  var openCbs = [], closeCbs = [];
  var sheet = null;                                        // {root, panel, title, who, pick, select, body, close, ...}
  var fan = null;                                          // {fig, rec}: one chart for the life of the page
  var opener = null;

  function ensureSheet() {
    if (sheet) return sheet;
    injectCss();
    var close = h('button', { type: 'button', class: 'dd-close', id: 'dd-close', text: 'Close', onclick: function () { api.close(); } });
    var title = h('h2', { id: 'dd-title', text: 'Loop' });
    var who = h('p', { class: 'dd-who', id: 'dd-who' });
    var select = h('select', { id: 'dd-sheet-variant' });
    select.addEventListener('change', function () {
      if (state.spec) api.open({ day: state.spec.day, t: state.model ? state.model.t : state.spec.t, vhash: select.value, keepOpener: true, keepFocus: true });
    });
    var pick = h('div', { class: 'dd-pick' }, h('label', { for: 'dd-sheet-variant' }, 'variant on this day', select));
    var body = h('div', { class: 'dd-body', id: 'dd-body', tabindex: '-1' });
    var panel = h('aside', { class: 'dd-sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'dd-title', 'aria-describedby': 'dd-who' },
      h('div', { class: 'dd-head' },
        h('div', { class: 'dd-top' }, h('div', null, h('span', { class: 'eyebrow', text: 'Loop drill-down' }), title, who), close),
        pick),
      body);
    var scrim = h('div', { class: 'dd-scrim', onclick: function () { api.close(); } });
    var root = h('div', { id: 'hc-drilldown', hidden: true }, scrim, panel);
    root.addEventListener('keydown', function (ev) { if (ev.key === 'Tab') trapTab(ev, panel); });
    document.body.appendChild(root);
    sheet = { root: root, panel: panel, title: title, who: who, pick: pick, select: select, body: body, close: close };
    return sheet;
  }

  function trapTab(ev, panel) {
    var f = Array.prototype.filter.call(panel.querySelectorAll('button, select, summary, [tabindex="0"], a[href], input'), function (el) {
      return !el.disabled && el.offsetParent !== null;
    });
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (ev.shiftKey && document.activeElement === first) { last.focus(); ev.preventDefault(); }
    else if (!ev.shiftKey && document.activeElement === last) { first.focus(); ev.preventDefault(); }
  }

  function showSheet() {
    var s = ensureSheet();
    if (!state.open) {
      s.root.hidden = false;
      document.documentElement.classList.add('hc-dd-open');
      state.open = true;
    }
    return s;
  }

  function msg(text) { return h('p', { class: 'dd-msg', text: text }); }

  function part(title, note) {
    return h('section', { class: 'dd-part' }, h('header', null, h('h3', { text: title }), note ? h('p', { text: note }) : null));
  }

  function renderSheet(m, info) {
    var s = sheet, units = pageUnits(), slot = info.slot, sc = slotClass(slot);
    var tag = slot ? 'ABC'.charAt(slot - 1) + ' ' : '';
    s.title.textContent = hhmm(m.t) + ' UTC, ' + m.day;
    HC.clear(s.who);
    if (m.isBase) {
      s.who.appendChild(h('b', { text: 'base' }));
      s.who.appendChild(document.createTextNode(' (the phone\'s settings, replayed) against what the phone decided'));
    } else {
      s.who.appendChild(h('b', { text: tag + labelOf(m.vhash) }));
      s.who.appendChild(document.createTextNode(m.hasBase ? ' against base (the phone\'s settings, replayed) and the phone' : ' against the phone (no base replay of this loop)'));
    }
    HC.clear(s.select);
    var vs = variantsOn(m.day);
    vs.forEach(function (vh) { s.select.appendChild(h('option', { value: vh, selected: vh === m.vhash ? true : null, text: labelOf(vh) })); });
    s.pick.hidden = vs.length < 2;

    var b = HC.clear(s.body);
    var f = m.facts;
    var facts = h('dl', { class: 'dd-facts' });
    function fact(k, v) { facts.appendChild(h('div', null, h('dt', { text: k }), h('dd', { text: v }))); }
    fact('recorded BG', bgText(f.bg, units) + ' ' + bgUnit(units));
    fact('recorded IOB', num(f.iob) ? fmt.num(f.iob, 2) + ' U' : '-');
    fact('COB', num(f.cob) ? fmt.num(f.cob, 0) + ' g' : '-');
    fact('target', num(f.tar_lo) ? (bgText(f.tar_lo, units) === bgText(f.tar_hi, units) ? bgText(f.tar_lo, units) : bgText(f.tar_lo, units) + '-' + bgText(f.tar_hi, units)) + ' ' + bgUnit(units) : '-');
    fact('loop', fmt.int(m.index + 1) + ' of ' + fmt.int(m.loops));
    b.appendChild(facts);

    var badges = h('div', { class: 'dd-badges' });
    if (m.low) {
      badges.appendChild(h('span', { class: 'dd-badge', title: 'a recorded reading below ' + fmt.num(info.cfg.low_mgdl / MGDL, 1) + ' mmol/l within ' + Math.round(info.cfg.low_follow_s / 60) + ' min after this loop; a position in the recorded history, which no variant changes',
        text: 'a low followed at ' + hhmm(m.low.t) + (m.low.manual ? ' (' + MANUAL_LOW + ')' : '') }));
    }
    var nd = m.rows.filter(function (r) { return r.differs; }).length;
    badges.appendChild(h('span', { class: 'dd-badge quiet', text: nd ? nd + ' of ' + m.rows.length + ' decision values differ from ' + (m.ref === 'base' ? 'base' : 'the phone') : 'every decision value matches ' + (m.ref === 'base' ? 'base' : 'the phone') }));
    b.appendChild(badges);

    b.appendChild(decisionPart(m, info, sc, units));
    b.appendChild(fanPart(m, info, slot, units));
    b.appendChild(textPart(m, info, sc));
  }

  function fmtValue(r, v, units) {
    if (!num(v)) return '-';
    if (r.kind === 'bg') return bgText(v, units);
    if (r.kind === 'factor') return fmt.num(v, 2);
    return fmt.num(v, 2);
  }
  function fmtDelta(r, d, units) {
    if (!num(d)) return '';
    var dp = r.kind === 'bg' ? (units === 'mmol' ? 1 : 0) : 2;
    var x = r.kind === 'bg' ? bgv(Math.abs(d), units) : Math.round(Math.abs(d) * 100) / 100;
    if (!x) return '±' + fmt.num(0, dp);               // a difference that rounds away is shown as none
    return (d > 0 ? '+' : '−') + fmt.num(x, dp);
  }

  function decisionPart(m, info, sc, units) {
    var p = part('Decision pair', 'What the loop decided at this loop. ' + (m.ref === 'base'
      ? 'A striped variant cell differs from the base replay; base itself differs from the phone by the replay floor.'
      : 'A striped cell differs from what the phone decided.'));
    var thead = h('tr', null, h('th', { scope: 'col', text: '' }),
      h('th', { scope: 'col' }, h('i', { class: 'sw' }), 'phone'),
      m.hasBase ? h('th', { scope: 'col' }, h('i', { class: 'sw base' }), 'base') : null,
      h('th', { scope: 'col' }, h('i', { class: 'sw ' + sc }), m.isBase ? 'base' : 'variant'),
      h('th', { scope: 'col', text: (m.isBase ? 'base' : 'variant') + ' minus ' + m.ref }));
    var tbody = h('tbody');
    m.rows.forEach(function (r) {
      var vcell = h('td', { class: 'v' + (r.differs ? ' diff ' + sc : ''), title: r.differs ? 'differs from ' + m.ref : null }, fmtValue(r, r.variant, units));
      if (r.differs) vcell.appendChild(h('span', { class: 'visually-hidden', style: 'position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)', text: ' (differs)' }));
      tbody.appendChild(h('tr', null,
        h('td', { class: 'k' }, r.label, r.unit ? h('small', { text: ' ' + r.unit }) : null),
        h('td', { class: m.ref === 'phone' ? 'ref' : null, text: fmtValue(r, r.phone, units) }),
        m.hasBase ? h('td', { class: m.ref === 'base' ? 'ref' : null, text: fmtValue(r, r.base, units) }) : null,
        vcell,
        h('td', { class: 'd', text: fmtDelta(r, r.delta, units) })));
    });
    p.appendChild(h('div', { class: 'table-scroll' }, h('table', { class: 'dd-pair' }, h('thead', null, thead), tbody)));
    var notes = [];
    notes.push('Tolerances: ' + fmt.num(info.cfg.tol.insulin, 2) + ' U, ' + fmt.num(info.cfg.tol.tbr, 2) + ' U/h, factors and glucose levels as shown.');
    if (m.fromText.length) notes.push('The phone\'s ' + m.fromText.join(', ') + ' are read from its reason text.');
    if (!m.found.series) notes.push('The variant has no replayed decision for this loop.');
    p.appendChild(h('div', { class: 'dd-legend' }, h('span', null, h('i', { class: 'sw stripe ' + sc }), 'differs'), h('span', { class: 'long', style: 'white-space: normal', text: notes.join(' ') })));
    return p;
  }

  function fanPart(m, info, slot, units) {
    var guards = info.cfg.guards_mmol || DEFAULTS.guards_mmol;
    var p = part('Predictions at this loop', 'Both prediction fans on one axis: what each loop expected glucose to do when it decided, not what happened.');
    var sc = slotClass(slot);
    var leg = h('div', { class: 'dd-legend' },
      h('span', null, h('i', { class: 'sw' }), 'phone'),
      h('span', null, h('i', { class: 'sw ' + sc }), m.isBase ? 'base' : 'variant'),
      h('span', null, h('i', { class: 'sw base' }), 'IOB solid'),
      h('span', null, h('i', { class: 'sw base zt' }), 'ZT dashed'),
      h('span', null, h('i', { class: 'sw base uam' }), 'UAM dotted'),
      h('span', null, h('i', { class: 'sw base cob' }), 'COB thin solid'),
      h('span', null, h('i', { class: 'sw hair' }), 'guard lines ' + guards.map(function (g) { return fmt.num(g, 1); }).join(' / ') + ' mmol/l'));
    p.appendChild(leg);
    var problems = [];
    if (!m.fans.phone) problems.push(info.why.predOrig || 'The phone recorded no prediction for this loop.');
    if (!m.fans.variant) problems.push(info.why.pred || 'The replay kept no prediction for this loop.');
    if (problems.length) p.appendChild(msg(problems.join(' ')));
    if (!fan) {
      fan = { fig: HC.figure('Prediction fans', 'BG in ' + bgUnit(units) + ' over the next hours, 5 min steps') };
      fan.fig.querySelector('.plot').setAttribute('role', 'img');
    }
    fan.fig.hidden = !(m.fans.phone || m.fans.variant);
    p.appendChild(fan.fig);
    fan.model = m; fan.slot = slot; fan.units = units;
    if (!fan.fig.hidden) {
      fan.fig.querySelector('.plot').setAttribute('aria-label', 'Prediction fans of the phone and the ' + (m.isBase ? 'base replay' : 'variant') + ' at ' + hhmm(m.t) + ' UTC; the numbers are in the table below.');
      var build = function (t) { return fan.model ? fanOption(t, fan.model, fan.units, fan.slot, (window.innerWidth || 1200) < 600) : {}; };
      if (!fan.rec) fan.rec = HC.chart(fan.fig, build);
      else if (fan.rec.chart) { fan.rec.chart.setOption(build(HC.theme.current()), { notMerge: true }); }
      requestAnimationFrame(function () { if (fan.rec && fan.rec.chart) fan.rec.chart.resize(); });
      fanTwin(m, units);
    }
    return p;
  }

  function fanTwin(m, units) {
    var cols = [{ key: 'min', label: 'minutes ahead', numeric: true }];
    var n = 0;
    [['phone', m.fans.phone], ['variant', m.fans.variant]].forEach(function (side) {
      if (!side[1]) return;
      FAN_KINDS.forEach(function (fk) {
        var a = side[1][fk[0]];
        if (!a) return;
        n = Math.max(n, a.length);
        cols.push({ key: side[0] + fk[0], label: side[0] + ' ' + fk[1], numeric: true, arr: a });
      });
    });
    var rows = [];
    for (var k = 0; k < n; k++) {
      var r = { min: String(k * 5) };
      cols.slice(1).forEach(function (c) { r[c.key] = bgText(c.arr[k], units); });
      rows.push(r);
    }
    HC.tableTwin(fan.fig, { columns: cols.map(function (c) { return { key: c.key, label: c.label, numeric: c.numeric }; }), rows: rows,
                            summary: 'Table of the predictions (' + bgUnit(units) + ', ' + rows.length + ' steps)' });
  }

  function renderWords(container, runs, sc) {
    runs.forEach(function (w, k) {
      if (k) container.appendChild(document.createTextNode(' '));
      if (w.op === '=') container.appendChild(document.createTextNode(w.a));
      else if (w.op === '-') container.appendChild(h('del', { text: w.a }));
      else container.appendChild(h('ins', { class: sc, text: w.b }));
    });
  }

  function textPart(m, info, sc) {
    var who = m.isBase ? 'base' : 'variant';
    var p = part('Reason and consoleError', 'Word-level differences between the phone\'s texts and the ' + who + '\'s. Struck words appear only in the phone\'s text; underlined words only in the ' + who + '\'s.');
    p.appendChild(h('h3', { text: 'reason' }));
    if (m.reason.diff) {
      var box = h('div', { class: 'dd-text' });
      renderWords(box, m.reason.diff, sc);
      p.appendChild(box);
    } else p.appendChild(msg(info.why.pred || info.why.predOrig || 'Neither side has a reason text for this loop.'));
    if (m.console.diff) {
      var runs = m.console.diff, cbox = h('div', { class: 'dd-text' });
      runs.forEach(function (r) {
        if (r.op === '=') cbox.appendChild(h('span', { class: 'ln same', text: r.a }));
        else if (r.op === '-') cbox.appendChild(h('span', { class: 'ln gone' }, h('del', { text: r.a })));
        else if (r.op === '+') cbox.appendChild(h('span', { class: 'ln new' }, h('ins', { class: sc, text: r.b })));
        else { var ln = h('span', { class: 'ln' }); renderWords(ln, r.words, sc); cbox.appendChild(ln); }
      });
      var n = diffCount(runs);
      var mixed = (m.console.phone || []).join(' ').indexOf('mmol') >= 0 && (m.console.variant || []).join(' ').indexOf('mg/dL') >= 0;
      p.appendChild(h('details', { class: 'dd-fold', open: true },
        h('summary', { text: 'consoleError, ' + (m.console.phone || []).length + ' lines of the phone and ' + (m.console.variant || []).length + ' of the ' + who + ', ' + n + ' word' + (n === 1 ? '' : 's') + ' differ' }),
        mixed ? msg('Some numbers differ in units only: the replay echoes those lines in mg/dl where the phone printed mmol/l.') : null,
        cbox));
    } else p.appendChild(msg(info.why.predText || info.why.predOrigText || 'Neither side has consoleError lines for this loop.'));
    return p;
  }

  function whyMissing(res, what) {
    if (!res) return null;
    if (res.missing) {
      return what.indexOf('phone') >= 0 ? 'This report has no ' + what + ' document for this day.'
        : 'No ' + what + ' document was written for this variant and day (prediction documents are kept for finalists only).';
    }
    if (res.error) return res.error;
    return null;
  }

  // ------------------------------------------------------------------------------------------------ open / close
  function open(spec) {
    spec = spec || {};
    var day = spec.day, vhash = spec.vhash || baseVhash();
    if (!day) return Promise.resolve(null);
    if (!spec.keepOpener || !opener) opener = spec.opener || (document.activeElement !== document.body ? document.activeElement : null);
    var s = showSheet(), seq = ++state.seq, cfg = dd();
    state.spec = { day: day, t: spec.t, index: spec.index, vhash: vhash };
    s.title.textContent = (num(spec.t) ? hhmm(spec.t) + ' UTC, ' : '') + day;
    HC.clear(s.who).appendChild(document.createTextNode(labelOf(vhash)));
    HC.clear(s.body).appendChild(msg('loading this loop ...'));
    if (!spec.keepFocus) s.close.focus();                 // the variant select keeps the focus while it switches
    var vBase = isBase(vhash), bvh = baseVhash();
    var jobs = {
      orig: load(origPath(day)), series: load(seriesPath(day, vhash)),
      base: vBase || !bvh ? Promise.resolve({ missing: true }) : load(seriesPath(day, bvh)),
      pred: load((cfg.pred[day] || {})[vhash]), predText: load((cfg.pred_text[day] || {})[vhash]),
      predOrig: load(cfg.pred_orig[day]), predOrigText: load(cfg.pred_orig_text[day]),
      basePred: vBase || !bvh ? Promise.resolve({ missing: true }) : load((cfg.pred[day] || {})[bvh])
    };
    var keys = Object.keys(jobs);
    return Promise.all(keys.map(function (k) { return jobs[k]; })).then(function (res) {
      if (seq !== state.seq || !state.open) return null;
      var r = {};
      keys.forEach(function (k, j) { r[k] = res[j]; });
      if (!r.orig.doc) {
        HC.clear(s.body).appendChild(msg(r.orig.error || 'The phone\'s per-loop record of ' + day + ' is not in this report.'));
        return null;
      }
      var docs = {};
      keys.forEach(function (k) { docs[k] = r[k].doc || null; });
      if (spec.hhmm) {                                     // a row of the Scenarios table: the k-th loop at that HH:MM
        var hits = [];
        docs.orig.t.forEach(function (x, j) { if (hhmm(x) === spec.hhmm) hits.push(j); });
        if (hits.length) state.spec.index = hits[Math.min(spec.occurrence || 0, hits.length - 1)];
      }
      var m = buildModel(state.spec, docs, { cfg: cfg, units: pageUnits(), isBase: vBase, basePred: docs.basePred });
      if (!m) { HC.clear(s.body).appendChild(msg('That loop is not in the phone\'s record of ' + day + '.')); return null; }
      m.cfg = cfg;
      state.spec.t = m.t; state.spec.index = m.index;
      state.model = m;
      var info = { slot: slotOf(vhash, day), cfg: cfg,
                   why: { pred: whyMissing(r.pred, 'prediction'), predText: whyMissing(r.predText, 'consoleError'),
                          predOrig: whyMissing(r.predOrig, 'phone prediction'), predOrigText: whyMissing(r.predOrigText, 'phone consoleError') } };
      if (!m.fans.variant && r.pred.doc) info.why.pred = 'The replay kept no prediction for this loop.';
      if (!m.fans.phone && r.predOrig.doc) info.why.predOrig = 'The phone recorded no prediction for this loop.';
      renderSheet(m, info);
      openCbs.forEach(function (cb) { try { cb(m); } catch (e) { console.error('drilldown onOpen', e); } });
      return m;
    });
  }

  function close() {
    if (!state.open || !sheet) return;
    state.open = false;
    state.seq++;
    sheet.root.hidden = true;
    document.documentElement.classList.remove('hc-dd-open');
    if (fan && fan.rec && fan.rec.chart) fan.rec.chart.dispatchAction({ type: 'hideTip' });
    var back = opener;
    opener = null;
    if (back && document.contains(back) && typeof back.focus === 'function') back.focus();
    else { var tab = document.getElementById('tab-loop'); if (tab) tab.focus(); }
    closeCbs.forEach(function (cb) { try { cb(); } catch (e) { console.error('drilldown onClose', e); } });
  }

  // ------------------------------------------------------------------------------------------------ the Loop tab
  var LIST_STEP = 300;
  var tab = { el: null, variant: null, day: null, changedOnly: true, shown: LIST_STEP, seq: 0 };

  function renderTab(el) {
    injectCss();
    tab.el = el;
    el.appendChild(h('div', { class: 'block' },
      h('header', null, h('h2', { text: 'Loop drill-down' }),
        h('p', { text: 'Pick a loop to see the decision pair, both prediction fans and the word-level diff of the reason and consoleError texts. Loops also open from a click in the Scenarios charts or a row of their table.' }))));
    var days = daysWithData();
    if (!days.length) { el.appendChild(h('p', { class: 'placeholder', text: 'No day has per-loop data in this report yet.' })); return; }
    var defs = (scn().defaults || [])[0] || {};
    tab.day = tab.day || (days.indexOf(defs.day) >= 0 ? defs.day : days[0]);
    var vs = variantsOn(tab.day);
    tab.variant = tab.variant && vs.indexOf(tab.variant) >= 0 ? tab.variant : (vs.indexOf(defs.vhash) >= 0 ? defs.vhash : vs[0] || baseVhash());
    var daySel = h('select', { id: 'dd-day' }, days.map(function (d) { return h('option', { value: d, selected: d === tab.day ? true : null, text: d }); }));
    var varSel = h('select', { id: 'dd-variant' });
    var chg = h('input', { type: 'checkbox', id: 'dd-changed', checked: tab.changedOnly ? true : null });
    function fillVariants() {
      HC.clear(varSel);
      variantsOn(tab.day).forEach(function (vh) { varSel.appendChild(h('option', { value: vh, selected: vh === tab.variant ? true : null, text: labelOf(vh) })); });
    }
    fillVariants();
    daySel.addEventListener('change', function () { tab.day = daySel.value; var v2 = variantsOn(tab.day); if (v2.indexOf(tab.variant) < 0) tab.variant = v2[0]; fillVariants(); tab.shown = LIST_STEP; drawList(); });
    varSel.addEventListener('change', function () { tab.variant = varSel.value; tab.shown = LIST_STEP; drawList(); });
    chg.addEventListener('change', function () { tab.changedOnly = chg.checked; tab.shown = LIST_STEP; drawList(); });
    el.appendChild(h('div', { class: 'dd-controls' },
      h('label', { for: 'dd-variant' }, 'variant', varSel),
      h('label', { for: 'dd-day' }, 'day', daySel),
      h('label', { class: 'inline', for: 'dd-changed' }, chg, 'only loops where the variant decides differently from the phone')));
    tab.list = h('div', { class: 'dd-list' });
    el.appendChild(tab.list);
    drawList();
  }

  function drawList() {
    if (!tab.list) return;
    var seq = ++tab.seq, units = pageUnits(), day = tab.day, vh = tab.variant;
    HC.clear(tab.list).appendChild(h('p', { class: 'dd-msg', text: 'loading the loops of ' + day + ' ...' }));
    Promise.all([load(origPath(day)), load(seriesPath(day, vh))]).then(function (res) {
      if (seq !== tab.seq) return;
      var o = res[0].doc, s = res[1].doc;
      HC.clear(tab.list);
      if (!o) { tab.list.appendChild(h('p', { class: 'dd-msg', text: res[0].error || 'The phone\'s per-loop record of this day is not in this report.' })); return; }
      if (!s && res[1].error) tab.list.appendChild(h('p', { class: 'dd-msg', text: res[1].error }));
      var cfg = dd(), rows = [], n2 = function (v) { return num(v) ? fmt.num(v, 2) : '-'; };
      o.t.forEach(function (x, i) {
        var k = indexIn(s, x, cfg.match_s);
        var ch = k >= 0 && s.changed ? !!s.changed[k] : false;
        if (tab.changedOnly && s && !ch) return;
        var low = lowAfter(o, i, cfg.low_mgdl, cfg.low_follow_s);
        rows.push({ i: i, t: x, ch: ch, cells: [hhmm(x), bgText(at(o, 'bg', i), units), n2(at(o, 'ins_req', i)), n2(at(s, 'ins_req', k)),
          n2(at(o, 'smb', i)), n2(at(s, 'smb', k)), n2(at(o, 'tbr', i)), n2(at(s, 'tbr', k)), n2(at(o, 'isf_factor', i)), n2(at(s, 'isf_factor', k)),
          low ? hhmm(low.t) + (low.manual ? ' (manual bolus before, not counted for P)' : '') : ''] });
      });
      var total = o.t.length;
      tab.list.appendChild(h('p', { text: (tab.changedOnly && s ? rows.length + ' of ' + total + ' loops differ from the phone' : total + ' loops') +
        ' on ' + day + '. Select a time to open the loop; a striped time marks a loop where the variant differs from the phone.' }));
      if (!rows.length) { tab.list.appendChild(h('p', { class: 'dd-msg', text: 'No loop differs; clear the checkbox to list every loop.' })); return; }
      var heads = ['time (UTC)', 'BG (' + bgUnit(units) + ')', 'phone insulinReq', 'variant insulinReq', 'phone SMB', 'variant SMB',
                   'phone TBR', 'variant TBR', 'phone factor', 'variant factor', 'a low followed at'];
      var thead = h('tr', null, heads.map(function (x, j) { return h('th', { class: j && j < 10 ? 'n' : null, scope: 'col', text: x }); }));
      var tbody = h('tbody');
      rows.slice(0, tab.shown).forEach(function (r) {
        var tr = h('tr', { 'data-index': String(r.i) });
        r.cells.forEach(function (c, j) {
          if (j === 0) tr.appendChild(h('td', { class: r.ch ? 'chg' : null }, h('button', { type: 'button', class: 'dd-open', text: c, 'aria-label': 'open the loop at ' + c + ' UTC' })));
          else tr.appendChild(h('td', { class: j < 10 ? 'n' : null, text: c }));
        });
        tbody.appendChild(tr);
      });
      tbody.addEventListener('click', function (ev) {
        var tr = ev.target.closest ? ev.target.closest('tr[data-index]') : null;
        if (!tr) return;
        var btn = tr.querySelector('button.dd-open');
        api.open({ day: day, index: parseInt(tr.getAttribute('data-index'), 10), vhash: vh, opener: btn });
      });
      tab.list.appendChild(h('div', { class: 'table-scroll' }, h('table', { class: 'twin' }, h('thead', null, thead), tbody)));
      if (rows.length > tab.shown) {
        tab.list.appendChild(h('button', { type: 'button', class: 'dd-more', text: 'Show ' + Math.min(LIST_STEP, rows.length - tab.shown) + ' more of ' + (rows.length - tab.shown),
          onclick: function () { tab.shown += LIST_STEP; drawList(); } }));
      }
    });
  }

  // ------------------------------------------------------------------------------------------------ entry points
  function entityFor(info) {                                // the lowest slot shown on the clicked day, else base
    var es = (info.entities || []).filter(function (e) { return e.day === info.day; }).sort(function (a, b) { return a.slot - b.slot; });
    return es.length ? es[0].vhash : baseVhash();
  }
  function scenarioRow(tr) {                                // a row of the Scenarios table twin -> open spec
    var rd = tr.getAttribute('data-day'), ri = parseInt(tr.getAttribute('data-index'), 10), rt = parseFloat(tr.getAttribute('data-t'));
    if (rd && (ri === ri || rt === rt)) {                  // the row names its own loop and variant (scenarios.js twin())
      return { day: rd, index: ri === ri ? ri : undefined, t: rt === rt ? rt : undefined,
               vhash: tr.getAttribute('data-vhash') || baseVhash(), opener: tr };
    }
    var fig = tr.closest('figure');                        // an older page: read the day and time back from the cells
    var table = tr.closest('table');
    if (!fig || !table) return null;
    var heads = Array.prototype.map.call(table.querySelectorAll('thead th'), function (th) { return th.textContent; });
    var cells = Array.prototype.map.call(tr.children, function (td) { return td.textContent; });
    var hasDay = heads[0] === 'day';
    var day = hasDay ? cells[0] : (/\d{4}-\d{2}-\d{2}/.exec((fig.querySelector('figcaption .title') || {}).textContent || '') || [])[0];
    var time = cells[hasDay ? 1 : 0];
    if (!day || !time) return null;
    var k = 0, prev = tr.previousElementSibling;
    while (prev) {
      var pc = prev.children;
      if ((hasDay ? pc[0].textContent === day : true) && pc[hasDay ? 1 : 0].textContent === time) k++;
      prev = prev.previousElementSibling;
    }
    var slots = ((HC.scenarios && HC.scenarios.state && HC.scenarios.state.slots) || []).map(function (s, j) { return s ? { vhash: s.vhash, day: s.day, slot: j + 1 } : null; }).filter(Boolean);
    return { day: day, hhmm: time, occurrence: k, vhash: entityFor({ day: day, entities: slots }), opener: tr };
  }

  function hookScenarios() {
    if (HC.scenarios && typeof HC.scenarios.onLoop === 'function' && !hookScenarios.done) {
      hookScenarios.done = true;
      HC.scenarios.onLoop(function (info) {
        var act = document.activeElement;
        if (!act || act === document.body) act = document.querySelector('#view-scenarios figure.chart .plot[tabindex]');
        api.open({ day: info.day, t: info.t, index: info.index, vhash: entityFor(info), opener: act });
      });
    }
  }
  document.addEventListener('click', function (ev) {
    var tr = ev.target && ev.target.closest ? ev.target.closest('#view-scenarios details.twin tbody tr') : null;
    if (!tr) return;
    var spec = scenarioRow(tr);
    if (spec) api.open(spec);
  });
  document.addEventListener('keydown', function (ev) {   // Escape closes the sheet wherever the focus is
    if (ev.key === 'Escape' && state.open) { ev.preventDefault(); ev.stopPropagation(); close(); }
  }, true);
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    var tr = ev.target && ev.target.closest ? ev.target.closest('#view-scenarios details.twin tbody tr[data-dd]') : null;
    if (!tr || ev.target !== tr) return;
    ev.preventDefault();
    var spec = scenarioRow(tr);
    if (spec) api.open(spec);
  });
  document.addEventListener('toggle', function (ev) {      // a Scenarios table twin opened: its rows become focusable
    var d = ev.target;
    if (!d || !d.matches || !d.matches('#view-scenarios details.twin') || !d.open) return;
    injectCss();
    Array.prototype.forEach.call(d.querySelectorAll('tbody tr'), function (tr) {
      if (tr.hasAttribute('data-dd')) return;
      tr.setAttribute('data-dd', '');
      tr.setAttribute('tabindex', '0');
      tr.setAttribute('title', 'open this loop');
    });
  }, true);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', hookScenarios);
  else setTimeout(hookScenarios, 0);

  var api = HC.drilldown = {
    state: state,
    open: function (spec) { hookScenarios(); return open(spec); },
    close: close,
    onOpen: function (cb) { if (typeof cb === 'function') openCbs.push(cb); },
    onClose: function (cb) { if (typeof cb === 'function') closeCbs.push(cb); },
    buildModel: buildModel,
    fanOption: fanOption,
    wordDiff: wordDiff,
    lineDiff: lineDiff,
    parseReason: parseReason,
    lowAfter: lowAfter
  };

  HC.registerView('loop', { title: 'Loop', issue: 'H-405', render: function (el) { hookScenarios(); renderTab(el); } });
})();
