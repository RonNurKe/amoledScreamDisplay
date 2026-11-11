/* ============================================================
   bluetooth.js - Web Bluetooth 封装
   职责：
   1. 检测浏览器是否支持 Web Bluetooth、是否处于安全上下文；
   2. 请求设备 -> 连接 GATT -> 获取服务/特征 -> 订阅 notify；
   3. 把蓝牙收到的数据（UTF-8 文本，按换行分包）解析为 JSON 对象；
   4. 断线事件回调（由 app.js 决定是否重连/降级）；
   5. 所有失败场景返回清晰的中文错误说明。

   Android Chrome 限制（详见 docs/bluetooth.md）：
   - 必须在 HTTPS 或 localhost 安全上下文中才能使用；
   - 必须由用户点击（手势）触发 requestDevice；
   - 需要系统蓝牙开关 + 位置权限（部分安卓版本要求）；
   - notify 数据按 MTU 分片（约 20 字节），后端需按行分包发送。
   ============================================================ */

(function () {
  'use strict';

  /** 是否支持 Web Bluetooth API */
  function isSupported() {
    return typeof navigator !== 'undefined' && 'bluetooth' in navigator;
  }

  /** 是否处于安全上下文（HTTPS / localhost） */
  function isSecure() {
    return typeof window !== 'undefined' && window.isSecureContext === true;
  }

  /**
   * 把 Web Bluetooth 的 DOMException 翻译成用户能看懂的中文提示。
   * @param {Error} err
   * @returns {string}
   */
  function friendlyError(err) {
    var name = err && err.name;
    switch (name) {
      case 'NotFoundError':
        return '未找到匹配的蓝牙设备（请确认设备已开启/在附近，或用户取消了选择）';
      case 'SecurityError':
        return '安全策略阻止：Web Bluetooth 需要 HTTPS 或 localhost 环境';
      case 'NotAllowedError':
        return '用户拒绝了蓝牙选择器或权限请求';
      case 'AbortError':
        return '蓝牙连接被中止';
      case 'InvalidStateError':
        return '浏览器状态异常（可能未开启系统蓝牙）';
      default:
        return '蓝牙错误: ' + ((err && (err.message || err.name)) || String(err));
    }
  }

  /**
   * 蓝牙客户端。
   * @param {Object} options
   *   serviceUuid        蓝牙服务 UUID（与后端 config.json 保持一致）
   *   characteristicUuid 通知特征 UUID
   *   namePrefix         可选：设备名前缀过滤
   */
  function BluetoothClient(options) {
    options = options || {};
    this.serviceUuid = options.serviceUuid;
    this.characteristicUuid = options.characteristicUuid;
    this.namePrefix = options.namePrefix || '';

    this.device = null;
    this.characteristic = null;
    this._buffer = '';            // notify 分片重组缓冲（按 \n 切分 JSON）
    this._decoder = new TextDecoder('utf-8');

    /* 回调（由外部赋值） */
    this.onData = null;           // function(dataObject)
    this.onDisconnect = null;     // function()
    this.onLog = null;            // function(message) 调试输出
  }

  /** 内部日志 */
  BluetoothClient.prototype._log = function (msg) {
    if (typeof this.onLog === 'function') this.onLog('[bt] ' + msg);
  };

  /**
   * 连接流程：请求设备 -> GATT 连接 -> 服务 -> 特征 -> 订阅通知。
   * 注意：requestDevice 必须由用户手势（点击）直接或间接调用。
   * @returns {Promise<void>} 失败时 reject(Error)，message 为中文提示。
   */
  BluetoothClient.prototype.connect = function () {
    var self = this;

    if (!isSupported()) {
      return Promise.reject(new Error(
        '当前浏览器不支持 Web Bluetooth（需 Android Chrome 56+，且页面为 HTTPS/localhost）'));
    }
    if (!isSecure()) {
      return Promise.reject(new Error(
        '当前页面不是安全上下文：Web Bluetooth 仅在 HTTPS 或 localhost 下可用'));
    }

    /* 构造设备过滤器：优先按服务 UUID 过滤 */
    var requestOptions;
    if (self.serviceUuid) {
      requestOptions = { filters: [{ services: [self.serviceUuid] }] };
      if (self.namePrefix) {
        requestOptions.filters.push({ namePrefix: self.namePrefix });
      }
    } else {
      requestOptions = { acceptAllDevices: true };
    }

    self._log('正在请求设备...');
    return navigator.bluetooth.requestDevice(requestOptions)
      .then(function (device) {
        self.device = device;
        self._log('已选择设备: ' + (device.name || device.id));
        /* 断线监听：提前注册，掉线时通知外层决定重连或降级 */
        device.addEventListener('gattserverdisconnected', function () {
          self._log('GATT 连接已断开');
          self.characteristic = null;
          if (typeof self.onDisconnect === 'function') self.onDisconnect();
        });
        /* GATT 连接（设备可能还在广播，稍等片刻重试一次以提高成功率） */
        return device.gatt.connect();
      })
      .then(function (server) {
        self._log('GATT 已连接，正在获取服务...');
        return server.getPrimaryService(self.serviceUuid);
      })
      .then(function (service) {
        self._log('已获取服务，正在获取特征...');
        return service.getCharacteristic(self.characteristicUuid);
      })
      .then(function (characteristic) {
        self.characteristic = characteristic;
        characteristic.addEventListener('characteristicvaluechanged',
          function (event) { self._onValue(event); });
        self._log('正在订阅 notify ...');
        return characteristic.startNotifications();
      })
      .then(function () {
        self._buffer = '';
        self._log('订阅成功，开始接收数据');
      })
      .catch(function (err) {
        /* 清理半连接状态 */
        self._cleanup();
        var msg = friendlyError(err);
        self._log(msg);
        throw new Error(msg);
      });
  };

  /**
   * notify 数据到达：BLE 每次_notify_约 20 字节（MTU 限制），
   * 后端按「一行一个 JSON」发送，这里负责把分片拼接完整后逐行解析。
   */
  BluetoothClient.prototype._onValue = function (event) {
    var value = event.target.value; // DataView
    var text = this._decoder.decode(value);
    this._buffer += text;

    var idx;
    while ((idx = this._buffer.indexOf('\n')) >= 0) {
      var line = this._buffer.slice(0, idx).trim();
      this._buffer = this._buffer.slice(idx + 1);
      if (!line) continue;
      try {
        var data = JSON.parse(line);
        if (typeof this.onData === 'function') this.onData(data);
      } catch (e) {
        this._log('JSON 解析失败，丢弃不完整数据行');
      }
    }
    /* 防御：缓冲异常增长（对端未按行发送）时截断 */
    if (this._buffer.length > 64 * 1024) this._buffer = '';
  };

  /** 主动断开连接。 */
  BluetoothClient.prototype.disconnect = function () {
    this._cleanup();
  };

  BluetoothClient.prototype._cleanup = function () {
    var self = this;
    if (self.characteristic) {
      try { self.characteristic.stopNotifications(); } catch (e) { /* 忽略 */ }
      self.characteristic = null;
    }
    if (self.device && self.device.gatt && self.device.gatt.connected) {
      try { self.device.gatt.disconnect(); } catch (e) { /* 忽略 */ }
    }
    self.device = null;
  };

  /* 挂到全局命名空间，供 app.js / panels.js 使用 */
  window.Dashboard = window.Dashboard || {};
  window.Dashboard.BluetoothClient = BluetoothClient;
  window.Dashboard.bluetoothEnv = {
    isSupported: isSupported,
    isSecure: isSecure,
    friendlyError: friendlyError
  };
})();
