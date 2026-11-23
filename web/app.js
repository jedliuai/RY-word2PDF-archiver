'use strict';

/* Front end for RY 文档归档助手. All business logic lives in Python (app_controller.py);
   this file only renders the state snapshot and forwards user intents to window.pywebview.api. */

const STEP_LABELS = ['解压 / 扫描', 'WPS 转换', '解密与校验', 'RAR 打包校验', '清理 Word'];
const STEP_OF = {
  new: 1, scanned: 1, extracting: 1, waiting_input: 1,
  converting: 2, decrypting: 3, waiting: 3, verifying: 3,
  archiving: 4, review: 5, cleaning: 5, complete: 6,
};

const ICON = {
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7.5a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  drop: '<svg viewBox="0 0 24 24"><path d="M3 7.5a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v5M9.5 13.5L12 16l2.5-2.5"/></svg>',
  archive: '<svg viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="18" rx="2"/><path d="M10 3v2h2v2h-2v2h2v2h-2"/><rect x="9" y="13" width="4" height="4" rx="1"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  alert: '<svg viewBox="0 0 24 24"><path d="M12 3l9.5 17h-19z"/><path d="M12 10v4M12 17.5h.01"/></svg>',
  info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
  chevron: '<svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>',
};

let state = null;
let lastStep = 1;
let logOpen = false;
let modalHandler = null;
const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------------- backend bridge ---------------- */
const api = {
  async call(name, ...args) {
    if (!window.pywebview || !window.pywebview.api) return null;
    try {
      const result = await window.pywebview.api[name](...args);
      if (result && result.state) render(result.state);
      if (result && result.ok === false) toast(result.error);
      return result;
    } catch (err) {
      toast(String(err));
      return null;
    }
  },
};

window.__onState = (next) => render(next);
window.__onDropError = (message) => toast(message);
window.__onCloseBlocked = () => toast('任务正在运行。请先点击“停止”，等待任务停止后再关闭窗口。');

window.addEventListener('pywebviewready', async () => {
  const initial = await window.pywebview.api.get_state();
  render(initial);
});

/* ---------------- screen selection ---------------- */
function screenOf(s) {
  if (['review', 'cleaning', 'complete'].includes(s.phase) && !s.error) return 'review';
  if (s.busy || s.error || s.notice || ['waiting', 'waiting_input', 'error', 'stopped'].includes(s.phase)) return 'process';
  return 'start';
}

function currentStep(s) {
  const step = STEP_OF[s.displayPhase];
  if (step) lastStep = step;
  return lastStep;
}

/* ---------------- render ---------------- */
function render(next) {
  state = next;
  const s = state;
  const failed = Boolean(s.error) || s.phase === 'error' || s.phase === 'stopped';
  const step = currentStep(s);
  renderSteps(step, failed);
  renderSidebarInput(s);
  renderLog(s);

  const main = $('main');
  const scroll = {};
  main.querySelectorAll('[data-keep-scroll]').forEach((el) => { scroll[el.dataset.keepScroll] = el.scrollTop; });
  const screen = screenOf(s);
  main.innerHTML = screen === 'start' ? startScreen(s) : screen === 'process' ? processScreen(s, step, failed) : reviewScreen(s);
  main.querySelectorAll('[data-keep-scroll]').forEach((el) => { el.scrollTop = scroll[el.dataset.keepScroll] || 0; });
  tickElapsed();
}

function renderSteps(step, failed) {
  $('steps').innerHTML = STEP_LABELS.map((label, i) => {
    const n = i + 1;
    let cls = 'step';
    let dot = String(n);
    if (n < step) { cls += ' done'; dot = ICON.check; }
    else if (n === step) cls += failed ? ' failed' : ' active';
    return `<li class="${cls}"${n === step ? ' aria-current="step"' : ''}><span class="dot">${dot}</span><span>${label}</span></li>`;
  }).join('');
}

