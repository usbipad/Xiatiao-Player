#!/usr/bin/env bash
# 在 Debian 12 (bookworm, glibc 2.36) 基线上构建 xiatiao-player 的 .deb。
#
# 为什么需要这个脚本：
#   Rust 后端若在较新系统（如 Debian sid, glibc 2.43）上编译，产物会要求
#   GLIBC_2.43，无法在 Debian 12 / Ubuntu 22.04 等较旧系统运行。
#   本脚本用 sbuild + mmdebstrap 在一个 Debian 12 chroot 里构建，
#   使产物最高只需 GLIBC_2.34，覆盖 Debian 12+ / Ubuntu 22.04+。
#
# 前置条件（一次性）：
#   1. 安装 sbuild 与 mmdebstrap：sudo apt install sbuild mmdebstrap
#   2. 生成 chroot tarball（含 libclang-dev 与 rustup 的新版 Rust）：
#        bash tools/prepare_debian12_chroot.sh
#
# 用法：
#   bash tools/build_deb_debian12.sh
# 产物：
#   项目内 release/ 目录下的 xiatiao-player_1.0.1_amd64.deb
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(dirname "$PROJ")"
RELEASE="$PROJ/release"
CHROOT="${XIATIAO_DEBIAN12_CHROOT:-$HOME/.cache/sbuild/bookworm-rust-amd64.tar}"

# 版本号：从 debian/changelog 首行解析（唯一真相，避免多处硬编码漂移）。
VER="$(head -1 "$PROJ/debian/changelog" | sed -E 's/^[^(]+\(([^)]+)\).*/\1/')"
[ -n "$VER" ] || { echo "错误：无法从 debian/changelog 解析版本号" >&2; exit 1; }
SRC_BASE="xiatiao-player_${VER}"

if [ ! -f "$CHROOT" ]; then
    echo "错误：找不到 Debian 12 chroot tarball：$CHROOT" >&2
    echo "请先运行：bash tools/prepare_debian12_chroot.sh" >&2
    exit 1
fi

cd "$PROJ"

# 1) 确保 vendor 目录存在（离线构建依赖）
if [ ! -d audio_backend_rs/vendor ]; then
    echo "==> vendor 目录缺失，运行 cargo vendor"
    (cd audio_backend_rs && cargo vendor vendor >/dev/null)
fi

# 2) 生成源码包（.dsc + .tar.xz）
echo "==> 生成源码包（$VER）"
rm -f "$PARENT"/${SRC_BASE}.tar.xz "$PARENT"/${SRC_BASE}.dsc 2>/dev/null || true
dpkg-source -b . >/dev/null

# 3) 用 sbuild 在 Debian 12 chroot 里构建
#    --arch-any --no-arch-all：只构建架构相关包
#    --debbuildopts=-B：跑 build-arch + binary-arch
#    （debian/rules 里已声明 binary-arch: build-arch，确保先编译再打包）
echo "==> sbuild 构建（Debian 12 基线）"
sbuild \
    --chroot-mode=unshare \
    --chroot="$CHROOT" \
    --dist=bookworm \
    --arch=amd64 \
    --arch-any --no-arch-all \
    --no-run-lintian \
    --debbuildopts='-B' \
    "$PARENT/${SRC_BASE}.dsc"

# 3b) 把产物收拢到 release/
#     注意：脚本已 cd 到 $PROJ（仓库根），sbuild 把产物输出到 CWD = $PROJ，
#     不是 $PARENT（上级）。此处从 $PROJ 取，并清理构建中间产物。
mkdir -p "$RELEASE"
mv -f "$PROJ"/xiatiao-player_${VER}_amd64.deb "$RELEASE"/ 2>/dev/null || true
mv -f "$PROJ"/xiatiao-player-dbgsym_${VER}_amd64.deb "$RELEASE"/ 2>/dev/null || true
# 清理构建中间产物。
# 注意：sbuild 的日志会在版本号后插入时间戳
# （xiatiao-player_1.0.1_amd64-2026-09-21T04:22:10Z.build），
# 所以 glob 必须是 _amd64*.build*，否则匹配不到、日志每次残留。
rm -f "$PROJ"/xiatiao-player_${VER}_amd64*.build* \
      "$PROJ"/xiatiao-player_${VER}_amd64.changes \
      "$PROJ"/xiatiao-player_${VER}_amd64.buildinfo \
      "$PROJ"/${SRC_BASE}.dsc \
      "$PROJ"/${SRC_BASE}.tar.* 2>/dev/null || true
# 源码包由 dpkg-source -b 生成在仓库的上级目录，一并清理（28M+，留着没意义）
rm -f "$PARENT"/${SRC_BASE}.dsc \
      "$PARENT"/${SRC_BASE}.tar.* 2>/dev/null || true

echo "==> 完成。产物："
ls -lh "$RELEASE"/xiatiao-player_${VER}_amd64.deb

# 4) 校验 glibc 需求
BACKEND=$(mktemp -d)/xiatiao-audio-backend
dpkg-deb --fsys-tarfile "$RELEASE/xiatiao-player_${VER}_amd64.deb" \
    | tar -xO ./usr/lib/xiatiao-player/xiatiao-audio-backend > "$BACKEND" 2>/dev/null || true
if [ -s "$BACKEND" ]; then
    echo "==> Rust 后端最高 glibc 需求："
    objdump -T "$BACKEND" 2>/dev/null | grep -oE 'GLIBC_[0-9.]+' | sort -V -u | tail -1
fi
