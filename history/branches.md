# 旧仓库未合并的分支

四个旧仓库于 2026-10-28 归档前，只有下面 5 个分支有不在 main 里的提交，已按导入 main 的同一方式改写（作者、提交者、提交说明不变，目录放进组件子目录）。逐个提交的旧→新对照见 `imported-branches.commit-map`。

| 旧分支（作者） | 旧提交 | 本仓库分支 | 状态 |
|---|---|---|---|
| phonefarm `feat/selfsr`（BH3GEI，64 个提交） | b70637f | `phonefarm/feat/selfsr` | 已合上 main，可直接接着开发 |
| phonefarm `docs/stable-cd`（BH3GEI） | 2057d6f | `phonefarm/docs/stable-cd` | 已合上 main |
| game_opt_loop `fix/caps-battery-dumpsys-fallback`（BH3GEI） | 9a50c14 | `game_opt_loop/fix/caps-battery-dumpsys-fallback` | 已合上 main |
| refbench `feat/fps-cap`（BH3GEI） | 16f3c81 | `archive/refbench/feat/fps-cap` | 与 main 在 `refbench/app/src/main.cpp` 冲突，未合并；需作者 rebase |
| harness-ops `verify/job-inputs-20261025` | bf2bd1b | `archive/harness-ops/verify/job-inputs-20261025` | 早期版本，main 之后已用另一实现完成同一功能，未合并 |

`archive/` 分支只保留历史，检出后只有对应组件目录。