function renderSidebarInput(s) {
  const box = $('sidebar-input');
  if (!s.input || screenOf(s) === 'start') { box.innerHTML = ''; return; }
  const kind = s.input.kind === 'archive' ? '压缩包' : '文件夹';
  const words = s.counts.words ? ` · ${s.counts.words} 份 Word` : '';
  box.innerHTML = `<div class="input-mini" title="${esc(s.input.path)}"><span class="label">当前输入</span>
    <span class="value">${esc(s.input.name)}</span><span class="label">${kind}${words}</span></div>`;
}

function renderLog(s) {
  $('log-count').textContent = s.logs.length ? String(s.logs.length) : '';
  const list = $('log-list');
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.innerHTML = s.logs.length
    ? s.logs.map((l) => `<li><time>${esc(l.time)}</time><span>${esc(l.text)}</span></li>`).join('')
    : '<li><span>暂无日志</span></li>';
  if (atBottom) list.scrollTop = list.scrollHeight;
  $('log-drawer').hidden = !logOpen;
}

/* ---------- ① start ---------- */
function startScreen(s) {
  const busy = s.busy;
  let inputCard = '';
  if (s.input) {
    const isArchive = s.input.kind === 'archive';
    let meta = isArchive ? 'ZIP / RAR 压缩包 · 开始后在旁边解压，原包保留' : '文件夹 · 含子目录';
    if (s.phase === 'scanned') meta = `扫描完成：${s.counts.words} 份 Word，已有 ${s.counts.pdfs} 份 PDF`;
    inputCard = `<div class="card">
      <div class="tile-icon">${isArchive ? ICON.archive : ICON.folder}</div>
      <div class="body"><span class="primary-text" title="${esc(s.input.path)}">${esc(s.input.path)}</span>
      <span class="secondary-text">${esc(meta)}</span></div>
      <button type="button" class="link-btn" data-action="reset">清除</button></div>`;
  }
  const opt = (key, label, hint) => `<div class="option"><div class="text"><span class="label">${label}</span><span class="hint">${hint}</span></div>
    <button type="button" class="switch" role="switch" aria-checked="${s.options[key]}" aria-label="${label}" data-action="toggle" data-key="${key}" ${busy ? 'disabled' : ''}><span class="knob"></span></button></div>`;
  return `
    <header class="screen-head"><h1>选择要归档的资料</h1>
      <p class="subtitle">WPS 批量转换 · 绿盾官方解密 · 验证后清理</p></header>
    <div class="dropzone" id="dropzone">
      <span class="big-icon">${ICON.drop}</span>
      <span class="title">把文件夹、ZIP 或 RAR 拖到这里</span>
      <div class="alt"><span>或</span>
        <button type="button" class="link-btn" data-action="choose-folder">选择文件夹</button><span aria-hidden="true">·</span>
        <button type="button" class="link-btn" data-action="choose-archive">选择压缩包</button></div>
      <span class="hint">拖入只选择输入，点击“开始”才会执行</span>
    </div>
    ${inputCard}
    <div class="options">
      ${opt('overwrite', '备份旧 PDF 后重新转换', '目录里已有同名 PDF 时，先备份，再用本次导出结果替换')}
      ${opt('auto', '全自动清理', 'RAR 验证通过后，自动把全部 Word 移入回收站（试用期间建议关闭）')}
    </div>
    <footer class="footer">
      <span class="note">流程：扫描 → WPS 转换 → 解密与校验 → RAR 打包校验 → 清理 Word</span>
      <button type="button" class="btn lg" data-action="scan" ${!s.input || busy ? 'disabled' : ''}>仅扫描清单</button>
      <button type="button" class="btn lg primary" data-action="start" ${!s.input || busy ? 'disabled' : ''}>开始转换、解密并打包</button>
    </footer>`;
}

