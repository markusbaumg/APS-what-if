/* page.js (H-401 skeleton + H-402 Study overview)

   Data contract (what report.py embeds as <script type="application/json" id="tier1">; export.build_tier1 plus the
   keys report.build_page_data adds):
     honesty, not_testable[], locked[], study{}, status{}, rounds[], trials[], days{day: frozen meta}, base_vhash,
     paths{kind: template}, overview{summary, days{day: numbers}, banner, ties_text}, assumed{key: text},
     page{generated, single_file, inlined[], source, units}
   Tier-2 documents (series/pred) are loaded through HC.loadTier2(path): an inlined copy first
   (a JSON script element whose data-tier2 attribute holds the path, written by --single-file), then fetch(path) relative to
   the page, and when both fail (file:// blocks fetch) the promise rejects with a message that names the two ways out.

   Views register with HC.registerView(id, {title, issue, render(el, data)}); report.py appends templates/views/*.js
   after this file, so later waves add a file, never edit this one. Charts go through HC.chart(figure, build) and
   get a table twin with HC.tableTwin(); both are re-rendered when HC.theme changes. */
(function () {
  'use strict';
  var HC = window.HC = window.HC || {};

  // ------------------------------------------------------------------------------------------------ data access
  function readJsonScript(el) {
    try { return JSON.parse(el.textContent); } catch (e) { console.error('embedded JSON unreadable', el.id, e); return null; }
  }
  var tier1El = document.getElementById('tier1');
  HC.data = tier1El ? readJsonScript(tier1El) : null;
  HC.inline = {};
  Array.prototype.forEach.call(document.querySelectorAll('script[type="application/json"][data-tier2]'), function (el) {
    HC.inline[el.getAttribute('data-tier2')] = el;      // parsed lazily: a single-file page may inline megabytes
  });
  HC.fromDisk = location.protocol === 'file:';

  HC.tier2Path = function (kind, day, vhash) {
    var d = HC.data || {};
    var rows = d.trials || [];
    for (var i = 0; i < rows.length; i++) {              // the trial row names its own files when it has them
      var r = rows[i];
      if (r.day === day && (kind === 'orig' || kind === 'pred_orig' || kind === 'pred_orig_text' || r.vhash === vhash) &&
          r.files && r.files[kind]) return r.files[kind];
    }
    var tpl = (d.paths || {})[kind];
    return tpl ? tpl.replace('{day}', day).replace('{vhash}', vhash || d.base_vhash || '') : null;
  };

  var tier2Cache = {};
  HC.loadTier2 = function (path) {
    if (!path) return Promise.reject(new Error('no such tier-2 document'));
    if (tier2Cache[path]) return tier2Cache[path];
    var p;
    if (HC.inline[path]) {
      var doc = readJsonScript(HC.inline[path]);
      p = doc ? Promise.resolve(doc) : Promise.reject(new Error(path + ': the inlined copy is unreadable'));
    } else if (HC.fromDisk || typeof fetch !== 'function') {
      p = Promise.reject(new Error(HC.text.notFromDisk(path)));
    } else {
      p = fetch(path, { cache: 'no-cache' }).then(function (res) {
        if (!res.ok) throw new Error(path + ': HTTP ' + res.status);
        return res.json();
      }).catch(function (e) {
        throw new Error(HC.text.notFromDisk(path) + ' (' + (e && e.message ? e.message : e) + ')');
      });
    }
    tier2Cache[path] = p;
    p.catch(function () { delete tier2Cache[path]; });
    return p;
  };

  HC.text = {
    notFromDisk: function (path) {
      return 'per-loop data ' + path + ' is not available from disk; open the report through `hc serve <study>` or ' +
             'render it with `hc report <study> --single-file`';
    }
  };

  // ------------------------------------------------------------------------------------------------ formatting
  var fmt = HC.fmt = {
    num: function (v, dp) {
      if (v === null || v === undefined || v !== v) return '-';
      if (typeof v !== 'number') return String(v);
      return v.toLocaleString(undefined, { minimumFractionDigits: dp === undefined ? 2 : dp, maximumFractionDigits: dp === undefined ? 2 : dp });
    },
    signed: function (v, dp) {
      if (v === null || v === undefined || v !== v) return '-';
      var s = fmt.num(Math.abs(v), dp);
      return (v > 0 ? '+' : v < 0 ? '−' : '±') + s;
    },
    pct: function (v, dp) { return v === null || v === undefined ? '-' : fmt.num(v, dp === undefined ? 1 : dp) + ' %'; },
    int: function (v) { return v === null || v === undefined ? '-' : fmt.num(v, 0); },
    age: function (iso, now) {
      if (!iso) return '';
      var t = Date.parse(iso);
      if (isNaN(t)) return '';
      var s = Math.max(0, ((now || Date.now()) - t) / 1000);
      if (s < 90) return Math.round(s) + ' s ago';
      if (s < 5400) return Math.round(s / 60) + ' min ago';
      if (s < 172800) return (s / 3600).toFixed(1) + ' h ago';
      return Math.round(s / 86400) + ' d ago';
    },
    stamp: function (iso) { return iso ? iso.replace('T', ' ').replace('Z', ' UTC') : '-'; },
    sha: function (s) { return s ? String(s).slice(0, 12) : '-'; },
    bg: function (mgdl, units) {
      if (mgdl === null || mgdl === undefined) return '-';
      return units === 'mmol' ? fmt.num(mgdl / 18.0, 1) + ' mmol/l' : fmt.num(mgdl, 0) + ' mg/dl';
    }
  };

  // ------------------------------------------------------------------------------------------------ DOM helper
  var h = HC.h = function (tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'html') el.innerHTML = v;           // only for the page's own literal fragments, never data
        else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : v);
      });
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  };
  function append(el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(el, c); }); return; }
    el.appendChild(typeof child === 'string' || typeof child === 'number' ? document.createTextNode(String(child)) : child);
  }
  HC.clear = function (el) { while (el.firstChild) el.removeChild(el.firstChild); return el; };

  // ------------------------------------------------------------------------------------------------ charts
  var charts = [];
  /* figure: a <figure class="chart"> holding a .plot div. build(tokens, echarts) returns the option. The chart is
     (re)created with the 'hindcast' theme now and on every theme change; a missing library leaves the table twin. */
  HC.chart = function (figure, build) {
    var plot = figure.querySelector('.plot') || figure.appendChild(h('div', { class: 'plot' }));
    var rec = { figure: figure, plot: plot, build: build, chart: null };
    charts.push(rec);
    renderChart(rec);
    if (window.ResizeObserver) {
      new ResizeObserver(function () { if (rec.chart) rec.chart.resize(); }).observe(plot);
    }
    return rec;
  };
  function renderChart(rec) {
    if (!window.echarts) {
      rec.plot.className = 'plot unavailable';
      rec.plot.textContent = 'chart library not loaded (offline?); the table below carries the same numbers';
      return;
    }
    var t = HC.theme.current();
    if (rec.chart) { rec.chart.dispose(); rec.chart = null; }
    rec.plot.className = 'plot';
    rec.plot.textContent = '';
    rec.chart = window.echarts.init(rec.plot, HC.theme.name, { renderer: 'canvas' });
    try {
      rec.chart.setOption(rec.build(t, window.echarts), { notMerge: true });
    } catch (e) {
      console.error('chart option', e);
      rec.plot.className = 'plot unavailable';
      rec.plot.textContent = 'chart could not be drawn: ' + e.message;
    }
  }
  HC.theme.onChange(function () { charts.forEach(renderChart); });
  window.addEventListener('resize', function () { charts.forEach(function (r) { if (r.chart) r.chart.resize(); }); });

  /* The table twin of a chart: columns [{key, label, numeric, format}], rows [{...}]; inside a <details>. */
  HC.tableTwin = function (figure, spec) {
    var old = figure.querySelector('details.twin');
    if (old) old.remove();
    var thead = h('tr');
    spec.columns.forEach(function (c) { thead.appendChild(h('th', { class: c.numeric ? 'n' : null, text: c.label })); });
    var tbody = h('tbody');
    spec.rows.forEach(function (r) {
      var tr = h('tr');
      spec.columns.forEach(function (c) {
        var v = r[c.key];
        tr.appendChild(h('td', { class: c.numeric ? 'n' : null, text: c.format ? c.format(v, r) : (v === null || v === undefined ? '-' : String(v)) }));
      });
      tbody.appendChild(tr);
    });
    var details = h('details', { class: 'twin', open: spec.open || null },
      h('summary', { text: spec.summary || 'Table of the numbers in this chart' }),
      h('div', { class: 'table-scroll' }, h('table', { class: 'twin' }, h('thead', null, thead), tbody)));
    figure.appendChild(details);
    return details;
  };

  HC.figure = function (title, note) {
    return h('figure', { class: 'chart' },
      h('figcaption', null, h('span', { class: 'title', text: title }), note ? h('span', { class: 'note', text: note }) : null),
      h('div', { class: 'plot' }));
  };

  // ------------------------------------------------------------------------------------------------ views + router
  var views = HC.views = {};
  var order = HC.viewOrder = [];
  HC.registerView = function (id, spec) {
    if (!views[id]) order.push(id);
    views[id] = spec;
  };
  HC.placeholderView = function (id, title, issue, what) {
    HC.registerView(id, { title: title, issue: issue, todo: true, render: function (el) {
      el.appendChild(h('div', { class: 'placeholder' },
        h('p', null, h('b', { text: title + ' is not built yet' }), ' (backlog ' + issue + ').'),
        h('p', { class: 'muted', text: what })));
    } });
  };

  var rendered = {};
  function currentId() {
    var id = (location.hash || '').replace(/^#/, '');
    return views[id] ? id : order[0];
  }
  HC.show = function (id) {
    var main = document.getElementById('views');
    if (!main) return;
    order.forEach(function (v) {
      var sec = document.getElementById('view-' + v);
      if (!sec) {
        sec = h('section', { class: 'view', id: 'view-' + v, 'aria-labelledby': 'tab-' + v });
        main.appendChild(sec);
      }
      sec.hidden = v !== id;
      if (v === id && !rendered[v]) {
        rendered[v] = true;
        try { views[v].render(sec, HC.data); } catch (e) {
          console.error('view ' + v, e);
          sec.appendChild(h('p', { class: 'placeholder', text: 'this view failed to render: ' + e.message }));
        }
        charts.forEach(function (r) { if (r.chart) r.chart.resize(); });
      }
    });
    Array.prototype.forEach.call(document.querySelectorAll('nav.tabs a'), function (a) {
      if (a.getAttribute('href') === '#' + id) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
  };
  HC.boot = function () {
    var q = /[?&]theme=(dark|light)\b/.exec(location.search || '');
    if (q && !document.documentElement.hasAttribute('data-theme')) document.documentElement.setAttribute('data-theme', q[1]);
    var nav = document.getElementById('tabs');
    if (nav) {
      HC.clear(nav);
      order.forEach(function (id) {
        nav.appendChild(h('a', { href: '#' + id, id: 'tab-' + id, class: views[id].todo ? 'todo' : null, text: views[id].title }));
      });
    }
    HC.theme.register();
    HC.show(currentId());
    window.addEventListener('hashchange', function () { HC.show(currentId()); });
  };

  // ------------------------------------------------------------------------------------------------ overview view
  function roleLabel(role) { return role === 'holdout' ? 'hold-out' : role; }

  function baseRowsByDay(data) {
    var out = {};
    (data.trials || []).forEach(function (r) {
      if (r.status === 'done' && (r.control === 'base' || r.vhash === data.base_vhash)) out[r.day] = r;
    });
    return out;
  }

  function renderBanner(el, data) {
    var ov = data.overview || {};
    var s = ov.summary || {};
    var banner = h('div', { class: 'banner', role: 'note' },
      h('div', { class: 'eyebrow', text: 'Noise floor of the replay' }),
      h('p', { class: 'lead', text: ov.banner || 'No base replay finished yet; the floor is unknown until the base variant has run on a day.' }));
    if (ov.ties_text) banner.appendChild(h('p', { class: 'ink-2', text: ov.ties_text }));
    var floors = h('div', { class: 'floor-list' });
    if (s.loops_with_base) {
      floors.appendChild(h('span', null, h('b', { text: fmt.pct(s.agreement_min_pct) }), ' all four decisions within tolerance (worst day)'));
      floors.appendChild(h('span', null, h('b', { text: fmt.pct(s.smb_exact_min_pct) }), ' SMB exact (worst day)'));
      floors.appendChild(h('span', null, h('b', { text: fmt.signed(s.smb_bias_U, 2) + ' U' }), ' emulated SMB total vs the phone, over ' + fmt.num(s.hours_total, 1) + ' h replayed'));
      floors.appendChild(h('span', null, h('b', { text: fmt.num(s.tie_rise_U, 2) + ' U / ' + fmt.num(s.tie_rest_U, 2) + ' U' }), ' tie threshold, rising loops / the rest'));
      floors.appendChild(h('span', null, h('b', { text: fmt.int(s.days_with_base) + ' of ' + fmt.int(s.days) }), ' days have a base replay'));
    }
    banner.appendChild(floors);
    el.appendChild(banner);
  }

  function tile(label, value, sub, cls) {
    return h('div', { class: 'tile' },
      h('div', { class: 'label', text: label }),
      h('div', { class: 'value' + (cls ? ' ' + cls : '') }, value),
      sub ? h('div', { class: 'sub' }, sub) : null);
  }

  function renderTiles(el, data) {
    var st = data.status || {};
    var study = data.study || {};
    var page = data.page || {};
    var ov = (data.overview || {}).summary || {};
    var tiles = h('div', { class: 'tiles' });
    tiles.appendChild(tile('Trials', fmt.int(st.trials_done) + ' / ' + fmt.int(st.trials_planned),
      (st.trials_failed ? fmt.int(st.trials_failed) + ' failed, ' : '') + (st.trials_blocked ? fmt.int(st.trials_blocked) + ' blocked, ' : '') + 'done / planned'));
    tiles.appendChild(tile('Phase', st.phase || '-', st.round ? 'round ' + st.round : (study.phases ? study.phases.length + ' phase(s) planned' : '')));
    var hb = st.heartbeat;
    tiles.appendChild(tile('Run', st.running ? h('span', { class: 'pill good', text: 'running' }) : h('span', { class: 'pill', text: 'not running' }),
      hb ? 'heartbeat ' + fmt.stamp(hb) + (fmt.age(hb) ? ', ' + fmt.age(hb) : '') : 'no heartbeat yet'));
    if (st.exit_code !== undefined && st.exit_code !== null) {
      tiles.appendChild(tile('Last run', 'exit ' + st.exit_code, st.stop_reason || '', 'small'));
    }
    tiles.appendChild(tile('Question', study.question || '-',
      study.question === 'explore' ? 'nothing is ranked; the table is in review order' : 'a declared study setting, not a finding', 'small'));
    var live = ov.trials_live || 0, frozen = ov.trials_frozen || 0;
    tiles.appendChild(tile('Data', frozen && !live ? 'frozen days' : live && !frozen ? 'live source' : (frozen || live) ? 'mixed' : '-',
      (frozen ? fmt.int(frozen) + ' trial(s) from frozen days' : '') + (frozen && live ? ', ' : '') +
      (live ? fmt.int(live) + ' trial(s) from the live source, no freeze record' : '') +
      (ov.frozen_days ? '; ' + fmt.int(ov.frozen_final_days) + ' of ' + fmt.int(ov.frozen_days) + ' study days frozen and final now' : ''), 'small'));
    var stale = st.stale_context || [];
    tiles.appendChild(tile('Stale context', stale.length ? h('span', { class: 'pill warning', text: stale.length + ' item(s)' }) : h('span', { class: 'pill good', text: 'none' }),
      stale.length ? stale.join('; ') : 'code and settings unchanged since the trials ran'));
    tiles.appendChild(tile('Stamps', h('span', { class: 'mono', text: fmt.sha(study.code_sha) + ' / ' + fmt.sha(study.settings_sha) }),
      'code_sha / settings_sha; page generated ' + fmt.stamp(page.generated), 'small'));
    el.appendChild(h('div', { class: 'block' }, h('header', null, h('h2', { text: 'Progress and freshness' })), tiles));
  }

  function renderDays(el, data) {
    var study = data.study || {};
    var byDay = baseRowsByDay(data);
    var ovDays = (data.overview || {}).days || {};
    var meta = data.days || {};
    var units = (data.page || {}).units || 'mg/dl';
    var trialsPerDay = {};
    (data.trials || []).forEach(function (r) { trialsPerDay[r.day] = (trialsPerDay[r.day] || 0) + 1; });
    var strip = h('div', { class: 'days' });
    (study.days || []).forEach(function (d) {
      var base = byDay[d.day];
      var m = (base && base.metrics) || {};
      var o = ovDays[d.day] || {};
      var fm = meta[d.day] || {};
      var ch = fm.character || {};
      var card = h('div', { class: 'day-card' + (d.role === 'quarantined' ? ' quarantined' : '') });
      card.appendChild(h('header', null, h('h3', { text: d.day }),
        h('div', { class: 'chips' }, h('span', { class: 'chip role', text: roleLabel(d.role) }),
          (d.tags || []).map(function (t) { return h('span', { class: 'chip user', text: t }); }))));
      if (d.role === 'quarantined') card.appendChild(h('div', null, h('span', { class: 'pill serious', text: 'quarantined: ' + (d.quarantine_reason || 'no reason recorded') })));
      var dl = h('dl');
      function row(k, v) { dl.appendChild(h('dt', { text: k })); dl.appendChild(h('dd', null, v)); }
      row('loops', base ? fmt.int(m.loops) + (o.hours ? ' in ' + fmt.num(o.hours, 1) + ' h' : '') : 'no base replay yet');
      row('trials', fmt.int(trialsPerDay[d.day] || 0));
      if (base) {
        row('base vs phone', fmt.pct(m.agreement_pct) + ' agree, SMB ' + fmt.signed(m.smb_emul_U - m.smb_orig_U, 2) + ' U');
        row('floor', 'rise ' + fmt.num(o.floor_rise_U, 2) + ' U, rest ' + fmt.num(o.floor_rest_U, 2) + ' U, ' + fmt.int(o.floor_loops) + ' loops');
        if (m.lows_recorded !== undefined) row('recorded lows', fmt.int(m.lows_recorded) + ' (positional marker only)');
      }
      if (fm.character) {
        row('whole UTC day', fmt.int(fm.loops) + ' loops, insulin ' + fmt.num(ch.tdd_U, 1) + ' U, ' + fmt.num(ch.carbs_g, 0) + ' g carbs' +
          (ch.tt_minutes ? ', temp target ' + fmt.int(ch.tt_minutes) + ' min' : '') + (ch.activity_min ? ', activity ' + fmt.int(ch.activity_min) + ' min' : ''));
        row('mean glucose', fmt.bg(ch.bg_mean, units) + ' (describes the day, never a score)');
        if (ch.cgm_gap_min) row('longest CGM gap', fmt.int(ch.cgm_gap_min) + ' min');
        row('frozen', fm.final ? 'final' : 'not final' + (fm.partial ? ' (partial)' : ''));
      } else {
        row('frozen', 'no frozen record for this day');
      }
      card.appendChild(dl);
      var conf = fm.confounders || [];
      if (conf.length) card.appendChild(h('div', { class: 'chips' }, conf.map(function (c) { return h('span', { class: 'pill warning', text: c }); })));
      strip.appendChild(card);
    });
    el.appendChild(h('div', { class: 'block' },
      h('header', null, h('h2', { text: 'Days' }), h('p', { text: 'Days are the unit of everything. Chips are what you typed into study.json; the record describes the day and is never a score.' })),
      strip));
  }

  function renderFloorChart(el, data) {
    var ovDays = (data.overview || {}).days || {};
    var days = Object.keys(ovDays).filter(function (d) { return ovDays[d].has_base; }).sort();
    if (!days.length) return;
    var fig = HC.figure('Replay floor by day', '|base - phone| in U; a variant inside twice this is a tie on that component');
    if (days.length < 2) {                                // one day: two bars are no chart; the table carries it
      fig.querySelector('.plot').remove();
    } else HC.chart(fig, function (t) {
      return {
        grid: { left: 90, right: 24, top: 28, bottom: 24, containLabel: false },
        legend: { top: 0, left: 0, data: ['rising loops', 'the rest'] },
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: function (v) { return fmt.num(v, 2) + ' U'; } },
        xAxis: { type: 'value', name: 'U', nameLocation: 'end', axisLabel: { formatter: function (v) { return fmt.num(v, 2); } } },
        yAxis: { type: 'category', data: days, inverse: true },
        series: [
          { name: 'rising loops', type: 'bar', data: days.map(function (d) { return ovDays[d].floor_rise_U; }), itemStyle: { color: t['seq-4'], borderRadius: [0, 4, 4, 0] }, barGap: '20%' },
          { name: 'the rest', type: 'bar', data: days.map(function (d) { return ovDays[d].floor_rest_U; }), itemStyle: { color: t['seq-2'], borderRadius: [0, 4, 4, 0] } }
        ]
      };
    });
    HC.tableTwin(fig, {
      columns: [
        { key: 'day', label: 'day' },
        { key: 'loops', label: 'loops', numeric: true, format: fmt.int },
        { key: 'floor_rise_U', label: 'floor rise (U)', numeric: true, format: function (v) { return fmt.num(v, 3); } },
        { key: 'floor_rest_U', label: 'floor rest (U)', numeric: true, format: function (v) { return fmt.num(v, 3); } },
        { key: 'floor_total_U', label: 'floor total (U)', numeric: true, format: function (v) { return fmt.num(v, 3); } },
        { key: 'floor_loops', label: 'loops changed by the replay', numeric: true, format: fmt.int },
        { key: 'tie_rise_U', label: 'tie below, rise (U)', numeric: true, format: function (v) { return fmt.num(v, 2); } },
        { key: 'tie_rest_U', label: 'tie below, rest (U)', numeric: true, format: function (v) { return fmt.num(v, 2); } },
        { key: 'agreement_pct', label: 'agreement', numeric: true, format: function (v) { return fmt.pct(v); } }
      ],
      rows: days.map(function (d) { var o = ovDays[d]; o.day = d; return o; }),
      open: days.length < 2
    });
    el.appendChild(fig);
  }

  function renderLimits(el, data) {
    var left = h('div', { class: 'block' },
      h('header', null, h('h2', { text: 'Cannot be tested with this method' }),
        h('p', { text: 'These enter through the inputs the replay takes as recorded; a study cannot vary them.' })),
      h('ul', { class: 'plain-list' }, (data.not_testable || []).map(function (x) { return h('li', { text: x }); })));
    var dead = ((data.rounds || []).slice(-1)[0] || {}).dead_parameters || [];
    if (dead.length) {
      left.appendChild(h('h3', { text: 'Not testable on these days' }));
      left.appendChild(h('ul', { class: 'plain-list' }, dead.map(function (d) {
        return h('li', null, h('code', { text: d.key }), ' - ' + d.why + (d.floor_ratio !== undefined && d.floor_ratio !== null ? ' (largest effect ' + fmt.num(d.floor_ratio, 2) + ' x floor)' : ''));
      })));
    }
    var locked = data.locked || [];
    left.appendChild(h('details', { class: 'fold' }, h('summary', { text: 'Locked for unattended runs (' + locked.length + ' keys)' }),
      h('p', { class: 'muted', text: 'Refused in study.json with no override; a human can still set them in the plain emulator.' }),
      h('ul', { class: 'plain-list' }, locked.map(function (k) { return h('li', null, h('code', { text: k })); }))));

    var assumed = data.assumed || {};
    var keys = Object.keys(assumed);
    var dl = h('dl', { class: 'kv' });
    keys.forEach(function (k) { dl.appendChild(h('div', null, h('dt', { text: k }), h('dd', { text: assumed[k] }))); });
    var right = h('div', { class: 'block' },
      h('header', null, h('h2', { text: 'Assumed settings' }),
        h('p', { text: keys.length ? 'Values in ns_settings.json that were inferred rather than exported from the phone (its _assumed section). Every replay carries them.' : 'ns_settings.json lists no assumed values.' })),
      keys.length ? dl : null);
    el.appendChild(h('div', { class: 'two-col' }, left, right));
  }

  function renderNotes(el, data) {
    var study = data.study || {};
    var notes = (study.notes || []).slice();
    var space = study.space || {};
    var params = Object.keys(space);
    var block = h('div', { class: 'block' }, h('header', null, h('h2', { text: 'Study' })));
    var dl = h('dl', { class: 'kv' });
    function row(k, v) { dl.appendChild(h('div', null, h('dt', { text: k }), h('dd', null, v))); }
    row('source', (study.source || '-') + (study.host ? ' (' + study.host + ')' : ''));
    row('created', fmt.stamp(study.created));
    row('parameters', params.length ? params.map(function (p) {
      var sp = space[p];
      var desc = sp.type === 'choice' ? (sp.choices || []).join(', ') : sp.type === 'bool' ? 'false, true' : sp.low + ' .. ' + sp.high + ' step ' + sp.step;
      return h('div', null, h('code', { text: p }), ' ' + desc + (sp.phone !== undefined && sp.phone !== null ? ' (phone: ' + sp.phone + ')' : ''));
    }) : 'none');
    row('phases', (study.phases || []).map(function (p) { return p.kind + ' [' + p.state + ']'; }).join(', ') || '-');
    row('budget', (study.budget ? study.budget.hours + ' h, ' + study.budget.workers + ' worker(s)' : '-'));
    if (study.core_versions) row('emulator', Object.keys(study.core_versions).map(function (k) { return k + ' ' + study.core_versions[k]; }).join('; '));
    block.appendChild(dl);
    if (notes.length) block.appendChild(h('ul', { class: 'plain-list' }, notes.map(function (n) { return h('li', { text: n }); })));
    el.appendChild(block);
  }

  HC.registerView('overview', { title: 'Overview', render: function (el, data) {
    if (!data) { el.appendChild(h('p', { class: 'placeholder', text: 'the embedded study data is missing or unreadable' })); return; }
    renderBanner(el, data);
    renderTiles(el, data);
    renderDays(el, data);
    renderFloorChart(el, data);
    renderLimits(el, data);
    renderNotes(el, data);
  } });

  HC.placeholderView('parameters', 'Parameters', 'H-403', 'one panel per searched parameter, the leaderboard in review order with the floor band, the delta heatmap');
  HC.placeholderView('scenarios', 'Scenarios', 'H-404', 'small multiples with a linked crosshair and the per-loop difference strip');
  HC.placeholderView('loop', 'Loop', 'H-405', 'the drill-down into one loop: decision pair, prediction fans, text diff');
  HC.placeholderView('candidates', 'Candidates', 'H-408', 'candidate cards and the round log');
})();
