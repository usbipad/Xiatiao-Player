#!/usr/bin/env bash
# 生成用于构建 xiatiao-player 的 Debian 12 (bookworm) chroot tarball。
#
# 目标：
#   - 基础系统为 Debian 12（glibc 2.36），使产物兼容 Debian 12+ / Ubuntu 22.04+。
#   - 预装 rustup 的新版 Rust（Debian 12 自带的 1.63 太旧，无法解析 Cargo.lock v4，
#     且项目依赖 camilladsp-src 要求 Rust >= 1.90）。
#   - 预装 libclang-dev（libspa-sys 的 bindgen 需要 libclang）。
#
# 前置：sudo apt install sbuild mmdebstrap
# 产物：~/.cache/sbuild/bookworm-rust-amd64.tar
set -euo pipefail

OUT="${XIATIAO_DEBIAN12_CHROOT:-$HOME/.cache/sbuild/bookworm-rust-amd64.tar}"
mkdir -p "$(dirname "$OUT")"

echo "==> 生成 Debian 12 chroot（含 libclang-dev + rustup 新版 Rust）"
mmdebstrap \
    --mode=unshare \
    --variant=buildd \
    --include=cargo,rustc,pkg-config,libasound2-dev,libpipewire-0.3-dev,debhelper-compat,curl,ca-certificates,xz-utils,libclang-dev \
    bookworm "$OUT" \
    --chrooted-customize-hook='
        export RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
        export RUSTUP_INIT_SKIP_PATH_CHECK=yes
        curl -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
        rm -rf /usr/local/cargo/registry /usr/local/rustup/downloads /usr/local/rustup/tmp
    '

echo "==> 完成：$OUT"
ls -lh "$OUT"
