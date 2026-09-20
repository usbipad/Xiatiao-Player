#!/usr/bin/env bash
# 构建 Xiatiao Player 的 AppImage（全包含：内嵌 Python 运行时 + 应用依赖）。
#
# 自包含范围：
#   ✅ 项目 Python 代码
#   ✅ Rust 音频后端
#   ✅ 独立 CPython 运行时（python-build-standalone，内嵌）
#   ✅ mutagen（标签读取）、PyYAML（DSP 配置）、numpy（频谱可视化）
#   ✅ ffmpeg + ffprobe（DSD/APE/WavPack 软解）+ 其完整共享库闭包
#   ❌ GTK4 / libadwaita / PyGObject / girepository（仍依赖系统）
#
# 为什么 GTK 不内嵌：
#   GTK4 + PyGObject 的 GI typelib 与上百个共享库路径耦合极深，强行内联易碎臃肿。
#   能跑 GTK4 的桌面系统（GNOME/KDE）通常已带这些依赖。
#   若需连 GTK 也内嵌，应改用 Flatpak（GNOME runtime 自带 GTK4/Adw）。
#
# 为什么 ffmpeg 可以内嵌而 GTK 不行：
#   ffmpeg 是【子进程可执行文件】，其库闭包是纯用户态编解码库（libav*/libx264...），
#   与硬件/内核无绑定。GTK 则依赖图形驱动层，而驱动与主机内核 DRM 死绑，
#   内嵌会导致 ABI 不匹配 → 崩或花屏。二者性质不同。
#
# 关键约束（勿改错）：
#   内嵌 CPython 的 minor 版本必须与系统 PyGObject 的 _gi 扩展 ABI 一致。
#   本机系统 gi 为 _gi.cpython-314-x86_64-linux-gnu.so → 必须用 CPython 3.14。
#   换用其它 minor 版本会导致 `import gi` 失败。
#
# 渲染 / 驱动策略（勿内嵌驱动层）：
#   - GSK_RENDERER 不设 → 用 GTK 默认的渲染器自动降级（vulkan → gl → cairo）。
#     渲染器选择与失败降级是 GTK 官方职责，应用不干预、不兜底。
#   - 绝不内嵌图形驱动层（libvulkan* / libEGL* / libGL* / libGLX* / libgbm /
#     libdrm / dri/ / icd.d/）。这些必须由主机提供：
#       * 驱动与主机内核 DRM 版本死绑，内嵌会 ABI 不匹配 → 崩或花屏；
#       * GLVND（libEGL/libGL）是稳定抽象层，主机据此路由到自身正确的驱动。
#     把驱动留给主机，等于让每种显卡用它自己匹配的驱动，这是唯一正确做法。
#     下方 driver_excluded() 守卫用于防止将来手滑把驱动闭包拷进 AppDir。
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${XIATIAO_APPIMAGE_BUILD:-/tmp/xiatiao-appimage}"
APPDIR="$BUILD/Xiatiao.AppDir"
APP_ID="com.xiatiao.player"
ARCH="${ARCH:-x86_64}"

# --- 内嵌 Python 运行时（python-build-standalone）---
PY_STANDALONE_VER="${XIATIAO_PY_STANDALONE_VER:-3.14.7}"
PY_STANDALONE_TAG="${XIATIAO_PY_STANDALONE_TAG:-20260901}"
PY_TARBALL="cpython-${PY_STANDALONE_VER}+${PY_STANDALONE_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_STANDALONE_TAG}/${PY_TARBALL}"
PY_CACHE="${XIATIAO_PY_CACHE:-$HOME/.cache/xiatiao/python-standalone}"

# 系统 PyGObject 所在目录（内嵌解释器需靠它 import gi）
SYSTEM_DIST_PACKAGES="${XIATIAO_SYSTEM_DIST_PACKAGES:-/usr/lib/python3/dist-packages}"

# 要内嵌进 AppImage 的系统 Python 包（目录名）。
# 这些是应用的基本功能依赖，目标机不一定装，因此打包进来。
EMBED_PY_PACKAGES="${XIATIAO_EMBED_PY_PACKAGES:-numpy mutagen yaml _yaml}"
# 对应的分发元数据目录（有则一起带，无则跳过）。
EMBED_PY_DISTINFO="numpy-2.4.6.dist-info pyyaml-6.0.3.dist-info"

# 要内嵌的可执行文件（子进程调用）。
EMBED_BINARIES="${XIATIAO_EMBED_BINARIES:-ffmpeg ffprobe}"

