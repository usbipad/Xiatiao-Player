"""DLNA 推送端（DMC 控制器）：把本地曲目推送到局域网 DLNA 渲染器播放。

组成：
- SSDP 发现：组播 M-SEARCH 搜索 MediaRenderer，收集设备列表；
- SOAP 控制：对选中设备发 SetAVTransportURI / Play / Pause / Stop；
- HTTP 文件服务：把本地音频文件暴露成 http://本机IP:端口/file?path=... ，
  供渲染器拉流播放（渲染器无法访问本机文件系统，必须经 HTTP）。

说明：推送时本机不播放（B1 模式）。
"""
from __future__ import annotations

import html
import logging
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

#: SSDP 组播地址
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

#: 发现的目标类型（MediaRenderer）
ST_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"


# ============================================================
# 工具
# ============================================================
def _is_usable_lan_ip(ip: str) -> bool:
    """是否是可用于局域网组播的真实地址。

    排除：
    - 回环 127.x
    - 链路本地 169.254.x
    - 保留/基准测试段 198.18.0.0/15（Clash/Mihomo 等代理的 fake-ip 网段）
    这些都不是真实局域网地址，SSDP 从它们出去会扫不到设备。
    """
    try:
        parts = [int(x) for x in ip.split(".")]
        if len(parts) != 4:
            return False
        if ip.startswith("127.") or ip.startswith("169.254."):
            return False
        # 198.18.0.0/15：代理 fake-ip / RFC2544 基准测试段
        if parts[0] == 198 and parts[1] in (18, 19):
            return False
        return True
    except Exception:
        return False


def local_ip() -> str:
    """获取本机局域网 IPv4 地址（跳过代理/虚拟网段）。

    优先用「真实私有网段」的网卡地址（192.168/10/172.16）。
    注意：不能只用 connect(8.8.8.8) 探测——若系统挂了代理（Clash/Mihomo
    等），流量被接管，getsockname() 会拿到代理虚拟 IP（如 198.18.0.1），
    导致 SSDP 组播从错误网卡出去、扫不到设备。
    """
    candidates = []
    # 1) 枚举所有网卡地址
    try:
        import subprocess
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=3)
        for line in out.stdout.splitlines():
            # 形如：3: wlp0s20f3    inet 192.168.1.41/24 ...
            toks = line.split()
            for i, t in enumerate(toks):
                if t == "inet" and i + 1 < len(toks):
                    ip = toks[i + 1].split("/")[0]
                    if _is_usable_lan_ip(ip):
                        candidates.append(ip)
    except Exception:
        pass
    # 优先私有网段
    def _rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3
    if candidates:
        candidates.sort(key=_rank)
        return candidates[0]
    # 2) 回退：connect 探测（可能拿到代理 IP，但聊胜于无）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ============================================================
# SSDP 发现
# ============================================================
def _lan_interfaces() -> List[str]:
    """枚举可用于局域网组播的真实网卡地址（跳过代理/虚拟/回环段）。

    运行期从 `ip addr` 读取，不硬编码网卡名——任何机器、任何代理都通用。
    返回按「私有网段优先级」排序的地址列表（192.168 优先）。
    """
    addrs = []
    try:
        import subprocess
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=3)
        for line in out.stdout.splitlines():
            toks = line.split()
            for i, t in enumerate(toks):
                if t == "inet" and i + 1 < len(toks):
                    ip = toks[i + 1].split("/")[0]
                    if _is_usable_lan_ip(ip):
                        addrs.append(ip)
    except Exception:
        pass

    def _rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3
    addrs.sort(key=_rank)
    # 去重保序
    seen = set()
    out_list = []
    for a in addrs:
        if a not in seen:
            seen.add(a)
            out_list.append(a)
    return out_list


