//! 外部依赖查找（服务打包与跨系统）。
//!
//! 后端依赖若干外部程序：ffmpeg / ffprobe（冷门格式解码）、pw-cat（PipeWire 输出）。
//! 不同打包形态下，这些程序的位置不同：
//!   - deb 包：由系统包管理器安装，位于 PATH；
//!   - AppImage / 绿色包：随包附带，位于应用可执行文件同目录或其 bin/ 下。
//!
//! 本模块统一查找顺序（优先级从高到低）：
//!   1. 环境变量覆盖（XIATIAO_<NAME>）—— 调试 / 特殊场景；
//!   2. 应用可执行文件同目录、同目录/bin/、同目录/../lib/ —— 打包进包内；
//!   3. 系统 PATH —— deb / 系统已装。
//!
//! 查找结果缓存，避免每次调用都扫盘。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

/// 查找结果缓存：程序名 -> 绝对路径（None 表示未找到，也缓存避免重复扫盘）。
fn cache() -> &'static Mutex<HashMap<String, Option<PathBuf>>> {
    static CACHE: OnceLock<Mutex<HashMap<String, Option<PathBuf>>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

/// 应用可执行文件所在目录（用于打包场景的包内查找）。
fn exe_dir() -> Option<PathBuf> {
    std::env::current_exe().ok().and_then(|p| p.parent().map(|d| d.to_path_buf()))
}

/// 在指定目录下找可执行文件 `name`（POSIX：有执行权限）。
fn find_in_dir(dir: &std::path::Path, name: &str) -> Option<PathBuf> {
    let candidate = dir.join(name);
    if is_executable(&candidate) {
        return Some(candidate);
    }
    None
}

fn is_executable(path: &std::path::Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = std::fs::metadata(path) {
            return meta.is_file() && (meta.permissions().mode() & 0o111 != 0);
        }
        false
    }
    #[cfg(not(unix))]
    {
        path.is_file()
    }
}

/// 在 PATH 中查找可执行文件。
fn find_in_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        if let Some(p) = find_in_dir(&dir, name) {
            return Some(p);
        }
    }
    None
}

/// 查找外部可执行文件，返回绝对路径（找不到返回 None）。
///
/// 顺序：环境变量覆盖 → 包内目录 → PATH。结果缓存。
pub(crate) fn find_executable(name: &str) -> Option<PathBuf> {
    // 1) 缓存命中
    if let Ok(guard) = cache().lock() {
        if let Some(hit) = guard.get(name) {
            return hit.clone();
        }
    }

    let found = find_executable_uncached(name);

    // 写缓存
    if let Ok(mut guard) = cache().lock() {
        guard.insert(name.to_string(), found.clone());
    }
    found
}

fn find_executable_uncached(name: &str) -> Option<PathBuf> {
    // 1) 环境变量覆盖：XIATIAO_FFMPEG / XIATIAO_FFPROBE / XIATIAO_PWCAT
    let env_key = format!("XIATIAO_{}", name.to_ascii_uppercase().replace('-', "_"));
    if let Ok(v) = std::env::var(&env_key) {
        if !v.is_empty() {
            let p = PathBuf::from(&v);
            if is_executable(&p) {
                return Some(p);
            }
        }
    }

    // 2) 包内目录（AppImage / 绿色包）：可执行文件同目录、bin/、../lib/、../bin/
    if let Some(dir) = exe_dir() {
        let candidates = [
            dir.clone(),
            dir.join("bin"),
            dir.join("../bin"),
            dir.join("../lib"),
        ];
        for d in candidates.iter() {
            if let Some(p) = find_in_dir(d, name) {
                return Some(p);
            }
        }
    }

    // 3) 系统 PATH
    find_in_path(name)
}

/// 判断某外部程序是否可用（用于优雅降级决策）。
pub(crate) fn has_executable(name: &str) -> bool {
    find_executable(name).is_some()
}

/// 构造一个指向外部程序的 Command（已解析到实际路径；找不到回退原名走 PATH）。
///
/// 返回 Command 而非直接执行，便于调用方继续加参数。
pub(crate) fn command(name: &str) -> std::process::Command {
    match find_executable(name) {
        Some(path) => std::process::Command::new(path),
        None => std::process::Command::new(name),
    }
}

/// 后端依赖的外部程序清单。
pub(crate) const REQUIRED_DEPS: &[&str] = &["ffmpeg", "ffprobe", "pw-cat"];

/// 探测所有外部依赖，打印状态（启动时调用，便于打包后诊断）。
///
/// 输出形如：
///   [deps] ffmpeg      : /usr/bin/ffmpeg
///   [deps] ffprobe     : /usr/bin/ffprobe
///   [deps] pw-cat      : (未找到，将走原生 PipeWire 输出)
///
/// 说明：ffmpeg/ffprobe 仅冷门格式（APE/DSF 等）需要；pw-cat 仅是
/// PipeWire 输出的一种方式（可用原生 PipeWire 代替）。缺失不致命。
pub(crate) fn probe_all() {
    for name in REQUIRED_DEPS {
        match find_executable(name) {
            Some(path) => eprintln!("[deps] {name:<10}: {}", path.display()),
            None => {
                let hint = match *name {
                    "ffmpeg" | "ffprobe" => "（冷门格式解码将不可用）",
                    "pw-cat" => "（将回退原生 PipeWire 输出）",
                    _ => "",
                };
                eprintln!("[deps] {name:<10}: 未找到 {hint}");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// 在临时目录造一个可执行文件，验证 find_in_dir 能识别。
    #[test]
    fn find_in_dir_detects_executable() {
        let dir = std::env::temp_dir().join("xiatiao_deps_test");
        let _ = fs::create_dir_all(&dir);
        let bin = dir.join("fake_tool_xyz");
        fs::write(&bin, b"#!/bin/sh\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perm = fs::metadata(&bin).unwrap().permissions();
            perm.set_mode(0o755);
            fs::set_permissions(&bin, perm).unwrap();
        }
        assert!(find_in_dir(&dir, "fake_tool_xyz").is_some());
        let _ = fs::remove_file(&bin);
    }

    /// PATH 查找：系统必有 sh。
    #[test]
    fn find_in_path_finds_sh() {
        assert!(find_in_path("sh").is_some());
    }

    /// 不存在的程序返回 None。
    #[test]
    fn find_missing_returns_none() {
        assert!(find_executable("definitely_not_a_real_program_zzz").is_none());
    }

    /// 缓存一致性：同一程序两次查找结果相同。
    #[test]
    fn cache_consistent() {
        let a = find_executable("sh");
        let b = find_executable("sh");
        assert_eq!(a.is_some(), b.is_some());
    }
}
