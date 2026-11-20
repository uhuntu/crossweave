const $ = id => document.getElementById(id);
const state = {
  mode: 'sessions',
  rows: [], selected: null, info: null,
  wiki: { configured: false, online: false, path: '', name: '', pages: 0 },
  wikiPages: [], wikiSelected: null, wikiInfo: null, collapsedCats: new Set(),
  busy: false, version: 0, initHomeApplied: false,
};
const token = document.querySelector('meta[name="cw-token"]').content;
const initialHome = (document.querySelector('meta[name="cw-home"]')?.content || '').trim();

function status(text, error = false) {
  $('status').textContent = text;
  $('status').classList.toggle('error', error);
}

async function api(url, opts) {
  const res = await fetch(url, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || '请求失败');
  return data;
}

function postJson(url, body) {
  return api(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CW-Token': token },
    body: JSON.stringify(body),
  });
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function shortPath(cwd) {
  if (!cwd) return '未知目录';
  const parts = cwd.split(/[\\/]/).filter(Boolean);
  return parts.pop() || cwd;
}

function homeLabel(path) {
  if (!path) return '本机';
  const parts = path.split(/[\\/]/).filter(Boolean);
  if (parts.length >= 3 && parts[parts.length - 2].toLowerCase() === 'users') {
    return `${parts[parts.length - 3]} · ${parts[parts.length - 1]}`;
  }
  return parts.slice(-2).join('/') || path;
}

function homeBadgeText(path) {
  const parts = path.split(/[\\/]/).filter(Boolean);
  if (parts.length >= 3 && parts[parts.length - 2].toLowerCase() === 'users') {
    return parts[parts.length - 3];
  }
  return parts.pop() || 'ext';
}

function filtered() {
  const words = $('search').value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const toolVal = $('tool').value;
  const homeVal = $('home').value;
  return state.rows.filter(r => {
    if (toolVal && r.tool !== toolVal) return false;
    if (homeVal === '__local__' && r.extra_home) return false;
    if (homeVal && homeVal !== '__local__' && r.extra_home !== homeVal) return false;
    const hay = `${r.title} ${r.cwd} ${r.id} ${r.extra_home || ''}`.toLowerCase();
    return words.every(w => hay.includes(w));
  });
}

function drawList() {
  const rows = filtered();
  $('count').textContent = `${rows.length} 个会话`;
  $('sessions').replaceChildren();
  for (const r of rows) {
    const active = state.selected?.id === r.id && state.selected?.tool === r.tool;
    const b = el('button', 'session' + (active ? ' active' : ''));
    b.disabled = state.busy;
    b.setAttribute('aria-pressed', String(active));
    b.title = `${r.title}\n${r.cwd}${r.extra_home ? '\n来源: ' + r.extra_home : ''}`;
    b.append(el('div', 'session-title', r.title || '未命名会话'));
    const meta = el('div', 'session-meta');
    meta.append(el('span', 'tool-badge', r.tool));
    if (r.extra_home) meta.append(el('span', 'home-badge', homeBadgeText(r.extra_home)));
    meta.append(el('span', 'session-path', shortPath(r.cwd)), el('span', 'session-age', r.age));
    b.append(meta);
    b.onclick = () => select(r);
    $('sessions').append(b);
  }
  if (!rows.length) $('sessions').append(el('p', 'list-empty', '没有匹配的会话。试试其他关键词。'));
}

function composer() {
  const isSession = state.mode === 'sessions';
  const isWiki = state.mode === 'wiki';
  $('composer').hidden = !isSession;
  $('session-card').hidden = !isSession;
  $('wiki-links-card').hidden = !isWiki;
  const ok = isSession && !!state.info?.can_chat && !state.busy;
  $('message').disabled = !ok;
  $('send').disabled = !ok || !$('message').value.trim();
  $('export').hidden = state.mode === 'graph';
  $('export').disabled = isWiki ? !state.wikiInfo : (!state.info || state.busy);
  $('export-wiki').hidden = !isSession || !state.wiki.configured;
  $('export-wiki').disabled = !isSession || !state.info || state.busy;
  $('rename').hidden = !isSession;
  $('rename').disabled = !isSession || !state.info?.can_rename || state.busy;
  $('rename').title = state.info?.extra_home ? '外部目录会话为只读，暂不支持重命名' : '';
  $('delete').hidden = !isSession;
  $('delete').disabled = !isSession || !state.info?.can_delete || state.busy;
  $('copy').disabled = !state.info;
  $('copy-resume').disabled = !state.info;
  $('refresh').disabled = state.busy;
  $('message').placeholder = !state.info
    ? '选择左侧会话后可继续对话…'
    : state.info.can_chat
      ? '继续这段对话…（Ctrl / ⌘ + Enter 发送）'
      : state.info.extra_home
        ? '外部目录会话仅供查看；请在终端用 cw resume 自动 handoff 续接'
        : '该会话仅供查看；工具暂不支持聊天或未安装 CLI';
  $('compose-hint').textContent = state.busy
    ? '正在处理，请等待本轮完成…'
    : state.info?.can_chat
      ? '在原会话目录运行 · 可调用工具和修改文件'
      : '保留各工具权限设置 · 暂不支持网页交互审批';
}

