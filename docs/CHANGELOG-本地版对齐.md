# 本地版 UI 对齐改动记录

> 时间：2026-09-23
> 范围：`xiatiao-player/`（纯本地版）
> 方式：以「带汽水版」为参照，对齐**纯界面**部分（**不含任何汽水后端逻辑**）

---

## 一、顶部导航

- **结构**（`ui/window.py`）：导航按钮包进 `.nav-segmented-bar` 胶囊容器
- **样式**（`ui/style.css`）：新增 `.nav-segmented-bar`；`.nav-pill` 换新版（间距/字重/过渡）；`.nav-pill.nav-active` 渐变+投影；裸 `.nav-active` 同步换渐变

## 二、卡片（专辑/艺术家网格）

- **构建**（`ui/pages/media_grid.py`）：
  - `CARD_COVER_PX` 150 → 184
  - 新增阴影留白常量（`CARD_SHADOW_PAD` / `CARD_COVER_INSET` / `CARD_EDGE_GAP`）
  - 新增辅助函数：`_title_max_chars` / `_make_cover_content` / `_set_cover_content` / `_make_placeholder`
  - `make_group_card` 重写（容器留白、封面显示尺寸、占位容器、`_apply_size`）
- **样式**（`ui/style.css`）：`.media-card-cover` 阴影、`.media-card-title` / `-subtitle`、`.media-card-placeholder`
- **间距**（`ui/pages/__init__.py`）：FlowBox 行列间距 8 → 4

## 三、左侧播放器面板

- **布局**（`ui/player_panel.py`）：面板边距 10/14、inner spacing 10、封面切换动画 SLIDE、控制行间距 12
- **底部 Tab**：加 `.player-tab-bar` 胶囊容器；Lyrics 图标换 `xiatiao-lyrics-symbolic`
- **删除**：下载 / 分享按钮（+ 图标文件、`_toast_func`）
- **随机按钮激活态**：`suggested-action` → `np-toggle-on`

## 四、封面（左侧面板）

- **`ui/widgets/cover_art.py`**：去掉 `.cover-shade` 厚度层；加阴影留白 `_SHADOW_PAD=22` / `_SHADOW_BOTTOM_EXTRA=12`
- **样式**：`.cover-shadow` 由 `none` → 外阴影 `0 11px 20px` + `0 3px 8px`

## 五、沉浸式播放页

- **`ui/now_playing.py`**：
  - 音量按钮：`MenuButton` → `Button` + 手动 Popover（跟随主题变色）
  - 控制栏重排：音量移最左，加音效按钮，随机移到循环后
  - 播放键 CSS provider（按封面主色染色）
  - 随机激活态改 `np-toggle-on`
  - 封面阴影层（三层：`shadow_box` 留白 → `shadow_layer` 画阴影 → `cover_frame` 裁圆角）
  - 新增 `_current_theme_dark` / `refresh_dark_bg`（背景取色关闭时前景跟系统）
- **样式**：歌词行（字号/字重/光晕）、播放按钮（微透纽扣）、进度条、面板辅助按钮、暗背景规则、`.np-title`
- **`ui/window.py`**：`NowPlayingPage` 构造补 `on_effect`

## 六、背景色算法

- **`models/coverart.py`**：
  - `tint_for_background` 加 `dark` 参数（暗色模式压暗）
  - `make_blurred_bg` 替换为新模糊算法
- **`services/track_assets.py`**：加 `dark_theme` 参数、返回 `dominant_rgb`、`seekbar_rgb` 受进度条开关控制
- **`ui/window.py`**：
  - 缓存封面主色 `_current_dominant_rgb`
  - 订阅系统明暗 `notify::dark` → 重算主界面背景 + 沉浸页前景
  - `reapply_progress_color` / `_current_theme_dark`

## 七、进度条

- **`ui/widgets/seek_bar.py`**（自绘）：
  - `BAR_H` 4→5、`THUMB_R` 5→7
  - 已播段**优先用封面主色**（不受暗背景强制白色）
  - 滑块加极淡柔光
- **新增设置**（`config/settings.py` + `ui/settings_dialog.py` + `ui/window.py`）：
  - `progress_follow_cover` 开关（默认 True）
  - 关闭时进度条回退默认深灰/白

## 八、歌曲列表

- **`ui/pages/song_list.py`**：无内嵌封面的曲目**显示占位**（圆角底 + 音符图标）
- **`ui/window.py`**：队列列表**始终刷新**（不再依赖 Queue 是否可见）

## 九、其它样式对齐

- 菜单 `.media-menu`：半透明 → 不透明 + 去白环 + 投影
- `row.effect-selected`（音效选中/播放高亮）：竖条 4px + 圆角 + 投影 + label 650
- 音质徽标（`.hires-badge` / `.cd-badge` / `.dsd-badge`）：渐变宝石质感

## 十、图标

- 新增：`xiatiao-lyrics-symbolic.svg`
- 删除：`xiatiao-download-symbolic.svg` / `xiatiao-share-symbolic.svg`

## 十一、清理

- 删除无效 CSS：`.np-progress`（自绘进度条不读 CSS）、`.hot-key-chip`、`.cover-fixed`、`.np-title`、`.media-card` + `:hover` + `.media-card:hover .media-card-cover`、`.media-card-badge`、`.media-card-placeholder-box`
- 删除死方法：`_toast_func`
- 删除调试日志

---

## 排除项（汽水相关，未移植）

- `ui/qishui_*.py`、`core/qishui_bridge.py`、`providers/qishui.py`
- `tools/qishui_bridge/`、`ly-music-source/`
- `window.py` / `settings_dialog.py` 里的汽水分支
- `hide_header_widget`、`load_cover_bytes_from_url`、分页/加载更多等汽水支撑逻辑

---

## 注意

- 本次改动**仅本地工作区**，未 commit / 未 push
- 原文件在 git 中可恢复（`git checkout -- <file>`）
