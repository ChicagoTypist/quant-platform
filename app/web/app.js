/* =========================================================================
   A股量化交易平台 - 前端逻辑
   纯原生 JS，不依赖任何外部库（离线也能用）
   ========================================================================= */

/* ---------------- 基础工具 ---------------- */
function $(id) { return document.getElementById(id); }
function $$(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
function on(el, ev, fn) { if (el) el.addEventListener(ev, fn); }

function fmtNum(v, d) {
  if (v === null || v === undefined || v === '' || isNaN(v)) return '—';
  var n = Number(v);
  if (d === undefined) d = 2;
  return n.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtMoney(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  var n = Number(v);
  var s = Math.abs(n).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (n < 0 ? '-' : '') + s;
}
function fmtBig(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  var n = Number(v);
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿';
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万';
  return n.toFixed(0);
}
function cls(v) { if (v === null || v === undefined || isNaN(v)) return 'muted'; return Number(v) > 0 ? 'up' : (Number(v) < 0 ? 'down' : 'muted'); }
function sign(v, d) { if (v === null || v === undefined || isNaN(v)) return '—'; var n = Number(v); return (n > 0 ? '+' : '') + n.toFixed(d === undefined ? 2 : d); }
function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"]/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]; }); }

function toast(msg, kind, ms) {
  var box = $('toasts');
  var d = document.createElement('div');
  d.className = 'toast ' + (kind || '');
  d.innerHTML = esc(msg);
  box.appendChild(d);
  setTimeout(function () { d.style.opacity = '0'; d.style.transition = 'opacity .3s'; }, (ms || 4200) - 300);
  setTimeout(function () { if (d.parentNode) d.parentNode.removeChild(d); }, ms || 4200);
}

function api(path, opt) {
  opt = opt || {};
  var url = path;
  if (opt.params) {
    var qs = [];
    for (var k in opt.params) {
      if (opt.params[k] !== undefined && opt.params[k] !== null && opt.params[k] !== '') {
        qs.push(encodeURIComponent(k) + '=' + encodeURIComponent(opt.params[k]));
      }
    }
    if (qs.length) url += (url.indexOf('?') >= 0 ? '&' : '?') + qs.join('&');
  }
  var init = { method: opt.method || 'GET', headers: { 'Content-Type': 'application/json' } };
  if (opt.body !== undefined) init.body = JSON.stringify(opt.body);
  return fetch(url, init).then(function (r) {
    return r.json().catch(function () { return { ok: false, message: '服务器返回了无法解析的内容 (HTTP ' + r.status + ')' }; });
  }).catch(function (e) {
    return { ok: false, message: '网络错误：' + e.message };
  });
}

/* ---------------- 全局状态 ---------------- */
var S = {
  boot: null, cfg: null, symbol: null, period: 'daily', chartData: null,
  templates: [], strategies: [], brokers: [], activeBroker: 'paper',
  rankType: 'gainers', currentTab: 'dash', refreshTimer: null, logTimer: null
};

/* =========================================================================
   画图引擎
   ========================================================================= */
function setupCanvas(cv) {
  var dpr = window.devicePixelRatio || 1;
  var w = cv.clientWidth || cv.parentNode.clientWidth || 800;
  var h = parseInt(cv.getAttribute('data-h') || cv.getAttribute('height') || 300, 10);
  cv.setAttribute('data-h', h);
  cv.style.height = h + 'px';
  cv.width = Math.floor(w * dpr);
  cv.height = Math.floor(h * dpr);
  var ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx: ctx, w: w, h: h };
}

var UP = '#f85149', DOWN = '#3fb950', GRID = '#1e2530', AXIS = '#6b7684', FG = '#e6edf3';

function drawKline(cv, d, hover) {
  if (!d || !d.klines || !d.klines.length) return;
  var g = setupCanvas(cv), ctx = g.ctx, W = g.w, H = g.h;
  var kl = d.klines, ind = d.indicators || {};
  var n = kl.length;
  var padL = 6, padR = 62, padT = 10, padB = 18;
  var subH = $('mkt-sub1').checked ? 76 : 0;
  var volH = 50;
  var mainH = H - padT - padB - subH - volH - 6;
  if (mainH < 60) { mainH = 60; }
  var plotW = W - padL - padR;

  var lo = Infinity, hi = -Infinity;
  for (var i = 0; i < n; i++) {
    if (kl[i].low < lo) lo = kl[i].low;
    if (kl[i].high > hi) hi = kl[i].high;
  }
  if ($('mkt-boll').checked && ind.boll_up) {
    for (var i2 = 0; i2 < n; i2++) {
      if (ind.boll_up[i2] != null && ind.boll_up[i2] > hi) hi = ind.boll_up[i2];
      if (ind.boll_low[i2] != null && ind.boll_low[i2] < lo) lo = ind.boll_low[i2];
    }
  }
  var pad = (hi - lo) * 0.06 || 1;
  hi += pad; lo -= pad;
  var bw = plotW / n;
  var cw = Math.max(1, Math.min(bw * 0.68, 14));

  function X(i) { return padL + (i + 0.5) * bw; }
  function Y(p) { return padT + (hi - p) / (hi - lo) * mainH; }

  ctx.font = '11px Consolas,Monaco,monospace';
  /* 网格 + 价格轴 */
  for (var k = 0; k <= 4; k++) {
    var y = padT + mainH * k / 4;
    var p = hi - (hi - lo) * k / 4;
    ctx.strokeStyle = GRID; ctx.beginPath(); ctx.moveTo(padL, Math.round(y) + .5); ctx.lineTo(padL + plotW, Math.round(y) + .5); ctx.stroke();
    ctx.fillStyle = AXIS; ctx.textAlign = 'left';
    ctx.fillText(p.toFixed(2), padL + plotW + 5, y + 3.5);
  }
  /* 日期轴 */
  ctx.textAlign = 'center';
  var step = Math.max(1, Math.floor(n / 7));
  for (var j = 0; j < n; j += step) {
    ctx.fillStyle = AXIS;
    ctx.fillText(String(kl[j].date).slice(5, 10), X(j), H - 5);
  }

  /* 成交量 */
  var vmax = 0;
  for (var v = 0; v < n; v++) if (kl[v].volume > vmax) vmax = kl[v].volume;
  var volTop = padT + mainH + 8;
  for (var b = 0; b < n; b++) {
    var vh = vmax > 0 ? (kl[b].volume / vmax) * (volH - 6) : 0;
    ctx.fillStyle = kl[b].close >= kl[b].open ? 'rgba(248,81,73,.55)' : 'rgba(63,185,80,.55)';
    ctx.fillRect(X(b) - cw / 2, volTop + (volH - 6) - vh, cw, vh);
  }
  ctx.fillStyle = AXIS; ctx.textAlign = 'left';
  ctx.fillText('成交量 ' + fmtBig(vmax), padL + 2, volTop + 10);

  /* K 线 */
  for (var c = 0; c < n; c++) {
    var kk = kl[c];
    var up = kk.close >= kk.open;
    ctx.strokeStyle = up ? UP : DOWN;
    ctx.fillStyle = up ? UP : DOWN;
    var x = X(c);
    ctx.beginPath(); ctx.moveTo(Math.round(x) + .5, Y(kk.high)); ctx.lineTo(Math.round(x) + .5, Y(kk.low)); ctx.stroke();
    var yo = Y(kk.open), yc = Y(kk.close);
    var top = Math.min(yo, yc), hgt = Math.max(Math.abs(yc - yo), 1);
    if (up) { ctx.fillRect(x - cw / 2, top, cw, hgt); }
    else { ctx.fillRect(x - cw / 2, top, cw, hgt); }
  }

  /* 指标线 */
  function line(arr, color, width) {
    if (!arr) return;
    ctx.strokeStyle = color; ctx.lineWidth = width || 1.1; ctx.beginPath();
    var started = false;
    for (var i = 0; i < n; i++) {
      var v = arr[i];
      if (v == null) { started = false; continue; }
      if (!started) { ctx.moveTo(X(i), Y(v)); started = true; } else { ctx.lineTo(X(i), Y(v)); }
    }
    ctx.stroke(); ctx.lineWidth = 1;
  }
  if ($('mkt-ma').checked) {
    line(ind.ma5, '#f0b429'); line(ind.ma10, '#58a6ff');
    line(ind.ma20, '#bc8cff'); line(ind.ma60, '#39c5cf');
  }
  if ($('mkt-boll').checked) { line(ind.boll_up, '#8b949e'); line(ind.boll_mid, '#8b949e'); line(ind.boll_low, '#8b949e'); }

  /* 副图 MACD / RSI */
  if (subH > 0) {
    var st = padT + mainH + volH + 12;
    ctx.strokeStyle = GRID; ctx.beginPath(); ctx.moveTo(padL, st - 6); ctx.lineTo(padL + plotW, st - 6); ctx.stroke();
    var sh = subH - 12;
    if (ind.macd_hist) {
      var mh = ind.macd_hist, mmax = 1e-9;
      for (var q = 0; q < n; q++) if (mh[q] != null && Math.abs(mh[q]) > mmax) mmax = Math.abs(mh[q]);
      var zero = st + sh / 2;
      for (var q2 = 0; q2 < n; q2++) {
        if (mh[q2] == null) continue;
        var hh = (mh[q2] / mmax) * (sh / 2 - 2);
        ctx.fillStyle = mh[q2] >= 0 ? UP : DOWN;
        ctx.fillRect(X(q2) - cw / 2, hh >= 0 ? zero - hh : zero, cw, Math.max(Math.abs(hh), 1));
      }
      ctx.strokeStyle = GRID; ctx.beginPath(); ctx.moveTo(padL, zero); ctx.lineTo(padL + plotW, zero); ctx.stroke();
      function mline(arr, color) {
        if (!arr) return; ctx.strokeStyle = color; ctx.beginPath(); var s2 = false;
        for (var i = 0; i < n; i++) {
          if (arr[i] == null) { s2 = false; continue; }
          var y = zero - (arr[i] / mmax) * (sh / 2 - 2);
          if (!s2) { ctx.moveTo(X(i), y); s2 = true; } else { ctx.lineTo(X(i), y); }
        }
        ctx.stroke();
      }
      mline(ind.macd_dif, '#f0b429'); mline(ind.macd_dea, '#58a6ff');
      ctx.fillStyle = AXIS; ctx.textAlign = 'left'; ctx.fillText('MACD', padL + 2, st + 10);
    } else if (ind.rsi14) {
      ctx.strokeStyle = GRID;
      [30, 70].forEach(function (lv) {
        var y = st + sh - (lv / 100) * sh;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
      });
      function rline(arr, color) {
        ctx.strokeStyle = color; ctx.beginPath(); var s3 = false;
        for (var i = 0; i < n; i++) {
          if (arr[i] == null) { s3 = false; continue; }
          var y = st + sh - (arr[i] / 100) * sh;
          if (!s3) { ctx.moveTo(X(i), y); s3 = true; } else { ctx.lineTo(X(i), y); }
        }
        ctx.stroke();
      }
      rline(ind.rsi6, '#f0b429'); rline(ind.rsi14, '#58a6ff');
      ctx.fillStyle = AXIS; ctx.textAlign = 'left'; ctx.fillText('RSI', padL + 2, st + 10);
    }
  }

  /* 十字光标 */
  if (hover != null && hover >= 0 && hover < n) {
    var hx = X(hover);
    ctx.strokeStyle = '#8b949e'; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(Math.round(hx) + .5, padT); ctx.lineTo(Math.round(hx) + .5, padT + mainH); ctx.stroke();
    var k3 = kl[hover];
    ctx.beginPath(); ctx.moveTo(padL, Y(k3.close)); ctx.lineTo(padL + plotW, Y(k3.close)); ctx.stroke();
    ctx.setLineDash([]);
    var lines = [
      k3.date, '开 ' + k3.open.toFixed(2) + '  收 ' + k3.close.toFixed(2),
      '高 ' + k3.high.toFixed(2) + '  低 ' + k3.low.toFixed(2),
      '量 ' + fmtBig(k3.volume) + (k3.change_pct != null ? '   涨跌 ' + sign(k3.change_pct) + '%' : '')
    ];
    var bw2 = 190, bh = 14 + lines.length * 15;
    var bx = hx + 12; if (bx + bw2 > W) bx = hx - bw2 - 12;
    if (bx < padL) bx = padL;
    ctx.fillStyle = 'rgba(22,27,34,.95)'; ctx.strokeStyle = '#2a3240';
    ctx.fillRect(bx, padT + 6, bw2, bh); ctx.strokeRect(bx + .5, padT + 6.5, bw2, bh);
    ctx.fillStyle = FG; ctx.textAlign = 'left';
    for (var li = 0; li < lines.length; li++) ctx.fillText(lines[li], bx + 8, padT + 22 + li * 15);
  }
}

function drawLines(cv, series, opts) {
  opts = opts || {};
  if (!series || !series.length) return;
  var g = setupCanvas(cv), ctx = g.ctx, W = g.w, H = g.h;
  var padL = 6, padR = 66, padT = 10, padB = 20;
  var plotW = W - padL - padR, plotH = H - padT - padB;
  var n = 0, lo = Infinity, hi = -Infinity;
  series.forEach(function (s) {
    n = Math.max(n, s.data.length);
    s.data.forEach(function (v) { if (v < lo) lo = v; if (v > hi) hi = v; });
  });
  if (!isFinite(lo)) return;
  var pad = (hi - lo) * 0.08 || 1; hi += pad; lo -= pad;
  function X(i) { return padL + (n <= 1 ? 0 : (i / (n - 1)) * plotW); }
  function Y(v) { return padT + (hi - v) / (hi - lo) * plotH; }
  ctx.font = '11px Consolas,Monaco,monospace';
  for (var k = 0; k <= 4; k++) {
    var y = padT + plotH * k / 4, v = hi - (hi - lo) * k / 4;
    ctx.strokeStyle = GRID; ctx.beginPath(); ctx.moveTo(padL, Math.round(y) + .5); ctx.lineTo(padL + plotW, Math.round(y) + .5); ctx.stroke();
    ctx.fillStyle = AXIS; ctx.textAlign = 'left'; ctx.fillText(fmtBig(v), padL + plotW + 5, y + 3.5);
  }
  series.forEach(function (s) {
    ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 1.5; ctx.beginPath();
    var started = false;
    for (var i = 0; i < s.data.length; i++) {
      var v = s.data[i];
      if (v == null) { started = false; continue; }
      if (!started) { ctx.moveTo(X(i), Y(v)); started = true; } else { ctx.lineTo(X(i), Y(v)); }
    }
    ctx.stroke(); ctx.lineWidth = 1;
  });
  if (opts.legend) {
    var lx = padL + 6, ly = padT + 14;
    series.forEach(function (s) {
      if (!s.name) return;
      ctx.fillStyle = s.color; ctx.fillRect(lx, ly - 8, 10, 3);
      ctx.fillStyle = AXIS; ctx.textAlign = 'left'; ctx.fillText(s.name, lx + 14, ly - 3.5);
      lx += 14 + ctx.measureText(s.name).width + 18;
    });
  }
}

/* =========================================================================
   启动
   ========================================================================= */
function boot() {
  api('/api/bootstrap').then(function (r) {
    S.boot = r; S.cfg = r.config; S.templates = r.templates || [];
    S.brokers = r.brokers || []; S.activeBroker = r.active_broker || 'paper';
    renderHeader(r);
    fillTemplateSelect($('st-template'));
    fillTemplateSelect($('bt-template'));
    presetOptions($('st-wpreset'));
    renderStrategyParams($('st-template'), $('st-params'));
    renderStrategyParams($('bt-template'), $('bt-params'));
    renderSettings();
    buildLogSources();
    refreshAll();
    S.refreshTimer = setInterval(refreshLoop, Math.max(3, (S.cfg.ui && S.cfg.ui.refresh_sec) || 5) * 1000);
    S.logTimer = setInterval(function () { if (S.currentTab === 'logs' && $('log-auto').checked) loadLogs(); }, 5000);
  });
}

function renderHeader(r) {
  if (r.version && $('app-ver')) {
    $('app-ver').textContent = 'v' + r.version + (r.version_name ? ' · ' + r.version_name : '');
  }
  var m = r.market || {};
  var pm = $('pill-market');
  pm.textContent = (m.label || '未知') + (m.now ? ' · ' + String(m.now).slice(11, 16) : '');
  pm.className = 'pill ' + (m.session_open ? 'green' : '');
  var e = r.engine || {};
  var pe = $('pill-engine');
  pe.textContent = '引擎：' + (e.running ? '运行中' : '停止') + (e.halted ? '（已熔断）' : '');
  pe.className = 'pill ' + (e.running ? 'green' : '');
  var b = null;
  for (var i = 0; i < (r.brokers || []).length; i++) if (r.brokers[i].name === r.active_broker) b = r.brokers[i];
  var pb = $('pill-broker');
  pb.textContent = '通道：' + (b ? b.display : r.active_broker) + (b && b.connected ? ' ✓' : '');
  pb.className = 'pill ' + (b && b.connected ? 'green' : '');
  var live = $('pill-live');
  live.style.display = (e.live_enabled && e.is_live) ? 'inline-block' : 'none';
}

var _refreshing = false;

function refreshLoop() {
  // 关键：上一轮没回来之前不要再发。否则网络一慢，请求堆起来，
  // 浏览器 6 个并发连接被占满，整个界面就会「卡住」。
  if (_refreshing) return;
  if (document.hidden) return;          // 页面在后台就别刷了
  if ($('modal') && $('modal').style.display === 'flex') return;   // 弹窗开着时别打扰用户
  _refreshing = true;
  api('/api/bootstrap').then(function (r) {
    S.boot = r; S.cfg = r.config; S.brokers = r.brokers || []; S.activeBroker = r.active_broker;
    renderHeader(r);
    loadIndices();                       // 指数以前只在开页面时取一次，现在跟着刷新
    if (S.currentTab === 'dash') loadDash();
    if (S.currentTab === 'trade') loadTrade();
    if (S.currentTab === 'market') { loadWatchlist(); loadRank(); if (S.symbol) loadQuote(); }
    if (S.currentTab === 'strategy') loadStrategies();
    if (S.currentTab === 'trade') loadRuntime();
  }).then(function () { _refreshing = false; },
          function () { _refreshing = false; });
}

function refreshAll() {
  loadDash(); loadIndices(); loadWatchlist(); loadRank(); loadStrategies(); loadLivePanel(); loadBrokers(); loadHistory();
}

/* =========================================================================
   标签切换
   ========================================================================= */
function switchTab(name) {
  S.currentTab = name;
  $$('.tabbtn').forEach(function (b) { b.classList.toggle('active', b.getAttribute('data-tab') === name); });
  $$('.tab').forEach(function (t) { t.classList.toggle('active', t.id === 'tab-' + name); });
  if (name === 'dash') loadDash();
  if (name === 'market') { loadWatchlist(); loadRank(); if (S.symbol) loadChart(); }
  if (name === 'strategy') loadStrategies();
  if (name === 'factor') { loadFactorTab(); loadDlFactorTab(); }
  if (name === 'screener') loadScreener();
  if (name === 'optimizer') loadOptimizerTab();
  if (name === 'ai') loadAssistantTab();
  if (name === 'backtest') { loadHistory(); }
  if (name === 'trade') loadTrade();
  if (name === 'settings') renderSettings();
  if (name === 'logs') loadLogs();
}

/* =========================================================================
   自动选股
   ========================================================================= */
S.screener = { presets: [], factors: [], preset: '', result: null, custom: null, loaded: false };

function loadScreener(force) {
  api('/api/screener/presets').then(function (r) {
    if (!r || !r.presets) return;
    S.screener.presets = r.presets;
    S.screener.factors = r.factors || [];
    S.screener.loaded = true;
    renderPresetGrid();
    updateSnapPill(r.snapshot);
    if (!S.screener.preset && r.presets.length) pickPreset(r.presets[0].key);
  });
}

function updateSnapPill(snap) {
  if (!snap || !$('sc-snap')) return;
  var age = '';
  if (snap.fetched_at) {
    var t = new Date(snap.fetched_at.replace(/-/g, '/'));
    var mins = Math.floor((Date.now() - t.getTime()) / 60000);
    age = mins <= 0 ? '刚刚' : (mins + ' 分钟前');
  }
  $('sc-snap').textContent = '全市场 ' + (snap.count || 0) + ' 只 · ' + age;
}

function renderPresetGrid() {
  var box = $('sc-presets');
  box.innerHTML = S.screener.presets.map(function (p) {
    return '<div class="presetcard' + (p.key === S.screener.preset ? ' on' : '') +
      '" data-preset="' + esc(p.key) + '"><b>' + esc(p.name) + '</b><span>' +
      esc((p.desc || '').slice(0, 46)) + '…</span></div>';
  }).join('');
  $$('#sc-presets .presetcard').forEach(function (el) {
    on(el, 'click', function () { pickPreset(el.getAttribute('data-preset')); });
  });
}

function pickPreset(key) {
  S.screener.preset = key;
  S.screener.custom = null;
  $$('#sc-presets .presetcard').forEach(function (el) {
    el.classList.toggle('on', el.getAttribute('data-preset') === key);
  });
  var p = null;
  for (var i = 0; i < S.screener.presets.length; i++) {
    if (S.screener.presets[i].key === key) p = S.screener.presets[i];
  }
  if (!p) return;
  var rows = Object.keys(p.weights || {}).map(function (k) {
    var nm = (p.factor_names && p.factor_names[k]) || k;
    return '<span class="tag">' + esc(nm) + ' ×' + p.weights[k] + '</span>';
  }).join(' ');
  $('sc-preset-desc').innerHTML = '<b>' + esc(p.name) + '</b> —— ' + esc(p.desc) +
    '<div style="margin-top:6px">' + rows + '</div>';
  if ($('sc-weights').style.display !== 'none') renderCustomWeights();
}

function renderCustomWeights() {
  var base = S.screener.custom || {};
  if (!Object.keys(base).length) {
    var p = null;
    for (var i = 0; i < S.screener.presets.length; i++) {
      if (S.screener.presets[i].key === S.screener.preset) p = S.screener.presets[i];
    }
    base = (p && p.weights) || {};
  }
  var html = '<div class="panel" style="margin-top:10px"><div class="panel-h"><h3>自定义因子权重</h3>' +
    '<span class="hint">填 0 = 不用这个因子。方向已经标好了，不用自己反着填。</span>' +
    '<button class="btn tiny" id="sc-wreset">恢复模板默认</button></div><div class="weightgrid">' +
    S.screener.factors.map(function (f) {
      var v = base[f.key] !== undefined ? base[f.key] : 0;
      return '<div class="wi"><span title="' + esc(f.name) + '">' + esc(f.name) +
        ' <em style="color:#6b7684;font-style:normal">' + esc(f.direction) + '</em></span>' +
        '<input class="input" type="number" step="0.1" min="0" data-fk="' + esc(f.key) +
        '" value="' + v + '"></div>';
    }).join('') + '</div></div>';
  $('sc-weights').innerHTML = html;
  on($('sc-wreset'), 'click', function () { S.screener.custom = null; renderCustomWeights(); toast('已恢复模板默认权重'); });
}

function readCustomWeights() {
  var out = {};
  $$('#sc-weights [data-fk]').forEach(function (el) {
    var v = parseFloat(el.value);
    if (!isNaN(v) && v > 0) out[el.getAttribute('data-fk')] = v;
  });
  return out;
}

function screenerFilters() {
  var boards = [];
  $$('.sc-board').forEach(function (c) { if (c.checked) boards.push(c.value); });
  var exind = $('sc-exind').value.split(/[,，\s]+/).map(function (s) { return s.trim(); })
    .filter(function (s) { return s; });
  return {
    boards: boards,
    exclude_st: $('sc-st').checked,
    exclude_limit_up: $('sc-limit').checked,
    exclude_limit_down: $('sc-limit').checked,
    exclude_loss: $('sc-loss').checked,
    industries_exclude: exind,
    min_amount: (parseFloat($('sc-amount').value) || 0) * 10000,
    min_mktcap: (parseFloat($('sc-mktcap').value) || 0) * 100000000,
    max_mktcap: (parseFloat($('sc-mktcapmax').value) || 0) * 100000000,
    min_listed_days: parseInt($('sc-listed').value, 10) || 0,
    min_price: parseFloat($('sc-price').value) || 0
  };
}

function runScreener() {
  var boards = [];
  $$('.sc-board').forEach(function (c) { if (c.checked) boards.push(c.value); });
  if (!boards.length) { toast('至少要勾一个板块', 'warn'); return; }
  var body = {
    preset: S.screener.preset,
    top_n: parseInt($('sc-topn').value, 10) || 20,
    with_tech: $('sc-tech').checked,
    neutralize_industry: $('sc-neutral').checked,
    with_fundamental: $('sc-fnd') ? $('sc-fnd').checked : true,
    filters: screenerFilters()
  };
  if (S.screener.custom) {
    var w = readCustomWeights();
    if (!Object.keys(w).length) { toast('自定义权重全是 0，至少给一个因子填正数', 'warn'); return; }
    body.weights = w;
  }
  var btn = $('sc-run');
  btn.disabled = true;
  var slow = ($('sc-fnd') && $('sc-fnd').checked) ? '，估值增强会多花 10~20 秒' : '';
  btn.textContent = '选股中…（全市场扫描约 5-10 秒' + slow + '）';
  $('sc-status').innerHTML = '<span class="spin"></span>正在扫描全市场…';
  api('/api/screener/run', { method: 'POST', body: body }).then(function (r) {
    btn.disabled = false;
    btn.textContent = '开始选股';
    if (!r.ok) {
      $('sc-status').textContent = '';
      toast(r.message || '选股失败', 'err', 9000);
      if (r.filter_stats) renderFunnel(r);
      return;
    }
    S.screener.result = r;
    $('sc-status').innerHTML = '<span style="color:#7ee787">完成，用时 ' + r.elapsed + ' 秒</span>';
    renderScreenerResult(r);
  });
}

function renderFunnel(r) {
  var fs = r.filter_stats || {};
  var cuts = [];
  for (var k in fs) {
    if (k !== '总数' && k !== '通过' && fs[k] > 0) cuts.push(k + ' ' + fs[k]);
  }
  cuts.sort(function (a, b) { return parseInt(b.split(' ').pop(), 10) - parseInt(a.split(' ').pop(), 10); });
  $('sc-funnel').innerHTML =
    '<div class="step">全市场 <b>' + (fs['总数'] || 0) + '</b> 只</div><span class="arrow">→</span>' +
    '<div class="step">粗筛后 <b>' + (fs['通过'] || 0) + '</b> 只</div><span class="arrow">→</span>' +
    '<div class="step">选出 <b>' + ((r.picked || []).length) + '</b> 只</div>' +
    (cuts.length ? '<span class="hint" style="margin-left:10px">剔除明细：' + esc(cuts.join(' ｜ ')) + '</span>' : '');
}

function renderScreenerResult(r) {
  $('sc-result').style.display = '';
  renderFunnel(r);
  var pct = function (v) { return v === null || v === undefined ? '-' : (v > 0 ? '+' : '') + fmtNum(v, 2) + '%'; };
  var tb = $('sc-table').querySelector('tbody');
  tb.innerHTML = (r.picked || []).map(function (p) {
    var why = (p.why || []).map(function (w) {
      return esc(w.factor) + '<i> ' + fmtNum(w.pct, 0) + '</i>';
    }).join(' ｜ ');
    var tech = p.tech && p.tech.vol_ann ? ' <span class="hint">波动' + fmtNum(p.tech.vol_ann, 0) + '%</span>' : '';
    // 估值分位：越低越便宜。没抓到时显示 "-"
    var f = p.fnd || {};
    var pePct = (f.pe_pct_5y === null || f.pe_pct_5y === undefined)
      ? '<span class="muted">-</span>'
      : '<span class="' + (f.pe_pct_5y <= 30 ? 'up' : (f.pe_pct_5y >= 70 ? 'down' : '')) + '">'
        + fmtNum(f.pe_pct_5y, 0) + '</span>';
    var dy = (f.dividend_yield === null || f.dividend_yield === undefined)
      ? '<span class="muted">-</span>' : fmtNum(f.dividend_yield, 2);
    return '<tr>' +
      '<td>' + p.rank + '</td>' +
      '<td style="font-family:Consolas,monospace">' + esc(p.code) + '</td>' +
      '<td><b>' + esc(p.name) + '</b></td>' +
      '<td class="muted">' + esc(p.industry) + '</td>' +
      '<td>' + fmtNum(p.price, 2) + '</td>' +
      '<td class="' + cls(p.chg_pct) + '">' + pct(p.chg_pct) + '</td>' +
      '<td>' + (p.pe ? fmtNum(p.pe, 1) : '<span class="muted">亏损</span>') + '</td>' +
      '<td>' + pePct + '</td>' +
      '<td>' + dy + '</td>' +
      '<td>' + (p.roe === null || p.roe === undefined ? '-' : fmtNum(p.roe, 1)) + '</td>' +
      '<td>' + fmtNum((p.mktcap || 0) / 1e8, 0) + '</td>' +
      '<td>' + (p.turnover === null || p.turnover === undefined ? '-' : fmtNum(p.turnover, 1)) + '</td>' +
      '<td class="' + cls(p.main_pct) + '">' + (p.main_pct === null || p.main_pct === undefined ? '-' : fmtNum(p.main_pct, 2)) + '</td>' +
      '<td class="' + cls(p.chg_60d) + '">' + pct(p.chg_60d) + '</td>' +
      '<td><b>' + fmtNum(p.final, 1) + '</b>' + tech + '</td>' +
      '<td class="why">' + why + '</td>' +
      '</tr>';
  }).join('') || '<tr><td colspan="16" class="empty">没有选出任何股票</td></tr>';

  var meta = (r.preset_name || '') + ' · 候选 ' + r.after_filter + ' 只 · ' +
    (r.neutralize_industry ? '已行业中性化' : '未中性化') + ' · ' + r.elapsed + 's';
  if (r.fundamental_used > 0) meta += ' · 估值增强 ' + r.fundamental_used + ' 只';
  $('sc-meta').textContent = meta;

  var warns = (r.warnings || []).slice();
  if (r.tech_note) warns.push(r.tech_note);
  if (r.fundamental_note) warns.push(r.fundamental_note);
  if (warns.length) {
    $('sc-warn').style.display = '';
    $('sc-warn').innerHTML = warns.map(function (w) { return '⚠️ ' + esc(w); }).join('<br>');
  } else {
    $('sc-warn').style.display = 'none';
  }
}

function screenerToStrategy() {
  var r = S.screener.result;
  if (!r || !(r.picked || []).length) { toast('先选股', 'warn'); return; }
  api('/api/screener/to-strategy', {
    method: 'POST',
    body: { picks: r.picked, preset_name: r.preset_name }
  }).then(function (x) {
    if (!x.ok) { toast(x.message || '生成失败', 'err', 8000); return; }
    toast(x.message, 'ok', 9000);
    loadStrategies();
    switchTab('strategy');
  });
}

function screenerToWatch() {
  var r = S.screener.result;
  if (!r || !(r.picked || []).length) { toast('先选股', 'warn'); return; }
  api('/api/screener/to-watch', { method: 'POST', body: { picks: r.picked } }).then(function (x) {
    toast(x.message, x.ok ? 'ok' : 'err');
    if (x.ok) loadWatchlist();
  });
}

function screenerExport() {
  var r = S.screener.result;
  if (!r || !(r.picked || []).length) { toast('先选股', 'warn'); return; }
  var head = ['排名','代码','名称','行业','现价','今日涨跌%','市盈率','PE近5年分位%','股息率%',
              '市净率','ROE%','毛利率%',
              '营收增速%','净利润增速%','总市值','换手率%','主力净占比%','60日涨跌%','得分','为什么选它'];
  var lines = [head.join(',')];
  r.picked.forEach(function (p) {
    var why = (p.why || []).map(function (w) { return w.factor + w.pct; }).join(' / ');
    var f = p.fnd || {};
    var row = [p.rank, p.code, p.name, p.industry, p.price, p.chg_pct, p.pe,
               f.pe_pct_5y, f.dividend_yield, p.pb, p.roe,
               p.gross_margin, p.rev_yoy, p.profit_yoy, (p.mktcap || 0), p.turnover,
               p.main_pct, p.chg_60d, p.final, why];
    lines.push(row.map(function (v) {
      var s = (v === null || v === undefined) ? '' : String(v);
      return '"' + s.replace(/"/g, '""') + '"';
    }).join(','));
  });
  var blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '选股结果_' + (r.preset_name || '') + '_' + new Date().toISOString().slice(0, 10) + '.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  toast('已导出 CSV（可以用 Excel 打开）', 'ok');
}

/* =========================================================================
   个股基本面速查（可选增强，不参与回测）
   ========================================================================= */
function runFundamentalQuery() {
  var sym = ($('fn-sym').value || '').trim().toUpperCase();
  if (!sym) { toast('先填一个股票代码，比如 600519.SH', 'warn'); return; }
  var btn = $('fn-run');
  btn.disabled = true;
  $('fn-status').innerHTML = '<span class="spin"></span>查询中…（首次抓取约 2~6 秒）';
  api('/api/fundamental/fetch', { method: 'POST', body: { symbols: sym } }).then(function (r) {
    btn.disabled = false;
    if (!r.ok) {
      $('fn-status').textContent = '';
      toast(r.message || '查询失败', 'err', 9000);
      return;
    }
    var it = (r.items || [])[0] || {};
    if (!it.symbol) {
      $('fn-status').textContent = '';
      toast('股票代码格式不对，要像 600519.SH 这样（6位数字 + .SH/.SZ/.BJ）', 'err', 9000);
      return;
    }
    $('fn-status').textContent = '';
    renderFundamental(it, r.note);
  });
}

function renderFundamental(it, note) {
  var v = it.valuation || {};
  var ind = it.industry || {};
  var divs = it.dividend || [];
  var isNum = function (x) { return typeof x === 'number' && isFinite(x); };
  var kv = function (label, val, good) {
    var cls2 = good === true ? 'up' : (good === false ? 'down' : '');
    var s = (val === null || val === undefined || val === '')
      ? '<span class="muted">—</span>'
      : '<b class="' + cls2 + '">' + esc(String(val)) + '</b>';
    return '<div style="min-width:110px"><div class="hint">' + esc(label) + '</div><div>' + s + '</div></div>';
  };
  var html = '';
  if (isNum(v.pe_ttm) || isNum(v.pb)) {
    html += '<div style="display:flex;flex-wrap:wrap;gap:18px">' +
      kv('PE(TTM)', isNum(v.pe_ttm) ? fmtNum(v.pe_ttm, 2) : null) +
      kv('PE近5年分位', isNum(v.pe_pct_5y) ? fmtNum(v.pe_pct_5y, 1) + '%' : null,
         isNum(v.pe_pct_5y) ? v.pe_pct_5y <= 30 : null) +
      kv('PB', isNum(v.pb) ? fmtNum(v.pb, 2) : null) +
      kv('PB近5年分位', isNum(v.pb_pct_5y) ? fmtNum(v.pb_pct_5y, 1) + '%' : null,
         isNum(v.pb_pct_5y) ? v.pb_pct_5y <= 30 : null) +
      kv('股息率', isNum(v.dividend_yield) ? fmtNum(v.dividend_yield, 2) + '%' : null) +
      kv('总市值', isNum(v.mktcap) ? fmtNum(v.mktcap / 1e8, 0) + ' 亿' : null) +
      '</div>';
  } else {
    html += '<div class="hint">估值没取到（可能没配数据令牌、网络不通，或这只票没有估值数据）。</div>';
  }
  if (ind.sw_l1) {
    var chain = [ind.sw_l1, ind.sw_l2, ind.sw_l3].filter(Boolean).join(' › ');
    html += '<div style="margin-top:10px"><span class="hint">申万行业：</span><b>' + esc(chain) + '</b></div>';
    if (ind.concepts && ind.concepts.length) {
      html += '<div style="margin-top:4px"><span class="hint">概念：</span>' +
        ind.concepts.slice(0, 12).map(function (c) {
          return '<span class="tag">' + esc(c) + '</span>';
        }).join(' ') + '</div>';
    }
  }
  if (divs.length) {
    html += '<div style="margin-top:10px"><div class="hint">最近分红（除权除息日 · 每10股派息）：</div>' +
      '<div class="tablewrap"><table><thead><tr><th>除权除息日</th><th>每10股派息</th><th>方案</th></tr></thead><tbody>' +
      divs.slice(0, 6).map(function (d) {
        return '<tr><td>' + esc(d.ex_date || '-') + '</td><td>' +
          (isNum(d.cash_per10) ? fmtNum(d.cash_per10, 2) : '-') +
          '</td><td class="muted">' + esc(d.plan || '-') + '</td></tr>';
      }).join('') + '</tbody></table></div></div>';
  }
  if (note) html += '<div class="hint" style="margin-top:6px">' + esc(note) + '</div>';
  $('fn-result').innerHTML = html;
}

function clearFundamentalCache() {
  if (!confirm('清空本机基本面缓存？下次查询会重新联网抓取。')) return;
  api('/api/fundamental/clear', { method: 'POST', body: {} }).then(function (r) {
    toast(r.ok ? ('已清空 ' + (r.removed || 0) + ' 个缓存文件') : '清空失败',
          r.ok ? 'ok' : 'err');
  });
}

/* =========================================================================
   总览
   ========================================================================= */
function loadDash() {
  api('/api/account').then(function (r) {
    var b = r.balance || {};
    $('c-total').textContent = fmtMoney(b.total_asset);
    $('c-cash').textContent = fmtMoney(b.available_cash !== undefined ? b.available_cash : b.cash);
    $('c-mv').textContent = fmtMoney(b.market_value);
    $('c-day').textContent = sign(b.day_pnl, 2);
    $('c-day').className = 'v ' + cls(b.day_pnl);
    $('c-pnl').textContent = sign(b.total_pnl, 2);
    $('c-pnl').className = 'v ' + cls(b.total_pnl);
    $('c-total-s').textContent = '初始资金 ' + fmtMoney(b.initial_capital) + '（' + sign(b.total_pnl_pct, 2) + '%）';
    $('c-cash-s').textContent = b.frozen ? ('挂单冻结 ' + fmtMoney(b.frozen)) : '全部可用';
    $('c-mv-s').textContent = '浮动盈亏 ' + sign(b.float_pnl, 2);
    $('c-day-s').textContent = '今日已实现 ' + sign(b.realized_pnl_today, 2);
    // 累计盈亏下面那行：把"手续费到底扣没扣"直接摆出来。
    // 光看总资产是查不出来有没有扣费的——买入费摊在持仓成本里、
    // 卖出费混在已实现盈亏里，两个数都不带"费"字。
    var feeLine = '';
    var feeTitle = '';
    if (b.fee_total !== undefined && b.fee_total !== null) {
      feeLine = '已扣手续费 ' + fmtMoney(b.fee_total);
      if (b.fee_delta !== null && b.fee_delta !== undefined && Math.abs(b.fee_delta) >= 0.01) {
        feeLine += ' · 按当前费率 ' + fmtMoney(b.fee_recalc);
      }
      feeTitle = '买入 ' + fmtMoney(b.fee_buy) + '（' + (b.fee_buy_count || 0) + ' 笔）'
        + ' / 卖出 ' + fmtMoney(b.fee_sell) + '（' + (b.fee_sell_count || 0) + ' 笔）'
        + '\n累计换手 ' + fmtMoney(b.fee_turnover) + ' 元'
        + '\n\n累计盈亏已含全部手续费：买入费摊进持仓成本，卖出费直接从现金扣。';
      if (b.fee_delta !== null && b.fee_delta !== undefined && Math.abs(b.fee_delta) >= 0.01) {
        feeTitle += '\n\n按「当前设置」的费率重算，同样这些成交应收 '
          + fmtMoney(b.fee_recalc) + ' 元，差 ' + sign(b.fee_delta, 2) + ' 元。'
          + '\n历史成交按当时费率入账，不会追溯改账；新费率只影响之后的成交。';
      }
    } else if (r.status && r.status.name && r.status.name !== 'paper') {
      // 实盘通道只回报余额，不给逐笔费用明细，这里如实说明而不是留空
      feeLine = '手续费由券商结算';
      feeTitle = '实盘通道不返回逐笔费用明细，以券商对账单为准。';
    }
    $('c-pnl-s').textContent = feeLine;
    $('c-pnl-s').title = feeTitle;
    $('c-orders').textContent = (b.pending_orders || 0) + ' 笔挂单';
    // 通道必须写清楚：模拟盘的钱是虚拟的，实盘是真钱。
    // 只写"通道已连接"等于没说，容易把模拟盘浮盈当成真收益。
    var chan = r.status ? (r.status.is_live ? '实盘' : '模拟盘') : '';
    $('c-orders-s').textContent = chan + (r.status && r.status.connected ? ' · 已连接' : ' · 未连接');
    if (b.error) toast('账户读取失败：' + b.error, 'err');
  });
  api('/api/positions/overview').then(function (r) {
    var pt = $('dash-positions-paper').querySelector('tbody');
    renderPositions(pt, r.paper || [], true);
    var lt = $('dash-positions-live').querySelector('tbody');
    if (!r.live_connected) {
      lt.innerHTML = '<tr><td colspan="10" class="empty">未连接实盘通道（到「实盘交易 → 交易通道」配置并连接）</td></tr>';
    } else {
      renderPositions(lt, r.live || [], true);
    }
    $('pos-paper-hint').textContent = r.paper_ok
      ? '数据来自本地模拟引擎（虚拟资金）'
      : '模拟盘读取失败：' + (r.paper_error || '');
    $('pos-live-hint').textContent = r.live_connected
      ? '数据来自 ' + (r.live_display || r.live_broker || '') + '（真钱）'
      : '未连接实盘通道';
  });
  loadEquity();
  loadEngineActions();
}

function loadEquity() {
  api('/api/equity', { params: { limit: 500 } }).then(function (r) {
    var c = r.curve || [];
    if (!c.length) { $('eq-hint').textContent = '还没有记录（引擎运行后每分钟记一次）'; return; }
    drawLines($('dash-equity'), [{ data: c.map(function (x) { return x.total; }), color: '#3b82f6', name: '总资产', width: 1.8 }], { legend: true });
    var first = c[0].total, last = c[c.length - 1].total;
    $('eq-hint').textContent = c.length + ' 个点 · 区间 ' + sign((last / first - 1) * 100, 2) + '%';
  });
}

function loadEngineActions() {
  api('/api/engine/status').then(function (r) {
    var box = $('dash-actions');
    var a = r.last_actions || [];
    if (!a.length) { box.textContent = r.running ? '引擎运行中，暂无动作' : '引擎未启动'; return; }
    box.innerHTML = a.slice().reverse().map(function (x) {
      var t = x.action === 'buy' ? '买入' : x.action === 'sell' ? '卖出' : x.action === 'blocked' ? '被风控拦下' : '跳过';
      var c = x.action === 'buy' ? 'up' : x.action === 'sell' ? 'down' : 'muted';
      return '<div><span class="a">' + esc(x.symbol) + '</span> <span class="' + c + '">' + t + '</span> ' +
        (x.qty ? esc(x.qty) + '股 ' : '') + '<span class="muted">' + esc(x.reason || x.message || '') + '</span></div>';
    }).join('');
  });
}

/* =========================================================================
   持仓表
   ========================================================================= */
function renderPositions(tbody, list, compact) {
  if (!list.length) { tbody.innerHTML = '<tr><td colspan="10" class="empty">空仓（没有持仓）</td></tr>'; return; }
  tbody.innerHTML = list.map(function (p) {
    var sym = p.symbol || p.code;
    return '<tr>' +
      '<td><a href="javascript:void(0)" onclick="pickSymbol(\'' + esc(sym) + '\')">' + esc(p.code || sym) + '</a></td>' +
      '<td>' + esc(p.name || '') + '</td>' +
      '<td>' + esc(p.qty) + '</td>' +
      '<td>' + esc(p.available !== undefined ? p.available : p.qty) + '</td>' +
      '<td>' + fmtNum(p.avg_cost, 3) + '</td>' +
      '<td>' + fmtNum(p.last_price, 3) + '</td>' +
      '<td>' + fmtMoney(p.market_value) + '</td>' +
      '<td class="' + cls(p.pnl) + '">' + sign(p.pnl, 2) + '</td>' +
      '<td class="' + cls(p.pnl_pct) + '">' + sign(p.pnl_pct, 2) + '%</td>' +
      '<td>' + (compact ? '' : '<button class="btn tiny" onclick="quickSell(\'' + esc(sym) + '\',' + (p.available || 0) + ')">卖出</button>') + '</td>' +
      '</tr>';
  }).join('');
}

function quickSell(sym, qty) {
  if (!qty || qty <= 0) { toast('该股当前可卖数量为 0（T+1 或已挂单占用）', 'warn'); return; }
  openOrderModal(sym, 'sell', qty);
}
function pickSymbol(sym) {
  switchTab('market');
  S.symbol = sym;
  loadChart(); loadQuote();
}

/* =========================================================================
   行情
   ========================================================================= */
function loadIndices() {
  api('/api/market/indices').then(function (r) {
    var box = $('dash-indices');
    var list = r.indices || [];
    box.innerHTML = (list.map(function (i) {
      return '<div class="idx"><div class="n">' + esc(i.name) + '</div>' +
        '<div class="p ' + cls(i.change_pct) + '">' + fmtNum(i.price, 2) + '</div>' +
        '<div class="c ' + cls(i.change_pct) + '">' + sign(i.change_pct, 2) + '%</div></div>';
    }).join('')) + (list.length
      ? '<div class="idx" style="grid-column:1/-1;background:transparent"><div class="n">实时刷新于 ' +
        new Date().toLocaleTimeString('zh-CN') + '（每几秒自动更新）</div></div>'
      : '<div class="empty">指数获取失败</div>');
  });
}

function doSearch() {
  var kw = $('mkt-search').value.trim();
  if (!kw) return;
  $('mkt-search-hint').innerHTML = '<span class="spin"></span>搜索中…';
  api('/api/market/search', { params: { q: kw, limit: 12 } }).then(function (r) {
    var list = r.results || [];
    $('mkt-search-hint').textContent = list.length ? ('找到 ' + list.length + ' 个结果，点一下查看') : '没找到，换个关键词试试（也可以直接输入6位代码）';
    var box = $('mkt-results');
    box.innerHTML = list.map(function (x) {
      return '<span class="chip" onclick="pickSymbol(\'' + esc(x.symbol) + '\')">' + esc(x.code) + ' ' + esc(x.name) + '</span>';
    }).join('');
    if (list.length === 1) pickSymbol(list[0].symbol);
  });
}

function loadQuote() {
  if (!S.symbol) return;
  api('/api/market/quote', { params: { symbols: S.symbol } }).then(function (r) {
    var q = (r.quotes || {})[S.symbol];
    if (!q) { $('mkt-quote').innerHTML = '<div class="empty">行情获取失败</div>'; return; }
    $('mkt-title').textContent = q.code + ' ' + q.name;
    $('mkt-sub').textContent = '数据源 ' + (q.source || '-') + ' · 昨收 ' + fmtNum(q.prev_close, 2) +
      ' · 涨停 ' + fmtNum(q.limit_up, 2) + ' / 跌停 ' + fmtNum(q.limit_down, 2);
    var items = [
      ['最新价', fmtNum(q.price, 2), q.change_pct],
      ['涨跌幅', sign(q.change_pct, 2) + '%', q.change_pct],
      ['涨跌额', sign(q.change, 2), q.change],
      ['今开', fmtNum(q.open, 2), q.open - q.prev_close],
      ['最高', fmtNum(q.high, 2), q.high - q.prev_close],
      ['最低', fmtNum(q.low, 2), q.low - q.prev_close],
      ['成交量', fmtBig(q.volume) + '手', null],
      ['成交额', fmtBig(q.amount), null],
      ['换手率', q.turnover != null ? q.turnover + '%' : '—', null],
      ['振幅', q.amplitude != null ? q.amplitude + '%' : '—', null],
      ['市盈率', fmtNum(q.pe, 2), null],
      ['市净率', fmtNum(q.pb, 2), null],
      ['总市值', fmtBig(q.market_cap), null],
      ['流通市值', fmtBig(q.float_cap), null]
    ];
    $('mkt-quote').innerHTML = items.map(function (it) {
      return '<div class="qq"><div class="k">' + it[0] + '</div><div class="v ' + cls(it[2]) + '">' + it[1] + '</div></div>';
    }).join('');
  });
}

function loadChart() {
  if (!S.symbol) return;
  $('mkt-title').textContent = S.symbol + ' 加载中…';
  api('/api/market/kline', { params: { symbol: S.symbol, period: S.period, adjust: 1, limit: 260, indicators: 1 } }).then(function (r) {
    if (!r.klines || !r.klines.length) { toast('K线获取失败', 'err'); return; }
    S.chartData = r;
    drawKline($('mkt-chart'), r, null);
    loadQuote();
  });
}

function loadWatchlist() {
  api('/api/watchlist').then(function (r) {
    var rows = r.watchlist || [];
    var tb = $('mkt-watch').querySelector('tbody');
    if (!rows.length) { tb.innerHTML = '<tr><td colspan="5" class="empty">还没有自选股，搜一只然后点【加自选】</td></tr>'; return; }
    tb.innerHTML = rows.map(function (x) {
      return '<tr>' +
        '<td><a href="javascript:void(0)" onclick="pickSymbol(\'' + esc(x.symbol) + '\')">' + esc(x.code || x.symbol) + '</a></td>' +
        '<td>' + esc(x.name || '') + '</td>' +
        '<td class="' + cls(x.change_pct) + '">' + fmtNum(x.price, 2) + '</td>' +
        '<td class="' + cls(x.change_pct) + '">' + sign(x.change_pct, 2) + '%</td>' +
        '<td><span class="x" onclick="delWatch(\'' + esc(x.symbol) + '\')">删除</span></td>' +
        '</tr>';
    }).join('');
  });
}

function addWatch() {
  if (!S.symbol) { toast('先搜索选一只股票', 'warn'); return; }
  api('/api/watchlist', { method: 'POST', body: { action: 'add', symbol: S.symbol } }).then(function (r) {
    toast(r.message || '已添加', 'ok'); loadWatchlist();
  });
}
function delWatch(sym) {
  api('/api/watchlist', { method: 'POST', body: { action: 'remove', symbol: sym } }).then(function () { loadWatchlist(); });
}

function loadRank() {
  api('/api/market/rank', { params: { type: S.rankType, limit: 15 } }).then(function (r) {
    var tb = $('mkt-rank').querySelector('tbody');
    var list = r.list || [];
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="4" class="empty">' + esc(r.message || '数据获取失败') + '</td></tr>';
      return;
    }
    var html = '';
    // 数据源降级（行情源不稳，用的是缓存/本地快照）必须让用户看见，不能装作正常
    if (r.stale) {
      html += '<tr><td colspan="4" style="color:var(--warn);font-size:12px">⚠ ' +
        esc(r.message || '行情源不稳定，显示的是旧数据') + '</td></tr>';
    }
    html += list.map(function (x) {
      return '<tr><td><a href="javascript:void(0)" onclick="pickSymbol(\'' + esc(x.symbol) + '\')">' + esc(x.code) + '</a></td>' +
        '<td>' + esc(x.name) + '</td>' +
        '<td class="' + cls(x.change_pct) + '">' + fmtNum(x.price, 2) + '</td>' +
        '<td class="' + cls(x.change_pct) + '">' + sign(x.change_pct, 2) + '%</td></tr>';
    }).join('');
    tb.innerHTML = html;
  });
}

/* =========================================================================
   策略
   ========================================================================= */
S.strategyWeights = {};

function presetOptions(sel) {
  var opts = [
    ['preset_momentum', '中期动量组合'],
    ['preset_reversal', '短期反转组合'],
    ['preset_lowvol', '低波动稳健组合'],
    ['preset_pullback', '回调买入法组合'],
    ['preset_alpha101', 'Alpha101 精选组合'],
    ['preset_volume_price', '量价背离组合']
  ];
  if (!sel) return opts;
  sel.innerHTML = opts.map(function (o) {
    return '<option value="' + o[0] + '">' + o[1] + '</option>';
  }).join('');
  return opts;
}

function currentStrategySymbols() {
  return parseSymbols($('st-symbols').value);
}

function renderWeights() {
  var syms = currentStrategySymbols();
  var box = $('st-weights');
  if (!syms.length) {
    box.innerHTML = '<span class="hint">填了股票池后，这里会出现每只股票的权重输入框。</span>';
    return;
  }
  box.innerHTML = syms.map(function (sym) {
    var v = S.strategyWeights[sym] !== undefined ? S.strategyWeights[sym] : '';
    return '<div class="wit"><span style="color:#58a6ff;font-family:Consolas,monospace">' +
      esc(sym.split('.')[0]) + '</span><input class="input" type="number" step="0.5" min="0" max="100" ' +
      'data-ws="' + esc(sym) + '" value="' + esc(v) + '" placeholder="默认">%</div>';
  }).join('') +
  '<div class="wit" style="grid-column:1/-1;color:var(--fg2)">' +
  '合计 <b id="st-wsum" style="color:#e6edf3">—</b>%（不要求等于 100，引擎会按比例自动调整）</div>';
}

function readWeights() {
  var out = {};
  $$('#st-weights [data-ws]').forEach(function (el) {
    var v = parseFloat(el.value);
    if (!isNaN(v) && v > 0) out[el.getAttribute('data-ws')] = v;
  });
  return out;
}

function updateWeightSum() {
  var w = readWeights();
  var s = 0;
  for (var k in w) s += w[k];
  if ($('st-wsum')) $('st-wsum').textContent = Object.keys(w).length ? s.toFixed(1) : '—';
}

function applyWeights(wmap) {
  S.strategyWeights = wmap || {};
  renderWeights();
  var s = 0;
  for (var k in S.strategyWeights) s += S.strategyWeights[k];
  toast('已填入权重（合计 ' + s.toFixed(1) + '%）。点【保存策略】才会生效', 'ok', 6000);
}

function calcOptimalWeights() {
  var syms = currentStrategySymbols();
  if (syms.length < 2) { toast('至少要有 2 只股票才能优化权重（1 只的话权重就是 100%）', 'warn'); return; }
  var method = $('st-wmethod').value;
  var preset = $('st-wpreset') ? $('st-wpreset').value : 'preset_momentum';
  var btn = $('st-wcalc');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>计算中…';
  api('/api/optimizer/run', {
    method: 'POST',
    body: { symbols: syms, method: method, preset: preset, total_pct: 90, max_single_pct: 30 }
  }).then(function (r) {
    btn.disabled = false;
    btn.textContent = '一键计算最优权重';
    if (!r.ok) { toast('计算失败：' + (r.message || ''), 'err', 7000); return; }
    if (r.note) toast(r.note, 'warn', 9000);
    applyWeights(r.weights);
  });
}

function equalWeights() {
  var syms = currentStrategySymbols();
  if (!syms.length) { toast('先填股票池', 'warn'); return; }
  var w = 100 / syms.length;
  var map = {};
  syms.forEach(function (s) { map[s] = Math.round(w * 10) / 10; });
  applyWeights(map);
}

function clearWeights() {
  S.strategyWeights = {};
  renderWeights();
  toast('已清空，将使用策略默认分配');
}
function fillTemplateSelect(sel) {
  if (!sel) return;
  sel.innerHTML = S.templates.map(function (t) {
    return '<option value="' + esc(t.key) + '">' + esc(t.name) + '</option>';
  }).join('');
}

function getTemplate(key) {
  for (var i = 0; i < S.templates.length; i++) if (S.templates[i].key === key) return S.templates[i];
  return null;
}

function renderStrategyParams(sel, box, values) {
  if (!sel || !box) return;
  var t = getTemplate(sel.value);
  var desc = box.previousElementSibling;
  if (sel.id === 'st-template' && $('st-desc')) $('st-desc').textContent = t ? t.desc : '';
  if (!t) { box.innerHTML = ''; return; }
  box.innerHTML = t.schema.map(function (f) {
    var v = (values && values[f.name] !== undefined) ? values[f.name] : f.default;
    var input = '';
    if (f.type === 'select') {
      input = '<select class="input" data-p="' + esc(f.name) + '">' + (f.options || []).map(function (o) {
        return '<option value="' + esc(o.value) + '"' + (String(o.value) === String(v) ? ' selected' : '') + '>' + esc(o.label) + '</option>';
      }).join('') + '</select>';
    } else if (f.type === 'bool') {
      input = '<label class="chk"><input type="checkbox" data-p="' + esc(f.name) + '"' + (v ? ' checked' : '') + '> 开启</label>';
    } else if (f.type === 'str' || f.type === 'text') {
      input = '<input class="input" type="text" data-p="' + esc(f.name) + '" value="' + esc(v) + '" placeholder="' + esc(f.placeholder || '') + '">';
    } else {
      input = '<input class="input" type="number" step="' + (f.step || (f.type === 'int' ? 1 : 0.1)) +
        '" data-p="' + esc(f.name) + '" value="' + esc(v) + '"' +
        (f.min !== undefined ? ' min="' + f.min + '"' : '') + (f.max !== undefined ? ' max="' + f.max + '"' : '') + '>';
    }
    return '<div class="param"><label>' + esc(f.label) + '</label>' + input +
      (f.help ? '<div class="help">' + esc(f.help) + '</div>' : '') + '</div>';
  }).join('');
}

function collectParams(box, sel) {
  var t = getTemplate(sel.value);
  var out = {};
  if (!t) return out;
  t.schema.forEach(function (f) {
    var el = box.querySelector('[data-p="' + f.name + '"]');
    if (!el) { out[f.name] = f.default; return; }
    if (f.type === 'bool') out[f.name] = el.checked;
    else if (f.type === 'int') out[f.name] = parseInt(el.value, 10) || f.default;
    else if (f.type === 'float') out[f.name] = parseFloat(el.value);
    else out[f.name] = el.value;
  });
  return out;
}

function parseSymbols(txt) {
  return String(txt || '').split(/[,，\s]+/).map(function (s) { return s.trim(); }).filter(Boolean);
}

function loadStrategies() {
  api('/api/strategies/runtime').then(function (r) {
    S.strategies = r.strategies || [];
    S.engineRunning = r.engine_running;
    fillBacktestStrategySelect();
    var box = $('st-list');
    if (!S.strategies.length) {
      box.innerHTML = '<div class="empty">还没有策略。左边填一个，点【保存策略】。</div>';
      return;
    }
    box.innerHTML = S.strategies.map(function (s) {
      var t = getTemplate(s.stype);
      var badge, state;
      if (s.deployed && s.expired) {
        badge = 'badge signal'; state = '授权已过期';
      } else if (s.deployed && s.direct_send) {
        badge = 'badge auto'; state = '已部署 · 全自动';
      } else if (s.deployed) {
        badge = 'badge confirm'; state = '已部署 · 待确认';
      } else if (s.mode === 'auto') {
        badge = 'badge signal'; state = '未部署';
      } else {
        badge = 'badge signal'; state = '只看信号';
      }
      var acts = '';
      if (s.deployed) {
        acts = '<button class="btn tiny danger" onclick="undeployStrategy(' + s.id + ')">下线（停止自动交易）</button>' +
               '<button class="btn tiny" onclick="deployStrategy(' + s.id + ')">改授权参数</button>';
      } else {
        acts = '<button class="btn tiny primary" onclick="deployStrategy(' + s.id + ')">部署 · 开始自动交易</button>';
      }
      // 持仓明细：把每只票买了多少、成本、现价、盈亏直接摊开
      var holdHtml = '';
      if (s.deployed) {
        var hs = s.holdings || [];
        if (hs.length) {
          holdHtml = '<div class="holds">' + hs.map(function (h) {
            var locked = (h.available !== undefined && h.available < h.qty);
            return '<div class="hold"><b>' + esc(h.name) + '</b>' +
              ' <span class="muted">' + esc(h.symbol) + '</span>' +
              ' ｜ ' + h.qty + ' 股' +
              (locked ? '<span class="lock">（可用 ' + h.available + '，其余 T+1 锁定）</span>' : '') +
              ' ｜ 成本 ' + fmtNum(h.avg_cost, 3) + ' → 现价 ' + fmtNum(h.last_price, 3) +
              ' ｜ <span class="' + cls(h.pnl) + '">' + sign(h.pnl_pct, 2) + '% / ' +
              sign(h.pnl, 2) + ' 元</span></div>';
          }).join('') + '</div>';
        } else {
          holdHtml = '<div class="holds"><div class="hold muted">当前没有持仓</div></div>';
        }
        // 额度使用情况：买满额度就加不了仓，这是「只买了 2 只」最常见的原因
        var left = (s.capital_left === undefined || s.capital_left === null) ? null : s.capital_left;
        holdHtml += '<div class="hint" style="margin-top:6px">额度 ' + fmtMoney(s.capital_value) +
          ' 元 ｜ 已用 ' + fmtMoney(s.capital_used) + ' 元 ｜ 剩余 ' + fmtMoney(left) + ' 元' +
          (left !== null && left <= 0 ? ' <b class="down">（额度已用满，买不了新的了）</b>' : '') +
          '</div>';
      }
      return '<div class="strat ' + (s.deployed ? 'deployed' : (s.enabled ? 'on' : '')) + '">' +
        '<div class="t"><b>' + esc(s.name) + '</b>' +
        '<span class="' + badge + '">' + esc(state) + '</span>' +
        (s.enabled ? '<span class="tag on">已启用</span>' : '<span class="tag off">未启用</span>') +
        '<span class="tag">' + esc(t ? t.name : s.stype) + '</span></div>' +
        '<div class="meta">股票池：' + esc((s.symbols || []).join(' ')) +
        (s.deployed ? '<br>资金额度 ' + fmtNum(s.capital_pct, 1) + '% ｜ 单只上限 ' + fmtNum(s.max_single_pct, 1) +
          '% ｜ 单笔上限 ' + fmtMoney(s.max_order_amount) + ' 元 ｜ 止损 -' + fmtNum(s.stop_loss_pct, 1) + '%' : '') +
        (s.deployed ? '<br>已用仓位 ' + fmtMoney(s.market_value) + ' 元 ｜ 持仓 ' + s.holding_count + ' 只 ｜ 浮动 ' +
          sign(s.pnl, 2) + ' 元' : '') +
        '<br>上次运行：' + esc(s.last_run || '从未') +
        (s.last_action_at ? ' ｜ 最近下单：' + esc(s.last_action_at) : '') +
        (s.last_signal ? '<br>最近动作：' + esc(s.last_signal) : '') +
        (s.expires_at ? '<br>授权到期：' + esc(s.expires_at) : '') + '</div>' +
        holdHtml +
        '<div class="acts">' + acts +
        '<button class="btn tiny ' + (s.enabled ? '' : 'primary') + '" onclick="toggleStrategy(' + s.id + ',' + (s.enabled ? 'false' : 'true') + ')">' + (s.enabled ? '停用' : '启用') + '</button>' +
        '<button class="btn tiny" onclick="editStrategy(' + s.id + ')">编辑</button>' +
        '<button class="btn tiny" onclick="previewStrategy(' + s.id + ')">预览信号</button>' +
        '<button class="btn tiny primary" onclick="backtestStrategy(' + s.id + ')">回测</button>' +
        '<button class="btn tiny danger" onclick="delStrategy(' + s.id + ')">删除</button>' +
        '</div></div>';
    }).join('');
  });
}

/* ---------------- 策略部署 ---------------- */
function deployStrategy(id) {
  var s = null;
  for (var i = 0; i < S.strategies.length; i++) if (S.strategies[i].id === id) s = S.strategies[i];
  if (!s) { toast('找不到策略', 'err'); return; }
  if (!s.symbols || !s.symbols.length) { toast('这个策略没有股票池，先在【编辑】里填好', 'warn'); return; }
  var cap = s.capital_pct || 30, single = s.max_single_pct || 20, order = s.max_order_amount || 50000;
  var sl = s.stop_loss_pct || 8, tp = s.take_profit_pct || 0, direct = s.deployed ? s.direct_send : true;
  showModal('部署策略 · ' + s.name,
    '<div class="desc">部署 = <b>一次性授权</b>。确认之后，引擎会在交易时段自动替这个策略下单，' +
    '不需要你再一张一张点发送。<br>' +
    '想人工把关就把下面的「直接发送委托单」取消勾选 —— 那样策略只生成待确认单，你点发送才真发。</div>' +
    '<div class="deploybox">' +
    '<label class="chk" style="font-size:14px"><input type="checkbox" id="dp-direct"' + (direct ? ' checked' : '') + '> ' +
    '<b>直接发送委托单（全自动）</b></label>' +
    '<div class="hint" style="margin-left:22px">取消勾选 = 只生成待确认委托单，你点【发送】才发给券商</div>' +
    '<div class="formgrid" style="margin-top:12px">' +
    '<label class="fld"><span>资金额度（占总资产 %）</span><input class="input" id="dp-cap" type="number" step="5" value="' + cap + '"></label>' +
    '<label class="fld"><span>单只股票上限（%）</span><input class="input" id="dp-single" type="number" step="5" value="' + single + '"></label>' +
    '<label class="fld"><span>单笔委托上限（元）</span><input class="input" id="dp-order" type="number" step="1000" value="' + order + '"></label>' +
    '<label class="fld"><span>止损（%）必填</span><input class="input" id="dp-sl" type="number" step="1" value="' + sl + '"></label>' +
    '<label class="fld"><span>止盈（%）0=不用</span><input class="input" id="dp-tp" type="number" step="1" value="' + tp + '"></label>' +
    '<label class="fld"><span>授权有效期（天）0=长期</span><input class="input" id="dp-days" type="number" step="30" value="30"></label>' +
    '</div>' +
    '<div class="hint" id="dp-preview"></div>' +
    '</div>' +
    '<div id="dp-phrase-box"></div>' +
    '<div class="row"><button class="btn primary" onclick="doDeploy(' + id + ')">确认部署</button>' +
    '<button class="btn" onclick="closeModal()">取消</button>' +
    '<span class="hint" id="dp-msg"></span></div>');
  updateDeployPreview();
  on($('dp-direct'), 'change', updateDeployPreview);
}

function updateDeployPreview() {
  var direct = $('dp-direct').checked;
  var need = direct ? '我确认自动交易' : '我确认托管交易';
  $('dp-phrase-box').innerHTML =
    '<label class="fld"><span>确认口令（请一字不差输入：<b style="color:#e3c46a">' + need + '</b>）</span>' +
    '<input class="input" id="dp-phrase" placeholder="' + need + '"></label>';
  var cap = parseFloat($('dp-cap').value) || 0;
  var total = (S.boot && S.boot.balance && S.boot.balance.total_asset) || 200000;
  $('dp-preview').innerHTML = '按当前总资产约 ' + fmtMoney(total) + ' 元算，这个策略最多能持有 ' +
    fmtMoney(total * cap / 100) + ' 元的仓位。';
}

function doDeploy(id) {
  var body = {
    id: id,
    direct_send: $('dp-direct').checked,
    capital_pct: parseFloat($('dp-cap').value) || 30,
    max_single_pct: parseFloat($('dp-single').value) || 20,
    max_order_amount: parseFloat($('dp-order').value) || 50000,
    stop_loss_pct: parseFloat($('dp-sl').value) || 0,
    take_profit_pct: parseFloat($('dp-tp').value) || 0,
    expires_days: parseInt($('dp-days').value, 10) || 0,
    phrase: $('dp-phrase') ? $('dp-phrase').value : ''
  };
  $('dp-msg').innerHTML = '<span class="spin"></span>部署中…';
  api('/api/strategies/deploy', { method: 'POST', body: body }).then(function (r) {
    if (!r.ok) {
      $('dp-msg').textContent = r.message || '部署失败';
      toast(r.message || '部署失败', 'err', 9000);
      if (r.phrase_required) {
        var el = $('dp-phrase');
        if (el) { el.value = ''; el.placeholder = r.phrase_required; el.focus(); }
      }
      return;
    }
    toast(r.message, 'ok', 7000);
    closeModal();
    loadStrategies();
    loadRuntime();
    loadPreflight();
  });
}

function undeployStrategy(id) {
  if (!confirm('确定让这个策略下线吗？\n\n引擎会立刻停止为它下单（已持有的仓位不会自动卖出，' +
               '需要你自己决定怎么处理）。')) return;
  api('/api/strategies/undeploy', { method: 'POST', body: { id: id } }).then(function (r) {
    toast(r.message || '已下线', 'ok');
    loadStrategies(); loadRuntime();
  });
}

/* ---------------- 实盘：自动交易监控 ---------------- */
function loadRuntime() {
  api('/api/strategies/runtime').then(function (r) {
    if (!r.strategies) return;
    S.runtime = r;
    $('rt-count').textContent = r.deployed_count + ' 个自动交易中';
    var dep = r.strategies.filter(function (s) { return s.deployed; });
    var tb = $('rt-table').querySelector('tbody');
    if (!dep.length) {
      tb.innerHTML = '<tr><td colspan="11" class="empty">还没有部署任何策略。' +
        '去【策略】页把策略配好，点【部署 · 开始自动交易】。</td></tr>';
      return;
    }
    tb.innerHTML = dep.map(function (s) {
      var total = (r.balance && r.balance.total_asset) || 0;
      var used = total ? (s.market_value / total * 100) : 0;
      var mode = s.direct_send ? '<span class="badge auto">全自动直发</span>'
                               : '<span class="badge confirm">待你确认</span>';
      var st = s.expired ? '<span class="tag off">已过期</span>'
                         : (r.engine_running ? '<span class="tag on">运行中</span>'
                                             : '<span class="tag off">引擎未启动</span>');
      return '<tr>' +
        '<td><b>' + esc(s.name) + '</b></td>' +
        '<td>' + mode + '</td>' +
        '<td>' + st + '</td>' +
        '<td>' + fmtNum(s.capital_pct, 0) + '%（' + fmtMoney(total * s.capital_pct / 100) + '）</td>' +
        '<td>' + fmtMoney(s.market_value) + ' <span class="muted">(' + fmtNum(used, 1) + '%)</span></td>' +
        '<td class="down">-' + fmtNum(s.stop_loss_pct, 1) + '%</td>' +
        '<td>' + s.holding_count + ' 只' +
          ((s.holdings && s.holdings.length)
            ? '<div class="hint" style="font-size:11px;line-height:1.5">' +
              s.holdings.map(function (h) {
                return esc(h.name) + ' ' + h.qty +
                  (h.available !== undefined && h.available < h.qty ? '（可用' + h.available + '）' : '');
              }).join('<br>') + '</div>'
            : '') +
        '</td>' +
        '<td class="' + cls(s.pnl) + '">' + sign(s.pnl, 2) + '</td>' +
        '<td class="muted">' + (s.today_orders || 0) + ' 委托 / ' + (s.today_tickets || 0) + ' 待确认</td>' +
        '<td class="muted">' + esc(s.expires_at || '长期') + '</td>' +
        '<td><button class="btn tiny" onclick="previewStrategy(' + s.id + ')">看信号</button> ' +
        '<button class="btn tiny danger" onclick="undeployStrategy(' + s.id + ')">下线</button></td>' +
        '</tr>';
    }).join('');
  });
}

function saveStrategy() {
  var body = {
    id: parseInt($('st-id').value, 10) || undefined,
    name: $('st-name').value.trim(),
    stype: $('st-template').value,
    params: collectParams($('st-params'), $('st-template')),
    symbols: parseSymbols($('st-symbols').value),
    symbol_weights: readWeights(),
    mode: $('st-mode').value,
    enabled: false
  };
  if (body.id) {
    for (var i = 0; i < S.strategies.length; i++) if (S.strategies[i].id === body.id) body.enabled = S.strategies[i].enabled;
  }
  api('/api/strategies', { method: 'POST', body: body }).then(function (r) {
    if (r.ok) { toast('策略已保存', 'ok'); $('st-id').value = r.id; loadStrategies(); }
    else toast(r.message || '保存失败', 'err');
  });
}

function editStrategy(id) {
  for (var i = 0; i < S.strategies.length; i++) {
    var s = S.strategies[i];
    if (s.id !== id) continue;
    $('st-id').value = s.id; $('st-name').value = s.name; $('st-template').value = s.stype;
    $('st-symbols').value = (s.symbols || []).join(','); $('st-mode').value = s.mode || 'signal';
    S.strategyWeights = s.symbol_weights || {};
    renderStrategyParams($('st-template'), $('st-params'), s.params);
    renderWeights();
    switchTab('strategy');
    toast('已载入策略（含权重），改完记得点【保存策略】');
    return;
  }
}

function toggleStrategy(id, enabled) {
  api('/api/strategies/toggle', { method: 'POST', body: { id: id, enabled: enabled } }).then(function (r) {
    if (r.ok) { toast(enabled ? '已启用，引擎启动后会执行' : '已停用', 'ok'); loadStrategies(); }
  });
}
function delStrategy(id) {
  if (!confirm('确定删除这个策略吗？')) return;
  api('/api/strategies', { method: 'DELETE', body: { id: id } }).then(function () { loadStrategies(); toast('已删除'); });
}
function previewStrategy(id) {
  api('/api/strategies/preview', { method: 'POST', body: { id: id } }).then(function (r) {
    showPreview(r);
  });
}
function runStrategy(id) {
  api('/api/strategies/run', { method: 'POST', body: { id: id, dry_run: true } }).then(function (r) {
    var a = r.actions || [];
    if (!a.length) { toast('这次没有产生任何动作（可能没到信号，或非交易时段）'); return; }
    toast(a.map(function (x) { return x.symbol + ' ' + x.action + (x.qty ? ' ' + x.qty + '股' : ''); }).join('；'), 'ok', 8000);
  });
}
function previewForm() {
  api('/api/strategies/preview', {
    method: 'POST',
    body: { stype: $('st-template').value, params: collectParams($('st-params'), $('st-template')), symbols: parseSymbols($('st-symbols').value) }
  }).then(showPreview);
}
function showPreview(r) {
  var box = $('st-preview-box');
  if (!r.ok) { box.textContent = '失败：' + (r.message || ''); return; }
  var st = r.state || {};
  var keys = Object.keys(st);
  if (!keys.length) { box.textContent = '没有算出任何信号（可能是历史数据不够）'; return; }
  box.innerHTML = keys.map(function (k) {
    var v = st[k];
    var col = v.target === 'long' ? 'up' : 'down';
    return k + '  →  ' + (v.target === 'long' ? '应该持有' : '应该空仓') +
      '  [' + (v.fresh ? '信号是新的' : '信号已过期 ' + v.signal_date) + ']\n' +
      '     信号日 ' + (v.signal_date || '-') + '，说明：' + (v.reason || '-') +
      '，建议仓位 ' + (v.weight ? v.weight.toFixed(1) : '-') + '%';
  }).join('\n\n');
}

/* =========================================================================
   回测
   ========================================================================= */
function runBacktest(extra) {
  // 从策略卡片点【回测】进来时会传 { __bt:true, name, symbol_weights }；
  // 直接点页面上的【开始回测】时 extra 是 MouseEvent，要走空对象。
  var opt = (extra && extra.__bt === true) ? extra : {};
  var body = {
    stype: $('bt-template').value,
    params: collectParams($('bt-params'), $('bt-template')),
    symbols: parseSymbols($('bt-symbols').value),
    start: $('bt-start').value,
    end: $('bt-end').value,
    capital: parseFloat($('bt-capital').value) || 200000,
    position_pct: $('bt-pct').value === '' ? null : parseFloat($('bt-pct').value),
    name: opt.name || '',
    symbol_weights: opt.symbol_weights || null
  };
  if (!body.symbols.length) { toast('请填写股票池', 'warn'); return; }
  $('bt-hint').innerHTML = '<span class="spin"></span>正在拉取历史数据并逐日模拟，请稍等…（股票越多、时间越长越慢）';
  $('bt-run').disabled = true;
  api('/api/backtest/run', { method: 'POST', body: body }).then(function (r) {
    $('bt-run').disabled = false;
    if (!r.ok) { $('bt-hint').textContent = '失败：' + (r.message || r.error || ''); toast('回测失败：' + (r.message || r.error), 'err'); return; }
    $('bt-hint').textContent = '完成，用时 ' + r.elapsed_sec + ' 秒';
    renderBacktest(r);
    loadHistory();
    toast('回测完成：总收益 ' + sign(r.metrics.total_return_pct, 2) + '%', 'ok');
  });
}

// 已保存策略 → 一键回测：把策略的类型/参数/股票池/自定义权重原样载入回测页并直接开跑
function backtestStrategy(id) {
  var s = null;
  for (var i = 0; i < S.strategies.length; i++) if (S.strategies[i].id === id) s = S.strategies[i];
  if (!s) { toast('找不到策略', 'err'); return; }
  if (!s.symbols || !s.symbols.length) { toast('这个策略没有股票池，先点【编辑】填好再回测', 'warn'); return; }
  $('bt-template').value = s.stype;
  $('bt-symbols').value = s.symbols.join(',');
  renderStrategyParams($('bt-template'), $('bt-params'), s.params);
  if (!$('bt-start').value) $('bt-start').value = '2022-01-01';
  switchTab('backtest');
  window.scrollTo(0, 0);
  toast('已载入策略《' + s.name + '》的参数，开始回测…', 'ok', 5000);
  runBacktest({ __bt: true, name: s.name, symbol_weights: s.symbol_weights || null });
}

// 回测页的「从已保存策略载入」下拉框
function fillBacktestStrategySelect() {
  var sel = $('bt-from-strategy');
  if (!sel) return;
  var cur = sel.value;
  var opts = ['<option value="">— 手动配置（不载入策略）—</option>'];
  (S.strategies || []).forEach(function (s) {
    var n = (s.symbols || []).length;
    opts.push('<option value="' + s.id + '">' + esc(s.name) + '（' + n + ' 只股票）</option>');
  });
  sel.innerHTML = opts.join('');
  if (cur) sel.value = cur;
}

function loadBacktestFromStrategy() {
  var sel = $('bt-from-strategy');
  var id = parseInt(sel.value, 10);
  if (!id) return;
  var s = null;
  for (var i = 0; i < S.strategies.length; i++) if (S.strategies[i].id === id) s = S.strategies[i];
  if (!s) return;
  $('bt-template').value = s.stype;
  $('bt-symbols').value = (s.symbols || []).join(',');
  renderStrategyParams($('bt-template'), $('bt-params'), s.params);
  if (!$('bt-start').value) $('bt-start').value = '2022-01-01';
  toast('已载入策略《' + s.name + '》的参数，点【开始回测】即可', 'ok', 5000);
}

function renderBacktest(r) {
  var m = r.metrics;
  $('bt-result').style.display = 'block';
  var cards = [
    ['总收益', sign(m.total_return_pct, 2) + '%', m.total_return_pct],
    ['年化收益', sign(m.annual_return_pct, 2) + '%', m.annual_return_pct],
    ['最大回撤', m.max_drawdown_pct.toFixed(2) + '%', m.max_drawdown_pct],
    ['夏普比率', m.sharpe.toFixed(2), m.sharpe],
    ['胜率', m.win_rate_pct.toFixed(1) + '%', null],
    ['盈亏笔数', m.win_count + ' 赢 / ' + m.lose_count + ' 亏', null],
    ['交易次数', m.trade_count, null],
    ['总手续费', fmtMoney(m.total_fee), null],
    ['对比沪深300', sign(m.excess_return_pct, 2) + '%', m.excess_return_pct],
    ['期末资金', fmtMoney(m.final_equity), null]
  ];
  $('bt-cards').innerHTML = cards.map(function (c) {
    return '<div class="card"><div class="k">' + c[0] + '</div><div class="v ' + cls(c[2]) + '">' + c[1] + '</div></div>';
  }).join('');

  var wbox = $('bt-warn');
  var ws = r.warnings || [];
  if (ws.length) {
    wbox.style.display = 'block';
    wbox.innerHTML = '<b>需要注意：</b><br>' + ws.map(function (w) { return '· ' + esc(w); }).join('<br>');
  } else { wbox.style.display = 'none'; }

  renderAttribution(r.attribution);

  var eq = r.equity_curve.map(function (x) { return x.equity; });
  var series = [{ data: eq, color: '#3b82f6', name: '策略', width: 1.8 }];
  if (r.benchmark_curve && r.benchmark_curve.length) {
    var bm = r.benchmark_curve.map(function (x) { return x.value; });
    while (bm.length < eq.length) bm.push(bm[bm.length - 1]);
    series.push({ data: bm.slice(0, eq.length), color: '#8b949e', name: '沪深300', width: 1.2 });
  }
  drawLines($('bt-chart'), series, { legend: true });

  var tb = $('bt-trades').querySelector('tbody');
  var tr = r.trades || [];
  $('bt-trades-hint').textContent = tr.length + ' 条，显示最近 200 条';
  var show = tr.slice(-200).reverse();
  tb.innerHTML = show.length ? show.map(function (t) {
    return '<tr><td>' + esc(t.date) + '</td><td>' + esc(t.symbol) + '</td>' +
      '<td class="' + (t.side === 'buy' ? 'up' : 'down') + '">' + (t.side === 'buy' ? '买入' : '卖出') + '</td>' +
      '<td>' + fmtNum(t.price, 3) + '</td><td>' + t.qty + '</td><td>' + fmtMoney(t.amount) + '</td>' +
      '<td>' + fmtNum(t.fee, 2) + '</td><td class="' + cls(t.pnl) + '">' + (t.side === 'sell' ? sign(t.pnl, 2) : '—') + '</td>' +
      '<td class="muted">' + esc(t.reason || '') + '</td></tr>';
  }).join('') : '<tr><td colspan="9" class="empty">这次没有任何成交</td></tr>';
}

// 收益归因：把总收益拆成 市场给的 / 择时赚的 / 选股赚的 / 费用拖累
function renderAttribution(att) {
  var box = $('bt-attr');
  if (!att) { if (box) box.style.display = 'none'; return; }
  box.style.display = 'block';
  var cards = [
    ['市场贡献', sign(att.market_return_pct, 2) + '%', att.market_return_pct,
     '跟着大盘被动涨跌的部分'],
    ['择时贡献', sign(att.timing_contribution_pct, 2) + '%', att.timing_contribution_pct,
     '仓位高低带来的额外收益（负=被现金拖累）'],
    ['选股贡献', sign(att.selection_contribution_pct, 2) + '%', att.selection_contribution_pct,
     '选出跑赢大盘的股票带来的收益（含费用）'],
    ['费用拖累', '-' + fmtNum(att.fee_drag_pct, 2) + '%', -att.fee_drag_pct,
     '手续费+印花税+滑点吃掉的部分'],
    ['市场暴露 β', att.beta === null ? '—' : att.beta.toFixed(2), null, '相对沪深300的敏感度'],
    ['年化 α', att.alpha_annualized_pct === null ? '—' : sign(att.alpha_annualized_pct, 2) + '%',
     att.alpha_annualized_pct, '剔除市场影响后的超额能力']
  ];
  $('bt-attr-cards').innerHTML = cards.map(function (c) {
    return '<div class="card"><div class="k">' + c[0] + '</div>' +
      '<div class="v ' + cls(c[2]) + '">' + c[1] + '</div>' +
      (c[3] ? '<div class="s">' + esc(c[3]) + '</div>' : '') + '</div>';
  }).join('');

  function lst(title, arr, color) {
    if (!arr || !arr.length) return '';
    var items = arr.map(function (x) {
      var label = x.industry ? x.industry : x.symbol;
      return '<span class="' + color + '">' + esc(label) + ' ' + sign(x.pnl, 0) + ' 元</span>';
    }).join('　');
    return '<div class="panel"><b>' + title + '</b><div class="attr-list">' + items + '</div></div>';
  }
  $('bt-attr-detail').innerHTML =
    lst('贡献最大的票', att.top_winners, 'pos') +
    lst('拖累最大的票', att.top_losers, 'neg') +
    lst('行业贡献', att.industry_contribution, '');
}

// 样本外验证（walk-forward）
function runWalkForward() {
  var body = {
    stype: $('bt-template').value,
    params: collectParams($('bt-params'), $('bt-template')),
    symbols: parseSymbols($('bt-symbols').value),
    start: $('bt-start').value,
    end: $('bt-end').value,
    capital: parseFloat($('bt-capital').value) || 200000,
    position_pct: $('bt-pct').value === '' ? null : parseFloat($('bt-pct').value),
    train_days: 252, test_days: 63, step_days: 63
  };
  if (!body.symbols.length) { toast('请填写股票池', 'warn'); return; }
  $('bt-hint').innerHTML = '<span class="spin"></span>正在滚动样本外验证（多个窗口来回跑，比普通回测慢几倍）…';
  $('bt-wf').disabled = true;
  api('/api/backtest/walk_forward', { method: 'POST', body: body }).then(function (r) {
    $('bt-wf').disabled = false;
    if (!r.ok) { $('bt-hint').textContent = '失败：' + (r.error || r.message || ''); toast('样本外验证失败：' + (r.error || r.message), 'err'); return; }
    $('bt-hint').textContent = '样本外验证完成，共 ' + r.window_count + ' 个滚动窗口';
    var box = $('bt-wf-result');
    box.style.display = 'block';
    var s = r.summary || {};
    var lvl = r.level === 'good' ? 'ok' : (r.level === 'warn' ? 'warn' : 'err');
    var sum = $('bt-wf-summary');
    sum.style.display = 'block';
    sum.innerHTML = '<b>判定：' + esc(r.verdict) + '</b>';
    var cards = [
      ['样本外平均收益', sign(s.oos_avg_return_pct, 2) + '%', s.oos_avg_return_pct],
      ['样本外年化(估算)', sign(s.oos_avg_annualized_pct, 2) + '%', s.oos_avg_annualized_pct],
      ['样本外正收益占比', s.oos_win_rate_pct.toFixed(0) + '%', null],
      ['样本外平均夏普', (s.oos_avg_sharpe || 0).toFixed(2), null],
      ['样本外最差一段', sign(s.oos_worst_return_pct, 2) + '%', s.oos_worst_return_pct],
      ['样本内/外收益比', (s.is_oos_ratio || 0).toFixed(2), null,
       '越接近 1 越稳，明显大于 2 说明可能过拟合']
    ];
    $('bt-wf-cards').innerHTML = cards.map(function (c) {
      return '<div class="card"><div class="k">' + c[0] + '</div>' +
        '<div class="v ' + cls(c[2]) + '">' + c[1] + '</div>' +
        (c[3] ? '<div class="s">' + esc(c[3]) + '</div>' : '') + '</div>';
    }).join('');
    var tb = $('bt-wf-table').querySelector('tbody');
    tb.innerHTML = (r.windows || []).map(function (w) {
      return '<tr><td>' + esc(w.train_start) + '~' + esc(w.train_end) + '</td>' +
        '<td>' + esc(w.test_start) + '~' + esc(w.test_end) + '</td>' +
        '<td class="' + cls(w.is_return_pct) + '">' + sign(w.is_return_pct, 2) + '%</td>' +
        '<td class="' + cls(w.oos_return_pct) + '">' + sign(w.oos_return_pct, 2) + '%</td>' +
        '<td>' + (w.oos_sharpe || 0).toFixed(2) + '</td>' +
        '<td>' + (w.oos_maxdd_pct || 0).toFixed(1) + '%</td>' +
        '<td class="' + cls(w.oos_excess_pct) + '">' + sign(w.oos_excess_pct, 2) + '%</td></tr>';
    }).join('');
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    toast('样本外验证完成，判定：' + (r.level === 'good' ? '稳健' : r.level === 'warn' ? '谨慎' : '疑似过拟合'), lvl);
  });
}

function loadHistory() {
  api('/api/backtest/list').then(function (r) {
    var tb = $('bt-history').querySelector('tbody');
    var list = r.list || [];
    if (!list.length) { tb.innerHTML = '<tr><td colspan="5" class="empty">还没有回测记录</td></tr>'; return; }
    tb.innerHTML = list.map(function (x) {
      return '<tr><td>' + esc(x.ts) + '</td><td>' + esc(x.name) + '</td><td>' + esc(x.stype) + '</td>' +
        '<td>' + esc((x.symbols || []).join(',')) + '</td>' +
        '<td><button class="btn tiny" onclick="openBacktest(' + x.id + ')">查看</button> ' +
        '<button class="btn tiny danger" onclick="delBacktest(' + x.id + ')">删除</button></td></tr>';
    }).join('');
  });
}
function openBacktest(id) {
  api('/api/backtest/get', { params: { id: id } }).then(function (r) {
    if (!r.result) { toast('读取失败', 'err'); return; }
    renderBacktest(r.result);
    switchTab('backtest');
    window.scrollTo(0, 0);
  });
}
function delBacktest(id) {
  api('/api/backtest/delete', { method: 'POST', body: { id: id } }).then(function () { loadHistory(); });
}

/* =========================================================================
   交易通道 & 实盘
   ========================================================================= */
function loadBrokers() {
  api('/api/broker/list').then(function (r) {
    S.brokers = r.brokers || []; S.activeBroker = r.active;
    api('/api/broker/credential', { params: { name: 'ths' } }).then(function (c) {
      S.cred = c || {};
      var box = $('br-list');
      box.innerHTML = S.brokers.map(function (b) {
        var flds = (b.fields || []).map(function (f) {
          var val = '';
          if (S.cfg && S.cfg.broker && S.cfg.broker[b.name]) val = S.cfg.broker[b.name][f.key] || '';
          if (f.type === 'select') {
            return '<label class="fld"><span>' + esc(f.label) + '</span><select class="input" data-b="' + esc(b.name) + '" data-k="' + esc(f.key) + '">' +
              (f.options || []).map(function (o) { return '<option value="' + esc(o.value) + '"' + (String(o.value) === String(val || f.default) ? ' selected' : '') + '>' + esc(o.label) + '</option>'; }).join('') +
              '</select></label>';
          }
          return '<label class="fld"><span>' + esc(f.label) + '</span><input class="input" data-b="' + esc(b.name) + '" data-k="' + esc(f.key) + '" value="' + esc(val) + '" placeholder="' + esc(f.placeholder || '') + '">' +
            (f.help ? '<div class="help">' + esc(f.help) + '</div>' : '') + '</label>';
        }).join('');
        if (b.name === 'ths') flds += credentialBox();
        return '<div class="broker ' + (b.name === S.activeBroker ? 'active' : '') + '">' +
          '<h4>' + esc(b.display) +
          '<span class="tag ' + (b.is_live ? 'live' : 'paper') + '">' + (b.is_live ? '真钱' : '虚拟') + '</span>' +
          '<span class="tag ' + (b.connected ? 'on' : 'off') + '">' + (b.connected ? '已连接' : '未连接') + '</span>' +
          (b.name === S.activeBroker ? '<span class="tag on">当前使用</span>' : '') + '</h4>' +
          '<div class="d">' + esc(b.desc || '') + '</div>' + flds +
          (b.last_error ? '<div class="d" style="color:#f85149">最近错误：' + esc(b.last_error) + '</div>' : '') +
          '<div class="acts row">' +
          '<button class="btn tiny primary" onclick="switchBroker(\'' + b.name + '\')">' + (b.name === S.activeBroker ? '重新连接' : '切换到它') + '</button>' +
          '<button class="btn tiny" onclick="checkBroker(\'' + b.name + '\')">环境自检</button>' +
          '</div></div>';
      }).join('');
    });
  });
}

function credentialBox() {
  var c = S.cred || {};
  return '<div class="credbox">' +
    '<div class="d" style="margin-bottom:6px"><b>同花顺登录账号（可选）</b><br>' +
    '不填也能用 —— 你只要自己把同花顺下单窗口打开、登录好，程序就能驱动它下单。<br>' +
    '填了的话，程序会尝试帮你自动填登录框，省一步手动登录。</div>' +
    '<label class="fld"><span>资金账号</span>' +
    '<input class="input" id="cred-user" value="' + esc(c.username || '') + '" placeholder="你的资金账号"></label>' +
    '<label class="fld"><span>交易密码' +
    (c.has_password ? '（已保存，留空表示不修改）' : '') + '</span>' +
    '<input class="input" id="cred-pass" type="password" autocomplete="new-password" placeholder="' +
    (c.has_password ? '已加密保存，留空不改' : '输入交易密码') + '"></label>' +
    '<label class="chk"><input type="checkbox" id="cred-auto"' + (c.auto_login ? ' checked' : '') + '> 连接时自动登录</label>' +
    '<div class="row">' +
    '<button class="btn tiny primary" onclick="saveCredential()">保存账号</button>' +
    '<button class="btn tiny danger" onclick="clearCredential()">删除已保存密码</button>' +
    '<span class="hint" id="cred-msg">' +
    (c.encryption_available
      ? (c.has_password ? '密码已用 Windows 系统加密保存' : '密码会用 Windows 系统加密保存，只有这台电脑能解开')
      : '⚠️ 系统加密不可用，出于安全我不会保存密码，请手动登录') +
    '</span></div></div>';
}

function saveCredential() {
  var body = {
    name: 'ths',
    username: $('cred-user').value,
    password: $('cred-pass').value,
    auto_login: $('cred-auto').checked
  };
  $('cred-msg').innerHTML = '<span class="spin"></span>保存中…';
  api('/api/broker/credential', { method: 'POST', body: body }).then(function (r) {
    toast(r.message || (r.ok ? '已保存' : '保存失败'), r.ok ? 'ok' : 'err', 7000);
    $('cred-pass').value = '';
    loadBrokers();
  });
}

function clearCredential() {
  if (!confirm('确定删除已保存的同花顺密码吗？')) return;
  api('/api/broker/credential/clear', { method: 'POST', body: { name: 'ths' } }).then(function (r) {
    toast(r.message || '已删除', 'ok');
    loadBrokers();
  });
}

/* =========================================================================
   实盘就绪检查
   ========================================================================= */
function loadPreflight() {
  api('/api/live/preflight').then(function (r) {
    var box = $('pf-box');
    if (!r.checks) { box.innerHTML = '<div class="empty">检查失败</div>'; return; }
    box.innerHTML = r.checks.map(function (c) {
      var k = c.ok ? 'ok' : (c.critical ? 'bad' : 'warn');
      var ico = c.ok ? '✔' : (c.critical ? '✘' : '!');
      return '<div class="pfitem ' + k + '"><span class="ico">' + ico + '</span>' +
        '<span class="nm">' + esc(c.name) + (c.critical ? ' <span class="tag live">关键</span>' : '') + '</span>' +
        '<span class="dt">' + esc(c.detail) + '</span></div>';
    }).join('') +
      '<div class="row"><b style="color:' + (r.ready ? '#3fb950' : '#f85149') + '">' +
      (r.ready ? '全部关键项通过，可以开实盘' : '还有关键项没过：' + esc((r.critical_failed || []).join('、'))) +
      '</b></div>';
  });
}

/* =========================================================================
   待确认委托单
   ========================================================================= */
function loadTickets() {
  api('/api/tickets').then(function (r) {
    var list = r.tickets || [];
    var pending = list.filter(function (t) { return t.status === '待确认'; });
    $('tk-count').textContent = pending.length + ' 张待确认';
    var tb = $('tk-table').querySelector('tbody');
    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="11" class="empty">暂时没有委托单。策略出信号或你手动下单时会出现在这里。</td></tr>';
      return;
    }
    tb.innerHTML = list.slice(0, 80).map(function (t) {
      var can = t.status === '待确认';
      return '<tr>' +
        '<td>' + esc(String(t.created_at || '').slice(5, 16)) + '</td>' +
        '<td>' + esc(t.source || '') + (t.strategy_name ? '<br><span class="muted" style="font-size:11px">' + esc(t.strategy_name) + '</span>' : '') + '</td>' +
        '<td>' + esc(String(t.symbol || '').split('.')[0]) + '</td>' +
        '<td>' + esc(t.name || '') + '</td>' +
        '<td class="' + (t.side === 'buy' ? 'tk-buy' : 'tk-sell') + '">' + (t.side === 'buy' ? '买入' : '卖出') + '</td>' +
        '<td>' + esc(t.qty) + '</td>' +
        '<td>' + fmtNum(t.price, 3) + '</td>' +
        '<td id="tk-live-' + t.id + '" class="muted">—</td>' +
        '<td class="muted" style="max-width:200px">' + esc(t.reason || '') + '</td>' +
        '<td>' + esc(t.status) + (t.result ? '<br><span class="muted" style="font-size:11px">' + esc(String(t.result).slice(0, 40)) + '</span>' : '') + '</td>' +
        '<td>' + (can
          ? '<button class="btn tiny ok" onclick="sendTicket(' + t.id + ')">发送</button> ' +
            '<button class="btn tiny" onclick="ignoreTicket(' + t.id + ')">忽略</button>'
          : '') + '</td>' +
        '</tr>';
    }).join('');
    // 顺便把当前价填上，方便判断价格跑了多少
    var syms = list.filter(function (t) { return t.status === '待确认'; })
      .map(function (t) { return t.symbol; });
    if (syms.length) {
      api('/api/market/quote', { params: { symbols: syms.join(',') } }).then(function (qr) {
        list.forEach(function (t) {
          var cell = $('tk-live-' + t.id);
          var q = (qr.quotes || {})[t.symbol];
          if (!cell || !q || !q.price) return;
          var drift = t.price ? (q.price / t.price - 1) * 100 : 0;
          cell.innerHTML = '<span class="' + cls(drift) + '">' + fmtNum(q.price, 3) +
            '</span> <span class="muted" style="font-size:11px">' + sign(drift, 2) + '%</span>';
        });
      });
    }
  });
}

