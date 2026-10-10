# Arch Linux（AUR）打包

本目录提供 Arch Linux 的 PKGBUILD，附带提供、未在 Arch 环境实测。

## 未测试

作者不使用 Arch，此 PKGBUILD 未经实际构建验证。若你在 Arch 上构建或使用遇到问题，欢迎反馈：

- 提交 Issue：https://github.com/usbipad/Xiatiao-Player/issues

## 使用

在本目录下运行：

    makepkg -si

## 说明

- 构建时从 GitHub Release 下载对应版本的源码 tarball，联网拉取 Rust 依赖并编译后端。
- sha256sums 当前为 SKIP（源码 tarball 发布后可用 updpkgsums 补真实校验值）。
- 依赖列表若与你的 Arch 环境有出入，请反馈。
