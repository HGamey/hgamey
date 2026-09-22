use crate::TrackMode;
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candidate {
    pub id: String,
    pub generation: u32,
    pub track: String,
    pub parent_id: Option<String>,
    pub budget_ms: f32,
    pub shader_source: String,
}

pub struct Validator;

impl Validator {
    pub fn new() -> Self {
        Self
    }

    /// 语法与防爆静态检查
    pub fn verify_syntax(&self, source: &str) -> Result<()> {
        if !source.contains("#version 450") {
            bail!("Shader 缺少必须的 #version 450 标头");
        }
        if !source.contains("void main()") {
            bail!("Shader 缺少 void main() 入口函数");
        }
        if source.contains("while(true)") || source.contains("for(;;)") {
            bail!("检测到无界死循环危险指令，触发沙盒直杀");
        }
        Ok(())
    }
}

pub fn mock_generate_candidates(track: TrackMode, template: &str, n: u32) -> Result<Vec<Candidate>> {
    let mut out = Vec::new();
    for i in 0..n {
        let source = if i == 0 {
            template.replace("// [LLM 变异插槽: 高频重构与边缘自适应锐化]", "color = clamp(color * 1.05, 0.0, 1.0);")
        } else if i == 1 {
            template.replace("void main()", "void broken_func()")
        } else {
            template.to_string()
        };

        out.push(Candidate {
            id: format!("gen0_cand{}", i + 1),
            generation: 0,
            track: match track {
                TrackMode::Sr => "sr".to_string(),
                TrackMode::FrameGen => "frame_gen".to_string(),
            },
            parent_id: None,
            budget_ms: 1.5,
            shader_source: source,
        });
    }
    Ok(out)
}

pub fn run_evolution_loop(
    _track: TrackMode,
    _generations: u32,
    _children: u32,
    _budget: f32,
    _out: &Path,
) -> Result<()> {
    println!("进化循环运行中 (待与真实 phonefarm 物理机联调)...");
    Ok(())
}
