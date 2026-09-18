# 虾条播放器 Xiatiao Player

GTK4 界面的本地音乐播放器，音频后端用 Rust。名字谐音「瞎调」与「虾条」。

## 功能

- 本地曲库扫描（mp3/flac/ape/wv/m4a/ogg/wav/dsf/dff）
- 保持源采样率，不重采样；DSP 关闭时 bit-perfect 直通
- 采样率跟随（原生 PipeWire）
- 音效：EQ/PEQ/低音/高音/Loudness/压缩/限幅/宽度/平衡/Crossfeed/混响 等
- 内嵌 CamillaDSP，参数平滑更新
- 解码：symphonia + ffmpeg
- 沉浸式全屏页、歌词、可视化、收藏、歌单、历史
- MPRIS2、系统托盘

## 架构

Python(GTK4 UI) <-- IPC(Unix socket + JSON) --> Rust(音频后端)

## 构建运行（Debian/Ubuntu）

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-mutagen pipewire ffmpeg cargo rustc
    cd audio_backend_rs && cargo build --release && cd ..
    python3 main.py

## 许可证

GPL-3.0（因内嵌 CamillaDSP）。详见 LICENSE。
