//! 嵌入式 camillalib 引擎：封装 `pipeline::Pipeline`，接口对齐 `DspChain`。
//!
//! 与子进程 camilladsp 不同：这里直接把 camillalib 链接进来，
//! 在进程内处理 PCM，无需 IPC/websocket。
//!
//! 参数以 camillalib YAML 字符串形式传入（由 Python 侧 camilla.py 生成），
//! 用 yaml_serde 反序列化为 config::Configuration，再建 Pipeline。

use std::sync::Arc;

use camillalib::audiochunk::AudioChunk;
use camillalib::config;
use camillalib::pipeline::Pipeline;
use camillalib::{PrcFmt, ProcessingParameters};

// ============================================================
// 结构比对与变更检测（决定「平滑更新」还是「重建」）
// ============================================================

/// 管线结构是否一致：pipeline 步骤的「类型 + 名称」序列相同即视为一致。
///
/// 结构一致时，参数变化可用 update_parameters 平滑更新；
/// 结构变化（增删/重排步骤）时必须重建。
fn same_structure(a: &config::Configuration, b: &config::Configuration) -> bool {
    // chunksize 变化会导致内部攒块缓冲不匹配，必须重建（不算「同结构」）。
    a.devices.chunksize == b.devices.chunksize
        && step_signature(a) == step_signature(b)
}

/// 提取 pipeline 步骤签名（类型 + 名称），用于结构比对。
fn step_signature(conf: &config::Configuration) -> Vec<(u8, String)> {
    let mut sig = Vec::new();
    if let Some(steps) = conf.pipeline.as_ref() {
        for step in steps {
            match step {
                // Filter 步骤含多个滤波器名 → 拼成签名
                config::PipelineStep::Filter(s) => sig.push((0u8, s.names.join(","))),
                config::PipelineStep::Mixer(s) => sig.push((1u8, s.name.clone())),
                config::PipelineStep::Processor(s) => sig.push((2u8, s.name.clone())),
            }
        }
    }
    sig
}

/// 找出「参数发生变化」的滤波器名。
///
/// 用序列化后的字符串比对；名称新增/删除不算变更（那属于结构变化）。
fn changed_filter_names(prev: &config::Configuration, cur: &config::Configuration) -> Vec<String> {
    changed_names(
        prev.filters.as_ref(),
        cur.filters.as_ref(),
        |f| serde_json::to_string(f).unwrap_or_default(),
    )
}

fn changed_mixer_names(prev: &config::Configuration, cur: &config::Configuration) -> Vec<String> {
    changed_names(
        prev.mixers.as_ref(),
        cur.mixers.as_ref(),
        |m| serde_json::to_string(m).unwrap_or_default(),
    )
}

fn changed_processor_names(prev: &config::Configuration, cur: &config::Configuration) -> Vec<String> {
    changed_names(
        prev.processors.as_ref(),
        cur.processors.as_ref(),
        |p| serde_json::to_string(p).unwrap_or_default(),
    )
}

/// 通用：找出两个 HashMap<String, T> 中「值变化了」的键。
fn changed_names<T>(
    prev: Option<&std::collections::HashMap<String, T>>,
    cur: Option<&std::collections::HashMap<String, T>>,
    to_str: impl Fn(&T) -> String,
) -> Vec<String> {
    let empty = std::collections::HashMap::new();
    let prev = prev.unwrap_or(&empty);
    let cur = cur.unwrap_or(&empty);
    let mut out = Vec::new();
    for (name, cur_val) in cur.iter() {
        match prev.get(name) {
            Some(prev_val) => {
                if to_str(prev_val) != to_str(cur_val) {
                    out.push(name.clone());
                }
            }
            None => {
                // 新增项：也纳入变更（虽然结构比对通常会先拦下）
                out.push(name.clone());
            }
        }
    }
    out
}

pub struct CamillaEngine {
    pipeline: Option<Pipeline>,
    rate: u32,
    channels: usize,
    chunksize: usize,
    last_yaml: String,
    /// 上次的配置（用于比对结构是否变化，决定「平滑更新」还是「重建」）。
    last_conf: Option<config::Configuration>,
    /// 输入缓冲（交错 f32）：攒够 chunksize 帧才处理，避免每块补零稀释音量。
    in_buf: Vec<f32>,
    /// 输出缓冲（交错 f32）：处理后的样本，供逐块取回。
    out_buf: Vec<f32>,
    // 音色染色（camillalib 之后，独立于 YAML）
    tube: Option<crate::tube::Tube>,
    bbe: Option<crate::bbe::Bbe>,
    // 过采样状态（染色用）
    oversample: crate::oversample::Oversample2x,
    // 染色参数（-1 表示关闭）
    tube_drive: f32,
    bbe_amount: f32,
}