function sendTicket(id) {
  var btn = event && event.target;
  if (btn) btn.disabled = true;
  api('/api/tickets/send', { method: 'POST', body: { id: id } }).then(function (r) {
    toast((r.ok ? '✔ ' : '✘ ') + (r.message || ''), r.ok ? 'ok' : 'err', 8000);
    loadTickets(); loadTrade(); loadStops(); loadDailyReport();
  });
}

function sendAllTickets() {
  api('/api/tickets').then(function (r) {
    var p = (r.tickets || []).filter(function (t) { return t.status === '待确认'; });
    if (!p.length) { toast('没有待确认的委托单', 'warn'); return; }
    var txt = p.map(function (t) {
      return (t.side === 'buy' ? '买入' : '卖出') + ' ' + t.symbol.split('.')[0] + ' ' + t.qty + '股 @' + fmtNum(t.price, 2);
    }).join('\n');
    if (!confirm('即将发送 ' + p.length + ' 张委托单：\n\n' + txt + '\n\n确认发送？')) return;
    api('/api/tickets/send', { method: 'POST', body: { all: true } }).then(function (r) {
      toast(r.message || '已发送', 'ok', 9000);
      loadTickets(); loadTrade(); loadStops(); loadDailyReport();
    });
  });
}

function ignoreTicket(id) {
  api('/api/tickets/ignore', { method: 'POST', body: { id: id } }).then(function (r) {
    toast(r.message || '已忽略', 'ok');
    loadTickets();
  });
}

