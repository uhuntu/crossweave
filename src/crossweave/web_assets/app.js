const $ = id => document.getElementById(id);
const state = {
  mode: 'sessions',
  rows: [], selected: null, info: null,
  wiki: { configured: false, online: false, path: '', name: '', pages: 0 },
  wikiPages: [], wikiSelected: null, wikiInfo: null,
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
  const isWiki = state.mode === 'wiki';
  $('composer').hidden = isWiki;
  $('session-card').hidden = isWiki;
  $('wiki-links-card').hidden = !isWiki;
  const ok = !isWiki && !!state.info?.can_chat && !state.busy;
  $('message').disabled = !ok;
  $('send').disabled = !ok || !$('message').value.trim();
  $('export').disabled = isWiki ? !state.wikiInfo : (!state.info || state.busy);
  $('export-wiki').hidden = isWiki || !state.wiki.configured;
  $('export-wiki').disabled = isWiki || !state.info || state.busy;
  $('rename').hidden = isWiki;
  $('rename').disabled = isWiki || !state.info?.can_rename || state.busy;
  $('rename').title = state.info?.extra_home ? '外部目录会话为只读，暂不支持重命名' : '';
  $('delete').hidden = isWiki;
  $('delete').disabled = isWiki || !state.info?.can_delete || state.busy;
  $('copy').disabled = !state.info;
  $('copy-resume').disabled = !state.info;
  $('refresh').disabled = state.busy;
  $('message').placeholder = state.info?.can_chat
    ? '继续这段对话…（Ctrl / ⌘ + Enter 发送）'
    : state.info?.extra_home
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

function paintConversation(info) {
  $('thread').replaceChildren();
  if (info.messages) {
    for (const m of info.messages) bubble(m.role, m.text, true);
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

function drawWikiList() {
  const pages = filteredWikiPages();
  $('count').textContent = `${pages.length} 个页面`;
  $('sessions').replaceChildren();
  for (const p of pages) {
    const active = state.wikiSelected?.rel === p.rel;
    const b = el('button', 'session' + (active ? ' active' : ''));
    b.disabled = state.busy;
    b.title = `${p.title}\n${p.rel}`;
    b.append(el('div', 'session-title', p.title));
    const meta = el('div', 'session-meta');
    if (p.category) meta.append(el('span', 'tool-badge', p.category));
    meta.append(el('span', 'session-path', p.rel));
    b.append(meta);
    b.onclick = () => selectWikiPage(p);
    $('sessions').append(b);
  }
  if (!pages.length) $('sessions').append(el('p', 'list-empty', '没有匹配的 Wiki 页面。'));
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

async function setMode(mode) {
  if (state.busy || state.mode === mode) return;
  state.mode = mode;
  $('tab-sessions').classList.toggle('active', mode === 'sessions');
  $('tab-sessions').setAttribute('aria-selected', String(mode === 'sessions'));
  $('tab-wiki').classList.toggle('active', mode === 'wiki');
  $('tab-wiki').setAttribute('aria-selected', String(mode === 'wiki'));
  $('search').value = '';
  $('search').placeholder = mode === 'wiki' ? '搜索 Wiki 标题、路径、分类' : '搜索标题、目录、会话 ID';
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
  } else {
    await load();
    if (state.selected) select(state.selected);
    else resetSelection();
  }
}

$('search').oninput = () => (state.mode === 'wiki' ? drawWikiList() : drawList());
$('tool').onchange = () => (state.mode === 'wiki' ? drawWikiList() : drawList());
$('home').onchange = drawList;
$('refresh').onclick = () => {
  loadWikiStatus();
  if (state.mode === 'wiki') loadWikiPages();
  else load();
};
$('tab-sessions').onclick = () => setMode('sessions');
$('tab-wiki').onclick = () => setMode('wiki');
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

$('rename').onclick = async () => {
  if (state.busy || !state.info?.can_rename || !state.selected) return;
  const current = state.selected.title || '';
  const next = window.prompt('输入新的会话标题：', current);
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
  const ok = window.confirm(
    `确定删除 [${row.tool}] ${row.title || row.id}？${where}\n\n文件与数据库记录会自动备份后再删除。`
  );
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