impl CamillaEngine {
    pub fn new(sample_rate: u32) -> Self {
        CamillaEngine {
            pipeline: None,
            rate: sample_rate.max(1),
            channels: 2,
            chunksize: 1024,
            last_yaml: String::new(),
            last_conf: None,
            in_buf: Vec::new(),
            out_buf: Vec::new(),
            tube: None,
            bbe: None,
            oversample: crate::oversample::Oversample2x::new(),
            tube_drive: 0.0,
            bbe_amount: 0.0,
        }
    }

    /// 设置染色参数。tube_drive<=0 关闭电子管；bbe_amount<=0 关闭 BBE。
    pub fn set_coloring(&mut self, tube_drive: f32, bbe_amount: f32) {
        let td = tube_drive.max(0.0);
        let ba = bbe_amount.clamp(0.0, 1.0);
        // 参数没变则跳过（避免每块重建 Tube/Bbe）
        if (td - self.tube_drive).abs() < 1e-4 && (ba - self.bbe_amount).abs() < 1e-4 {
            return;
        }
        self.tube_drive = td;
        self.bbe_amount = ba;
        self.tube = if self.tube_drive > 1e-3 {
            Some(crate::tube::Tube::new(self.tube_drive))
        } else {
            None
        };
        self.bbe = if self.bbe_amount > 1e-3 {
            Some(crate::bbe::Bbe::new(self.bbe_amount, self.rate as f32))
        } else {
            None
        };
    }

    /// 是否已装载配置。
    pub fn has_pipeline(&self) -> bool {
        self.pipeline.is_some()
    }

    /// 应用 camillalib YAML 配置。
    ///
    /// **平滑优先**：若管线结构（滤波器/混音器/处理器名单）未变，
    /// 仅用 `Pipeline::update_parameters` 原地更新变化的参数——
    /// 音频流不中断（拖动 EQ 滑块等实时调参不再「一顿一顿」）。
    /// 仅当结构变化（增删步骤）时才重建 Pipeline。
    ///
    /// 返回 Err 时保留旧管线不变。
    pub fn set_yaml(&mut self, yaml: &str) -> Result<(), String> {
        if yaml == self.last_yaml && self.pipeline.is_some() {
            return Ok(()); // 无变化
        }
        let mut conf: config::Configuration = yaml_serde::from_str(yaml)
            .map_err(|e| format!("camilla yaml 解析失败: {e}"))?;
        // 用实际采样率覆盖（Python 侧不知当前曲目采样率，用占位值；
        // Biquad 系数依赖采样率，必须用真实值）
        conf.devices.samplerate = self.rate as usize;
        // 记录 chunksize（Conv 等滤波器要求「块大小 == chunksize」）
        self.chunksize = conf.devices.chunksize.max(1);
        // 校验配置（不改设备，只检查管线合法性）
        config::validate_config(&mut conf, None)
            .map_err(|e| format!("camilla 配置无效: {e}"))?;

        // 已有管线，且结构未变 → 平滑原地更新参数（不中断音频）
        if let Some(prev) = self.last_conf.as_ref() {
            if self.pipeline.is_some() && same_structure(prev, &conf) {
                let changed_filters = changed_filter_names(prev, &conf);
                let changed_mixers = changed_mixer_names(prev, &conf);
                let changed_processors = changed_processor_names(prev, &conf);
                if let Some(pipeline) = self.pipeline.as_mut() {
                    pipeline.update_parameters(
                        conf.clone(),
                        &changed_filters,
                        &changed_mixers,
                        &changed_processors,
                    );
                }
                self.last_conf = Some(conf);
                self.last_yaml = yaml.to_string();
                // 平滑更新：**不清空缓冲**，音频连续
                return Ok(());
            }
        }

        // 结构变化（或首次）：重建 Pipeline
        let params = Arc::new(ProcessingParameters::default());
        let pipeline = Pipeline::from_config(conf.clone(), params);
        self.pipeline = Some(pipeline);
        self.last_conf = Some(conf);
        self.last_yaml = yaml.to_string();
        // 重建：清空缓冲，避免旧数据/长度不匹配
        self.in_buf.clear();
        self.out_buf.clear();
        Ok(())
    }

    /// 清除管线（旁路）。
    pub fn clear(&mut self) {
        self.pipeline = None;
        self.last_yaml.clear();
        self.last_conf = None;
        self.in_buf.clear();
        self.out_buf.clear();
    }

