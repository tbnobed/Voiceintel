// VoiceIntel — app.js

document.addEventListener('DOMContentLoaded', () => {
  // Auto-refresh pending voicemails
  const hasPending = document.querySelectorAll('.status-processing, .status-pending').length > 0;
  if (hasPending) {
    setTimeout(() => window.location.reload(), 8000);
  }

  // Animate stat numbers
  document.querySelectorAll('.stat-number').forEach(el => {
    const target = parseInt(el.textContent, 10);
    if (isNaN(target) || target === 0) return;
    let current = 0;
    const step = Math.ceil(target / 30);
    const interval = setInterval(() => {
      current = Math.min(current + step, target);
      el.textContent = current;
      if (current >= target) clearInterval(interval);
    }, 20);
  });
});
