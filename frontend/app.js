// --- Theme (light/dark) -----------------------------------------------------
// Fully isolated: only touches a data-attribute on <html> and one
// localStorage key. Never calls the API, never touches library/chat/analyze
// state, runs before anything else boots.
function setTheme(mode) {
  document.documentElement.setAttribute('data-theme', mode);
  localStorage.setItem('echo_theme', mode);
  const lightBtn = document.getElementById('themeLightBtn');
  const darkBtn = document.getElementById('themeDarkBtn');
  if (lightBtn && darkBtn) {
    lightBtn.classList.toggle('active', mode === 'light');
    darkBtn.classList.toggle('active', mode === 'dark');
  }
}

function initTheme() {
  const saved = localStorage.getItem('echo_theme');
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  setTheme(saved || (prefersDark ? 'dark' : 'light'));
}

initTheme();

const API = window.ECHO_API_BASE || 'http://localhost:8000';

let googleToken = sessionStorage.getItem('echo_google_token') || null;
let currentUser = null;

let library = [];
let selected = new Set();
let libraryHealth = null;
let currentPaperId = sessionStorage.getItem('echo_currentPaperId') || null;
let chatHistory = {};
let pollTimer = null;

// Server-owned relevance thresholds/labels, fetched once at boot so the
// frontend never hardcodes a threshold number that could drift out of sync
// with relevance.py (the single source of truth on the backend).
let relevanceConfig = null;

async function loadRelevanceConfig() {
  try {
    const res = await fetch(API + '/config/relevance');
    relevanceConfig = await res.json();
  } catch (e) {
    relevanceConfig = {
      high_relevance_threshold: 0.45,
      relevant_threshold: 0.75,
      labels: { reviewed: 'Highly relevant', preprint: 'Relevant', low: 'Loosely relevant / No evidence found' },
    };
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}

// --- Google Sign-In ---------------------------------------------------------

function decodeJwtPayload(token) {
  try {
    const base64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(decodeURIComponent(atob(base64).split('').map(c =>
      '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2)
    ).join('')));
  } catch (e) { return null; }
}

function handleCredentialResponse(response) {
  googleToken = response.credential;
  sessionStorage.setItem('echo_google_token', googleToken);
  currentUser = decodeJwtPayload(googleToken);
  showApp();
}

function signOut() {
  sessionStorage.removeItem('echo_google_token');
  sessionStorage.removeItem('echo_currentPaperId');
  googleToken = null;
  currentUser = null;
  library = [];
  if (window.google && google.accounts && google.accounts.id) {
    google.accounts.id.disableAutoSelect();
  }
  showAuthGate();
}

function showAuthGate() {
  document.getElementById('authGate').style.display = 'flex';
  if (window.google && google.accounts && google.accounts.id && window.GOOGLE_CLIENT_ID) {
    google.accounts.id.initialize({ client_id: window.GOOGLE_CLIENT_ID, callback: handleCredentialResponse });
    google.accounts.id.renderButton(document.getElementById('googleSignInDiv'), { theme: 'filled_black', size: 'large', shape: 'pill' });
  } else {
    setTimeout(showAuthGate, 200);
  }
}

function showApp() {
  document.getElementById('authGate').style.display = 'none';
  const badge = document.getElementById('userBadge');
  if (currentUser) {
    badge.innerHTML = `Signed in as <strong>${escapeHtml(currentUser.name || currentUser.email || 'User')}</strong><br><span style="cursor:pointer; text-decoration:underline;" onclick="signOut()">Sign out</span>`;
  }
  refreshLibrary().catch(() => showToast('Could not reach the Echo API — is the backend running?'));
}

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + googleToken },
    ...opts,
  });
  if (res.status === 401) {
    signOut();
    throw new Error('Session expired — please sign in again');
  }
  if (!res.ok) { throw new Error('API error ' + res.status); }
  return res.json();
}

function goTo(screenId) {
  document.querySelectorAll('.navitem').forEach(i => i.classList.remove('active'));
  const item = document.querySelector(`.navitem[data-screen="${screenId}"]`);
  if (item && !item.classList.contains('disabled')) item.classList.add('active');
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById(screenId).classList.add('active');

  if (screenId === 'paper') renderPaper();
  if (screenId === 'compare') renderCompare();
  if (screenId === 'summaries') renderSummaries();
  if (screenId === 'library') renderLibrary();
} document.querySelectorAll('.navitem').forEach(item => {
  if (!item.classList.contains('disabled')) item.addEventListener('click', () => goTo(item.dataset.screen));
});

