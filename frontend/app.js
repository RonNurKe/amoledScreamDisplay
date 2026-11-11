/* ============================================================
   app.js - 主应用逻辑
   职责：
   1. DataService：统一数据接入抽象层
      （websocket -> polling -> mock 自动降级；可选手动蓝牙）
   2. 连接状态显示（未连接/正在连接/已连接/连接失败/已降级）
   3. 滑动切换面板（触摸 + 鼠标）、指示器圆点
   4. 防烧屏：定时位置偏移 + 屏保模式
   5. 屏幕常亮（Screen Wake Lock）、全屏、Service Worker 注册
   6. 页面隐藏时降低刷新频率，重新可见时恢复

   依赖：bluetooth.js（BluetoothClient）、panels.js（PanelManager / bus）
   ============================================================ */

(function () {
  'use strict';

  var D = window.Dashboard;
  var bus = D.bus;

  /* ============================================================
     实用工具（覆盖 panels.js 的兜底实现）
     ============================================================ */
  D.Utils.formatBytes = function (bytes) {
    if (bytes === null || bytes === undefined || isNaN(bytes)) return '--';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0, v = Number(bytes);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
  };
  D.Utils.formatSpeed = function (bytesPerSec) {
    if (bytesPerSec === null || bytesPerSec === undefined || isNaN(bytesPerSec)) {
      return '--';
    }
    return D.Utils.formatBytes(bytesPerSec) + '/s';
  };
  D.Utils.clamp = function (v, min, max) {
    return Math.max(min, Math.min(max, v));
  };

  /* ============================================================
     全局应用状态
     ============================================================ */
  var App = {
    config: {
      collect_interval: 2,
      websocket_port: 8091,
      enable_bluetooth: false,
      bluetooth_service_uuid: '0000ffe0-0000-1000-8000-00805f9b34fb',
      bluetooth_characteristic_uuid: '0000ffe1-0000-1000-8000-00805f9b34fb',
      burn_in_shift: true
    },
    lanUrl: '',
    latestData: null,
    wakeLock: null
  };

  /* ============================================================
     状态栏 UI：dot + 文案
     状态语义：
       off        未连接
       connecting 正在连接
       on         已连接
       fail       连接失败
       fallback   已降级到局域网模式（蓝牙不可用 -> WebSocket/轮询）
       mock       本地模拟数据（无后端时保证页面可演示）
     ============================================================ */
  var STATUS_TEXT = {
    off: '未连接',
    connecting: '正在连接',
    on: '已连接',
    fail: '连接失败',
    fallback: '已降级到局域网模式',
    mock: '本地模拟数据'
  };
  var STATUS_DOT = {
    off: 'dot-off', connecting: 'dot-connecting', on: 'dot-on',
    fail: 'dot-fail', fallback: 'dot-fallback', mock: 'dot-mock'
  };

  var StatusUI = {
    set: function (key, extra) {
      var dot = document.getElementById('conn-dot');
      var text = document.getElementById('conn-text');
      if (dot) dot.className = 'conn-dot ' + (STATUS_DOT[key] || 'dot-off');
      if (text) text.textContent = STATUS_TEXT[key] + (extra ? ' · ' + extra : '');

      /* 同步到设置面板 */
      bus.emit('status', {
        key: key,
        modeText: STATUS_TEXT[key] + (extra ? ' · ' + extra : ''),
        lanText: App.lanUrl || '--',
        btButtonText: '连接蓝牙设备'
      });
    }
  };

  /* ============================================================
     DataService：统一数据接入抽象层
     前端面板只依赖 bus 上的 'data' 事件，不关心底层传输。
     优先级：
       1. WebSocket 推送（默认兼容桥接模式）
       2. HTTP 轮询（WebSocket 不可用时自动降级，期间持续重试 WS）
       3. Mock 模拟（页面不在后端环境下打开时，保证可演示）
       4. 蓝牙（用户在设置面板手动触发；失败自动降级回局域网）
     ============================================================ */
  var DataService = {
    mode: 'offline',          /* bluetooth | websocket | polling | mock | offline */
    _ws: null,
    _pollTimer: null,
    _mockTimer: null,
    _wsRetry: 0,
    _wsRetryTimer: null,
    _btClient: null,

    /** 入口：先拿后端公开配置，再决定传输方式 */
    start: function () {
      var self = this;
      StatusUI.set('connecting');
      this._fetchConfig()
        .then(function (cfg) {
          if (cfg) {
            App.config = cfg;
            App.lanUrl = location.origin + '/';
            bus.emit('config', cfg);
            self._connectWebSocket();
          } else {
            /* 拿不到后端配置（file:// 直接打开 或 后端未启动）-> Mock 演示 */
            self._startMock();
          }
        })
        .catch(function () { self._startMock(); });
    },

    /** 获取 /api/config；失败返回 null（不抛异常） */
    _fetchConfig: function () {
      return fetch('api/config', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; });
    },

    /* ---------------- WebSocket ---------------- */
    _connectWebSocket: function () {
      var self = this;
      if (this._ws) { try { this._ws.close(); } catch (e) {} }

      var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      var url = proto + '//' + location.hostname + ':' +
        App.config.websocket_port + '/ws';
      var ws;
      try {
        ws = new WebSocket(url);
      } catch (e) {
        this._startPolling();
        return;
      }
      this._ws = ws;
      StatusUI.set('connecting', 'WebSocket');

      ws.onopen = function () {
        self._wsRetry = 0;
        self._stopPolling();
        self.mode = 'websocket';
        StatusUI.set('on', 'WebSocket');
      };
      ws.onmessage = function (evt) {
        self._handleData(evt.data);
      };
      ws.onclose = function () {
        if (self.mode === 'bluetooth') return;   /* 已切蓝牙，无需处理 */
        /* 重试若干次后降级为 HTTP 轮询（轮询期间持续重试 WS） */
        self._wsRetry += 1;
        if (self._wsRetry <= 5) {
          var delay = Math.min(15000, 1000 * Math.pow(2, self._wsRetry - 1));
          StatusUI.set('connecting', 'WebSocket 重试 ' + self._wsRetry);
          self._wsRetryTimer = setTimeout(function () {
            self._connectWebSocket();
          }, delay);
        } else {
          self._startPolling();
        }
      };
      ws.onerror = function () { try { ws.close(); } catch (e) {} };
    },

    /* ---------------- HTTP 轮询（兼容桥接） ---------------- */
    _startPolling: function () {
      var self = this;
      if (this._pollTimer) return;
      this.mode = 'polling';
      /* 轮询本身即“局域网模式”；从 WS 降级而来时给出降级提示 */
      StatusUI.set('fallback', 'HTTP 轮询');
      var interval = (Number(App.config.collect_interval) || 2) * 1000;

      var tick = function () {
        if (document.hidden) return;        /* 页面隐藏时暂停轮询，省电省流量 */
        fetch('api/data', { cache: 'no-store' })
          .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
          .then(function (data) {
            if (self.mode !== 'polling') return;
            StatusUI.set('fallback', 'HTTP 轮询');
            self._publish(data);
          })
          .catch(function () {
            if (self.mode === 'polling') StatusUI.set('fail', 'HTTP 轮询');
          });
      };
      tick();
      this._pollTimer = setInterval(tick, interval);

      /* 轮询期间每 30 秒尝试一次 WebSocket 恢复 */
      if (!this._wsRetryTimer) {
        this._wsRetryTimer = setInterval(function () {
          if (self.mode === 'polling') {
            self._wsRetry = 0;
            self._connectWebSocket();
          }
        }, 30000);
      }
    },

    _stopPolling: function () {
      if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
    },

    /* ---------------- Mock（离线演示） ---------------- */
    _startMock: function () {
      var self = this;
      if (this._mockTimer) return;
      this.mode = 'mock';
      StatusUI.set('mock');
      var cpu = 20, mem = 45, up = 100 * 1024, down = 800 * 1024;

      /* OpenCode 状态（模拟）：三个会话各自的状态机随机推进，
         保证无后端时面板仍是"活的"（静态 mock 曾被误认为故障） */
      var pad2 = function (n) { return (n < 10 ? '0' : '') + n; };
      var isoOf = function (d) {
        return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' +
          pad2(d.getDate()) + 'T' + pad2(d.getHours()) + ':' +
          pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
      };
      var ocCycle = ['thinking', 'reading', 'writing', 'working', 'idle'];
      var mkOcSession = function (sid, title, project) {
        var st = ocCycle[Math.floor(Math.random() * ocCycle.length)];
        var t = isoOf(new Date());
        return { sid: sid, title: title, project: project,
                 state: st, state_since: t, last_event_at: t, stale: false };
      };
      var ocSessions = [
        mkOcSession('ses_mock0001', '实施 OpenCode 状态面板', 'amoledScreamDisplay'),
        mkOcSession('ses_mock0002', '排查 pip 编码问题', 'infra-tools'),
        mkOcSession('ses_mock0003', '重构 transport 层', 'infra-tools')
      ];
      var ocEvents = [];
      var ocTick = function () {
        var nowIso = isoOf(new Date());
        ocSessions.forEach(function (s) {
          if (Math.random() < 0.35) {
            s.state = ocCycle[(ocCycle.indexOf(s.state) + 1) % ocCycle.length];
            s.state_since = nowIso;
            s.last_event_at = nowIso;
            ocEvents.unshift({ state: s.state, at: nowIso, project: s.project });
          }
        });
        if (ocEvents.length > 5) ocEvents = ocEvents.slice(0, 5);
      };
      ocTick();

      this._mockTimer = setInterval(function () {
        if (document.hidden) return;
        /* 随机游走，模拟平滑变化的真实数据 */
        cpu = D.Utils.clamp(cpu + (Math.random() - 0.5) * 12, 3, 96);
        mem = D.Utils.clamp(mem + (Math.random() - 0.5) * 4, 20, 90);
        up = D.Utils.clamp(up + (Math.random() - 0.5) * 100 * 1024, 0, 2 * 1024 * 1024);
        down = D.Utils.clamp(down + (Math.random() - 0.5) * 600 * 1024, 0, 20 * 1024 * 1024);
        var now = new Date();
        var pad = function (n) { return (n < 10 ? '0' : '') + n; };
        self._publish({
          timestamp: now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' +
            pad(now.getDate()) + 'T' + pad(now.getHours()) + ':' +
            pad(now.getMinutes()) + ':' + pad(now.getSeconds()),
          source: 'mock',
          transport: 'mock',
          system: {
            hostname: 'MOCK-PC', os: 'MockOS 10 (simulated)',
            cpu_percent: cpu, cpu_count: 8,
            memory_total: 16 * 1024 * 1024 * 1024,
            memory_used: Math.round(16 * 1024 * 1024 * 1024 * mem / 100),
            memory_available: Math.round(16 * 1024 * 1024 * 1024 * (100 - mem) / 100),
            memory_percent: mem,
            disk: { total: 1024 * 1024 * 1024 * 1024, used: 640 * 1024 * 1024 * 1024,
                    free: 384 * 1024 * 1024 * 1024, percent: 62.5 },
            disk_usage_summary: { total: 1024 * 1024 * 1024 * 1024,
                                  used: 640 * 1024 * 1024 * 1024,
                                  free: 384 * 1024 * 1024 * 1024, percent: 62.5 },
            disk_partitions: [
              { device: 'C:', mountpoint: 'C:\\', fstype: 'NTFS',
                total: 1024 * 1024 * 1024 * 1024, used: 640 * 1024 * 1024 * 1024,
                free: 384 * 1024 * 1024 * 1024, percent: 62.5 }
            ],
            network: { bytes_sent: 123456789, bytes_recv: 987654321,
                       send_speed: up, recv_speed: down }
          },
          extensions: {
            clock: { enabled: true, timezone: 'local',
                     backend_time: '（模拟）', weekday: '星期六' },
            weather: { enabled: false, city: '', temperature: null,
                       humidity: null, condition: '' },
            token_usage: { enabled: false, provider: 'example', used: null,
                           limit: null, remaining: null, percent: null },
            /* Agents 状态（模拟）：每 tick 推进状态机并重组快照 */
            agents_status: (function () {
              ocTick();
              var active = ocSessions[0];
              return { enabled: true, state: active.state,
                       state_since: active.state_since,
                       last_event_at: active.last_event_at, stale: false,
                       sessions: ocSessions, events: ocEvents };
            })(),
            custom: []
          }
        });
      }, 2000);
    },

    /* ---------------- 蓝牙（用户手动触发） ---------------- */
    connectBluetooth: function () {
      var self = this;
      var BT = D.BluetoothClient;
      if (!BT) { StatusUI.set('fail', '蓝牙模块未加载'); return; }

      /* 已连接蓝牙时再次点击 = 断开 */
      if (this.mode === 'bluetooth') {
        this._disconnectBluetooth();
        StatusUI.set('connecting');
        this._connectWebSocket();
        return;
      }

      StatusUI.set('connecting', '蓝牙');
      var client = new BT({
        serviceUuid: App.config.bluetooth_service_uuid,
        characteristicUuid: App.config.bluetooth_characteristic_uuid,
        namePrefix: App.config.bluetooth_device_name || ''
      });
      this._btClient = client;

      client.onData = function (data) {
        self.mode = 'bluetooth';
        StatusUI.set('on', '蓝牙');
        self._publish(data);
      };
      /* 蓝牙断开 -> 自动降级回局域网兼容模式 */
      client.onDisconnect = function () {
        if (self.mode !== 'bluetooth') return;
        self.mode = 'offline';
        StatusUI.set('fallback', '蓝牙断开');
        self._connectWebSocket();
      };
      client.onLog = function (msg) {
        if (App.config.debug) console.log(msg);
      };

      client.connect().catch(function (err) {
        /* 权限拒绝 / 不支持 / 找不到设备：清晰提示并降级 */
        StatusUI.set('fallback', err.message);
        if (self.mode !== 'bluetooth') self._connectWebSocket();
      });
    },

    _disconnectBluetooth: function () {
      if (this._btClient) {
        try { this._btClient.disconnect(); } catch (e) {}
        this._btClient = null;
      }
      this.mode = 'offline';
    },

    /* ---------------- 数据出口 ---------------- */
    _handleData: function (raw) {
      try { this._publish(JSON.parse(raw)); } catch (e) { /* 忽略坏包 */ }
    },
    _publish: function (data) {
      if (!data || !data.system) return;
      App.latestData = data;
      bus.emit('data', data);
    }
  };

  D.App = App;
  D.DataService = DataService;
  D.StatusUI = StatusUI;

  /* ============================================================
     滑动切换面板：触摸（touchstart/move/end）+ 鼠标（pointer events）
     手指水平滑动超过阈值即切换，带过渡动画；垂直滑动不受影响。
     ============================================================ */
  var Swipe = {
    index: 0,
    startX: 0, startY: 0, startT: 0, deltaX: 0, dragging: false,
    onWidget: false,   /* 触摸起点是否落在按钮/开关等交互元素上 */
    width: 1,
    track: null,

    init: function () {
      var self = this;
      this.track = document.getElementById('panels-track');
      this.buildIndicators();

      /* 触摸优先；无触摸能力时使用 Pointer 事件（桌面鼠标拖拽调试） */
      if ('ontouchstart' in window) {
        var vp = document.getElementById('panels-viewport');
        vp.addEventListener('touchstart', function (e) {
          self.onStart(e.target, e.touches[0].clientX, e.touches[0].clientY);
        }, { passive: true });
        vp.addEventListener('touchmove', function (e) {
          /* 水平意图明确时阻止浏览器默认滚动 */
          if (self.dragging &&
              Math.abs(self.deltaX) > Math.abs(e.touches[0].clientY - self.startY)) {
            e.preventDefault();
          }
          self.onMove(e.touches[0].clientX);
        }, { passive: false });
        vp.addEventListener('touchend', function (e) {
          var t = e.changedTouches && e.changedTouches[0];
          self.onEnd(t ? t.clientX : null, t ? t.clientY : null);
        });
        vp.addEventListener('touchcancel', function () { self.onEnd(null, null); });
      } else {
        var target = document.getElementById('panels-viewport');
        target.addEventListener('pointerdown', function (e) {
          if (e.pointerType === 'mouse' && e.button === 0) {
            self.onStart(e.target, e.clientX, e.clientY);
            target.setPointerCapture(e.pointerId);
          }
        });
        target.addEventListener('pointermove', function (e) {
          if (self.dragging) self.onMove(e.clientX);
        });
        target.addEventListener('pointerup', function (e) {
          self.onEnd(e.clientX, e.clientY);
        });
      }
    },

    onStart: function (target, x, y) {
      this.startX = x; this.startY = y; this.startT = Date.now();
      this.deltaX = 0; this.dragging = true;
      this.width = this.track.parentElement.clientWidth || 1;
      this.track.style.transition = 'none';        /* 拖拽期间跟手 */
      /* 起点在按钮/开关等交互元素上时，不做点按翻页，避免误触 */
      this.onWidget = !!(target && target.closest &&
        target.closest('button, a, input, .set-row, .fs-ctrl'));
    },

    onMove: function (x) {
      if (!this.dragging) return;
      this.deltaX = x - this.startX;
      var offset = -this.index * this.width + this.deltaX;
      /* 边缘阻尼：第一屏左滑 / 最后一屏右滑只移动一半距离 */
      if ((this.index === 0 && this.deltaX > 0) ||
          (this.index === this.count() - 1 && this.deltaX < 0)) {
        offset = -this.index * this.width + this.deltaX * 0.4;
      }
      this.track.style.transform = 'translateX(' + offset + 'px)';
    },

    onEnd: function (endX, endY) {
      if (!this.dragging) return;
      this.dragging = false;
      this.track.style.transition = '';            /* 恢复 CSS 过渡动画 */
      var dt = Date.now() - this.startT;
      var velocity = Math.abs(this.deltaX) / Math.max(1, dt);  /* px/ms */
      var threshold = this.width * 0.18;
      if (Math.abs(this.deltaX) > threshold ||
          (Math.abs(this.deltaX) > 40 && velocity > 0.5)) {
        this.goTo(this.index + (this.deltaX < 0 ? 1 : -1));    /* 滑动翻页 */
      } else if (this.isTap(endX, endY, dt) && !this.onWidget) {
        this.handleTapZone(endX);                              /* 点按翻页 */
      } else {
        this.goTo(this.index);                                 /* 回弹 */
      }
    },

    /* 点按判定：位移极小（<10px）且时间短（<350ms） */
    isTap: function (endX, endY, dt) {
      if (endX === null || endX === undefined) return false;
      var dx = Math.abs(endX - this.startX);
      var dy = Math.abs((endY === null || endY === undefined
        ? this.startY : endY) - this.startY);
      return dx < 10 && dy < 10 && dt < 350;
    },

    /* 点按翻页热区：左右边缘各 1/5（20%）翻页，中间 60% 保留给交互 */
    handleTapZone: function (x) {
      var w = this.track.parentElement.clientWidth || window.innerWidth;
      if (x < w * 0.2) this.goTo(this.index - 1);
      else if (x > w * 0.8) this.goTo(this.index + 1);
    },

    count: function () { return D.PanelManager.count(); },

    goTo: function (index) {
      index = D.Utils.clamp(index, 0, this.count() - 1);
      this.index = index;
      this.track.style.transform =
        'translateX(' + (-index * 100) + '%)';
      this.updateIndicators();
      D.PanelManager.show(index);
    },

    /* 底部小圆点指示器 */
    buildIndicators: function () {
      var nav = document.getElementById('indicators');
      nav.innerHTML = '';
      var self = this;
      for (var i = 0; i < this.count(); i++) {
        (function (idx) {
          var dot = document.createElement('button');
          dot.className = 'indicator-dot';
          dot.setAttribute('aria-label', '面板 ' + (idx + 1));
          dot.addEventListener('click', function () { self.goTo(idx); });
          nav.appendChild(dot);
        })(i);
      }
      this.updateIndicators();
    },
    updateIndicators: function () {
      var dots = document.querySelectorAll('#indicators .indicator-dot');
      for (var i = 0; i < dots.length; i++) {
        dots[i].classList.toggle('active', i === this.index);
      }
    }
  };

  /* ============================================================
     防烧屏机制
     1) 定时位置偏移：30~120 秒随机间隔，对 #app 平移 ±12px；
     2) 屏保模式：5 分钟无交互 -> 降低透明度并缓慢漂移。
     均可通过 localStorage / 后端配置开关。
     ============================================================ */
  var BurnIn = {
    shiftEnabled: true,
    timer: null,

    init: function () {
      var self = this;
      var appEl = document.getElementById('app');
      appEl.classList.add('burn-in-shift');
      var stored = localStorage.getItem('dash.burnInShift');
      this.shiftEnabled = stored === null
        ? (App.config.burn_in_shift !== false) : stored === '1';
      this.apply();

      bus.on('burnin-toggle', function (on) {
        self.shiftEnabled = on;
        self.apply();
      });
      this.scheduleNext();
    },

    apply: function () {
      var appEl = document.getElementById('app');
      if (this.shiftEnabled) {
        appEl.classList.add('burn-in-shift');
        this.scheduleNext();
      } else {
        appEl.classList.remove('burn-in-shift');
        appEl.style.transform = '';
        if (this.timer) { clearTimeout(this.timer); this.timer = null; }
      }
    },

    scheduleNext: function () {
      var self = this;
      if (this.timer) clearTimeout(this.timer);
      if (!this.shiftEnabled) return;
      /* 30 ~ 120 秒随机触发一次 */
      var delay = 30000 + Math.random() * 90000;
      this.timer = setTimeout(function () {
        if (!document.hidden && !document.body.classList.contains('screen-saver-mode')) {
          var dx = Math.round((Math.random() * 2 - 1) * 12);   /* -12 ~ 12px */
          var dy = Math.round((Math.random() * 2 - 1) * 12);
          document.getElementById('app').style.transform =
            'translate(' + dx + 'px, ' + dy + 'px)';
        }
        self.scheduleNext();
      }, delay);
    }
  };

  var ScreenSaver = {
    TIMEOUT: 20 * 1000,         /* 无操作 20 秒：一级屏保（压暗 + 漂移） */
    TIMEOUT_OFF: 40 * 1000,     /* 无操作 40 秒：二级熄屏（全黑，等效息屏） */
    timer: null,
    offTimer: null,
    ocActive: false,            /* 是否存在 非 idle 且未过期 的 OpenCode 会话 */
    hold: false,                /* 当前面板要求常亮（键盘面板）：不压暗、不息屏 */
    inited: false,

    init: function () {
      var self = this;
      var reset = function () { self.reset(); };
      /* 任何交互都视为“有操作” */
      ['touchstart', 'pointerdown', 'keydown', 'wheel'].forEach(function (evt) {
        window.addEventListener(evt, reset, { passive: true });
      });
      /* OpenCode 面板广播会话活跃状态：
         活跃 → 抑制二级熄屏（若已熄屏则点亮回一级变暗）；
         全部空闲 → 处于一级屏保时重新武装二级熄屏倒计时 */
      bus.on('agents-active', function (active) {
        self.ocActive = !!active;
        if (active) {
          if (self.offTimer) {
            clearTimeout(self.offTimer);
            self.offTimer = null;
          }
          document.body.classList.remove('screen-off-mode');
          OffWidget.stop();   /* 点亮回一级变暗，飘屏组件停走并隐藏 */
        } else if (!self.hold &&
                   document.body.classList.contains('screen-saver-mode') &&
                   !document.body.classList.contains('screen-off-mode') &&
                   !self.offTimer) {
          self.offTimer = setTimeout(function () {
            self.offTimer = null;
            if (!self.ocActive) {
              document.body.classList.add('screen-off-mode');
              OffWidget.start();   /* 重新进入熄屏：启动飘屏组件（此前漏调导致卡住） */
            }
          }, self.TIMEOUT_OFF);
        }
      });
      /* 面板切换：键盘面板要求常亮（不压暗、不息屏），
         防烧屏位移仍由 BurnIn 独立调度，不受影响 */
      bus.on('panel-change', function (name) {
        self.setHold(name === 'keyboard');
      });
      this.inited = true;
      this.reset();
    },

    /* 切换“常亮面板”状态：键盘面板需要一直亮着，方便边打边看 */
    setHold: function (on) {
      on = !!on;
      if (this.hold === on) return;
      this.hold = on;
      if (this.inited) this.reset();
    },

    reset: function () {
      var self = this;
      document.body.classList.remove('screen-saver-mode');
      document.body.classList.remove('screen-off-mode');
      OffWidget.stop();     /* 任何交互/复位都收起飘屏组件 */
      if (this.timer) clearTimeout(this.timer);
      if (this.offTimer) clearTimeout(this.offTimer);
      this.timer = null;
      this.offTimer = null;

      /* 常亮面板：交互只负责“保持亮着”，不重新武装熄屏倒计时。
         防烧屏位移由 BurnIn 独立调度，不受这里影响，仍然照常跑。 */
      if (this.hold) return;

      this.timer = setTimeout(function () {
        document.body.classList.add('screen-saver-mode');
      }, this.TIMEOUT);
      this.offTimer = setTimeout(function () {
        self.offTimer = null;
        /* 有非空闲会话时最多保持一级变暗，不熄屏；
           被抑制后等待 agents-active(false) 事件重新武装 */
        if (!self.ocActive) {
          document.body.classList.add('screen-off-mode');
          OffWidget.start();   /* 黑屏起跑：日期/时间/天气飘屏 */
        }
      }, this.TIMEOUT_OFF);
    }
  };

  /* ============================================================
     二级熄屏飘屏小组件：黑屏上显示 日期/星期 · 时间 · 天气，
     沿斜线匀速移动，碰到屏幕边缘按反射角反弹（分量取反，速度不变）。
     仅 screen-off-mode 期间可见并运行（组件在 #app 之外，
     不受熄屏 opacity:0 影响）；pointer-events 穿透不挡唤醒。
     天气数据来自 weather 扩展快照，未接入时显示占位文案。
     ============================================================ */
  var OffWidget = {
    el: null, dEl: null, tEl: null, wEl: null,
    x: 0, y: 0, vx: 0, vy: 0,
    raf: null, lastT: 0, lastSec: -1, lastFrameAt: 0, lastPing: 0,
    tick: null,                  /* 秒级定时器：文本刷新 + 循环自愈 */
    active: false,               /* 是否处于熄屏运行态 */
    SPEED: 46,                   /* 移动速度 px/s（缓慢匀速） */

    init: function () {
      this.el = document.createElement('div');
      this.el.id = 'ss-widget';
      this.el.setAttribute('aria-hidden', 'true');
      this.el.innerHTML =
        '<span class="d"></span> · <span class="t"></span> · ' +
        '<span class="w"></span> · <span class="b"></span>';
      document.body.appendChild(this.el);
      this.dEl = this.el.querySelector('.d');
      this.tEl = this.el.querySelector('.t');
      this.wEl = this.el.querySelector('.w');
      this.bEl = this.el.querySelector('.b');

      /* 数据管道驱动刷新：即使页面计时器被浏览器冻结，
         WS/轮询每 2s 到达的 data 事件也会让日期/时间保持新鲜 */
      var self = this;
      bus.on('data', function () {
        if (!document.body.classList.contains('screen-off-mode')) return;
        self.active = true;
        self._render();
        self._ping('widget-data');
      });
      /* 回到前台：立即重绘并确保动画循环在跑 */
      document.addEventListener('visibilitychange', function () {
        if (document.hidden ||
            !document.body.classList.contains('screen-off-mode')) return;
        self.active = true;
        self._render();
        self._restartLoop();
      });
      /* 常驻秒级定时器：以 CSS 类为准自愈——任何路径只要让熄屏类出现，
         1 秒内组件自动开始渲染与移动（免疫"漏调 start()"类缺陷） */
      this.tick = setInterval(function () {
        if (!document.body.classList.contains('screen-off-mode')) return;
        self.active = true;
        self._render();
        if (Date.now() - self.lastFrameAt > 1500) self._restartLoop();
        /* 每 5 秒上报一次心跳（证明页面计时器在跑） */
        var secs = Math.floor(Date.now() / 1000);
        if (secs % 5 === 0 && secs !== self.lastPing) {
          self.lastPing = secs;
          self._ping('widget');
        }
      }, 1000);
    },

    /* 心跳上报（诊断用）：区分计时器驱动与数据驱动 */
    _ping: function (src) {
      try {
        fetch('/api/ping?src=' + src, { cache: 'no-store' });
      } catch (e) { /* 心跳失败不影响功能 */ }
    },

    /* 进入二级熄屏：随机初始位置 + 随机斜向匀速起跑
       （定时器常驻在 init()，此处只负责起跑；即使未被调用，
         常驻定时器检测到熄屏类后也会自动接管） */
    start: function () {
      if (!this.el) this.init();
      this.active = true;
      var w = window.innerWidth, h = window.innerHeight;
      var ew = this.el.offsetWidth || 220, eh = this.el.offsetHeight || 20;
      this.x = Math.random() * Math.max(1, w - ew);
      this.y = Math.random() * Math.max(1, h - eh);
      var ang;
      do {
        ang = Math.random() * Math.PI * 2;
      } while (Math.abs(Math.cos(ang)) < 0.35 || Math.abs(Math.sin(ang)) < 0.35);
      this.vx = Math.cos(ang) * this.SPEED;
      this.vy = Math.sin(ang) * this.SPEED;
      this._render();                 /* 立即出内容，不等下一帧 */
      this._ping('widget');
      this._restartLoop();
    },

    /* 取消陈旧帧并重启 rAF 循环（帧 id 与运行态解耦，避免"假活"死锁） */
    _restartLoop: function () {
      if (this.raf) {
        cancelAnimationFrame(this.raf);
        this.raf = null;
      }
      if (!this.active) return;
      var self = this;
      this.lastT = 0;                 /* 重置 dt 基准，避免恢复时大跳 */
      this.raf = requestAnimationFrame(function (t) { self._step(t); });
    },

    /* 唤醒/退出熄屏：停掉动画循环（常驻定时器保留，用于自愈） */
    stop: function () {
      this.active = false;
      if (this.raf) {
        cancelAnimationFrame(this.raf);
        this.raf = null;
      }
    },

    _step: function (t) {
      var self = this;
      if (!document.body.classList.contains('screen-off-mode')) {
        this.active = false;
        this.raf = null;
        return;
      }
      this.active = true;
      this.lastFrameAt = Date.now();
      try {
        if (!this.lastT) this.lastT = t;
        var dt = Math.min(0.1, (t - this.lastT) / 1000); /* 秒；切页大跳步长钳制 */
        this.lastT = t;

        var w = window.innerWidth, h = window.innerHeight;
        var ew = this.el.offsetWidth || 220, eh = this.el.offsetHeight || 20;
        this.x += this.vx * dt;
        this.y += this.vy * dt;
        /* 边缘反射：撞哪条边取反哪个速度分量（速度大小不变） */
        if (this.x <= 0) { this.x = 0; this.vx = Math.abs(this.vx); }
        if (this.x >= w - ew) { this.x = w - ew; this.vx = -Math.abs(this.vx); }
        if (this.y <= 0) { this.y = 0; this.vy = Math.abs(this.vy); }
        if (this.y >= h - eh) { this.y = h - eh; this.vy = -Math.abs(this.vy); }
        this.el.style.transform =
          'translate(' + this.x.toFixed(1) + 'px,' + this.y.toFixed(1) + 'px)';

        /* 文本每秒刷新一次（日期/星期/时间/天气） */
        var sec = Math.floor(t / 1000);
        if (sec !== this.lastSec) {
          this.lastSec = sec;
          this._render();
        }
      } catch (e) {
        /* 单帧异常不终止循环 */
      }
      this.raf = requestAnimationFrame(function (t2) { self._step(t2); });
    },

    _render: function () {
      var now = new Date();
      var pad = function (n) { return (n < 10 ? '0' : '') + n; };
      var week = ['星期日', '星期一', '星期二', '星期三', '星期四',
        '星期五', '星期六'][now.getDay()];
      this.dEl.textContent = now.getFullYear() + '-' +
        pad(now.getMonth() + 1) + '-' + pad(now.getDate()) + ' ' + week;
      this.tEl.textContent = pad(now.getHours()) + ':' +
        pad(now.getMinutes()) + ':' + pad(now.getSeconds());
      /* 天气：weather 扩展快照；未接入/无数据时占位 */
      var wth = App.latestData && App.latestData.extensions &&
        App.latestData.extensions.weather;
      if (wth && wth.enabled && wth.temperature !== null &&
          wth.temperature !== undefined) {
        this.wEl.textContent = (wth.city || '--') + ' ' +
          Math.round(wth.temperature) + '\u00b0c' +
          (wth.humidity !== null && wth.humidity !== undefined
            ? ' ' + wth.humidity + '%' : '');
      } else {
        this.wEl.textContent = '天气待接入';
      }
      /* 本机电量（Battery Status API；不支持时隐藏该段）
         语义色与面板时间行一致：充电=绿、低电(<20%)=紫、其余=青 */
      if (typeof BatteryInfo !== 'undefined' && BatteryInfo.supported &&
          BatteryInfo.level !== null) {
        var pct = Math.round(BatteryInfo.level * 100);
        this.bEl.style.display = '';
        this.bEl.className = 'b' + (BatteryInfo.charging
          ? ' green' : (pct < 20 ? ' purple' : ''));
        this.bEl.textContent = 'bat ' + pct + '%' +
          (BatteryInfo.charging ? ' \u26a1' : '');
      } else {
        this.bEl.style.display = 'none';
      }
    }
  };

  /* ============================================================
     屏幕常亮（Screen Wake Lock API）
     失败不阻断页面；页面切回前台时自动重新申请。
     ============================================================ */
  var WakeLock = {
    enabled: false,

    init: function () {
      var self = this;
      this.enabled = localStorage.getItem('dash.wakeLock') === '1';
      bus.on('wake-toggle', function (on) {
        self.enabled = on;
        if (on) self.request(); else self.release();
      });
      if (this.enabled) this.request();

      /* 手机息屏会释放锁；回到前台时按需重新申请 */
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden && self.enabled) self.request();
      });
    },

    request: function () {
      var self = this;
      if (!('wakeLock' in navigator)) return;    /* 不支持则静默跳过 */
      navigator.wakeLock.request('screen').then(function (lock) {
        App.wakeLock = lock;
        lock.addEventListener('release', function () { App.wakeLock = null; });
      }).catch(function () { /* 被系统拒绝：不阻断页面运行 */ });
    },

    release: function () {
      if (App.wakeLock) {
        try { App.wakeLock.release(); } catch (e) {}
        App.wakeLock = null;
      }
    }
  };

  /* ============================================================
     全局字号调节：根字号 --fs-root（12~17px），CSS 用 rem 随动缩放。
     选择持久化在 localStorage('dash.fontSize')，
     设置面板通过 step(+1/-1) 调整。
     ============================================================ */
  var FontScale = {
    MIN: 12,
    MAX: 17,
    DEFAULT: 13,
    current: 13,

    init: function () {
      var saved = parseInt(localStorage.getItem('dash.fontSize'), 10);
      this.current = isNaN(saved) ? this.DEFAULT
        : Math.min(this.MAX, Math.max(this.MIN, saved));
      this.apply();
    },

    apply: function () {
      document.documentElement.style.setProperty('--fs-root',
        this.current + 'px');
      localStorage.setItem('dash.fontSize', String(this.current));
      bus.emit('fontsize', this.current);
    },

    step: function (delta) {
      this.current = Math.min(this.MAX,
        Math.max(this.MIN, this.current + delta));
      this.apply();
    }
  };
  D.FontScale = FontScale;

  /* ============================================================
     本机电量显示（安卓 Chrome Battery Status API）
     - navigator.getBattery() 可用时监听 level/charging 变化；
     - 不支持（iOS / 部分桌面浏览器）或被拒时整段自动隐藏；
     - 语义色：充电=绿，低电量(<20%)=紫，其余=青（数据强调）。
     ============================================================ */
  var BatteryInfo = {
    supported: false,
    level: null,        /* 0~1，null 表示未知 */
    charging: false,
    _paints: [],        /* 每个面板时间行注册的绘制函数 */

    init: function () {
      var self = this;
      if (!navigator.getBattery) return;   /* 不支持：保持隐藏 */
      navigator.getBattery().then(function (bat) {
        self.supported = true;
        var update = function () {
          self.level = bat.level;
          self.charging = bat.charging;
          self.paint();
        };
        bat.addEventListener('levelchange', update);
        bat.addEventListener('chargingchange', update);
        update();
      }).catch(function () { self.supported = false; });
    },

    /** TimeLine 为每个面板注册一个电量显示元素 */
    attach: function (valueEl, rootEl) {
      var self = this;
      this._paints.push(function () { self._paintOne(valueEl, rootEl); });
      this._paintAll();
    },

    _paintOne: function (valueEl, rootEl) {
      if (!this.supported || this.level === null) {
        rootEl.style.display = 'none';
        return;
      }
      rootEl.style.display = '';
      var pct = Math.round(this.level * 100);
      valueEl.textContent = pct + '%' + (this.charging ? ' · charging' : '');
      valueEl.className = 'v' +
        (this.charging ? ' green' : (pct < 20 ? ' purple' : ''));
    },

    _paintAll: function () {
      this._paints.forEach(function (fn) { try { fn(); } catch (e) {} });
    },

    paint: function () { this._paintAll(); }
  };

  /* ============================================================
     时间行：每个面板顶部显示实时时间（独立时间面板已取消）
     左侧：TIME · 14:32:05 · 09-06 星期六
     右侧：BAT · 87%（充电时 BAT · 87% · charging）
     单个 1s 定时器驱动全部时间行，页面隐藏时暂停。
     ============================================================ */
  var TimeLine = {
    els: [],
    timer: null,

    init: function () {
      var sections = document.querySelectorAll('#panels-track .panel');
      for (var i = 0; i < sections.length; i++) {
        var head = document.createElement('div');
        head.className = 'panel-head';
        head.innerHTML =
          '<span class="tl-left"><span class="k">time</span> · ' +
          '<span class="v">--:--:--</span></span>' +
          '<span class="tl-bat"><span class="k">bat</span> · ' +
          '<span class="v">--</span></span>';
        /* 面板标题 + 时间日期 + 电量合并为一行：
           [标题（超长截断） ......] [time · 日期 · 星期] [bat · xx%] */
        var title = sections[i].querySelector('.panel-title');
        if (title) {
          sections[i].replaceChild(head, title);
          head.insertBefore(title, head.firstChild);
        } else {
          sections[i].insertBefore(head, sections[i].firstChild);
        }
        this.els.push(head.querySelector('.tl-left .v'));
        /* 右侧电量段（不支持 Battery API 的环境会自动隐藏） */
        BatteryInfo.attach(head.querySelector('.tl-bat .v'),
          head.querySelector('.tl-bat'));
      }
      var self = this;
      this._tick();
      this.timer = setInterval(function () {
        if (!document.hidden) self._tick();
      }, 1000);
    },

    _tick: function () {
      var now = new Date();
      var pad = function (n) { return (n < 10 ? '0' : '') + n; };
      var hm = pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' +
        pad(now.getSeconds());
      var date = pad(now.getMonth() + 1) + '-' + pad(now.getDate());
      var week = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五',
        '星期六'][now.getDay()];
      var text = hm + ' · ' + date + ' ' + week;
      for (var i = 0; i < this.els.length; i++) {
        this.els[i].textContent = text;
      }
    }
  };

  /* ============================================================
     全屏 / Service Worker / 设置入口 / 启动流程
     ============================================================ */
  function setupFullscreen() {
    var btn = document.getElementById('btn-fullscreen');
    var toggle = function () {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(function () {});
      } else {
        document.exitFullscreen().catch(function () {});
      }
    };
    btn.addEventListener('click', toggle);
    bus.on('fullscreen-request', toggle);
    var gear = document.getElementById('btn-settings');
    gear.addEventListener('click', function () {
      Swipe.goTo(Swipe.count() - 1);   /* 设置面板固定为最后一屏 */
    });
  }

  /* 刷新页面按钮：Service Worker 感知的刷新
     普通刷新要两次才能换新版本（第一次装新 SW，第二次才被接管），
     这里点击一次就够：先 reg.update()，等新 SW 接管（controllerchange）
     再 reload；若离线 / 没有新版本 / 浏览器不支持，则超时兜底重载。 */
  function setupRefresh() {
    var btn = document.getElementById('btn-refresh');
    if (!btn) return;

    var reloaded = false;        /* done 标记：最多重载一次 */
    var go = function () {
      if (reloaded) return;
      reloaded = true;
      location.reload();
    };

    btn.addEventListener('click', function () {
      /* 完全不支持 Service Worker：直接重载 */
      if (!('serviceWorker' in navigator)) { go(); return; }

      navigator.serviceWorker.getRegistration()
        .then(function (reg) {
          if (!reg) { go(); return; }               /* 没有注册：直接重载 */
          /* 新 SW 接管页面时再重载，保证拿到的是最新资源 */
          navigator.serviceWorker.addEventListener('controllerchange', go, { once: true });
          /* 主动检查更新（失败不影响重载） */
          if (typeof reg.update === 'function') return reg.update();
        })
        .catch(function () { /* 查询/更新失败都走兜底重载 */ })
        .then(function () {
          /* 兜底：controllerchange 没在 1.2s 内到来也要重载 */
          setTimeout(go, 1200);
        });
    });
  }

  function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    if (location.protocol !== 'http:' && location.protocol !== 'https:') return;
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('service-worker.js')
        .catch(function () { /* 注册失败不影响使用 */ });
    });
  }

  function boot() {
    /* 0. 恢复用户字号（在面板渲染前应用，避免首屏闪动） */
    FontScale.init();

    /* 1. 面板初始化 */
    D.PanelManager.initAll();

    /* 2. 数据事件 -> 渲染所有面板 */
    bus.on('data', function (data) {
      App.latestData = data;
      D.PanelManager.renderAll(data);
    });

    /* 3. 每个面板顶部时间行 + 右侧本机电量（时间面板已取消） */
    BatteryInfo.init();
    TimeLine.init();

    /* 3.5 旋转/缩放窗口后重渲染：ASCII 进度条按新宽度重新计算字符数 */
    var resizeTimer = null;
    window.addEventListener('resize', function () {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        if (App.latestData) D.PanelManager.renderAll(App.latestData);
      }, 200);
    });

    /* 4. 滑动 / 指示器 */
    Swipe.init();
    Swipe.goTo(0);

    /* 5. 防烧屏 / 屏保 / 常亮 */
    BurnIn.init();
    ScreenSaver.init();
    OffWidget.init();
    WakeLock.init();

    /* 6. 刷新按钮 + 全屏按钮 + 设置入口 + SW */
    setupRefresh();
    setupFullscreen();
    registerServiceWorker();

    /* 7. 蓝牙手动连接（设置面板按钮） */
    bus.on('bluetooth-request', function () {
      D.DataService.connectBluetooth();
    });

    /* 8. 连接数据源 */
    D.DataService.start();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

