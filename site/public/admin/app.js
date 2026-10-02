const loginPanel = document.querySelector('#login-panel');
const dashboardPanel = document.querySelector('#dashboard-panel');
const loginForm = document.querySelector('#login-form');
const loginStatus = document.querySelector('#login-status');
const logoutButton = document.querySelector('#logout');
const dashboard = document.querySelector('#dashboard');

function showLogin(message = '') {
  loginPanel.hidden = false;
  dashboardPanel.hidden = true;
  logoutButton.hidden = true;
  loginStatus.textContent = message;
}

function showDashboard() {
  loginPanel.hidden = true;
  dashboardPanel.hidden = false;
  logoutButton.hidden = false;
}

async function loadDashboard() {
  const response = await fetch('/api/admin/dashboard', { credentials: 'same-origin' });
  if (response.status === 401) {
    showLogin();
    return;
  }
  if (!response.ok) throw new Error('Impossible de charger le studio');
  const data = await response.json();
  dashboard.innerHTML = `
    <article class="card"><span>Coûts</span><h3>${Number(data.cost_eur).toFixed(2)} €</h3></article>
    <article class="card"><span>Incidents</span><h3>${data.incidents.length}</h3></article>
    <article class="card"><span>Tâches</span><h3>${data.tasks.length}</h3></article>
    <article class="card"><span>Avis</span><h3>${data.feedback.length}</h3></article>`;
  showDashboard();
}

loginForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = loginForm.querySelector('button');
  button.disabled = true;
  loginStatus.textContent = 'Connexion…';
  try {
    const response = await fetch('/api/admin/login', {
      method: 'POST',
      body: new FormData(loginForm),
      credentials: 'same-origin',
    });
    if (!response.ok) {
      showLogin(response.status === 401 ? 'Identifiants invalides.' : 'Connexion impossible.');
      return;
    }
    loginForm.reset();
    await loadDashboard();
  } catch {
    showLogin('Connexion impossible.');
  } finally {
    button.disabled = false;
  }
});

logoutButton.addEventListener('click', async () => {
  await fetch('/api/admin/logout', { method: 'POST', credentials: 'same-origin' });
  showLogin('Session fermée.');
});

loadDashboard().catch(() => showLogin('Studio temporairement indisponible.'));
