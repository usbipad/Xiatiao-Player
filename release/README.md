# 发布产物（Release Artifacts）

本目录存放可分发到 GitHub Releases 的构建产物。
**产物本身不纳入 git**（见 `.gitignore`），因为可由构建脚本重建。

## 当前产物

| 文件 | 说明 |
|---|---|
| xiatiao-player_1.0.3_amd64.deb | 主安装包 |
| xiatiao-player-dbgsym_1.0.3_amd64.deb | 调试符号（可选） |

## 覆盖范围

本包在 Debian 12（bookworm, glibc 2.36）基线上构建，Rust 后端最高只需
GLIBC_2.34。由于 glibc 向上兼容，一个包即可覆盖：

- Debian 12 (bookworm)
- Debian 13 (trixie)
- Debian 14 (forky)
- Ubuntu 22.04 / 24.04 及更新

无需为每个发行版单独打包。

## 如何重建

一次性生成构建 chroot：

    bash tools/prepare_debian12_chroot.sh

构建并输出到本目录：

    bash tools/build_deb_debian12.sh

## 安装

    sudo apt install ./xiatiao-player_1.0.3_amd64.deb

详见项目根目录 HANDOVER.md 第 9 节。