function clearTicketsUI() {
  api('/api/tickets/clear', { method: 'POST', body: { all: false } }).then(function (r) {
    toast(r.message, 'ok'); loadTickets();
  });
}

/* =========================================================================
   止损监控 / 一键止损
   ========================================================================= */
function loadStops() {
  api('/api/live/stops').then(function (r) {
    var tb = $('st-table').querySelector('tbody');
    var items = r.items || [];
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="11" class="empty">当前没有持仓</td></tr>';
      return;
    }
    tb.innerHTML = items.map(function (x) {
      return '<tr>' +
        '<td><a href="javascript:void(0)" onclick="pickSymbol(\'' + esc(x.symbol) + '\')">' + esc(String(x.symbol).split('.')[0]) + '</a></td>' +
        '<td>' + esc(x.name || '') + '</td>' +
        '<td>' + esc(x.available) + '</td>' +
        '<td>' + fmtNum(x.avg_cost, 3) + '</td>' +
        '<td>' + fmtNum(x.last_price, 3) + '</td>' +
        '<td class="' + cls(x.pnl_pct) + '">' + sign(x.pnl_pct, 2) + '%</td>' +
        '<td>' + (x.stop ? fmtNum(x.stop, 3) : '—') + '</td>' +
        '<td class="' + cls(x.dist_pct) + '">' + (x.dist_pct === null ? '—' : sign(x.dist_pct, 2) + '%') + '</td>' +
        '<td class="muted" style="font-size:11.5px">' + esc(x.stop_source || '—') + '</td>' +
        '<td style="color:#d29922">' + esc(x.alert || '') + '</td>' +
        '<td>' + (x.available > 0
          ? '<button class="btn tiny danger" onclick="quickSell(\'' + esc(x.symbol) + '\',' + x.available + ')">止损卖出</button>'
          : '<span class="muted">T+1锁定</span>') + '</td>' +
        '</tr>';
    }).join('');
  });
}

