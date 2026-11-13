/* ============================================================
   panels.js - 面板管理与渲染（终端日志流风格）
   每个面板 = 一组日志条目（entry）：
     像素图标 | 标题行（NAME · TASK）→ 描述行 → 指标行
   指标行固定格式：大写指标名 · 数值（小写单位）
   面板统一接口：init() / render(data) / show() / hide() / destroy()
   新增面板：index.html 加 section + PanelClasses 注册 <名>Panel 类
   ============================================================ */

(function () {
  'use strict';

  var D = window.Dashboard = window.Dashboard || {};

  /* 共享工具函数（app.js 会覆盖部分实现，这里提供兜底） */
  var Utils = D.Utils || {};
  Utils.formatBytes = Utils.formatBytes || function (bytes) {
    if (bytes === null || bytes === undefined || isNaN(bytes)) return '--';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0, v = Number(bytes);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
  };
  Utils.formatSpeed = Utils.formatSpeed || function (bytesPerSec) {
    if (bytesPerSec === null || bytesPerSec === undefined || isNaN(bytesPerSec)) return '--';
    return Utils.formatBytes(bytesPerSec) + '/s';
  };
  /* Token 数值：3.4m tok / 12.3k tok（数值小写，指标名大写由模板控制） */
  Utils.formatTokens = function (n) {
    if (n === null || n === undefined || isNaN(n)) return '-- tok';
    n = Number(n);
    if (n < 1000) return n.toFixed(0) + ' tok';
    if (n < 1e6) return (n / 1e3).toFixed(1) + 'k tok';
    if (n < 1e9) return (n / 1e6).toFixed(1) + 'm tok';
    return (n / 1e9).toFixed(2) + 'b tok';
  };
  /* ASCII 进度条：[▓▓▓▓░░░░] */
  Utils.asciiBar = function (percent, width) {
    width = width || 14;
    var p = Math.max(0, Math.min(100, Number(percent) || 0));
    var filled = Math.round(p / 100 * width);
    var s = '[';
    for (var i = 0; i < width; i++) s += (i < filled ? '\u2593' : '\u2591');
    return s + ']';
  };
  Utils.clamp = Utils.clamp || function (v, min, max) {
    return Math.max(min, Math.min(max, v));
  };
  D.Utils = Utils;

  /** 从 HTML 字符串创建 DOM 元素 */
  function el(html) {
    var tpl = document.createElement('template');
    tpl.innerHTML = html.trim();
    return tpl.content.firstElementChild;
  }

  /** 简易事件总线：解耦 app.js 与各面板 */
  var bus = {
    _listeners: {},
    on: function (evt, fn) {
      (this._listeners[evt] = this._listeners[evt] || []).push(fn);
    },
    emit: function (evt, payload) {
      (this._listeners[evt] || []).forEach(function (fn) {
        try { fn(payload); } catch (e) { /* 单个监听器出错不影响其他 */ }
      });
    }
  };
  D.bus = bus;

  /* ============================================================
     面板基类
     ============================================================ */
  function BasePanel(name, bodyId) {
    this.name = name;
    this.body = document.getElementById(bodyId);
  }
  BasePanel.prototype.init = function () {};
  BasePanel.prototype.render = function (data) {};  /* data 为后端统一 JSON */
  BasePanel.prototype.show = function () {};
  BasePanel.prototype.hide = function () {};
  BasePanel.prototype.destroy = function () {};

  /* ============================================================
     日志条目构造器：像素图标 | 标题行 → 描述行 → 指标行
     返回带 metric()/bar()/clear() 的小控制器，指标每次 render 重建
     ============================================================ */
  function entry(iconVariant, iconColor, title, desc) {
    var root = el(
      '<div class="entry">' +
      '  <div class="entry-head">' +
      '    <span class="px ' + iconVariant + ' ' + iconColor + '"></span>' +
      '    <span class="t"></span>' +
      '  </div>' +
      '  <div class="entry-desc"></div>' +
      '  <div class="metrics"></div>' +
      '</div>');
    root.querySelector('.t').textContent = title;
    if (desc !== undefined) {
      root.querySelector('.entry-desc').textContent = desc;
    }

    var api = {
      root: root,
      head: root.querySelector('.t'),
      desc: root.querySelector('.entry-desc'),
      metrics: root.querySelector('.metrics'),

      setTitle: function (t) { this.head.textContent = t; return this; },
      setDesc: function (d) {
        if (this.desc) this.desc.textContent = (d === undefined || d === null) ? '' : d;
        return this;
      },
      /* 追加一行指标：大写指标名 · 数值
         vClass 语义色；wide=true 跨双列整行 */
      metric: function (k, v, vClass, wide) {
        var m = el('<div class="metric' + (wide ? ' wide' : '') + '"><span class="k"></span> · ' +
          '<span class="v"></span></div>');
        m.querySelector('.k').textContent = k;
        var vv = m.querySelector('.v');
        vv.textContent = v;
        if (vClass) vv.className = 'v ' + vClass;
        this.metrics.appendChild(m);
        return m;
      },
      /* 追加 ASCII 进度条：字符数按容器实际宽度动态计算，撑满整行 */
      bar: function (percent) {
        var line = el('<div class="bar-line"><span class="v"></span>' +
          '<span class="pct"></span></div>');
        this.metrics.appendChild(line);
        var vEl = line.querySelector('.v');
        var pctEl = line.querySelector('.pct');
        pctEl.textContent =
          (Math.max(0, Math.min(100, Number(percent) || 0))).toFixed(0) + '%';
        /* 等宽字体：先写单个字符测出字宽，再按剩余宽度算字符数 */
        vEl.textContent = '\u2593';
        var charW = vEl.offsetWidth || 7;
        var avail = this.metrics.clientWidth - pctEl.offsetWidth - 12;
        var width = (avail > 60 && charW > 0)
          ? Math.max(8, Math.min(80, Math.floor(avail / charW) - 2))
          : 14;   /* 布局尚未就绪时的兜底宽度 */
        vEl.textContent = Utils.asciiBar(percent, width);
        return line;
      },
      /* 在已存在的行上更新进度（用于避免整行重建的场合） */
      clear: function () { this.metrics.innerHTML = ''; return this; }
    };
    return api;
  }

  /* ============================================================
     系统监控面板：CPU / MEMORY / DISK / NETWORK / HOST 五条日志
     ============================================================ */
  function SystemPanel() {
    BasePanel.call(this, 'system', 'panel-system-body');
  }
  SystemPanel.prototype = Object.create(BasePanel.prototype);

  SystemPanel.prototype.init = function () {
    this.body.innerHTML = '';
    this.wrap = el('<div class="entries"></div>');
    this.body.appendChild(this.wrap);

    this.eCpu  = entry('px-a', 'px-cyan', 'CPU · LOAD');
    this.eMem  = entry('px-b', 'px-cyan', 'MEMORY · USAGE');
    this.eDisk = entry('px-c', 'px-cyan', 'DISK · PARTITIONS');
    this.eNet  = entry('px-a', 'px-blue', 'NETWORK · THROUGHPUT');
    this.eHost = entry('px-b', 'px-green', 'HOST');

    this.wrap.appendChild(this.eCpu.root);
    this.wrap.appendChild(this.eMem.root);
    this.wrap.appendChild(this.eDisk.root);
    this.wrap.appendChild(this.eNet.root);
    this.wrap.appendChild(this.eHost.root);
  };

  SystemPanel.prototype.render = function (data) {
    if (!data || !data.system) return;
    var s = data.system;

    /* CPU */
    this.eCpu.setDesc((s.cpu_count || 0) + ' threads · ' + (s.os || '--'));
    this.eCpu.clear();
    this.eCpu.metric('LOAD', (Number(s.cpu_percent) || 0).toFixed(1) + '%');
    this.eCpu.bar(s.cpu_percent);

    /* 内存 */
    this.eMem.setDesc(
      Utils.formatBytes(s.memory_used) + ' / ' + Utils.formatBytes(s.memory_total));
    this.eMem.clear();
    this.eMem.metric('USED', Utils.formatBytes(s.memory_used));
    this.eMem.metric('FREE', Utils.formatBytes(s.memory_available));
    this.eMem.metric('PERCENT', (Number(s.memory_percent) || 0).toFixed(1) + '%');
    this.eMem.bar(s.memory_percent);

    /* 磁盘分区列表：分区行信息量大，使用 wide 整行 + 整行进度条 */
    var parts = s.disk_partitions || [];
    this.eDisk.setDesc(parts.length + ' partitions · total ' +
      Utils.formatBytes(s.disk ? s.disk.total : 0));
    this.eDisk.clear();
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      var name = p.device || p.mountpoint || ('DISK ' + (i + 1));
      this.eDisk.metric(name,
        (Number(p.percent) || 0).toFixed(0) + '% · used ' +
        Utils.formatBytes(p.used) + ' / free ' + Utils.formatBytes(p.free),
        null, true);
      this.eDisk.bar(p.percent);
    }

    /* 网络：DOWN/UP 一行两列，RX/TX 一行两列 */
    var net = s.network || {};
    this.eNet.clear();
    this.eNet.metric('DOWN', Utils.formatBytes(net.recv_speed) + '/s');
    this.eNet.metric('UP', Utils.formatBytes(net.send_speed) + '/s');
    this.eNet.metric('RX TOTAL', Utils.formatBytes(net.bytes_recv), 'dim');
    this.eNet.metric('TX TOTAL', Utils.formatBytes(net.bytes_sent), 'dim');

    /* 主机 */
    this.eHost.setTitle('HOST · ' + (s.hostname || '--'));
    this.eHost.setDesc((s.os || '--') + ' · updated ' + (data.timestamp || '--'));
    this.eHost.clear();
    this.eHost.metric('SOURCE', data.source || 'backend',
      data.source === 'mock' ? 'purple' : 'green');
  };

  D.BasePanel = BasePanel;
  D._panelHelpers = { el: el, entry: entry };
  D.PanelClasses = D.PanelClasses || {};
  D.PanelClasses.SystemPanel = SystemPanel;
})();
/* ============================================================
   天气面板：预留显示区域（无数据显示占位文案）
   ============================================================ */
