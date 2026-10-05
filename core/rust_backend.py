"""Rust 音频后端（IPC 客户端）。

与独立进程 xiatiao-audio-backend 通过 Unix domain socket +
JSON Lines 通信。本类实现 AudioBackend 接口，可被 PlayerCore 持有。

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
from .ipc_protocol import Cmd, Evt, Field

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
        #: 已接受的播放代次（Position 事件按此过滤过期事件）。
        self._pos_gen = 0
        #: 期望位置（秒）——切歌/切音质后的「目标位置」。设置后，同代次的
        #: position 只接受接近它的（±_EXPECT_TOLERANCE），直到达成或超时。
        #: 用于跳过切换瞬间后端可能残留的旧位置（gen 无法覆盖的窗口）。
        self._expected_pos: Optional[float] = None
        #: 期望位置的达成时间戳（单调时钟），用于超时兜底（防 seek/加载失败
        #: 导致位置永不达成而永久卡住）。
        self._expected_deadline = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._alive = False
        self._send_lock = threading.Lock()
        # 保护「读线程是否活跃」标志：防止 _start_backend 与 _try_reconnect
        # 并发启动两个 _read_loop 读同一 socket（buf 各自累积→事件重复/错乱）。
        # 锁只覆盖标志读写，不跨 _read_loop 的阻塞循环，故不会死锁。
        self._reader_lock = threading.Lock()
        self._reader_active = False
        # 主动关闭标志：shutdown 时置 True，_read_loop 断开不再触发自愈。
        self._closing = False
        # 上次自愈重启的时间戳（单调时钟，秒），用于限流。
        self._last_restart = 0.0
        # 自愈重启的最小间隔（秒）：避免后端持续崩溃时疯狂重启。
        self._restart_interval = 3.0

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
            # Popen 已把 fd dup 给子进程，父进程不再需要 _out；
            # 显式关闭避免频繁自愈重启时累积文件描述符。
            # _out 可能是 DEVNULL 整数（无 close），故先判断。
            if hasattr(_out, "close"):
                try:
                    _out.close()
                except Exception:
                    pass
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
                except Exception as exc:
                    # 连接失败（socket 刚出现可能尚未 ready）：记录原因便于诊断。
                    log.debug("连接后端 socket 失败（将重试）: %s", exc)
            time.sleep(0.1)

        if not self._alive:
            log.warning("连接 Rust 后端 socket 失败: %s", self._socket_path)
            return
        log.info("已连接后端: %s", self._socket_path)

        self._start_reader()

    def _start_reader(self) -> None:
        """启动读线程；若已有活跃 reader 则跳过（防重复）。"""
        with self._reader_lock:
            if self._reader_active:
                log.debug("读线程已活跃，跳过重复启动")
                return
            self._reader_active = True
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
                except OSError as exc:
                    # 权限不足或进程已退出：记录，不阻断后续清理。
                    log.debug("清理残留进程 pid=%s 失败: %s", pid, exc)
        except Exception as exc:
            log.debug("清理残留进程失败: %s", exc)
        # 删旧 socket
        try:
            if os.path.exists(self._socket_path):
                os.remove(self._socket_path)
        except OSError as exc:
            log.debug("删除旧 socket 失败: %s", exc)
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
        # 读线程退出：清活跃标志，允许后续重连再起新 reader。
        with self._reader_lock:
            self._reader_active = False
        # 非主动关闭 → 后端意外断开：通知 UI 并尝试自愈重启。
        if not self._closing:
            self._handle_disconnect()

    def _handle_disconnect(self) -> None:
        """后端断开：发信号通知 UI，并在后台尝试重启进程 + 重连。

        限流：距上次重启不足 _restart_interval 秒则不再重启（避免后端
        持续崩溃时疯狂重启）。重启后恢复关键状态（音量/DSP/DSD/输出设备）。
        """
        log.warning("检测到后端断开（非主动关闭），尝试自愈")
        # 通知 UI（主线程信号）
        GLib.idle_add(self._emit_backend_lost)
        now = time.monotonic()
        if now - self._last_restart < self._restart_interval:
            log.debug("自愈限流：距上次重启 %.1fs，跳过", now - self._last_restart)
            return
        self._last_restart = now
        # 后台线程重启，避免阻塞读线程退出
        t = threading.Thread(target=self._restart_backend, daemon=True)
        t.start()

    def _emit_backend_lost(self) -> bool:
        """主线程：发 backend-lost 信号。"""
        try:
            self.emit("backend-lost", "音频后端已断开，正在尝试恢复")
        except Exception:
            pass
        return False

    def _restart_backend(self) -> None:
        """重启后端进程并恢复关键状态。"""
        try:
            # 关闭旧资源
            try:
                if self._sock is not None:
                    self._sock.close()
            except Exception as exc:
                log.debug("重启前关闭旧 socket 失败: %s", exc)
            self._sock = None
            try:
                if self._proc is not None:
                    self._proc.kill()
            except Exception as exc:
                log.debug("重启前终止旧进程失败: %s", exc)
            self._proc = None
            # 重启 + 重连
            self._start_backend()
            if not self._alive:
                log.warning("后端自愈失败：无法重连")
                return
            log.info("后端自愈成功，恢复状态")
            self._restore_state_after_restart()
        except Exception as exc:
            log.warning("后端自愈异常: %s", exc)

    def _restore_state_after_restart(self) -> None:
        """重启后恢复关键状态到新后端进程。"""
        # 恢复失败 = 重启后状态丢失（音量/DSP/DSD/设备），属重要问题，
        # 用 warning 而非静默，便于排查自愈后的异常行为。
        try:
            if self._volume is not None:
                self._send({Field.CMD: Cmd.SET_VOLUME, Field.VALUE: float(self._volume)})
        except Exception as exc:
            log.warning("自愈后恢复音量失败: %s", exc)
        try:
            if self._dsp:
                self._send({Field.CMD: Cmd.SET_DSP, Field.PARAMS: dict(self._dsp)})
        except Exception as exc:
            log.warning("自愈后恢复 DSP 失败: %s", exc)
        try:
            self._send({Field.CMD: Cmd.SET_DSD_MODE, Field.MODE: self._dsd_mode or "auto"})
        except Exception as exc:
            log.warning("自愈后恢复 DSD 模式失败: %s", exc)
        try:
            self._send({Field.CMD: Cmd.SET_OUTPUT_DEVICE, Field.NAME: self._output_device or ""})
        except Exception as exc:
            log.warning("自愈后恢复输出设备失败: %s", exc)

    def _dispatch(self, evt: dict) -> bool:
        """主线程：把后端事件转为信号。"""
        name = evt.get(Field.EVENT)
        # 位置事件太频繁，降噪：仅在非 position 时打印
        if name != "position":
            log.debug("[IPC←] %s", json.dumps(evt, ensure_ascii=False))
        if name == Evt.POSITION:
            # 播放代次过滤：切歌/切音质后，旧代次的 position 事件可能仍在路上，
            # 若不过滤会把进度条拉回旧值（回 0 再跳回 / 闪回上一首进度）。
            gen = evt.get(Field.GEN)
            if gen is not None:
                gen = int(gen)
                if gen != self._pos_gen:
                    # 新代次：接受并记录。比新代次旧的（<）直接丢弃。
                    if gen > self._pos_gen:
                        self._pos_gen = gen
                    else:
                        return False
            pos = float(evt.get(Field.SEC, 0.0))
            # 期望位置过滤：切歌/切音质后，同代次的 position 只接受接近
            # 目标位置的（跳过切换瞬间残留的旧位置）。达成或超时即解除。
            exp = self._expected_pos
            if exp is not None:
                import time as _t
                # 注意：gen 变化（切歌/切音质都会 gen+1）时**不清除期望**——
                # 期望位置在新代次里依然有效（切歌期望 0、切音质期望原进度）。
                # 仅当「位置接近期望」或「超时」才解除。
                if abs(pos - exp) <= self._EXPECT_TOLERANCE:
                    # 达成目标位置：恢复正常跟随。
                    log.debug("[expect] 达成→接受（pos=%.3f exp=%.3f）", pos, exp)
                    self._expected_pos = None
                elif _t.monotonic() >= self._expected_deadline:
                    # 超时兜底：放弃过滤，避免位置永不达成导致永久卡住。
                    log.debug("[expect] 超时兜底→接受（pos=%.3f exp=%.3f）", pos, exp)
                    self._expected_pos = None
                else:
                    # 仍在过渡中且位置远离目标：忽略本次（不更新进度）。
                    log.debug("[expect] 过滤丢弃（pos=%.3f exp=%.3f 差=%.3f）",
                              pos, exp, abs(pos - exp))
                    return False
            self._position = pos
            self.emit("position-update", self._position)
        elif name == Evt.DURATION:
            self.emit("duration-changed", float(evt.get(Field.SEC, 0.0)))
        elif name == Evt.STATE:
            self._state = evt.get(Field.STATE, PlayerState.STOPPED)
            self.emit("play-state-changed", self._state)
        elif name == Evt.END_OF_STREAM:
            self.emit("end-of-stream")
        elif name == Evt.ERROR:
            self.emit("error-occur", str(evt.get(Field.MESSAGE, "")))
        elif name == Evt.AUDIO_INFO:
            info = evt.get(Field.INFO) or {}
            if isinstance(info, dict):
                self._audio_info = info
                self.emit("audio-info", info)
        elif name == Evt.EFFECT:
            self._effect = evt.get(Field.PRESET, "off")
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
            s.sendall((json.dumps({Field.CMD: Cmd.LIST_OUTPUT_DEVICES}) + "\n").encode("utf-8"))
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
                    if evt.get(Field.EVENT) == Evt.OUTPUT_DEVICES:
                        return [d for d in (evt.get(Field.DEVICES) or [])
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
            # 重启读线程（_start_reader 内部防重复）
            self._start_reader()
            return True
        except Exception as exc:
            log.debug("重连后端失败: %s", exc)
            return False

    def _send(self, obj: dict) -> bool:
        """发送一条 IPC 命令。返回是否确实写入 socket。

        返回 False 表示后端不可用（未启动/未连接），命令已被丢弃——
        调用方据此可向用户明确报错，而非静默失败。
        """
        log.debug("[IPC→] %s", json.dumps(obj, ensure_ascii=False))
        if not self._alive or self._sock is None:
            # 断线：尝试重连一次
            if not self._try_reconnect():
                log.warning("[IPC→] 丢弃(未连接): %s", obj.get(Field.CMD))
                return False
        try:
            with self._send_lock:
                # 锁内取本地引用：避免 self._sock 在检查后、发送前被
                # 后台 _restart_backend 置 None（TOCTOU），导致 AttributeError。
                sock = self._sock
                if sock is None:
                    log.warning("[IPC→] 丢弃(连接已关闭): %s", obj.get(Field.CMD))
                    return False
                sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))
                return True
        except Exception as exc:
            log.warning("发送 IPC 命令失败: %s", exc)
            return False

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
        if not self._send({Field.CMD: Cmd.PLAY, Field.PATH: path}):
            # 后端未启动/未连接：命令被丢弃。明确告知用户并回退状态，
            # 避免静默失败后由「卡住检测」给出笼统的「播放失败，已跳过」。
            self._state = PlayerState.STOPPED
            self._position = 0.0
            self.emit("error-occur", "音频后端未连接（未启动或已断开），请检查后端是否就绪")
            return False
        return True

    def pause(self) -> None:
        log.info("[API] pause")
        # 立即本地更新状态，避免依赖异步事件导致 UI 判断滞后
        self._state = PlayerState.PAUSED
        self._send({Field.CMD: Cmd.PAUSE})

    def resume(self) -> None:
        log.info("[API] resume")
        self._state = PlayerState.PLAYING
        self._send({Field.CMD: Cmd.RESUME})

    def stop(self) -> None:
        self._send({Field.CMD: Cmd.STOP})
        self._position = 0.0

    def seek_seconds(self, seconds: float) -> None:
        self._send({Field.CMD: Cmd.SEEK, Field.SECONDS: max(0.0, float(seconds))})

    #: 期望位置达成容差（秒）。
    _EXPECT_TOLERANCE = 1.0
    #: 期望位置超时（秒）：超过仍未达成则强制放弃过滤（防永久卡死）。
    _EXPECT_TIMEOUT = 3.0

    def expect_position(self, seconds: float) -> None:
        """声明「目标位置」：之后的同代次 position 只接受接近它的。

        用于切歌（target=0）/ 切音质（target=当前进度）：跳过切换瞬间
        后端可能残留的旧位置（gen 过滤无法覆盖「新代次 + 旧值」窗口）。
        位置达成（±容差）或超时（防卡死）后自动恢复正常跟随。
        """
        try:
            import time as _t
            self._expected_pos = max(0.0, float(seconds))
            self._expected_deadline = _t.monotonic() + self._EXPECT_TIMEOUT
            log.debug("[expect] 设置期望位置=%.3fs（容差%.1f 超时%.1fs）",
                      self._expected_pos, self._EXPECT_TOLERANCE, self._EXPECT_TIMEOUT)
        except Exception:
            self._expected_pos = None

    def set_volume(self, v: float) -> None:
        v = max(0.0, min(1.0, float(v)))
        self._volume = v
        self._send({Field.CMD: Cmd.SET_VOLUME, Field.VALUE: v})

    def set_effect(self, preset: str) -> None:
        self._effect = preset or "off"
        self._send({Field.CMD: Cmd.SET_EFFECT, Field.PRESET: self._effect})

    def set_dsp(self, params: dict) -> None:
        """下发完整 DSP 参数（实时生效）。"""
        self._dsp = dict(params or {})
        self._send({Field.CMD: Cmd.SET_DSP, Field.PARAMS: self._dsp})

    def set_camilla_yaml(self, yaml_str: str) -> None:
        """下发嵌入式 camillalib 配置（YAML 字符串）。"""
        if not yaml_str:
            return
        self._send({Field.CMD: Cmd.SET_CAMILLA_YAML, Field.YAML: yaml_str})

    def set_coloring(self, tube_drive: float, bbe_amount: float) -> None:
        """下发音色染色参数（电子管 drive / BBE amount）。"""
        log.debug("下发音色染色: td=%s ba=%s alive=%s",
                  tube_drive, bbe_amount, self._alive)
        self._send({Field.CMD: Cmd.SET_COLORING,
                    Field.TUBE_DRIVE: float(tube_drive),
                    Field.BBE_AMOUNT: float(bbe_amount)})

    def reset_dsp(self) -> None:
        """重置 DSP 到默认参数。"""
        self._send({Field.CMD: Cmd.RESET_DSP})

    def set_dsd_mode(self, mode: str) -> None:
        """下发 DSD 输出模式（auto/native/dop/pcm）。"""
        self._dsd_mode = mode or "auto"
        self._send({Field.CMD: Cmd.SET_DSD_MODE, Field.MODE: self._dsd_mode})

    def set_output_device(self, name: str) -> None:
        """下发输出设备（PipeWire sink 名；空=默认）。"""
        self._output_device = name or ""
        self._send({Field.CMD: Cmd.SET_OUTPUT_DEVICE, Field.NAME: self._output_device})

    def is_alive(self) -> bool:
        """后端进程/连接是否可用（未启动或已断开 → False）。

        供启动时与播放前做就绪检测，避免静默失败。
        """
        return bool(self._alive and self._sock is not None)

    def state(self) -> str:
        return self._state

    def position(self) -> float:
        return self._position

    def effect(self) -> str:
        return self._effect

    def audio_info(self) -> dict:
        return dict(self._audio_info)

    def shutdown(self) -> None:
        self._closing = True
        self._send({Field.CMD: Cmd.SHUTDOWN})
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
