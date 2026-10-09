# Xiatiao Player 1.0.4

**发布日期**：2026-09-28

本次发布聚焦 **DLNA 投送改进**，并新增 Fedora RPM 包。

---

## 新增

### Fedora RPM 包

新增原生 RPM 包（Fedora 44），与 deb 包并列：

- `xiatiao-player-1.0.4-1.fc44.x86_64.rpm`

RPM 与 deb 均为原生安装（非沙箱），完整支持 DSD 直通、ALSA 独占等音质特性。

## 改进

### DLNA 投送

- **播放结束自动下一首**：投送播放完后按播放列表自动推进。
- **遵循播放模式**：顺序 / 列表循环 / 单曲循环 / 随机 / 不循环，全部按你的设置执行。
  - 单曲循环时重播当前曲目。
- 补充 DLNA 响应头（`transferMode` / `contentFeatures`），提升部分设备的兼容性。

---

## 安装

### Debian / Ubuntu

    sudo apt install ./xiatiao-player_1.0.4_amd64.deb

### Fedora

    sudo dnf install ./xiatiao-player-1.0.4-1.fc44.x86_64.rpm

## 产物

| 文件 | 说明 | 大小 |
|---|---|---|
| xiatiao-player_1.0.4_amd64.deb | Debian/Ubuntu 安装包 | ~1.7 MB |
| xiatiao-player-dbgsym_1.0.4_amd64.deb | 调试符号（可选） | ~141 KB |
| xiatiao-player-1.0.4-1.fc44.x86_64.rpm | Fedora 44 安装包 | ~1.9 MB |

## 兼容性

- **deb**：在 Debian 12（bookworm, glibc 2.36）基线上构建，Rust 后端最高只需 **GLIBC_2.34**。覆盖 Debian 12 / 13 / 14 与 Ubuntu 22.04 / 24.04 及更新。
- **rpm**：Fedora 44 专用（不跨 Fedora 版本）。

---

## 完整变更

见 debian/changelog。