(function () {
  'use strict';
  var D = window.Dashboard;
  var entry = D._panelHelpers.entry;
  var Utils = D.Utils;

  function WeatherPanel() {
    D.BasePanel.call(this, 'weather', 'panel-weather-body');
  }
  WeatherPanel.prototype = Object.create(D.BasePanel.prototype);

  WeatherPanel.prototype.init = function () {
    this.body.innerHTML = '';
    this.wrap = D._panelHelpers.el('<div class="entries"></div>');
    this.body.appendChild(this.wrap);
    this.e = entry('px-b', 'px-green', 'WEATHER · FORECAST');
    this.wrap.appendChild(this.e.root);
  };

  /* 数值格式化：非法/缺失返回 '--' */
  function wxNum(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '--';
    return Number(v).toFixed(digits === undefined ? 1 : digits);
  }

  /* 风向角度 -> 八方位中文 */
  function wxCompass(deg) {
    if (deg === null || deg === undefined || isNaN(deg)) return '--';
    var dirs = ['北', '东北', '东', '东南', '南', '西南', '西', '西北'];
    return dirs[Math.round((Number(deg) % 360) / 45) % 8];
  }

  /* 紫外线强度分级 */
  function wxUvLevel(v) {
    if (v === null || v === undefined || isNaN(v)) return '';
    var n = Number(v);
    if (n < 3) return '弱';
    if (n < 6) return '中等';
    if (n < 8) return '强';
    if (n < 11) return '很强';
    return '极强';
  }

  WeatherPanel.prototype.render = function (data) {
    var w = data && data.extensions && data.extensions.weather;
    var hasData = w && w.enabled && w.temperature !== null && w.temperature !== undefined;
    if (hasData) {
      /* 标题/描述：城市 · 现象 · 昼夜 · 观测时间 */
      var dayText = (w.is_day === null || w.is_day === undefined)
        ? '' : (Number(w.is_day) ? '白天' : '夜间');
      var obs = (w.observed_at || '').substring(11, 16);
      this.e.setTitle('WEATHER · ' + (w.city || 'UNKNOWN'));
      this.e.setDesc([w.condition || '--', dayText, obs ? '观测 ' + obs : '']
        .filter(Boolean).join(' · '));

      /* 全部请求项按双列网格排布 */
      this.e.clear();
      this.e.metric('TEMP', wxNum(w.temperature) + '\u00b0c');
      this.e.metric('FEELS', wxNum(w.apparent_temperature) + '\u00b0c');
      this.e.metric('MAX', wxNum(w.temp_max) + '\u00b0c');
      this.e.metric('MIN', wxNum(w.temp_min) + '\u00b0c');
      this.e.metric('HUMIDITY', (w.humidity === null || w.humidity === undefined)
        ? '--' : w.humidity + '%');
      this.e.metric('CLOUD', (w.cloud_cover === null || w.cloud_cover === undefined)
        ? '--' : w.cloud_cover + '%');
      this.e.metric('WIND', wxNum(w.wind_speed) + ' km/h');
      this.e.metric('DIR', wxCompass(w.wind_direction) +
        ((w.wind_direction === null || w.wind_direction === undefined)
          ? '' : ' ' + Math.round(Number(w.wind_direction)) + '\u00b0'));
      this.e.metric('RAIN', wxNum(w.precipitation) + ' mm');
      this.e.metric('PRESSURE', wxNum(w.pressure) + ' hPa');
      var uv = wxNum(w.uv_index) +
        (wxUvLevel(w.uv_index) ? ' ' + wxUvLevel(w.uv_index) : '');
      this.e.metric('UV', uv);
      this.e.metric('SUNRISE', w.sunrise || '--');
      this.e.metric('SUNSET', w.sunset || '--');

      /* 位置元数据（整行） */
      var loc = '';
      if (w.latitude !== null && w.latitude !== undefined) {
        loc = wxNum(w.latitude, 2) + ', ' + wxNum(w.longitude, 2);
      }
      if (w.elevation !== null && w.elevation !== undefined) {
        loc += (loc ? ' · ' : '') + wxNum(w.elevation, 0) + 'm';
      }
      if (loc) this.e.metric('LOCATION', loc, 'dim', true);
    } else {
      this.e.setTitle('WEATHER · FORECAST');
      this.e.setDesc('等待天气数据接入 · weather extension disabled or no data');
      this.e.clear();
      this.e.metric('STATUS', 'no data', 'purple');
    }
  };

  D.PanelClasses.WeatherPanel = WeatherPanel;
})();

/* ============================================================
   Token 用量面板：预留显示区域
   ============================================================ */