function resolveWikiTarget(target) {
  const clean = target.replace(/\.md$/i, '').trim().toLowerCase();
  return state.wikiPages.find(p =>
    p.rel.replace(/\.md$/i, '').toLowerCase() === clean
    || p.stem.toLowerCase() === clean
    || p.title.toLowerCase() === clean
  );
}

function appendInline(parent, text) {
  const re = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[\[[^\]\n]+\]\])/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.append(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (tok.startsWith('`')) {
      parent.append(el('code', 'inline-code', tok.slice(1, -1)));
    } else if (tok.startsWith('[[')) {
      const inner = tok.slice(2, -2);
      const [rawTarget, rawLabel] = inner.split('|');
      const target = (rawTarget || '').trim();
      const label = (rawLabel || target).trim();
      const btn = el('button', 'wikilink', label);
      btn.type = 'button';
      btn.title = target;
      btn.onclick = () => openWikiLink(target);
      parent.append(btn);
    } else {
      parent.append(el('strong', null, tok.slice(2, -2)));
    }
    last = re.lastIndex;
  }
  if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
}

function renderProse(container, chunk) {
  const lines = chunk.split('\n');
  let list = null;
  let listTag = null;
  let para = [];

  const flushPara = () => {
    if (!para.length) return;
    const p = el('p', 'md-p');
    para.forEach((line, idx) => {
      if (idx > 0) p.append(el('br'));
      appendInline(p, line);
    });
    container.append(p);
    para = [];
  };
  const flushList = () => {
    if (list) {
      container.append(list);
      list = null;
      listTag = null;
    }
  };

  for (const line of lines) {
    const h = line.match(/^(#{1,3})\s+(.+)$/);
    if (h) {
      flushPara();
      flushList();
      const head = el('div', `md-h md-h${h[1].length}`);
      appendInline(head, h[2]);
      container.append(head);
      continue;
    }
    const ul = line.match(/^\s*[-*]\s+(.+)$/);
    const ol = !ul && line.match(/^\s*\d+\.\s+(.+)$/);
    if (ul || ol) {
      flushPara();
      const tag = ul ? 'ul' : 'ol';
      if (listTag !== tag) {
        flushList();
        list = el(tag, 'md-list');
        listTag = tag;
      }
      const li = el('li');
      appendInline(li, (ul || ol)[1]);
      list.append(li);
      continue;
    }
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
}

function renderMarkdown(container, text) {
  container.replaceChildren();
  const fence = /```([^\n`]*)\n([\s\S]*?)```/g;
  let last = 0;
  let m;
  while ((m = fence.exec(text)) !== null) {
    if (m.index > last) renderProse(container, text.slice(last, m.index));
    const lang = m[1].trim();
    const code = m[2].replace(/\n$/, '');
    const wrap = el('div', 'code-block');
    const head = el('div', 'code-head');
    head.append(el('span', null, lang || 'code'));
    const btn = el('button', 'code-copy', '复制');
    btn.type = 'button';
    btn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(code);
        btn.textContent = '已复制';
        setTimeout(() => { btn.textContent = '复制'; }, 1200);
      } catch (e) {
        status('复制代码失败', true);
      }
    };
    head.append(btn);
    const pre = el('pre');
    pre.append(el('code', null, code));
    wrap.append(head, pre);
    container.append(wrap);
    last = fence.lastIndex;
  }
  if (last < text.length) renderProse(container, text.slice(last));
}

function bubble(role, text, markdown = true) {
  const box = el('article', 'message ' + role);
  const label = el('div', 'message-label');
  label.append(
    el('span', 'avatar', role === 'user' ? '你' : '✳'),
    document.createTextNode(role === 'user' ? 'YOU' : (state.selected?.tool || 'ASSISTANT').toUpperCase())
  );
  const body = el('div', 'message-body' + (markdown ? ' md' : ''));
  if (markdown && text) renderMarkdown(body, text);
  else body.textContent = text;
  box.append(label, body);
  $('thread').append(box);
  return body;
}

function systemBubble(text) {
  const details = document.createElement('details');
  details.className = 'system-msg';
  const summary = el('summary', 'system-msg-head');
  const firstLine = (text || '').split('\n').map(s => s.replace(/^#+\s*/, '').trim()).find(Boolean) || '';
  const preview = firstLine.length > 56 ? firstLine.slice(0, 55) + '…' : firstLine;
  summary.append(
    el('span', 'system-msg-tag', '⚙ 系统上下文'),
    el('span', 'system-msg-preview', preview),
    el('span', 'system-msg-toggle', '展开')
  );
  const body = el('div', 'system-msg-body message-body md');
  renderMarkdown(body, text);
  details.addEventListener('toggle', () => {
    summary.lastChild.textContent = details.open ? '收起' : '展开';
  });
  details.append(summary, body);
  $('thread').append(details);
  return details;
}

function paintConversation(info) {
  $('thread').replaceChildren();
  if (info.messages) {
    for (const m of info.messages) {
      if (m.system) systemBubble(m.text);
      else bubble(m.role, m.text, true);
    }
    if (!info.messages.length) $('thread').append(el('p', 'list-empty', '这个会话暂无文本消息。'));
  } else {
    $('thread').append(el('pre', 'transcript', info.markdown));
  }
}

function resetSelection() {
  state.selected = null;
  state.info = null;
  $('eyebrow').textContent = 'CROSSWEAVE WORKSPACE';
  $('title').textContent = '让每段对话接得上。';
  $('subtitle').textContent = '选择一个会话，查看原文或继续工作。';
  $('breadcrumb').textContent = '会话';
  $('info-tool').textContent = '—';
  $('info-cwd').textContent = '—';
  $('info-id').textContent = '—';
  $('info-home-label').hidden = true;
  $('info-home').hidden = true;
  $('resume-command').textContent = 'cw resume <工具> <会话ID>';
  $('show-command').textContent = 'cw show <工具> <会话ID>';
  $('thread').replaceChildren(el('p', 'list-empty', '请选择左侧会话。'));
  composer();
}

async function select(row) {
  if (state.busy) return;
  const version = ++state.version;
  state.selected = row;
  state.info = null;
  drawList();
  composer();
  $('title').textContent = row.title || '未命名会话';
  $('subtitle').textContent = row.cwd || '未知目录';
  $('eyebrow').textContent = row.tool.toUpperCase() + ' / CONVERSATION';
  $('breadcrumb').textContent = row.tool;
  $('info-tool').textContent = row.tool;
  $('info-cwd').textContent = row.cwd || '—';
  $('info-id').textContent = row.id;
  const hasHome = !!row.extra_home;
  $('info-home-label').hidden = !hasHome;
  $('info-home').hidden = !hasHome;
  $('info-home').textContent = row.extra_home || '—';
  $('resume-command').textContent = `cw resume ${row.tool} ${row.id}`;
  $('show-command').textContent = `cw show ${row.tool} ${row.id}`;
  $('thread').replaceChildren(el('p', 'list-empty', '正在读取原文…'));
  status('读取会话…');
  try {
    const info = await api(`/api/conversation?${new URLSearchParams({ tool: row.tool, id: row.id })}`);
    if (version !== state.version) return;
    state.info = info;
    paintConversation(info);
    status('原文已加载 · 不含思考过程和工具调用输出');
  } catch (e) {
    if (version === state.version) {
      $('thread').replaceChildren(el('p', 'list-empty', e.message));
      status(e.message, true);
    }
  } finally {
    if (version === state.version) composer();
  }
}

function populateHomes(rows) {
  const extras = [...new Set(rows.map(r => r.extra_home).filter(Boolean))].sort();
  const sel = $('home');
  if (!extras.length) {
    sel.hidden = true;
    sel.value = '';
    return;
  }
  const previous = sel.value;
  sel.replaceChildren(el('option', null, '全部来源'));
  sel.firstChild.value = '';
  const localOpt = el('option', null, '本机');
  localOpt.value = '__local__';
  sel.append(localOpt);
  for (const h of extras) {
    const opt = el('option', null, homeLabel(h));
    opt.value = h;
    opt.title = h;
    sel.append(opt);
  }
  sel.hidden = false;
  if (!state.initHomeApplied && initialHome) {
    state.initHomeApplied = true;
    const low = initialHome.toLowerCase();
    if (low === 'local' || low === 'primary' || low === '~') {
      sel.value = '__local__';
    } else {
      const match = extras.find(h => h.toLowerCase() === low || h.toLowerCase().includes(low));
      if (match) sel.value = match;
    }
  } else if ([...sel.options].some(o => o.value === previous)) {
    sel.value = previous;
  }
}

async function load() {
  status('正在读取本地会话…');
  $('refresh').disabled = true;
  try {
    state.rows = await api('/api/sessions');
    const previous = $('tool').value;
    $('tool').replaceChildren(el('option', null, '所有工具'));
    $('tool').firstChild.value = '';
    for (const tool of [...new Set(state.rows.map(r => r.tool))].sort()) {
      const option = el('option', null, tool);
      option.value = tool;
      $('tool').append(option);
    }
    $('tool').value = previous;
    populateHomes(state.rows);
    drawList();
    status(`已读取 ${state.rows.length} 个会话`);
  } catch (e) {
    status(e.message, true);
    $('sessions').replaceChildren(el('p', 'list-empty', '读取失败，请点击刷新重试。'));
  } finally {
    $('refresh').disabled = state.busy;
  }
}

function paintWikiStatus() {
  const w = state.wiki;
  $('tab-wiki').hidden = !w.configured;
  $('tab-graph').hidden = !w.configured;
  $('rail-wiki').hidden = !w.configured;
  $('rail-graph').hidden = !w.configured;
  $('wiki-pill').hidden = !w.configured;
  if (w.configured) {
    $('tab-wiki').textContent = w.pages ? `Wiki (${w.pages})` : 'Wiki';
    $('wiki-pill').classList.toggle('online', !!w.online);
    $('wiki-pill-text').textContent = w.online ? `LLM Wiki · 在线` : `LLM Wiki · 离线`;
    $('wiki-pill').title = w.online
      ? `${w.path}\n点击触发 LLM Wiki 重新扫描`
      : `${w.path}\nLLM Wiki 应用未启动（仍可直接浏览和导出快照）`;
  }
  composer();
}

async function loadWikiStatus() {
  try {
    state.wiki = await api('/api/wiki/status');
  } catch (e) {
    state.wiki = { configured: false, online: false, path: '', name: '', pages: 0 };
  }
  paintWikiStatus();
}

function filteredWikiPages() {
  const words = $('search').value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const cat = $('tool').value;
  return state.wikiPages.filter(p => {
    if (cat && p.category !== cat) return false;
    const hay = `${p.title} ${p.rel} ${p.category}`.toLowerCase();
    return words.every(w => hay.includes(w));
  });
}

function catLabel(cat) {
  if (!cat) return 'Overview';
  return cat.charAt(0).toUpperCase() + cat.slice(1);
}

function drawWikiList() {
  const pages = filteredWikiPages();
  const searching = !!$('search').value.trim();
  $('count').textContent = `${pages.length} 个页面`;
  $('sessions').replaceChildren();
  if (!pages.length) {
    $('sessions').append(el('p', 'list-empty', '没有匹配的 Wiki 页面。'));
    return;
  }
  const groups = new Map();
  for (const p of pages) {
    const key = p.category || '';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  }
  const order = ['', 'entities', 'concepts', 'projects', 'queries', 'sources'];
  const keys = [...groups.keys()].sort((a, b) => {
    const ia = order.indexOf(a.toLowerCase());
    const ib = order.indexOf(b.toLowerCase());
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return a.localeCompare(b);
  });
  for (const key of keys) {
    const list = groups.get(key);
    const details = document.createElement('details');
    details.className = 'wiki-group';
    details.open = searching || !state.collapsedCats.has(key);
    const summary = el('summary', 'wiki-group-head');
    summary.append(el('span', 'wiki-group-name', catLabel(key)), el('span', 'wiki-group-count', String(list.length)));
    details.append(summary);
    details.addEventListener('toggle', () => {
      if (searching) return;
      if (details.open) state.collapsedCats.delete(key);
      else state.collapsedCats.add(key);
    });
    for (const p of list) {
      const active = state.wikiSelected?.rel === p.rel;
      const b = el('button', 'wiki-leaf' + (active ? ' active' : ''));
      b.type = 'button';
      b.disabled = state.busy;
      b.title = `${p.title}\n${p.rel}`;
      b.textContent = p.title;
      b.onclick = () => selectWikiPage(p);
      details.append(b);
    }
    $('sessions').append(details);
  }
}

async function selectWikiPage(page) {
  if (state.busy) return;
  const version = ++state.version;
  state.wikiSelected = page;
  state.wikiInfo = null;
  drawWikiList();
  composer();
  $('eyebrow').textContent = 'LLM WIKI / PAGE';
  $('title').textContent = page.title;
  $('subtitle').textContent = page.rel;
  $('breadcrumb').textContent = page.category || 'wiki';
  $('info-tool').textContent = state.wiki.name || 'LLM Wiki';
  $('info-home-label').hidden = true;
  $('info-home').hidden = true;
  $('info-cwd').textContent = state.wiki.path || '—';
  $('info-id').textContent = page.rel;
  $('wiki-links').replaceChildren();
  $('thread').replaceChildren(el('p', 'list-empty', '正在读取 Wiki 页面…'));
  status('读取 Wiki 页面…');
  try {
    const info = await api(`/api/wiki/page?${new URLSearchParams({ rel: page.rel })}`);
    if (version !== state.version) return;
    state.wikiInfo = info;
    $('title').textContent = info.title;
    const article = el('article', 'wiki-doc message-body md');
    renderMarkdown(article, info.body);
    $('thread').replaceChildren(article);
    $('wiki-links').replaceChildren();
    if (info.links && info.links.length) {
      for (const link of info.links) {
        const btn = el('button', 'wiki-link-item', link.label);
        btn.type = 'button';
        btn.onclick = () => openWikiLink(link.target);
        $('wiki-links').append(btn);
      }
    } else {
      $('wiki-links').append(el('p', 'wiki-links-empty', '本页暂无双向链接'));
    }
    status(`已加载 ${info.rel}`);
  } catch (e) {
    if (version === state.version) {
      $('thread').replaceChildren(el('p', 'list-empty', e.message));
      status(e.message, true);
    }
  } finally {
    if (version === state.version) composer();
  }
}

async function openWikiLink(target) {
  if (state.mode !== 'wiki') await setMode('wiki');
  const match = resolveWikiTarget(target);
  if (match) {
    selectWikiPage(match);
  } else {
    status(`未找到 Wiki 页面：${target}`, true);
  }
}

async function loadWikiPages() {
  status('正在读取 LLM Wiki 页面…');
  $('refresh').disabled = true;
  try {
    state.wikiPages = await api('/api/wiki/pages');
    const previous = $('tool').value;
    $('tool').replaceChildren(el('option', null, '所有分类'));
    $('tool').firstChild.value = '';
    for (const cat of [...new Set(state.wikiPages.map(p => p.category).filter(Boolean))].sort()) {
      const opt = el('option', null, cat);
      opt.value = cat;
      $('tool').append(opt);
    }
    $('tool').value = previous;
    $('home').hidden = true;
    drawWikiList();
    status(`已读取 ${state.wikiPages.length} 个 Wiki 页面`);
  } catch (e) {
    status(e.message, true);
    $('sessions').replaceChildren(el('p', 'list-empty', '读取 Wiki 页面失败。'));
  } finally {
    $('refresh').disabled = state.busy;
  }
}

const GRAPH_PALETTE = ['#1f6956', '#4a7c66', '#6b8550', '#4d6f80', '#8c6d5c', '#6e6580'];

function renderGraph(data) {
  const nodes = (data.nodes || []).map((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, (data.nodes || []).length);
    const radius = 140 + (i % 5) * 32;
    return { ...n, x: Math.cos(angle) * radius, y: Math.sin(angle) * radius, vx: 0, vy: 0 };
  });
  if (!nodes.length) {
    $('thread').replaceChildren(el('p', 'list-empty', 'Wiki 目录中暂无页面可生成图谱。'));
    return;
  }
  const byId = new Map(nodes.map(n => [n.id, n]));
  const edges = (data.edges || [])
    .map(e => ({ a: byId.get(e.source), b: byId.get(e.target) }))
    .filter(e => e.a && e.b);

  // Deterministic spring-repulsion layout
  const kRep = 4200;
  const kSpring = 0.045;
  const ideal = 115;
  for (let iter = 0; iter < 110; iter++) {
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        const d2 = dx * dx + dy * dy + 25;
        const f = kRep / d2;
        const d = Math.sqrt(d2);
        dx = (dx / d) * f;
        dy = (dy / d) * f;
        a.vx -= dx; a.vy -= dy;
        b.vx += dx; b.vy += dy;
      }
    }
    for (const e of edges) {
      const dx = e.b.x - e.a.x;
      const dy = e.b.y - e.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - ideal) * kSpring;
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      e.a.vx += fx; e.a.vy += fy;
      e.b.vx -= fx; e.b.vy -= fy;
    }
    for (const n of nodes) {
      n.vx = (n.vx - n.x * 0.012) * 0.82;
      n.vy = (n.vy - n.y * 0.012) * 0.82;
      n.x += Math.max(-28, Math.min(28, n.vx));
      n.y += Math.max(-28, Math.min(28, n.vy));
    }
  }

  const NS = 'http://www.w3.org/2000/svg';
  const sEl = (tag, attrs = {}) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
    return node;
  };
  const wrap = el('div', 'graph-wrap');
  const svg = sEl('svg', { class: 'graph-svg', viewBox: '-420 -300 840 600' });
  const viewport = sEl('g');
  const edgeLayer = sEl('g');
  const nodeLayer = sEl('g');
  viewport.append(edgeLayer, nodeLayer);
  svg.append(viewport);

  const cats = [...new Set(nodes.map(n => n.category || ''))];
  const colorOf = cat => GRAPH_PALETTE[cats.indexOf(cat || '') % GRAPH_PALETTE.length];
  const neighbors = new Map(nodes.map(n => [n.id, new Set([n.id])]));
  const edgeEls = edges.map(e => {
    neighbors.get(e.a.id).add(e.b.id);
    neighbors.get(e.b.id).add(e.a.id);
    const line = sEl('line', { class: 'graph-edge', x1: e.a.x, y1: e.a.y, x2: e.b.x, y2: e.b.y });
    edgeLayer.append(line);
    return { ...e, el: line };
  });

  const nodeEls = nodes.map(n => {
    const r = Math.min(15, 6 + Math.sqrt(n.degree || 0) * 2.4);
    const g = sEl('g', { class: 'graph-node', transform: `translate(${n.x},${n.y})` });
    const circle = sEl('circle', { r, fill: colorOf(n.category) });
    const label = sEl('text', { x: r + 5, y: 4 });
    label.textContent = n.title.length > 22 ? n.title.slice(0, 21) + '…' : n.title;
    g.append(circle, label);
    g.addEventListener('mouseenter', () => {
      const keep = neighbors.get(n.id);
      for (const item of nodeEls) item.el.classList.toggle('dim', !keep.has(item.n.id));
      for (const item of edgeEls) {
        const hit = item.a.id === n.id || item.b.id === n.id;
        item.el.classList.toggle('hi', hit);
        item.el.classList.toggle('dim', !hit);
      }
    });
    g.addEventListener('mouseleave', () => {
      for (const item of nodeEls) item.el.classList.remove('dim');
      for (const item of edgeEls) item.el.classList.remove('hi', 'dim');
    });
    g.addEventListener('click', ev => {
      ev.stopPropagation();
      openWikiLink(n.id);
    });
    nodeLayer.append(g);
    return { n, el: g };
  });

  // Pan & zoom
  let pan = { x: 0, y: 0, scale: 1 };
  const applyPan = () => viewport.setAttribute('transform', `translate(${pan.x},${pan.y}) scale(${pan.scale})`);
  let drag = null;
  svg.addEventListener('mousedown', ev => {
    if (ev.target.closest('.graph-node')) return;
    drag = { x: ev.clientX, y: ev.clientY, px: pan.x, py: pan.y };
    svg.classList.add('dragging');
  });
  window.addEventListener('mousemove', ev => {
    if (!drag) return;
    pan.x = drag.px + (ev.clientX - drag.x);
    pan.y = drag.py + (ev.clientY - drag.y);
    applyPan();
  });
  window.addEventListener('mouseup', () => {
    drag = null;
    svg.classList.remove('dragging');
  });
  svg.addEventListener('wheel', ev => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.12 : 0.89;
    pan.scale = Math.max(0.35, Math.min(3, pan.scale * factor));
    applyPan();
  }, { passive: false });

  wrap.append(svg, el('div', 'graph-hint', `${nodes.length} 个页面 · ${edges.length} 条链接 · 滚轮缩放 · 拖拽平移 · 点击节点打开`));
  $('thread').replaceChildren(wrap);
}

async function loadWikiGraph() {
  status('正在构建知识图谱…');
  $('eyebrow').textContent = 'LLM WIKI / KNOWLEDGE GRAPH';
  $('title').textContent = `${state.wiki.name || 'LLM Wiki'} 知识图谱`;
  $('subtitle').textContent = state.wiki.path || '';
  $('breadcrumb').textContent = '图谱';
  $('info-tool').textContent = state.wiki.name || 'LLM Wiki';
  $('info-home-label').hidden = true;
  $('info-home').hidden = true;
  $('info-cwd').textContent = state.wiki.path || '—';
  $('info-id').textContent = 'wiki graph';
  $('thread').replaceChildren(el('p', 'list-empty', '正在计算节点布局…'));
  try {
    const [data] = await Promise.all([api('/api/wiki/graph'), state.wikiPages.length ? Promise.resolve() : loadWikiPages()]);
    renderGraph(data);
    status(`图谱已加载：${data.nodes.length} 个页面，${data.edges.length} 条链接`);
  } catch (e) {
    $('thread').replaceChildren(el('p', 'list-empty', e.message));
    status(e.message, true);
  }
}

async function setMode(mode) {
  if (state.busy || state.mode === mode) return;
  state.mode = mode;
  for (const [m, id, railId] of [
    ['sessions', 'tab-sessions', 'rail-sessions'],
    ['wiki', 'tab-wiki', 'rail-wiki'],
    ['graph', 'tab-graph', 'rail-graph'],
  ]) {
    $(id).classList.toggle('active', mode === m);
    $(id).setAttribute('aria-selected', String(mode === m));
    $(railId).classList.toggle('active', mode === m);
  }
  $('library-title').textContent = mode === 'sessions' ? '会话库' : (state.wiki.name || '知识库');
  $('search').value = '';
  $('search').placeholder = mode === 'sessions' ? '搜索标题、目录、会话 ID' : '搜索 Wiki 标题、路径、分类';
  if (mode === 'wiki') {
    await loadWikiPages();
    if (state.wikiSelected) selectWikiPage(state.wikiSelected);
    else if (state.wikiPages.length) selectWikiPage(state.wikiPages[0]);
    else {
      $('eyebrow').textContent = 'LLM WIKI';
      $('title').textContent = state.wiki.name || 'LLM Wiki';
      $('subtitle').textContent = state.wiki.path || '';
      $('thread').replaceChildren(el('p', 'list-empty', 'Wiki 目录中暂无 Markdown 页面。'));
      composer();
    }
  } else if (mode === 'graph') {
    composer();
    await loadWikiGraph();
  } else {
    await load();
    if (state.selected) select(state.selected);
    else resetSelection();
  }
}

$('search').oninput = () => (state.mode === 'sessions' ? drawList() : drawWikiList());
$('tool').onchange = () => (state.mode === 'sessions' ? drawList() : drawWikiList());
$('home').onchange = drawList;
$('refresh').onclick = () => {
  loadWikiStatus();
  if (state.mode === 'wiki') loadWikiPages();
  else if (state.mode === 'graph') loadWikiGraph();
  else load();
};
$('tab-sessions').onclick = () => setMode('sessions');
$('tab-wiki').onclick = () => setMode('wiki');
$('tab-graph').onclick = () => setMode('graph');
$('rail-sessions').onclick = () => setMode('sessions');
$('rail-wiki').onclick = () => setMode('wiki');
$('rail-graph').onclick = () => setMode('graph');
$('rail-search').onclick = () => openPalette();
$('rail-refresh').onclick = () => $('refresh').click();

// ---------- Ctrl+K Global Command Palette ----------
const palette = { open: false, items: [], cursor: 0 };

function paletteItems(query) {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const out = [];
  for (const p of state.wikiPages) {
    const hay = `wiki ${p.title} ${p.rel} ${p.category}`.toLowerCase();
    if (words.every(w => hay.includes(w))) {
      out.push({ kind: 'wiki', badge: 'Wiki', title: p.title, sub: p.rel, data: p });
    }
  }
  for (const r of state.rows) {
    const hay = `${r.tool} ${r.title} ${r.cwd} ${r.id} ${r.extra_home || ''}`.toLowerCase();
    if (words.every(w => hay.includes(w))) {
      out.push({ kind: 'session', badge: r.tool, title: r.title || '未命名会话', sub: `${r.cwd || ''} · ${r.age}`, data: r });
    }
  }
  return out.slice(0, 40);
}

function drawPalette() {
  palette.items = paletteItems($('palette-input').value);
  palette.cursor = Math.min(palette.cursor, Math.max(0, palette.items.length - 1));
  const box = $('palette-list');
  box.replaceChildren();
  if (!palette.items.length) {
    box.append(el('div', 'palette-empty', '没有匹配的会话或 Wiki 页面'));
    return;
  }
  palette.items.forEach((item, idx) => {
    const btn = el('button', 'palette-item' + (idx === palette.cursor ? ' active' : ''));
    btn.type = 'button';
    btn.append(el('span', 'palette-kind' + (item.kind === 'wiki' ? ' wiki' : ''), item.badge));
    const main = el('div', 'palette-main');
    main.append(el('div', 'palette-title', item.title), el('div', 'palette-sub', item.sub));
    btn.append(main);
    btn.onmouseenter = () => { palette.cursor = idx; drawPalette(); };
    btn.onclick = () => pickPalette(item);
    box.append(btn);
  });
}

async function pickPalette(item) {
  closePalette();
  if (item.kind === 'wiki') {
    if (state.mode !== 'wiki') await setMode('wiki');
    selectWikiPage(item.data);
  } else {
    if (state.mode !== 'sessions') await setMode('sessions');
    select(item.data);
  }
}

async function openPalette() {
  if (state.busy) return;
  palette.open = true;
  palette.cursor = 0;
  $('palette-backdrop').hidden = false;
  $('palette-input').value = '';
  if (state.wiki.configured && !state.wikiPages.length) {
    try { state.wikiPages = await api('/api/wiki/pages'); } catch (e) {}
  }
  drawPalette();
  $('palette-input').focus();
}

function closePalette() {
  palette.open = false;
  $('palette-backdrop').hidden = true;
}

$('open-palette').onclick = openPalette;
$('palette-backdrop').onclick = e => { if (e.target === $('palette-backdrop')) closePalette(); };
$('palette-input').oninput = () => { palette.cursor = 0; drawPalette(); };
$('palette-input').onkeydown = e => {
  if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); palette.cursor = Math.min(palette.items.length - 1, palette.cursor + 1); drawPalette(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); palette.cursor = Math.max(0, palette.cursor - 1); drawPalette(); }
  else if (e.key === 'Enter' && palette.items[palette.cursor]) { e.preventDefault(); pickPalette(palette.items[palette.cursor]); }
};
window.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    if (palette.open) closePalette();
    else openPalette();
  }
});
$('wiki-pill').onclick = async () => {
  if (state.busy || !state.wiki.configured) return;
  if (!state.wiki.online) {
    await loadWikiStatus();
    if (!state.wiki.online) {
      status('LLM Wiki 应用当前离线，请先启动桌面端并开启本地 API', true);
      return;
    }
  }
  status('正在触发 LLM Wiki 扫描…');
  try {
    await postJson('/api/wiki/rescan', {});
    status('已触发 LLM Wiki 扫描，可在 LLM Wiki 应用中查看进度');
  } catch (e) {
    status(e.message, true);
    loadWikiStatus();
  }
};
$('export-wiki').onclick = async () => {
  if (state.busy || !state.selected || !state.wiki.configured) return;
  state.busy = true;
  composer();
  status('正在导出会话到 LLM Wiki…');
  try {
    const res = await postJson('/api/export-wiki', {
      tool: state.selected.tool,
      id: state.selected.id,
    });
    const verb = res.written ? '已导出到 LLM Wiki' : '快照已是最新';
    const tail = res.rescanned ? '并已触发扫描' : '（LLM Wiki 离线，快照已落盘，启动后可点右上角扫描）';
    status(`${verb}${tail}`);
    loadWikiStatus();
  } catch (e) {
    status(e.message, true);
  } finally {
    state.busy = false;
    composer();
  }
};
$('message').oninput = composer;
$('message').onkeydown = e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    $('composer').requestSubmit();
  }
};

$('export').onclick = () => {
  const doc = state.mode === 'wiki' ? state.wikiInfo : state.info;
  if (!doc) return;
  const blob = new Blob([doc.markdown], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = el('a');
  a.href = url;
  a.download = state.mode === 'wiki' ? `${doc.stem}.md` : `${doc.tool}-${doc.id}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

async function copyText(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    status(okMsg);
  } catch (e) {
    status('复制失败，请手动选择右侧命令', true);
  }
}

$('copy').onclick = () => copyText($('show-command').textContent, '已复制原文查看命令');
$('copy-resume').onclick = () => copyText($('resume-command').textContent, '已复制终端恢复命令');

// In-app dialog replacing window.prompt / window.confirm.
function askDialog({ title, message = '', value = null, okText = '确定', danger = false, propose = null }) {
  return new Promise(resolve => {
    const backdrop = $('dialog-backdrop');
    const input = $('dialog-input');
    const ok = $('dialog-ok');
    const cancel = $('dialog-cancel');
    const proposeBtn = $('dialog-propose');
    const errEl = $('dialog-error');
    let busy = false;

    $('dialog-title').textContent = title;
    $('dialog-message').textContent = message;
    $('dialog-message').hidden = !message;
    input.hidden = value === null;
    input.value = value || '';
    input.disabled = false;
    errEl.hidden = true;
    errEl.textContent = '';
    ok.textContent = okText;
    ok.disabled = false;
    ok.classList.toggle('danger', danger);
    cancel.disabled = false;
    proposeBtn.hidden = !propose;
    proposeBtn.disabled = false;
    proposeBtn.textContent = '✨ AI 拟定';
    backdrop.hidden = false;
    (value === null ? ok : input).focus();
    if (value !== null) input.select();

    const finish = result => {
      if (busy) return;
      backdrop.hidden = true;
      backdrop.onclick = null;
      $('dialog-form').onsubmit = null;
      cancel.onclick = null;
      proposeBtn.onclick = null;
      window.removeEventListener('keydown', onKey, true);
      resolve(result);
    };
    const onKey = e => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(null); }
    };
    window.addEventListener('keydown', onKey, true);
    backdrop.onclick = e => { if (e.target === backdrop) finish(null); };
    cancel.onclick = () => finish(null);
    proposeBtn.onclick = async () => {
      if (!propose || busy) return;
      busy = true;
      errEl.hidden = true;
      errEl.textContent = '';
      proposeBtn.disabled = true;
      proposeBtn.textContent = '拟定中…';
      input.disabled = true;
      ok.disabled = true;
      cancel.disabled = true;
      try {
        const proposed = await propose();
        if (proposed) {
          input.value = proposed;
        }
      } catch (e) {
        errEl.textContent = e.message || 'AI 拟定失败';
        errEl.hidden = false;
      } finally {
        busy = false;
        proposeBtn.disabled = false;
        proposeBtn.textContent = '✨ AI 拟定';
        input.disabled = false;
        ok.disabled = false;
        cancel.disabled = false;
        input.focus();
        input.select();
      }
    };
    $('dialog-form').onsubmit = e => {
      e.preventDefault();
      if (!busy) finish(value === null ? true : input.value);
    };
  });
}