function pdfLinkHtml(p) {
  return p.pdf_url ? `<a class="btn btn-ghost btn-sm" href="${p.pdf_url}" target="_blank" rel="noopener">Open PDF ↗</a>` : '';
}

async function doSearch() {
  const q = document.getElementById('searchInput').value.trim();
  if (!q) return;
  const yearFrom = document.getElementById('yearFrom').value.trim() || null;
  const yearTo = document.getElementById('yearTo').value.trim() || null;
  const maxResults = parseInt(document.getElementById('resultCount').value, 10) || 15;
  const el = document.getElementById('searchResults');
  el.innerHTML = `<div class="empty-state"><span class="spinner" style="border-color:rgba(20,24,31,.2); border-top-color:var(--ink);"></span>Searching arXiv...</div>`;
  try {
    const data = await api('/search', { method: 'POST', body: JSON.stringify({ query: q, max_results: maxResults, year_from: yearFrom, year_to: yearTo }) });
    if (data.results && data.results.error) { el.innerHTML = `<div class="empty-state">Search failed: ${data.results.error}</div>`; return; }
    window._lastResults = data.results || [];
    renderSearchResults();
  } catch (e) {
    el.innerHTML = `<div class="empty-state">Could not reach the Echo API at ${API}. Is the backend running?</div>`;
  }
}

function renderSearchResults() {
  const el = document.getElementById('searchResults');
  const results = window._lastResults || [];
  if (results.length === 0) { el.innerHTML = ''; return; }
  el.innerHTML = results.map(p => {
    const added = library.some(lp => lp.id === p.id || lp.title === p.title);
    return `<div class="card"><div class="card-top"><div>
      <div class="paper-title">${escapeHtml(p.title)}</div>
      <div class="paper-meta">${escapeHtml((p.authors || []).join(', '))} · ${escapeHtml(p.year)} · ${escapeHtml(p.venue)}</div>
      <span class="badge preprint"><span class="dot"></span>arXiv</span>
    </div></div>
    <div class="card-actions">
      <button class="btn ${added ? 'btn-ghost' : 'btn-teal'}" ${added ? 'disabled' : ''} onclick='addFromSearch(this, ${JSON.stringify(p).replace(/'/g, "&#39;")})'>${added ? 'Added ✓' : '+ Add to library'}</button>
      ${pdfLinkHtml(p)}
    </div></div>`;
  }).join('');
}

async function addFromSearch(btn, paper) {
  if (btn) { btn.disabled = true; btn.textContent = 'Adding...'; }
  showToast('Adding to library — full summary generates in the background');
  try {
    const result = await api('/library/add-from-search', { method: 'POST', body: JSON.stringify(paper) });
    if (result.error) {
      showToast(result.error);
      if (btn) { btn.disabled = false; btn.textContent = '+ Add to library'; }
      return;
    }
    await refreshLibrary();
    renderSearchResults();
    showToast('Added! Summaries will fill in shortly.');
  } catch (e) {
    showToast('Failed to add paper');
    if (btn) { btn.disabled = false; btn.textContent = '+ Add to library'; }
  }
}

async function doUpload() {
  const input = document.getElementById('fileInput');
  if (!input.files.length) return;
  const form = new FormData();
  form.append('file', input.files[0]);
  showToast('Uploading and processing PDF...');
  try {
    const res = await fetch(API + '/library/upload', { method: 'POST', headers: { 'Authorization': 'Bearer ' + googleToken }, body: form });
    if (res.status === 401) { signOut(); return; }
    const data = await res.json();
    if (data.error) { showToast(data.error); input.value = ''; return; }
    await refreshLibrary();
    showToast('Uploaded! Summaries will fill in shortly.');
  } catch (e) { showToast('Upload failed'); }
  input.value = '';
}

async function refreshLibrary() {
  const data = await api('/library');
  library = data.papers || [];
  library.forEach(p => selected.add(p.id));
  try { libraryHealth = await api('/library/health'); } catch (e) { libraryHealth = null; }
  renderLibrary();
  renderSummaries();
  updateNavCount();
  if (document.getElementById('paper').classList.contains('active')) renderPaper();

  clearTimeout(pollTimer);
  const stillProcessing = library.some(p => !p.methodology);
  if (stillProcessing) {
    pollTimer = setTimeout(refreshLibrary, 4000);
  }
}