(function () {
  'use strict';
  var D = window.Dashboard;
  var entry = D._panelHelpers.entry;
  var Utils = D.Utils;

  function TokenPanel() {
    D.BasePanel.call(this, 'token', 'panel-token-body');
    this._latest = null;
    /* 统计方式：session（按会话）/ model（按模型），持久化 */
    this.view = localStorage.getItem('dash.tokenView') === 'model'
      ? 'model' : 'session';
    /* 数据来源：all（合并）/ oc（OpenCode）/ wb（WorkBuddy），持久化 */
    var src = localStorage.getItem('dash.tokenSource');
    this.source = (src === 'oc' || src === 'wb') ? src : 'all';
  }
  TokenPanel.prototype = Object.create(D.BasePanel.prototype);

  function tokenCost(v) {
    return v ? ' · $' + Number(v).toFixed(3) : '';
  }

  /* 平台信用点（WorkBuddy 侧只有 credit，没有美元成本） */
  function tokenCredit(v) {
    if (!v) return '';
    var n = Number(v);
    return ' · ' + (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + ' cr';
  }

  var SOURCE_TAG = { opencode: '[OC] ', workbuddy: '[WB] ' };

  TokenPanel.prototype.init = function () {
    var self = this;
    this.body.innerHTML = '';
    this.wrap = D._panelHelpers.el('<div class="entries"></div>');
    this.body.appendChild(this.wrap);

    this.e = entry('px-a', 'px-cyan', 'TOKEN · ALL AGENTS');
    this.wrap.appendChild(this.e.root);

    /* 开关条：来源 [ALL][OC][WB] ＋ 视图 [SESSION][MODEL] */
    this.tog = D._panelHelpers.el('<div class="togbar"></div>');
    this.wrap.appendChild(this.tog);

    this.btnAll = D._panelHelpers.el('<button class="tswitch">[ALL]</button>');
    this.btnOc = D._panelHelpers.el('<button class="tswitch">[OC]</button>');
    this.btnWb = D._panelHelpers.el('<button class="tswitch">[WB]</button>');
    this.btnSession = D._panelHelpers.el('<button class="tswitch">[SESSION]</button>');
    this.btnModel = D._panelHelpers.el('<button class="tswitch">[MODEL]</button>');

    this.btnAll.addEventListener('click', function () { self._setSource('all'); });
    this.btnOc.addEventListener('click', function () { self._setSource('oc'); });
    this.btnWb.addEventListener('click', function () { self._setSource('wb'); });
    this.btnSession.addEventListener('click', function () { self._setView('session'); });
    this.btnModel.addEventListener('click', function () { self._setView('model'); });

    var sep = D._panelHelpers.el('<span class="togsep">·</span>');
    this.tog.appendChild(this.btnAll);
    this.tog.appendChild(this.btnOc);
    this.tog.appendChild(this.btnWb);
    this.tog.appendChild(sep);
    this.tog.appendChild(this.btnSession);
    this.tog.appendChild(this.btnModel);
    this._paintToggle();

    /* 明细列表（按会话 / 按模型） */
    this.eList = entry('px-c', 'px-blue', 'TOKEN · BY SESSION');
    this.wrap.appendChild(this.eList.root);
  };

  TokenPanel.prototype._setView = function (view) {
    this.view = view;
    localStorage.setItem('dash.tokenView', view);
    this._paintToggle();
    this._paint();
  };

  TokenPanel.prototype._setSource = function (source) {
    this.source = source;
    localStorage.setItem('dash.tokenSource', source);
    this._paintToggle();
    this._paint();
  };

  TokenPanel.prototype._paintToggle = function () {
    this.btnAll.className = 'tswitch' + (this.source === 'all' ? '' : ' off');
    this.btnOc.className = 'tswitch' + (this.source === 'oc' ? '' : ' off');
    this.btnWb.className = 'tswitch' + (this.source === 'wb' ? '' : ' off');
    this.btnSession.className = 'tswitch' + (this.view === 'session' ? '' : ' off');
    this.btnModel.className = 'tswitch' + (this.view === 'model' ? '' : ' off');
  };

  TokenPanel.prototype.render = function (data) {
    this._latest = data && data.extensions ? data.extensions : null;
    this._paint();
  };

  /* 按当前来源取对应 payload */
  TokenPanel.prototype._payload = function () {
    var ext = this._latest || {};
    if (this.source === 'oc') return ext.opencode_usage;
    if (this.source === 'wb') return ext.workbuddy_usage;
    return ext.token_usage_all || ext.opencode_usage;
  };

  TokenPanel.prototype._title = function () {
    if (this.source === 'oc') return 'TOKEN · LOCAL OPENCODE';
    if (this.source === 'wb') return 'TOKEN · WORKBUDDY';
    return 'TOKEN · ALL AGENTS';
  };

  TokenPanel.prototype._paint = function () {
    var ext = this._latest;
    var u = this._payload();

    /* 本地用量（优先）：opencode.db / WorkBuddy 日志 / 两者合并 */
    if (u && u.enabled && u.available) {
      var total = u.total || {};
      var today = u.today || {};
      var upd = (u.updated_at || '').substring(11, 19) || '--';
      var desc = u.sessions + ' sessions';
      if (this.source === 'all') {
        var avail = (u.sources || []).filter(function (s) { return s.available; }).length;
        desc = avail + ' sources · ' + desc;
      } else if (this.source === 'wb') {
        desc = (u.files || 0) + ' files · ' + desc;
      }
      this.e.setTitle(this._title());
      this.e.setDesc(desc + ' · 更新 ' + upd);
      this.e.clear();
      this.e.metric('TOTAL', Utils.formatTokens(total.all) + ' · ' + (total.calls || 0) + '次');
      this.e.metric('TODAY', '+' + Utils.formatTokens(today.all) + ' · ' + (today.calls || 0) + '次');
      this.e.metric('THIS WEEK', '+' + Utils.formatTokens((u.week || {}).all) + ' · ' + ((u.week || {}).calls || 0) + '次');
      this.e.metric('THIS MONTH', '+' + Utils.formatTokens((u.month || {}).all) + ' · ' + ((u.month || {}).calls || 0) + '次');
      this.e.metric('LAST MONTH', '+' + Utils.formatTokens((u.last_month || {}).all) + ' · ' + ((u.last_month || {}).calls || 0) + '次');
      this.e.metric('INPUT', Utils.formatTokens(total.input));
      this.e.metric('OUTPUT', Utils.formatTokens(total.output));
      /* 合并视图：把两路各自的总量摊开，一眼看出谁在烧 */
      if (this.source === 'all') {
        (u.sources || []).forEach(function (s) {
          if (!s.available) return;
          var label = s.source === 'workbuddy' ? 'WB' : 'OC';
          this.e.metric(label, Utils.formatTokens(s.total));
        }, this);
      }
      if (u.cost) {
        this.e.metric('COST', '$' + Number(u.cost).toFixed(3), 'green');
      }
      if (u.credit) {
        this.e.metric('CREDITS', tokenCredit(u.credit).replace(' · ', ''), 'purple');
      }

      var bySession = this.view === 'session';
      this.eList.setTitle(bySession ? 'TOKEN · BY SESSION' : 'TOKEN · BY MODEL');
      this.eList.clear();
      var rows = (bySession ? u.by_session : u.models) || [];
      if (!rows.length) {
        this.eList.metric('STATUS', 'no data', 'dim');
        return;
      }
      for (var i = 0; i < rows.length; i++) {
        var it = rows[i];
        var name;
        if (bySession) {
          name = it.title || (it.sid ? it.sid.substring(0, 12) : 'session');
          if (it.project) name = it.project + ' · ' + name;
        } else {
          name = it.model || '--';
        }
        /* 合并视图下打来源徽标，单来源视图不打 */
        name = (this.source === 'all' ? (SOURCE_TAG[it.source] || '') : '') + name;
        this.eList.metric(name,
          Utils.formatTokens(it.all) + tokenCost(it.cost) + tokenCredit(it.credit),
          bySession ? 'blue' : 'cyan', true);
      }
      return;
    }

    /* 本地用量不可用（如所选来源未启用 / 还没数据） */
    if (u && u.enabled && !u.available) {
      this.e.setTitle(this._title());
      this.e.setDesc('本地统计不可用 · ' + (u.reason || 'no data'));
      this.e.clear();
      this.e.metric('STATUS', 'no data', 'purple');
      this.eList.clear();
      this.eList.metric('STATUS', 'no data', 'dim');
      return;
    }

    /* 平台 token 配额（原有接入路径） */
    var t = ext && ext.token_usage;
    var hasData = t && t.enabled && t.used !== null && t.used !== undefined;
    if (hasData) {
      this.e.setTitle('TOKEN · ' + (t.provider || 'UNKNOWN'));
      this.e.setDesc('platform token quota');
      this.e.clear();
      this.e.metric('USED', Utils.formatTokens(t.used));
      this.e.metric('LIMIT', Utils.formatTokens(t.limit));
      this.e.metric('REMAINING', Utils.formatTokens(t.remaining));
      var p = Number(t.percent) || 0;
      this.e.bar(p);
    } else {
      this.e.setTitle(this._title());
      this.e.setDesc((u && u.enabled)
        ? ('本地统计不可用 · ' + (u.reason || 'no data'))
        : '该来源未启用 · 见 backend\\config.json 的 extensions 段');
      this.e.clear();
      this.e.metric('STATUS', 'no data', 'purple');
    }
  };

  D.PanelClasses.TokenPanel = TokenPanel;
})();

/* ============================================================
   Agents 状态面板：AI 代理实时工作状态（多来源 / 多会话独立行）
   数据源：data.extensions.agents_status（后端 agents_status.py）
     · source=opencode  ← opencode-status-sync 插件推送
     · source=workbuddy ← workbuddy_watch.py 观察会话 .jsonl 推断
   每会话一行，带来源徽标，动态增删；像素块按状态以不同节奏闪烁；
   行内时长需要秒级刷新：show()/hide() 启停本地定时器
   ============================================================ */
(function () {
  'use strict';
  var D = window.Dashboard;
  var entry = D._panelHelpers.entry;
  var el = D._panelHelpers.el;

  /* 状态 -> 中文文案 / 像素块颜色 / 像素块闪烁动画 / 文字语义色 / 闪烁周期
     闪烁节奏一态一味：思考=呼吸 读取=扫描 写入=打字 出错=告警
     blinkMs 须与 style.css 中对应动画的 duration 保持一致 */
  var STATE_META = {
    thinking: { label: '思考中', color: 'px-cyan', blink: 'px-blink-think', vClass: 'cyan', blinkMs: 1400 },
    reading:  { label: '读取中', color: 'px-blue', blink: 'px-blink-read', vClass: 'blue', blinkMs: 1300 },
    writing:  { label: '写入中', color: 'px-purple', blink: 'px-blink-write', vClass: 'purple', blinkMs: 550 },
    working:  { label: '执行中', color: 'px-green', blink: '', vClass: 'green', blinkMs: 1200 },
    idle:     { label: '空闲', color: 'ag-px-idle', blink: '', vClass: 'dim', blinkMs: 1200 },
    error:    { label: '出错', color: 'px-purple', blink: 'px-blink-error', vClass: 'purple', blinkMs: 600 }
  };

  /* 来源 -> 行首徽标文字（未知来源取前两位大写） */
  var SOURCE_BADGE = { opencode: 'OC', workbuddy: 'WB' };

  function AgentsPanel() {
    D.BasePanel.call(this, 'agents', 'panel-agents-body');
    this._timer = null;
    this._latest = null;   /* 最近一次 render 的 agents_status 数据 */
    this._rows = Object.create(null);  /* sid -> 行控制器，动态增删复用 */
    this._order = [];      /* 当前行顺序（避免无谓重排重启 CSS 动画） */
    this._prevStates = Object.create(null);  /* sid -> 上次快照的状态 */
    this._idleSince = Object.create(null);   /* sid -> 观察到进入空闲的本地时间 */
    this._flashTimers = Object.create(null); /* sid -> 闪烁结束定时器 */
    this._empty = true;    /* 概要条目是否处于"无数据占位"态 */
    this._lastActive = null; /* 上次广播的会话活跃状态（null=未知） */
    /* 像素块单块点名闪烁：每行独立循环，行内一次只闪一个小块 */
    this._blockTimers = Object.create(null); /* sid -> 单块循环定时器 */
    this._rowInfo = Object.create(null);     /* sid -> {state, stale, flying[]} */
  }
  AgentsPanel.prototype = Object.create(D.BasePanel.prototype);

  /** "2026-09-07T10:00:00" -> Date（非法返回 null） */
  function parseStamp(s) {
    if (!s) return null;
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  /** ISO 时间 -> "HH:MM:SS" */
  function formatClock(iso) {
    return (typeof iso === 'string' && iso.length >= 19)
      ? iso.substring(11, 19) : '--:--:--';
  }

  /** 毫秒 -> "45s" / "5m" / "1h 03m"（超过一分钟不再显示秒数） */
  function formatDuration(ms) {
    if (ms === null || ms === undefined || ms < 0) return '--';
    var sec = Math.floor(ms / 1000);
    if (sec < 60) return sec + 's';
    var min = Math.floor(sec / 60);
    if (min < 60) return min + 'm';
    var hour = Math.floor(min / 60);
    var rest = min % 60;
    return hour + 'h ' + (rest < 10 ? '0' : '') + rest + 'm';
  }

  AgentsPanel.prototype.init = function () {
    this.body.innerHTML = '';
    /* 概要条目（ag-head 标记：屏保调暗时排除自身，行级亮度自行控制），
       会话行直接放入其 .metrics（跨双列、动态增删复用）；
       事件日志独立成组（屏保时随内容压暗） */
    this.headWrap = el('<div class="entries ag-head"></div>');
    this.body.appendChild(this.headWrap);
    this.e = entry('px-a', 'px-cyan', 'AGENTS · SESSIONS');
    this.headWrap.appendChild(this.e.root);

    this.logWrap = el('<div class="entries"></div>');
    this.body.appendChild(this.logWrap);
    this.eLog = entry('px-c', 'px-blue', 'EVENT LOG · RECENT');
    this.logWrap.appendChild(this.eLog.root);
  };

  AgentsPanel.prototype.render = function (data) {
    var ext = data && data.extensions;
    /* 新键优先；opencode_status 是后端双写的兼容别名 */
    var o = ext && (ext.agents_status || ext.opencode_status);
    this._latest = o || null;
    this._paint();
  };

  /* 行键：与后端主键一致（source:sid）。sid 只在来源内唯一，
     不同来源可能各有自己的 ID 空间，因此不能拿裸 sid 当行键。 */
  function sessKey(s) {
    return (s.source || 'opencode') + ':' + (s.sid || '');
  }

  /* 会话行显示名：项目 · 对话名（缺省回退 sid 前 8 位） */
  function sessionName(s) {
    if (s.project && s.title) return s.project + ' · ' + s.title;
    return s.title || s.project ||
      (s.sid ? s.sid.substring(0, 8) : 'session');
  }

  /* 会话行图标的 5 个小块位置（复刻 .px-a 十字形，容器 20×20） */
  var PXM_POS = [[7, 0], [0, 7], [7, 7], [14, 7], [7, 14]];
  var PXM_BREATHE_MS = 1600;   /* 呼吸缩放周期（与 style.css pxm-scale 时长保持一致） */

  /** 创建一行（多块像素图标 + 名称 + 状态/时长）；
      每个小块 = 外层 .pxm-b（定位 + 呼吸缩放）套内层 .pxm-bi（显示 + 闪烁透明度），
      两层动画分别作用于 transform/opacity，互不覆盖；
      外层挂随机负延迟，等时长但相位错开 */
  function buildRow() {
    var root = el(
      '<div class="ag-row">' +
      '  <span class="pxm"></span>' +
      '  <span class="ag-src"></span>' +
      '  <span class="ag-name"></span>' +
      '  <span class="ag-state"></span>' +
      '</div>');
    var pxEl = root.querySelector('.pxm');
    var blocks = [];
    for (var i = 0; i < PXM_POS.length; i++) {
      var w = document.createElement('i');
      w.className = 'pxm-b';
      w.style.left = PXM_POS[i][0] + 'px';
      w.style.top = PXM_POS[i][1] + 'px';
      w.style.animationDelay =
        '-' + (Math.random() * PXM_BREATHE_MS / 1000).toFixed(2) + 's';
      var inner = document.createElement('i');
      inner.className = 'pxm-bi';
      w.appendChild(inner);
      pxEl.appendChild(w);
      blocks.push(inner);
    }
    return {
      root: root,
      pxEl: pxEl,
      blocks: blocks,
      src: root.querySelector('.ag-src'),
      name: root.querySelector('.ag-name'),
      state: root.querySelector('.ag-state')
    };
  }

  /* 状态跨界判定：空闲离开 / 进入空闲或错误 → 触发 5s 闪烁 */
  AgentsPanel.prototype._isFlashTransition = function (prev, next) {
    if (prev === 'idle' && next !== 'idle') return true;
    if (prev !== 'idle' && (next === 'idle' || next === 'error')) return true;
    return false;
  };

  /* 行闪烁 5s：挂 ag-flash 类（CSS 一次性动画 5×1s），到点摘类 */
  AgentsPanel.prototype._startFlash = function (sid) {
    var row = this._rows[sid];
    if (!row) return;
    row.root.classList.remove('ag-flash');
    void row.root.offsetWidth;   /* 强制 reflow，保证动画可重启动 */
    row.root.classList.add('ag-flash');
    var self = this;
    if (this._flashTimers[sid]) clearTimeout(this._flashTimers[sid]);
    this._flashTimers[sid] = setTimeout(function () {
      row.root.classList.remove('ag-flash');
      delete self._flashTimers[sid];
    }, 5100);
  };

  AgentsPanel.prototype._clearFlashTimers = function () {
    var self = this;
    Object.keys(this._flashTimers).forEach(function (sid) {
      clearTimeout(self._flashTimers[sid]);
      delete self._flashTimers[sid];
    });
  };

  /* ---- 单块追逐闪烁：每行独立循环，节拍 = 周期的一半 ----
     每拍随机挑一个"未在动画中"的小块点亮（暗→亮→暗）；
     上一块走到正好一半（峰值）时下一块起步，同屏最多两块亮着；
     到期的块摘类回到暗态待命；常亮/过期状态不闪（自动停） */
  AgentsPanel.prototype._tickBlockBlink = function (sid) {
    this._blockTimers[sid] = null;
    var row = this._rows[sid];
    var info = this._rowInfo[sid];
    if (!row || !info || info.stale) { this._stopRowBlink(sid, info); return; }
    var meta = STATE_META[info.state];
    if (!meta || !meta.blink) { this._stopRowBlink(sid, info); return; }

    var now = Date.now();
    var flying = info.flying || (info.flying = []);
    var i, j;

    /* 清理到期块：动画已走完（暗→亮→暗结束），摘类回到暗态待命 */
    for (i = flying.length - 1; i >= 0; i--) {
      if (now >= flying[i].endAt) {
        flying[i].el.classList.remove(flying[i].cls);
        flying.splice(i, 1);
      }
    }

    /* 挑一个当前未在动画中的小块随机点亮 */
    var cands = [];
    for (i = 0; i < row.blocks.length; i++) {
      var busy = false;
      for (j = 0; j < flying.length; j++) {
        if (flying[j].el === row.blocks[i]) { busy = true; break; }
      }
      if (!busy) cands.push(row.blocks[i]);
    }
    if (cands.length) {
      var blk = cands[Math.floor(Math.random() * cands.length)];
      blk.classList.add(meta.blink);
      flying.push({ el: blk, cls: meta.blink, endAt: now + meta.blinkMs });
    }

    /* 下一拍 = 半个周期：上一块正好走到峰值时下一块起步 */
    var self = this;
    this._blockTimers[sid] = setTimeout(function () {
      self._tickBlockBlink(sid);
    }, (meta.blinkMs || 1200) / 2);
  };

  /* 摘除该行全部在飞动画类并停止循环（状态转入常亮/过期/行消失/隐藏时调用） */
  AgentsPanel.prototype._stopRowBlink = function (sid, info) {
    if (info && info.flying) {
      for (var i = 0; i < info.flying.length; i++) {
        info.flying[i].el.classList.remove(info.flying[i].cls);
      }
      info.flying = [];
    }
    if (this._blockTimers[sid]) {
      clearTimeout(this._blockTimers[sid]);
      delete this._blockTimers[sid];
    }
  };

  /* 展示顺序：森林展开——子会话紧跟父会话之后出现（缩进一级）。
     保持传入数组的相对新旧顺序（根与兄弟节点均按原序）；
     parent 缺失/为空/不在列表/自指 → 作为根；环由 visited 防护（环内节点按根发射）。
     索引一律用复合键 source:sid；parent 只在同来源内匹配（父会话必与子同源）。
     返回 { list: [会话对象（展示顺序）], depth: key -> 层级 } */
  function flattenSessions(sessions) {
    var present = Object.create(null);
    var kids = Object.create(null);   /* 父 key -> 子会话数组（保持原相对顺序） */
    var i, s, p, pk;
    for (i = 0; i < sessions.length; i++) present[sessKey(sessions[i])] = true;
    for (i = 0; i < sessions.length; i++) {
      s = sessions[i];
      p = (typeof s.parent === 'string') ? s.parent : '';
      pk = p ? ((s.source || 'opencode') + ':' + p) : '';
      if (p && p !== s.sid && present[pk]) {
        if (!kids[pk]) kids[pk] = [];
        kids[pk].push(s);
      }
    }
    var list = [];
    var depth = Object.create(null);
    var visited = Object.create(null);
    function emit(s, d) {
      var k = sessKey(s);
      if (visited[k]) return;          /* 环/重复防护：已发射的不再进入 */
      visited[k] = true;
      depth[k] = d;
      list.push(s);
      var ch = kids[k];
      if (ch) {
        for (var j = 0; j < ch.length; j++) emit(ch[j], d + 1);  /* 深度不限 */
      }
    }
    for (i = 0; i < sessions.length; i++) {   /* 先发射所有真根及其子树 */
      s = sessions[i];
      p = (typeof s.parent === 'string') ? s.parent : '';
      pk = p ? ((s.source || 'opencode') + ':' + p) : '';
      if (!p || !present[pk] || p === s.sid) emit(s, 0);
    }
    for (i = 0; i < sessions.length; i++) {   /* 兜底：环内未达节点 → 作为根 */
      emit(sessions[i], 0);
    }
    return { list: list, depth: depth };
  }

  /* 同步会话行（位于概要条目的 .metrics 内）：新会话创建、消失会话移除、
     其余原位更新；顺序变化才重排（appendChild 移动节点会重启 CSS 动画）。
     同时维护：状态转换检测（闪烁/空闲时间戳）与屏保亮度豁免标记 */
  AgentsPanel.prototype._syncRows = function (sessions) {
    var self = this;
    var box = this.e.metrics;
    var seen = Object.create(null);
    var flat = flattenSessions(sessions);
    var display = flat.list;   /* 展示顺序（旧负载无 parent 时 = 原数组序） */
    var orderChanged = this._order.length !== display.length;
    var now = Date.now();
    var i, s, row;

    for (i = 0; i < display.length; i++) {
      s = display[i];
      var key = sessKey(s);
      seen[key] = true;
      if (!orderChanged && this._order[i] !== key) orderChanged = true;

      /* 状态转换检测：与上次快照比较（本地时间，免时钟偏差） */
      var prev = this._prevStates[key];
      if (prev && prev !== s.state) {
        if (s.state === 'idle') {
          this._idleSince[key] = now;             /* 记录进入空闲时刻 */
        } else if (prev === 'idle') {
          delete this._idleSince[key];            /* 离开空闲 */
        }
        if (this._isFlashTransition(prev, s.state)) {
          this._startFlash(key);
        }
      }

      row = this._rows[key];
      if (!row) {
        row = buildRow();
        this._rows[key] = row;
        box.appendChild(row.root);
      }

      /* 层级缩进：子会话右移一级；深度封顶 3（更深的行共用 lvl-3 缩进） */
      var lvl = Math.min(flat.depth[key] || 0, 3);
      var lvlCls = lvl > 0 ? 'lvl-' + lvl : '';
      if ((row.lvlCls || '') !== lvlCls) {
        if (row.lvlCls) row.root.classList.remove(row.lvlCls);
        if (lvlCls) row.root.classList.add(lvlCls);
        row.lvlCls = lvlCls;
      }

      var meta = STATE_META[s.state] ||
        { label: s.state || '--', color: '', blink: '', vClass: 'dim', blinkMs: 1200 };
      /* 非空闲行挂呼吸缩放（等时长随机相位）；idle/过期行为实心块 */
      row.pxEl.className = 'pxm ' + meta.color +
        (s.state !== 'idle' && !s.stale ? ' pxm-breathe' : '');
      /* 保留块级闪烁的运行时跟踪字段（flying 在飞块列表） */
      var prevInfo = this._rowInfo[key] || {};
      this._rowInfo[key] = {
        state: s.state, stale: !!s.stale,
        flying: prevInfo.flying || []
      };
      row.name.textContent = sessionName(s);
      /* 来源徽标：OC=OpenCode 插件推送，WB=WorkBuddy 会话观察，其它取前两位 */
      var srcKey = s.source || 'opencode';
      var srcTxt = SOURCE_BADGE[srcKey] ||
        srcKey.substring(0, 2).toUpperCase();
      if (row.src.textContent !== srcTxt) row.src.textContent = srcTxt;
      var srcCls = 'ag-src src-' + srcKey;
      if (row.src.className !== srcCls) row.src.className = srcCls;

      var dur = '';
      if (s.state_since) {
        var since = parseStamp(s.state_since);
        if (since) dur = ' · ' + formatDuration(now - since.getTime());
      }
      row.state.textContent = meta.label + (s.stale ? ' ?' : '') + dur;
      row.state.className = 'ag-state ' + (s.stale ? 'dim' : meta.vClass);
      if (s.stale) row.root.classList.add('stale');
      else row.root.classList.remove('stale');

      /* 屏保亮度豁免：非空闲，或刚进入空闲 ≤10s */
      var keep;
      if (s.state !== 'idle') {
        keep = true;
      } else {
        var idleAt = this._idleSince[key];
        keep = !!(idleAt && (now - idleAt) < 10000);
      }
      if (keep) row.root.classList.add('keep-bright');
      else row.root.classList.remove('keep-bright');

      this._prevStates[key] = s.state;
    }

    /* 移除已消失的会话行及其跟踪状态 */
    Object.keys(this._rows).forEach(function (key) {
      if (!seen[key]) {
        box.removeChild(self._rows[key].root);
        delete self._rows[key];
        delete self._prevStates[key];
        delete self._idleSince[key];
        delete self._rowInfo[key];
        if (self._flashTimers[key]) {
          clearTimeout(self._flashTimers[key]);
          delete self._flashTimers[key];
        }
      }
    });

    /* 仅在顺序变化时重排（按展示顺序），避免每秒重启闪烁动画 */
    if (orderChanged) {
      for (i = 0; i < display.length; i++) {
        row = this._rows[sessKey(display[i])];
        if (row) box.appendChild(row.root);
      }
    }
    this._order = display.map(sessKey);

    /* 单块点名闪烁：为每个可闪行启动独立循环（已在跑的跳过；
       常亮/过期行会自停，状态转回可闪时由这里重启） */
    for (i = 0; i < sessions.length; i++) {
      var bk = sessKey(sessions[i]);
      if (!this._blockTimers[bk] && !sessions[i].stale) {
        this._tickBlockBlink(bk);
      }
    }
  };

  AgentsPanel.prototype._clearRows = function () {
    this.e.metrics.innerHTML = '';
    this._rows = Object.create(null);
    this._order = [];
    this._prevStates = Object.create(null);
    this._idleSince = Object.create(null);
    this._rowInfo = Object.create(null);
    var self = this;
    Object.keys(this._blockTimers).forEach(function (sid) {
      clearTimeout(self._blockTimers[sid]);
      delete self._blockTimers[sid];
    });
    this._clearFlashTimers();
  };

  /* 重绘（render 与秒级定时器共用）
     数据结构（多来源 / 多会话版）：
       { state/state_since/last_event_at/stale,        <- 最近活跃会话镜像
         sessions: [{sid,source,title,project,state,state_since,
                     last_event_at,stale,parent}],
         events:   [{state,at,source,sid,title,project}],
         sources:  {opencode:{sessions,active}, workbuddy:{...}} } */
  AgentsPanel.prototype._paint = function () {
    var o = this._latest;
    var sessions = (o && o.sessions) || [];
    /* 兼容旧版后端：无 sessions[] 时用顶层字段合成单会话行 */
    if (!sessions.length && o && o.enabled && o.state) {
      sessions = [{ sid: '', source: 'opencode', title: null, project: null,
                    state: o.state,
                    state_since: o.state_since, last_event_at: o.last_event_at,
                    stale: !!o.stale }];
    }
    var hasData = !!(o && o.enabled && sessions.length);

    /* 广播活跃状态：存在 非 idle 且未过期 的会话 → 抑制二级熄屏。
       仅在状态翻转时发射（app.js ScreenSaver 据此抑制/重新武装熄屏） */
    var anyActive = false;
    for (var na = 0; na < sessions.length; na++) {
      var ns = sessions[na];
      if (ns.state && ns.state !== 'idle' && !ns.stale) { anyActive = true; break; }
    }
    if (anyActive !== this._lastActive) {
      this._lastActive = anyActive;
      D.bus.emit('agents-active', anyActive);
    }

    if (!hasData) {
      this.e.setTitle('AGENTS · SESSIONS');
      this.e.setDesc('等待 Agents 数据接入 · agents_status extension disabled or no data');
      this.e.clear();            /* 无数据态可整清（行不存在，无动画可重启） */
      this._clearRows();
      this._empty = true;
      this.e.metric('STATUS', 'no data', 'purple');
      this.eLog.clear();
      this.eLog.metric('STATUS', 'no events', 'dim');
      return;
    }

    /* 数据态：仅在 无数据→有数据 切换时清一次占位；
       之后严禁 clear 概要条目——会话行含 CSS 动画，每秒重建会重启动画 */
    if (this._empty) {
      this.e.clear();
      this._clearRows();
      this._empty = false;
    }

    /* 概要行：会话数 / 各来源分布 / 超时数 */
    var staleCount = 0;
    var bySource = Object.create(null);
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].stale) staleCount++;
      var sk = sessions[i].source || 'opencode';
      bySource[sk] = (bySource[sk] || 0) + 1;
    }
    var srcParts = [];
    for (var sk2 in bySource) {
      if (!Object.prototype.hasOwnProperty.call(bySource, sk2)) continue;
      srcParts.push((SOURCE_BADGE[sk2] || sk2.substring(0, 2).toUpperCase()) +
        ' ' + bySource[sk2]);
    }
    this.e.setTitle('AGENTS · SESSIONS');
    this.e.setDesc(sessions.length + ' sessions' +
      (srcParts.length ? ' · ' + srcParts.join(' · ') : '') +
      ' · 最近活跃在前' +
      (staleCount ? ' · ' + staleCount + ' 已超时' : ''));

    this._syncRows(sessions);

    /* 事件历史（最新在前，终端日志流样式；带项目归属，只显示最近三条） */
    this.eLog.clear();
    var evs = (o && o.events) || [];
    if (!evs.length) {
      this.eLog.metric('STATUS', 'no events yet', 'dim');
    } else {
      var shown = evs.slice(0, 3);
      for (var j = 0; j < shown.length; j++) {
        var ev = shown[j];
        var em = STATE_META[ev.state] ||
          { label: ev.state || '--', vClass: 'dim' };
        var text = ev.project ? ev.project + ' · ' + em.label : em.label;
        this.eLog.metric(formatClock(ev.at), text, em.vClass, true);
      }
    }
  };

  /* 面板处于当前屏时启动秒级定时器，驱动 DURATION 刷新 */
  AgentsPanel.prototype.show = function () {
    var self = this;
    this._paint();
    if (this._timer) return;
    this._timer = setInterval(function () {
      if (document.hidden) return;
      self._paint();
    }, 1000);
  };

  AgentsPanel.prototype.hide = function () {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
    /* 停掉全部单块闪烁循环并摘除残留动画类（show 后由 _syncRows 重启） */
    var self = this;
    Object.keys(this._blockTimers).forEach(function (sid) {
      self._stopRowBlink(sid, self._rowInfo[sid]);
    });
  };

  AgentsPanel.prototype.destroy = function () {
    this.hide();
    this._clearFlashTimers();
  };

  D.PanelClasses.AgentsPanel = AgentsPanel;
})();

