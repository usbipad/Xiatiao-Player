"""音频可视化数据管线（方案乙：Rust 旁路写 FIFO，Python 侧 FFT）。

链路：
    Rust 引擎 ──(降采样单声道 PCM)──► FIFO ──► 本模块读取线程
                                                    │
                                            纯 Python FFT + 分 bin
                                                    │
                                            幅度数组（供渲染器）

设计要点：
- FIFO 数据走内核内存（$XDG_RUNTIME_DIR 是 tmpfs），不落盘；
- 读取在独立线程，读慢/断流只影响可视化，不影响播放；
- 纯 Python FFT（无 numpy 依赖），窗口/bin 数可配。
"""
from __future__ import annotations

import bisect
import cmath
import logging
import math
import os
import struct
import threading
import time
from collections import deque
from typing import List, Optional

try:
    import numpy as _np  # FFT 走底层 C，比纯 Python 快 50-100 倍
    _HAS_NUMPY = True
except Exception:  # noqa: BLE001
    _np = None
    _HAS_NUMPY = False

log = logging.getLogger(__name__)

#: 应用标识：仅用于拼运行时目录，包名变更时改这一处（须与 Rust 侧一致）。
APP_ID = "xiatiao"

#: 旁路采样率（须与 Rust viz.rs::VIZ_RATE 一致）
VIZ_RATE = 16000

#: 默认参数（FPS / FFT 窗口 / bin 数）。
#  无 numpy 时纯 Python FFT 很贵：把默认 FPS 降到 30，避免常驻吃满一核。
DEFAULT_FPS = 60 if _HAS_NUMPY else 30
DEFAULT_FFT_WINDOW = 1024
DEFAULT_BIN_COUNT = 32
#: 滑窗步进（样本）：每 hop 个样本产出一帧。
#  256=16ms@16kHz ≈ 62.5 帧/秒，与 GTK 渲染节拍（~60fps）匹配，
#  避免「渲染快、产帧慢」导致插值退化成重复帧（视觉卡顿）。
#  纯 Python FFT 时成本翻倍，故 numpy 下用 256、无 numpy 退回 512。
DEFAULT_HOP = 256 if _HAS_NUMPY else 512


def viz_fifo_path() -> str:
    """解析 FIFO 路径（与 Rust engine.rs::viz_fifo_path 保持一致）。"""
    env = os.environ.get("XIATIAO_VIZ_FIFO")
    if env:
        return env
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, APP_ID, "viz.fifo")
    for base in ("/dev/shm", "/tmp"):
        if os.path.isdir(base):
            return os.path.join(base, APP_ID, "viz.fifo")
    return os.path.join("/tmp", APP_ID, "viz.fifo")


def _fft(samples: List[float]) -> List[complex]:
    """迭代式基-2 FFT（要求长度是 2 的幂）。纯 Python 实现，16kHz/1024 点足够。"""
    n = len(samples)
    if n & (n - 1) != 0:
        raise ValueError("FFT 长度必须是 2 的幂")
    # 位反转置换
    j = 0
    a = [complex(x, 0.0) for x in samples]
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    # 蝶形
    length = 2
    while length <= n:
        ang = -2.0 * math.pi / length
        wlen = cmath.exp(complex(0, ang))
        for i in range(0, n, length):
            w = 1 + 0j
            half = length >> 1
            for k in range(half):
                u = a[i + k]
                v = a[i + k + half] * w
                a[i + k] = u + v
                a[i + k + half] = u - v
                w *= wlen
        length <<= 1
    return a


def _hann(n: int) -> List[float]:
    """汉宁窗，减少频谱泄漏。"""
    if n <= 1:
        return [1.0]
    return [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1)) for i in range(n)]


