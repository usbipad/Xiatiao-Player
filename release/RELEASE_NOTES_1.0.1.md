# Xiatiao Player 1.0.1

**发布日期**：2026-09-21

本次为维护版本，主要修复首页内存占用问题。

---

## 修复

### 首页展开专辑/艺术家时内存飙升

根因是封面缩图**先整张解码再缩放**：6000x6000 的内嵌封面解码成 RGBA 约 **144 MB**，
而目标尺寸只有 150px。展开分组时首页会并发解码封面（线程池 4 并发），瞬时峰值可达 **576 MB**。

现改为走 `new_from_stream_at_scale`，在**解码阶段**就缩到目标尺寸的 2 倍，
单张封面内存降到几百 KB（旧版 gdk-pixbuf 自动回退到原行为）。

### 折叠视图卡片重复构建

此前每个专辑/艺术家都建两份卡片（展开网格 + 折叠横排），封面被解码两次、控件数量翻倍。
改为**懒构建**，仅在需要折叠时才创建。

### 其它

- 卡片轮播加守卫：折叠视图未构建或页面不可见时不再轮换，避免定时器堆积与无谓解码。
- 构建日志残留：`.gitignore` 补 `*.build`，清理 glob 改为 `_amd64*.build*`。

## 改进

- 消除窗口缩放 / 切页 / 进出沉浸页时的同步重活，提升响应。
- 隐藏歌词区滚动条。

---

## 安装

```bash
sudo apt install ./xiatiao-player_1.0.1_amd64.deb
```

## 产物

| 文件 | 说明 | 大小 |
|---|---|---|
| `xiatiao-player_1.0.1_amd64.deb` | 主安装包 | 1.7 MB |
| `xiatiao-player-dbgsym_1.0.1_amd64.deb` | 调试符号（可选） | 144 KB |

## 兼容性

在 Debian 12（bookworm, glibc 2.36）基线上构建，Rust 后端最高只需 **GLIBC_2.34**。
由于 glibc 向上兼容，一个包即可覆盖：

- Debian 12 / 13 / 14
- Ubuntu 22.04 / 24.04 及更新
- 其它 glibc ≥ 2.34 的 Debian 系发行版

无需为每个发行版单独打包。

---

## 完整变更

见 [`debian/changelog`](https://github.com/usbipad/Xiatiao-Player/blob/master/debian/changelog)。