/* ============================================================
   扩展面板：已注册扩展列表 + 未来功能占位
   ============================================================ */
(function () {
  'use strict';
  var D = window.Dashboard;
  var entry = D._panelHelpers.entry;
  var el = D._panelHelpers.el;

  function ExtensionsPanel() {
    D.BasePanel.call(this, 'extensions', 'panel-extensions-body');
  }
  ExtensionsPanel.prototype = Object.create(D.BasePanel.prototype);

  ExtensionsPanel.prototype.init = function () {
    this.body.innerHTML = '';
    this.wrap = el('<div class="entries"></div>');
    this.body.appendChild(this.wrap);
    this.e = entry('px-c', 'px-blue', 'EXTENSIONS · REGISTRY');
    this.wrap.appendChild(this.e.root);
    this.eFuture = entry('px-b', 'px-purple', 'FUTURE · SLOT',
      '通过后端 register_extension 与前端 PanelClasses 注册新功能');
    this.wrap.appendChild(this.eFuture.root);
  };

  ExtensionsPanel.prototype.render = function (data) {
    var ext = data && data.extensions;
    this.e.clear();
    var descMap = {
      clock: '时间数据 · 各面板顶部时间行',
      weather: '天气数据 · 预留接口',
      token_usage: '平台 Token 用量 · 预留接口'
    };
    var added = 0;
    if (ext) {
      Object.keys(ext).forEach(function (key) {
        if (key === 'custom') return;
        var item = ext[key] || {};
        this.e.metric(key, item.enabled ? 'enabled' : 'disabled',
          item.enabled ? 'green' : 'dim');
        added++;
      }, this);
      (ext.custom || []).forEach(function (c) {
        this.e.metric(c.name, c.enabled ? 'enabled' : 'disabled',
          c.enabled ? 'green' : 'dim');
        added++;
      }, this);
    }
    if (!added) this.e.metric('STATUS', 'no data', 'purple');
    this.eFuture.clear();
    this.eFuture.metric('SLOT', 'free', 'dim');
  };

  D.PanelClasses.ExtensionsPanel = ExtensionsPanel;
})();

