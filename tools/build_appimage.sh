#!/usr/bin/env bash
# 构建 Xiatiao Player 的 AppImage。
#
# 方案说明（务实）：
#   AppImage 内含：项目 Python 代码 + Rust 音频后端 + 图标/desktop。
#   运行依赖：系统的 python3 + PyGObject(GTK4/Adw) + numpy + pipewire + ffmpeg。
#   原因：GTK4 + PyGObject 的 GI typelib 路径与上百个共享库极难安全内联，
#   强行打包易碎且臃肿；桌面系统（GNOME/KDE）通常已具备这些依赖。
#
# 若需完全自包含，请改用 Flatpak（GNOME runtime 已含 GTK4/Adw）。
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${XIATIAO_APPIMAGE_BUILD:-/tmp/xiatiao-appimage}"
APPDIR="$BUILD/Xiatiao.AppDir"
APP_ID="com.xiatiao.player"
ARCH="${ARCH:-x86_64}"

# appimagetool：优先环境变量，其次 /tmp/appimage-build/
APPIMAGETOOL="${APPIMAGETOOL:-/tmp/appimage-build/appimagetool}"
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "错误：找不到 appimagetool（$APPIMAGETOOL）" >&2
    echo "下载：curl -L -o /tmp/appimage-build/appimagetool \\" >&2
    echo "  https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" >&2
    exit 1
fi

echo "==> 清理构建目录"
rm -rf "$BUILD"
mkdir -p "$APPDIR/usr/lib/xiatiao-player"

cd "$PROJ"

# 1) Rust 后端（若缺失则编译）
BACKEND="audio_backend_rs/target/release/xiatiao-audio-backend"
if [ ! -f "$BACKEND" ]; then
    echo "==> 编译 Rust 后端"
    (cd audio_backend_rs && cargo build --release)
fi

# 2) 项目代码 + 运行时资源
#    注意：排除 __pycache__、tools、tests、docs 等非运行必需项。
echo "==> 复制项目代码"
for item in main.py config core models providers services ui data; do
    cp -r "$item" "$APPDIR/usr/lib/xiatiao-player/"
done
install -m755 "$BACKEND" "$APPDIR/usr/lib/xiatiao-player/xiatiao-audio-backend"

# 清理复制进来的 __pycache__（保持 AppImage 干净）
find "$APPDIR/usr/lib/xiatiao-player" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 3) 图标（hicolor 各尺寸 + AppDir 根图标）
echo "==> 安装图标"
for size in 16 24 32 48 64 128 256; do
    d="$APPDIR/usr/share/icons/hicolor/${size}x${size}/apps"
    mkdir -p "$d"
    cp "data/icons/xiatiao-${size}.png" "$d/xiatiao.png"
done
cp data/icons/xiatiao-256.png "$APPDIR/xiatiao.png"

# 4) desktop 文件（文件名用 APP_ID）
cp data/com.xiatiao.player.desktop "$APPDIR/$APP_ID.desktop"

# 5) AppRun 启动脚本
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
# Xiatiao Player AppImage 启动器。
HERE="$(dirname "$(readlink -f "$0")")"
APP="$HERE/usr/lib/xiatiao-player"

# 依赖检查（GTK4 / Adw / PyGObject）
if ! python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw' 2>/dev/null; then
    echo "Xiatiao Player: 缺少运行依赖。" >&2
    echo "请安装（Debian/Ubuntu）：" >&2
    echo "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gdkpixbuf-2.0" >&2
    echo "推荐（功能完整）：python3-numpy python3-mutagen python3-yaml ffmpeg pipewire pipewire-bin gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0" >&2
    exit 1
fi

exec python3 "$APP/main.py" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# 6) 打包成 AppImage
echo "==> 打包 AppImage"
OUT="$PROJ/Xiatiao-Player-${ARCH}.AppImage"
cd "$BUILD"
ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "$OUT"

chmod +x "$OUT"
echo "==> 完成：$OUT"
ls -la "$OUT"