function renderLibrary() {
  const el = document.getElementById('libraryContent');
  if (library.length === 0) {
    el.innerHTML = `<div class="empty-state"><div class="big">Your library is empty</div>Search for papers or upload your own draft — only what you add shows up here.<br><button class="btn btn-primary" onclick="goTo('search')">Go to Search &amp; Upload</button></div>`;
    return;
  }
  const healthBadge = (libraryHealth && libraryHealth.score !== null && libraryHealth.score !== undefined)
    ? `<span class="badge ${libraryHealth.score >= 65 ? 'reviewed' : (libraryHealth.score >= 35 ? 'preprint' : 'low')}" style="margin-left:10px;" title="${escapeHtml(libraryHealth.label)}"><span class="dot"></span>Diversity ${libraryHealth.score}%</span>`
    : '';
  const toolbar = `<div class="lib-toolbar"><span>${library.length}/5 papers in your library · ${selected.size} selected${healthBadge}</span>
    <div style="display:flex; gap:8px;"><button class="btn btn-ghost" onclick="goTo('compare')">Compare selected</button><button class="btn btn-primary" onclick="goTo('summaries')">View summaries</button></div></div>`;
  const grid = '<div class="lib-grid">' + library.map(p => {
    const isChecked = selected.has(p.id);
    return `<div class="card">
      <div class="remove-btn" onclick="removeFromLibrary('${p.id}')" title="Remove">✕</div>
      <div class="check ${isChecked ? 'checked' : ''}" onclick="toggleSelect('${p.id}')" title="Select for compare"></div>
      <div class="paper-title" style="font-size:15px; padding-right:50px;">${escapeHtml(p.title)}</div>
      <div class="paper-meta">${escapeHtml(p.authors || '')} ${p.year ? '· ' + escapeHtml(p.year) : ''}</div>
      ${p.methodology ? '' : '<div class="paper-meta" style="color:#b8860b;">Summarizing...</div>'}
      <div class="card-actions"><button class="btn btn-ghost btn-sm" onclick="viewPaper('${p.id}')">View &amp; Ask</button><button class="btn btn-ghost btn-sm" onclick="openReader('${p.id}')">Read full paper</button>${pdfLinkHtml(p)}</div>
    </div>`;
  }).join('') + '</div>';
  el.innerHTML = toolbar + grid;
}

function toggleSelect(id) { if (selected.has(id)) selected.delete(id); else selected.add(id); renderLibrary(); renderCompare(); }

async function removeFromLibrary(id) {
  await api('/library/' + id, { method: 'DELETE' });
  selected.delete(id);
  if (currentPaperId === id) {
    currentPaperId = null;
    sessionStorage.removeItem('echo_currentPaperId');
  }
  await refreshLibrary();
  renderSearchResults();
}

async function viewPaper(id) {
  currentPaperId = id;
  sessionStorage.setItem('echo_currentPaperId', id);
  goTo('paper');
}

