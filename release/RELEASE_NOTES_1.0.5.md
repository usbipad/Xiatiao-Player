# Xiatiao Player 1.0.5

**发布日期**：2026-10-10

本次发布聚焦 **内存优化** 与 **在线播放稳定性**。

---

## 改进

### 内存优化

- 缓解切歌、开关页面时的内存增长，长时间使用更稳定。

### 在线播放

- 修复进度回跳、切歌爆音等问题，播放更顺畅。
- 增强在线音源加载的健壮性。

---

## 安装

### Debian / Ubuntu

    sudo apt install ./xiatiao-player_1.0.5_amd64.deb

### Fedora

    sudo dnf install ./xiatiao-player-1.0.5-1.fc44.x86_64.rpm

## 兼容性

- **deb**：Rust 后端最高只需 **GLIBC_2.34**，覆盖 Debian 12 / 13 / 14 与 Ubuntu 22.04 / 24.04 及更新。
- **rpm**：Fedora 专用。