/* ============================================================
   设置 / 连接面板：终端命令风格
   与 app.js 通过事件总线（D.bus）通信，保持松耦合。
   ============================================================ */
(function () {
  'use strict';
  var D = window.Dashboard;
  var entry = D._panelHelpers.entry;
  var el = D._panelHelpers.el;

  function SettingsPanel() {
    D.BasePanel.call(this, 'settings', 'panel-settings-body');
  }
  SettingsPanel.prototype = Object.create(D.BasePanel.prototype);

  /** 文本开关：[ ON ] / [ OFF ] */
  function makeSwitch(initialOn, onChange) {
    var sw = el('<button class="tswitch' + (initialOn ? '' : ' off') + '"></button>');
    var paint = function () {
      sw.textContent = sw.classList.contains('off') ? '[ OFF ]' : '[ ON ]';
    };
    sw.addEventListener('click', function () {
      var next = sw.classList.contains('off');
      sw.classList.toggle('off', !next);
      paint();
      onChange(next);
    });
    paint();
    return sw;
  }

  SettingsPanel.prototype.init = function () {
    var self = this;
    this.body.innerHTML = '';
    this.wrap = el('<div class="entries"></div>');
    this.body.appendChild(this.wrap);

    /* 连接状态 */
    this.eConn = entry('px-a', 'px-green', 'CONNECTION · MODE');
    this.wrap.appendChild(this.eConn.root);
    this.modeMetric = this.eConn.metric('MODE', '--', 'dim');
    this.lanMetric = this.eConn.metric('LAN', '--', 'dim');

    /* 命令区 */
    this.eCmd = entry('px-c', 'px-blue', 'COMMANDS');
    this.wrap.appendChild(this.eCmd.root);
    this.btBtn = el('<button class="tbtn">&gt; connect bluetooth device</button>');
    this.btBtn.addEventListener('click', function () {
      D.bus.emit('bluetooth-request');
    });
    this.eCmd.metrics.appendChild(this.btBtn);
    var fsBtn = el('<button class="tbtn">&gt; toggle fullscreen</button>');
    fsBtn.addEventListener('click', function () { D.bus.emit('fullscreen-request'); });
    this.eCmd.metrics.appendChild(fsBtn);

    /* 配置区 */
    this.eCfg = entry('px-b', 'px-cyan', 'CONFIG · RUNTIME');
    this.wrap.appendChild(this.eCfg.root);
    this.intervalMetric = this.eCfg.metric('REFRESH', '--');

    var burnRow = el('<div class="set-row"><span class="k">burn-in shift</span></div>');
    var burnOn = localStorage.getItem('dash.burnInShift');
    burnOn = burnOn === null ? true : burnOn === '1';
    burnRow.appendChild(makeSwitch(burnOn, function (on) {
      localStorage.setItem('dash.burnInShift', on ? '1' : '0');
      D.bus.emit('burnin-toggle', on);
    }));
    this.eCfg.metrics.appendChild(burnRow);

    var wakeRow = el('<div class="set-row"><span class="k">wake lock</span></div>');
    var wakeOn = localStorage.getItem('dash.wakeLock') === '1';
    wakeRow.appendChild(makeSwitch(wakeOn, function (on) {
      localStorage.setItem('dash.wakeLock', on ? '1' : '0');
      D.bus.emit('wake-toggle', on);
    }));
    this.eCfg.metrics.appendChild(wakeRow);

    /* 字号调节：[-] 13px [+]（全局根字号，全部文字随动缩放） */
    var fsRow = el('<div class="set-row"><span class="k">font size</span></div>');
    var fsCtrl = el('<span class="fs-ctrl">' +
      '<button class="tbtn-inline" aria-label="减小字号">[-]</button>' +
      '<span class="fs-val"></span>' +
      '<button class="tbtn-inline" aria-label="增大字号">[+]</button>' +
      '</span>');
    var fsVal = fsCtrl.querySelector('.fs-val');
    var paintFs = function () {
      fsVal.textContent = D.FontScale.current + 'px';
    };
    var fsBtns = fsCtrl.querySelectorAll('.tbtn-inline');
    fsBtns[0].addEventListener('click', function () {
      D.FontScale.step(-1);
      paintFs();
    });
    fsBtns[1].addEventListener('click', function () {
      D.FontScale.step(1);
      paintFs();
    });
    paintFs();
    D.bus.on('fontsize', paintFs);   /* 其他来源调整字号时同步显示 */
    fsRow.appendChild(fsCtrl);
    this.eCfg.metrics.appendChild(fsRow);

    /* 监听 app.js 广播的状态 */
    D.bus.on('status', function (s) { self._onStatus(s); });
    D.bus.on('config', function (cfg) {
      if (cfg && cfg.collect_interval) {
        self.intervalMetric.querySelector('.v').textContent =
          cfg.collect_interval + 's';
      }
    });
  };

  SettingsPanel.prototype._onStatus = function (s) {
    if (!s) return;
    this.modeMetric.querySelector('.v').textContent =
      (s.modeText || '--');
    this.lanMetric.querySelector('.v').textContent =
      (s.lanText || '--');
    this.btBtn.innerHTML = '&gt; ' + (s.btButtonText || 'connect bluetooth device');
  };

  D.PanelClasses.SettingsPanel = SettingsPanel;
})();