function stopAllPositions() {
  api('/api/live/stops').then(function (r) {
    var items = (r.items || []).filter(function (x) { return x.available > 0; });
    if (!items.length) { toast('没有可卖出的持仓（可能都是 T+1 锁定）', 'warn'); return; }
    var txt = items.map(function (x) {
      return x.symbol.split('.')[0] + ' ' + x.name + ' ' + x.available + '股' +
        (x.pnl_pct !== null ? '（' + sign(x.pnl_pct, 2) + '%）' : '');
    }).join('\n');
    if (!confirm('一键止损会用【市价】卖出以下全部持仓：\n\n' + txt + '\n\n市价卖出不保证价格，确认吗？')) return;
    api('/api/live/stop-all', { method: 'POST', body: {} }).then(function (r) {
      var ok = (r.results || []).filter(function (x) { return x.ok; }).length;
      toast('已提交 ' + ok + ' 笔止损委托', 'warn', 9000);
      loadStops(); loadTrade(); loadDailyReport();
    });
  });
}

/* =========================================================================
   当日交易报告
   ========================================================================= */
function loadDailyReport() {
  api('/api/report/daily').then(function (r) {
    if (!r.ok) return;
    $('rp-hint').textContent = r.date + ' · ' + r.broker + (r.is_live ? '（实盘）' : '（模拟盘）');
    var cards = [
      ['成交笔数', r.trade_count + ' 笔', null],
      ['买 / 卖', r.buy_count + ' / ' + r.sell_count, null],
      ['已实现盈亏', sign(r.realized_pnl, 2) + ' 元', r.realized_pnl],
      ['胜率', r.win_rate === null ? '—' : r.win_rate + '%', null],
      ['手续费合计', fmtMoney(r.fee_total) + ' 元', null],
      ['委托笔数', r.order_count + ' 笔', null]
    ];
    $('rp-cards').innerHTML = cards.map(function (c) {
      return '<div class="card"><div class="k">' + c[0] + '</div><div class="v ' + cls(c[2]) +
        '" style="font-size:17px">' + c[1] + '</div></div>';
    }).join('');
    var tb = $('rp-trades').querySelector('tbody');
    var ts = r.trades || [];
    tb.innerHTML = ts.length ? ts.map(function (t) {
      return '<tr><td>' + esc(String(t.ts || '').slice(11)) + '</td>' +
        '<td>' + (t.symbol
          ? '<a href="javascript:void(0)" onclick="pickSymbol(\'' + esc(t.symbol) + '\')">' + esc(String(t.symbol).split('.')[0]) + '</a>'
          : '—') + '</td>' +
        '<td>' + (t.symbol
          ? '<a href="javascript:void(0)" onclick="pickSymbol(\'' + esc(t.symbol) + '\')">' + esc(t.name || '') + '</a>'
          : esc(t.name || '')) + '</td>' +
        '<td class="' + (t.side === 'buy' ? 'tk-buy' : 'tk-sell') + '">' + (t.side === 'buy' ? '买入' : '卖出') + '</td>' +
        '<td>' + fmtNum(t.price, 3) + '</td><td>' + esc(t.qty) + '</td>' +
        '<td>' + fmtMoney(t.amount) + '</td><td>' + fmtNum(t.fee, 2) + '</td>' +
        '<td class="' + cls(t.pnl) + '">' + (t.side === 'sell' ? sign(t.pnl, 2) : '—') + '</td></tr>';
    }).join('') : '<tr><td colspan="9" class="empty">今天还没有成交</td></tr>';
  });
}

