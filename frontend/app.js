const API = window.ECHO_API_BASE || 'http://localhost:8000';

let library = [];
let selected = new Set();
let currentPaperId = sessionStorage.getItem('echo_currentPaperId') || null;
let chatHistory = {};
let pollTimer = null;

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
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
}
document.querySelectorAll('.navitem').forEach(item => {
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
      <div class="paper-title">${p.title}</div>
      <div class="paper-meta">${(p.authors || []).join(', ')} · ${p.year} · ${p.venue}</div>
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
    await api('/library/add-from-search', { method: 'POST', body: JSON.stringify(paper) });
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
    const res = await fetch(API + '/library/upload', { method: 'POST', body: form });
    if (!res.ok) throw new Error();
    await refreshLibrary();
    showToast('Uploaded! Summaries will fill in shortly.');
  } catch (e) { showToast('Upload failed'); }
  input.value = '';
}

async function refreshLibrary() {
  const data = await api('/library');
  library = data.papers || [];
  library.forEach(p => selected.add(p.id));
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
  const toolbar = `<div class="lib-toolbar"><span>${library.length} paper(s) in your library · ${selected.size} selected</span>
    <div style="display:flex; gap:8px;"><button class="btn btn-ghost" onclick="goTo('compare')">Compare selected</button><button class="btn btn-primary" onclick="goTo('summaries')">View summaries</button></div></div>`;
  const grid = '<div class="lib-grid">' + library.map(p => {
    const isChecked = selected.has(p.id);
    return `<div class="card">
      <div class="remove-btn" onclick="removeFromLibrary('${p.id}')" title="Remove">✕</div>
      <div class="check ${isChecked ? 'checked' : ''}" onclick="toggleSelect('${p.id}')" title="Select for compare"></div>
      <div class="paper-title" style="font-size:15px; padding-right:50px;">${p.title}</div>
      <div class="paper-meta">${p.authors || ''} ${p.year ? '· ' + p.year : ''}</div>
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
  // Fall back to the most recently added paper if nothing is explicitly selected
  // (e.g. first visit, or the previously selected paper was removed).
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
        ${library.map(lp => `<option value="${lp.id}" ${lp.id === p.id ? 'selected' : ''}>${lp.title.slice(0, 60)}</option>`).join('')}
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
      <div class="chat-log" id="chatLog"></div>
      <div class="chat-input"><input type="text" id="chatInput" placeholder="Ask a question..." onkeydown="if(event.key==='Enter') sendChat()"><button class="btn btn-teal" onclick="sendChat()">Ask</button></div>
    </div>
  </div>`;
  renderChatLog();
}

function renderMarkdown(text) {
  if (!text) return '';
  let html = text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(?:^|\n)(\d+)\.\s+(.+)/g, '<br>$1. $2')
    .replace(/(?:^|\n)[-*]\s+(.+)/g, '<br>• $1')
    .replace(/\n/g, '<br>');
  return html;
}

function renderChatLog() {
  const log = document.getElementById('chatLog');
  if (!log) return;
  const hist = chatHistory[currentPaperId] || [];
  log.innerHTML = hist.map(m => m.role === 'q'
    ? `<div class="bubble q">${m.text}</div>`
    : `<div class="bubble">${m.pending ? m.text : renderMarkdown(m.text)}${(m.sources || []).map((s, i) => `<span class="src-chip">Source ${i + 1} · ${s.section}</span>`).join('')}</div>`
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
    const data = await api('/ask', { method: 'POST', body: JSON.stringify({ question: q, paper_ids: [currentPaperId] }) });
    chatHistory[currentPaperId].pop(); // remove the pending placeholder
    chatHistory[currentPaperId].push({ role: 'a', text: data.answer, sources: data.sources });
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
  let html = `<table class="compare"><tr><th></th>${rows.map(p => `<th>${p.title.slice(0, 30)}</th>`).join('')}</tr>`;
  fields.forEach(([key, label]) => { html += `<tr><td><strong>${label}</strong></td>${rows.map(p => `<td>${p[key] || '—'}</td>`).join('')}</tr>`; });
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
      <div class="paper-title" style="font-size:16px;">${p.title}</div>
      <div class="sum-row"><span class="field-label">Methodology</span><span>${p.methodology || 'Processing...'}</span></div>
      <div class="sum-row"><span class="field-label">Findings</span><span>${p.findings || 'Processing...'}</span></div>
      <div class="sum-row"><span class="field-label">Research gap</span><span><span class="gap-highlight">${p.research_gap || 'Processing...'}</span></span></div>
      <div class="sum-row"><span class="field-label">Future work</span><span>${p.future_work || 'Processing...'}</span></div>
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
        <div class="paper-title">${data.title}</div>
        <span class="badge ${noveltyClass}"><span class="dot"></span>${data.novelty}</span>
        <div class="card-actions"><button class="btn btn-ghost btn-sm" onclick="viewPaper('${data.paper_id}')">Open this paper</button></div>
      </div>
      <div class="card"><div class="field-label">How you could build on it</div><div class="field-body" style="margin-top:8px; white-space:pre-wrap;">${data.guidance}</div></div>`;
  } catch (e) { el.innerHTML = `<div class="empty-state">Something went wrong reaching Echo.</div>`; }
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

refreshLibrary().catch(() => showToast('Could not reach the Echo API — start the backend first.'));