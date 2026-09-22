# game_opt_loop

移动游戏自主优化与画质跃升闭环 (Rust 原生)

> **让游戏的性能与画面表现，如同魔法一般跃升。**
> 本仓库是面向移动端游戏 (Android / OpenHarmony) 的自演化优化闭环。由大模型在真机硬件约束下变异生成极致轻量的图形算子 (Compute Shader)，通过显存零拷贝挂载与 phonefarm 自动化物理 Harness 驱动，实现无源码侵入的帧率稳定性提升、功耗下降与画质重构。

---

## 📌 快速导航

| 文档 | 是什么 | 什么时候读 |
|---|---|---|
| **[DESIGN.md](DESIGN.md)** | 系统架构设计、两大赛道硬约束、Harness 数据流与验收标准 | **开展具体开发前必读** (Implementation Spec) |
| **[contracts/](contracts/)** | 算子契约、评测请求、判定报告、优胜档案的标准 JSON Schema | **模块间联调与数据契约** |
| **[tracks/](src/tracks/)** | 空间超分辨率 (SR) 与 双倍插帧 (Frame Gen) 赛道定义 | **新增或调试优化算法时** |

---

## 架构分工：与 phonefarm 的边界

- **phonefarm (外部 Rust 物理底座)**：负责所有真机硬件的脏活累活。包括 adb/hdc 真机通信、外挂风扇控温冷机基准、60s 确定性脚本自动化回放、SmartPerf 硬件功耗与帧时采样、冻结规则两样本 t 检验 (p < 0.01) 以及 100% 还原系统初始状态。
- **game_opt_loop (本仓库，纯 Rust 原生)**：负责**算法自主进化与管线编排**。
  1. **本地防爆编译**：零开销编译 GLSL Compute Shader 为 SPIR-V 二进制，语法错与非法内存秒级熔断打回。
  2. **赛道契约抽象**：统一管理超分辨率 (SR) 与双倍插帧 (Frame Gen) 两大赛道的 Shader 模板与 Prompt。
  3. **演化档案维护**：维护 [耗时 ms, 功耗 W, 画质 dB] 的 Pareto 前沿档案，驱动 LLM 算法探索。

---

## 快速开始 (实现就绪状态)

```bash
# 1. 编译验证 (纯 Rust 原生，零 Python 依赖)
cargo build

# 2. 语法校验测试 (冒烟测试本地 Shader 校验器)
cargo test

# 3. 运行演化单代验证 (Dry Run 模式)
cargo run -- --track sr --dry-run
```
