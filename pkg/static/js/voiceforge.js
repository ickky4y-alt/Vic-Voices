const form = document.querySelector('#tts-form');
const text = document.querySelector('#text');
const voice = document.querySelector('#voice');
const style = document.querySelector('#style');
const styleDescription = document.querySelector('#style-description');
const speed = document.querySelector('#speed');
const speedValue = document.querySelector('#speed-value');
const playerSpeed = document.querySelector('#player-speed');
const counter = document.querySelector('#counter');
const button = document.querySelector('#generate');
const message = document.querySelector('#message');
const result = document.querySelector('#result');
const player = document.querySelector('#player');
const download = document.querySelector('#download');
if (form) {
const processingStatus = document.createElement('div');
processingStatus.className = 'processing-status';
processingStatus.innerHTML = '<strong>Processing your audio…</strong><div class="progress-bar"><span></span></div>';
form.appendChild(processingStatus);

text.addEventListener('input', () => { counter.textContent = `${text.value.length} / 2000`; });
speed.addEventListener('input', () => { speedValue.value = `${Number(speed.value).toFixed(2)}x`; });
playerSpeed.addEventListener('change', () => { player.playbackRate = Number(playerSpeed.value); });

async function loadVoices() {
  try {
    const response = await fetch('/api/voices');
    const data = await response.json();
    voice.innerHTML = '<option value="">Choose a voice...</option>';
    for (const gender of ['female', 'male']) {
      const group = document.createElement('optgroup');
      group.label = `${gender[0].toUpperCase()}${gender.slice(1)} voices`;
      data.voices.filter(item => item.gender === gender).forEach(item => {
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = item.name;
        group.append(option);
      });
      voice.append(group);
    }
    style.innerHTML = '<option value="">Choose a style...</option>';
    data.styles.forEach(item => {
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = item.name;
      style.append(option);
    });
    style.value = 'neutral';
    styleDescription.textContent = data.styles[0].description;
    style.addEventListener('change', () => { styleDescription.textContent = data.styles.find(item => item.id === style.value)?.description || ''; });
  } catch (error) {
    voice.innerHTML = '<option value="">Voices unavailable</option>';
    message.textContent = 'Could not load the preset voices.';
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  button.disabled = true;
  result.hidden = true;
  processingStatus.classList.add('is-active');
  message.textContent = 'Loading the speech model. The first run may take a minute.';
  try {
    const response = await fetch('/api/tts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: text.value, voice: voice.value, style: style.value, speed: Number(speed.value) }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Speech generation failed.');
    player.src = data.audio_url;
    player.playbackRate = Number(playerSpeed.value);
    download.href = data.audio_url;
    result.hidden = false;
    processingStatus.classList.remove('is-active');
    message.textContent = 'Your speech is ready.';
  } catch (error) {
    processingStatus.classList.remove('is-active');
    message.textContent = error.message;
    if (error.message && error.message.includes('free trial') || error.message.includes('log in')) {
      message.textContent = `${error.message} Please log in or create an account to continue.`;
    }
  } finally {
    button.disabled = false;
  }
});

loadVoices();
}