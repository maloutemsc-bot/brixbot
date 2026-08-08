/* ==========================================================================
   admin.js — Logique du panneau d'administration BrixBot
   ========================================================================== */

(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];

  const state = {
    tab: 'dashboard',
    logsPage: 1,
    aiModels: [],
  };

  /* --------------------------------------------------------------------------
     Utilitaires
     -------------------------------------------------------------------------- */

  // Échappe le HTML pour éviter toute injection XSS dans le panneau
  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function api(url, opts = {}) {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* réponse non JSON */ }
    if (response.status === 401 && window.BRIXBOT_AUTH) {
      // Session expirée ou non authentifiée → retour à la page de connexion
      window.location.href = '/login';
      throw new Error('Authentification requise');
    }
    if (!response.ok) throw new Error(data.error || `Erreur HTTP ${response.status}`);
    return data;
  }

  function toast(message, type = 'success') {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    $('#toasts').appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 320);
    }, 3500);
  }

  function fmtDate(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (isNaN(date)) return iso;
    return date.toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
  }

  const STATUS_LABELS = {
    connected: '✅ Connecté',
    qr: '📱 QR requis',
    connecting: '⏳ Connexion…',
    disconnected: '❌ Déconnecté',
    unknown: '❓ Inconnu',
  };

  function setWsPill(status) {
    const cls = status || 'unknown';
    $$('.ws-pill').forEach((el) => {
      el.className = `ws-pill ${cls}`;
      el.textContent = STATUS_LABELS[cls] || '❓ Inconnu';
    });
  }

  /* --------------------------------------------------------------------------
     Navigation par onglets
     -------------------------------------------------------------------------- */

  const TITLES = {
    dashboard: 'Tableau de bord',
    config: 'Configuration',
    whatsapp: 'WhatsApp',
    ia: 'Intelligence Artificielle',
    logs: 'Logs',
    test: 'Test API',
  };

  function switchTab(tab) {
    state.tab = tab;
    $$('.nav-link').forEach((link) => link.classList.toggle('active', link.dataset.tab === tab));
    $$('.tab').forEach((section) => section.classList.toggle('active', section.id === `tab-${tab}`));
    $('#pageTitle').textContent = TITLES[tab];
    document.body.classList.remove('sidebar-open');

    if (tab === 'dashboard') loadDashboard();
    if (tab === 'config') loadConfig();
    if (tab === 'whatsapp') loadWhatsApp();
    if (tab === 'ia') loadAI();
    if (tab === 'logs') loadLogs();
  }

  /* --------------------------------------------------------------------------
     Tableau de bord
     -------------------------------------------------------------------------- */

  async function loadDashboard() {
    try {
      const data = await api('/api/dashboard');
      const stats = data.stats;

      setWsPill(data.whatsapp?.status);
      $('#dash-whatsapp').textContent = STATUS_LABELS[data.whatsapp?.status] || '❓ Inconnu';
      $('#dash-total').textContent = stats.total;
      $('#dash-success').textContent = stats.success;
      $('#dash-errors').textContent = stats.errors;
      $('#dash-avg').textContent = `${Math.round(stats.avg_response_time * 1000)} ms`;
      $('#dash-quota').textContent = data.quota != null ? data.quota : '—';

      await renderRecentLogs();
    } catch (_) { /* le polling reprendra */ }
  }

  async function renderRecentLogs() {
    const data = await api('/api/logs?per_page=10&page=1');
    const box = $('#recentLogs');

    if (!data.logs.length) {
      box.innerHTML = '<p class="empty">Aucune activité pour le moment.</p>';
      return;
    }

    box.innerHTML = data.logs.map((log) => `
      <div class="log-row">
        <span class="badge ${log.status}">${log.status === 'success' ? '✓' : '✗'}</span>
        <span class="log-command">${esc(log.command)}</span>
        <span class="log-query">${esc(log.query || '—')}</span>
        <span class="log-meta">${fmtDate(log.timestamp)} · ${Math.round(log.response_time * 1000)} ms · ${log.results_count} résultat(s)</span>
      </div>`).join('');
  }

  /* --------------------------------------------------------------------------
     Configuration .search
     -------------------------------------------------------------------------- */

  async function loadConfig() {
    try {
      const config = await api('/api/config');
      $('#cfg-enabled').checked = config.command_enabled;
      $('#cfg-flexible').checked = config.flexible_search;
      $('#cfg-auto').checked = config.auto_response;
      $('#cfg-api').value = config.api_key || '';
      $('#cfg-max').value = config.max_results;
    } catch (err) { toast(err.message, 'error'); }
  }

  async function saveConfig() {
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({
          command_enabled: $('#cfg-enabled').checked,
          api_key: $('#cfg-api').value.trim(),
          max_results: parseInt($('#cfg-max').value, 10) || 10,
          flexible_search: $('#cfg-flexible').checked,
          auto_response: $('#cfg-auto').checked,
        }),
      });
      toast('Configuration enregistrée ✅');
    } catch (err) { toast(err.message, 'error'); }
  }

  /* --------------------------------------------------------------------------
     WhatsApp
     -------------------------------------------------------------------------- */

  async function loadWhatsApp() {
    try {
      const status = await api('/api/whatsapp/status');
      setWsPill(status.status);

      const big = $('#ws-big');
      big.textContent = STATUS_LABELS[status.status] || '❓ Inconnu';
      big.className = `ws-big ${status.status || 'unknown'}`;

      $('#ws-number').textContent = status.number || '—';
      $('#ws-session').textContent = 'auth_info (persistant)';
      $('#ws-updated').textContent = fmtDate(status.updated_at);

      const qrCard = $('#qr-card');
      if (status.status === 'qr' && status.qr) {
        qrCard.style.display = '';
        $('#qr-image').src = status.qr;
      } else {
        qrCard.style.display = 'none';
      }
    } catch (_) { /* le polling reprendra */ }
  }

  async function restartBot() {
    if (!confirm('Redémarrer le bot WhatsApp ? La session sera conservée.')) return;
    try {
      const result = await api('/api/whatsapp/restart', { method: 'POST' });
      toast(result.message || 'Redémarrage demandé', 'success');
      setTimeout(loadWhatsApp, 4000);
    } catch (err) { toast(err.message, 'error'); }
  }

  /* --------------------------------------------------------------------------
     IA (GROQ)
     -------------------------------------------------------------------------- */

  async function loadAI() {
    try {
      if (!state.aiModels.length) {
        state.aiModels = await api('/api/ai/models');
        $('#ai-model').innerHTML = state.aiModels
          .map((m) => `<option value="${esc(m.value)}">${esc(m.label)}</option>`)
          .join('');
      }

      const config = await api('/api/ai/config');
      $('#ai-enabled').checked = config.enabled;
      $('#ai-key').value = config.api_key || '';
      $('#ai-model').value = config.model || (state.aiModels[0]?.value || '');
      $('#ai-prompt').value = config.system_prompt || '';
      $('#ai-temp').value = config.temperature ?? 0.7;
      $('#ai-temp-value').textContent = Number(config.temperature ?? 0.7).toFixed(2);
      $('#ai-tokens').value = config.max_tokens || 1024;
      $('#ai-memory').checked = !!config.memory_enabled;
      $('#ai-memory-count').value = config.memory_exchanges || 5;
      $('#ai-whitelist').value = config.ai_whitelist || '';

      await loadAILogs();
    } catch (err) { toast(err.message, 'error'); }
  }

  async function saveAI() {
    try {
      await api('/api/ai/config', {
        method: 'POST',
        body: JSON.stringify({
          enabled: $('#ai-enabled').checked,
          api_key: $('#ai-key').value.trim(),
          model: $('#ai-model').value,
          system_prompt: $('#ai-prompt').value.trim(),
          temperature: parseFloat($('#ai-temp').value),
          max_tokens: parseInt($('#ai-tokens').value, 10) || 1024,
          memory_enabled: $('#ai-memory').checked,
          memory_exchanges: parseInt($('#ai-memory-count').value, 10) || 5,
          ai_whitelist: $('#ai-whitelist').value,
        }),
      });
      toast('Configuration IA enregistrée ✅');
    } catch (err) { toast(err.message, 'error'); }
  }

  async function loadAILogs() {
    try {
      const logs = await api('/api/ai/logs?limit=15');
      const box = $('#ai-logs');

      if (!logs.length) {
        box.innerHTML = '<p class="empty">Aucune conversation IA pour le moment.</p>';
        return;
      }

      box.innerHTML = logs.map((log) => `
        <div class="ai-log">
          <div class="ai-log-head">
            <strong>${esc(log.sender)}</strong>
            <span>${fmtDate(log.timestamp)}</span>
          </div>
          <div class="ai-log-q">👤 ${esc(log.user_message)}</div>
          <div class="ai-log-a">🤖 ${esc(log.ai_response)}</div>
          <div class="ai-log-meta">Modèle : ${esc(log.model)} · ${log.tokens_used} tokens · ${log.duration_ms} ms</div>
        </div>`).join('');
    } catch (_) { /* silencieux */ }
  }

  async function testAI() {
    const message = $('#ai-test-msg').value.trim();
    if (!message) return toast('Entrez un message de test d\'abord', 'error');

    const btn = $('#ai-test');
    btn.disabled = true;
    btn.textContent = '⏳ Appel à GROQ…';

    try {
      const result = await api('/api/ai/test', {
        method: 'POST',
        body: JSON.stringify({ message }),
      });

      const box = $('#ai-test-result');
      box.style.display = '';
      box.innerHTML = `
        <div class="ai-log-a">🤖 ${esc(result.reply)}</div>
        <div class="ai-log-meta">Modèle : ${esc(result.model)} · ${result.tokens_used} tokens · ${result.duration_ms} ms</div>`;
      toast('Réponse reçue ✅');
    } catch (err) { toast(err.message, 'error'); }
    finally {
      btn.disabled = false;
      btn.textContent = '🧪 Tester';
    }
  }

  /* --------------------------------------------------------------------------
     Logs
     -------------------------------------------------------------------------- */

  async function loadLogs() {
    try {
      const params = new URLSearchParams({ page: state.logsPage, per_page: 15 });
      if ($('#log-status').value) params.set('status', $('#log-status').value);
      if ($('#log-type').value !== '') params.set('is_ai', $('#log-type').value);

      const data = await api(`/api/logs?${params.toString()}`);
      const body = $('#logs-body');

      if (!data.logs.length) {
        body.innerHTML = '<tr><td colspan="9" class="empty">Aucune commande enregistrée.</td></tr>';
      } else {
        body.innerHTML = data.logs.map((log) => `
          <tr>
            <td class="nowrap">${fmtDate(log.timestamp)}</td>
            <td><span class="badge ${log.is_ai ? 'ai' : 'cmd'}">${esc(log.command)}</span></td>
            <td class="trunc">${esc(log.query || '—')}</td>
            <td><span class="badge ${log.status}">${log.status}</span></td>
            <td>${log.results_count}</td>
            <td class="nowrap">${Math.round(log.response_time * 1000)} ms</td>
            <td class="trunc">${esc(log.error || '')}</td>
            <td class="trunc" title="${esc(log.chat || '')}">${esc(log.chat || '—')}</td>
            <td class="trunc" title="${esc(log.sender || '')}">${esc(log.sender || '—')}</td>
          </tr>`).join('');
      }

      $('#logs-info').textContent = `Page ${data.page} / ${data.pages} · ${data.total} entrée(s)`;
      $('#logs-prev').disabled = data.page <= 1;
      $('#logs-next').disabled = data.page >= data.pages;
    } catch (err) { toast(err.message, 'error'); }
  }

  async function clearLogs() {
    if (!confirm('Effacer tous les logs (commandes et IA) ? Cette action est irréversible.')) return;
    try {
      await api('/api/logs/clear', { method: 'POST' });
      toast('Logs effacés 🗑');
      state.logsPage = 1;
      loadLogs();
    } catch (err) { toast(err.message, 'error'); }
  }

  /* --------------------------------------------------------------------------
     Diagnostic
     -------------------------------------------------------------------------- */

  async function openDiagnostic() {
    const modal = $('#diagModal');
    const content = $('#diagContent');
    modal.style.display = '';
    content.textContent = 'Génération du diagnostic…';
    try {
      const data = await api('/api/debug/dump');
      content.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
      content.textContent = `Erreur : ${err.message}`;
    }
  }

  function closeDiagnostic() {
    $('#diagModal').style.display = 'none';
  }

  async function copyDiagnostic() {
    try {
      await navigator.clipboard.writeText($('#diagContent').textContent);
      toast('Diagnostic copié 📋');
    } catch (_) {
      const range = document.createRange();
      range.selectNodeContents($('#diagContent'));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('copy');
      toast('Diagnostic copié (sélection) 📋');
    }
  }

  /* --------------------------------------------------------------------------
     Test API BrixHub
     -------------------------------------------------------------------------- */

  async function testSearch() {
    const nom = $('#tst-nom').value.trim();
    if (!nom) return toast('Le champ « Nom » est requis', 'error');

    const btn = $('#tst-run');
    btn.disabled = true;
    btn.textContent = '⏳ Recherche…';

    try {
      const result = await api('/api/test-search', {
        method: 'POST',
        body: JSON.stringify({
          nom_famille: nom,
          prenom: $('#tst-prenom').value.trim() || null,
          ville: $('#tst-ville').value.trim() || null,
          flexible: $('#tst-flex').checked,
          per_page: parseInt($('#tst-perpage').value, 10) || 10,
        }),
      });

      $('#tst-result-card').style.display = '';
      $('#tst-formatted').textContent = result.formatted;
      $('#tst-raw').textContent = JSON.stringify(result.result, null, 2);
      toast('Recherche effectuée ✅');
    } catch (err) { toast(err.message, 'error'); }
    finally {
      btn.disabled = false;
      btn.textContent = '🧪 Tester';
    }
  }

  /* --------------------------------------------------------------------------
     Écouteurs d'événements
     -------------------------------------------------------------------------- */

  $$('.nav-link').forEach((link) => link.addEventListener('click', () => switchTab(link.dataset.tab)));

  $('#menuBtn').addEventListener('click', () => document.body.classList.toggle('sidebar-open'));

  $('#diagBtn').addEventListener('click', openDiagnostic);
  $('#diagClose').addEventListener('click', closeDiagnostic);
  $('#diagBackdrop').addEventListener('click', closeDiagnostic);
  $('#diagCopy').addEventListener('click', copyDiagnostic);

  // Bouton de déconnexion (masqué si l'authentification est désactivée)
  if (!window.BRIXBOT_AUTH) {
    const logoutBtn = $('#logoutBtn');
    if (logoutBtn) logoutBtn.style.display = 'none';
  } else {
    $('#logoutBtn').addEventListener('click', async () => {
      try {
        await api('/api/logout', { method: 'POST' });
        window.location.href = '/login';
      } catch (_) { window.location.href = '/login'; }
    });
  }

  $('#refreshBtn').addEventListener('click', () => {
    if (state.tab === 'dashboard') loadDashboard();
    if (state.tab === 'whatsapp') loadWhatsApp();
    if (state.tab === 'logs') loadLogs();
  });

  $('#cfg-save').addEventListener('click', saveConfig);
  $('#ws-refresh').addEventListener('click', loadWhatsApp);
  $('#ws-restart').addEventListener('click', restartBot);
  $('#ai-save').addEventListener('click', saveAI);
  $('#ai-test').addEventListener('click', testAI);
  $('#ai-logs-refresh').addEventListener('click', loadAILogs);

  $('#log-status').addEventListener('change', () => { state.logsPage = 1; loadLogs(); });
  $('#log-type').addEventListener('change', () => { state.logsPage = 1; loadLogs(); });
  $('#logs-prev').addEventListener('click', () => { state.logsPage = Math.max(1, state.logsPage - 1); loadLogs(); });
  $('#logs-next').addEventListener('click', () => { state.logsPage += 1; loadLogs(); });
  $('#logs-clear').addEventListener('click', clearLogs);

  $('#ai-temp').addEventListener('input', () => {
    $('#ai-temp-value').textContent = Number($('#ai-temp').value).toFixed(2);
  });

  $('#tst-run').addEventListener('click', testSearch);

  // Entrée sur les champs de test IA
  $('#ai-test-msg').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') testAI();
  });

  /* --------------------------------------------------------------------------
     Polling en temps réel
     -------------------------------------------------------------------------- */

  // Le statut WhatsApp est rafraîchi toutes les 5 s quand l'onglet est visible
  setInterval(() => { if (state.tab === 'whatsapp') loadWhatsApp(); }, 5000);
  // Le tableau de bord est rafraîchi toutes les 20 s
  setInterval(() => { if (state.tab === 'dashboard') loadDashboard(); }, 20000);

  /* --------------------------------------------------------------------------
     Démarrage
     -------------------------------------------------------------------------- */
  switchTab('dashboard');
})();
