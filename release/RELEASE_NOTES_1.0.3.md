# Xiatiao Player 1.0.3

**发布日期**：2026-09-25

本次发布聚焦 DLNA 投送、交互修复与界面打磨，并补齐 Debian 运行时依赖。

---

## 新增

### DLNA 投送

- 扫描改为枚举网卡逐个发 SSDP：绕开代理抢占默认路由导致 SSDP 组播发不出去的问题。
- ssdp:all 查询：兼容非标准设备（如小爱音箱）。
- 按 deviceType 过滤：只保留 MediaRenderer，避免混入无关设备。
- 投送对话框：打开时立即显示已连接设备；刷新按 UDN 合并，避免重复条目。

## 修复

### 右键菜单点击无反应（歌曲列表 / 歌单卡片 / 队列行）

ColumnView 单元格上的临时 Popover 无法可靠解析挂在其祖先上的 Gio 动作组，菜单项会灰显或点击无反应。改用 Gtk.Popover + 按钮直连回调，绕开 Gio 动作解析。

## 界面

- 沉浸页歌名 / 歌手跑马灯：超长文本横向滚动；左侧面板跑马灯左右渐隐。
- 沉浸页歌词背景：上下暗角，边缘歌词渐隐；歌词渐隐改为单层实现，消除色带。
- 底部 Tab：新增选中指示；容器加阴影；顺序调整为 Queue / Player / Lyrics；切页不强制重建（更顺滑）。
- 右上角菜单：改为圆底三圆点图标。
- 主页：区块标题间距收紧、卡片顶部留白减小；去掉媒体卡片 hover 的整卡底色与阴影。

## 打包

- 补齐运行时依赖：python3-cairo、librsvg2-common（SVG 图标）、webp-pixbuf-loader（WebP 封面）、iproute2（DLNA 扫描）、libglib2.0-bin（回收站）、xdg-utils、dbus-bin | dbus。
- dbus-bin 加 dbus 别名兜底，兼容未拆分该包的旧发行版。

---

## 安装

    sudo apt install ./xiatiao-player_1.0.3_amd64.deb

## 产物

| 文件 | 说明 | 大小 |
|---|---|---|
| xiatiao-player_1.0.3_amd64.deb | 主安装包 | ~1.7 MB |
| xiatiao-player-dbgsym_1.0.3_amd64.deb | 调试符号（可选） | ~140 KB |

## 兼容性

在 Debian 12（bookworm, glibc 2.36）基线上构建，Rust 后端最高只需 GLIBC_2.34。由于 glibc 向上兼容，一个包即可覆盖：

- Debian 12 / 13 / 14
- Ubuntu 22.04 / 24.04 及更新
- 其它 glibc ≥ 2.34 的 Debian 系发行版

无需为每个发行版单独打包。

---

## 完整变更

见 debian/changelog。
