# Xiatiao Player 1.0.2

本次发布聚焦**稳定性修复**与**界面细节打磨**。

---

## 修复

- **GTK4 段错误崩溃**（SIGSEGV in gtk_widget_unparent）
  右键菜单等临时 Popover 只 `set_parent` 未 `unparent`，父控件销毁时
  GTK 访问已释放对象导致崩溃（在 GTK 4.22 上必现）。已为全部临时 popover
  补解父。
- **切歌后歌词不显示**
  `_apply_track_assets` 引用了未定义变量抛异常、被静默 except 吞掉，
  连带进度条跟随色、主界面背景跟随一并失效。已修正。
- **系统托盘 GLib-CRITICAL**
  dbusmenu 方法返回值未按 DBus 规范用元组包裹，`GetGroupProperties`
  会超时、部分桌面托盘菜单无法弹出。已修正。

## 新增 / 优化

- **音效弹窗重构**：由居中对话框改为从音效按钮旁弹出的气泡，主页与沉浸页
  共用；背景跟随主界面背景色。
- **音量滚轮**：主页与沉浸页的音量图标支持鼠标滚轮调节；图标随音量大小变化。
- **专辑/艺术家卡片悬停**：鼠标悬停时浮起（上浮 + 阴影 + 封面轻放大）。
- **沉浸页控制栏按钮重排**。
- **统一调试入口**：环境变量 `XIATIAO_DEBUG` 开启全量诊断日志
  （见 `docs/DEBUG.md`）。

## 下载

| 文件 | 说明 | 大小 |
|---|---|---|
| `xiatiao-player_1.0.2_amd64.deb` | 主安装包 | ~1.7 MB |
| `xiatiao-player-dbgsym_1.0.2_amd64.deb` | 调试符号（可选） | ~140 KB |

## 安装

    sudo apt install ./xiatiao-player_1.0.2_amd64.deb

## 覆盖范围

在 Debian 12（glibc 2.36）基线构建，Rust 后端最高只需 **GLIBC_2.34**，
一个包覆盖 Debian 12/13/14 与 Ubuntu 22.04/24.04 及更新版本。

---

完整变更见 `debian/changelog`。