/* ---------- ② processing ---------- */
function processScreen(s, step, failed) {
  const p = s.progress;
  const determinate = s.displayPhase === 'converting' && p.total > 0;
  const pct = determinate ? Math.round((p.current * 100) / p.total) : 0;
  const waiting = s.canResume;

  let eyebrow = step <= 5 ? `第 ${step} 步 · ${STEP_LABELS[step - 1]}` : '';
  let title = s.statusText;
  if (s.busy && s.busyKind === 'scan' && ['new', 'scanned'].includes(s.displayPhase)) title = '正在扫描并建立核对副本';
  else if (s.busy && s.displayPhase === 'new') title = '正在准备输入';
  if (failed && !s.busy) { title = s.phase === 'stopped' ? '已停止' : '流程已停止'; }

  let barClass = 'bar';
  let fill = `style="width:${pct}%"`;
  if (failed && !s.busy) { barClass += ' failed'; fill = `style="width:${Math.max(pct, 100)}%"`; }
  else if (waiting) { barClass += ' paused'; fill = 'style="width:100%"'; }
  else if (!determinate) barClass += ' indeterminate';

  const left = determinate ? `第 ${p.current} / ${p.total} 份` : (waiting ? '已暂停，等待审批' : (s.busy ? '处理中' : ''));
  const exported = s.files.filter((f) => f.pages > 0).length;
  const tiles = `<div class="tiles">
    <div class="tile"><span class="k">Word 文档</span><span class="v">${s.counts.words || '—'}${s.counts.words ? ' 份' : ''}</span></div>
    <div class="tile"><span class="k">已导出 PDF</span><span class="v">${exported} 份</span></div>
    <div class="tile"><span class="k">已解密校验</span><span class="v">${s.counts.verified} 份</span></div>
    <div class="tile"><span class="k">处理目录</span><span class="v" title="${esc(s.processingFolder)}">${esc(s.processingFolder ? s.processingFolder.split(/[\\/]/).pop() : '—')}</span></div>
  </div>`;

  let callout = '';
  if (s.error) {
    callout = `<div class="callout red" role="alert"><div class="c-icon">${ICON.alert}</div>
      <div class="c-body"><span class="c-title">${esc(s.error.title)}</span><span class="c-text">${esc(s.error.message)}</span></div>
      <div class="c-actions">${s.error.jobDir ? '<button type="button" class="btn" data-action="open-job">打开任务记录</button>' : ''}
      <button type="button" class="btn primary" data-action="back">返回重新开始</button></div></div>`;
  } else if (waiting) {
    const msg = s.notice ? s.notice.message
      : (s.phase === 'waiting_input' ? '压缩包本身受绿盾保护，已提交解密申请。' : '已对处理目录提交解密申请。')
        + '请在绿盾窗口中批准，然后点击继续，不会重复提交。';
    callout = `<div class="callout amber"><div class="c-icon">${ICON.clock}</div>
      <div class="c-body"><span class="c-title">等待绿盾审批</span><span class="c-text">${esc(msg)}</span></div>
      <div class="c-actions"><button type="button" class="btn primary" data-action="resume">继续等待解密</button></div></div>`;
  } else if (failed && !s.busy) {
    callout = `<div class="callout red"><div class="c-icon">${ICON.alert}</div>
      <div class="c-body"><span class="c-title">任务已停止</span><span class="c-text">后续步骤不会自动执行，Word 不会被清理。</span></div>
      <div class="c-actions"><button type="button" class="btn primary" data-action="back">返回重新开始</button></div></div>`;
  }

  const latest = s.logs.length ? s.logs[s.logs.length - 1] : null;
  return `
    <header class="screen-head">${eyebrow ? `<span class="eyebrow${failed && !s.busy ? ' red' : ''}">${esc(eyebrow)}</span>` : ''}
      <h1 class="title-xl">${esc(title)}</h1></header>
    <div class="progress">
      <div class="${barClass}" role="progressbar" aria-label="处理进度" ${determinate ? `aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"` : ''}><div class="fill" ${fill}></div></div>
      <div class="progress-meta"><span>${esc(left)}${s.startedAt ? ` · 已用时 <span id="elapsed"></span>` : ''}</span><strong>${determinate ? pct + '%' : ''}</strong></div>
    </div>
    ${tiles}
    ${callout}
    <div class="grow"></div>
    <footer class="log-bar">
      <button type="button" class="log-toggle" data-action="toggle-log" aria-expanded="${logOpen}">${ICON.chevron}运行日志</button>
      <span class="log-latest">${latest ? `${esc(latest.time)}  ${esc(latest.text)}` : ''}</span>
      ${s.busy ? '<button type="button" class="btn" data-action="stop">停止</button>' : ''}
    </footer>`;
}

/* ---------- ③ review ---------- */
function reviewScreen(s) {
  const complete = s.phase === 'complete';
  const cleaning = s.busy;
  const can = s.canCleanup;
  const a = s.archive;
  const files = s.files;
  const remaining = files.filter((f) => !f.recycled).length;

  const success = a ? `<div class="callout green"><div class="c-icon">${ICON.check}</div>
      <div class="c-body"><span class="c-title">${complete ? '全部完成' : '交付物已就绪'}</span>
      <span class="c-text" title="${esc(a.path)}">${esc(a.name)} · ${s.counts.pdfs} 个 PDF · RAR 校验通过</span></div>
      <div class="c-actions"><button type="button" class="btn" data-action="open-output">打开交付目录</button></div></div>` : '';

  const warning = s.cleanupWarning
    ? `<div class="inline-note amber">${ICON.alert}<span>交付已完成，部分中间文件未清理：${esc(s.cleanupWarning)}</span></div>` : '';
  const note = complete
    ? `<div class="inline-note">${ICON.info}<span>${s.isArchiveInput ? '解压目录已清理，原压缩包和最终 RAR 保留。' : 'PDF 和最终 RAR 保留，核对副本与临时文件已自动清理。'}</span></div>`
    : `<div class="inline-note">${ICON.info}<span>清理前请先抽查 PDF 的排版、图片和内容。Word 会移入回收站，可以恢复。</span></div>`;

  const rows = files.length ? files.map((f) => {
    const disabled = f.recycled || !can;
    const dot = f.recycled ? 'gone' : (f.verified ? 'ok' : '');
    return `<label class="row${f.recycled ? ' recycled' : ''}${disabled ? ' disabled' : ''}">
      <input type="checkbox" data-action="select" data-source="${esc(f.source)}" ${f.selected || f.recycled ? 'checked' : ''} ${disabled ? 'disabled' : ''} aria-label="清理 ${esc(f.name)}">
      <span class="name" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="st"><i class="${dot}"></i>${esc(f.status)}</span></label>`;
  }).join('') : '<div class="empty">本次没有需要清理的 Word。</div>';

  let cta;
  if (complete) cta = '<button type="button" class="btn lg primary" data-action="reset">处理下一批</button>';
  else if (cleaning) cta = '<button type="button" class="btn lg primary" disabled>正在移入回收站…</button>';
  else cta = `<button type="button" class="btn lg primary" data-action="cleanup" ${s.counts.selected && can ? '' : 'disabled'}>${s.counts.selected ? `将选中的 ${s.counts.selected} 份 Word 移入回收站` : '请先勾选要清理的 Word'}</button>`;

  return `
    <header class="screen-head"><h1>${complete ? '归档完成' : '交付与清理'}</h1>
      <p class="subtitle">${complete ? '交付物已生成，本批 Word 已全部处理。' : '全部 PDF 已解密并校验，RAR 已通过完整性测试。'}</p></header>
    ${success}
    ${warning}
    ${note}
    <section class="list" aria-label="Word 清理清单">
      <div class="list-head"><span class="t">Word 清理清单</span>
        <span class="c">${complete ? `已清理 ${s.counts.recycled} / ${files.length}` : `已选 ${s.counts.selected} / ${remaining}`}</span>
        <button type="button" class="link-btn" data-action="select-all" ${can && remaining ? '' : 'disabled'}>全选可清理项</button>
        <button type="button" class="link-btn" data-action="clear-all" ${can && s.counts.selected ? '' : 'disabled'}>取消全选</button></div>
      <div class="list-body" data-keep-scroll="files">${rows}</div>
    </section>
    <footer class="footer">
      <span class="note">${cleaning ? '每份清理前都会重新核对 PDF 与 RAR。' : 'PDF 与 RAR 会保留，核对副本和临时文件在全部完成后自动清理。'}</span>
      ${cta}
    </footer>`;
}

/* ---------------- elapsed timer ---------------- */
function tickElapsed() {
  const el = $('elapsed');
  if (!el || !state || !state.startedAt) return;
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - state.startedAt));
  const mm = String(Math.floor(secs / 60)).padStart(2, '0');
  const ss = String(secs % 60).padStart(2, '0');
  el.textContent = secs >= 3600 ? `${Math.floor(secs / 3600)}:${mm.slice(-2)}:${ss}` : `${mm}:${ss}`;
}
setInterval(() => { if (state && state.busy) tickElapsed(); }, 1000);

/* ---------------- modal / toast ---------------- */
function confirmModal(title, bodyHtml, okLabel, onOk) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = bodyHtml;
  $('modal-ok').textContent = okLabel;
  modalHandler = onOk;
  $('modal').hidden = false;
  $('modal-ok').focus();
}
function closeModal() { $('modal').hidden = true; modalHandler = null; }

function toast(message) {
  if (!message) return;
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = message;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

/* ---------------- events ---------------- */
document.addEventListener('click', (event) => {
  const target = event.target.closest('[data-action]');
  if (!target || target.disabled) return;
  const action = target.dataset.action;
  switch (action) {
    case 'choose-folder': api.call('choose_folder'); break;
    case 'choose-archive': api.call('choose_archive'); break;
    case 'reset': api.call('reset'); break;
    case 'toggle': api.call('set_options', ...(target.dataset.key === 'overwrite'
      ? [!state.options.overwrite, null] : [null, !state.options.auto])); break;
    case 'scan': api.call('scan'); break;
    case 'start':
      if (state.options.auto) {
        confirmModal('已开启全自动清理', 'RAR 验证通过后，会<strong>自动把本次全部 Word 移入回收站</strong>，不再等待你抽查 PDF。\n\n确定继续吗？',
          '开始', () => api.call('start'));
      } else api.call('start');
      break;
    case 'resume': api.call('resume'); break;
    case 'stop': api.call('stop'); break;
    case 'back':
      if (state.input) api.call('accept_input', state.input.path); else api.call('reset');
      break;
    case 'open-job': api.call('open_job_dir'); break;
    case 'open-output': api.call('open_output'); break;
    case 'select-all': api.call('select_all', true); break;
    case 'clear-all': api.call('select_all', false); break;
    case 'cleanup': {
      const n = state.counts.selected;
      const outcome = state.isArchiveInput
        ? '任务完成后自动清理解压目录，原压缩包和最终 RAR 会保留。'
        : 'PDF 和最终 RAR 会保留，工作副本和备份自动清理。';
      confirmModal('确认清理', `已生成并验证 RAR。请确认已抽查 PDF 的排版、图片和内容。\n\n将选中的 <strong>${n} 份 Word</strong> 移入回收站？\n${esc(outcome)}`,
        '移入回收站', () => api.call('cleanup'));
      break;
    }
    case 'toggle-log':
      logOpen = !logOpen;
      $('log-drawer').hidden = !logOpen;
      if (logOpen) { const l = $('log-list'); l.scrollTop = l.scrollHeight; }
      document.querySelectorAll('.log-toggle').forEach((b) => b.setAttribute('aria-expanded', String(logOpen)));
      break;
    case 'modal-cancel': closeModal(); break;
    case 'modal-ok': { const fn = modalHandler; closeModal(); if (fn) fn(); break; }
    default: break;
  }
});

document.addEventListener('change', (event) => {
  const box = event.target.closest('input[data-action="select"]');
  if (box) api.call('select', box.dataset.source, box.checked);
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (!$('modal').hidden) closeModal();
    else if (logOpen) { logOpen = false; $('log-drawer').hidden = true; }
  }
});

// Drag & drop: the browser only needs to accept the drop; the real path is read in Python
// (pywebview exposes pywebviewFullPath there), so nothing is uploaded or read in JS.
let dragDepth = 0;
const dropzone = () => document.getElementById('dropzone');
document.addEventListener('dragenter', (e) => { e.preventDefault(); dragDepth += 1; dropzone()?.classList.add('over'); });
document.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) dropzone()?.classList.remove('over'); });
document.addEventListener('dragover', (e) => { e.preventDefault(); if (state && state.busy) e.dataTransfer.dropEffect = 'none'; });
document.addEventListener('drop', (e) => {
  e.preventDefault(); dragDepth = 0; dropzone()?.classList.remove('over');
  if (state && state.busy) toast('任务正在运行，请先停止。');
});

/* ---------------- browser preview (no pywebview): ?demo=start|process|waiting|review|error ---------------- */
(function demo() {
  const mode = new URLSearchParams(location.search).get('demo');
  if (!mode) return;
  const now = Date.now() / 1000;
  const names = ['申报书/技术报告.docx', '申报书/项目概况.docx', '申报书/申请表.docx', '附件/材料清单.docx', '附件/检测报告.docx', '附件/说明书.doc'];
  const files = names.map((n, i) => ({ source: 'D:\\业务资料\\2026-11 注册申报\\' + n, name: n, pages: mode === 'start' ? 0 : i % 4 + 1,
    status: mode === 'review' ? `校验通过 · ${i % 4 + 1} 页` : '已导出，待解密校验', verified: mode === 'review', recycled: false, selected: mode === 'review' }));
  const base = {
    busy: false, busyKind: '', phase: 'new', displayPhase: 'new', statusText: '',
    input: { path: 'D:\\业务资料\\2026-11 注册申报', name: '2026-11 注册申报', kind: 'folder' },
    processingFolder: 'D:\\业务资料\\2026-11 注册申报', options: { overwrite: true, auto: false },
    files: [], counts: { words: 6, verified: 0, selected: 0, recycled: 0, pdfs: 6 },
    progress: { indeterminate: false, current: 0, total: 0 }, archive: null, isArchiveInput: false,
    canResume: false, canCleanup: false, logs: [{ time: '09:41:02', text: 'WPS 会话已启动，共 6 份' }, { time: '09:41:37', text: '已向绿盾提交目录解密申请' }],
    error: null, notice: null, cleanupWarning: '', startedAt: now - 102,
  };
  const presets = {
    start: { counts: { ...base.counts, words: 0 }, startedAt: null },
    process: { busy: true, busyKind: 'run', phase: 'converting', displayPhase: 'converting', statusText: '正在调用 WPS 转换', files, progress: { indeterminate: false, current: 4, total: 6 } },
    waiting: { phase: 'waiting', displayPhase: 'waiting', statusText: '等待绿盾解密批准', files, canResume: true },
    error: { phase: 'error', displayPhase: 'error', statusText: '遇到问题', files, error: { title: '流程已停止', message: '已有 3 份同名 PDF，不能直接作为本次转换结果。请先备份移走，或勾选“备份旧 PDF 后重新转换”。', jobDir: 'x' } },
    review: { phase: 'review', displayPhase: 'review', statusText: '', files, canCleanup: true,
      counts: { words: 6, verified: 6, selected: 6, recycled: 0, pdfs: 9 },
      archive: { path: 'D:\\业务资料\\2026-11 注册申报-PDF.rar', name: '2026-11 注册申报-PDF.rar', folder: 'D:\\业务资料' } },
  };
  if (mode === 'error') lastStep = 2;
  render({ ...base, ...(presets[mode] || {}) });
})();