# 图形驱动层黑名单（必须由主机提供，绝不内嵌）。
# 见头部「渲染 / 驱动策略」。任何把 libgtk 闭包拷进 AppDir 的改动，
# 都必须先过滤掉这些模式，否则会在目标机上造成驱动错配 → 花屏。
# 注意：驱动库带版本后缀（如 libvulkan.so.1），正则用 [^/]*\.so[^/]* 匹配任意版本形式。
DRIVER_EXCLUDE_RE='(^|/)(libvulkan[^/]*\.so[^/]*|libEGL[^/]*\.so[^/]*|libGL\.so[^/]*|libGLX[^/]*\.so[^/]*|libGLESv[12][^/]*\.so[^/]*|libgbm\.so[^/]*|libdrm[^/]*\.so[^/]*|libnvidia-[^/]*\.so[^/]*|libcuda\.so[^/]*|.*/dri/.*|.*/icd\.d/.*)$'

# 守卫：扫描 AppDir 中是否混入了驱动层文件；若有则报错退出。
driver_excluded() {
    local hits
    hits="$(find "$APPDIR" -type f 2>/dev/null | grep -E "$DRIVER_EXCLUDE_RE" || true)"
    if [ -n "$hits" ]; then
        echo "错误：AppDir 中混入了图形驱动层文件，必须移除（否则目标机可能花屏）：" >&2
        echo "$hits" >&2
        return 1
    fi
    return 0
}

# appimagetool：优先环境变量，其次 /tmp/appimage-build/
APPIMAGETOOL="${APPIMAGETOOL:-/tmp/appimage-build/appimagetool}"
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "错误：找不到 appimagetool（$APPIMAGETOOL）" >&2
    echo "下载：curl -L -o /tmp/appimage-build/appimagetool \\" >&2
    echo "  https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" >&2
    exit 1
fi

# 1) 准备内嵌 Python（带缓存，避免重复下载）
mkdir -p "$PY_CACHE"
PY_TARBALL_PATH="$PY_CACHE/$PY_TARBALL"
if [ ! -f "$PY_TARBALL_PATH" ]; then
    echo "==> 下载内嵌 Python 运行时：$PY_TARBALL"
    curl -L --fail --retry 3 --max-time 300 -o "$PY_TARBALL_PATH.part" "$PY_URL"
    mv "$PY_TARBALL_PATH.part" "$PY_TARBALL_PATH"
else
    echo "==> 复用已缓存的内嵌 Python：$PY_TARBALL_PATH"
fi

# 2) 清理构建目录
echo "==> 清理构建目录"
rm -rf "$BUILD"
mkdir -p "$APPDIR/usr/lib/xiatiao-player"

cd "$PROJ"

# 3) Rust 后端（若缺失则编译）
BACKEND="audio_backend_rs/target/release/xiatiao-audio-backend"
if [ ! -f "$BACKEND" ]; then
    echo "==> 编译 Rust 后端"
    (cd audio_backend_rs && cargo build --release)
fi

# 4) 项目代码 + 运行时资源
#    排除 __pycache__、tools、tests、docs 等非运行必需项。
echo "==> 复制项目代码"
for item in main.py config core models providers services ui data; do
    cp -r "$item" "$APPDIR/usr/lib/xiatiao-player/"
done
install -m755 "$BACKEND" "$APPDIR/usr/lib/xiatiao-player/xiatiao-audio-backend"

# 清理复制进来的 __pycache__（保持 AppImage 干净）
find "$APPDIR/usr/lib/xiatiao-player" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 4b) 内嵌系统 Python 包（numpy / mutagen / yaml）
#     放入内嵌解释器的 site-packages，确保优先于系统版本被导入。
SITE_PACKAGES="$APPDIR/usr/lib/xiatiao-player/python/lib/python3.14/site-packages"
# 注意：此时 python 运行时尚未解压（下一步才做），先记路径，稍后填充。

# 5) 解压内嵌 Python 运行时到 AppDir
echo "==> 内嵌 Python 运行时（$PY_STANDALONE_VER）"
PY_DEST="$APPDIR/usr/lib/xiatiao-player/python"
mkdir -p "$PY_DEST"
tar -xzf "$PY_TARBALL_PATH" -C "$PY_DEST" --strip-components=1
# 去掉 pip/idle 等非运行必需项，压缩体积
rm -rf "$PY_DEST/lib/python3.14/ensurepip" \
       "$PY_DEST/lib/python3.14/idlelib" \
       "$PY_DEST/lib/python3.14/tkinter" \
       "$PY_DEST/lib/python3.14/test" \
       "$PY_DEST/lib/python3.14/turtledemo" \
       "$PY_DEST/share" 2>/dev/null || true
find "$PY_DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 5b) 内嵌系统 Python 包到 site-packages
SITE_PACKAGES="$PY_DEST/lib/python3.14/site-packages"
mkdir -p "$SITE_PACKAGES"
echo "==> 内嵌 Python 包：$EMBED_PY_PACKAGES"
for pkg in $EMBED_PY_PACKAGES; do
    if [ -e "$SYSTEM_DIST_PACKAGES/$pkg" ]; then
        cp -r "$SYSTEM_DIST_PACKAGES/$pkg" "$SITE_PACKAGES/"
    else
        echo "警告：系统未找到 Python 包 $pkg，跳过" >&2
    fi