function renderPaper() {
  const titleEl = document.getElementById('paperTitle');
  const metaEl = document.getElementById('paperMeta');
  const content = document.getElementById('paperContent');
  if ((!currentPaperId || !library.some(x => x.id === currentPaperId)) && library.length > 0) {
    currentPaperId = library[0].id;
    sessionStorage.setItem('echo_currentPaperId', currentPaperId);
  }
  const p = library.find(x => x.id === currentPaperId);
  if (!p) {
    titleEl.textContent = 'Paper & Ask';
    metaEl.textContent = '';
    content.innerHTML = `<div class="empty-state"><div class="big">No paper selected</div>Add a paper to your library, then click "View &amp; Ask" to open it here.<br><button class="btn btn-primary" onclick="goTo('library')">Go to Library</button></div>`;
    return;
  }
  titleEl.textContent = p.title;
  metaEl.textContent = [p.authors, p.year, p.venue].filter(Boolean).join(' · ');
  if (!chatHistory[p.id]) chatHistory[p.id] = [];
  const switcher = library.length > 1
    ? `<select onchange="viewPaper(this.value)" style="margin:-6px 0 16px; padding:8px 10px; border-radius:8px; border:1px solid var(--line); font-family:'Inter',sans-serif; font-size:13px; background:#fff;">
        ${library.map(lp => `<option value="${lp.id}" ${lp.id === p.id ? 'selected' : ''}>${escapeHtml(lp.title.slice(0, 60))}</option>`).join('')}
      </select>`
    : '';
  content.innerHTML = `${switcher}<div class="card-actions" style="margin:-10px 0 18px;"><button class="btn btn-ghost btn-sm" onclick="openReader('${p.id}')">Read full paper</button>${pdfLinkHtml(p)}</div>
  <div class="detail-grid">
    <div>
      <div class="field"><div class="field-label">Methodology</div><div class="field-body">${renderMarkdown(p.methodology) || 'Not yet summarized.'}</div></div>
      <div class="field"><div class="field-label">Findings</div><div class="field-body">${renderMarkdown(p.findings) || 'Not yet summarized.'}</div></div>
      <div class="field"><div class="field-label">Research gap</div><div class="field-body"><span class="gap-highlight">${renderMarkdown(p.research_gap) || 'Not yet summarized.'}</span></div></div>
      <div class="field"><div class="field-label">Future work</div><div class="field-body">${renderMarkdown(p.future_work) || 'Not yet summarized.'}</div></div>
    </div>
    <div class="chat-panel">
      <div class="chat-head">ASK ECHO ABOUT THIS PAPER</div>
      <div style="display:flex; gap:6px; margin-bottom:12px;">
        <button id="scopeBtnPaper" type="button" class="scope-btn active" onclick="setChatScope('paper')">This paper</button>
        <button id="scopeBtnLibrary" type="button" class="scope-btn" onclick="setChatScope('library')">Whole library</button>
      </div>
      <div class="chat-log" id="chatLog"></div>
      <div class="chat-input"><input type="text" id="chatInput" placeholder="Ask a question..." onkeydown="if(event.key==='Enter') sendChat()"><button class="btn btn-teal" onclick="sendChat()">Ask</button></div>
    </div>
  </div>`;
  renderChatLog();
  chatScope = 'paper';
} function renderMarkdown(text) {
  if (!text) return '';
  let html = text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(?:^|\n)(\d+)\.\s+(.+)/g, '<br>$1. $2')
    .replace(/(?:^|\n)[-*]\s+(.+)/g, '<br>• $1')
    .replace(/\n/g, '<br>');
  return html;
}

let chatScope = 'paper';

function setChatScope(scope) {
  chatScope = scope;
  const paperBtn = document.getElementById('scopeBtnPaper');
  const libBtn = document.getElementById('scopeBtnLibrary');
  if (!paperBtn || !libBtn) return;
  paperBtn.classList.toggle('active', scope === 'paper');
  libBtn.classList.toggle('active', scope === 'library');
  const chatInput = document.getElementById('chatInput');
  if (chatInput) chatInput.focus();
}

// Reads thresholds from the server-fetched relevanceConfig instead of
// hardcoding numbers here — keeps this in permanent lockstep with
// relevance.py on the backend.
function relevanceLabel(distance) {
  const cfg = relevanceConfig || {
    high_relevance_threshold: 0.45,
    relevant_threshold: 0.75,
    labels: { reviewed: 'Highly relevant', preprint: 'Relevant', low: 'Loosely relevant' },
  };
  if (distance === null || distance === undefined) return { text: 'No evidence found', cls: 'low' };
  if (distance < cfg.high_relevance_threshold) return { text: cfg.labels.reviewed, cls: 'reviewed' };
  if (distance < cfg.relevant_threshold) return { text: cfg.labels.preprint, cls: 'preprint' };
  return { text: (cfg.labels.low || 'Loosely relevant').split(' / ')[0], cls: 'low' };
}

function renderChatLog() {
  const log = document.getElementById('chatLog');
  if (!log) return;
  const hist = chatHistory[currentPaperId] || [];
  log.innerHTML = hist.map(m => m.role === 'q'
    ? `<div class="bubble q">${escapeHtml(m.text)}</div>`
    : `<div class="bubble">${m.pending ? m.text : renderMarkdown(m.text)}
        ${(m.sources || []).map((s, i) => `<span class="src-chip">Source ${i + 1} · ${s.section}</span>`).join('')}
        ${m.rankedPapers ? `<div style="margin-top:12px; border-top:1px solid #2E3542; padding-top:10px;">
            <div style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#9CA096; margin-bottom:8px;">MOST RELEVANT PAPERS IN YOUR LIBRARY</div>
            ${m.rankedPapers.map((rp, i) => {
      const rel = relevanceLabel(rp.avg_distance);
      return `<div style="display:flex; align-items:center; gap:8px; margin-bottom:6px; cursor:pointer;" onclick="viewPaper('${rp.paper_id}')">
                <span style="font-family:'IBM Plex Mono',monospace; font-size:11px; color:#767A72;">${i + 1}.</span>
                <span style="font-size:12.5px; flex:1; text-decoration:underline;">${escapeHtml(rp.title.slice(0, 55))}</span>
                <span class="badge ${rel.cls}" style="margin:0;"><span class="dot"></span>${rel.text}</span>
              </div>`;
    }).join('')}
          </div>` : ''}
       </div>`
  ).join('');
  log.scrollTop = log.scrollHeight;
}