$('rename').onclick = async () => {
  if (state.busy || !state.info?.can_rename || !state.selected) return;
  const current = state.selected.title || '';
  const target = state.selected;
  const next = await askDialog({
    title: '重命名会话',
    message: '输入新的会话标题，或让 AI 根据对话内容自动拟定',
    value: current,
    okText: '保存',
    propose: async () => {
      const res = await postJson('/api/rename/propose', { tool: target.tool, id: target.id });
      return res.title;
    },
  });
  if (next === null) return;
  const trimmed = next.trim();
  if (!trimmed || trimmed === current) return;
  state.busy = true;
  composer();
  status('正在重命名…');
  try {
    const res = await postJson('/api/rename', {
      tool: state.selected.tool,
      id: state.selected.id,
      title: trimmed,
    });
    state.selected.title = res.title;
    $('title').textContent = res.title;
    drawList();
    status('已更新标题');
  } catch (e) {
    status(e.message, true);
  } finally {
    state.busy = false;
    composer();
  }
};

$('delete').onclick = async () => {
  if (state.busy || !state.info?.can_delete || !state.selected) return;
  const row = state.selected;
  const where = row.extra_home ? `\n来源：${row.extra_home}` : '';
  const ok = await askDialog({
    title: '删除会话',
    message: `确定删除 [${row.tool}] ${row.title || row.id}？${where}\n\n文件与数据库记录会自动备份后再删除。`,
    okText: '删除',
    danger: true,
  });
  if (!ok) return;
  state.busy = true;
  composer();
  status('正在删除并备份…');
  try {
    const res = await postJson('/api/delete', { tool: row.tool, id: row.id });
    state.rows = state.rows.filter(r => !(r.tool === row.tool && r.id === row.id));
    resetSelection();
    drawList();
    status(`已删除，备份位置：${res.backup}`);
  } catch (e) {
    status(e.message, true);
  } finally {
    state.busy = false;
    composer();
  }
};

