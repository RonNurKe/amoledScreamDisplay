# -*- coding: utf-8 -*-
"""
autostart_run.py - 开机自启启动器（供计划任务 AMOLED-Dashboard-Backend 调用）

为什么需要这个文件：
  * 计划任务用 pythonw.exe 启动（GUI 子系统，无控制台窗口），但 pythonw 下
    sys.stdout / sys.stderr 是 None，main.py 的所有 print 直接丢失，
    出问题时完全无从排查（autostart.log 长期 0 字节就是这个原因）。
  * 这里在导入 main 之前，把 stdout/stderr 接到 autostart.log，保留日志能力，
    同时依旧无窗口。

用法：
  pythonw.exe autostart_run.py
  （不要用 cmd 的 >> 重定向去接 pythonw，它不产生任何输出）
"""

import io
import os
import socket
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "autostart.log")
LOG_MAX_BYTES = 2 * 1024 * 1024   # 超过 2MB 就截断，避免无限增长


def _already_running(port=8090, host="127.0.0.1"):
    """
    单实例保护：autostart.bat 用 start 立即脱离，任务本身会瞬间"完成"，
    因此计划任务的多实例策略挡不住重复登录时的二次拉起。
    这里用端口探测兜底：8090 已在监听就认为后端活着，安静退出。
    """
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _open_log():
    """追加模式打开日志；过大时先截断（保留本次会话，历史不重要）。"""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.remove(LOG_PATH)
    except OSError:
        pass
    return io.open(LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1)


def _setup_stdio():
    """
    pythonw 下 std 流为 None，替换成日志文件。
    用 python.exe 手动跑时 std 流正常，此时同时写日志（tee 效果不必要，
    保持简单：只有在 std 无效时才接管）。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    log = _open_log()
    sys.stdout = log
    sys.stderr = log
    return log


def main():
    log = _setup_stdio()
    os.chdir(BASE_DIR)
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    if _already_running():
        print("[autostart] 8090 已在监听，后端应已运行，跳过本次启动")
        return 0

    print("\n" + "=" * 56)
    print("[autostart] %s 以 pythonw 方式启动" %
          __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("[autostart] 工作目录: %s" % BASE_DIR)

    try:
        import main as backend_main
    except Exception:
        import traceback
        traceback.print_exc()
        print("[autostart] 导入 main 失败，退出")
        return 1

    try:
        backend_main.main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        print("[autostart] main() 异常退出")
        return 1
    finally:
        try:
            if log is not None:
                log.flush()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
