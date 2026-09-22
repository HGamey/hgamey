use crate::TrackMode;
use anyhow::{Context, Result};
use std::fs;
use std::path::Path;

pub fn load_template(track: TrackMode) -> Result<String> {
    let rel_path = match track {
        TrackMode::Sr => "templates/sr/template.comp",
        TrackMode::FrameGen => "templates/frame_gen/template.comp",
    };
    
    if Path::new(rel_path).exists() {
        fs::read_to_string(rel_path).with_context(|| format!("读取模板失败: {}", rel_path))
    } else {
        Ok(match track {
            TrackMode::Sr => include_str!("../../templates/sr/template.comp").to_string(),
            TrackMode::FrameGen => include_str!("../../templates/frame_gen/template.comp").to_string(),
        })
    }
}