function syncBrokerOrders() {
  api('/api/live/sync', { method: 'POST', body: {} }).then(function (r) {
    toast(r.ok ? ('已同步 ' + (r.synced || 0) + ' 条委托状态') : (r.message || '同步失败'),
      r.ok ? 'ok' : 'err');
    loadTrade();
  });
}

function brokerCfg(name) {
  var out = {};
  $$('[data-b="' + name + '"]').forEach(function (el) { out[el.getAttribute('data-k')] = el.value; });
  return out;
}

function switchBroker(name) {
  var cfg = brokerCfg(name);
  toast('正在切换/连接…');
  api('/api/broker/switch', { method: 'POST', body: { name: name, cfg: cfg, connect: true } }).then(function (r) {
    if (r.ok) { toast(r.message || '已切换', 'ok'); }
    else { toast(r.message || '连接失败', 'err', 9000); if (r.hint) showModal('连接失败', '<pre class="preview">' + esc(r.hint) + '</pre>'); }
    loadBrokers(); loadLivePanel();
    api('/api/bootstrap').then(function (b) { S.cfg = b.config; renderHeader(b); });
  });
}

function checkBroker(name) {
  api('/api/broker/check', { method: 'POST', body: { name: name } }).then(function (r) {
    var html = (r.items || []).map(function (i) {
      return '<div style="margin:6px 0"><b style="color:' + (i.ok ? '#3fb950' : '#f85149') + '">' + (i.ok ? '✔' : '✘') + '</b> ' +
        esc(i.name) + '<div class="hint">' + esc(i.detail) + '</div></div>';
    }).join('');
    showModal('环境自检：' + name, html);
  });
}

