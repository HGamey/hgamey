/* One public-job status interpretation for home, research and agent views. */
(() => {
  'use strict';
  const NOT_RUNNING = new Set(['preparing', 'queued', 'blocked', 'interrupted', 'finalizing', 'storage_blocked']);
  const LABELS = {queued: '排队', preparing: '准备中', blocked: '待核查', interrupted: '已中断',
                  finalizing: '收尾中', storage_blocked: '存储待处理', lost: '丢失'};
  const active = job => job.state === 'running' && !NOT_RUNNING.has(job.phase) && job.queue?.state !== 'blocked' && !job.lost;
  function label(job) {
    if (job.state === 'finished') return job.outcome || '已结束';
    if (job.state !== 'running') return LABELS[job.state] || job.state || '未知';
    if (job.queue?.state === 'blocked') return LABELS.blocked;
    return LABELS[job.phase] || '运行中';
  }
  window.HGJobState = {active, label};
})();
