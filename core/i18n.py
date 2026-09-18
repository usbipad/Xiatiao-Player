"""轻量国际化（i18n）。

设计：
- 以「中文原文」作为 key（界面原本就是中文），只需维护英文映射；
- 查不到翻译时回退中文原文，绝不会因缺翻译而崩；
- 语言选项：system（跟随系统）/ zh（中文）/ en（英文）；
- 切换语言后需重建界面生效（设置页会提示或自动刷新）。

用法：
    from core.i18n import _, set_language, get_language
    label.set_text(_("设置"))
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

#: 配置键
CONFIG_KEY = "language"

#: 支持的语言
SUPPORTED = ("system", "zh", "en")

#: 当前解析后的语言（zh / en）
_current = "zh"


# ---- 英文翻译表（中文原文 → 英文）----
# 未收录的条目回退中文原文。后续可逐步补充。
_EN: dict[str, str] = {
    # 窗口 / 通用
    "设置": "Settings",
    "外观": "Appearance",
    "界面": "Interface",
    "行为": "Behavior",
    "主题": "Theme",
    "跟随系统": "Follow System",
    "浅色": "Light",
    "深色": "Dark",
    "列表行高": "List Row Density",
    "紧凑": "Compact",
    "标准": "Normal",
    "宽松": "Relaxed",
    "语言": "Language",
    "中文": "Chinese",
    "英文": "English",
    "语言已切换，重启应用后生效":
        "Language changed. Restart the app to take effect.",
    # 主页导航
    "主页": "Home",
    "歌单": "Playlists",
    "曲库": "Library",
    "收藏": "Favorites",
    "本地曲库": "Local Library",
    "曲库为空": "Library is empty",
    "还没有播放记录": "No play history yet",
    "尚未添加音乐目录，请在设置中添加":
        "No music folders yet. Add one in Settings.",
    "添加音乐目录": "Add Music Folder",
    "添加多个音乐目录，程序启动时会自动扫描":
        "Add multiple music folders; the app scans them on startup",
    # 描述 / 选项
    "点击右侧按钮后按下新按键即可修改":
        "Click the button on the right, then press a new key",
    "调整可视化刷新率与频率分辨率":
        "Adjust the refresh rate and frequency resolution",
    "按文件标签拉平响度（需音频带 ReplayGain 标签）":
        "Level loudness by file tags (requires ReplayGain tags)",
    "固定中心频率的图示均衡": "Graphic EQ with fixed center frequencies",
    "每段可调频率 / 增益 / Q 值": "Each band: frequency / gain / Q",
    "加载脉冲响应（IR）文件，如耳机校正 / 房间混响":
        "Load impulse response (IR) files, e.g. headphone correction / room reverb",
    "高质量 Freeverb：预延迟 / 衰减 / 阻尼 / 低切":
        "High-quality Freeverb: pre-delay / decay / damping / low cut",
    "模拟声音绕过头部，减少「头中效应」":
        "Simulate sound bypassing the head to reduce \"in-head\" effect",
    "压大声、提小声，让响度更平稳":
        "Compress loud, boost quiet — for steadier loudness",
    "防止削波爆音": "Prevent clipping and distortion",
    "非对称软饱和，温暖音色（过采样）":
        "Asymmetric soft saturation, warm tone (oversampled)",
    "分频 + 高频相位超前，提升清晰度（过采样）":
        "Crossover + HF phase lead for clarity (oversampled)",
    "保存/加载整套 DSP 配置（EQ / 卷积 / 压缩 / 染色…）":
        "Save/load full DSP setups (EQ / Convolution / Compressor / Coloration…)",
    "峰值": "Peak",
    "低架": "Low Shelf",
    "高架": "High Shelf",
    "单路": "Mono",
    "立体声（左右分开）": "Stereo (separate L/R)",
    "左 (0)": "Left (0)",
    "右 (1)": "Right (1)",
    "曲目 (Track)": "Track",
    "专辑 (Album)": "Album",
    "关闭": "Off",
    "左右交换": "Swap L/R",
    "单声道": "Mono",
    "立体声": "Stereo",
    "左→双声道": "Left → both",
    "右→双声道": "Right → both",
    "脉冲响应 (wav, irs)": "Impulse Response (wav, irs)",
    "所有文件": "All files",
    "正在重新扫描本地曲库…": "Rescanning local library…",
    "主页专辑 / 艺术家卡片的随机轮播":
        "Random rotation of home album / artist cards",
    "搜索歌曲 / 歌手 / 专辑": "Search songs / artists / albums",
    # 列表表头 / 统计
    "歌名": "Title",
    "歌手": "Artist",
    "格式": "Format",
    "采样率": "Sample Rate",
    "首": "tracks",
    "已选": "Selected",
    # 菜单
    "退出程序": "Quit",
    "设置": "Settings",
    "复制歌名": "Copy title",
    "查看歌手": "View artist",
    "历史播放": "Recently Played",
    "专辑": "Albums",
    "艺术家": "Artists",
    "播放": "Play",
    "重命名…": "Rename…",
    "删除歌单": "Delete playlist",
    "下一首播放": "Play next",
    "添加到播放列表": "Add to queue",
    "添加到歌单…": "Add to playlist…",
    "喜欢 / 取消喜欢": "Like / Unlike",
    "查看歌曲信息": "View track info",
    "复制文件路径": "Copy file path",
    "在文件管理器中显示": "Show in file manager",
    "从列表移除": "Remove from list",
    "删除…": "Delete…",
    "提升到下一首": "Promote to next",
    "从列表丢弃": "Remove from queue",
    "重置": "Reset",
    "重置所有": "Reset All",
    "请按下要绑定的按键…": "Press a key to bind…",
    "新建并加入": "Create and add",
    "加入": "Add",
    "还没有歌单，可在上方新建": "No playlists yet. Create one above.",
    "取消": "Cancel",
    "确定": "OK",
    "加入歌单": "Add to playlist",
    "加载…": "Load…",
    "未播放": "Not playing",
    "暂无歌词": "No lyrics",
    "预设名称": "Preset name",
    "未设置": "Not set",
    "多选": "Multi-select",
    "新建歌单名称": "New playlist name",
    "个": "",
    "此键位已被「{conflict}」使用":
        "This key is already used by \"{conflict}\"",
    "加入歌单（{n} 首）": "Add to playlist ({n} tracks)",
    "个": "",
    # Toast 提示
    "已新建「{name}」并加入 {n} 首": "Created \"{name}\" with {n} tracks",
    "已加入「{name}」{n} 首": "Added {n} tracks to \"{name}\"",
    "已设为下一首：{title}": "Set as next: {title}",
    "已从播放列表移除：{title}": "Removed from queue: {title}",
    "已加入下一首播放：{title}": "Added to play next: {title}",
    "已添加到播放列表：{title}": "Added to queue: {title}",
    "已复制：{text}": "Copied: {text}",
    "已喜欢：{title}": "Liked: {title}",
    "已取消喜欢：{title}": "Unliked: {title}",
    "已从歌单移除：{title}": "Removed from playlist: {title}",
    "已创建歌单：{name}": "Playlist created: {name}",
    "已删除歌单：{name}": "Playlist deleted: {name}",
    # 外观页
    "启动时自动扫描本地曲库": "Scan local library on startup",
    "关闭时最小化到后台": "Minimize to background on close",
    "关闭窗口不退出程序": "Closing the window does not quit the app",
    "沉浸页背景模糊": "Now Playing blur background",
    "用当前封面生成模糊背景；关闭则用纯色背景":
        "Blur the current cover as background; use solid color when off",
    # 可视化设置页
    "可视化": "Visualization",
    "启用可视化": "Enable visualization",
    "显示沉浸式页面的频谱；关闭可省 CPU":
        "Show the spectrum on the Now Playing page; disable to save CPU",
    "频谱参数": "Spectrum Parameters",
    "调整可视化刷新率与频率分辨率":
        "Adjust the refresh rate and frequency resolution",
    "采样 FPS": "Sample FPS",
    "越高越流畅，但更耗电/占 CPU":
        "Higher is smoother, but uses more CPU / battery",
    "FFT 窗口大小": "FFT Window Size",
    "越大，频率分得越细，但计算更耗 CPU":
        "Larger gives finer frequency detail but costs more CPU",
    "频谱柱数量": "Spectrum Bar Count",
    "柱状": "Bars",
    "波形": "Wave",
    "平滑曲线": "Smooth Curve",
    "频率": "Frequency",
    "音阶": "Musical",
    "影响横向颗粒度": "Affects horizontal granularity",
    "显示": "Display",
    "样式": "Style",
    "排列方式": "Arrangement",
    "频率（对数均分）/ 音阶（按音高）":
        "Frequency (log-spaced) / Musical (by pitch)",
    "音画同步": "Audio/Video Sync",
    "频谱整体延后（ms），与耳朵听到的声音对齐":
        "Delay the spectrum (ms) to match the sound you hear",
    "动态范围": "Dynamic Range",
    "响度映射下限（dB）；越负动态越大、小信号越矮":
        "Loudness floor (dB); more negative = wider range, quieter signals shorter",

    # ---- 音效页（effect_page）----
    "DSP 音效": "DSP Effects",
    "高级设置": "Advanced Settings",
    "EQ / 限幅 / 压缩 / 响度 / 卷积 等": "EQ / Limiter / Compressor / Loudness / Convolution etc.",
    "预设": "Presets",
    "新建预设（输入名字，保存当前参数）":
        "New preset (enter a name to save current settings)",
    "保存：覆盖当前选中的预设": "Save: overwrite the selected preset",
    "删除当前选中的预设": "Delete the selected preset",
    "重置所有参数到默认": "Reset all parameters to default",
    "重置本组为默认值": "Reset this group to default",
    "重置本页所有功能为默认值": "Reset all features on this page to default",
    "启用 DSP": "Enable DSP",
    "把 DSP 音效所有功能恢复为默认值":
        "Restore all DSP effects to their defaults",
    "增益 / 余量": "Gain / Headroom",
    "预增益 (dB)": "Pre-gain (dB)",
    "启用余量管理 (Headroom)": "Enable Headroom Management",
    "预留峰值空间，防止削波": "Reserve peak headroom to prevent clipping",
    "余量 (dB)": "Headroom (dB)",
    "ReplayGain": "ReplayGain",
    "启用 ReplayGain": "Enable ReplayGain",
    "模式": "Mode",
    "前置增益 (dB)": "Pre-amp (dB)",
    "负值衰减（防削波）": "Negative values attenuate (prevent clipping)",
    "图形 EQ（10 段）": "Graphic EQ (10-band)",
    "启用图形 EQ": "Enable Graphic EQ",
    "Q 值": "Q Factor",
    "各段带宽，越大越窄": "Bandwidth per band; larger is narrower",
    "参数均衡器 (PEQ)": "Parametric EQ (PEQ)",
    "启用 PEQ": "Enable PEQ",
    "频段": "Bands",
    "添加频段": "Add band",
    "删除最后添加的频段": "Remove the last added band",
    "卷积 (Convolution)": "Convolution",
    "启用卷积": "Enable Convolution",
    "IR 文件": "IR File",
    "清除已加载的 IR": "Clear the loaded IR",
    "IR 模式": "IR Mode",
    "单路：左右用同一路；立体声：左用左、右用右":
        "Mono: same for both channels; Stereo: left/right separate",
    "IR 声道": "IR Channel",
    "单路模式下选哪一路": "Which channel to use in mono mode",
    "选择 IR 文件": "Choose IR File",
    "未加载": "Not loaded",
    "未知": "Unknown",
    "未命名歌单": "Untitled Playlist",
    "新歌单": "New Playlist",
    "编码格式": "Codec",
    "低音 / 高音": "Bass / Treble",
    "启用低音增强": "Enable Bass Boost",
    "低音增益 (dB)": "Bass Gain (dB)",
    "低音频率 (Hz)": "Bass Frequency (Hz)",
    "高音增益 (dB)": "Treble Gain (dB)",
    "高音频率 (Hz)": "Treble Frequency (Hz)",
    "响度补偿 (Loudness)": "Loudness Compensation",
    "启用响度补偿": "Enable Loudness",
    "小音量下提升低频": "Boost low frequencies at low volume",
    "响度强度": "Loudness Amount",
    "立体声": "Stereo",
    "启用立体声宽度": "Enable Stereo Width",
    "宽度": "Width",
    "0=单声道, 1=原始, 2=加宽": "0=Mono, 1=Original, 2=Widened",
    "平衡": "Balance",
    "-1=全左, 0=居中, 1=全右": "-1=Full left, 0=Center, 1=Full right",
    "混响 (Reverb)": "Reverb",
    "启用混响": "Enable Reverb",
    "干湿比": "Dry/Wet",
    "0=全干, 1=全湿": "0=Dry, 1=Wet",
    "预延迟 (ms)": "Pre-delay (ms)",
    "干声与混响的间隔，越大越清晰":
        "Gap between dry and reverb; larger is clearer",
    "衰减": "Decay",
    "房间大小感，越大混响越长": "Room size; larger means longer reverb",
    "阻尼": "Damping",
    "高频衰减，越大越闷": "High-frequency damping; larger is duller",
    "立体声宽度": "Stereo Width",
    "低切 (Hz)": "Low Cut (Hz)",
    "去掉混响低频浑浊": "Remove low-frequency muddiness in reverb",
    "调制": "Modulation",
    "打散金属感，让混响更自然": "Break up metallic tone for a more natural reverb",
    "耳机串扰 (Crossfeed)": "Crossfeed",
    "启用 Crossfeed": "Enable Crossfeed",
    "强度": "Amount",
    "延迟 (ms)": "Delay (ms)",
    "相位 / 通道": "Phase / Channel",
    "全局相位翻转": "Global Phase Invert",
    "整段反相（180°）": "Invert the whole signal (180°)",
    "通道矩阵": "Channel Matrix",
    "声道混音 / 交换 / 映射": "Channel mix / swap / map",
    "压缩器 (Compressor)": "Compressor",
    "启用压缩器": "Enable Compressor",
    "阈值 (dBFS)": "Threshold (dBFS)",
    "压缩比": "Ratio",
    "增益补偿 (dB)": "Makeup Gain (dB)",
    "启动 (s)": "Attack (s)",
    "越小反应越快": "Smaller reacts faster",
    "释放 (s)": "Release (s)",
    "越大恢复越慢": "Larger recovers slower",
    "限幅器": "Limiter",
    "启用限幅器": "Enable Limiter",
    "限幅阈值 (dBFS)": "Limiter Threshold (dBFS)",
    "软削波 (soft clip)": "Soft Clip",
    "峰值平滑过渡，减少高频毛刺（会轻微染色）":
        "Smooth peaks to reduce high-frequency harshness (slight coloration)",

    # ---- 设置窗口（settings_dialog）----
    "快捷键": "Shortcuts",
    "播放快捷键": "Playback Shortcuts",
    "播放 / 暂停": "Play / Pause",
    "上一首": "Previous",
    "下一首": "Next",
    "快退 5 秒": "Seek back 5s",
    "快进 5 秒": "Seek forward 5s",
    "音量 +5%": "Volume +5%",
    "音量 -5%": "Volume -5%",
    "顺序播放": "In order",
    "单曲循环": "Repeat one",
    "列表循环": "Repeat all",
    "慢速": "Slow",
    "中等": "Medium",
    "快速": "Fast",
    "恢复默认快捷键": "Restore default shortcuts",
    "点击后按下新按键录制": "Click, then press a new key to record",
    "清除此快捷键": "Clear this shortcut",
    "按下新快捷键": "Press a new shortcut",
    "播放": "Playback",
    "播放行为": "Playback Behavior",
    "启动时恢复上次播放会话": "Restore last session on startup",
    "恢复上次的播放队列、当前歌曲、进度、音量与播放模式":
        "Restore last queue, current track, position, volume and play mode",
    "记忆音量": "Remember Volume",
    "下次启动沿用上次的音量": "Reuse last volume on next startup",
    "切歌淡入淡出": "Crossfade on Track Change",
    "切换歌曲时平滑过渡，避免突兀":
        "Smooth transition when switching tracks",
    "默认播放模式": "Default Play Mode",
    "新队列开始时采用的循环方式":
        "Repeat mode used when a new queue starts",
    "主页卡片": "Home Cards",
    "主页卡片随机轮播": "Shuffle Home Cards",
    "折叠时卡片自动随机切换展示的专辑 / 艺术家":
        "Auto-rotate albums / artists on collapsed cards",
    "轮播速度": "Rotation Speed",
    "音源": "Sources",
    "本地音源": "Local Sources",
    "添加音乐目录": "Add Music Folder",
    "选择音乐目录": "Choose Music Folder",
    "已启用，重启应用后生效": "Enabled. Restart the app to take effect.",

    # ---- 可视化设置页（viz_settings_page）----
    "上升速度": "Rise Speed",
    "频谱柱上升快慢；越小越柔和（过小会迟滞）":
        "How fast bars rise; smaller is softer (too small lags)",
    "下落速度": "Fall Speed",
    "频谱柱回落快慢；越小越拖尾":
        "How fast bars fall; smaller trails longer",
    "打开频谱窗口": "Open Spectrum Window",
    "大尺寸独立窗口，可切换样式":
        "Large standalone window with switchable styles",

    # ---- 高级 DSP 窗口（advanced_dsp_window）----
    "高级 DSP 设置": "Advanced DSP Settings",
    "ReplayGain / 增益": "ReplayGain / Gain",
    "均衡器（图形 EQ / 参数 EQ）": "Equalizer (Graphic / Parametric EQ)",
    "卷积 / 音调": "Convolution / Tone",
    "响度 / 动态": "Loudness / Dynamics",
    "空间 / 声道": "Spatial / Channels",
    "音色染色": "Coloration",
    "电子管偶次谐波": "Tube Even Harmonics",
    "启用电子管": "Enable Tube",
    "驱动量": "Drive",
    "启用 BBE": "Enable BBE",
    "预设管理": "Preset Management",
    "DSP 预设": "DSP Presets",
    "保存当前配置为预设": "Save current settings as a preset",
    "加载选中预设": "Load the selected preset",
    "删除选中预设": "Delete the selected preset",

    # ---- DSP 页（dsp_page）----
    "卷积 IR / 动态 & 响度 / 可视化 / 音色染色 / 采样 & 通道 / 预设管理":
        "Convolution IR / Dynamics & Loudness / Visualization / Coloration / Sample & Channel / Presets",

    # ---- 左侧播放面板（player_panel）----
    "未登录": "Not logged in",
    "通知": "Notifications",
    "随机播放": "Shuffle",
    "不循环": "No repeat",
    "音量": "Volume",
    "喜欢": "Like",
    "取消喜欢": "Unlike",
    "音效": "Effects",
    "DSP 音效设置…": "DSP Effect Settings…",
    "手动微调 EQ / 低音 / 压缩等参数":
        "Fine-tune EQ / bass / compressor and more",

    # ---- 沉浸页（now_playing）----
    "返回（Esc）": "Back (Esc)",
    "最大化": "Maximize",
    "还原": "Restore",

    # ---- 歌单页（playlists_page）----
    "返回歌单": "Back to playlists",
    "播放整个歌单": "Play the whole playlist",
    "新建歌单": "New playlist",
    "还没有歌单，点右上角 + 或右键新建":
        "No playlists yet. Click + at top right or right-click to create one.",

    # ---- 曲库/列表页（pages）----
    "返回": "Back",
    "多选模式": "Multi-select",
    "重新扫描本地曲库（仅新增/删除会更新）":
        "Rescan local library (only additions/removals update)",
    "定位当前播放": "Locate now playing",
    "Hi-Res 高解析音频": "Hi-Res audio",

    # ---- 主窗口（window）----
    "菜单": "Menu",
    "选择已有歌单": "Choose an existing playlist",
    "歌曲信息": "Track Info",

    # ---- 频谱窗口 / 封面 ----
    "音频频谱": "Audio Spectrum",
    "进入全屏播放": "Enter fullscreen playback",
}


def _resolve_language(lang: str) -> str:
    """把配置里的语言选项解析成实际语言（zh / en）。"""
    lang = (lang or "system").lower()
    if lang in ("zh", "en"):
        return lang
    # system：看环境变量
    env = (os.environ.get("LANG") or os.environ.get("LC_ALL")
           or os.environ.get("LC_MESSAGES") or "").lower()
    if env.startswith("zh"):
        return "zh"
    if env.startswith(("en", "c", "posix")) or not env:
        return "en"
    return "en"


def _load() -> None:
    """从配置加载语言（启动时调一次）。"""
    global _current
    try:
        from config.settings import get_config
        lang = get_config().get_str(CONFIG_KEY, "system")
    except Exception:
        lang = "system"
    _current = _resolve_language(lang)


def get_language() -> str:
    """返回当前解析后的语言（zh / en）。"""
    return _current


def get_setting() -> str:
    """返回配置里的语言选项（system / zh / en）。"""
    try:
        from config.settings import get_config
        return get_config().get_str(CONFIG_KEY, "system")
    except Exception:
        return "system"


def set_language(lang: str) -> None:
    """设置语言（system / zh / en），持久化并立即更新当前语言。"""
    global _current
    lang = lang if lang in SUPPORTED else "system"
    try:
        from config.settings import get_config
        get_config().set_str(CONFIG_KEY, lang)
    except Exception:
        pass
    _current = _resolve_language(lang)


def _(text: str) -> str:
    """翻译函数：中文 → 英文（按当前语言）；无翻译回退原文。"""
    if not text:
        return text
    if _current == "en":
        return _EN.get(text, text)
    return text


# 启动时加载
_load()