class VizPipeline:
    """FIFO 读取 + FFT 管线。在独立线程读取，主线程按 FPS 取最新幅度数组。

    用法：
        pipe = VizPipeline()
        pipe.start()
        # 在 GTK 定时器里：
        data = pipe.latest()   # List[float]，长度 = bin_count
    """

    def __init__(
        self,
        fps: Optional[int] = None,
        fft_window: Optional[int] = None,
        bin_count: Optional[int] = None,
    ) -> None:
        # 未显式传入时从 settings 读取（键：viz_fps / viz_fft_window / viz_bin_count）
        fps, fft_window, bin_count = self._resolve_params(fps, fft_window, bin_count)
        self.fps = fps
        self.fft_window = fft_window
        self.bin_count = bin_count
        # 滑窗步进：每 hop 个样本产出一帧（越小越实时，CPU 越高）
        self.hop = self._resolve_hop(self.fps)
        # 排列方式：freq（频率）/ musical（音阶）
        self.scale_mode = self._resolve_scale_mode()
        # 采样率（音阶排列需要；旁路固定 VIZ_RATE）
        self.sample_rate = VIZ_RATE
        # 音画同步延迟（毫秒）：频谱整体延后，与耳朵听到的声音对齐
        self.sync_delay_ms = self._resolve_sync_delay()
        # 延迟缓冲：存 (时间戳, bins)，latest() 取 delay 毫秒前那一帧
        self._delay_buf = deque()
        # 与 _delay_buf 同步的时间戳序列，供 latest() 用 bisect 只读定位
        self._delay_times: List[float] = []
        self._window = _hann(self.fft_window)
        self._lock = threading.Lock()
        self._latest: List[float] = [0.0] * self.bin_count
        self._thread: Optional[threading.Thread] = None
        self._alive = False
        self._status = "idle"  # idle / reading / error

    @staticmethod
    def _resolve_params(fps, fft_window, bin_count):
        try:
            from config.settings import get_config
            cfg = get_config()
        except Exception:
            cfg = None

        def _int(v, key, default):
            if v is not None:
                return int(v)
            if cfg is not None:
                return cfg.get_int(key, default)
            return default

        f = max(1, _int(fps, "viz_fps", DEFAULT_FPS))
        w = max(64, _int(fft_window, "viz_fft_window", DEFAULT_FFT_WINDOW))
        # FFT 窗口必须是 2 的幂
        if w & (w - 1) != 0:
            w = 1 << (w.bit_length() - 1)
        b = max(1, _int(bin_count, "viz_bin_count", DEFAULT_BIN_COUNT))
        return f, w, b

    @staticmethod
    def _resolve_scale_mode() -> str:
        try:
            from config.settings import get_config
            mode = get_config().get_str("viz_scale_mode", "freq")
        except Exception:
            mode = "freq"
        return "musical" if mode == "musical" else "freq"

    @staticmethod
    def _resolve_sync_delay() -> float:
        try:
            from config.settings import get_config
            ms = float(get_config().get("viz_sync_delay_ms", 100.0))
        except Exception:
            ms = 100.0
        return max(0.0, min(500.0, ms))

    @staticmethod
    def _resolve_hop(fps: Optional[int] = None) -> int:
        """解析滑窗步进 hop。

        核心：hop 决定 FFT 产帧率 = VIZ_RATE / hop。
        产帧率必须 ≥ 渲染率，否则渲染端会拿到重复帧（视觉卡顿）。

        优先用显式 viz_hop 配置；未配置时按 fps 推导（hop ≈ VIZ_RATE/fps），
        保证产帧率与渲染节拍匹配。取 2 的幂（FFT 友好），范围 [64, 4096]。
        """
        try:
            from config.settings import get_config
            cfg = get_config()
            hop = cfg.get_int("viz_hop", DEFAULT_HOP)
        except Exception:
            cfg = None
            hop = DEFAULT_HOP
        # fps 明确时，优先按 fps 推导 hop（hop ≈ VIZ_RATE/fps），
        # 保证产帧率 ≥ 渲染率；这样「采样 FPS」滑块才真正决定流畅度。
        # 注意 viz_hop 有默认值，无法用「是否 None」判断用户意图，
        # 故一律以 fps 为准（UI 侧 fps 变更会同步写回 viz_hop）。
        if fps:
            target = max(1, int(VIZ_RATE / max(1, int(fps))))
            hop = target
        # hop 取 2 的幂
        h = max(64, min(4096, int(hop)))
        return 1 << (h.bit_length() - 1)

    def set_params(self, fps=None, fft_window=None, bin_count=None) -> None:
        """运行时更新参数（重建窗函数与 bin 缓冲）。读取线程正在用时也安全。"""
        f, w, b = self._resolve_params(fps, fft_window, bin_count)
        h = self._resolve_hop(f)
        mode = self._resolve_scale_mode()
        delay = self._resolve_sync_delay()
        with self._lock:
            self.fps = f
            self.fft_window = w
            self.bin_count = b
            self.hop = h
            self.scale_mode = mode
            self.sync_delay_ms = delay
            self._delay_buf.clear()
            self._delay_times = []
            self._window = _hann(w)
            self._latest = [0.0] * b

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._thread is not None:
            return
        self._alive = True
        self._thread = threading.Thread(target=self._read_loop, name="viz-fifo", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._alive = False
        t = self._thread
        self._thread = None
        if t is not None:
            # 读线程是 daemon：_alive=False 后会自行退出，不必 join。
            # 之前 join(timeout=0.2) 会阻塞主线程最多 200ms，导致退出沉浸页时
            # 动画/切页明显延迟（线程常阻塞在 f.read，不会立刻退出）。
            # 这里只给极短等待，几乎不卡主线程。
            try:
                t.join(timeout=0.01)
            except Exception:
                pass

    def status(self) -> str:
        with self._lock:
            return self._status

    def latest(self) -> List[float]:
        """返回「音画同步延迟」时刻的幅度（按相邻帧线性插值）。

        delay=0 时等价于返回最新帧。缓冲不足时退化为最近一帧。

        只读查询：不修改缓冲区（早期实现用 popleft 边查边丢，导致
        60fps 渲染端高频调用时把缓冲掏空、插值基准断裂，频谱一卡一卡）。
        缓冲区修剪统一交给生产者 _push_frame 负责。
        """
        with self._lock:
            delay = self.sync_delay_ms
            buf = self._delay_buf
            if not buf:
                return list(self._latest)
            if delay <= 0:
                return list(buf[-1][1])
            # target 基于「缓冲最新帧的时间戳」往回推，而非墙上时钟：
            # 虚拟时间由采样位置推导，可能略落后于 monotonic（写入/FIFO 抖动），
            # 若用墙上时钟，小 delay 时 target 会越过最新帧 → 无帧可插值 → 跳变。
            target = buf[-1][0] - delay / 1000.0
            # 只读二分查找：找到第一个时间戳 > target 的帧作为 t1，
            # 其前一帧即 t0；不 pop、不改变缓冲。
            n = len(buf)
            if n == 1:
                return list(buf[0][1])
            # 用 bisect 在时间戳序列上定位
            idx = bisect.bisect_right(self._delay_times, target)
            if idx <= 0:
                # target 比最早帧还早（启动初期）：返回最早一帧
                return list(buf[0][1])
            if idx >= n:
                # target 比最新帧还晚：返回最新一帧
                return list(buf[-1][1])
            # idx 处为 t1；t0 = idx-1
            t0, v0 = buf[idx - 1]
            t1, v1 = buf[idx]
            dt = t1 - t0
            if dt <= 1e-9:
                return list(v0)
            frac = (target - t0) / dt
            if frac < 0.0:
                frac = 0.0
            elif frac > 1.0:
                frac = 1.0
            return [a + (b - a) * frac for a, b in zip(v0, v1)]

    # ---- 读取线程 ----
    def _read_loop(self) -> None:
        path = viz_fifo_path()
        # 确保父目录存在（Rust 侧只 mkfifo 不了目录）
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass
        # 创建 FIFO 节点（已存在则忽略）
        try:
            if not os.path.exists(path):
                os.mkfifo(path, 0o600)
        except OSError as exc:
            log.debug("mkfifo 失败: %s", exc)
            with self._lock:
                self._status = "error"
            return

        # 滑窗模型：环形缓冲保存最近 fft_window 个样本；
        # 每积累 hop 个新样本就做一次 FFT，数据连续。
        #
        # 关键（消除脉冲卡顿）：
        # 1) 读块固定为 1024 字节（= Rust 侧 VizTap 单次写入 256 样本 × 4），
        #    保证每次 read 都能拿到 Rust 一次写入的数据，不再「攒够 4096
        #    才返回」造成一批多帧、然后长时间阻塞（脉冲式「走一下停一下」）。
        # 2) 用 pending 累积，每凑够 hop_bytes 才产一帧：产帧节拍由 hop 决定，
        #    与 Rust 写入粒度、读块大小解耦。
        ring = bytearray()          # 最近样本（字节，f32），保留最近一窗
        pending = bytearray()       # 已读、待凑够 hop 的字节
        # 数据流时间基准：FIFO 是连续 16kHz 流，每 hop 样本对应固定时长
        # hop/VIZ_RATE 秒。用「累计样本数」推导帧时间戳，保证产帧时间均匀。
        samples_seen = 0            # 已处理样本累计（决定虚拟时间）
        time_base = None            # 首个产帧时的 monotonic，用于对齐渲染时基
        while self._alive:
            try:
                # 阻塞打开读端；Rust 侧非阻塞写端连上后才开始有数据
                with open(path, "rb", buffering=0) as f:
                    with self._lock:
                        self._status = "reading"
                    while self._alive:
                        # 固定小读块，避免大块阻塞读的脉冲（见上注释）
                        chunk = f.read(1024)
                        if not chunk:
                            # 写端关闭（无播放）；跳出重开
                            break
                        pending.extend(chunk)
                        with self._lock:
                            win_bytes = self.fft_window * 4   # f32
                            hop_bytes = max(4, self.hop * 4)
                        # 每凑够一个 hop 就产一帧（节拍由 hop 决定）
                        while len(pending) >= hop_bytes:
                            step = pending[:hop_bytes]
                            del pending[:hop_bytes]
                            ring.extend(step)
                            # 环形缓冲只保留最近 win_bytes 个字节
                            if len(ring) > win_bytes:
                                del ring[: len(ring) - win_bytes]
                            # 用样本位置推进虚拟时间
                            samples_seen += hop_bytes // 4
                            # 攒够一窗才分析（前几帧先填满）
                            if len(ring) >= win_bytes:
                                if time_base is None:
                                    time_base = time.monotonic() - samples_seen / VIZ_RATE
                                vtime = time_base + samples_seen / VIZ_RATE
                                self._analyze(bytes(ring), vtime)
            except OSError as exc:
                log.debug("FIFO 读取异常: %s", exc)
                with self._lock:
                    self._status = "error"
                return
            # 写端关闭，等待后重开（保持线程存活，等下次播放）
            with self._lock:
                self._status = "idle"
            if self._alive:
                threading.Event().wait(0.2)
        return

    def _analyze(self, raw: bytes, vtime: Optional[float] = None) -> None:
        """对一窗 PCM 做 FFT，归并成 bin_count 个幅度。

        raw 的长度已由读取循环对齐到当前 fft_window；这里取当前窗函数，
        若长度不匹配（窗口刚变更的过渡帧）则取较小长度，不丢弃。

        vtime：本帧对应的「数据流时间戳」（由采样位置推导，均匀递增）。
               为 None 时回退用 time.monotonic()（兼容旧调用）。
        """
        n = len(raw) // 4
        if n < 2:
            return
        # 取 2 的幂长度做 FFT
        m = 1
        while m * 2 <= n:
            m *= 2
        with self._lock:
            bin_count = self.bin_count
        if _HAS_NUMPY:
            # numpy 路径：全程 numpy，避免逐元素转 Python 对象（GIL 持有时间）
            arr = _np.frombuffer(raw[: n * 4], dtype=_np.float32)[:m].astype(_np.float64)
            win = self._get_window_np(m)          # 缓存窗函数
            spec = _np.fft.rfft(arr * win)
            mags = _np.abs(spec)                   # 保持 numpy 数组，不 tolist
            with self._lock:
                scale_mode = self.scale_mode
                rate = self.sample_rate
            bins = self._merge_bins_np(mags, bin_count, scale_mode, rate)
            self._push_frame(bins.tolist(), vtime)   # 带（数据流）时间戳入延迟缓冲
        else:
            # 纯 Python 回退
            samples = struct.unpack(f"<{n}f", raw[: n * 4])
            with self._lock:
                win = self._window if len(self._window) == m else _hann(m)
            framed = [samples[i] * win[i] for i in range(m)]
            spec = _fft(framed)
            half = len(spec) // 2
            mags = [abs(spec[i]) for i in range(half)]
            bins = self._merge_bins(mags, bin_count)
            self._push_frame(bins, vtime)

    def _push_frame(self, bins: List[float], vtime: Optional[float] = None) -> None:
        """把一帧幅度推入延迟缓冲，并修剪过期数据。

        vtime：数据流时间戳（均匀递增）。None 时用 time.monotonic()。
        用数据流时间而非入队时刻，避免一批多帧时间戳挤在一起。
        """
        now = vtime if vtime is not None else time.monotonic()
        with self._lock:
            self._latest = list(bins)
            self._delay_buf.append((now, list(bins)))
            self._delay_times.append(now)
            # 修剪：只保留 delay + 500ms 内的数据（生产者负责，消费者只读）。
            # 时间戳序列与 _delay_buf 必须严格等长：bisect 得到的 idx
            # 要能直接索引 _delay_buf，故两者一起按同一长度修剪。
            cutoff = now - (self.sync_delay_ms / 1000.0) - 0.5
            while self._delay_buf and self._delay_buf[0][0] < cutoff:
                self._delay_buf.popleft()
            # 按剩余帧数对齐时间戳（丢弃最老的）
            extra = len(self._delay_times) - len(self._delay_buf)
            if extra > 0:
                del self._delay_times[:extra]

    def _get_window_np(self, m: int):
        """缓存汉宁窗（按长度），避免每帧重算。"""
        if _HAS_NUMPY:
            cache = getattr(self, "_win_np_cache", None)
            if cache is None:
                cache = {}
                self._win_np_cache = cache
            w = cache.get(m)
            if w is None:
                w = _np.hanning(m)
                cache[m] = w
            return w
        return None

    def _merge_bins_np(self, mags, bins: int, scale_mode: str = "freq", rate: int = VIZ_RATE):
        """numpy 向量化归并：把频谱归并成 bins 个（取每段最大值）。

        scale_mode:
          - "freq"：x 轴按频率对数均分（现状）
          - "musical"：x 轴按音高（半音）排，每个柱对应一个音高段

        用 np.maximum.reduceat 一次完成分段最大值，避免 Python 循环。
        """
        n = int(mags.shape[0])
        if n <= 0:
            return _np.zeros(bins, dtype=_np.float64)
        if scale_mode == "musical":
            edges = self._musical_edges(n, bins, rate)
        else:
            edges = self._freq_edges(n, bins)
        starts = edges[:-1]
        valid = starts < n
        out = _np.zeros(bins, dtype=_np.float64)
        if valid.any():
            idx = starts[valid]
            vals = _np.maximum.reduceat(mags, idx)
            out[valid] = vals
        return out

    @staticmethod
    def _freq_edges(n: int, bins: int):
        """频率对数均分边界。"""
        edges = _np.round(_np.power(n, _np.arange(bins + 1) / bins) - 1).astype(_np.int64)
        _np.clip(edges, 0, n - 1, out=edges)
        for i in range(1, len(edges)):
            if edges[i] <= edges[i - 1]:
                edges[i] = edges[i - 1] + 1
        _np.clip(edges, 0, n, out=edges)
        return edges

    @staticmethod
    def _musical_edges(n: int, bins: int, rate: int):
        """音阶（半音）边界：把可听频段按半音均分，边界映射到 FFT bin 索引。

        以 A4=440Hz 为参考，频率 f 的音高（半音）p = 12*log2(f/440)。
        在 [f_min, f_max] 的听感频段上均匀取 bins+1 个音高点，反解频率、再换算 bin。
        """
        import math as _m
        nyq = rate / 2.0
        f_min = 40.0                      # 下限频率
        f_max = min(nyq * 0.95, 16000.0)  # 上限频率
        if f_max <= f_min:
            return VizPipeline._freq_edges(n, bins)
        # 音高（相对 A4 的半音数）范围
        p_min = 12.0 * _m.log2(f_min / 440.0)
        p_max = 12.0 * _m.log2(f_max / 440.0)
        pitches = _np.linspace(p_min, p_max, bins + 1)
        freqs = 440.0 * _np.power(2.0, pitches / 12.0)
        # 频率 → FFT bin 索引（rfft 长度 = n，对应 0..nyq）
        idx = _np.round(freqs / nyq * n).astype(_np.int64)
        _np.clip(idx, 0, n - 1, out=idx)
        for i in range(1, len(idx)):
            if idx[i] <= idx[i - 1]:
                idx[i] = idx[i - 1] + 1
        _np.clip(idx, 0, n, out=idx)
        return idx

    def _merge_bins(self, mags: List[float], bins: int = None) -> List[float]:
        """把 FFT 幅度谱归并成 bin_count 个，用对数间隔（低频密、高频疏）。"""
        n = len(mags)
        if n <= 0:
            return [0.0] * (bins or self.bin_count)
        out: List[float] = []
        if bins is None:
            bins = self.bin_count
        for b in range(bins):
            lo = int(round((n ** (b / bins)) - 1))
            hi = int(round((n ** ((b + 1) / bins)) - 1))
            lo = max(0, min(n - 1, lo))
            hi = max(lo + 1, min(n, hi))
            seg = mags[lo:hi]
            out.append(max(seg) if seg else 0.0)
        return out
