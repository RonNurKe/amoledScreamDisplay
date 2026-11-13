# Windows 硬件监控 → AMOLED 手机仪表盘

用于旧手机重复利用的面板功能：Windows 硬件信息 + Agents 状态（opencode+workbuddy）、Token 用量、天气、键盘实时键位图（个人使用的cornix分体）。

- **后端** Python 3.12（标准库 + psutil + hidapi），采集数据、推 WebSocket、托管前端
- **前端** 原生 HTML/CSS/JS（无框架、无 CDN），安卓 Chrome / PWA
- **传输** 局域网 WebSocket 推送，断线自动降级 HTTP 轮询

## 快速开始

```bat
:: 1) 启动后端（首次会自动建 venv 装依赖）
backend\start_backend.bat
```

已配好登录自启的话，开机约 40 秒后它会自己起来，日志在 `backend\autostart.log`。

```text
2) 手机 Chrome 打开控制台打印的地址
   http://<电脑IP>:8090   →  菜单 → 添加到主屏幕
```

```bash
# 3) 探活
curl -s http://127.0.0.1:8090/api/status
```

## 面板

系统监控 · **Agents 状态** · Token 用量 · 天气 · **键盘键位图** · 扩展 · 设置

Token 面板统计两路本地用量（`opencode.db` + WorkBuddy 会话日志），顶部 `[ALL]/[OC]/[WB]`
切来源、`[SESSION]/[MODEL]` 切维度；合并视图里明细行带 `[OC]`/`[WB]` 徽标。

每个面板顶部有实时时间与本机电量；20 秒无操作降低亮度、40 秒熄屏（键盘面板不参与，
边打边看不黑屏）。

## 问题排查

| 现象 | 处理 |
|---|---|
| 键盘切层不动、层号一直停在 0 | **Vial 锁定态**。Vial → `Security` → `Unlock`，按住 **A + S** 约 5 秒，然后关掉 Vial。原理见「Vial 锁定」一节（`README.full.md`，本地完整手册） |
| 后端没起来 / 起来了没按键 | 看 `backend\autostart.log`；必须**登录用户**身份运行，SYSTEM 会落到会话 0 收不到按键 |
| 改了前端没生效 | 点键盘面板顶部的 `[ RELOAD ]`（Service Worker 缓存，当前 `v43`） |
| 手机打不开页面 | 同一局域网、关掉路由 AP 隔离；防火墙放行 TCP **8090 / 8091 / 8092** |
| 端口被占用 | 改 `backend\config.json` 的 `static_port` / `websocket_port` |

## 端口与端点

| 端口 | 用途 |
|---|---|
| `8090` | 前端静态资源 + `/api/*` + `/api/status` |
| `8091` | 主数据 WebSocket（2 秒一轮推送） |
| `8092` | 键盘按键 WebSocket（Raw Input 实时） |

Agents 状态推送（给插件/脚本用）：`GET|POST /api/agents/<state>?source=&sid=&title=&project=`，
`<state>` 取 `thinking` / `reading` / `writing` / `working` / `idle` / `error`。

## 致谢

这个项目站在几个很扎实的东西上，谢谢它们的作者：

**Vial / VIA** — 键盘实时键位图和切层能读到毫秒级，靠的是 Vial 在 QMK 之上公开的
VIA/Vial raw HID 协议（`0x02 0x03` 读矩阵、`0xFE 0x05` 查解锁状态）。没有这份协议文档
和开源固件，本项目的「方案 B 矩阵直读」根本无从下手。
[`vial-kb/vial-qmk`](https://github.com/vial-kb/vial-qmk)（GPL-2.0）· [`vial-kb/vial-gui`](https://github.com/vial-kb/vial-gui)（GPL-2.0）

> 本项目只按公开协议与键盘通信，不包含 Vial 的任何代码，也不分发固件二进制；
> 详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

**opencode-status-sync** — Agents 状态面板最初的形状由它定义：那套
`thinking / reading / writing / working / idle / error` 的状态语义和推送约定来自这个插件。
现在的后端已泛化成多来源（另外还支持 WorkBuddy、codex、cline 等），但协议设计仍然是它的。
[`fellow99/opencode-status-sync`](https://github.com/fellow99/opencode-status-sync)（MIT）

**hidapi / cython-hidapi**（[`hidapi/hidapi`](https://github.com/hidapi/hidapi)、
[`trezor/cython-hidapi`](https://github.com/trezor/cython-hidapi)）— 跨平台 HID 访问的事实标准，
Windows 上能稳定敲厂商自定义接口全靠它。

**psutil** — 由 Giampaolo Rodola 维护，Windows 上采集 CPU / 内存 / 磁盘 / 网络
最省心的选择。

以及 [QMK](https://github.com/qmk/qmk_firmware)、[VIA](https://github.com/the-via/app)
和 [opencode](https://github.com/sst/opencode) 这几个把生态撑起来的项目。

## 许可

本项目以 **MIT** 许可发布，见 [LICENSE](LICENSE)。

第三方组件的许可与义务见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
其中有一处**需要显式选择**：`cython-hidapi` 提供「GPL-3.0 / BSD / 原许可」三选一，
本项目声明按 **BSD 分支**使用，与 MIT 兼容，因此可自由分发、可闭源商用。
若你改选 GPL-3.0 分支，则整体将受 GPL-3.0 约束。

