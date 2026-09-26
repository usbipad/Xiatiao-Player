Name:           xiatiao-player
Version:        1.0.3
Release:        1%{?dist}
Summary:        GTK4 local music player with a Rust audio backend

License:        GPL-3.0-or-later
URL:            https://github.com/usbipad/Xiatiao-Player
Source0:        %{url}/archive/v%{version}/Xiatiao-Player-%{version}.tar.gz

BuildRequires:  cargo
BuildRequires:  rust
BuildRequires:  pkgconf-pkg-config
BuildRequires:  alsa-lib-devel
BuildRequires:  pipewire-devel
BuildRequires:  clang-devel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib

Requires:       python3
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4
Requires:       libadwaita
Requires:       gdk-pixbuf2
Requires:       gstreamer1
Requires:       gstreamer1-plugins-base
Requires:       librsvg2
Requires:       alsa-lib
Requires:       pipewire-libs
Requires:       python3-mutagen
Requires:       python3-numpy
Requires:       python3-pyyaml
Requires:       ffmpeg-free
Requires:       pipewire
Requires:       pipewire-utils
Requires:       pulseaudio-utils
Requires:       iproute
Requires:       glib2
Requires:       xdg-utils
Requires:       dbus-tools
Requires:       python3-setproctitle

%description
Xiatiao Player is a local music player with a GTK4 interface and a Rust
audio backend. It supports a wide range of formats, keeps the source
sample rate without resampling, and provides a DSP chain (EQ / PEQ /
loudness / limiter / etc.) via an embedded CamillaDSP.

Features:
  - Local library scan (mp3/flac/ape/wv/m4a/ogg/wav/dsf/dff)
  - Bit-perfect passthrough, sample-rate following (native PipeWire)
  - Embedded CamillaDSP with smooth parameter updates
  - Immersive full-screen page, lyrics, visualization
  - DLNA casting, MPRIS2 and system tray support

%prep
%autosetup -n Xiatiao-Player-%{version}

# 移除项目级 cargo 配置：它强制使用 vendor/（离线依赖目录），
# 但 vendor/ 未纳入版本控制，源码包中不存在。
# 删除后 cargo 走默认的 crates.io 源，由构建环境联网拉取依赖。
rm -f audio_backend_rs/.cargo/config.toml

%build
cd audio_backend_rs
cargo build --release
cd ..

%install
# Python sources + data -> /usr/lib/xiatiao-player
install -d %{buildroot}%{_libdir}/xiatiao-player
cp -r main.py config core models providers services ui \
    %{buildroot}%{_libdir}/xiatiao-player/
install -d %{buildroot}%{_libdir}/xiatiao-player/data
cp -r data/icons %{buildroot}%{_libdir}/xiatiao-player/data/

# Rust backend binary
install -m 0755 audio_backend_rs/target/release/xiatiao-audio-backend \
    %{buildroot}%{_libdir}/xiatiao-player/

# App icons -> hicolor
for size in 16 24 32 48 64 128 256; do
  install -d %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps
  cp data/icons/xiatiao-${size}.png \
     %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps/xiatiao.png
done
install -d %{buildroot}%{_datadir}/icons/hicolor/scalable/actions
cp data/icons/hicolor/scalable/actions/*.svg \
   %{buildroot}%{_datadir}/icons/hicolor/scalable/actions/ 2>/dev/null || true

# Launcher -> /usr/bin/xiatiao-player
install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/xiatiao-player <<EOF
#!/bin/sh
exec python3 %{_libdir}/xiatiao-player/main.py "\$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/xiatiao-player

# Desktop entry
install -d %{buildroot}%{_datadir}/applications
cp data/com.xiatiao.player.desktop \
   %{buildroot}%{_datadir}/applications/com.xiatiao.player.desktop

%post
touch --no-create %{_datadir}/icons/hicolor &>/dev/null || :
gtk-update-icon-cache %{_datadir}/icons/hicolor &>/dev/null || :
update-desktop-database %{_datadir}/applications &>/dev/null || :

%postun
touch --no-create %{_datadir}/icons/hicolor &>/dev/null || :
gtk-update-icon-cache %{_datadir}/icons/hicolor &>/dev/null || :
update-desktop-database %{_datadir}/applications &>/dev/null || :

%files
%license LICENSE
%{_bindir}/xiatiao-player
%{_datadir}/applications/com.xiatiao.player.desktop
%{_datadir}/icons/hicolor/*/apps/xiatiao.png
%{_datadir}/icons/hicolor/scalable/actions/*.svg
%{_libdir}/xiatiao-player/

%changelog
* Fri Sep 26 2026 usbipad <usbipad@163.com> - 1.0.3-1
- Initial RPM package (1.0.3)
