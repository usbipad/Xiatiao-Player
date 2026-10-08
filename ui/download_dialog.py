"""在线歌下载对话框与下载执行。

从 ui/window.py 的 _on_download_current / _show_download_dialog /
_start_download / _safe_filename 抽出。

抽出理由：下载是「弹窗选参数 + 后台执行」的独立流程，只依赖当前曲目、
provider、downloader 与几个宿主回调，不涉及播放/队列核心。放 window
（上帝对象）里既臃肿又难测。

通过注入回调与宿主交互（不依赖 MainWindow 类型）：
  get_current_track()  -> 当前播放曲目（或 None）
  get_provider(stype)  -> provider（'subsonic'）
  toast(msg)           -> 提示
"""
from __future__ import annotations

import logging
import os
from typing import Callable

from gi.repository import Adw, Gio, Gtk

from core.i18n import _
from config.settings import get_config

log = logging.getLogger(__name__)


def safe_filename(name: str) -> str:
    """把歌名里的非法文件名字符替换掉。"""
    import re
    s = re.sub(r'[\\/:*?"<>|]', "_", name or "")
    return s.strip() or "download"


def _default_download_dir() -> str:
    """默认下载目录：配置 download_dir，空则 ~/下载。"""
    d = get_config().get_str("download_dir", "").strip()
    if not d:
        d = os.path.join(os.path.expanduser("~"), "下载")
    return d


def _current_quality() -> str:
    """默认音质：跟当前播放档位（运行时优先，回落到配置）。"""
    cur_q = "lossless"
    try:
        from providers.subsonic import _RUNTIME_QUALITY
        cur_q = _RUNTIME_QUALITY or get_config().get_str("online_quality", "lossless")
    except Exception:
        pass
    return str(cur_q or "lossless").lower()