function loadLivePanel() {
  api('/api/broker/list').then(function (r) {
    var cur = null;
    for (var i = 0; i < (r.brokers || []).length; i++) if (r.brokers[i].name === r.active) cur = r.brokers[i];
    var live = S.cfg && S.cfg.live && S.cfg.live.enabled;
    var box = $('live-body');
    if (!cur) { box.textContent = '通道信息加载失败'; return; }
    if (!cur.is_live) {
      box.innerHTML = '<div class="d" style="color:#9aa7b4">当前是【' + esc(cur.display) + '】，用的是虚拟资金，' +
        '所有下单都只在本地模拟，不会动你一分钱真钱。想实盘请切换到【同花顺】或【QMT】通道。</div>';
      return;
    }
    box.innerHTML =
      '<div class="d" style="color:#ffb3ad">当前通道【' + esc(cur.display) + '】是<b>真实资金</b>通道。' +
      '打开下面的开关后，引擎会真的向券商发出买卖委托。</div>' +
      '<div class="d">开启前请确认：<br>1) 通道已连接成功；2) 已先用模拟盘把策略跑通；' +
      '3) 风控参数（单只上限 / 单笔金额 / 每日亏损熔断）已经设好；4) 你知道急停按钮在哪。</div>' +
      (live
        ? '<div class="row"><span class="tag live">实盘已开启</span><span class="hint">开启时间：' + esc((S.cfg.live && S.cfg.live.enabled_at) || '-') + '</span>' +
          '<button class="btn danger" onclick="disableLive()">关闭实盘</button></div>'
        : '<div class="row"><input class="input" id="live-phrase" style="max-width:260px" placeholder="请输入：' + esc((S.cfg.live && S.cfg.live.confirm_phrase) || '') + '">' +
          '<button class="btn danger" onclick="enableLive()">确认启用实盘</button></div>');
  });
}

function enableLive() {
  var p = $('live-phrase') ? $('live-phrase').value : '';
  api('/api/live/enable', { method: 'POST', body: { phrase: p } }).then(function (r) {
    toast(r.message || (r.ok ? '已启用' : '失败'), r.ok ? 'warn' : 'err', 8000);
    S.cfg = null; api('/api/bootstrap').then(function (b) { S.cfg = b.config; renderHeader(b); loadLivePanel(); });
  });
}
function disableLive() {
  api('/api/live/disable', { method: 'POST', body: {} }).then(function (r) {
    toast(r.message, 'ok');
    api('/api/bootstrap').then(function (b) { S.cfg = b.config; renderHeader(b); loadLivePanel(); });
  });
}

function loadTrade() {
  // 持仓统一在「持仓止损监控」里展示（信息更全：带止损位和距离）
  api('/api/orders', { params: { limit: 100 } }).then(function (r) {
    var tb = $('tr-orders').querySelector('tbody');
    var list = r.orders || [];
    if (!list.length) { tb.innerHTML = '<tr><td colspan="9" class="empty">今天还没有委托</td></tr>'; return; }
    tb.innerHTML = list.map(function (o) {
      var can = (o.status === '已报');
      return '<tr><td>' + esc(String(o.ts || '').slice(11)) + '</td><td>' + esc(o.symbol) + '</td>' +
        '<td class="' + (o.side === 'buy' ? 'up' : 'down') + '">' + (o.side === 'buy' ? '买入' : '卖出') + '</td>' +
        '<td>' + fmtNum(o.price, 3) + '</td><td>' + o.qty + '</td><td>' + (o.filled_qty || 0) + '</td>' +
        '<td>' + esc(o.status) + '</td><td class="muted">' + esc(o.message || o.reason || '') + '</td>' +
        '<td>' + (can ? '<button class="btn tiny" onclick="cancelOrder(' + o.id + ')">撤单</button>' : '') + '</td></tr>';
    }).join('');
  });
  api('/api/trades', { params: { limit: 100 } }).then(function (r) {
    var tb = $('tr-trades').querySelector('tbody');
    var list = r.trades || [];
    if (!list.length) { tb.innerHTML = '<tr><td colspan="8" class="empty">今天还没有成交</td></tr>'; return; }
    tb.innerHTML = list.map(function (t) {
      return '<tr><td>' + esc(String(t.ts || '').slice(11)) + '</td><td>' + esc(t.symbol) + '</td>' +
        '<td class="' + (t.side === 'buy' ? 'up' : 'down') + '">' + (t.side === 'buy' ? '买入' : '卖出') + '</td>' +
        '<td>' + fmtNum(t.price, 3) + '</td><td>' + t.qty + '</td><td>' + fmtMoney(t.amount) + '</td>' +
        '<td>' + fmtNum(t.fee, 2) + '</td><td class="' + cls(t.pnl) + '">' + (t.side === 'sell' ? sign(t.pnl, 2) : '—') + '</td></tr>';
    }).join('');
  });
  loadLivePanel();
  loadBrokers();
  loadPreflight();
  loadTickets();
  loadStops();
  loadDailyReport();
  loadRuntime();
}

function cancelOrder(id) {
  api('/api/order/cancel', { method: 'POST', body: { order_id: id } }).then(function (r) {
    toast(r.message || (r.ok ? '已撤单' : '撤单失败'), r.ok ? 'ok' : 'err');
    loadTrade();
  });
}

/* ---------------- 下单弹窗 ---------------- */
function openOrderModal(sym, side, qty) {
  showModal('下单 · ' + sym, '');
  $('modal-body').innerHTML =
    '<div class="formgrid">' +
    '<label class="fld"><span>股票代码</span><input class="input" id="mo-symbol" value="' + esc(sym) + '"></label>' +
    '<label class="fld"><span>方向</span><select class="input" id="mo-side"><option value="buy"' + (side === 'buy' ? ' selected' : '') + '>买入</option><option value="sell"' + (side === 'sell' ? ' selected' : '') + '>卖出</option></select></label>' +
    '<label class="fld"><span>价格（留空=市价）</span><input class="input" id="mo-price" type="number" step="0.01"></label>' +
    '<label class="fld"><span>数量（股）</span><input class="input" id="mo-qty" type="number" step="100" value="' + (qty || '') + '"></label>' +
    '</div>' +
    '<div class="row"><button class="btn primary" onclick="submitOrderModal()">确认下单</button>' +
    '<button class="btn" onclick="fillMaxQty()">算最大可买</button>' +
    '<span class="hint" id="mo-hint"></span></div>';
  fillCurrentPrice(sym);
}

function fillCurrentPrice(sym) {
  api('/api/market/quote', { params: { symbols: sym } }).then(function (r) {
    var q = (r.quotes || {})[sym];
    if (q && q.price) $('mo-price').value = q.price;
  });
}

function fillMaxQty() {
  var sym = $('mo-symbol').value.trim();
  var side = $('mo-side').value;
  api('/api/paper/max', { params: { symbol: sym } }).then(function (r) {
    if (side === 'buy') $('mo-qty').value = r.max_buy || 0;
    else $('mo-qty').value = r.sellable || 0;
    $('mo-hint').textContent = side === 'buy' ? '按当前可用资金最多能买这么多股' : '当前可卖这么多股';
  });
}

function submitOrderModal() {
  var body = {
    symbol: $('mo-symbol').value.trim(),
    side: $('mo-side').value,
    price: $('mo-price').value === '' ? null : parseFloat($('mo-price').value),
    qty: parseInt($('mo-qty').value, 10) || 0,
    order_type: $('mo-price').value === '' ? 'market' : 'limit',
    reason: '网页手动下单'
  };
  if (body.side === 'buy' && body.qty % 100 !== 0) { toast('买入数量必须是 100 的整数倍', 'warn'); return; }
  $('mo-hint').innerHTML = '<span class="spin"></span>提交中…';
  api('/api/order', { method: 'POST', body: body }).then(function (r) {
    if (r.ok) { toast('委托成功：' + (r.status || '') + ' ' + (r.message || ''), 'ok'); closeModal(); loadTrade(); }
    else { $('mo-hint').textContent = r.message || '下单失败'; toast(r.message || '下单失败', 'err', 7000); }
  });
}

function showModal(title, html) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = html;
  $('modal').style.display = 'flex';
}
function closeModal() { $('modal').style.display = 'none'; }

/* =========================================================================
   设置
   ========================================================================= */
var SET_GROUPS = {
  'set-paper': {
    paper: [
      ['initial_capital', '模拟盘初始资金(元)', 'number'],
      ['commission_rate', '佣金费率(小数, 0.00025=万2.5)', 'number'],
      ['commission_min', '最低佣金(元)', 'number'],
      ['stamp_tax_rate', '印花税率(卖出, 0.0005)', 'number'],
      ['transfer_fee_rate', '过户费率(0.00001)', 'number'],
      ['slippage_bps', '滑点(基点, 3=万分之三)', 'number']
    ]
  },
  'set-risk': {
    risk: [
      ['enabled', '启用风控', 'bool'],
      ['max_single_position_pct', '单只股票最大仓位(%)', 'number'],
      ['max_total_position_pct', '总仓位上限(%)', 'number'],
      ['max_order_amount', '单笔委托最大金额(元)', 'number'],
      ['min_order_amount', '单笔最小金额(元)', 'number'],
      ['max_orders_per_day', '每日最多下单笔数', 'number'],
      ['daily_loss_limit_pct', '当日亏损熔断线(%)', 'number'],
      ['stop_loss_pct', '全局止损(%) 0=关闭', 'number'],
      ['take_profit_pct', '全局止盈(%) 0=关闭', 'number'],
      ['block_st', '自动排除 ST 股', 'bool'],
      ['blacklist', '黑名单代码(逗号分隔)', 'list']
    ]
  },
  'set-trading': {
    trading: [
      ['lot_size', '一手股数', 'number'],
      ['price_limit_check', '启用涨跌停检查', 'bool'],
      ['poll_interval_sec', '引擎轮询间隔(秒)', 'number'],
      ['order_cooldown_sec', '同标的冷却时间(秒)', 'number'],
      ['max_signal_age_bars', '建仓信号最大过期K线数(1=最新)', 'number'],
      ['auto_start_engine', '程序启动时自动开引擎', 'bool']
    ]
  },
  'set-server': {
    server: [['port', '服务端口', 'number'], ['open_browser', '启动时自动打开浏览器', 'bool'], ['host', '监听地址(127.0.0.1=仅本机)', 'text']],
    ui: [['refresh_sec', '页面刷新间隔(秒)', 'number']]
  }
};

function getCfg(path) {
  var node = S.cfg;
  var parts = path.split('.');
  for (var i = 0; i < parts.length; i++) { if (node == null) return ''; node = node[parts[i]]; }
  return node;
}

function renderSettings() {
  if (!S.cfg) return;
  Object.keys(SET_GROUPS).forEach(function (boxId) {
    var box = $(boxId);
    if (!box) return;
    var groups = SET_GROUPS[boxId];
    var html = '';
    Object.keys(groups).forEach(function (g) {
      groups[g].forEach(function (f) {
        var v = getCfg(g + '.' + f[0]);
        var path = g + '.' + f[0];
        if (f[2] === 'bool') {
          html += '<label class="fld"><span>' + esc(f[1]) + '</span><label class="chk"><input type="checkbox" data-cfg="' + path + '"' + (v ? ' checked' : '') + '> 开启</label></label>';
        } else if (f[2] === 'list') {
          html += '<label class="fld"><span>' + esc(f[1]) + '</span><input class="input" data-cfg="' + path + '" data-type="list" value="' + esc((v || []).join(',')) + '"></label>';
        } else {
          html += '<label class="fld"><span>' + esc(f[1]) + '</span><input class="input" data-cfg="' + path + '" type="' + (f[2] === 'number' ? 'number' : 'text') + '" step="any" value="' + esc(v) + '"></label>';
        }
      });
    });
    box.innerHTML = html;
  });
  api('/api/settings').then(function (r) {
    if (r.paths) $('set-paths').innerHTML = '程序目录：' + esc(r.paths.base_dir) + '<br>数据目录：' + esc(r.paths.data_dir) + '<br>数据库：' + esc(r.paths.db);
  });
}

function saveSettings() {
  var patch = {};
  $$('[data-cfg]').forEach(function (el) {
    var path = el.getAttribute('data-cfg');
    var val;
    if (el.type === 'checkbox') val = el.checked;
    else if (el.getAttribute('data-type') === 'list') val = el.value.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean);
    else if (el.type === 'number') { val = parseFloat(el.value); if (isNaN(val)) val = 0; }
    else val = el.value;
    var parts = path.split('.');
    var node = patch;
    for (var i = 0; i < parts.length - 1; i++) { if (!node[parts[i]]) node[parts[i]] = {}; node = node[parts[i]]; }
    node[parts[parts.length - 1]] = val;
  });
  api('/api/settings', { method: 'POST', body: patch }).then(function (r) {
    if (r.ok) {
      S.cfg = r.config;
      $('set-msg').textContent = '已保存 ✓';
      var feeKeys = ['commission_rate', 'commission_min', 'stamp_tax_rate', 'transfer_fee_rate', 'slippage_bps'];
      var touchedFee = patch.paper && feeKeys.some(function (k) { return k in patch.paper; });
      if (touchedFee) {
        // 费率改了必须让总览重新取数，否则用户会以为"改了没反应"。
        // 历史成交按当时费率入账（账不能追溯改），所以这里刷新出来的是
        // 「累计手续费不变、按当前费率重算的对照值变了」。
        toast('费率已更新：新成交按新费率扣费，历史成交按当时费率入账', 'ok', 6000);
        loadDash();
      } else {
        toast('设置已保存', 'ok');
      }
    } else { $('set-msg').textContent = '保存失败'; toast('保存失败：' + r.message, 'err'); }
  });
}

function resetPaper() {
  var cap = parseFloat(prompt('输入模拟盘初始资金（元）：', String(getCfg('paper.initial_capital') || 200000)));
  if (!cap || cap <= 0) return;
  api('/api/paper/reset', { method: 'POST', body: { capital: cap } }).then(function (r) {
    toast('模拟盘已重置为 ' + fmtMoney(cap) + ' 元', 'ok');
    loadDash(); loadTrade();
  });
}

/* =========================================================================
   日志
   ========================================================================= */
function buildLogSources() {
  api('/api/logs', { params: { limit: 500 } }).then(function (r) {
    var set = {};
    (r.logs || []).forEach(function (l) { set[l.source] = 1; });
    var sel = $('log-source');
    sel.innerHTML = '<option value="">全部来源</option>' + Object.keys(set).sort().map(function (s) {
      return '<option value="' + esc(s) + '">' + esc(s) + '</option>';
    }).join('');
  });
}