async function sendChat() {
  const input = document.getElementById('chatInput');
  const askBtn = input.nextElementSibling;
  const q = input.value.trim();
  if (!q || !currentPaperId) return;
  chatHistory[currentPaperId].push({ role: 'q', text: q });
  input.value = '';
  input.disabled = true;
  if (askBtn) askBtn.disabled = true;
  chatHistory[currentPaperId].push({ role: 'a', text: '<span class="spinner" style="border-top-color:#fff;"></span>Thinking...', pending: true });
  renderChatLog();
  try {
    const body = { question: q };
    if (chatScope === 'paper') body.paper_ids = [currentPaperId];
    const data = await api('/ask', { method: 'POST', body: JSON.stringify(body) });
    chatHistory[currentPaperId].pop();
    chatHistory[currentPaperId].push({ role: 'a', text: data.answer, sources: data.sources, rankedPapers: data.ranked_papers });
  } catch (e) {
    chatHistory[currentPaperId].pop();
    chatHistory[currentPaperId].push({ role: 'a', text: 'Something went wrong reaching Echo. Please try again.' });
  }
  input.disabled = false;
  if (askBtn) askBtn.disabled = false;
  renderChatLog();
  input.focus();
}

function renderCompare() {
  const el = document.getElementById('compareContent');
  const rows = library.filter(p => selected.has(p.id));
  if (rows.length < 2) {
    el.innerHTML = `<div class="empty-state"><div class="big">Select at least 2 papers</div>Check papers in your library to compare them side by side.<br><button class="btn btn-primary" onclick="goTo('library')">Go to Library</button></div>`;
    return;
  }
  const fields = [['methodology', 'Methodology'], ['findings', 'Findings'], ['research_gap', 'Research gap'], ['future_work', 'Future work']];
  let html = `<table class="compare"><tr><th></th>${rows.map(p => `<th>${escapeHtml(p.title.slice(0, 30))}</th>`).join('')}</tr>`;
  fields.forEach(([key, label]) => { html += `<tr><td><strong>${label}</strong></td>${rows.map(p => `<td>${renderMarkdown(p[key]) || '—'}</td>`).join('')}</tr>`; });
  html += '</table>';
  el.innerHTML = html;
}

function renderSummaries() {
  const el = document.getElementById('summariesContent');
  if (library.length === 0) {
    el.innerHTML = `<div class="empty-state"><div class="big">No summaries yet</div>Add papers to your library and they'll be summarized automatically.<br><button class="btn btn-primary" onclick="goTo('search')">Go to Search &amp; Upload</button></div>`;
    return;
  }
  el.innerHTML = library.map(p => `<div class="card">
      <div class="paper-title" style="font-size:16px;">${escapeHtml(p.title)}</div>
      <div class="sum-row"><span class="field-label">Methodology</span><span>${renderMarkdown(p.methodology) || 'Processing...'}</span></div>
      <div class="sum-row"><span class="field-label">Findings</span><span>${renderMarkdown(p.findings) || 'Processing...'}</span></div>
      <div class="sum-row"><span class="field-label">Research gap</span><span><span class="gap-highlight">${renderMarkdown(p.research_gap) || 'Processing...'}</span></span></div>
      <div class="sum-row"><span class="field-label">Future work</span><span>${renderMarkdown(p.future_work) || 'Processing...'}</span></div>
      <div class="card-actions"><button class="btn btn-ghost btn-sm" onclick="openReader('${p.id}')">Read full paper</button><button class="btn btn-ghost btn-sm" onclick="viewPaper('${p.id}')">Ask about this</button>${pdfLinkHtml(p)}</div>
    </div>`).join('');
}