done
for di in $EMBED_PY_DISTINFO; do
    [ -e "$SYSTEM_DIST_PACKAGES/$di" ] && cp -r "$SYSTEM_DIST_PACKAGES/$di" "$SITE_PACKAGES/"
done
find "$SITE_PACKAGES" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 5c) 内嵌可执行文件（ffmpeg / ffprobe）+ 其完整共享库闭包
#     ffmpeg 是子进程调用，需要：二进制进 bin/、库进 lib/，AppRun 里设置 PATH/LD_LIBRARY_PATH。
BIN_DEST="$APPDIR/usr/lib/xiatiao-player/bin"
LIB_DEST="$APPDIR/usr/lib/xiatiao-player/lib"
mkdir -p "$BIN_DEST" "$LIB_DEST"
for exe in $EMBED_BINARIES; do
    src="$(command -v "$exe" || true)"
    if [ -z "$src" ]; then
        echo "警告：系统未找到可执行文件 $exe，跳过" >&2
        continue
    fi
    echo "==> 内嵌可执行文件：$exe（$src）"
    install -m755 "$src" "$BIN_DEST/$exe"
done
# 收集这些可执行文件的完整库闭包（去重），排除核心系统库与图形驱动层。
if [ -n "$(ls -A "$BIN_DEST" 2>/dev/null)" ]; then
    echo "==> 收集 ffmpeg 共享库闭包"
    : > /tmp/xiatiao-ff-libs.txt
    for exe in "$BIN_DEST"/*; do
        ldd "$exe" 2>/dev/null | awk '/=>/ && $3 ~ /^\// {print $3}' >> /tmp/xiatiao-ff-libs.txt
    done
    sort -u /tmp/xiatiao-ff-libs.txt \
        | grep -vE '/(libc|libm|libpthread|libdl|librt|libresolv|libgcc_s|libstdc\+\+|ld-linux)[^/]*\.so' \
        | grep -vE "$DRIVER_EXCLUDE_RE" \
        | while read -r lib; do
            [ -f "$lib" ] && cp -n "$lib" "$LIB_DEST/" 2>/dev/null || true
done
    echo "    内嵌库数量：$(ls -1 "$LIB_DEST" 2>/dev/null | wc -l)"
fi

# 6) 图标（hicolor 各尺寸 + AppDir 根图标）
echo "==> 安装图标"
for size in 16 24 32 48 64 128 256; do
    d="$APPDIR/usr/share/icons/hicolor/${size}x${size}/apps"
    mkdir -p "$d"
    cp "data/icons/xiatiao-${size}.png" "$d/xiatiao.png"
done
cp data/icons/xiatiao-256.png "$APPDIR/xiatiao.png"

# 7) desktop 文件（文件名用 APP_ID）
cp data/com.xiatiao.player.desktop "$APPDIR/$APP_ID.desktop"

# 8) AppRun 启动脚本（用内嵌 Python，PYTHONPATH 挂系统 gi）
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
# Xiatiao Player AppImage 启动器（内嵌 Python）。
HERE="$(dirname "$(readlink -f "$0")")"
APP="$HERE/usr/lib/xiatiao-player"
PY="$APP/python/bin/python3"

if [ ! -x "$PY" ]; then
    echo "Xiatiao Player: 内嵌 Python 运行时缺失（$PY）。" >&2
    exit 1
fi

# 1) 内嵌 Python 包（numpy/mutagen/yaml）优先，gi 等走系统 dist-packages。
#    必须把内嵌 site-packages 显式置于最前，否则系统同名包会被优先导入。
EMBED_SITE="$APP/python/lib/python3.14/site-packages"
export PYTHONPATH="$EMBED_SITE:$APP${PYTHONPATH:+:$PYTHONPATH}:/usr/lib/python3/dist-packages"

# 2) 内嵌 ffmpeg/ffprobe 优先于系统；其库闭包通过 LD_LIBRARY_PATH 暴露。
export PATH="$APP/bin${PATH:+:$PATH}"
export LD_LIBRARY_PATH="$APP/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# 依赖检查（GTK4 / Adw / PyGObject —— 这些仍来自系统）
if ! "$PY" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw' 2>/dev/null; then
    echo "Xiatiao Player: 系统缺少 GTK4/Adw/PyGObject 运行库。" >&2
    echo "请安装（Debian/Ubuntu）：" >&2
    echo "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gdkpixbuf-2.0" >&2
    exit 1
fi

# 系统音频运行环境（PipeWire / ALSA）由主机提供，属系统服务/内核接口，不内嵌。

exec "$PY" "$APP/main.py" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# 9) 守卫：确认没有把图形驱动层混进 AppDir
echo "==> 校验未内嵌图形驱动层"
driver_excluded

# 10) 打包成 AppImage
echo "==> 打包 AppImage"
OUT="$PROJ/Xiatiao-Player-${ARCH}.AppImage"
cd "$BUILD"
ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "$OUT"

chmod +x "$OUT"
echo "==> 完成：$OUT"
ls -la "$OUT"