$('composer').onsubmit = async e => {
  e.preventDefault();
  if (state.busy || !state.info?.can_chat) return;
  const text = $('message').value.trim();
  if (!text) return;
  state.busy = true;
  const row = state.selected;
  const draft = text;
  $('message').value = '';
  composer();
  drawList();
  bubble('user', text, true);
  const output = bubble('assistant', '', false);
  $('thread').scrollTop = $('thread').scrollHeight;
  status(`正在启动 ${row.tool}…`);
  let success = false;
  let gotOutput = false;
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CW-Token': token },
      body: JSON.stringify({ tool: row.tool, id: row.id, text }),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error || '发送失败');
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';
    let done = false;
    while (true) {
      const part = await reader.read();
      pending += decoder.decode(part.value || new Uint8Array(), { stream: !part.done });
      const lines = pending.split('\n');
      pending = lines.pop();
      for (const line of lines) {
        if (!line) continue;
        const event = JSON.parse(line);
        if (event.output) {
          output.textContent += event.output;
          gotOutput = true;
          status(`${row.tool} 正在回复…`);
          $('thread').scrollTop = $('thread').scrollHeight;
        }
        if (event.done) {
          done = true;
          if (event.code !== 0) throw new Error(`${row.tool} 退出码 ${event.code}，可检查输出后重试`);
          success = true;
        }
      }
      if (part.done) break;
    }
    if (!done) throw new Error('连接中断，无法确认本轮是否完成，请刷新会话后检查');
    status('回复完成');
  } catch (e) {
    status(e.message, true);
    if (!gotOutput) {
      output.textContent = e.message;
      $('message').value = draft;
    }
  } finally {
    if (success) {
      try {
        const info = await api(`/api/conversation?${new URLSearchParams({ tool: row.tool, id: row.id })}`);
        state.info = info;
        paintConversation(info);
        $('thread').scrollTop = $('thread').scrollHeight;
      } catch (e) {
        status('回复已完成；重新读取原文失败，请刷新', true);
      }
    }
    state.busy = false;
    composer();
    drawList();
    $('message').focus();
  }
};

window.addEventListener('beforeunload', e => {
  if (state.busy) {
    e.preventDefault();
    e.returnValue = '';
  }
});

loadWikiStatus();
load();