function loadLogs() {
  api('/api/logs', { params: { limit: 300, level: $('log-level').value, source: $('log-source').value } }).then(function (r) {
    var box = $('log-list');
    var list = r.logs || [];
    if (!list.length) { box.innerHTML = '<div class="empty">没有日志</div>'; return; }
    box.innerHTML = list.map(function (l) {
      return '<div class="logline"><span class="ts">' + esc(l.ts) + '</span>' +
        '<span class="lv lv-' + esc(l.level) + '">' + esc(l.level) + '</span>' +
        '<span class="src">' + esc(l.source) + '</span>' +
        '<span class="msg">' + esc(l.message) + (l.detail ? '\n' + esc(String(l.detail).slice(0, 600)) : '') + '</span></div>';
    }).join('');
  });
}

/* =========================================================================
   引擎控制
   ========================================================================= */
function engineStart() {
  api('/api/engine/start', { method: 'POST', body: {} }).then(function (r) {
    toast(r.message || (r.ok ? '已启动' : '启动失败'), r.ok ? 'ok' : 'err', 6000);
    api('/api/bootstrap').then(function (b) { renderHeader(b); });
  });
}
function engineStop() {
  api('/api/engine/stop', { method: 'POST', body: {} }).then(function (r) {
    toast(r.message || '已停止', 'ok');
    api('/api/bootstrap').then(function (b) { renderHeader(b); });
  });
}
function panic() {
  var liquidate = confirm('急停将立刻停止引擎。\n\n点【确定】= 停止引擎并市价清掉所有持仓\n点【取消】= 只停止引擎，保留持仓\n\n要继续吗？');
  api('/api/engine/panic', { method: 'POST', body: { liquidate: liquidate } }).then(function (r) {
    var msg = r.stopped ? '引擎已停止' : '';
    if (liquidate) msg += '，清仓结果：' + JSON.stringify((r.liquidated || []).map(function (x) { return x.symbol + (x.ok ? '成功' : '失败'); }));
    toast(msg || '已急停', 'warn', 9000);
    loadTrade(); loadDash();
  });
}

/* =========================================================================
   事件绑定
   ========================================================================= */
on(document, 'DOMContentLoaded', function () {
  $$('.tabbtn').forEach(function (b) { on(b, 'click', function () { switchTab(b.getAttribute('data-tab')); }); });
  on($('btn-engine-start'), 'click', engineStart);
  on($('btn-engine-stop'), 'click', engineStop);
  on($('btn-panic'), 'click', panic);

  on($('mkt-search-btn'), 'click', doSearch);
  on($('mkt-search'), 'keydown', function (e) { if (e.key === 'Enter') doSearch(); });
  on($('mkt-watch-btn'), 'click', addWatch);
  $$('.per').forEach(function (b) {
    on(b, 'click', function () {
      $$('.per').forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active'); S.period = b.getAttribute('data-p'); loadChart();
    });
  });
  $$('.rk').forEach(function (b) {
    on(b, 'click', function () {
      $$('.rk').forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active'); S.rankType = b.getAttribute('data-r'); loadRank();
    });
  });
  ['mkt-ma', 'mkt-boll', 'mkt-sub1'].forEach(function (id) {
    on($(id), 'change', function () { if (S.chartData) drawKline($('mkt-chart'), S.chartData, null); });
  });
  on($('mkt-chart'), 'mousemove', function (e) {
    if (!S.chartData) return;
    var rect = this.getBoundingClientRect();
    var x = e.clientX - rect.left;
    var n = S.chartData.klines.length;
    var padL = 6, padR = 62;
    var bw = (rect.width - padL - padR) / n;
    var i = Math.floor((x - padL) / bw);
    if (i >= 0 && i < n) drawKline(this, S.chartData, i);
    else drawKline(this, S.chartData, null);
  });
  on($('mkt-chart'), 'mouseleave', function () { if (S.chartData) drawKline(this, S.chartData, null); });
  on($('mkt-buy'), 'click', function () { if (S.symbol) openOrderModal(S.symbol, 'buy', 0); else toast('先选一只股票', 'warn'); });
  on($('mkt-sell'), 'click', function () { if (S.symbol) openOrderModal(S.symbol, 'sell', 0); else toast('先选一只股票', 'warn'); });

  on($('st-template'), 'change', function () { renderStrategyParams($('st-template'), $('st-params')); });
  on($('st-save'), 'click', saveStrategy);
  on($('st-preview'), 'click', previewForm);
  on($('st-new'), 'click', function () {
    $('st-id').value = ''; $('st-name').value = ''; $('st-symbols').value = ''; $('st-mode').value = 'signal';
    S.strategyWeights = {};
    renderStrategyParams($('st-template'), $('st-params'));
    renderWeights();
  });
  on($('st-symbols'), 'input', renderWeights);
  on($('st-weights'), 'input', updateWeightSum);
  on($('st-wcalc'), 'click', calcOptimalWeights);
  on($('st-wequal'), 'click', equalWeights);
  on($('st-wclear'), 'click', clearWeights);

  on($('bt-template'), 'change', function () { renderStrategyParams($('bt-template'), $('bt-params')); });
  on($('bt-from-strategy'), 'change', loadBacktestFromStrategy);
  on($('bt-run'), 'click', runBacktest);
  on($('bt-wf'), 'click', runWalkForward);
  on($('bt-end'), 'focus', function () { if (!this.value) this.value = new Date().toISOString().slice(0, 10); });

  on($('od-submit'), 'click', function () {
    var body = {
      symbol: $('od-symbol').value.trim(), side: $('od-side').value,
      price: $('od-price').value === '' ? null : parseFloat($('od-price').value),
      qty: parseInt($('od-qty').value, 10) || 0,
      order_type: $('od-price').value === '' ? 'market' : 'limit',
      reason: '实盘交易页手动下单'
    };
    if (!body.symbol) { toast('请填写股票代码', 'warn'); return; }
    $('od-msg').innerHTML = '<span class="spin"></span>提交中…';
    api('/api/order', { method: 'POST', body: body }).then(function (r) {
      $('od-msg').textContent = (r.ok ? '✔ ' : '✘ ') + (r.message || '');
      toast((r.ok ? '委托成功：' : '下单失败：') + (r.message || ''), r.ok ? 'ok' : 'err', 7000);
      loadTrade();
    });
  });
  on($('od-max'), 'click', function () {
    api('/api/paper/max', { params: { symbol: $('od-symbol').value.trim() } }).then(function (r) {
      $('od-qty').value = r.max_buy || 0; $('od-msg').textContent = '最大可买 ' + (r.max_buy || 0) + ' 股';
    });
  });
  on($('od-25'), 'click', function () {
    api('/api/paper/max', { params: { symbol: $('od-symbol').value.trim() } }).then(function (r) {
      $('od-qty').value = Math.floor((r.max_buy || 0) / 4 / 100) * 100; $('od-msg').textContent = '已填入 1/4 仓位';
    });
  });
  on($('od-50'), 'click', function () {
    api('/api/paper/max', { params: { symbol: $('od-symbol').value.trim() } }).then(function (r) {
      $('od-qty').value = Math.floor((r.max_buy || 0) / 2 / 100) * 100; $('od-msg').textContent = '已填入半仓';
    });
  });

  on($('set-save'), 'click', saveSettings);
  on($('set-reset-paper'), 'click', resetPaper);
  on($('set-reset-all'), 'click', function () {
    if (!confirm('确定恢复全部默认设置吗？（策略和记录不会丢）')) return;
    api('/api/settings/reset', { method: 'POST', body: {} }).then(function (r) {
      S.cfg = r.config; renderSettings(); toast('已恢复默认设置', 'ok');
    });
  });

  on($('log-refresh'), 'click', loadLogs);
  on($('log-level'), 'change', loadLogs);
  on($('log-source'), 'change', loadLogs);
  on($('log-clear'), 'click', function () {
    api('/api/logs/clear', { method: 'POST', body: {} }).then(function () { loadLogs(); toast('日志已清空'); });
  });

  on($('modal-close'), 'click', closeModal);
  on($('modal'), 'click', function (e) { if (e.target === $('modal')) closeModal(); });

  on($('bt-end'), 'click', function () { if (!this.value) this.value = new Date().toISOString().slice(0, 10); });

  boot();
  setTimeout(function () { if (!$('bt-end').value) $('bt-end').value = new Date().toISOString().slice(0, 10); }, 800);
});

window.addEventListener('resize', function () {
  if (S.chartData) drawKline($('mkt-chart'), S.chartData, null);
});


/* =========================================================================
   因子实验室
   ========================================================================= */
S.factorList = [];
S.factorFamilies = [];
S.factorFilter = '';
S.scanTimer = null;

function drawBars(cv, labels, values, opts) {
  opts = opts || {};
  if (!cv || !values || !values.length) return;
  var g = setupCanvas(cv), ctx = g.ctx, W = g.w, H = g.h;
  var padL = opts.horizontal ? 64 : 34, padR = 12, padT = 12, padB = 22;
  var plotW = W - padL - padR, plotH = H - padT - padB;
  var lo = 0, hi = 0;
  values.forEach(function (v) { if (v < lo) lo = v; if (v > hi) hi = v; });
  if (hi === lo) { hi = lo + 1; }
  hi *= 1.12; lo *= 1.12;
  ctx.font = '11px Consolas,Monaco,monospace';

  function zeroY() { return padT + (hi - 0) / (hi - lo) * plotH; }
  if (opts.horizontal) {
    var bw = Math.min(26, plotH / values.length - 6);
    var zx = padL + (0 - lo) / (hi - lo) * plotW;
    ctx.strokeStyle = GRID;
    ctx.beginPath(); ctx.moveTo(Math.round(zx) + .5, padT); ctx.lineTo(Math.round(zx) + .5, padT + plotH); ctx.stroke();
    values.forEach(function (v, i) {
      var y = padT + i * (plotH / values.length) + (plotH / values.length - bw) / 2;
      var w2 = (v - 0) / (hi - lo) * plotW;
      ctx.fillStyle = v >= 0 ? UP : DOWN;
      ctx.fillRect(w2 >= 0 ? zx : zx + w2, y, Math.max(Math.abs(w2), 1), bw);
      ctx.fillStyle = AXIS; ctx.textAlign = 'right';
      ctx.fillText(labels[i], padL - 6, y + bw / 2 + 3.5);
      ctx.textAlign = 'left';
      ctx.fillStyle = v >= 0 ? UP : DOWN;
      ctx.fillText(v.toFixed(2) + '%', w2 >= 0 ? zx + w2 + 4 : zx + w2 - 46, y + bw / 2 + 3.5);
    });
  } else {
    var zy = zeroY();
    ctx.strokeStyle = GRID;
    ctx.beginPath(); ctx.moveTo(padL, Math.round(zy) + .5); ctx.lineTo(padL + plotW, Math.round(zy) + .5); ctx.stroke();
    var bw2 = plotW / values.length;
    values.forEach(function (v, i) {
      var h2 = (v - 0) / (hi - lo) * plotH;
      var x = padL + i * bw2 + bw2 * 0.15;
      ctx.fillStyle = v >= 0 ? UP : DOWN;
      ctx.fillRect(x, h2 >= 0 ? zy - h2 : zy, bw2 * 0.7, Math.max(Math.abs(h2), 1));
    });
    ctx.fillStyle = AXIS; ctx.textAlign = 'center';
    labels.forEach(function (lab, i) {
      if (values.length > 40 && i % Math.ceil(values.length / 8) !== 0) return;
      ctx.fillText(lab, padL + i * bw2 + bw2 / 2, H - 6);
    });
    ctx.textAlign = 'right';
    ctx.fillText(hi.toFixed(2), padL - 4, padT + 9);
    ctx.fillText(lo.toFixed(2), padL - 4, padT + plotH);
  }
}

function levelTag(level, verdict) {
  var map = { strong: 'on', medium: 'on', weak: 'off', none: 'off' };
  var color = { strong: '#3fb950', medium: '#d29922', weak: '#8b949e', none: '#6b7684' };
  return '<span class="tag ' + (map[level] || 'off') + '" style="color:' + (color[level] || '#9aa7b4') + '">' +
    esc(verdict || level) + '</span>';
}

function loadFactorTab() {
  if (S.factorList.length) { renderFactorCatalog(); }
  else {
    api('/api/factors').then(function (r) {
      S.factorList = r.factors || [];
      S.factorFamilies = r.families || [];
      S.defaultPool = r.default_pool || [];
      $('fc-count').textContent = r.count || S.factorList.length;
      var sel = $('fa-factor');
      sel.innerHTML = S.factorList.map(function (f) {
        return '<option value="' + esc(f.key) + '">' + esc(f.family) + ' · ' + esc(f.name) + '</option>';
      }).join('');
      renderFactorCatalog();
    });
  }
  // 恢复上次的扫描结果
  api('/api/factors/scan').then(function (s) {
    if (s.running) { startScanPoll(); }
    else if (s.result) { renderLeaderboard(s.result); $('fs-status').textContent = '上次扫描完成于 ' + (s.finished || ''); }
  });
}

function renderFactorCatalog() {
  var kw = ($('fc-search') && $('fc-search').value || '').trim();
  var fams = ['全部'].concat(S.factorFamilies);
  $('fc-families').innerHTML = fams.map(function (f) {
    var on = (f === '全部' && !S.factorFilter) || f === S.factorFilter;
    return '<span class="chip' + (on ? ' primary' : '') + '" onclick="setFactorFamily(\'' + esc(f) + '\')" ' +
      (on ? 'style="border-color:#3b82f6;color:#58a6ff"' : '') + '>' + esc(f) + '</span>';
  }).join('');
  var list = S.factorList.filter(function (f) {
    if (S.factorFilter && f.family !== S.factorFilter) return false;
    if (kw && (f.name + f.desc + f.key).indexOf(kw) < 0) return false;
    return true;
  });
  $('fc-list').innerHTML = list.map(function (f) {
    return '<div class="fcat"><div class="fn">' + esc(f.name) + '</div>' +
      '<div class="fk">' + esc(f.key) + ' · ' + esc(f.family) + '</div>' +
      '<div class="fd">' + esc(f.desc) + '</div></div>';
  }).join('') || '<div class="empty">没有匹配的因子</div>';
}

function setFactorFamily(f) {
  S.factorFilter = (f === '全部') ? '' : f;
  renderFactorCatalog();
}

function startFactorScan(force) {
  var pool = parseSymbols($('fs-pool').value);
  $('fs-status').innerHTML = '<span class="spin"></span>正在启动…';
  api('/api/factors/scan', {
    method: 'POST',
    body: { symbols: pool.length ? pool : null, days: parseInt($('fs-days').value, 10),
            horizon: parseInt($('fs-horizon').value, 10), force: !!force,
            neutralize: $('fs-neutral').checked ? 'industry' : 'none' }
  }).then(function (r) {
    if (!r.ok) { $('fs-status').textContent = r.message || '启动失败'; return; }
    if (r.cached) { renderLeaderboard(r.result); $('fs-status').textContent = '用的是 30 分钟内的缓存结果'; return; }
    $('fs-status').textContent = r.message || '扫描中…';
    startScanPoll();
  });
}

function startScanPoll() {
  if (S.scanTimer) clearInterval(S.scanTimer);
  S.scanTimer = setInterval(function () {
    api('/api/factors/scan').then(function (s) {
      $('fs-bar').style.width = (s.progress || 0) + '%';
      $('fs-status').innerHTML = '<span class="spin"></span>' + esc(s.stage || '') + ' ' + (s.progress || 0) + '%';
      if (!s.running) {
        clearInterval(S.scanTimer); S.scanTimer = null;
        if (s.error) { $('fs-status').textContent = '失败：' + s.error; toast('扫描失败：' + s.error, 'err'); }
        else if (s.result) {
          $('fs-status').textContent = '扫描完成 ' + (s.finished || '');
          renderLeaderboard(s.result);
          toast('扫描完成，最强因子：' + (s.result.top[0] ? s.result.top[0].name : ''), 'ok');
        }
      }
    });
  }, 2000);
}

function renderLeaderboard(res) {
  var rows = (res.top || []).concat((res.all || []).slice(25));
  var tb = $('fs-table').querySelector('tbody');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="10" class="empty">没有结果</td></tr>'; return; }
  $('fs-status').textContent = '生成于 ' + (res.generated_at || '') + '，股票池 ' +
    res.params.pool_size + ' 只，共分析 ' + res.params.factor_count + ' 个因子，持有期 ' + res.params.horizon + ' 天';
  tb.innerHTML = rows.map(function (r) {
    var ls = (r.long_short_pct === null || r.long_short_pct === undefined) ? '—' : sign(r.long_short_pct, 3) + '%';
    return '<tr>' +
      '<td>' + r.rank + '</td>' +
      '<td><b>' + esc(r.name) + '</b><br><span class="muted" style="font-size:11px">' + esc(r.key) + '</span></td>' +
      '<td class="muted">' + esc(r.family) + '</td>' +
      '<td class="' + cls(r.ic_mean) + '">' + sign(r.ic_mean, 4) + '</td>' +
      '<td class="' + cls(r.icir) + '">' + sign(r.icir, 3) + '</td>' +
      '<td class="' + cls(r.t_stat) + '">' + sign(r.t_stat, 2) + '</td>' +
      '<td>' + (r.ic_positive_ratio * 100).toFixed(0) + '%</td>' +
      '<td class="' + cls(r.long_short_pct) + '">' + ls + '</td>' +
      '<td>' + levelTag(r.level, r.verdict) + '</td>' +
      '<td><button class="btn tiny" onclick="analyzeFactor(\'' + esc(r.key) + '\')">详情</button> ' +
      '<button class="btn tiny" onclick="makeStrategyFromFactor(\'' + esc(r.key) + '\')">建策略</button></td>' +
      '</tr>';
  }).join('');
}

function analyzeFactor(key) {
  if (key) { $('fa-factor').value = key; switchTab('factor'); }
  key = $('fa-factor').value;
  if (!key) return;
  var pool = parseSymbols($('fa-pool').value);
  var meta = null;
  for (var i = 0; i < S.factorList.length; i++) if (S.factorList[i].key === key) meta = S.factorList[i];
  if (meta) { $('fa-desc').style.display = 'block'; $('fa-desc').innerHTML = '<b>' + esc(meta.name) + '</b>（' + esc(meta.family) + '）：' + esc(meta.desc); }
  $('fa-status').innerHTML = '<span class="spin"></span>计算中，股票多的话要等十几秒…';
  api('/api/factors/analyze', {
    method: 'POST',
    body: { factor: key, symbols: pool.length ? pool : null,
            days: parseInt($('fa-days').value, 10) || 400,
            horizon: parseInt($('fa-horizon').value, 10) || 5,
            neutralize: ($('fa-neutral') && $('fa-neutral').checked) ? 'industry' : 'none' }
  }).then(function (r) {
    if (!r.ok) { $('fa-status').textContent = '失败：' + (r.message || ''); return; }
    $('fa-status').textContent = '股票池 ' + r.symbol_count + ' 只 · ' + (r.note || '');
    renderFactorAnalysis(r);
  });
}

