const audio = document.querySelector('#audio');
const play = document.querySelector('#play');
const now = document.querySelector('#now');
const isHomepage = ['/', '/index.html'].includes(window.location.pathname);

let starting = false;
let gestureArmed = false;
let gestureResume = null;

async function refresh() {
  if (gestureArmed) {
    now.textContent = 'Touchez la page pour activer le direct';
    return;
  }
  try {
    const response = await fetch('/api/now');
    const data = await response.json();
    now.textContent = data ? `${data.title} · ${data.format}` : 'Playlist de secours';
  } catch {
    now.textContent = 'Direct';
  }
}

function disarmFirstGesture() {
  if (!gestureResume) return;
  document.removeEventListener('pointerdown', gestureResume);
  document.removeEventListener('keydown', gestureResume);
  gestureResume = null;
  gestureArmed = false;
}

function armFirstGesture() {
  if (gestureArmed) return;
  gestureArmed = true;

  const resume = (event) => {
    if (event.target?.closest?.('a, button, input, textarea, select, label')) return;
    disarmFirstGesture();
    void startLive();
  };
  gestureResume = resume;

  document.addEventListener('pointerdown', resume);
  document.addEventListener('keydown', resume);
}

async function startLive({ automatic = false } = {}) {
  if (!audio || !play || starting || !audio.paused) return;
  if (!automatic) disarmFirstGesture();
  starting = true;
  play.disabled = true;
  now.textContent = 'Connexion au direct…';
  audio.src = `/live.mp3?attempt=${Date.now()}`;
  audio.load();
  try {
    await audio.play();
    disarmFirstGesture();
    play.textContent = 'Ⅱ';
    play.setAttribute('aria-label', 'Mettre le direct en pause');
    await refresh();
  } catch (error) {
    play.textContent = '▶';
    play.setAttribute('aria-label', 'Écouter le direct');
    if (automatic && error?.name === 'NotAllowedError') {
      now.textContent = 'Touchez la page pour activer le direct';
      armFirstGesture();
    } else {
      audio.removeAttribute('src');
      audio.load();
      now.textContent = 'Antenne en cours de démarrage — réessayer';
    }
  } finally {
    starting = false;
    play.disabled = false;
  }
}

play?.addEventListener('click', async () => {
  if (audio.paused) {
    await startLive();
  } else {
    audio.pause();
    play.textContent = '▶';
    play.setAttribute('aria-label', 'Écouter le direct');
  }
});

async function bootstrap() {
  await refresh();
  if (isHomepage) await startLive({ automatic: true });
}

void bootstrap();
setInterval(refresh, 15000);
