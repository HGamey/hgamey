# AGENTS.md

## 代码仓库只放代码

- 不要在本仓库新增或修改说明文档、图片、PPT、PDF、实验报告。
- 这次改了什么、为什么改：写在 commit message 正文。
- 一个功能或修复的完整说明、验证方法、实验数字、截图：写在 PR 描述。
- 长期文档（设计、规格、架构图、进展汇报）：提交到 https://github.com/HGamey/docs ，本仓库的旧文档在那里的 `repos/<仓库名>/` 下。
- 允许的例外：根目录 README.md（40 行以内）、AGENTS.md、skills/、许可证、程序或测试要读的数据。CI 检查 `code-only` 会拦下其他文档类文件。