/* ============================================================
   键盘面板：实时按键状态
   数据来源：后端 keys.py 的独立 WebSocket（默认 8092），
   采集自 Windows Raw Input，毫秒级推送，与 2 秒主数据流无关。
   ============================================================ */
(function () {
  'use strict';
  var D = window.Dashboard;
  var BasePanel = D.BasePanel;

  /* 真实布局：Cornix 50 键分体（列阶梯 + 带旋转的拇指键）
     坐标实测自 Vial 布局截图，画布 14.44u × 5.20u。
     字段：[物理键码, 层0标签, 中心x(u), 中心y(u), 宽(u), 高(u), 旋转角°, 矩阵行, 矩阵列]
     后两个是 .vil 里的物理矩阵坐标，用来把「输出码」换算回「哪一颗键」
     —— 层解析在固件里做完了，系统只看得到输出码，所以必须靠它反查。
     标签是层 0 的兜底值，收到后端下发的键位表后会被按层替换。
     注意：MO() / TD() 长按 / Mouse3 走鼠标通道，系统层看不到对应事件，
     这几颗永远不会点亮，属预期行为。 */
  var KEYS = [
    ['KeyE', 'E', 3.476, 0.500, 1, 1, 0, 0, 3],
    ['KeyI', 'I', 10.939, 0.500, 1, 1, 0, 4, 3],
    ['KeyW', 'W', 2.500, 0.744, 1, 1, 0, 0, 2],
    ['KeyR', 'R', 4.476, 0.744, 1, 1, 0, 0, 4],
    ['KeyU', 'U', 9.964, 0.793, 1, 1, 0, 4, 4],
    ['KeyO', 'O', 11.939, 0.817, 1, 1, 0, 4, 2],
    ['Tab', 'Tab', 0.500, 0.988, 1, 1, 0, 0, 0],
    ['KeyQ', 'Q', 1.500, 0.988, 1, 1, 0, 0, 1],
    ['KeyT', 'T', 5.476, 0.988, 1, 1, 0, 0, 5],
    ['KeyY', 'Y', 8.964, 0.988, 1, 1, 0, 4, 5],
    ['KeyP', 'P', 12.939, 0.988, 1, 1, 0, 4, 1],
    ['Backspace', 'Bksp', 13.939, 0.988, 1, 1, 0, 4, 0],
    ['KeyD', 'D', 3.476, 1.500, 1, 1, 0, 1, 3],
    ['KeyK', 'K', 10.939, 1.500, 1, 1, 0, 5, 3],
    ['KeyS', 'S', 2.500, 1.744, 1, 1, 0, 1, 2],
    ['KeyF', 'F', 4.476, 1.744, 1, 1, 0, 1, 4],
    ['KeyJ', 'J', 9.964, 1.793, 1, 1, 0, 5, 4],
    ['KeyL', 'L', 11.939, 1.817, 1, 1, 0, 5, 2],
    ['CapsLock', 'TD(0)', 0.500, 1.988, 1, 1, 0, 1, 0],
    ['KeyA', 'A', 1.500, 1.988, 1, 1, 0, 1, 1],
    ['KeyG', 'G', 5.476, 1.988, 1, 1, 0, 1, 5],
    ['KeyH', 'H', 8.964, 1.988, 1, 1, 0, 5, 5],
    ['Backslash', '\\', 12.939, 1.988, 1, 1, 0, 5, 1],
    ['Enter', 'Enter', 13.939, 1.988, 1, 1, 0, 5, 0],
    ['KeyC', 'C', 3.476, 2.500, 1, 1, 0, 2, 3],
    ['__BTN3', 'Mouse3', 6.476, 2.500, 1, 1, 0, 5, 6],
    ['AudioVolumeMute', 'Mute', 7.964, 2.500, 1, 1, 0, 2, 6],
    ['Comma', '<', 10.939, 2.500, 1, 1, 0, 6, 3],
    ['KeyX', 'X', 2.500, 2.744, 1, 1, 0, 2, 2],
    ['KeyV', 'V', 4.476, 2.744, 1, 1, 0, 2, 4],
    ['KeyM', 'M', 9.964, 2.793, 1, 1, 0, 6, 4],
    ['Period', '>', 11.939, 2.817, 1, 1, 0, 6, 2],
    ['ShiftLeft', 'LShift', 0.500, 3.037, 1, 1, 0, 2, 0],
    ['KeyZ', 'Z', 1.500, 3.037, 1, 1, 0, 2, 1],
    ['KeyB', 'B', 5.476, 3.037, 1, 1, 0, 2, 5],
    ['KeyN', 'N', 8.964, 3.037, 1, 1, 0, 6, 5],
    ['ArrowUp', 'Up', 12.939, 3.037, 1, 1, 0, 6, 1],
    ['Slash', '?', 13.939, 3.037, 1, 1, 0, 6, 0],
    ['AltLeft', 'LAlt', 2.500, 3.793, 1, 1, 0, 3, 2],
    ['ArrowLeft', 'Left', 11.939, 3.866, 1, 1, 0, 7, 2],
    ['ControlLeft', 'LCtrl', 0.500, 4.037, 1, 1, 0, 3, 0],
    ['MetaLeft', 'LGui', 1.500, 4.037, 1, 1, 0, 3, 1],
    ['ArrowDown', 'Down', 12.939, 4.037, 1, 1, 0, 7, 1],
    ['ArrowRight', 'Right', 13.939, 4.037, 1, 1, 0, 7, 0],
    ['__TD1A', 'TD(1)', 3.988, 4.110, 1, 1, 0, 3, 3],
    ['__MO2', 'MO(2)', 10.452, 4.110, 1, 1, 0, 7, 3],
    ['__TD1B', 'TD(1)', 5.110, 4.196, 1, 1, 10, 3, 4],
    ['__TD1C', 'TD(1)', 9.342, 4.196, 1, 1, -8.1, 7, 4],
    ['__MO3', 'MO(3)', 6.135, 4.561, 1, 1, 18.3, 3, 5],
    ['__MO4', 'MO(4)', 8.293, 4.561, 1, 1, -16, 7, 5],
  ];

  var CANVAS_W = 14.44;   /* 实测包围盒 14.439 × 5.193，向上取整留 0.01u 余量 */
  var CANVAS_H = 5.20;
  var MAX_LOG = 12;

  /* 层 1 副标签的「上档符号」：数字/符号键在真实键帽上会同时印出
     Shift 后的字符，这里按 US 布局补一份，画在副标签的左侧。 */
  var SUB_SHIFT = {
    '1': '!', '2': '@', '3': '#', '4': '$', '5': '%',
    '6': '^', '7': '&', '8': '*', '9': '(', '0': ')',
    '-': '_', '=': '+', ';': ':', "'": '"'
  };

  /* 标签越长字号越小，三档 */
  function sizeClass(text) {
    return text.length > 4 ? ' xs' : (text.length > 1 ? ' sm' : '');
  }

  function KeyboardPanel() {
    BasePanel.call(this, 'keyboard', 'panel-keyboard-body');
  }
  KeyboardPanel.prototype = Object.create(BasePanel.prototype);

  KeyboardPanel.prototype.init = function () {
    var self = this;
    this.body.innerHTML = '';

    this.held = {};        /* "r,c" -> true，按物理位置记，与层无关 */
    this.keyEls = {};      /* code -> 元素（层 0 的兜底映射） */
    this.elByRc = {};      /* "r,c" -> 元素 */
    this.baseLabel = {};   /* "r,c" -> 层 0 标签，用作死键兜底 */
    this.layers = null;    /* 后端下发的键位表：[[{"r,c": 标签}], ...] */
    this.layer = 0;
    this.layerCount = 0;
    this.total = 0;
    this.peak = 0;
    this.stamps = [];
    this.recent = [];
    this.src = '--';
    this.ws = null;
    this.retry = 0;
    this.retryTimer = null;
    this.port = 8092;
    this.alive = true;
    this.disabled = false;

    /* ---- 状态条目 ---- */
    var e = D._panelHelpers.entry('px-a', 'px-cyan', 'KEYBOARD · RAW INPUT',
                                  'Cornix · 50 键分体 · 后台捕获物理键位');
    this.body.appendChild(e.root);
    this.descEl = e.desc;
    this.mLink = e.metric('LINK', 'connecting', 'purple');
    this.mLayer = e.metric('LAYER', '0', 'dim');
    this.mSrc = e.metric('SOURCE', '--', 'dim');
    this.mTotal = e.metric('KEYS', '0', '');
    this.mCps = e.metric('CPS', '0', '');
    this.mHeld = e.metric('HELD', '0', '');
    this.mPeak = e.metric('PEAK', '0', '');

    /* ---- 键位图 ---- */
    this.mapEl = document.createElement('div');
    this.mapEl.className = 'kb-map';
    this.mapEl.style.setProperty('--kb-cw', CANVAS_W);
    this.mapEl.style.setProperty('--kb-ch', CANVAS_H);
    this.body.appendChild(this.mapEl);
    this._buildMap();

    /* ---- 当前按下 ---- */
    this.heldEl = document.createElement('div');
    this.heldEl.className = 'kb-line';
    this.heldEl.innerHTML = '<span class="k">held</span> · ' +
      '<span class="v dim">—</span>';
    this.body.appendChild(this.heldEl);

    /* ---- 最近按键 ---- */
    this.logEl = document.createElement('div');
    this.logEl.className = 'kb-log';
    this.body.appendChild(this.logEl);

    this._fit();
    this._renderLayer();
    this._onResize = function () { self._fit(); };
    window.addEventListener('resize', this._onResize);

    this._loadConfig(function () { self._connect(); });
  };

  /* ---- 键位图构建：按实测坐标绝对定位，支持旋转 ---- */
  KeyboardPanel.prototype._buildMap = function () {
    var frag = document.createDocumentFragment();
    var self = this;
    KEYS.forEach(function (k) {
      var code = k[0];
      var label = k[1];
      var rc = k[7] + ',' + k[8];

      var d = document.createElement('div');
      d.className = 'kb-key' + sizeClass(label);
      d.style.setProperty('--l', (k[2] - k[4] / 2).toFixed(3));
      d.style.setProperty('--t', (k[3] - k[5] / 2).toFixed(3));
      d.style.setProperty('--w', k[4]);
      d.style.setProperty('--h', k[5]);
      d.style.setProperty('--r', (k[6] || 0) + 'deg');

      /* 主标签 + 底部副标签（层 1），分两个 span 好分别定位 */
      var main = document.createElement('span');
      main.className = 'kb-main';
      main.textContent = label;

      var sub = document.createElement('span');
      sub.className = 'kb-sub';

      d.appendChild(main);
      d.appendChild(sub);
      frag.appendChild(d);

      self.keyEls[code] = d;
      self.elByRc[rc] = d;
      self.baseLabel[rc] = label;
      d._main = main;
      d._sub = sub;
    });
    this.mapEl.appendChild(frag);
  };

  /* ---- 按当前层重绘标签 ----
     主标签：有值 → 换成该层的标签；没有（KC_NO）→ 保留层 0 标签但标灰，
     既保住物理布局的可辨识度，又能一眼看出这颗键在这一层是死的。
     副标签：只在层 0 显示，内容是层 1 的同位置键位；与主标签相同则不画。 */
  KeyboardPanel.prototype._applyLabels = function () {
    var rc, el, text, dead, sub, up, cls;
    var table = this.layers && this.layers[this.layer];
    var subTable = (this.layers && this.layer === 0) ? this.layers[1] : null;

    for (rc in this.elByRc) {
      el = this.elByRc[rc];

      text = table ? table[rc] : null;
      dead = !text;
      if (dead) text = this.baseLabel[rc] || '';

      sub = subTable ? subTable[rc] : null;
      if (sub === text) sub = null;          /* 与主标签重复就不画 */

      cls = 'kb-key' + sizeClass(text) +
        (dead ? ' kb-dead' : '') + (sub ? ' has-sub' : '');
      if (el.className !== cls) el.className = cls;

      if (el._main.textContent !== text) el._main.textContent = text;

      if (sub) {
        up = SUB_SHIFT[sub];
        el._sub.textContent = '';
        if (up) {
          var a = document.createElement('b');
          var b = document.createElement('b');
          a.textContent = up;
          b.textContent = sub;
          el._sub.appendChild(a);
          el._sub.appendChild(b);
          el._sub.className = 'kb-sub';
        } else {
          el._sub.textContent = sub;
          el._sub.className = 'kb-sub one';
        }
      } else if (el._sub.textContent) {
        el._sub.textContent = '';
        el._sub.className = 'kb-sub';
      }
    }
    this._renderLayer();
  };

  KeyboardPanel.prototype._renderLayer = function () {
    var v = this.mLayer && this.mLayer.querySelector('.v');
    if (!v) return;
    /* 后端没下发键位表时不做层解析，显示 -- 而不是假的 0 */
    if (!this.layers) {
      v.className = 'v dim';
      v.textContent = '--';
      return;
    }
    var txt = String(this.layer);
    if (this.layerCount) txt += '/' + this.layerCount;
    v.textContent = txt;
    v.className = 'v' + (this.layer ? '' : ' dim');
  };

  /* 1u = 容器宽度 / 画布宽度，整块键位图正好铺满一屏宽 */
  KeyboardPanel.prototype._fit = function () {
    if (!this.mapEl) return;
    var w = this.body.clientWidth;
    if (!w) return;
    this.mapEl.style.setProperty('--kb-u', (w / CANVAS_W).toFixed(2) + 'px');
  };

  /* ---- 配置与连接 ---- */
  KeyboardPanel.prototype._loadConfig = function (done) {
    var self = this;
    var finished = false;
    function fin() { if (!finished) { finished = true; done(); } }
    try {
      fetch('/api/config', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (cfg) {
          if (cfg && cfg.keys_port) self.port = cfg.keys_port;
          if (cfg && cfg.keys_enabled === false) {
            self.disabled = true;
            self._setLink('disabled');
            if (self.descEl) {
              self.descEl.textContent = '按键采集已关闭（config.json → keys.enabled = false）';
              self.descEl.classList.add('kb-warn');
            }
          }
          fin();
        })
        .catch(fin);
      setTimeout(fin, 1500);
    } catch (err) {
      fin();
    }
  };

  KeyboardPanel.prototype._connect = function () {
    if (!this.alive || this.disabled) return;
    var self = this;
    var proto = (location.protocol === 'https:') ? 'wss' : 'ws';
    var host = location.hostname || 'localhost';
    var url = proto + '://' + host + ':' + this.port + '/ws';

    this._setLink('connecting');

    var ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      this._scheduleRetry();
      return;
    }
    this.ws = ws;

    ws.onopen = function () {
      self.retry = 0;
      self._setLink('online');
    };
    ws.onmessage = function (msg) {
      var data;
      try { data = JSON.parse(msg.data); } catch (e) { return; }
      self._handle(data);
    };
    ws.onclose = function () {
      self.ws = null;
      self._setLink('offline');
      self._clearKeys();
      self._scheduleRetry();
    };
    ws.onerror = function () {
      try { ws.close(); } catch (e) {}
    };
  };

  KeyboardPanel.prototype._scheduleRetry = function () {
    if (!this.alive) return;
    var self = this;
    this.retry = Math.min(this.retry + 1, 6);
    var delay = Math.min(1000 * this.retry, 8000);
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(function () { self._connect(); }, delay);
  };

  /* ---- 事件处理 ---- */
  KeyboardPanel.prototype._handle = function (data) {
    if (!data || !data.t) return;

    /* 键位表：接入时下发一次，之后只在换层时发层号 */
    if (data.t === 'keymap') {
      this.layers = data.layers || null;
      this.layerCount = (this.layers && this.layers.length) || 0;
      this._applyLabels();
      this._repaintHeld();
      return;
    }

    if (data.t === 'layer') {
      if (typeof data.n === 'number' && data.n !== this.layer) {
        this.layer = data.n;
        this._applyLabels();
        this._repaintHeld();
      }
      return;
    }

    /* 矩阵直读（混合模式）：层键 / KC_NO 这类不产生 HID 报告的按压也能点亮。
       held 是完整的按压位置集合，直接用它对账，和 state.heldrc 同一套画法。 */
    if (data.t === 'mx') {
      if (typeof data.layer === 'number' && data.layer !== this.layer) {
        this.layer = data.layer;
        this._applyLabels();
      }
      this._syncHeld(data.held || []);
      this._renderStats();
      return;
    }

    if (data.t === 'state' || data.t === 'hb') {
      this._syncHeld(data.heldrc || data.held || []);
      if (typeof data.layer === 'number') {
        if (typeof data.layers === 'number' && data.layers !== this.layerCount) {
          this.layerCount = data.layers;
        }
        if (data.layer !== this.layer) {
          this.layer = data.layer;
          this._applyLabels();
          this._repaintHeld();
        } else {
          this._renderLayer();
        }
      }
      if (data.src) this.src = data.src;
      if (typeof data.total === 'number' && data.total > this.total) {
        this.total = data.total;
      }
      /* 后端会话隔离等告警：直接替换描述行，提示为什么没反应 */
      if (data.warn && this.descEl) {
        this.descEl.textContent = data.warn;
        this.descEl.classList.add('kb-warn');
      }
      this._renderStats();
      return;
    }

    if (data.t !== 'k') return;

    /* rc 是后端按当前层反查出的物理位置；拿不到时退回用输出码匹配 */
    var key = data.rc ? (data.rc[0] + ',' + data.rc[1]) : data.code;

    if (typeof data.layer === 'number' && data.layer !== this.layer) {
      this.layer = data.layer;
      this._applyLabels();
      this._repaintHeld();
    }

    if (data.down) {
      this.total++;
      this.stamps.push(Date.now());
      this.held[key] = true;
      this.peak = Math.max(this.peak, this._heldCount());
      this._paint(key, true);
      this._pushRecent(this._labelOf(key));
    } else {
      delete this.held[key];
      this._paint(key, false);
    }
    if (data.src) this.src = data.src;
    this._renderStats();
    this._renderHeld();
  };

  /* 心跳/接入快照对账：以服务端为准修正本地按下集合 */
  KeyboardPanel.prototype._syncHeld = function (list) {
    var next = {};
    var i;
    for (i = 0; i < list.length; i++) next[list[i]] = true;

    var key;
    for (key in this.held) {
      if (!next[key]) this._paint(key, false);
    }
    for (key in next) {
      if (!this.held[key]) this._paint(key, true);
    }
    this.held = next;
    this._renderHeld();
  };

  KeyboardPanel.prototype._clearKeys = function () {
    for (var key in this.held) this._paint(key, false);
    this.held = {};
    this.stamps = [];
    this._renderHeld();
    this._renderStats();
  };

  /* 换层后把仍按住的键重新点亮（标签变了，on 状态得重新落一次） */
  KeyboardPanel.prototype._repaintHeld = function () {
    for (var key in this.held) this._paint(key, true);
  };

  KeyboardPanel.prototype._paint = function (key, on) {
    var el = this.elByRc[key] || this.keyEls[key];
    if (el) el.classList.toggle('on', !!on);
  };

  KeyboardPanel.prototype._heldCount = function () {
    var n = 0;
    for (var k in this.held) n++;
    return n;
  };

  KeyboardPanel.prototype._labelOf = function (key) {
    /* "r,c"：查当前层标签，死键退回层 0 标签 */
    if (String(key).indexOf(',') > 0) {
      var table = this.layers && this.layers[this.layer];
      var t = table && table[key];
      if (t) return t;
      return this.baseLabel[key] || key;
    }
    return String(key || '')
      .replace(/^Key/, '')
      .replace(/^Digit/, '')
      .replace(/^Numpad/, 'N');
  };

  KeyboardPanel.prototype._pushRecent = function (label) {
    this.recent.unshift(label);
    if (this.recent.length > MAX_LOG) this.recent.pop();
    var html = '';
    for (var i = 0; i < this.recent.length; i++) {
      html += '<span class="kb-tag' + (i === 0 ? ' newest' : '') + '">' +
        this.recent[i] + '</span>';
    }
    this.logEl.innerHTML = html;
  };

  /* ---- 渲染 ---- */
  KeyboardPanel.prototype._renderHeld = function () {
    var names = [];
    for (var code in this.held) names.push(this._labelOf(code));
    var v = this.heldEl.querySelector('.v');
    if (!v) return;
    if (!names.length) {
      v.className = 'v dim';
      v.textContent = '—';
    } else {
      v.className = 'v';
      v.textContent = names.join(' + ');
    }
  };

  KeyboardPanel.prototype._renderStats = function () {
    var now = Date.now();
    var i = 0;
    while (i < this.stamps.length && now - this.stamps[i] > 1000) i++;
    if (i) this.stamps.splice(0, i);

    this._setVal(this.mTotal, String(this.total));
    this._setVal(this.mCps, String(this.stamps.length));
    this._setVal(this.mHeld, String(this._heldCount()));
    this._setVal(this.mPeak, String(this.peak));
    this._setVal(this.mSrc, this.src === 'bt' ? 'BLE' : this.src.toUpperCase(),
      this.src === 'bt' ? 'purple' : 'dim');
  };

  KeyboardPanel.prototype._setVal = function (metricEl, text, cls) {
    if (!metricEl) return;
    var v = metricEl.querySelector('.v');
    if (!v) return;
    v.textContent = text;
    if (cls !== undefined) {
      v.className = 'v' + (cls ? ' ' + cls : '');
    }
  };

  KeyboardPanel.prototype._setLink = function (state) {
    var map = {
      online: ['online', 'green'],
      offline: ['offline', 'purple'],
      connecting: ['connecting', 'blue'],
      disabled: ['disabled', 'purple']
    };
    var pair = map[state] || ['--', 'dim'];
    this._setVal(this.mLink, pair[0], pair[1]);
  };

  KeyboardPanel.prototype.show = function () {
    this._fit();
  };

  KeyboardPanel.prototype.destroy = function () {
    this.alive = false;
    clearTimeout(this.retryTimer);
    if (this._onResize) window.removeEventListener('resize', this._onResize);
    if (this.ws) {
      try { this.ws.close(); } catch (e) {}
      this.ws = null;
    }
  };

  D.PanelClasses.KeyboardPanel = KeyboardPanel;
})();

