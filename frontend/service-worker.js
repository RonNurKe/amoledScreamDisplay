/* ============================================================
   service-worker.js - 前端静态资源缓存
   策略：
   - 预缓存核心静态资源，支持离线打开基础页面；
   - /api/ 接口永不缓存（实时数据不经过 SW 缓存）；
   - 其余 GET 请求：缓存优先，未命中时走网络并写入运行时缓存；
   - WebSocket 连接不受 Service Worker 影响。
   ============================================================ */

var CACHE_NAME = 'amoled-dashboard-v44';

/* 安装时预缓存的核心资源列表 */
var PRECACHE = [
  './',
  './index.html',
  './style.css',
  './theme.css',
  './app.js',
  './panels.js',
  './bluetooth.js',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-maskable-512.png'
];

/* 安装：预缓存全部核心资源 */
self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      /* 单个资源失败不阻断整体安装 */
      return Promise.all(PRECACHE.map(function (url) {
        return cache.add(url).catch(function () { /* 忽略 */ });
      }));
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

/* 激活：清理旧版本缓存 */
self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        if (key !== CACHE_NAME) return caches.delete(key);
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

/* 请求拦截 */
self.addEventListener('fetch', function (event) {
  var req = event.request;

  /* 只处理同源 GET 请求 */
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  /* 实时数据接口：直连网络，绝不读缓存 */
  if (url.pathname.indexOf('/api/') === 0) return;

  event.respondWith(
    caches.match(req).then(function (cached) {
      if (cached) {
        /* 后台静默更新（stale-while-revalidate 简化版） */
        fetch(req).then(function (resp) {
          if (resp && resp.ok) {
            caches.open(CACHE_NAME).then(function (c) {
              c.put(req, resp);
            });
          }
        }).catch(function () { /* 离线时忽略 */ });
        return cached;
      }
      /* 未命中缓存：走网络并写入运行时缓存 */
      return fetch(req).then(function (resp) {
        if (resp && resp.ok && resp.type === 'basic') {
          var clone = resp.clone();
          caches.open(CACHE_NAME).then(function (c) { c.put(req, clone); });
        }
        return resp;
      }).catch(function () {
        /* 导航请求离线兜底：返回缓存的 index.html */
        if (req.mode === 'navigate') {
          return caches.match('./index.html');
        }
        return Response.error();
      });
    })
  );
});
