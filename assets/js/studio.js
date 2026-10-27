/* ==========================================================================
   WayinVideo Studio — frontend state, API client and all five tool flows
   No dependencies. Every flow runs on the same page: POST job -> poll status
   -> render result. Talks to the FastAPI backend in backend/.
   ========================================================================== */
(function () {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const API_CANDIDATES = ['', 'http://127.0.0.1:8000', 'http://localhost:8000'];
  let API = '';
  let HEALTH = null;

  /* ---------------------------------------------------------------- utils */
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&' + 'amp;').replace(/</g, '&' + 'lt;').replace(/>/g, '&' + 'gt;')
    .replace(/"/g, '&' + 'quot;');

  const fmtTime = (sec) => {
    sec = Math.max(0, Number(sec) || 0);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    return (h ? `${h}:${String(m).padStart(2, '0')}` : String(m)) + ':' + String(s).padStart(2, '0');
  };

  const show = (el, on = true) => { if (el) el.hidden = !on; };
  const fail = (el, msg) => { if (!el) return; el.hidden = !msg; el.textContent = msg || ''; };

  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
    }
    if (btn) {
      const old = btn.textContent;
      btn.textContent = 'Copied ✓';
      setTimeout(() => { btn.textContent = old; }, 1400);
    }
  }

  async function download(path, body, filename) {
    const res = await fetch(API + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
      throw new Error(detail);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || (res.headers.get('content-disposition') || '').split('filename=')[1] || 'export';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  /* ------------------------------------------------------------ API client */
  async function request(path, opts = {}) {
    const res = await fetch(API + path, opts);
    const type = res.headers.get('content-type') || '';
    const data = type.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok) {
      const detail = data && data.detail
        ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
        : `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return data;
  }

  const getJSON = (path) => request(path);
  const postJSON = (path, body) => request(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });

  /** Poll a /status endpoint until the task finishes; render steps while it runs. */
  function poll(statusPath, ui) {
    return new Promise((resolve, reject) => {
      let stopped = false;
      const tick = async () => {
        if (stopped) return;
        let data;
        try { data = await getJSON(statusPath); }
        catch (err) { stopped = true; return reject(err); }

        if (ui) {
          ui.render(data);
          if (data.status === 'error') { stopped = true; ui.error(data.error || 'failed'); return reject(new Error(data.error || 'failed')); }
          if (data.status === 'done') { stopped = true; ui.render(data); return resolve(data.result); }
        } else if (data.status === 'done') {
          stopped = true; return resolve(data.result);
        } else if (data.status === 'error') {
          stopped = true; return reject(new Error(data.error || 'failed'));
        }
        setTimeout(tick, 1100);
      };
      tick();
    });
  }

  /** Standard progress block renderer (bar + stage + step list). */
  function progressUI(ids) {
    const box = $(ids.box), bar = $(ids.bar), pct = $(ids.pct),
      stage = $(ids.stage), steps = $(ids.steps);
    return {
      start() { show(box, true); this.render({ progress: 0, stage: 'Queued', steps: [] }); },
      render(task) {
        show(box, true);
        if (bar) bar.style.width = (task.progress || 0) + '%';
        if (pct) pct.textContent = Math.round(task.progress || 0) + '%';
        if (stage) stage.textContent = task.stage || task.status;
        if (steps) {
          steps.innerHTML = (task.steps || []).map((s) =>
            `<li class="${esc(s.state)}">${esc(s.label)}</li>`).join('');
        }
      },
      error(msg) {
        const li = document.createElement('li');
        li.className = 'error'; li.textContent = msg;
        if (steps) steps.appendChild(li);
      },
    };
  }

  async function probeBackend() {
    const dot = $('#apiDot'), text = $('#apiText');
    for (const base of API_CANDIDATES) {
      try {
        const res = await fetch(base + '/api/health', { cache: 'no-store' });
        if (!res.ok) continue;
        HEALTH = await res.json();
        API = base;
        if (dot) dot.parentElement.classList.add('ok');
        if (text) {
          text.textContent = `Engine online · Whisper ${HEALTH.whisper.available ? 'ready' : 'missing'} · LLM ${HEALTH.llm}`;
        }
        const fe = $('#footEngine'), fl = $('#footLlm');
        if (fe) fe.textContent = `Whisper ${HEALTH.whisper.model} · ${HEALTH.whisper.available ? 'ready' : 'install faster-whisper'}`;
        if (fl) fl.textContent = HEALTH.llm === 'on' ? `LLM: ${HEALTH.llm_provider}` : 'LLM: local fallback (no key)';
        return;
      } catch (e) { /* try next base */ }
    }
    if (dot) dot.parentElement.classList.add('bad');
    if (text) text.textContent = 'Engine offline — start it with: uvicorn app.main:app --port 8000';
  }

  /* ------------------------------------------------------------- tool tabs */
  const TOOLS = ['clips', 'editor', 'summarizer', 'v2t', 'notes'];

  function activate(tool, push = true) {
    if (!TOOLS.includes(tool)) tool = 'clips';
    $$('.stab').forEach((b) => b.classList.toggle('active', b.dataset.tool === tool));
    $$('.tool-panel').forEach((p) => p.classList.toggle('active', p.id === 'tool-' + tool));
    if (push && location.hash !== '#' + tool) history.replaceState(null, '', '#' + tool);
  }

  $$('.stab').forEach((b) => b.addEventListener('click', () => activate(b.dataset.tool)));
  window.addEventListener('hashchange', () => activate(location.hash.slice(1), false));

  /* generic chip/swatch selectors */
  function bindChips(scope, attr, onChange) {
    $$(`[${attr}]`, scope).forEach((row) => {
      const key = row.getAttribute(attr);
      $$('.chip-btn, .swatch', row).forEach((btn) => {
        btn.addEventListener('click', () => {
          $$('.chip-btn, .swatch', row).forEach((b) => b.classList.remove('active'));
          btn.classList.add('active');
          onChange(key, btn.dataset.value);
        });
      });
    });
  }

  /* ======================================================= 1. CLIP MAKER == */
  const clipState = { mode: 'url', kind: 'normal', style: {}, count: 10, taskId: null, file: null };

  const KIND_HINTS = {
    normal: 'Standard cut — scored clips with titles, tags and a description per platform.',
    hook: 'Adds 7 scroll-stopping hooks per clip: on-screen text, spoken line, the pattern used, why it works — plus the best pick and an A/B variant.',
  };

  const PALETTE_HEX = {
    classic: ['#ffe14d', '#ffffff'], white: ['#ffffff', '#ffffff'],
    neon: ['#00fff7', '#f2ffff'], contrast: ['#ffe14d', '#ffffff'],
    brand: ['#2bf5a0', '#ffffff'], ocean: ['#38bdf8', '#eaf6ff'],
  };
  const SIZE_PX = { small: 17, medium: 23, large: 30 };

  function readStyle(rootSel) {
    const root = document.querySelector(rootSel);
    if (!root) return {};
    const out = {};
    $$('[data-style] .active, [data-estyle] .active', root).forEach((btn) => {
      const row = btn.parentElement;
      const key = (row.getAttribute('data-style') || row.getAttribute('data-estyle'));
      out[key] = btn.dataset.value;
    });
    const font = root.querySelector('#styleFont, #editFont');
    if (font) out.font = font.value;
    return out;
  }

  function styleLabel(s) {
    const cap = (v) => (v || '').charAt(0).toUpperCase() + (v || '').slice(1);
    return [s.motion, s.palette, s.font, s.size, s.placement].map(cap).join(' · ');
  }

  function paintPreview() {
    const s = readStyle('#styleDrawer');
    clipState.style = s;
    const box = $('#subPreview'), txt = $('#subText');
    if (!box || !txt) return;
    const hex = PALETTE_HEX[s.palette] || PALETTE_HEX.classic;
    txt.style.setProperty('--sub-primary', hex[1]);
    txt.style.setProperty('--sub-outline', '#000');
    txt.style.setProperty('--sub-font', s.font);
    txt.style.setProperty('--sub-size', (SIZE_PX[s.size] || 23) + 'px');
    txt.classList.toggle('bounce', s.motion === 'moving');
    box.style.setProperty('--sub-primary', hex[0]);
    const sum = $('#styleSummary');
    if (sum) sum.textContent = styleLabel(s);
    const gen = $('#clipGenerate');
    if (gen) gen.textContent = `Generate ${clipState.count} clip${clipState.count > 1 ? 's' : ''}`;
  }

  function paintEditStyle() {
    const s = readStyle('#editStyleDrawer');
    clipState.editStyle = s;
    const sum = $('#editStyleSummary');
    if (sum) sum.textContent = styleLabel(s);
  }

  // input mode toggle + dropzone
  $$('#clipInputMode .seg-btn').forEach((btn) => btn.addEventListener('click', () => {
    clipState.mode = btn.dataset.mode;
    $$('#clipInputMode .seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
    $$('.seg-pane', $('#clipInputMode').parentElement).forEach((p) =>
      p.classList.toggle('active', p.dataset.pane === btn.dataset.mode));
  }));

  // clip type toggle: Normal clips vs Hook clips
  $$('#clipKindMode .seg-btn').forEach((btn) => btn.addEventListener('click', () => {
    clipState.kind = btn.dataset.kind;
    $$('#clipKindMode .seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
    const hint = $('#clipKindHint');
    if (hint) hint.textContent = KIND_HINTS[clipState.kind] || KIND_HINTS.normal;
  }));

  const drop = $('#clipDrop'), fileInput = $('#clipFile');
  if (drop) {
    const setFile = (f) => {
      clipState.file = f;
      $('#clipDropLabel').textContent = f ? f.name : 'Drop a long-form video here';
      drop.classList.toggle('drag', !!f);
    };
    drop.addEventListener('click', () => fileInput.click());
    drop.addEventListener('keydown', (e) => { if (e.key === 'Enter') fileInput.click(); });
    fileInput.addEventListener('change', () => setFile(fileInput.files[0]));
    ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); drop.classList.add('drag');
    }));
    ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); if (ev === 'drop') setFile(e.dataTransfer.files[0]);
      else drop.classList.remove('drag');
    }));
  }

  // collapsible drawers
  function bindDrawer(headId, bodyId) {
    const head = $('#' + headId), body = $('#' + bodyId);
    if (!head || !body) return;
    head.addEventListener('click', () => {
      const open = head.getAttribute('aria-expanded') === 'true';
      head.setAttribute('aria-expanded', String(!open));
      body.hidden = open;
    });
  }
  bindDrawer('styleToggle', 'styleDrawer');
  bindDrawer('editStyleToggle', 'editStyleDrawer');

  const countInput = $('#clipCount');
  if (countInput) countInput.addEventListener('input', () => {
    clipState.count = Number(countInput.value);
    $('#clipCountVal').textContent = clipState.count;
    paintPreview();
  });

  bindChips(document, 'data-style', paintPreview);
  bindChips(document, 'data-estyle', paintEditStyle);
  ['#styleFont', '#editFont'].forEach((sel) => { const el = $(sel); if (el) el.addEventListener('change', paintPreview); });

  const clipProgress = progressUI({ box: '#clipProgress', bar: '#clipBar', pct: '#clipPct', stage: '#clipStage', steps: '#clipSteps' });

  async function generateClips() {
    const errBox = $('#clipError');
    fail(errBox, '');
    const isUrl = clipState.mode === 'url';
    const url = ($('#clipUrl').value || '').trim();

    if (isUrl && !url) return fail(errBox, 'Paste a video URL first.');
    if (!isUrl && !clipState.file) return fail(errBox, 'Choose a video file first.');

    const btn = $('#clipGenerate');
    btn.disabled = true; btn.textContent = 'Starting…';
    show($('#clipEmpty'), false); show($('#clipGrid'), false);
    clipProgress.start();

    try {
      let taskId;
      if (isUrl) {
        const data = await postJSON('/api/clip-maker/process', {
          source: url, style: clipState.style, count: clipState.count, mode: clipState.kind,
        });
        taskId = data.task_id;
      } else {
        const fd = new FormData();
        fd.append('file', clipState.file);
        fd.append('style', JSON.stringify(clipState.style));
        fd.append('count', String(clipState.count));
        fd.append('mode', clipState.kind);
        const data = await request('/api/clip-maker/upload', { method: 'POST', body: fd });
        taskId = data.task_id;
      }
      clipState.taskId = taskId;
      const result = await poll(`/api/clip-maker/status/${taskId}`, clipProgress);
      renderClips(result);
    } catch (e) {
      fail(errBox, e.message);
      clipProgress.error(e.message);
      show($('#clipEmpty'), true);
    } finally {
      btn.disabled = false;
      paintPreview();
    }
  }

  $('#clipGenerate').addEventListener('click', generateClips);

  const META_TABS = [
    { key: 'youtube', label: 'YouTube' },
    { key: 'reels', label: 'Reels' },
    { key: 'tiktok', label: 'TikTok' },
  ];
  const TAG_KEY = { youtube: 'youtube', reels: 'instagram', tiktok: 'tiktok' };

  function metaBody(clip, platform) {
    const m = clip.metadata || {};
    const titles = m.titles || {};
    const tags = (m.tags || {})[TAG_KEY[platform]] || [];
    const hashes = (m.hashtags || {})[TAG_KEY[platform]] || (m.hashtags || {}).instagram || [];
    const title = titles[platform] || titles.youtube || 'Untitled clip';
    const tagsKey = TAG_KEY[platform];
    const desc = platform === 'youtube'
      ? `<span class="label">YouTube description</span>
         <textarea readonly>${esc(m.youtube_description || '')}</textarea>`
      : '';
    return `
      <span class="label">Title</span>
      <div class="meta-title">${esc(title)}</div>
      <div class="tagline">${tags.slice(0, 14).map((t) => `<span class="tag">#${esc(t)}</span>`).join('')}</div>
      ${hashes.length ? `<div class="tagline">${hashes.slice(0, 10).map((h) => `<span class="tag">${esc(h)}</span>`).join('')}</div>` : ''}
      ${desc}
      <button class="mini-copy" data-copymeta="${esc(JSON.stringify({ title, tags, hashes, description: m.youtube_description || '' }))}">Copy all</button>`;
  }

  /** Hook mode: 7 hooks + best pick + A/B variant, rendered inside the card's Hooks tab. */
  function hooksBody(clip) {
    const hk = clip.hooks || {};
    const hooks = hk.hooks || [];
    const best = hk.best || {};
    const ab = hk.ab_test || {};
    if (!hooks.length) return '<span class="label">Hooks</span><p class="hint">No hooks were written for this clip.</p>';

    const rows = hooks.map((h, i) => `
      <div class="hook ${best.pick === i + 1 ? 'is-best' : ''}">
        <div class="hook-top">
          <span class="hook-num">${i + 1}</span>
          <span class="hook-pat">${esc(h.pattern)}</span>
          ${best.pick === i + 1 ? '<span class="hook-flag">★ Best</span>' : ''}
          <button class="mini-copy" data-hook="${esc(h.on_screen)}">Copy</button>
        </div>
        <p class="hook-on">${esc(h.on_screen)}</p>
        ${h.spoken && h.spoken !== h.on_screen ? `<p class="hook-spoken"><b>Spoken:</b> ${esc(h.spoken)}</p>` : ''}
        <p class="hook-why">${esc(h.why)}</p>
      </div>`).join('');

    const research = hk.research || {};
    return `
      <span class="label">7 hooks · ${esc(hk.platform || 'short-form')}</span>
      <div class="hook-list">${rows}</div>
      <div class="hook is-pick">
        <div class="hook-top"><span class="hook-flag">★ Best hook</span>
          <button class="mini-copy" data-hook="${esc(best.on_screen || '')}">Copy</button></div>
        <p class="hook-on">${esc(best.on_screen || '')}</p>
        ${best.spoken && best.spoken !== best.on_screen ? `<p class="hook-spoken"><b>Spoken:</b> ${esc(best.spoken)}</p>` : ''}
        <p class="hook-why">${esc(best.reason || '')}</p>
      </div>
      <div class="hook is-ab">
        <div class="hook-top"><span class="hook-flag">A/B test variant</span>
          <button class="mini-copy" data-hook="${esc(ab.on_screen || '')}">Copy</button></div>
        <p class="hook-on">${esc(ab.on_screen || '')}</p>
        ${ab.spoken && ab.spoken !== ab.on_screen ? `<p class="hook-spoken"><b>Spoken:</b> ${esc(ab.spoken)}</p>` : ''}
        ${ab.note ? `<p class="hook-why">${esc(ab.note)}</p>` : ''}
      </div>
      <button class="mini-copy" data-hookall="1">Copy all hooks</button>
      <p class="hook-src">Research: ${esc((research.patterns || []).length)} patterns · ${esc(research.source || 'curated')}${hk.source === 'llm' ? ' · model-written' : ' · local writer (no LLM key)'}</p>`;
  }

  /** Plain-text bundle for the "Copy all hooks" button. */
  function hooksText(clip) {
    const hk = clip.hooks || {};
    const list = (hk.hooks || []).map((h, i) => {
      const spoken = h.spoken && h.spoken !== h.on_screen ? `\n   spoken: ${h.spoken}` : '';
      return `${i + 1}. [${h.pattern}] ${h.on_screen}${spoken}\n   why: ${h.why}`;
    }).join('\n\n');
    const b = hk.best || {}, ab = hk.ab_test || {};
    return `${list}\n\nBEST (#${b.pick || 1}): ${b.on_screen || ''}\n${b.reason || ''}` +
      `\n\nA/B VARIANT: ${ab.on_screen || ''}${ab.note ? '\n' + ab.note : ''}`;
  }

  function clipCard(clip, taskId) {
    const meta = clip.metadata || {};
    const firstTitle = (meta.titles || {}).youtube || `Clip ${clip.index}`;
    const tabs = clip.hooks && (clip.hooks.hooks || []).length
      ? META_TABS.concat([{ key: 'hooks', label: '🪝 Hooks' }])
      : META_TABS;
    const card = document.createElement('article');
    card.className = 'clip-card';
    card.style.animationDelay = (clip.index * 40) + 'ms';
    card.innerHTML = `
      <div class="clip-media">
        <video src="${esc(clip.url)}#t=0.1" poster="${esc(clip.poster_url)}" preload="metadata" muted playsinline controls></video>
        <span class="score">⚡ ${Number(clip.score).toFixed(1)}</span>
        <span class="range">${esc(clip.start_label)} → ${esc(clip.end_label)} · ${Math.round(clip.duration)}s</span>
      </div>
      <div class="clip-body">
        <h4>${esc(firstTitle)}</h4>
        <div class="reasons">${(clip.reasons || []).map((r) => `<span class="reason">${esc(r)}</span>`).join('')}</div>
        <div class="meta-tabs">${tabs.map((t, i) =>
          `<button class="meta-tab ${i === 0 ? 'active' : ''}" data-mtab="${t.key}">${t.label}</button>`).join('')}</div>
        <div class="meta-body" id="meta-${taskId}-${clip.index}">${metaBody(clip, 'youtube')}</div>
        <div class="clip-actions">
          <a class="btn btn-primary btn-sm" href="${esc(clip.url)}" download="${esc(clip.download_name)}">Download</a>
          <button class="btn btn-outline btn-sm" data-edit="1">Edit in AI Video Editor</button>
        </div>
      </div>`;

    $$('.meta-tab', card).forEach((tab) => tab.addEventListener('click', () => {
      $$('.meta-tab', card).forEach((t) => t.classList.toggle('active', t === tab));
      $('.meta-body', card).innerHTML = tab.dataset.mtab === 'hooks'
        ? hooksBody(clip) : metaBody(clip, tab.dataset.mtab);
    }));
    card.addEventListener('click', (e) => {
      const hc = e.target.closest('[data-hook]');
      if (hc) { copyText(hc.dataset.hook, hc); return; }
      const ha = e.target.closest('[data-hookall]');
      if (ha) { copyText(hooksText(clip), ha); return; }
      const cp = e.target.closest('[data-copymeta]');
      if (cp) { const d = JSON.parse(cp.dataset.copymeta); copyText(`${d.title}\n\n${d.tags.join(', ')}\n\n${d.hashes.join(' ')}\n\n${d.description}`, cp); return; }
      if (e.target.closest('[data-edit]')) openEditorForClip(clip, taskId);
    });
    return card;
  }

  function renderClips(result) {
    show($('#clipProgress'), false);
    show($('#clipEmpty'), false);
    const grid = $('#clipGrid');
    grid.hidden = false;
    grid.innerHTML = '';
    (result.clips || []).forEach((clip) => grid.appendChild(clipCard(clip, result.task_id)));
    if (result.llm === 'local-fallback') {
      const note = document.createElement('p');
      note.className = 'hint';
      const hookNote = result.mode === 'hook'
        ? ' Hooks come from the local pattern writer (STEP 1 research built in) — set WV_LLM_API_KEY for model-written hooks.'
        : '';
      note.textContent = 'No LLM key configured — titles, tags and descriptions were generated by the local fallback writer. Set WV_LLM_API_KEY for model-written metadata.' + hookNote;
      grid.appendChild(note);
    }
    grid.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ====================================================== 2. AI VIDEO EDITOR */
  const editorState = { source: '', segments: [], removed: new Set(), taskId: null };

  function openEditorForClip(clip, taskId) {
    editorState.source = clip.url;
    editorState.taskId = taskId;
    editorState.segments = clip.segments || [];
    editorState.lines = segsToLines(clip.segments);
    editorState.removed = new Set();
    $('#editSource').value = clip.url;
    $('#editorTargetLabel').textContent =
      `Editing clip #${clip.index} · ${clip.start_label} → ${clip.end_label} · ${Math.round(clip.duration)}s of the source video.`;
    activate('editor');
    renderTranscript();
    show($('#editResult'), false);
  }

  function segsToLines(segments) {
    return (segments || []).filter((s) => (s.text || '').trim() || (s.words || []).length).map((s, i) => ({
      id: i,
      start: Number(s.start) || 0,
      end: Number(s.end) || 0,
      text: (s.text || '').trim() || (s.words || []).map((w) => w.w).join(' '),
      words: s.words || [],
      orig: (s.text || '').trim() || (s.words || []).map((w) => w.w).join(' '),
    }));
  }

  function renderTranscript() {
    const box = $('#editTranscript');
    const lines = editorState.lines || [];
    show($('#editEmpty'), false);
    show($('#editToolbar'), true);
    box.hidden = false;
    box.innerHTML = lines.map((l) => `
      <div class="tline${editorState.removed.has(l.id) ? ' removed' : ''}" data-line="${l.id}">
        <span class="tstamp">${fmtTime(l.start)}</span>
        <div class="ttext" contenteditable="true" spellcheck="false">${esc(l.text)}</div>
        <button class="tdel" title="Delete this line and the footage it covers" aria-label="Delete line">✕</button>
      </div>`).join('') +
      `<p class="hint">Deleting a line cuts exactly that footage. Typing in a line only changes the burned-in caption — the video keeps rolling.</p>`;

    $$('.tline', box).forEach((row) => {
      const id = Number(row.dataset.line);
      $('.tdel', row).addEventListener('click', () => {
        if (editorState.removed.has(id)) editorState.removed.delete(id); else editorState.removed.add(id);
        row.classList.toggle('removed', editorState.removed.has(id));
        updateCounts();
      });
      $('.ttext', row).addEventListener('input', () => {
        const line = editorState.lines.find((l) => l.id === id);
        if (line) line.text = $('.ttext', row).textContent.trim();
      });
    });
    updateCounts();
  }

  function updateCounts() {
    const lines = editorState.lines || [];
    const kept = lines.filter((l) => !editorState.removed.has(l.id));
    const removed = lines.length - kept.length;
    const total = lines.reduce((a, l) => a + (l.end - l.start), 0);
    const gone = lines.filter((l) => editorState.removed.has(l.id))
      .reduce((a, l) => a + (l.end - l.start), 0);
    $('#editKeptCount').textContent = `${kept.length} lines kept`;
    $('#editRemovedCount').textContent = `${removed} lines removed (−${gone.toFixed(1)}s of ${total.toFixed(1)}s)`;
    $('#editDuration').textContent = `≈ ${Math.max(0, total - gone).toFixed(1)}s output`;
  }

  async function loadTranscript() {
    const src = ($('#editSource').value || '').trim();
    const errBox = $('#editError');
    fail(errBox, '');
    if (!src) return fail(errBox, 'Enter a URL or media path first.');
    const btn = $('#editLoad');
    btn.disabled = true; btn.textContent = 'Transcribing…';
    try {
      const data = await postJSON('/api/editor/transcribe', { source: src });
      editorState.source = src;
      editorState.lines = segsToLines(data.segments);
      editorState.removed = new Set();
      show($('#editResult'), false);
      renderTranscript();
      $('#editDuration').textContent = `source ${fmtTime(data.duration)} · ${data.language || ''}`;
    } catch (e) {
      fail(errBox, e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Load transcript';
    }
  }

  $('#editLoad').addEventListener('click', loadTranscript);

  $('#editSilence').addEventListener('click', async () => {
    const src = ($('#editSource').value || '').trim();
    const errBox = $('#editError');
    fail(errBox, '');
    if (!src) return fail(errBox, 'Enter a URL or media path first.');
    const btn = $('#editSilence');
    btn.disabled = true; btn.textContent = 'Scanning…';
    try {
      const data = await postJSON('/api/editor/detect-silence', { source: src });
      const info = $('#silenceInfo');
      show(info, true);
      info.innerHTML = `<strong>${data.silences.length} silent gap${data.silences.length === 1 ? '' : 's'}</strong>
        (< ${data.threshold_db} dB for > ${data.min_duration}s) — ${data.removed_seconds}s of dead air across
        ${fmtTime(data.duration)}. First gaps: ${data.silences.slice(0, 4).map((s) => fmtTime(s.start)).join(', ') || 'none'}.
        Tick “Remove silence” to cut them on render.`;
    } catch (e) {
      fail(errBox, e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Detect silence';
    }
  });

  $('#editReset').addEventListener('click', () => {
    if (editorState.lines) {
      editorState.lines.forEach((l) => { l.text = l.orig; });
      editorState.removed = new Set();
      renderTranscript();
    }
  });

  /** Kept footage ranges: each surviving line contributes its own span, and
   * adjacent kept lines are merged when the gap holds no deleted line (so a
   * normal pause survives but deleted footage never does). */
  function keptSpans(lines, removed) {
    const spans = [];
    lines.forEach((l) => {
      if (removed.has(l.id)) return;
      const prev = spans[spans.length - 1];
      if (!prev) { spans.push({ s: l.start, e: l.end }); return; }
      const gapLines = lines.filter((x) => x.end > prev.e && x.start < l.start);
      const gapHoldsRemoval = gapLines.some((x) => removed.has(x.id));
      if (!gapHoldsRemoval && l.start - prev.e <= 0.6) prev.e = l.end;
      else spans.push({ s: l.start, e: l.end });
    });
    return spans.map((s) => [s.s, s.e]);
  }

  async function renderFinal() {
    const errBox = $('#editError');
    fail(errBox, '');
    if (!editorState.source) return fail(errBox, 'Load a transcript first.');
    const keptLines = editorState.lines.filter((l) => !editorState.removed.has(l.id));
    if (!keptLines.length) return fail(errBox, 'Every line is deleted — keep at least one.');

    const btn = $('#editRender');
    btn.disabled = true; btn.textContent = 'Rendering…';
    try {
      const body = {
        source: editorState.source,
        kept_text: keptLines.map((l) => l.orig).join(' '),
        spans: keptSpans(editorState.lines, editorState.removed),
        remove_silence: $('#editRemoveSilence').checked,
        filename: 'wayin_edit.mp4',
      };
      if ($('#editBurn').checked) {
        body.style = clipState.editStyle || {};
        body.caption_lines = keptLines.map((l) => ({
          // edited text drives the caption; words only exist for untouched lines
          start: l.start, end: l.end,
          text: l.text,
          words: l.text === l.orig ? l.words : [],
        }));
      }
      const data = await postJSON('/api/clips/edit-clip', body);
      show($('#editResult'), true);
      $('#editVideo').src = data.url;
      const dl = $('#editDownload');
      dl.href = data.url;
      $('#editCopyPath').dataset.path = data.url;
      $('#editDuration').textContent =
        `output ${fmtTime(data.duration)} · ${data.spans.length} segment(s) kept` +
        (data.removed_silence ? ` · ${data.removed_silence}s silence removed` : '');
      $('#editResult').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) {
      fail(errBox, e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Render final video';
    }
  }

  $('#editRender').addEventListener('click', renderFinal);
  $('#editCopyPath').addEventListener('click', (e) => copyText(e.currentTarget.dataset.path || '', e.currentTarget));

  /* ======================================================== 3. SUMMARIZER == */
  const sumState = { options: { length: 'detailed', formats: { chapters: true, actions: true, moments: true } }, taskId: null, summary: null };
  const sumProgress = progressUI({ box: '#sumProgress', bar: '#sumBar', pct: '#sumPct', stage: '#sumStage', steps: '#sumSteps' });

  bindChips(document, 'data-sum', (key, value) => { if (key === 'length') sumState.options.length = value; });

  async function runSummary() {
    const errBox = $('#sumError');
    fail(errBox, '');
    const source = ($('#sumSource').value || '').trim();
    if (!source) return fail(errBox, 'Paste a video URL or file path first.');
    $$('[data-fmt]').forEach((c) => { sumState.options.formats[c.dataset.fmt] = c.checked; });

    const btn = $('#sumRun');
    btn.disabled = true; btn.textContent = 'Working…';
    show($('#sumEmpty'), true); show($('#sumResult'), false);
    sumProgress.start();
    try {
      const data = await postJSON('/api/summarizer/process', { source, options: sumState.options });
      sumState.taskId = data.task_id;
      const result = await poll(`/api/summarizer/status/${data.task_id}`, sumProgress);
      renderSummary(result);
    } catch (e) {
      fail(errBox, e.message);
      sumProgress.error(e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Generate summary';
    }
  }

  $('#sumRun').addEventListener('click', runSummary);

  function tsBtn(sec, label) {
    return `<button class="ts-btn" data-seek="${Number(sec)}">${fmtTime(sec)}</button>${esc(label)}`;
  }

  function renderSummary(result) {
    sumState.summary = result;
    show($('#sumProgress'), false);
    show($('#sumEmpty'), false);
    show($('#sumResult'), true);

    const s = result.summary || {};
    const video = $('#sumVideo');
    if (result.video_url) { video.src = result.video_url; show(video.parentElement, true); }
    else show(video.parentElement, false);

    $('#sumTitle').textContent = `${s.title || result.title || 'Summary'} · ${fmtTime(result.duration || 0)}`;
    $('#sumOverview').textContent = s.overview || '';

    const blocks = [];
    if ((s.chapters || []).length) {
      blocks.push(`<div class="sum-block"><h4>Chapters</h4><ul>${s.chapters.map((c) =>
        `<li>${tsBtn(c.start, c.title)}${c.summary ? `<div class="muted">${esc(c.summary)}</div>` : ''}</li>`).join('')}</ul></div>`);
    }
    if ((s.key_points || []).length) {
      blocks.push(`<div class="sum-block"><h4>Key points</h4><ul>${s.key_points.map((p) =>
        `<li>${esc(p)}</li>`).join('')}</ul></div>`);
    }
    if ((s.action_items || []).length) {
      blocks.push(`<div class="sum-block"><h4>Action items</h4><ul>${s.action_items.map((p) =>
        `<li>${esc(p)}</li>`).join('')}</ul></div>`);
    }
    if ((s.best_moments || []).length) {
      blocks.push(`<div class="sum-block"><h4>Best moments / highlight reel</h4><ul>${s.best_moments.map((m) =>
        `<li>${tsBtn(m.start, m.label || 'moment')}${m.why ? `<div class="muted">${esc(m.why)}</div>` : ''}</li>`).join('')}</ul></div>`);
    }
    if ((s.keywords || []).length) {
      blocks.push(`<div class="sum-block"><h4>Keywords</h4><div class="kw-row">${s.keywords.map((k) =>
        `<span class="kw">${esc(k)}</span>`).join('')}</div></div>`);
    }
    if (result.llm === 'local-fallback') {
      blocks.push(`<p class="hint">No LLM key configured — this summary was built by the offline extractive writer. Set WV_LLM_API_KEY for model-written prose.</p>`);
    }
    $('#sumSections').innerHTML = blocks.join('');

    $$('#sumSections .ts-btn').forEach((b) => b.addEventListener('click', () => {
      const t = Number(b.dataset.seek);
      if (video.src) { video.currentTime = t; video.play().catch(() => {}); }
      video.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }));

    $('#sumResult').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  $('#sumExports').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-export]');
    if (btn) {
      btn.disabled = true;
      try {
        await download('/api/summarizer/export',
          { task_id: sumState.taskId, format: btn.dataset.export },
          `summary.${btn.dataset.export}`);
      } catch (err) { alert('Export failed: ' + err.message); }
      btn.disabled = false;
      return;
    }
    const cp = e.target.closest('[data-copy]');
    if (cp && sumState.summary) {
      const md = summaryAsText(sumState.summary);
      copyText(md, cp);
    }
  });

  function summaryAsText(result) {
    const s = result.summary || {};
    const out = [`# ${s.title || result.title || 'Summary'}`, '', s.overview || '', ''];
    if ((s.chapters || []).length) out.push('## Chapters', ...s.chapters.map((c) => `- [${fmtTime(c.start)}] ${c.title} — ${c.summary || ''}`), '');
    if ((s.key_points || []).length) out.push('## Key points', ...s.key_points.map((p) => `- ${p}`), '');
    if ((s.action_items || []).length) out.push('## Action items', ...s.action_items.map((p) => `- [ ] ${p}`), '');
    if ((s.best_moments || []).length) out.push('## Best moments', ...s.best_moments.map((m) => `- [${fmtTime(m.start)}] ${m.label || ''}`), '');
    if ((s.keywords || []).length) out.push('## Keywords', s.keywords.join(', '), '');
    return out.join('\n');
  }

  /* ====================================================== 4. VIDEO TO TEXT = */
  const v2tState = { type: 'text-to-video', taskId: null, result: null };
  const v2tProgress = progressUI({ box: '#v2tProgress', bar: '#v2tBar', pct: '#v2tPct', stage: '#v2tStage', steps: '#v2tSteps' });

  bindChips(document, 'data-v2t', (key, value) => { if (key === 'type') v2tState.type = value; });
  const interval = $('#v2tInterval');
  if (interval) interval.addEventListener('input', () => { $('#v2tIntervalVal').textContent = interval.value; });

  async function runV2T() {
    const errBox = $('#v2tError');
    fail(errBox, '');
    const source = ($('#v2tSource').value || '').trim();
    if (!source) return fail(errBox, 'Paste a video URL or file path first.');
    const btn = $('#v2tRun');
    btn.disabled = true; btn.textContent = 'Analysing…';
    show($('#v2tEmpty'), true); show($('#v2tResult'), false);
    v2tProgress.start();
    try {
      const data = await postJSON('/api/video-to-text/process', {
        source,
        prompt_type: v2tState.type,
        frame_interval: Number(interval.value),
        scene_detect: $('#v2tScenes').checked,
      });
      v2tState.taskId = data.task_id;
      const result = await poll(`/api/video-to-text/status/${data.task_id}`, v2tProgress);
      v2tState.result = result;
      renderV2T(result);
    } catch (e) {
      fail(errBox, e.message);
      v2tProgress.error(e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Extract text & visuals';
    }
  }

  $('#v2tRun').addEventListener('click', runV2T);

  function renderV2T(result) {
    show($('#v2tProgress'), false);
    show($('#v2tEmpty'), false);
    const box = $('#v2tResult');
    box.hidden = false;

    const b = result.visual_breakdown || {};
    const p = result.prompt || {};
    const frames = Array.from({ length: Math.min(result.frame_count || 0, 12) },
      (_, i) => `/media/${result.task_id}/frames/frame_${String(i).padStart(3, '0')}.jpg`);

    const scenes = (b.scenes || []).map((s) =>
      `<li><b>${fmtTime(s.t)}</b> — ${esc(s.shot || '')} ${esc(s.subject ? '| ' + s.subject : '')}
        ${esc(s.camera ? '| camera: ' + s.camera : '')} ${esc(s.lighting ? '| light: ' + s.lighting : '')}</li>`).join('');

    box.innerHTML = `
      <div class="kv-grid">
        <div class="kv"><b>Title</b><span>${esc(result.title)}</span></div>
        <div class="kv"><b>Duration</b><span>${fmtTime(result.duration)}</span></div>
        <div class="kv"><b>Frames analysed</b><span>${result.frame_count}</span></div>
        <div class="kv"><b>Analysis method</b><span>${esc(b.method || 'n/a')}</span></div>
      </div>
      ${frames.length ? `<div class="frame-strip">${frames.map((f) => `<img src="${f}" alt="frame" loading="lazy">`).join('')}</div>` : ''}
      <div class="copy-block">
        <div class="copy-head"><span>Technical breakdown</span>
          <button class="btn btn-ghost btn-sm" data-copyel="breakdown">Copy</button></div>
        <div class="body" id="breakdown">${esc(JSON.stringify(
          { lighting: b.lighting, palette: b.palette, brightness: b.average_brightness,
            saturation: b.average_saturation, estimated_cuts: b.estimated_cuts,
            motion: b.motion, scenes: b.scenes }, null, 2))}</div>
      </div>
      ${scenes ? `<div class="copy-block"><div class="copy-head"><span>Scene list</span></div><div class="body">${scenes}</div></div>` : ''}
      <div class="copy-block">
        <div class="copy-head"><span>Generated prompt (${esc(result.prompt_type)})</span>
          <span>
            <button class="btn btn-ghost btn-sm" data-regen="1">Re-write</button>
            <button class="btn btn-ghost btn-sm" data-copyel="prompt">Copy</button>
          </span></div>
        <pre id="prompt">${esc(p.prompt || p.text_to_video || '')}</pre>
      </div>
      <div class="copy-block">
        <div class="copy-head"><span>Shot list</span>
          <button class="btn btn-ghost btn-sm" data-copyel="shots">Copy</button></div>
        <pre id="shots">${esc((p.shot_list || []).join('\n'))}</pre>
      </div>
      <div class="copy-block">
        <div class="copy-head"><span>Transcript</span>
          <button class="btn btn-ghost btn-sm" data-copyel="transcript">Copy</button></div>
        <pre id="transcript">${esc((result.transcript || {}).text || '')}</pre>
      </div>`;

    box.onclick = async (e) => {
      const cp = e.target.closest('[data-copyel]');
      if (cp) {
        const el = $('#' + cp.dataset.copyel);
        copyText(el ? (el.value || el.textContent) : '', cp);
        return;
      }
      if (e.target.closest('[data-regen]')) {
        const btn = e.target.closest('[data-regen]');
        btn.disabled = true; btn.textContent = 'Writing…';
        try {
          const data = await postJSON('/api/video-to-text/prompt',
            { task_id: v2tState.taskId, prompt_type: v2tState.type });
          v2tState.result.prompt = data.prompt;
          renderV2T(v2tState.result);
        } catch (err) { alert(err.message); btn.disabled = false; }
      }
    };
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* =========================================================== 5. NOTES ==== */
  const notesState = { task: 'blog', taskId: null, result: null };
  const notesProgress = progressUI({ box: '#notesProgress', bar: '#notesBar', pct: '#notesPct', stage: '#notesStage', steps: '#notesSteps' });

  bindChips(document, 'data-prompt', (key, value) => { if (key === 'task') notesState.task = value; });

  async function runNotes() {
    const errBox = $('#notesError');
    fail(errBox, '');
    const source = ($('#notesSource').value || '').trim();
    if (!source) return fail(errBox, 'Paste a video or audio URL first.');
    const btn = $('#notesRun');
    btn.disabled = true; btn.textContent = 'Working…';
    show($('#notesEmpty'), true); show($('#notesResult'), false);
    notesProgress.start();
    try {
      const data = await postJSON('/api/notes/process', {
        source,
        speakers: $('#notesSpeakers').checked,
        technical: $('#notesTechnical').checked,
      });
      notesState.taskId = data.task_id;
      const result = await poll(`/api/notes/status/${data.task_id}`, notesProgress);
      notesState.result = result;
      renderNotes(result);
    } catch (e) {
      fail(errBox, e.message);
      notesProgress.error(e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Take notes';
    }
  }

  $('#notesRun').addEventListener('click', runNotes);

  function listBlock(title, items) {
    if (!items || !items.length) return '';
    return `<div class="sum-block"><h4>${esc(title)}</h4><ul>${items.map((i) =>
      `<li>${typeof i === 'string' ? esc(i) : esc(i.term || '')}${i.definition ? ` — ${esc(i.definition)}` : ''}</li>`).join('')}</ul></div>`;
  }

  function renderNotes(result) {
    show($('#notesProgress'), false);
    show($('#notesEmpty'), false);
    show($('#notesResult'), true);
    const n = result.notes || {};

    $('#notesPane').innerHTML = `
      <div class="kv-grid">
        <div class="kv"><b>Title</b><span>${esc(result.title)}</span></div>
        <div class="kv"><b>Duration</b><span>${fmtTime(result.duration)}</span></div>
        <div class="kv"><b>Language</b><span>${esc(result.language || 'auto')}</span></div>
        <div class="kv"><b>Speakers</b><span>${new Set((result.transcript.segments || []).map((s) => s.speaker).filter(Boolean)).size || '—'}</span></div>
      </div>
      ${n.summary ? `<div class="sum-overview">${esc(n.summary)}</div>` : ''}
      <div class="sum-sections">
        ${listBlock('Key points', n.key_points)}
        ${listBlock('Action items', n.action_items)}
        ${listBlock('Decisions', n.decisions)}
        ${listBlock('Open questions', n.open_questions)}
        ${listBlock('Glossary', n.glossary)}
      </div>
      ${n._fallback ? '<p class="hint">No LLM key configured — notes come from the offline writer. Set WV_LLM_API_KEY for model-written notes.</p>' : ''}`;

    const t = result.technical;
    $('#techPane').innerHTML = t ? `
      <div class="kv-grid">
        <div class="kv"><b>Tools</b><span>${esc((t.tools || []).join(', ') || '—')}</span></div>
        <div class="kv"><b>Extraction</b><span>${esc(t.method || 'regex')}</span></div>
        <div class="kv"><b>Links</b><span>${(t.links || []).length}</span></div>
        <div class="kv"><b>Paths</b><span>${(t.paths || []).length}</span></div>
      </div>
      ${(t.commands || []).length ? `<div class="copy-block"><div class="copy-head"><span>Commands</span>
        <button class="btn btn-ghost btn-sm" data-copyel="cmds">Copy</button></div><pre id="cmds">${esc((t.commands || []).join('\n'))}</pre></div>` : ''}
      ${(t.steps || []).length ? listBlock('Steps', t.steps) : ''}
      ${(t.terms || []).length ? listBlock('Terminology', t.terms) : ''}
      ${(t.code || []).length ? `<div class="copy-block"><div class="copy-head"><span>Code</span></div><pre>${esc((t.code || []).map((c) => c.snippet).join('\n\n'))}</pre></div>` : ''}`
      : '<p class="hint">Technical mode was off for this run.</p>';

    const segs = (result.transcript || {}).segments || [];
    const drawTranscript = (filter = '') => {
      const q = filter.trim().toLowerCase();
      $('#transcriptLines').innerHTML = segs
        .filter((s) => !q || (s.text || '').toLowerCase().includes(q) || (s.speaker || '').toLowerCase().includes(q))
        .map((s) => {
          let text = esc(s.text || '');
          if (q) {
            const re = new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'ig');
            text = text.replace(re, '<mark>$1</mark>');
          }
          return `<div class="trow"><span class="spk">${esc(s.speaker || fmtTime(s.start))}</span>
            <span class="tstamp">${fmtTime(s.start)}</span><span>${text}</span></div>`;
        }).join('') || '<p class="hint">No matches.</p>';
    };
    drawTranscript();
    $('#transcriptSearch').oninput = (e) => drawTranscript(e.target.value);

    $('#promptText').textContent = (result.prompt || {}).prompt || '';
    show($('#promptOut'), !!result.prompt);
    $('#notesResult').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  $('#notesTabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.sub-tab');
    if (!tab) return;
    $$('.sub-tab', $('#notesTabs')).forEach((t) => t.classList.toggle('active', t === tab));
    $$('.sub-pane').forEach((p) => p.classList.toggle('active', p.dataset.npane === tab.dataset.ntab));
  });

  $('#promptRun').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    if (!notesState.taskId) return alert('Run Note Taker first.');
    btn.disabled = true; btn.textContent = 'Writing…';
    try {
      const data = await postJSON('/api/notes/prompt', {
        task_id: notesState.taskId,
        task: notesState.task,
        extra: $('#promptExtra').value.trim(),
      });
      notesState.result.prompt = data.prompt.prompt;
      $('#promptText').textContent = data.prompt.prompt || '';
      show($('#promptOut'), true);
    } catch (err) { alert(err.message); }
    finally { btn.disabled = false; btn.textContent = 'Generate prompt'; }
  });

  $('#notesResult').addEventListener('click', async (e) => {
    const exp = e.target.closest('[data-nexport]');
    if (exp) {
      exp.disabled = true;
      try {
        await download('/api/notes/export',
          { task_id: notesState.taskId, format: exp.dataset.nexport },
          `notes.${exp.dataset.nexport}`);
      } catch (err) { alert('Export failed: ' + err.message); }
      exp.disabled = false;
      return;
    }
    const cp = e.target.closest('[data-ncopy]');
    if (cp && notesState.result) {
      const n = notesState.result.notes || {};
      copyText([notesState.result.title, n.summary || '',
        ...(n.key_points || []).map((p) => '• ' + p),
        ...(n.action_items || []).map((p) => '- [ ] ' + p)].join('\n'), cp);
      return;
    }
    const cel = e.target.closest('[data-copyel]');
    if (cel) {
      const el = $('#' + cel.dataset.copyel);
      copyText(el ? (el.value || el.textContent) : '', cel);
    }
  });

  /* ================================================================ boot == */
  activate(location.hash.slice(1) || 'clips', false);
  paintPreview();
  paintEditStyle();
  probeBackend();
})();