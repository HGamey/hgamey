---
game: com.miHoYo.Yuanshen
device: 91253241019A
objective:
  metric: power_watt
  direction: lower
  target_pct: 5
constraints:
  - fps_mean: no_worse
  - frame_p95: no_worse
  - image: psnr_min_db 30
layers: [sysparam, gray]
per_generation: 2
forbid: []
budget:
  wall_minutes: 180
  max_generations: 6
  min_generations: 3
stop_when:
  - target_reached
  - budget_exhausted
  - no_gain: 2
proposer: llm
gate: shadow
---
在须弥城固定路线跑图（原神自带 60 帧档），想让整机功耗降 5%。
帧率、帧时抖动不能显著变差，画面和原来比 PSNR 不低于 30 dB。
偏好：先试系统参数（不改画面），再试去掉画面里多余的步骤。
