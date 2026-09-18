"""Rust 音频后端（IPC 客户端）。

与独立进程 xiatiao-audio-backend 通过 Unix domain socket +
JSON Lines 通信。本类实现 AudioBackend 接口，因此可被 PlayerCore
无缝替换（方法/信号与 GstBackend 一致）。

生命周期：
- 首次构造时尝试启动 Rust 进程；
- 找不到可执行文件时降级为「不可用」状态（方法空转，不崩溃）；
- shutdown() 时关闭 socket 并终止子进程。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from typing import Optional

from gi.repository import GLib

from .audio_backend import AudioBackend, PlayerState

log = logging.getLogger(__name__)

#: 默认 socket 路径（与 Rust 侧默认值一致）
DEFAULT_SOCKET = "/tmp/xiatiao-audio-backend.sock"


def _find_binary() -> Optional[str]:
    """定位 Rust 后端可执行文件。

    查找顺序（兼容「开发」与「安装后」两种布局）：
      1. 与 main.py 同目录（.deb 安装后：/usr/lib/xiatiao-player/）；
      2. 开发构建产物 audio_backend_rs/target/{release,debug}/；
      3. 系统 PATH（额外安装到 /usr/bin 等场景）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    bin_name = "xiatiao-audio-backend"
    candidates = [
        # 安装后：后端与 main.py 同目录（/usr/lib/xiatiao-player/）。
        os.path.join(project_root, bin_name),
        # 开发：cargo 构建产物（优先 release）。
        os.path.join(project_root, "audio_backend_rs", "target", "release", bin_name),
        os.path.join(project_root, "audio_backend_rs", "target", "debug", bin_name),
        # 系统 PATH 兜底。
        os.path.join("/usr/bin", bin_name),
        os.path.join("/usr/local/bin", bin_name),
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # 最后：在 PATH 中查找。
    import shutil as _shutil
    return _shutil.which(bin_name)


class RustBackend(AudioBackend):
    """基于 Rust 独立进程的音频后端。"""

    __gtype_name__ = "RustBackend"

    def __init__(self) -> None:
        super().__init__()
        self._state = PlayerState.STOPPED
        self._position = 0.0
        self._volume = 1.0
        self._effect = "off"
        self._dsp: dict = {}
        self._audio_info: dict = {}
        self._dsd_mode = "auto"
        self._output_device = ""

        self._socket_path = os.environ.get("XIATIAO_BACKEND_SOCKET", DEFAULT_SOCKET)
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._alive = False
        self._send_lock = threading.Lock()

        self._start_backend()

    # ------------------------------------------------------------
    # 进程与连接管理
    # ------------------------------------------------------------
    def _start_backend(self) -> None:
        binary = _find_binary()
        if binary is None:
            log.warning("未找到 Rust 后端可执行文件，RustBackend 不可用（请先 cargo build）")
            return
        # 清理上一次的残留：旧 socket + 可能残留的旧后端进程
        self._cleanup_stale(binary)
        try:
            env = dict(os.environ)
            env["XIATIAO_BACKEND_SOCKET"] = self._socket_path
            # 后端日志：始终写入 /tmp/xiatiao-backend.log（便于诊断）。
            # 用 "wb" 截断，保证每次启动是干净的日志。
            try:
                _out = open("/tmp/xiatiao-backend.log", "wb")
            except Exception:
                _out = subprocess.DEVNULL
            self._proc = subprocess.Popen(
                [binary], env=env,
                stdout=_out, stderr=_out,
            )
        except Exception as exc:
            log.warning("启动 Rust 后端失败: %s", exc)
            return

        # 等待新 socket 出现并连接（最多约 5 秒）
        for _ in range(50):
            if os.path.exists(self._socket_path):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self._socket_path)
                    # 读线程用阻塞 recv；不设 timeout，避免把“无数据”误判为断开。
                    self._sock = s
                    self._alive = True
                    break
                except Exception:
                    pass
            time.sleep(0.1)

        if not self._alive:
            log.warning("连接 Rust 后端 socket 失败: %s", self._socket_path)
            return
        log.info("已连接后端: %s", self._socket_path)

        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True
        )
        self._reader_thread.start()

    def _process_uses_socket(self, pid_dir: str, want_sock: str) -> bool:
        """判断某进程的环境变量 XIATIAO_BACKEND_SOCKET 是否指向 want_sock。

        用于清理残留后端时区分实例：读不到环境（权限/已退）时返回 False，
        即保守地「不杀」，避免误杀另一个实例。
        """
        try:
            with open(os.path.join(pid_dir, "environ"), "rb") as f:
                raw = f.read()
        except OSError:
            return False
        prefix = b"XIATIAO_BACKEND_SOCKET="
        for item in raw.split(b"\x00"):
            if item.startswith(prefix):
                try:
                    val = item[len(prefix):].decode("utf-8", "ignore")
                except Exception:
                    return False
                return os.path.abspath(val) == want_sock
        return False

    def _cleanup_stale(self, binary: str) -> None:
        """清理上一次的残留：杀掉旧后端进程 + 删旧 socket。

        非正常关闭时旧后端可能残留，占着 socket 导致新后端 bind 失败。

        安全约束：只清理「命令行/环境指向当前 socket」的进程，
        避免误杀另一个实例的后端（不同 socket）。
        """
        # 杀掉「与本实例同一 socket」的旧后端进程
        try:
            import glob
            base = os.path.basename(binary)
            want_sock = os.path.abspath(self._socket_path)
            for pid_dir in glob.glob("/proc/[0-9]*"):
                pid = os.path.basename(pid_dir)
                if pid == str(os.getpid()):
                    continue
                try:
                    exe = os.readlink(os.path.join(pid_dir, "exe"))
                except OSError:
                    continue
                if os.path.basename(exe) != base:
                    continue
                # 进一步校验：该进程的环境变量 socket 是否与本实例一致。
                # 读不到 environ（权限/进程已退）时保守起见跳过，不杀。
                if not self._process_uses_socket(pid_dir, want_sock):
                    continue
                try:
                    os.kill(int(pid), 9)    # SIGKILL：与原行为一致，确保残留必被清除
                    log.info("清理残留后端进程 pid=%s", pid)
                except OSError:
                    pass
        except Exception as exc:
            log.debug("清理残留进程失败: %s", exc)
        # 删旧 socket
        try:
            if os.path.exists(self._socket_path):
                os.remove(self._socket_path)
        except OSError:
            pass
        time.sleep(0.1)

    def _read_loop(self) -> None:
        """后台线程：读取后端事件，转成主线程信号。"""
        buf = b""
        while self._alive and self._sock is not None:
            try:
                data = self._sock.recv(4096)
            except socket.timeout:
                # 超时是正常的（后端平时无事件推送），继续等待，
                # 绝不能当成“断开”而触发重连。
                continue
            except OSError as exc:
                log.debug("_read_loop 异常断开: %s: %s", type(exc).__name__, exc)
                break
            if not data:
                log.debug("_read_loop 对端关闭(recv空)")
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                GLib.idle_add(self._dispatch, evt)
        self._alive = False

    def _dispatch(self, evt: dict) -> bool:
        """主线程：把后端事件转为信号。"""
        name = evt.get("event")
        # 位置事件太频繁，降噪：仅在非 position 时打印
        if name != "position":
            log.info("[IPC←] %s", json.dumps(evt, ensure_ascii=False))
        if name == "position":
            self._position = float(evt.get("sec", 0.0))
            self.emit("position-update", self._position)
        elif name == "duration":
            self.emit("duration-changed", float(evt.get("sec", 0.0)))
        elif name == "state":
            self._state = evt.get("state", PlayerState.STOPPED)
            self.emit("play-state-changed", self._state)
        elif name == "end_of_stream":
            self.emit("end-of-stream")
        elif name == "error":
            self.emit("error-occur", str(evt.get("message", "")))
        elif name == "audio_info":
            info = evt.get("info") or {}
            if isinstance(info, dict):
                self._audio_info = info
                self.emit("audio-info", info)
        elif name == "effect":
            self._effect = evt.get("preset", "off")
            self.emit("effect-changed", self._effect)
        return False

    def list_output_devices(self, timeout: float = 3.0) -> list:
        """枚举可用的 ALSA 硬件输出设备（同步短连接直读）。

        返回 [{"id": "hw:...", "description": "..."}, ...]。
        后端不可用或超时时返回空列表。

        说明：用独立的一次性 socket 连接直接读响应，绕开主线程事件队列。
        若复用主连接 + 等 event，在 UI 主线程调用会与 GLib.idle_add
        派发互相阻塞，导致永远拿不到结果（下拉为空）。
        """
        if not os.path.exists(self._socket_path):
            log.warning("后端 socket 不存在，无法获取设备列表")
            return []
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self._socket_path)
        except Exception as exc:
            log.warning("连接后端失败（获取设备列表）: %s", exc)
            return []
        try:
            s.sendall((json.dumps({"cmd": "list_output_devices"}) + "\n").encode("utf-8"))
            buf = b""
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        evt = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    if evt.get("event") == "output_devices":
                        return [d for d in (evt.get("devices") or [])
                                if isinstance(d, dict)]
        except Exception as exc:
            log.warning("获取设备列表失败: %s", exc)
        finally:
            try:
                s.close()
            except Exception:
                pass
        return []

    def _try_reconnect(self) -> bool:
        """尝试重新连接后端 socket（断线后自愈）。"""
        if not os.path.exists(self._socket_path):
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self._socket_path)
            old = self._sock
            self._sock = s
            self._alive = True
            try:
                if old is not None:
                    old.close()
            except Exception:
                pass
            log.info("已重连后端: %s", self._socket_path)
            # 重启读线程
            t = threading.Thread(target=self._read_loop, daemon=True)
            t.start()
            return True
        except Exception as exc:
            log.debug("重连后端失败: %s", exc)
            return False

    def _send(self, obj: dict) -> None:
        log.info("[IPC→] %s", json.dumps(obj, ensure_ascii=False))
        if not self._alive or self._sock is None:
            # 断线：尝试重连一次
            if not self._try_reconnect():
                log.warning("[IPC→] 丢弃(未连接): %s", obj.get("cmd"))
                return
        try:
            with self._send_lock:
                self._sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except Exception as exc:
            log.warning("发送 IPC 命令失败: %s", exc)

    # ------------------------------------------------------------
    # AudioBackend 接口
    # ------------------------------------------------------------
    def play_file(self, path: str) -> bool:
        log.info("[API] play_file: %s", path)
        if not path:
            self.emit("error-occur", "播放地址为空")
            return False
        is_url = "://" in path
        if not is_url and not os.path.isfile(path):
            self.emit("error-occur", f"文件不存在: {path}")
            return False
        # 立即本地进入播放态，UI 按钮能马上响应
        self._state = PlayerState.PLAYING
        self._position = 0.0
        self._send({"cmd": "play", "path": path})
        return True

    def pause(self) -> None:
        log.info("[API] pause")
        # 立即本地更新状态，避免依赖异步事件导致 UI 判断滞后
        self._state = PlayerState.PAUSED
        self._send({"cmd": "pause"})

    def resume(self) -> None:
        log.info("[API] resume")
        self._state = PlayerState.PLAYING
        self._send({"cmd": "resume"})

    def stop(self) -> None:
        self._send({"cmd": "stop"})
        self._position = 0.0

    def seek_seconds(self, seconds: float) -> None:
        self._send({"cmd": "seek", "seconds": max(0.0, float(seconds))})

    def set_volume(self, v: float) -> None:
        v = max(0.0, min(1.0, float(v)))
        self._volume = v
        self._send({"cmd": "set_volume", "value": v})

    def set_effect(self, preset: str) -> None:
        self._effect = preset or "off"
        self._send({"cmd": "set_effect", "preset": self._effect})

    def set_dsp(self, params: dict) -> None:
        """下发完整 DSP 参数（实时生效）。"""
        self._dsp = dict(params or {})
        self._send({"cmd": "set_dsp", "params": self._dsp})

    def set_camilla_yaml(self, yaml_str: str) -> None:
        """下发嵌入式 camillalib 配置（YAML 字符串）。"""
        if not yaml_str:
            return
        self._send({"cmd": "set_camilla_yaml", "yaml": yaml_str})

    def set_engine(self, camilla: bool = False) -> None:
        """兼容接口：Camilla 是否参与由后端按 DSP 参数自动推导，此命令仅占位。"""
        self._send({"cmd": "set_engine", "camilla": bool(camilla)})

    def set_coloring(self, tube_drive: float, bbe_amount: float) -> None:
        """下发音色染色参数（电子管 drive / BBE amount）。"""
        log.debug("下发音色染色: td=%s ba=%s alive=%s",
                  tube_drive, bbe_amount, self._alive)
        self._send({"cmd": "set_coloring",
                    "tube_drive": float(tube_drive),
                    "bbe_amount": float(bbe_amount)})

    def reset_dsp(self) -> None:
        """重置 DSP 到默认参数。"""
        self._send({"cmd": "reset_dsp"})

    def set_dsd_mode(self, mode: str) -> None:
        """下发 DSD 输出模式（auto/native/dop/pcm）。"""
        self._dsd_mode = mode or "auto"
        self._send({"cmd": "set_dsd_mode", "mode": self._dsd_mode})

    def set_output_device(self, name: str) -> None:
        """下发输出设备（PipeWire sink 名；空=默认）。"""
        self._output_device = name or ""
        self._send({"cmd": "set_output_device", "name": self._output_device})

    def state(self) -> str:
        return self._state

    def position(self) -> float:
        return self._position

    def effect(self) -> str:
        return self._effect

    def audio_info(self) -> dict:
        return dict(self._audio_info)

    def shutdown(self) -> None:
        self._send({"cmd": "shutdown"})
        self._alive = False
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                # 缩短等待：关闭时不阻塞主线程太久（后端进程会自行退出）
                self._proc.wait(timeout=0.3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