class DownloadController:
    """在线歌下载：校验 → 弹窗 → 后台执行。"""

    def __init__(
        self,
        *,
        get_current_track: Callable[[], object],
        get_provider: Callable[[str], object],
        toast: Callable[[str], None],
        parent_window,
    ) -> None:
        self._get_current_track = get_current_track
        self._get_provider = get_provider
        self._toast = toast
        self._parent = parent_window
        #: 持有对话框引用，避免被 GC
        self.dialog = None

    # ------------------------------------------------------------
    # 入口：下载当前曲目
    # ------------------------------------------------------------
    def download_current(self) -> None:
        """下载当前播放曲目（在线歌）：校验后弹对话框。"""
        track = self._get_current_track()
        if track is None:
            self._toast(_("当前没有播放曲目"))
            return
        if getattr(track, "is_local", False):
            self._toast(_("本地曲目无需下载"))
            return
        if getattr(track, "source_type", "") != "subsonic":
            self._toast(_("仅支持下载在线曲目"))
            return
        sid = getattr(track, "source_id", "") or ""
        if not sid:
            self._toast(_("该曲目无下载地址"))
            return
        self._show_dialog(track)

    def _show_dialog(self, track) -> None:
        """下载对话框：音质（默认跟当前播放档位）/ 目录 / 文件名。"""
        from core.downloader import QUALITY_LABELS

        cur_q = _current_quality()
        default_dir = _default_download_dir()
        default_name = safe_filename(
            f"{getattr(track, 'artist', '')} - {getattr(track, 'title', '')}")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        # 音质档位：仅私有协议后端可选。
        # 标准 Subsonic 不按档位转码——下载原文件，故不显示音质选项。
        _is_private = bool(getattr(track, "is_private_backend", False))
        keys = [k for k, _ in QUALITY_LABELS]
        q_dropdown = None
        if _is_private:
            qrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            qrow.append(Gtk.Label(label=_("音质"), xalign=0))
            q_dropdown = Gtk.DropDown.new_from_strings([label for _, label in QUALITY_LABELS])
            q_dropdown.set_hexpand(True)
            if cur_q in keys:
                q_dropdown.set_selected(keys.index(cur_q))
            qrow.append(q_dropdown)
            box.append(qrow)

        # 目录行
        drow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        drow.append(Gtk.Label(label=_("目录"), xalign=0))
        dir_entry = Gtk.Entry()
        dir_entry.set_text(default_dir)
        dir_entry.set_hexpand(True)
        drow.append(dir_entry)
        browse_btn = Gtk.Button(label=_("浏览"))
        drow.append(browse_btn)
        box.append(drow)

        # 文件名
        nrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        nrow.append(Gtk.Label(label=_("文件名"), xalign=0))
        name_entry = Gtk.Entry()
        name_entry.set_text(default_name)
        name_entry.set_hexpand(True)
        nrow.append(name_entry)
        box.append(nrow)

        dlg = Adw.MessageDialog(
            transient_for=self._parent,
            heading=_("下载"),
            body=(_("选择音质与保存位置") if _is_private
                  else _("选择保存位置")),
        )
        dlg.set_extra_child(box)
        dlg.add_response("cancel", _("取消"))
        dlg.add_response("ok", _("开始下载"))
        dlg.set_default_response("ok")
        dlg.set_close_response("cancel")

        def _on_browse(_b):
            fd = Gtk.FileDialog()
            fd.set_title(_("选择下载目录"))
            fd.set_initial_folder(Gio.File.new_for_path(
                dir_entry.get_text().strip() or os.path.expanduser("~")))

            def _picked(d, result):
                try:
                    folder = d.select_folder_finish(result)
                except Exception:
                    return
                if folder is not None and folder.get_path():
                    dir_entry.set_text(folder.get_path())

            fd.select_folder(self._parent, None, _picked)

        browse_btn.connect("clicked", _on_browse)

        def _on_resp(_dlg, resp):
            if resp != "ok":
                return
            quality = cur_q
            if q_dropdown is not None:
                idx = q_dropdown.get_selected()
                quality = keys[idx] if 0 <= idx < len(keys) else cur_q
            dest = dir_entry.get_text().strip() or default_dir
            name = name_entry.get_text().strip() or default_name
            # 记住目录（下次默认）。
            try:
                get_config().set_str("download_dir", dest)
            except Exception:
                pass
            self.start_download(track, quality, dest, name)

        dlg.connect("response", _on_resp)
        dlg.present()
        self.dialog = dlg

    # ------------------------------------------------------------
    # 执行下载
    # ------------------------------------------------------------
    def start_download(self, track, quality: str, dest_dir: str,
                       base_name: str) -> None:
        """执行下载：音频 + 封面 / 歌词 / 标签内嵌（元数据失败仅警告）。"""
        from core.tasks import run_async
        from core import downloader as _dl

        sid = getattr(track, "source_id", "") or ""
        self._toast(_("开始下载：{name}").format(name=base_name))

        def _work():
            p = self._get_provider("subsonic")
            if p is None:
                raise RuntimeError("在线音源不可用")
            # 私有协议后端才按所选档位下载；
            # 标准 Subsonic 不按档位转码——下载原文件直传（maxBitRate=0）。
            _is_private = bool(getattr(track, "is_private_backend", False))
            if _is_private:
                url = p.download_url(sid, quality)
            else:
                url = p.stream_url(sid, max_bit_rate=0)
            path = _dl.download_audio(url, dest_dir, base_name)
            # 元数据（尽力而为，失败不影响音频）。
            meta_err = ""
            try:
                cover = _dl.fetch_bytes(getattr(track, "cover_url", "") or "")
                lyrics = p.best_lyrics_lrc(
                    sid, getattr(track, "artist", "") or "",
                    getattr(track, "title", "") or "")
                _dl.write_metadata(
                    path,
                    title=getattr(track, "title", "") or "",
                    artist=getattr(track, "artist", "") or "",
                    album=getattr(track, "album", "") or "",
                    cover_bytes=cover,
                    lyrics_lrc=lyrics,
                )
            except Exception as exc:
                meta_err = str(exc)
                log.info("写入元数据失败（不影响音频）: %s", exc)
            return path, meta_err

        def _done(result):
            path, meta_err = result
            if meta_err:
                self._toast(_("下载完成（元数据未写入）：{path}").format(path=path))
            else:
                self._toast(_("下载完成：{path}").format(path=path))

        def _err(exc):
            log.warning("下载失败: %s", exc)
            self._toast(_("下载失败：{err}").format(err=exc))

        run_async(work=_work, on_done=_done, on_error=_err)
