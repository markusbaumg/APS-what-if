/* theme.js (H-401): the ECharts theme is built from the CSS tokens with getComputedStyle, so the page's palette
   (page.css) is the only place a colour is defined. Rebuilt on a prefers-color-scheme change or a data-theme
   mutation; every chart created through HC.chart() is re-initialised with the new theme. ECharts' built-in 'dark'
   theme is never used. Loaded before page.js; defines window.HC.theme. */
(function () {
  'use strict';
  var HC = window.HC = window.HC || {};
  var TOKENS = ['page', 'surface', 'surface-2', 'ink', 'ink-2', 'muted', 'grid', 'axis', 'border',
                'series-1', 'series-2', 'series-3', 'phone', 'div-neg', 'div-mid', 'div-pos',
                'seq-1', 'seq-2', 'seq-3', 'seq-4', 'seq-5',
                'status-good', 'status-warning', 'status-serious', 'status-critical', 'good-text', 'font', 'mono'];
  var listeners = [];
  var current = null;
  var THEME_NAME = 'hindcast';

  function read() {
    var cs = getComputedStyle(document.documentElement);
    var t = {};
    TOKENS.forEach(function (name) { t[name] = cs.getPropertyValue('--' + name).trim(); });
    t.dark = isDark();
    return t;
  }

  function isDark() {
    var attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'dark') return true;
    if (attr === 'light') return false;
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }

  /* An ECharts theme object from the tokens: recessive grid and axes, text in the ink tokens, 2 px lines, >= 8 px
     markers, no animation when the viewer asked for reduced motion. Series colours are set per chart by the view
     (slot 1-3 by entity, phone grey) - the theme's `color` list is only the fallback order. */
  function echartsTheme(t) {
    var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var axis = {
      axisLine: { lineStyle: { color: t.axis } },
      axisTick: { show: false },
      axisLabel: { color: t.muted, fontFamily: t.font, fontSize: 11 },
      nameTextStyle: { color: t['ink-2'], fontFamily: t.font, fontSize: 11 },
      splitLine: { lineStyle: { color: t.grid, width: 1 } },
      splitArea: { show: false }
    };
    return {
      color: [t['series-1'], t['series-2'], t['series-3']],
      backgroundColor: 'transparent',
      textStyle: { color: t.ink, fontFamily: t.font },
      title: { textStyle: { color: t.ink, fontFamily: t.font, fontWeight: 600, fontSize: 13 },
               subtextStyle: { color: t['ink-2'], fontFamily: t.font } },
      legend: { textStyle: { color: t['ink-2'], fontFamily: t.font }, icon: 'roundRect', itemWidth: 14, itemHeight: 8 },
      tooltip: { backgroundColor: t.surface, borderColor: t.axis, borderWidth: 1,
                 textStyle: { color: t.ink, fontFamily: t.font, fontSize: 12 },
                 axisPointer: { lineStyle: { color: t.muted, width: 1 }, crossStyle: { color: t.muted } } },
      categoryAxis: axis, valueAxis: axis, timeAxis: axis, logAxis: axis,
      line: { lineStyle: { width: 2 }, symbolSize: 8, showSymbol: false, smooth: false },
      bar: { barMaxWidth: 14, itemStyle: { borderRadius: [0, 4, 4, 0] } },
      scatter: { symbolSize: 8 },
      dataZoom: { borderColor: t.grid, fillerColor: 'rgba(137,135,129,0.15)', handleStyle: { color: t.muted },
                  textStyle: { color: t.muted }, dataBackground: { lineStyle: { color: t.axis }, areaStyle: { color: t.grid } } },
      visualMap: { textStyle: { color: t['ink-2'], fontFamily: t.font } },
      animation: !reduced,
      animationDuration: reduced ? 0 : 300
    };
  }

  function register() {
    current = read();
    if (window.echarts && typeof window.echarts.registerTheme === 'function') {
      window.echarts.registerTheme(THEME_NAME, echartsTheme(current));
    }
    return current;
  }

  function rebuild() {
    register();
    document.documentElement.classList.toggle('is-dark', current.dark);
    listeners.forEach(function (cb) { try { cb(current); } catch (e) { console.error('theme listener', e); } });
  }

  HC.theme = {
    name: THEME_NAME,
    tokens: TOKENS,
    read: read,
    isDark: isDark,
    echartsTheme: echartsTheme,
    current: function () { return current || register(); },
    register: register,
    rebuild: rebuild,
    onChange: function (cb) { listeners.push(cb); },
    /* the three variant slots and the phone, as the picker hands them out (slot 4 is refused by the picker) */
    seriesColor: function (slot) { var t = HC.theme.current(); return t['series-' + slot] || t.muted; },
    phoneColor: function () { return HC.theme.current().phone; }
  };

  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) mq.addEventListener('change', rebuild);
    else if (mq.addListener) mq.addListener(rebuild);
  }
  if (window.MutationObserver) {
    new MutationObserver(rebuild).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  }
})();
