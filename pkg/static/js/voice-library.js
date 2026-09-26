document.querySelectorAll('.preview-button').forEach(button => {
  button.addEventListener('click', async () => {
    const card = button.closest('.voice-card');
    const player = card.querySelector('.preview-player');
    button.disabled = true;
    const originalLabel = button.textContent;
    button.textContent = 'Preparing free preview…';
    try {
      const response = await fetch('/api/tts/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ voice: button.dataset.voice, style: button.dataset.style }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Preview failed.');
      player.src = data.audio_url;
      player.hidden = false;
      await player.play();
    } catch (error) {
      button.setAttribute('aria-label', `${originalLabel}: ${error.message}`);
      button.textContent = originalLabel;
    } finally {
      button.disabled = false;
      if (button.textContent === 'Preparing free preview…') button.textContent = originalLabel;
    }
  });
});