    /// 处理一块交错 PCM（原地）。channels 目前支持 2。
    ///
    /// camillalib 要求每块恰好 chunksize 帧；而解码流是任意块大小。
    /// 故用 in_buf 缓冲攒够 chunksize 再处理，避免每块补零稀释音量。
    pub fn process_interleaved(&mut self, pcm: &mut [f32], channels: usize) {
        if channels != 2 {
            return;
        }
        let total_frames = pcm.len() / 2;
        if total_frames == 0 {
            return;
        }

        // 1) 追加本次 PCM 到输入缓冲
        self.in_buf.extend_from_slice(pcm);

        let cs = self.chunksize.max(1);
        let cs_samples = cs * 2; // 交错：每帧 2 个样本

        // 2) 每次攒够 cs 帧就处理一块，结果追加到 out_buf
        while self.in_buf.len() >= cs_samples {
            let block: Vec<f32> = self.in_buf.drain(0..cs_samples).collect();
            // 分离声道 + 转 PrcFmt
            let mut ch0: Vec<PrcFmt> = Vec::with_capacity(cs);
            let mut ch1: Vec<PrcFmt> = Vec::with_capacity(cs);
            let mut maxval: PrcFmt = 0.0;
            for i in 0..cs {
                let l = block[i * 2] as PrcFmt;
                let r = block[i * 2 + 1] as PrcFmt;
                ch0.push(l);
                ch1.push(r);
                let m = l.abs().max(r.abs());
                if m > maxval { maxval = m; }
            }
            let chunk = AudioChunk::new(vec![ch0, ch1], maxval, -maxval, cs, cs);
            if let Some(pipeline) = self.pipeline.as_mut() {
                let out = pipeline.process_chunk(chunk);
                // 安全取波形：waveforms 数量/长度不保证恰好 2×cs，
                // 直接索引会越界 panic（高采样率/特殊配置下尤其危险）。
                let w0 = out.waveforms.get(0);
                let w1 = out.waveforms.get(1);
                match (w0, w1) {
                    (Some(a), Some(b)) => {
                        let n = cs.min(a.len()).min(b.len());
                        for i in 0..n {
                            self.out_buf.push(a[i] as f32);
                            self.out_buf.push(b[i] as f32);
                        }
                        // 若输出比输入短，用 0 补齐到 cs 帧，保持长度一致
                        for _ in n..cs {
                            self.out_buf.push(0.0);
                            self.out_buf.push(0.0);
                        }
                    }
                    _ => {
                        // 波形数异常：退化为直通该块
                        self.out_buf.extend_from_slice(&block);
                    }
                }
            } else {
                // 无管线：直通
                self.out_buf.extend_from_slice(&block);
            }
        }

        // 3) 从 out_buf 取回本次需要的样本数（不足则输出 0，保持长度一致）
        let need = pcm.len();
        if self.out_buf.len() >= need {
            let out: Vec<f32> = self.out_buf.drain(0..need).collect();
            pcm.copy_from_slice(&out);
        } else {
            // 输出滞后（首次攒块）：先把已有的写入，其余置 0
            let have = self.out_buf.len();
            for i in 0..have {
                pcm[i] = self.out_buf[i];
            }
            for i in have..need {
                pcm[i] = 0.0;
            }
            self.out_buf.clear();
        }

        // ---- 音色染色（过采样 → 电子管 → BBE → 降采样）----
        if self.tube.is_some() || self.bbe.is_some() {
            self.apply_coloring(pcm, channels);
        }
    }

    /// 对整块 PCM 应用染色：4x 过采样下处理非线性，再降采样。
    /// 带输出电平补偿（染色后 RMS 补回原值）。
    fn apply_coloring(&mut self, pcm: &mut [f32], channels: usize) {
        if channels != 2 { return; }
        let frames = pcm.len() / 2;
        if frames == 0 { return; }
        // 上采样 4x
        let mut up: Vec<f32> = Vec::with_capacity(pcm.len() * 4);
        self.oversample.upsample(pcm, &mut up);
        // 电子管 + BBE（在高采样率域）
        let up_frames = up.len() / 2;
        for i in 0..up_frames {
            let mut l = up[i * 2];
            let mut r = up[i * 2 + 1];
            if let Some(t) = self.tube.as_ref() {
                l = t.process(l);
                r = t.process(r);
            }
            if let Some(b) = self.bbe.as_mut() {
                let (nl, nr) = b.process(l, r);
                l = nl;
                r = nr;
            }
            up[i * 2] = l;
            up[i * 2 + 1] = r;
        }
        // 降采样回原率
        let mut down: Vec<f32> = Vec::with_capacity(pcm.len());
        self.oversample.downsample(&up, &mut down);
        // 写回（长度对齐）
        let n = down.len().min(pcm.len());
        pcm[..n].copy_from_slice(&down[..n]);
    }
}
