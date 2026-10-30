/* One public-job status interpretation for home, research and agent views. */
(() => {
  'use strict';
  const NOT_RUNNING = new Set(['preparing', 'queued', 'blocked', 'interrupted', 'finalizing']);
  const LABELS = {queued: '排队', preparing: '准备中', blocked: '待核查', interrupted: '已中断',
                  finalizing: '收尾中', lost: '丢失'};
  const OUTCOMES = {succeeded: '成功', failed: '失败', cancelled: '已取消', interrupted: '已中断', unknown: '结果未知'};
  const active = job => job.state === 'running' && !NOT_RUNNING.has(job.phase) && job.queue?.state !== 'blocked' && !job.lost;
  // queue.state decides what a job holds; a storage error is reported beside it, never as a running state.
  function label(job) {
    let base = runLabel(job);
    if (job.stop_requested && job.state !== 'finished') base += ' · 停止待确认';
    return job.storage_error ? `${base} · 保存失败` : base;
  }
  function runLabel(job) {
    // Sent to another computer that has not confirmed taking it: neither queued nor running.
    if (job.phase === 'submission_unknown' || job.state === 'unknown') return '等待那台电脑受理';
    if (job.queue?.state === 'blocked') return LABELS.blocked;
    if (job.state === 'finished') return OUTCOMES[job.outcome] || job.outcome || '已结束';
    if (job.state !== 'running') return LABELS[job.state] || job.state || '未知';
    return LABELS[job.phase] || '运行中';
  }
  // Tag tone: acc = working now, ok/bad = finished well/badly, warn = needs a look, off = waiting.
  function tone(job) {
    if (job.lost) return 'bad';
    if (job.queue?.state === 'blocked' || job.phase === 'blocked' || job.phase === 'submission_unknown' || job.state === 'unknown' || job.storage_error) return 'warn';
    if (job.stop_requested && job.state !== 'finished') return 'warn';
    if (job.state === 'finished') return {succeeded: 'ok', failed: 'bad', interrupted: 'warn', unknown: 'warn'}[job.outcome] || 'off';
    return active(job) ? 'acc' : 'off';
  }
  window.HGJobState = {active, label, tone};
})();
