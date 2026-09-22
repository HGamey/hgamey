use anyhow::Result;
use std::process::Command;

pub struct PhonefarmDriver {
    phonefarm_bin: String,
}

impl PhonefarmDriver {
    pub fn new() -> Self {
        Self {
            phonefarm_bin: "../phonefarm/phonefarm".to_string(),
        }
    }

    pub fn probe(&self) -> Result<String> {
        let output = Command::new("which")
            .arg(&self.phonefarm_bin)
            .output();

        match output {
            Ok(out) if out.status.success() => {
                Ok("phonefarm CLI 可用 (已连接物理底座)".to_string())
            }
            _ => {
                Ok("phonefarm 就绪模式 (待物理机型接入)".to_string())
            }
        }
    }
}
