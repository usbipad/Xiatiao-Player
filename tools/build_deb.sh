#!/usr/bin/env bash
# 虾条播放器 — 本机 Debian 打包脚本
#
# 直接在本机系统上编译并打出 .deb，不使用 chroot / sbuild。
# 适合在开发机或与目标系统 glibc 版本相近的环境里快速出包。
#
# 注意：本机打包的产物 glibc 需求随本机系统而定。若需兼容较旧的
#       发行版（Debian 12 / Ubuntu 22.04），请用 chroot 基线构建
#       （见本地专用的 tools/build_deb_debian12.sh，不入库）。
#
# 前置：
#   sudo apt install debhelper-compat cargo rustc pkg-config \
#       libasound2-dev libpipewire-0.3-dev libclang-dev
#
# 用法：
#   bash tools/build_deb.sh
# 产物：
#   release/ 目录下的 xiatiao-player_<版本>_<架构>.deb
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE="$PROJ/release"

# 版本号：从 debian/changelog 首行解析（唯一真相，避免多处硬编码漂移）。
VER="$(head -1 "$PROJ/debian/changelog" | sed -E 's/^[^(]+\(([^)]+)\).*/\1/')"
[ -n "$VER" ] || { echo "错误：无法从 debian/changelog 解析版本号" >&2; exit 1; }
ARCH="$(dpkg --print-architecture)"

cd "$PROJ"

# 1) 确保 vendor 目录存在（离线构建依赖）。
#    vendor/ 体积较大且未入库，别人克隆后首次构建自动补齐。
if [ ! -d audio_backend_rs/vendor ]; then
    echo "==> vendor 目录缺失，运行 cargo vendor"
    (cd audio_backend_rs && cargo vendor vendor >/dev/null)
fi

# 2) 本机直接构建二进制包。
#    -b   仅构建二进制包（不打源码包）
#    -us  不签名源码包
#    -uc  不签名 .changes
#    -B   build-arch + binary-arch（debian/rules 已声明 binary-arch: build-arch）
echo "==> dpkg-buildpackage 本机构建（$VER, $ARCH）"
dpkg-buildpackage -b -us -uc -B

# 3) 产物收拢到 release/，并清理构建中间产物。
mkdir -p "$RELEASE"
mv -f "$PROJ"/../xiatiao-player_${VER}_${ARCH}.deb "$RELEASE"/ 2>/dev/null || \
    mv -f "$PROJ"/xiatiao-player_${VER}_${ARCH}.deb "$RELEASE"/ 2>/dev/null || true
mv -f "$PROJ"/../xiatiao-player-dbgsym_${VER}_${ARCH}.deb "$RELEASE"/ 2>/dev/null || \
    mv -f "$PROJ"/xiatiao-player-dbgsym_${VER}_${ARCH}.deb "$RELEASE"/ 2>/dev/null || true

# 清理构建中间产物（.buildinfo / .changes / 残留 deb）。
rm -f "$PROJ"/../xiatiao-player_${VER}_${ARCH}.buildinfo \
      "$PROJ"/../xiatiao-player_${VER}_${ARCH}.changes \
      "$PROJ"/../xiatiao-player_${VER}_${ARCH}.build \
      "$PROJ"/xiatiao-player_${VER}_${ARCH}.deb \
      "$PROJ"/xiatiao-player-dbgsym_${VER}_${ARCH}.deb 2>/dev/null || true

echo "==> 完成。产物："
ls -lh "$RELEASE"/xiatiao-player_${VER}_${ARCH}.deb 2>/dev/null || \
    echo "（未找到产物，请检查上方 dpkg-buildpackage 输出）"
