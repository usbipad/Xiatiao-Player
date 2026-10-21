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

# 0) 前置依赖检查：缺东西时提前报错并给出安装命令，
#    避免 dpkg-buildpackage 跑到一半才失败、日志难读。
MISSING=()
for cmd in dpkg-buildpackage dh cargo rustc pkg-config; do
    command -v "$cmd" >/dev/null 2>&1 || MISSING+=("$cmd")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "错误：缺少构建命令：${MISSING[*]}" >&2
    echo "请先安装：" >&2
    echo "  sudo apt install debhelper-compat cargo rustc pkg-config \\" >&2
    echo "      libasound2-dev libpipewire-0.3-dev libclang-dev" >&2
    exit 1
fi
# dpkg-checkbuilddeps 按 debian/control 的 Build-Depends 校验包依赖
# （覆盖命令行找不到、但实际缺失的 -dev 库等）。
# 注意：它按 dpkg 包名判定，若 Rust 是用 rustup 装的（而非 apt 的 rustc 包），
# 这里会误报「缺 rustc」。故失败只降级为警告——上面的命令行检查已保证能跑。
if ! dpkg-checkbuilddeps 2>/dev/null; then
    echo "警告：dpkg-checkbuilddeps 报告以下依赖未满足（若 rust 用 rustup 安装属正常）：" >&2
    dpkg-checkbuilddeps >&2 || true
    echo "如确有问题，可运行：sudo apt-get build-dep ." >&2
fi

cd "$PROJ"

# 1) 确保 vendor 目录存在（离线构建依赖）。
#    vendor/ 体积较大且未入库，别人克隆后首次构建自动补齐。
if [ ! -d audio_backend_rs/vendor ]; then
    echo "==> vendor 目录缺失，运行 cargo vendor"
    (cd audio_backend_rs && cargo vendor vendor >/dev/null)
fi

# 2) 本机直接构建二进制包。
#    -B   仅构建 arch 相关二进制包（build-arch + binary-arch；
#         debian/rules 已声明 binary-arch: build-arch，确保先编译再打包）。
#         注意：-B 已隐含「只出二进制」，不能再叠加 -b（二者互斥，dpkg 会报错）。
#    -us  不签名源码包
#    -uc  不签名 .changes
echo "==> dpkg-buildpackage 本机构建（$VER, $ARCH）"
dpkg-buildpackage -us -uc -B

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