def discover_renderers(timeout: float = 3.0) -> List[Dict[str, str]]:
    """SSDP 搜索局域网内的 MediaRenderer。

    **对每张「可用局域网网卡」各发一次 M-SEARCH**（不硬编码网卡名，运行期
    枚举）——这样即使系统挂了代理（Clash/Mihomo 等抢路由），也能从真实
    网卡发出、收到响应。

    返回 [{"name", "location", "udn", "control_url", "ip"}, ...]。
    """
    # 用 ssdp:all 查询：部分设备（如小爱音箱）不按标准 MediaRenderer ST
    # 响应，只对 ssdp:all 响应。收全部设备后，再按设备描述的 deviceType
    # 过滤出 MediaRenderer（见 _fetch_device_info）。
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: ssdp:all\r\n"
        "\r\n"
    ).encode()
    found: Dict[str, Dict[str, str]] = {}

    ifaces = _lan_interfaces()
    if not ifaces:
        # 无可枚举网卡：退回不绑定（系统默认路由）。
        ifaces = [""]

    deadline = time.time() + timeout
    for bind_ip in ifaces:
        remain = deadline - time.time()
        if remain <= 0:
            break
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 关键：把组播出口绑到该网卡地址，绕开代理抢走的路由。
            if bind_ip:
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                    socket.inet_aton(bind_ip))
                except Exception:
                    pass
            sock.settimeout(max(0.2, min(remain, 1.0)))
            sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        except Exception as exc:
            log.debug("SSDP 发送失败（网卡 %s）: %s", bind_ip, exc)
            continue

        # 收该网卡的响应（收到本轮剩余时间用完为止）
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError:
                break
            text = data.decode("utf-8", "ignore")
            headers = _parse_ssdp_headers(text)
            location = headers.get("location", "")
            if not location or location in found:
                continue
            info = _fetch_device_info(location)
            if info is None:
                continue
            info["location"] = location
            info["ip"] = addr[0]
            found[location] = info
        try:
            sock.close()
        except Exception:
            pass

    return list(found.values())


def _parse_ssdp_headers(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip()
    return out


def _fetch_device_info(location: str, timeout: float = 3.0) -> Optional[Dict[str, str]]:
    """拉取设备描述 XML，提取名称 + AVTransport 控制 URL。"""
    try:
        with urllib.request.urlopen(location, timeout=timeout) as resp:
            xml = resp.read().decode("utf-8", "ignore")
    except Exception as exc:
        log.debug("拉取设备描述失败 %s: %s", location, exc)
        return None
    # 显式按 deviceType 过滤：只保留 MediaRenderer。
    # （用 ssdp:all 查询会收到多种设备，需据此剔除路由器/打印机等。）
    dtype = _extract(xml, "deviceType")
    if dtype and "MediaRenderer" not in dtype:
        log.debug("跳过非 MediaRenderer 设备: %s", dtype)
        return None
    name = _extract(xml, "friendlyName") or "未知设备"
    udn = _extract(xml, "UDN") or ""
    # 从 location 推 base URL（用于相对 controlURL）
    base = location
    if base.endswith(".xml") or base.endswith("/"):
        base = base.rsplit("/", 1)[0]
    # 找 AVTransport 的 controlURL
    control = ""
    try:
        at_idx = xml.find("AVTransport")
        if at_idx >= 0:
            seg = xml[at_idx:at_idx + 1200]
            ctrl = _extract(seg, "controlURL")
            if ctrl:
                if ctrl.startswith("http"):
                    control = ctrl
                else:
                    control = base + "/" + ctrl.lstrip("/")
    except Exception:
        pass
    if not control:
        return None
    return {"name": name, "udn": udn, "control_url": control}


def _extract(xml: str, tag: str) -> str:
    """从 XML 取标签文本（兼容命名空间前缀）。"""
    import re
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml, re.S | re.I)
    if not m:
        m = re.search(rf"<\w+:{tag}[^>]*>(.*?)</\w+:{tag}>", xml, re.S | re.I)
    return html.unescape(m.group(1).strip()) if m else ""


# ============================================================
# HTTP 文件服务（供渲染器拉流）
# ============================================================
class _FileHandler(BaseHTTPRequestHandler):
    """极简 HTTP 文件服务：GET /file?path=<urlencoded> → 返回音频文件。"""

    server_version = "XiatiaoDLNA/1.0"
    # HTTP/1.1：Range / Connection 语义更规范，设备兼容性更好。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args) -> None:  # 记录请求（debug 级，避免刷屏）
        log.debug("[DLNA-HTTP] %s", fmt % args)

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def do_GET(self) -> None:
        self._serve(head_only=False)

    def _serve(self, head_only: bool) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            log.debug("[DLNA-HTTP] %s %s 来自 %s Range=%r",
                      self.command, parsed.path, self.client_address[0],
                      self.headers.get("Range", ""))
            if parsed.path != "/file":
                log.warning("[DLNA-HTTP] 路径非 /file: %s", parsed.path)
                self.send_error(404)
                return
            qs = urllib.parse.parse_qs(parsed.query)
            path = (qs.get("path") or [""])[0]
            if not path or not os.path.isfile(path):
                log.warning("[DLNA-HTTP] 文件不存在: %s", path)
                self.send_error(404)
                return
            size = os.path.getsize(path)
            ctype = _guess_mime(path)
            # 标准 HTTP Range 支持：设备按需自行请求片段（标准 DLNA 播放器用）。
            start = 0
            range_hdr = self.headers.get("Range", "")
            if range_hdr.startswith("bytes="):
                try:
                    rng = range_hdr[len("bytes="):].split("-")[0].strip()
                    if rng:
                        start = int(rng)
                except (ValueError, TypeError):
                    pass
            start = max(0, min(start, size))
            log.debug("[DLNA-HTTP] 请求 %s start=%s size=%s", path, start, size)
            if start > 0:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size - start))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "close")
            self.end_headers()
            if head_only:
                return
            with open(path, "rb") as fp:
                fp.seek(start)
                while True:
                    chunk = fp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.debug("HTTP 文件服务异常: %s", exc)