function renderFactorAnalysis(r) {
  $('fa-result').style.display = 'block';
  var cards = [
    ['IC 均值', sign(r.ic_mean, 4), r.ic_mean],
    ['ICIR', sign(r.icir, 3), r.icir],
    ['t 值', sign(r.t_stat, 2), r.t_stat],
    ['IC>0 占比', (r.ic_positive_ratio * 100).toFixed(1) + '%', null],
    ['多空收益', r.long_short_pct === null ? '—' : sign(r.long_short_pct, 3) + '%', r.long_short_pct],
    ['样本数', r.ic_count + ' 期', null],
    ['建议方向', r.suggest_direction > 0 ? '正向（值大→买）' : '反向（值小→买）', null],
    ['结论', r.verdict, r.level === 'strong' ? 1 : (r.level === 'none' ? -1 : 0)]
  ];
  $('fa-cards').innerHTML = cards.map(function (c) {
    return '<div class="card"><div class="k">' + c[0] + '</div><div class="v ' + cls(c[2]) + '" style="font-size:16px">' + c[1] + '</div></div>';
  }).join('');

  var ic = (r.ic_series || []).filter(function (x) { return x.date; });
  if (ic.length) {
    drawBars($('fa-icchart'), ic.map(function (x) { return x.date.slice(5); }), ic.map(function (x) { return x.ic; }), {});
  } else if (r.mode === 'time_series') {
    drawLines($('fa-icchart'), [{ data: [r.ic_mean], color: '#3b82f6', name: '时序IC均值' }], { legend: true });
  }
  var qs = r.quantiles || [];
  drawBars($('fa-quant'), qs.map(function (q) { return q.label; }),
    qs.map(function (q) { return q.mean_forward_return_pct; }), { horizontal: true });

  var tb = $('fa-latest').querySelector('tbody');
  var latest = r.latest || [];
  tb.innerHTML = latest.length ? latest.map(function (x, i) {
    return '<tr><td>' + (i + 1) + '</td>' +
      '<td><a href="javascript:void(0)" onclick="pickSymbol(\'' + esc(x.symbol) + '\')">' + esc(x.symbol.split('.')[0]) + '</a></td>' +
      '<td>' + esc(x.name) + '</td><td>' + fmtNum(x.value, 5) + '</td><td>' + fmtNum(x.close, 2) + '</td>' +
      '<td><button class="btn tiny" onclick="openOrderModal(\'' + esc(x.symbol) + '\',\'buy\',0)">下单</button></td></tr>';
  }).join('') : '<tr><td colspan="6" class="empty">没有数据</td></tr>';
}

function makeStrategyFromFactor(key) {
  var name = key;
  for (var i = 0; i < S.factorList.length; i++) if (S.factorList[i].key === key) name = S.factorList[i].name;
  switchTab('strategy');
  $('st-id').value = '';
  $('st-template').value = 'factor_ts';
  $('st-name').value = name + ' 择时策略';
  $('st-mode').value = 'signal';
  renderStrategyParams($('st-template'), $('st-params'), { factor: key });
  if (!$('st-symbols').value) $('st-symbols').value = '000001.SZ,600036.SH,000333.SZ';
  toast('已经帮你把【' + name + '】填进策略表单，改完点【保存策略】。建议先点【预览当前信号】看看。', 'ok', 8000);
}


/* =========================================================================
   AI 深度学习因子（机器自动挖因子）
   ========================================================================= */
function loadDlFactorTab() {
  // 恢复上次训练结果 / 状态
  api('/api/dlfactor/status').then(function (s) {
    if (s.running) {
      startDlPoll();
    } else if (s.result) {
      renderDlResult(s.result);
      $('dl-status').textContent = '上次训练完成于 ' + (s.finished || '');
    }
  });
}

function startDlTrain() {
  var pool = parseSymbols($('dl-pool').value);
  $('dl-status').innerHTML = '<span class="spin"></span>正在启动训练…';
  $('dl-result').style.display = 'none';
  api('/api/dlfactor/train', {
    method: 'POST',
    body: {
      task: $('dl-task').value,
      symbols: pool.length ? pool : null,
      days: parseInt($('dl-days').value, 10),
      horizon: parseInt($('dl-horizon').value, 10)
    }
  }).then(function (r) {
    if (!r.ok) { $('dl-status').textContent = r.message || '启动失败'; toast('训练启动失败：' + (r.message || ''), 'err'); return; }
    $('dl-status').textContent = r.message || '训练中…';
    startDlPoll();
  });
}

function startDlPoll() {
  if (S.dlTimer) clearInterval(S.dlTimer);
  S.dlTimer = setInterval(function () {
    api('/api/dlfactor/status').then(function (s) {
      $('dl-bar').style.width = (s.progress || 0) + '%';
      $('dl-status').innerHTML = '<span class="spin"></span>' + esc(s.stage || '') + ' ' + (s.progress || 0) + '%';
      if (!s.running) {
        clearInterval(S.dlTimer); S.dlTimer = null;
        if (s.error) { $('dl-status').textContent = '失败：' + s.error; toast('训练失败：' + s.error, 'err'); }
        else if (s.result) {
          $('dl-status').textContent = '训练完成 ' + (s.finished || '');
          renderDlResult(s.result);
          toast('深度学习因子训练完成！', 'ok');
        }
      }
    });
  }, 2000);
}

function renderDlResult(r) {
  $('dl-result').style.display = 'block';
  var ic = r.ic_val;
  var icTxt = (ic === null || ic === undefined) ? '—' : sign(ic, 4);
  var nSamp = r.n_samples || 0;
  var hidden = (r.hidden && r.hidden.length) ? r.hidden.join('-') : '—';
  $('dl-pill').textContent = '已训练：' + (r.factor_name || r.task || '');
  $('dl-cards').innerHTML =
    '<div class="card"><div class="k">样本外 IC</div><div class="v ' + cls(ic) + '">' + icTxt + '</div><div class="s">越高越有效（&gt;0.05 算不错）</div></div>' +
    '<div class="card"><div class="k">训练样本</div><div class="v">' + fmtNum(nSamp, 0) + '</div><div class="s">前70%训练 + 后30%样本外验证</div></div>' +
    '<div class="card"><div class="k">网络结构</div><div class="v">' + esc(String(hidden)) + '</div><div class="s">隐藏层神经元（自动搜索得出）</div></div>' +
    '<div class="card"><div class="k">预测持有期</div><div class="v">' + esc(String(r.horizon)) + ' 天</div><div class="s">未来 N 日</div></div>';
  $('dl-verdict').innerHTML = '<b>结论：</b>' + esc(r.verdict || '') +
    '（' + esc(r.note || '') + '）';
  // 模型打分结果表
  var tb = $('dl-table').querySelector('tbody');
  if (r.symbols && r.symbols.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">点上面的【用已训练模型打分】看当前排名</td></tr>';
  } else {
    tb.innerHTML = '<tr><td colspan="6" class="empty">没有数据</td></tr>';
  }
}

function dlPreview() {
  var task = $('dl-task').value;
  $('dl-status').innerHTML = '<span class="spin"></span>正在用模型打分…';
  api('/api/dlfactor/preview', { params: { task: task } }).then(function (r) {
    if (!r.ok) { $('dl-status').textContent = r.message || '打分失败'; toast(r.message || '打分失败', 'err'); return; }
    var rows = r.ranking || [];
    var tb = $('dl-table').querySelector('tbody');
    $('dl-result').style.display = 'block';
    if (!rows.length) { tb.innerHTML = '<tr><td colspan="6" class="empty">没有结果</td></tr>'; }
    else {
      tb.innerHTML = rows.map(function (x, i) {
        return '<tr><td>' + (i + 1) + '</td>' +
          '<td><a href="javascript:void(0)" onclick="pickSymbol(\'' + esc(x.symbol) + '\')">' + esc(x.symbol.split('.')[0]) + '</a></td>' +
          '<td>' + esc(x.name) + '</td>' +
          '<td>' + fmtNum(x.close, 2) + '</td>' +
          '<td class="' + (x.score >= 0.5 ? 'up' : 'muted') + '">' + fmtNum(x.score, 4) + '</td>' +
          '<td class="muted">' + esc(x.date) + '</td></tr>';
      }).join('');
    }
    $('dl-status').textContent = '打分完成，共 ' + rows.length + ' 只';
  });
}

// 因子相关性检测：找出冗余因子
function checkFactorCorrelation() {
  var factors = parseSymbols($('fc-factors').value);
  if (!factors.length) { toast('请填写要检查的因子', 'warn'); return; }
  var body = {
    factors: factors,
    symbols: parseSymbols($('fc-pool').value),
    days: parseInt($('fc-days').value, 10) || 400,
    threshold: parseFloat($('fc-threshold').value) || 0.7
  };
  $('fc-status').innerHTML = '<span class="spin"></span>正在计算两两相关性（要拉股票池数据，约 10~30 秒）…';
  api('/api/factors/correlation', { method: 'POST', body: body }).then(function (r) {
    if (!r.ok) { $('fc-status').textContent = '失败：' + (r.message || ''); toast('相关性检测失败：' + (r.message || ''), 'err'); return; }
    $('fc-status').textContent = '完成，共检查 ' + (r.factors || []).length + ' 个因子';
    $('fc-result').style.display = 'block';
    var adv = $('fc-advice');
    if ((r.advice || []).length) {
      adv.style.display = 'block';
      adv.innerHTML = '<b>发现冗余因子：</b><br>' + r.advice.map(function (a) { return '· ' + esc(a); }).join('<br>') +
        (r.keep && r.keep.length ? '<br><b>建议保留：</b>' + r.keep.map(function (k) { return '<span class="chip">' + esc(k) + '</span>'; }).join('') : '');
    } else {
      adv.style.display = 'none';
    }
    var tb = $('fc-table').querySelector('tbody');
    var pairs = r.redundant_pairs || [];
    tb.innerHTML = pairs.length ? pairs.map(function (p) {
      var s = Math.abs(p.corr);
      var tag = s >= 0.9 ? '几乎重复' : (s >= 0.7 ? '高度相关' : '相关');
      return '<tr><td>' + esc(p.name_a) + ' <span class="muted">' + esc(p.a) + '</span></td>' +
        '<td>' + esc(p.name_b) + ' <span class="muted">' + esc(p.b) + '</span></td>' +
        '<td class="' + cls(p.corr) + '">' + (p.corr >= 0 ? '+' : '') + p.corr.toFixed(3) + '</td>' +
        '<td class="muted">' + tag + '</td></tr>';
    }).join('') : '<tr><td colspan="4" class="empty">没有发现超过阈值的冗余因子，这套因子组合比较分散 ✓</td></tr>';
    toast('检测完成：发现 ' + pairs.length + ' 组冗余因子', pairs.length ? 'warn' : 'ok');
  });
}

on(document, 'DOMContentLoaded', function () {
  on($('fs-run'), 'click', function () { startFactorScan(false); });
  on($('fs-force'), 'click', function () { startFactorScan(true); });
  on($('fa-run'), 'click', function () { analyzeFactor(); });
  on($('fc-search'), 'input', renderFactorCatalog);
  on($('fa-mkstrat'), 'click', function () { makeStrategyFromFactor($('fa-factor').value); });
  on($('fa-factor'), 'change', function () {
    if ($('fa-result').style.display === 'block') analyzeFactor();
  });
  on($('fc-run'), 'click', function () { checkFactorCorrelation(); });
  on($('dl-run'), 'click', startDlTrain);
  on($('dl-preview'), 'click', dlPreview);
});


/* =========================================================================
   组合优化
   ========================================================================= */
S.optMethods = [];

function loadOptimizerTab() {
  if (!S.optMethods.length) {
    api('/api/optimizer/methods').then(function (r) {
      S.optMethods = r.methods || [];
      $('op-method').innerHTML = S.optMethods.map(function (m) {
        return '<option value="' + esc(m.key) + '"' + (m.key === 'risk_parity' ? ' selected' : '') + '>' +
          esc(m.name) + '</option>';
      }).join('');
      renderOptMethodDesc();
    });
  }
  if (!$('op-preset').options.length) {
    loadFactorTab();
    setTimeout(function () {
      var opts = ['<option value="preset_momentum" selected>中期动量组合</option>',
        '<option value="preset_reversal">短期反转组合</option>',
        '<option value="preset_lowvol">低波动稳健组合</option>',
        '<option value="preset_pullback">回调买入法组合</option>',
        '<option value="preset_alpha101">Alpha101 精选组合</option>',
        '<option value="preset_volume_price">量价背离组合</option>'];
      $('op-preset').innerHTML = opts.join('');
    }, 400);
  }
}

function renderOptMethodDesc() {
  var k = $('op-method').value;
  for (var i = 0; i < S.optMethods.length; i++) {
    if (S.optMethods[i].key === k) { $('op-status').textContent = S.optMethods[i].desc; return; }
  }
}

function runOptimizer() {
  var pool = parseSymbols($('op-symbols').value);
  $('op-status').innerHTML = '<span class="spin"></span>计算中（要拉行情 + 算协方差，稍等十几秒）…';
  $('op-run').disabled = true;
  api('/api/optimizer/run', {
    method: 'POST',
    body: { symbols: pool.length ? pool : null, method: $('op-method').value,
            preset: $('op-preset').value,
            total_pct: parseFloat($('op-total').value) || 90,
            max_single_pct: parseFloat($('op-single').value) || 15 }
  }).then(function (r) {
    $('op-run').disabled = false;
    if (!r.ok) { $('op-status').textContent = '失败：' + (r.message || ''); toast('优化失败：' + (r.message || ''), 'err'); return; }
    $('op-status').textContent = '完成：' + r.n + ' 只股票，总仓位 ' + r.total_pct + '%';
    renderOptimizer(r);
  });
}

function renderOptimizer(r) {
  $('op-result').style.display = 'block';
  var cards = [
    ['优化方法', (function () {
      for (var i = 0; i < S.optMethods.length; i++) if (S.optMethods[i].key === r.method) return S.optMethods[i].name;
      return r.method;
    })(), null],
    ['总仓位', r.total_pct + '%', null],
    ['最大单只', r.max_single_pct + '%', null],
    ['组合年化波动', r.annual_vol_pct + '%', null],
    ['股票数量', r.n + ' 只', null]
  ];
  $('op-cards').innerHTML = cards.map(function (c) {
    return '<div class="card"><div class="k">' + c[0] + '</div><div class="v" style="font-size:16px">' + c[1] + '</div></div>';
  }).join('');

  var note = $('op-note');
  if (r.note) { note.style.display = 'block'; note.textContent = r.note; }
  else note.style.display = 'none';

  var rows = r.rows || [];
  drawBars($('op-wchart'), rows.map(function (x) { return x.name.slice(0, 5); }),
    rows.map(function (x) { return x.weight_pct; }), { horizontal: true });
  var rc = rows.map(function (x) { return x.risk_contribution_pct || 0; });
  drawBars($('op-rchart'), rows.map(function (x) { return x.name.slice(0, 5); }), rc, { horizontal: true });

  var tb = $('op-table').querySelector('tbody');
  tb.innerHTML = rows.map(function (x) {
    return '<tr>' +
      '<td><a href="javascript:void(0)" onclick="pickSymbol(\'' + esc(x.symbol) + '\')">' + esc(x.symbol.split('.')[0]) + '</a></td>' +
      '<td>' + esc(x.name) + '</td>' +
      '<td class="muted">' + esc(x.industry || '-') + '</td>' +
      '<td>' + fmtNum(x.score, 3) + '</td>' +
      '<td><b>' + fmtNum(x.weight_pct, 2) + '%</b></td>' +
      '<td>' + fmtNum(x.risk_contribution_pct, 2) + '%</td>' +
      '<td>' + fmtMoney(x.weight_pct / 100 * 100000) + '</td>' +
      '</tr>';
  }).join('');
}

/* =========================================================================
   AI 选股助手
   ========================================================================= */
function mdLite(t) {
  var s = esc(t);
  s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  return s;
}

function loadAssistantTab() {
  api('/api/assistant/status').then(function (r) {
    var p = $('ai-mode');
    p.textContent = r.mode + (r.has_key ? '（' + (r.model || '') + '）' : '');
    p.className = 'pill ' + (r.has_key && r.enabled ? 'green' : '');
  });
  if (!$('ai-chat').childNodes.length) {
    aiBotMsg('我是这个平台内置的选股助手。所有数字都来自平台自己算出来的真实数据，我不编。\n\n' +
      '试试点上面的快捷按钮，或者直接输入问题。想看完整说明就输入「帮助」。\n\n' +
      '⚠️ 我给的只是统计结果，不是投资建议。买之前自己再看一眼，务必设止损。');
  }
}

function aiUserMsg(t) {
  var box = $('ai-chat');
  var d = document.createElement('div');
  d.className = 'msg user';
  d.innerHTML = '<div class="who">我</div><div class="bubble">' + mdLite(t) + '</div>';
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}

function aiBotMsg(t) {
  var box = $('ai-chat');
  var d = document.createElement('div');
  d.className = 'msg bot';
  d.innerHTML = '<div class="who">AI</div><div class="bubble">' + (t || '') + '</div>';
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d.querySelector('.bubble');
}

function aiClear() { $('ai-chat').innerHTML = ''; loadAssistantTab(); }

function aiAsk(q) {
  q = (q || '').trim();
  if (!q) return;
  $('ai-input').value = '';
  aiUserMsg(q);
  var bubble = aiBotMsg('<span class="spin"></span>正在计算…（第一次问「今天买什么」要几十秒，之后会很快）');
  $('ai-send').disabled = true;
  api('/api/assistant/ask', { method: 'POST', body: { question: q } }).then(function (r) {
    $('ai-send').disabled = false;
    if (!r.ok) { bubble.innerHTML = mdLite(r.answer || '出错了'); return; }
    var html = mdLite(r.answer || '');
    (r.blocks || []).forEach(function (b) {
      if (b.type === 'table' && b.rows && b.rows.length) {
        html += '<div class="bwrap"><table><thead><tr>' +
          (b.columns || []).map(function (c) { return '<th>' + esc(c) + '</th>'; }).join('') +
          '</tr></thead><tbody>' +
          b.rows.map(function (row) {
            return '<tr>' + row.map(function (c) {
              var s = String(c === null || c === undefined ? '' : c);
              var klass = '';
              if (/^[+-]\d/.test(s) && /%$/.test(s)) klass = ' class="' + cls(parseFloat(s)) + '"';
              return '<td' + klass + '>' + esc(s) + '</td>';
            }).join('') + '</tr>';
          }).join('') + '</tbody></table></div>';
      } else if (b.type === 'text' && b.text && b.text !== r.answer) {
        html += '\n\n' + mdLite(b.text);
      }
    });
    bubble.innerHTML = html;
    $('ai-chat').scrollTop = $('ai-chat').scrollHeight;
  });
}

on(document, 'DOMContentLoaded', function () {
  on($('fs-run'), 'click', function () { startFactorScan(false); });
  on($('fs-force'), 'click', function () { startFactorScan(true); });
  on($('fa-run'), 'click', function () { analyzeFactor(); });
  on($('fc-search'), 'input', renderFactorCatalog);
  on($('fa-mkstrat'), 'click', function () { makeStrategyFromFactor($('fa-factor').value); });
  on($('fa-factor'), 'change', function () {
    if ($('fa-result').style.display === 'block') analyzeFactor();
  });

  on($('op-run'), 'click', runOptimizer);
  on($('op-method'), 'change', renderOptMethodDesc);

  on($('pf-refresh'), 'click', loadPreflight);
  on($('rt-refresh'), 'click', loadRuntime);
  on($('sc-run'), 'click', runScreener);
  on($('fn-run'), 'click', runFundamentalQuery);
  on($('fn-clear'), 'click', clearFundamentalCache);
  on($('fn-sym'), 'keydown', function (e) { if (e.key === 'Enter') runFundamentalQuery(); });
  on($('sc-refresh'), 'click', function () {
    $('sc-status').innerHTML = '<span class="spin"></span>正在重新抓取全市场行情…';
    api('/api/screener/refresh', { method: 'POST', body: {} }).then(function (r) {
      $('sc-status').textContent = '';
      toast(r.message || '已刷新', 'ok', 6000);
      updateSnapPill(r.snapshot);
    });
  });
  on($('sc-advanced'), 'click', function () {
    var box = $('sc-weights');
    if (box.style.display === 'none') {
      box.style.display = '';
      S.screener.custom = {};
      renderCustomWeights();
    } else {
      box.style.display = 'none';
      S.screener.custom = null;
    }
  });
  on($('sc-tostrategy'), 'click', screenerToStrategy);
  on($('sc-towatch'), 'click', screenerToWatch);
  on($('sc-export'), 'click', screenerExport);
  on($('rt-tostrategy'), 'click', function () { switchTab('strategy'); });
  on($('tk-refresh'), 'click', loadTickets);
  on($('tk-send-all'), 'click', sendAllTickets);
  on($('tk-clear'), 'click', clearTicketsUI);
  on($('st-refresh'), 'click', loadStops);
  on($('st-all'), 'click', stopAllPositions);
  on($('rp-refresh'), 'click', loadDailyReport);
  on($('od-sync'), 'click', syncBrokerOrders);

  on($('ai-send'), 'click', function () { aiAsk($('ai-input').value); });
  on($('ai-input'), 'keydown', function (e) { if (e.key === 'Enter') aiAsk($('ai-input').value); });
});