/* ============================================================
   PanelManager：面板注册表与统一调度
   ============================================================ */
(function () {
  'use strict';
  var D = window.Dashboard;

  var PanelManager = {
    /* 时间面板已取消：每个面板顶部都有实时时间行 */
    order: ['system', 'agents', 'token', 'weather', 'keyboard',
            'extensions', 'settings'],
    panels: [],   /* 实例数组，顺序与 order 一致 */

    /** 实例化所有面板并挂载到 index.html 中的 section 容器 */
    initAll: function () {
      var sections = document.querySelectorAll('#panels-track .panel');
      var byName = {};
      sections.forEach(function (sec) {
        byName[sec.getAttribute('data-panel')] = sec.querySelector('.panel-body');
      });

      /* 构建 小写面板名 -> 类 的映射（SystemPanel -> system） */
      var registry = D.PanelClasses;
      var classMap = {};
      Object.keys(registry).forEach(function (key) {
        if (/Panel$/.test(key)) {
          classMap[key.replace(/Panel$/, '').toLowerCase()] = registry[key];
        }
      });

      var self = this;
      this.order.forEach(function (name) {
        var Cls = classMap[name] || D.GenericPanel;
        self._mount(name, byName[name], Cls);
      });

      /* 自动注册 index.html 中存在但 order 未列出的新面板（前端扩展机制） */
      Object.keys(byName).forEach(function (name) {
        if (self.order.indexOf(name) >= 0) return;
        var Cls = classMap[name] || D.GenericPanel;
        self._mount(name, byName[name], Cls);
        self.order.push(name);
      });
    },

    _mount: function (name, bodyEl, Cls) {
      if (!bodyEl) return;
      var inst = new Cls(name, null);
      inst.name = name;
      inst.body = bodyEl;
      inst.init();
      this.panels.push(inst);
    },

    renderAll: function (data) {
      this.panels.forEach(function (p) {
        try { p.render(data); } catch (e) { /* 单面板出错不影响其他 */ }
      });
    },

    panelAt: function (i) { return this.panels[i]; },
    count: function () { return this.panels.length; },

    show: function (index) {
      this.panels.forEach(function (p, i) {
        if (i === index) { try { p.show(); } catch (e) {} }
        else { try { p.hide(); } catch (e) {} }
      });
      /* 广播当前面板名：屏保据此判断要不要压暗/熄屏
         （键盘面板要求常亮，见 app.js 的 ScreenSaver） */
      try { D.bus.emit('panel-change', this.order[index]); } catch (e) {}
    },

    destroyAll: function () {
      this.panels.forEach(function (p) {
        try { p.destroy(); } catch (e) {}
      });
    }
  };

  /* 通用占位面板：未注册专属实现的面板兜底 */
  function GenericPanel(name) {
    D.BasePanel.call(this, name, null);
  }
  GenericPanel.prototype = Object.create(D.BasePanel.prototype);
  GenericPanel.prototype.init = function () {
    var entry = D._panelHelpers.entry;
    this.body.innerHTML = '';
    var e = entry('px-c', 'px-purple', this.name.toUpperCase() + ' · PLACEHOLDER');
    e.setDesc('该面板尚未实现');
    this.body.appendChild(e.root);
  };
  D.GenericPanel = GenericPanel;
  D.PanelManager = PanelManager;
})();