def _guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".dsf": "audio/x-dsf",
        ".dff": "audio/x-dff",
    }.get(ext, "application/octet-stream")


# ============================================================
# SOAP 控制
# ============================================================
def _soap_call(control_url: str, service: str, action: str, args: str,
               timeout: float = 5.0) -> bool:
    """向渲染器发一条 SOAP 控制命令。成功返回 True。"""
    return _soap_call_raw(control_url, service, action, args, timeout) is not None


def _soap_call_raw(control_url: str, service: str, action: str, args: str,
                   timeout: float = 5.0) -> Optional[str]:
    """发 SOAP 命令并返回响应 XML；失败返回 None。"""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
        f'{args}</u:{action}></s:Body></s:Envelope>'
    )
    req = urllib.request.Request(
        control_url,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"urn:schemas-upnp-org:service:{service}:1#{action}"',
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                log.debug("[SOAP→] %s | 200", action)
                return resp.read().decode("utf-8", "ignore")
            log.warning("[SOAP→] %s | HTTP %s", action, resp.status)
            return None
    except Exception as exc:
        log.warning("[SOAP→] %s | 失败: %s", action, exc)
        return None


# ============================================================
# 推送控制器
# ============================================================
class DlnaPusher:
    """DLNA 推送控制器：选设备、推歌、控制。"""

    def __init__(self) -> None:
        self._http: Optional[ThreadingHTTPServer] = None
        self._http_port = 0
        self._ip = local_ip()
        self._device: Optional[Dict[str, str]] = None
        self._lock = threading.Lock()

    # ---- HTTP 文件服务 ----
    def start_http(self, port: int = 8201) -> int:
        """启动本地 HTTP 文件服务，返回实际端口。"""
        if self._http is not None:
            return self._http_port
        for p in range(port, port + 20):
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", p), _FileHandler)
                self._http = srv
                self._http_port = p
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                log.info("DLNA 文件服务: http://%s:%s/file", self._ip, p)
                return p
            except OSError:
                continue
        log.warning("DLNA 文件服务启动失败（端口被占用）")
        return 0

    def stop_http(self) -> None:
        if self._http is not None:
            try:
                self._http.shutdown()
                self._http.server_close()
            except Exception:
                pass
            self._http = None
            self._http_port = 0

    # ---- 设备 ----
    def set_device(self, device: Dict[str, str]) -> None:
        with self._lock:
            self._device = device

    def current_device(self) -> Optional[Dict[str, str]]:
        return self._device

    # ---- 播放控制 ----
    def _control(self) -> str:
        return (self._device or {}).get("control_url", "")

    def _file_url(self, filepath: str) -> str:
        q = urllib.parse.urlencode({"path": filepath})
        return f"http://{self._ip}:{self._http_port}/file?{q}"

    def push(self, filepath: str, title: str = "", artist: str = "") -> bool:
        """推送一个本地文件到选中设备并开始播放（标准 DLNA）。

        只做标准的两步：SetAVTransportURI + Play。之后设备的播放/暂停/
        seek/进度全由 SOAP 命令驱动，不重推 URI、不做偏移补偿。
        """
        ctrl = self._control()
        if not ctrl:
            log.warning("DLNA 推送：未选择设备")
            return False
        if self._http is None:
            self.start_http()
        if self._http is None:
            return False
        url = self._file_url(filepath)
        meta = _didl_metadata(url, title or os.path.basename(filepath), artist,
                              _guess_mime(filepath))
        escaped_url = html.escape(url)
        escaped_meta = html.escape(meta)
        ok = _soap_call(
            ctrl, "AVTransport", "SetAVTransportURI",
            f"<InstanceID>0</InstanceID>"
            f"<CurrentURI>{escaped_url}</CurrentURI>"
            f"<CurrentURIMetaData>{escaped_meta}</CurrentURIMetaData>",
        )
        if not ok:
            return False
        _soap_call(ctrl, "AVTransport", "Play",
                   "<InstanceID>0</InstanceID><Speed>1</Speed>")
        log.info("DLNA 已推送: %s → %s", title or filepath, self._device.get("name"))
        return True

    def pause(self) -> bool:
        ctrl = self._control()
        if not ctrl:
            return False
        return _soap_call(ctrl, "AVTransport", "Pause", "<InstanceID>0</InstanceID>")

    def resume(self) -> bool:
        """恢复播放：Seek(当前位置) + Play。

        部分设备（如小爱音箱）暂停后不重新拉流，直接 Play 只从内部状态恢复，
        可能失败（静默）。标准做法是先 Seek 到当前位置再 Play——Seek 会触发
        设备重新定位/初始化，确保恢复出声。标准设备上等价于一次 Play。
        """
        ctrl = self._control()
        if not ctrl:
            return False
        # 查当前位置，Seek 到该位置（触发设备重新初始化）。
        pos = 0.0
        info = self.get_position()
        if info:
            pos = float(info.get("position", 0.0))
        s = max(0, int(pos))
        target = f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
        _soap_call(ctrl, "AVTransport", "Seek",
                   f"<InstanceID>0</InstanceID><Unit>REL_TIME</Unit><Target>{target}</Target>")
        return _soap_call(ctrl, "AVTransport", "Play",
                          "<InstanceID>0</InstanceID><Speed>1</Speed>")

    def stop(self) -> bool:
        ctrl = self._control()
        if not ctrl:
            return False
        return _soap_call(ctrl, "AVTransport", "Stop", "<InstanceID>0</InstanceID>")

    def get_position(self) -> Optional[Dict[str, float]]:
        """查询远端播放位置与时长（秒）。

        返回 {"position": float, "duration": float}；查询失败返回 None。
        """
        ctrl = self._control()
        if not ctrl:
            return None
        xml = _soap_call_raw(
            ctrl, "AVTransport", "GetPositionInfo",
            "<InstanceID>0</InstanceID>",
        )
        if not xml:
            return None
        rel = _extract(xml, "RelTime")
        dur = _extract(xml, "TrackDuration")
        return {"position": _parse_hms(rel), "duration": _parse_hms(dur)}

    def seek(self, seconds: float) -> bool:
        """跳转到指定位置（标准 AVTransport Seek，REL_TIME）。

        由渲染器自己执行跳转；本机只发命令，不碰数据流。
        （不标准的设备可能不真正执行——那是设备问题，非本实现。）
        """
        ctrl = self._control()
        if not ctrl:
            return False
        s = max(0, int(seconds))
        target = f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
        return _soap_call(
            ctrl, "AVTransport", "Seek",
            f"<InstanceID>0</InstanceID><Unit>REL_TIME</Unit><Target>{target}</Target>",
        )

    def set_volume(self, volume: int) -> bool:
        """设置渲染器音量（0-100）。"""
        ctrl = self._control()
        if not ctrl:
            return False
        v = max(0, min(100, int(volume)))
        return _soap_call(
            ctrl, "RenderingControl", "SetVolume",
            f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{v}</DesiredVolume>",
        )


def _parse_hms(text: str) -> float:
    """解析 HH:MM:SS(.mmm) 为秒；失败返回 0.0。"""
    if not text:
        return 0.0
    try:
        parts = text.strip().split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(text)
    except Exception:
        return 0.0


def _didl_metadata(url: str, title: str, artist: str,
                   mime: str = "audio/mpeg") -> str:
    """构造 DIDL-Lite 元数据（部分渲染器据此显示曲名）。

    mime 用实际文件类型（如 audio/flac），避免元数据与真实流不符导致
    部分设备误判（如把 flac 当 mp3 处理）。
    """
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f'<dc:title>{html.escape(title)}</dc:title>'
        f'<dc:creator>{html.escape(artist)}</dc:creator>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:{mime}:*">{html.escape(url)}</res>'
        '</item></DIDL-Lite>'
    )


# 单例
_instance: Optional[DlnaPusher] = None


def get_dlna_pusher() -> DlnaPusher:
    global _instance
    if _instance is None:
        _instance = DlnaPusher()
    return _instance