async function analyzeIdea() {
  const el = document.getElementById('contribResult');
  const idea = document.getElementById('ideaInput').value.trim();
  if (library.length === 0) {
    el.innerHTML = `<div class="empty-state"><div class="big">Add papers first</div>Echo matches your idea against papers in your library — add a few, then come back here.<br><button class="btn btn-primary" onclick="goTo('search')">Go to Search &amp; Upload</button></div>`;
    return;
  }
  el.innerHTML = `<div class="empty-state"><span class="spinner" style="border-color:rgba(20,24,31,.2); border-top-color:var(--ink);"></span>Matching against your library...</div>`;
  try {
    const data = await api('/contribute', { method: 'POST', body: JSON.stringify({ idea, paper_ids: library.map(p => p.id) }) });
    if (data.error) { el.innerHTML = `<div class="empty-state">${data.error}</div>`; return; }
    let noveltyClass = 'reviewed';
    if (data.novelty.startsWith('High')) noveltyClass = 'low';
    else if (data.novelty.startsWith('Medium')) noveltyClass = 'preprint';
    el.innerHTML = `<div class="card">
        <div class="eyebrow">Most relevant paper in your library</div>
        <div class="paper-title">${escapeHtml(data.title)}</div>
        <span class="badge ${noveltyClass}"><span class="dot"></span>${data.novelty}</span>
        <div class="card-actions"><button class="btn btn-ghost btn-sm" onclick="viewPaper('${data.paper_id}')">Open this paper</button></div>
      </div>
      <div class="card"><div class="field-label">How you could build on it</div><div class="field-body" style="margin-top:8px;">${renderMarkdown(data.guidance)}</div></div>`;
  } catch (e) { el.innerHTML = `<div class="empty-state">Something went wrong reaching Echo.</div>`; }
}

async function runAnalysis() {
  const q = document.getElementById('analyzeInput').value.trim();
  const el = document.getElementById('analyzeResult');
  if (!q) return;
  el.innerHTML = `<div class="empty-state"><span class="spinner" style="border-color:rgba(20,24,31,.2); border-top-color:var(--ink);"></span>Analyzing your library...</div>`;
  try {
    const data = await api('/analyze', { method: 'POST', body: JSON.stringify({ question: q }) });
    if (data.error) { el.innerHTML = `<div class="empty-state">${escapeHtml(data.error)}</div>`; return; }
    el.innerHTML = renderAnalysisResult(data);
  } catch (e) {
    console.error('Analyze failed:', e);
    el.innerHTML = `<div class="empty-state">Something went wrong reaching Echo. (${escapeHtml(e.message || 'unknown error')})</div>`;
  }
}

