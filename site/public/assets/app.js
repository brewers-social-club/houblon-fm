const audio = document.querySelector('#audio');
const play = document.querySelector('#play');
const now = document.querySelector('#now');

async function refresh() {
  try {
    const response = await fetch('/api/now');
    const data = await response.json();
    now.textContent = data ? `${data.title} · ${data.format}` : 'Playlist de secours';
  } catch {
    now.textContent = 'Direct';
  }
}

async function startLive() {
  play.disabled = true;
  now.textContent = 'Connexion au direct…';
  audio.src = `/live.mp3?attempt=${Date.now()}`;
  audio.load();
  try {
    await audio.play();
    play.textContent = 'Ⅱ';
    await refresh();
  } catch {
    audio.removeAttribute('src');
    audio.load();
    play.textContent = '▶';
    now.textContent = 'Antenne en cours de démarrage — réessayer';
  } finally {
    play.disabled = false;
  }
}

play?.addEventListener('click', async () => {
  if (audio.paused) {
    await startLive();
  } else {
    audio.pause();
    play.textContent = '▶';
  }
});

refresh();
setInterval(refresh, 15000);
