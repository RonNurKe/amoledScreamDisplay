# 第三方组件与许可声明

本项目（Windows 硬件监控 → AMOLED 手机仪表盘）本体以 **MIT** 许可发布，见 [LICENSE](LICENSE)。

本文列出运行、构建或对接过程中涉及的第三方组件及其许可，并按「是否随本项目分发」
分成两类。**分发本项目时请连同本文件一起保留。**

---

## 一、随本项目一同分发的组件

这些是运行时依赖，会以源码或二进制形式进入你的部署环境（如 `backend/venv/`）。

### 1. psutil — BSD-3-Clause

- 用途：采集 CPU / 内存 / 磁盘 / 网络 / 电池数据（`backend/monitor.py`）
- 版权：Copyright (c) 2009 Giampaolo Rodola'
- 许可：BSD 3-Clause
- 义务：保留版权声明与许可全文，不得用作者名义为衍生品背书
- 与 MIT 兼容，无传染性

### 2. cython-hidapi（PyPI 包名 `hidapi`）— **本项目按 BSD 分支使用**

- 用途：访问键盘厂商自定义 HID 接口（usage page `0xFF60`），读取物理按键矩阵
  （`backend/vialmatrix.py`）
- 版权：Copyright (c) 2010-2021 trezor.io / Signal 11 Software（底层 hidapi C 库）
- 该包**同时提供三种许可，由使用者自行选择**：

  > cython-hidapi can be used under one of three licenses.
  > 1. GPL v3.0
  > 2. A BSD-Style License
  > 3. The more liberal original cython-hidapi license
  >
  > The license chosen is at the discretion of the user.

- **本项目明确选择第 2 项（BSD-Style License）**，理由：BSD 与 MIT 兼容，
  使本项目整体可以保持 MIT，并可闭源分发或商用。

  > ⚠️ 若你（或你的下游）改选 GPL v3.0 分支，则本项目整体将受 GPL-3.0 约束。
  > 这是一个**显式的选择点**，不是默认结果。

- 义务：保留 hidapi 的版权声明与 BSD 许可全文（`venv/Lib/site-packages/hidapi-*.dist-info/LICENSE*`）
- 说明：`hid.cp3xx-win_amd64.pyd` 内含静态编译的 hidapi C 源码，因此上面的选择
  是实质性授权选择，而非形式条款。

---

## 二、不随本项目分发、仅运行时或对接涉及的组件

### 3. Python 3.12 — PSF-2.0

- 用途：后端运行时
- 许可：Python Software Foundation License 2.0（含历史 PSF 条款）
- 本项目不包含 Python 的解释器或标准库源码，仅要求用户安装

### 4. Vial GUI / vial-qmk — GPL-2.0

- 项目：[vial-kb/vial-gui](https://github.com/vial-kb/vial-gui)、[vial-kb/vial-qmk](https://github.com/vial-kb/vial-qmk)
- 用途：**仅用于协议互操作**。本项目向键盘的厂商 HID 接口发送 VIA/Vial 报文
  （`0x02 0x03` 读矩阵、`0xFE 0x05` 查解锁状态），并解析用户自行从 Vial 导出的
  `keymap.vil` 键位文件
- 关系判定：
  - **未链接** Vial 的库，**未包含**其源码，**未修改**其程序
  - 固件运行在键盘 MCU 上，由用户自行刷写，本项目不分发固件二进制
  - 属于 GPL 明确容许的「独立程序通过公开协议通信」（at arm's length）
  - `keymap.vil` 是用户产生的**数据文件**，不受 GPL 约束
- 结论：**GPL-2.0 的传染性不及于本项目**

> 备注：文档中解释锁定机制时使用伪代码描述固件行为，未复制 Vial/via.c 源码。

### 5. opencode — MIT

- 项目：[sst/opencode](https://github.com/sst/opencode)
- 用途：Agents 状态面板的可选数据来源。本项目只**读取** opencode 写入的会话数据库，
  不包含、不链接其任何代码

### 6. opencode-status-sync — MIT

- 项目：[fellow99/opencode-status-sync](https://github.com/fellow99/opencode-status-sync)
- 用途：可选插件。由用户自行安装到 `~/.config/opencode/plugins/`，通过 HTTP
  把 AI 会话状态推给本后端（`/api/agents/<state>`）
- 许可：MIT，无传染性
- 本项目不分发该插件，仅在文档中说明安装方式

### 7. WorkBuddy — 商业闭源软件

- 用途：Agents 状态面板的另一个可选数据来源。本项目只读取其在
  `~/.workbuddy/projects/**/*.jsonl` 下生成的会话日志，无代码依赖

---

## 三、前端资源

`frontend/` 下全部 HTML / CSS / JavaScript / SVG 图标均为本项目自行编写，
**不依赖任何 CDN、框架或第三方库**；字体使用操作系统自带的等宽字体族，
不内嵌也不分发字体文件。因此前端部分无第三方许可义务。

---

## 附：

| 组件 | 许可 | 传染性 | 形态 |
|---|---|---|---|
| 本项目 | MIT | — | 本体 |
| psutil | BSD-3-Clause | 无 | 依赖，随分发 |
| cython-hidapi | **BSD（本项目选定）** / GPL-3 / 原许可 三选一 | 选 BSD 则无 | 依赖，随分发 |
| Python 3.12 | PSF-2.0 | 无 | 运行时 |
| Vial GUI / vial-qmk | GPL-2.0 | 有，但不及于本项目 | 未包含，协议通信 |
| opencode | MIT | 无 | 未包含，仅读数据 |
| opencode-status-sync | MIT | 无 | 未包含，用户自装 |
| WorkBuddy | 商业闭源 | 无 | 未包含，仅读数据 |
| 前端全部资源 | 本项目 MIT | — | 自写，零外部依赖 |