function renderAnalysisResult(data) {
  const titles = data.titles || {};
  const paperIds = Object.keys(titles);
  let html = '';

  // Every badge shows the 0-100 score (higher = better) instead of raw
  // distance — the score is server-computed and its tier boundaries are
  // mathematically anchored to the same thresholds driving the color, so
  // score and color can never disagree.
  const badgeFor = (item) => `<span class="badge ${item.css_class}" style="margin:0;" title="${item.score !== null && item.score !== undefined ? item.score + '% match' : 'no evidence'}"><span class="dot"></span>${escapeHtml(item.label)}</span>`;

  // 0) Fit summary — accounts for ALL papers (high + relevant + loosely
  // relevant), so the sentence never leaves papers unaccounted for.
  const fs = data.fit_summary;
  if (fs) {
    const parts = [];
    parts.push(`<strong>${fs.high_count}</strong> of <strong>${fs.total}</strong> papers are ${fs.high_label.toLowerCase()} to this question`);
    if (fs.relevant_count > 0) parts.push(`${fs.relevant_count} more are ${fs.relevant_label.toLowerCase()}`);
    if (fs.low_count > 0) parts.push(`${fs.low_count} ${fs.low_count === 1 ? 'is' : 'are'} only ${fs.low_label.toLowerCase()}`);
    if (fs.coverage_pct !== null && fs.coverage_pct !== undefined) {
      parts.push(`your library has at least a partial match for <strong>${fs.coverage_pct}%</strong> of this question's sub-topics`);
    }
    let weakLine = '';
    if (fs.weakest_title) {
      weakLine = `<div style="margin-top:8px; font-size:12.5px; color:var(--ink-soft);">Weakest fit for this question: <strong>${escapeHtml(fs.weakest_title.slice(0, 60))}</strong> — likely still useful for other angles, just not this one.</div>`;
    }
    html += `<div class="card" style="background:var(--teal-soft); border-color:var(--teal);">
        <div class="eyebrow">How well your library fits this question</div>
        <div style="font-size:14.5px; margin-top:6px; line-height:1.6;">${parts.join(' · ')}.</div>
        ${weakLine}
      </div>`;
  }

  // 1) Overall ranked papers
  html += `<div class="card"><div class="eyebrow">Papers ranked by overall relevance</div>`;
  if (!data.ranked_overall || data.ranked_overall.length === 0) {
    html += `<div class="field-body" style="margin-top:8px;">No relevant content found for this question.</div>`;
  } else {
    html += data.ranked_overall.map((item, i) => `
      <div style="display:flex; align-items:center; gap:10px; margin:10px 0; cursor:pointer;" onclick="viewPaper('${item.paper_id}')">
        <span style="font-family:'IBM Plex Mono',monospace; color:#767A72; font-size:12px;">${i + 1}.</span>
        <span style="flex:1; text-decoration:underline; font-size:13.5px;">${escapeHtml((titles[item.paper_id] || item.paper_id).slice(0, 60))}</span>
        ${badgeFor(item)}
      </div>`).join('');
  }
  html += `</div>`;

  // 2) Section leaderboards
  const catEntries = Object.entries(data.section_leaders || {});
  html += `<div class="card"><div class="eyebrow">Most relevant paper, by section</div>`;
  if (catEntries.length === 0) {
    html += `<div class="field-body" style="margin-top:8px;">No section-level matches found.</div>`;
  } else {
    html += catEntries.map(([cat, ranked]) => {
      if (!ranked.length) return '';
      const top = ranked[0];
      return `<div style="margin:12px 0; padding-bottom:12px; border-bottom:1px solid var(--line);">
          <div class="field-label">${escapeHtml(cat)}</div>
          <div style="display:flex; align-items:center; gap:10px; margin-top:5px; cursor:pointer;" onclick="viewPaper('${top.paper_id}')">
            <span style="flex:1; text-decoration:underline; font-size:13.5px;">${escapeHtml((titles[top.paper_id] || top.paper_id).slice(0, 55))}</span>
            ${badgeFor(top)}
          </div>
        </div>`;
    }).join('');
  }
  html += `</div>`;

  // 3) Heatmap — cells show a 0-100 score (higher = better) with strong
  // contrast (bold text + shadow) so numbers stay legible on every color,
  // and columns are ordered by how much real data is available so the
  // most informative sections read first, left to right.
  let categories = Object.keys(data.section_leaders || {});
  if (categories.length && paperIds.length) {
    const countForCat = (cat) => paperIds.filter(pid => (data.paper_section_scores[pid] || {})[cat]).length;
    categories = categories.slice().sort((a, b) => countForCat(b) - countForCat(a) || a.localeCompare(b));

    const colWidth = 92;
    const cellColor = (cssClass) => cssClass === 'reviewed' ? '#2F6F6B' : (cssClass === 'preprint' ? '#C48A2E' : '#B4432E');
    html += `<div class="card"><div class="eyebrow">Relevance heatmap</div>
      <div style="font-size:12.5px; color:var(--ink-soft); margin-top:4px;">Each cell is a match score from 0–100 (higher = closer match). Columns are ordered left-to-right by how much data is available.</div>
      <div style="overflow-x:auto; margin-top:12px;">
        <div style="display:grid; grid-template-columns: 150px repeat(${categories.length}, ${colWidth}px); gap:4px; min-width:${150 + categories.length * (colWidth + 4)}px;">
          <div></div>
          ${categories.map(c => `<div style="font-family:'IBM Plex Mono',monospace; font-size:10px; font-weight:600; text-align:center; color:var(--ink-soft); align-self:end; padding-bottom:5px; line-height:1.2;">${escapeHtml(c)}</div>`).join('')}
          ${paperIds.map(pid => {
      const scores = data.paper_section_scores[pid] || {};
      const rowLabel = `<div style="font-size:12px; padding:6px 4px; display:flex; align-items:center;">${escapeHtml((titles[pid] || pid).slice(0, 20))}</div>`;
      const cells = categories.map(c => {
        const item = scores[c];
        if (!item) {
          return `<div title="${escapeHtml(c)}: no matching content found in this section" style="background:var(--line); border-radius:5px; height:34px; display:flex; align-items:center; justify-content:center; color:var(--ink-soft); font-size:11px;">—</div>`;
        }
        return `<div title="${escapeHtml(c)}: ${item.label} (${item.score}% match)" style="background:${cellColor(item.css_class)}; border-radius:5px; height:34px; display:flex; align-items:center; justify-content:center; color:#fff; font-size:12.5px; font-weight:700; font-family:'IBM Plex Mono',monospace; text-shadow:0 1px 2px rgba(0,0,0,.4);">${item.score}%</div>`;
      }).join('');
      return rowLabel + cells;
    }).join('')}
        </div>
      </div>
      <div style="margin-top:12px; font-size:11px; color:var(--ink-soft);">Teal = highly relevant (75–100%) · amber = relevant (40–74%) · red = loosely relevant (below 40%) · gray "—" = no matching content found in that section for that paper. Higher % is always better.</div>
    </div>`;
  }

  // 4) Sub-topic coverage — colored dot uses the same score-based tier as
  // the heatmap, with the score visible in the tooltip.
  const subtopics = data.subtopics || [];
  if (subtopics.length) {
    const colWidth = 100;
    const dotColor = (cssClass) => cssClass === 'reviewed' ? '#2F6F6B' : (cssClass === 'preprint' ? '#C48A2E' : null);
    html += `<div class="card"><div class="eyebrow">Sub-topic coverage</div>
      <div style="overflow-x:auto; margin-top:12px;">
        <div style="display:grid; grid-template-columns: 170px repeat(${paperIds.length}, ${colWidth}px); gap:4px; min-width:${170 + paperIds.length * (colWidth + 4)}px;">
          <div></div>
          ${paperIds.map(pid => `<div style="font-size:10.5px; text-align:center; color:var(--ink-soft); align-self:end; padding-bottom:5px; line-height:1.2;">${escapeHtml((titles[pid] || pid).slice(0, 16))}</div>`).join('')}
          ${subtopics.map(st => {
      const row = data.coverage[st] || {};
      const rowLabel = `<div style="font-size:12px; padding:6px 4px; display:flex; align-items:center;">${escapeHtml(st)}</div>`;
      const cells = paperIds.map(pid => {
        const cell = row[pid] || { covered: false, distance: null, score: null, label: 'No evidence found', css_class: 'low' };
        const hasScore = cell.score !== null && cell.score !== undefined;
        const title = hasScore ? `${cell.label} (${cell.score}% match)` : 'No evidence found';
        const color = dotColor(cell.css_class);
        return `<div style="display:flex; align-items:center; justify-content:center; height:32px;" title="${escapeHtml(title)}">
                ${color ? `<div style="width:14px; height:14px; border-radius:50%; background:${color};"></div>` : '<span style="color:var(--ink-soft); font-size:12px;">—</span>'}
              </div>`;
      }).join('');
      return rowLabel + cells;
    }).join('')}
        </div>
      </div>
      <div style="margin-top:12px; font-size:11px; color:var(--ink-soft);">Sub-topics are auto-derived from your question and matched independently — a short sub-topic can score better than the full question. Dot color follows the same scale as the heatmap above: teal = highly relevant, amber = relevant, blank = no strong match.</div>
    </div>`;
  }

  return html;
}

async function openReader(id) {
  const p = library.find(x => x.id === id) || await api('/paper/' + id);
  document.getElementById('readerMeta').textContent = [p.authors, p.year, p.venue].filter(Boolean).join(' · ');
  document.getElementById('readerTitle').textContent = p.title;
  document.getElementById('readerBody').innerHTML = `<p>${(p.full_text || 'No full text stored for this paper.').slice(0, 6000)}</p>`;
  document.getElementById('readerModal').classList.remove('hidden');
}
function closeReader() { document.getElementById('readerModal').classList.add('hidden'); }
document.getElementById('readerModal').addEventListener('click', e => { if (e.target.id === 'readerModal') closeReader(); });

function updateNavCount() {
  const badge = document.getElementById('navLibCount');
  badge.textContent = library.length;
  badge.classList.toggle('show', library.length > 0);
}

// App boot
loadRelevanceConfig().then(() => {
  if (googleToken) {
    currentUser = decodeJwtPayload(googleToken);
    showApp();
  } else {
    showAuthGate();
  }
});