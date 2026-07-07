/**
 * emotion_favour WebUI · 印象管理台 v2
 *
 * 列表展示精要信息 + 单行编辑按钮；
 * 点击编辑弹原生 <dialog>，内部完整呈现 favour + 12 维滑块；
 * 顶栏「+ 新增」按钮弹另一个 dialog，手动添加 record。
 *
 * vanilla JS，无框架。AstrBot 主 webui 注入 bridge 时优先用 bridge，
 * 降级走 fetch（本地开发调试）。
 */

// ==================== [1] 桥接 + 状态 ====================

const PLUGIN_NAME = 'emotion_favour';

// 桥接对象由后端注入的 bridge-sdk.js 提供。
// 注入时序晚于本脚本首次执行 → 用函数运行时读取，并轮询等待就绪。
function getBridge() {
  return window.AstrBotPluginPage || null;
}

const GROUP_LABELS = {
  volatile: '挥发组',
  standard: '标准组',
  sticky: '黏附组',
};

// user_id 白名单（与后端 _is_valid_userid 同步）
const USERID_RE = /^[a-zA-Z0-9_-]{1,64}$/;

// 维度 → 所属 group（供 chip 染色用，与后端 EMOTION_GROUPS 同步）
const DIM_TO_GROUP = {
  surprise: 'volatile', anticipation: 'volatile',
  joy: 'standard', anger: 'standard', disgust: 'standard', fear: 'standard',
  trust: 'sticky', sadness: 'sticky', guilt: 'sticky',
  shame: 'sticky', pride: 'sticky', envy: 'sticky',
};

const state = {
  personas: [],
  currentPersona: '',
  meta: null,             // {favour_min, favour_max, emotion_dimensions, emotion_display_names, emotion_groups, relationship_mode}
  records: [],            // 后端原始数据，唯一的真相源
  sortKey: 'favour',      // 'user_id' | 'favour' | 'updated_at'
  sortOrder: 'desc',      // 'asc' | 'desc'
};

// dialog 运行时上下文（打开时填，关闭时清）
let editCtx = null;       // { userId, snapshot, refs }
let createCtx = null;     // { refs }

// ==================== [2] API client ====================

// 从当前 URL 提取 asset_token（后端鉴权用，TTL 60s）。降级 fetch 必须手动附加。
function getAssetToken() {
  try {
    return new URLSearchParams(window.location.search).get('asset_token') || '';
  } catch (e) {
    return '';
  }
}

async function apiGet(endpoint, params = {}) {
  const bridge = getBridge();
  if (bridge && typeof bridge.apiGet === 'function') {
    return bridge.apiGet(endpoint, params);
  }
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
  });
  const token = getAssetToken();
  if (token) qs.set('asset_token', token);
  const query = qs.toString();
  const url = `/api/${PLUGIN_NAME}/${endpoint}${query ? '?' + query : ''}`;
  const resp = await fetch(url, { credentials: 'include' });
  return resp.json();
}

async function apiPost(endpoint, body = {}) {
  const bridge = getBridge();
  if (bridge && typeof bridge.apiPost === 'function') {
    return bridge.apiPost(endpoint, body);
  }
  const token = getAssetToken();
  const query = token ? `?asset_token=${encodeURIComponent(token)}` : '';
  const resp = await fetch(`/api/${PLUGIN_NAME}/${endpoint}${query}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'include',
  });
  return resp.json();
}

const api = {
  getAbout: () => apiGet('about'),
  getPersonas: () => apiGet('personas'),
  getRecords: (persona_id) => apiGet('records', { persona_id }),
  getRelationship: (persona_id, user_id, favour) =>
    apiGet('relationship', { persona_id, user_id, favour }),
  saveRecord: (payload) => apiPost('records/save', payload),
};

/** 等待 bridge-sdk 注入完成（最多轮询 2 秒）。降级模式立即返回。 */
async function waitBridgeReady() {
  for (let i = 0; i < 20; i++) {
    const bridge = getBridge();
    if (bridge && typeof bridge.ready === 'function') {
      try { await bridge.ready(); } catch (e) { /* 握手失败仍允许尝试调用 */ }
      return;
    }
    await new Promise(r => setTimeout(r, 100));
  }
}

// ==================== [3] 工具 ====================

function debounce(fn, ms) {
  let timer = null;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), ms);
  };
}

let toastTimer = null;
function showToast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + (type === 'error' ? 'error' : type === 'success' ? 'success' : '');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.className = 'toast';
  }, 2400);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

function formatDate(s) {
  if (!s) return '—';
  return s.length > 16 ? s.slice(0, 16) : s;  // 截到分钟
}

function rangeText(range) {
  return range && range.length === 2 ? `[${range[0]}, ${range[1]}]` : '';
}

// ==================== [4] 列表渲染 ====================

async function init() {
  await waitBridgeReady();
  try {
    const about = await api.getAbout();
    if (about.success) {
      document.getElementById('version-tag').textContent = about.version || '';
    }
    await loadPersonas();
    setupEventListeners();
    setupDialogs();
  } catch (e) {
    showError('初始化失败：' + (e?.message || e));
  }
}

async function loadPersonas() {
  const resp = await api.getPersonas();
  if (!resp.success) {
    showError(resp.error || '加载 persona 列表失败');
    return;
  }
  state.personas = resp.personas || [];
  const switcher = document.getElementById('persona-switcher');
  const select = document.getElementById('persona-select');

  if (state.personas.length === 0) {
    switcher.hidden = true;
    showEmpty();
    return;
  }

  // currentPersona 不在 personas 列表里（首次进入 / 切换后失效）→ 重置到第一个
  if (!state.currentPersona || !state.personas.includes(state.currentPersona)) {
    state.currentPersona = state.personas[0];
  }

  if (state.personas.length > 1) {
    const cur = state.currentPersona;
    select.innerHTML = state.personas.map(p =>
      `<option value="${escapeHtml(p)}"${p === cur ? ' selected' : ''}>${escapeHtml(p)}</option>`
    ).join('');
    switcher.hidden = false;
  } else {
    switcher.hidden = true;
  }

  await loadRecords();
}

async function loadRecords() {
  if (!state.currentPersona) {
    showEmpty();
    return;
  }
  showLoading();
  try {
    const resp = await api.getRecords(state.currentPersona);
    if (!resp.success) {
      showError(resp.error || '加载记录失败');
      return;
    }
    state.meta = {
      favour_min: resp.favour_min,
      favour_max: resp.favour_max,
      emotion_dimensions: resp.emotion_dimensions,
      emotion_display_names: resp.emotion_display_names,
      emotion_groups: resp.emotion_groups,
      relationship_mode: resp.relationship_mode,
    };
    state.records = resp.records || [];
    renderStats();
    renderRecords();
  } catch (e) {
    showError('加载记录异常：' + (e?.message || e));
  }
}

function renderStats() {
  document.getElementById('persona-tag').textContent = state.currentPersona;
  document.getElementById('user-count').textContent = state.records.length;
  if (state.meta) {
    document.getElementById('range-tag').textContent = `${state.meta.favour_min} ~ ${state.meta.favour_max}`;
    const modeTag = document.getElementById('mode-tag');
    const modeValue = document.getElementById('mode-value');
    modeTag.hidden = false;
    modeValue.textContent = state.meta.relationship_mode === 'advance'
      ? 'advance（区间制）'
      : 'simple（均分制）';
  }
}

function getSortedRecords() {
  const key = state.sortKey;
  const order = state.sortOrder === 'asc' ? 1 : -1;
  const sorted = [...state.records];
  sorted.sort((a, b) => {
    let av, bv;
    if (key === 'user_id') {
      av = a.user_id; bv = b.user_id;
      return order * String(av).localeCompare(String(bv));
    }
    if (key === 'updated_at') {
      av = a.updated_at || ''; bv = b.updated_at || '';
      return order * String(av).localeCompare(String(bv));
    }
    // favour
    av = Number(a.favour) || 0; bv = Number(b.favour) || 0;
    return order * (av - bv);
  });
  return sorted;
}

function renderRecords() {
  const list = document.getElementById('records-list');
  const empty = document.getElementById('empty-state');
  const error = document.getElementById('error-state');

  empty.hidden = true;
  error.hidden = true;

  updateSortChips();

  if (state.records.length === 0) {
    list.innerHTML = '';
    empty.hidden = false;
    if (state.personas && state.personas.length === 0) {
      empty.innerHTML = `
        <div class="empty-icon">📭</div>
        <div>当前实例下暂无任何印象记录</div>
        <div style="font-size: 12px; color: var(--text-muted); margin-top: 8px;">
          用户需先与 bot 对话产生印象数据后，才能在此页面查看与编辑
        </div>
      `;
    }
    return;
  }

  list.innerHTML = '';
  getSortedRecords().forEach(record => {
    list.appendChild(buildCard(record));
  });
}

function updateSortChips() {
  document.querySelectorAll('.sort-chip').forEach(chip => {
    const isActive = chip.dataset.key === state.sortKey;
    chip.classList.toggle('active', isActive);
    if (isActive) {
      chip.dataset.order = state.sortOrder;
    } else {
      delete chip.dataset.order;
    }
  });
}

function buildCard(record) {
  const el = document.createElement('div');
  el.className = 'record-card';
  el.dataset.userId = record.user_id;

  const row = document.createElement('div');
  row.className = 'card-row';
  row.innerHTML = `
    <span class="user-id">${escapeHtml(record.user_id)}</span>
    ${record.is_admin_override ? '<span class="admin-tag">管理员覆盖</span>' : ''}
    <span class="favour-display">${record.favour}</span>
    <span class="relationship-tag">${escapeHtml(record.relationship)}</span>
    <span class="range-tag">${escapeHtml(rangeText(record.relationship_range))}</span>
    <span class="updated-at">${formatDate(record.updated_at)}</span>
    <button class="btn btn-sm btn-edit" type="button">编辑</button>
  `;

  row.querySelector('.btn-edit').addEventListener('click', () => openEditDialog(record.user_id));

  el.appendChild(row);

  // 情感摘要行（按 group 顺序展开：volatile → standard → sticky）
  const emo = document.createElement('div');
  emo.className = 'card-emotions';
  const groups = state.meta?.emotion_groups || {};
  const dims = [
    ...(groups.volatile || []),
    ...(groups.standard || []),
    ...(groups.sticky || []),
  ];
  const displayNames = state.meta?.emotion_display_names || {};
  dims.forEach(dim => {
    const v = record.emotions?.[dim] ?? 0;
    const chip = document.createElement('span');
    chip.className = 'emotion-chip';
    chip.dataset.group = DIM_TO_GROUP[dim] || 'standard';
    chip.title = `${displayNames[dim] || dim}: ${v}`;
    chip.innerHTML = `<span class="chip-name">${escapeHtml(displayNames[dim] || dim)}</span><span class="chip-value">${v}</span>`;
    emo.appendChild(chip);
  });
  el.appendChild(emo);

  return el;
}

// ==================== [5] 编辑 Dialog ====================

function buildEmotionGrid(container, emotions) {
  const groups = state.meta?.emotion_groups || {};
  const displayNames = state.meta?.emotion_display_names || {};
  container.innerHTML = '';

  ['volatile', 'standard', 'sticky'].forEach(groupKey => {
    const dims = groups[groupKey] || [];
    if (dims.length === 0) return;

    const groupBox = document.createElement('div');
    groupBox.className = `emotion-group ${groupKey}`;
    groupBox.innerHTML = `<div class="group-label">${GROUP_LABELS[groupKey] || groupKey}</div>`;

    dims.forEach(dim => {
      const value = emotions[dim] ?? 0;
      const row = document.createElement('div');
      row.className = 'slider-row';
      row.innerHTML = `
        <label>${escapeHtml(displayNames[dim] || dim)}</label>
        <input type="range" min="0" max="100" value="${value}" data-dim="${dim}" style="--progress: ${value}%">
        <span class="slider-value">${value}</span>
      `;
      groupBox.appendChild(row);
    });

    container.appendChild(groupBox);
  });
}

/** 收集 grid 内所有 slider 的当前值 */
function collectEmotions(container) {
  const result = {};
  container.querySelectorAll('input[type="range"]').forEach(s => {
    result[s.dataset.dim] = parseInt(s.value, 10);
  });
  return result;
}

function bindSliderEvents(container) {
  container.querySelectorAll('input[type="range"]').forEach(slider => {
    const valueSpan = slider.parentElement.querySelector('.slider-value');
    slider.addEventListener('input', () => {
      const v = parseInt(slider.value, 10);
      valueSpan.textContent = v;
      slider.style.setProperty('--progress', `${v}%`);
    });
  });
}

function openEditDialog(userId) {
  const record = state.records.find(r => r.user_id === userId);
  if (!record) {
    showToast('未找到记录', 'error');
    return;
  }

  const dialog = document.getElementById('edit-dialog');
  const favourInput = document.getElementById('edit-favour');
  const favourMin = state.meta?.favour_min ?? -100;
  const favourMax = state.meta?.favour_max ?? 100;
  favourInput.min = favourMin;
  favourInput.max = favourMax;

  document.getElementById('edit-user-id').textContent = record.user_id;
  document.getElementById('edit-admin-tag').hidden = !record.is_admin_override;
  document.getElementById('edit-updated-at').textContent = '最后更新：' + formatDate(record.updated_at);
  favourInput.value = record.favour;
  document.getElementById('edit-relationship').textContent = record.relationship;
  document.getElementById('edit-range').textContent = rangeText(record.relationship_range);

  const grid = document.getElementById('edit-emotion-grid');
  buildEmotionGrid(grid, record.emotions || {});
  bindSliderEvents(grid);

  // 实时算关系名
  const updateRelationship = debounce(async (favour) => {
    try {
      const resp = await api.getRelationship(state.currentPersona, userId, favour);
      if (!resp.success) return;
      document.getElementById('edit-relationship').textContent = resp.name;
      document.getElementById('edit-range').textContent = rangeText(resp.range);
    } catch (e) { /* 静默 */ }
  }, 200);

  // 关闭时清理 listener：先把 input clone 替换掉
  const newFavourInput = favourInput.cloneNode(true);
  favourInput.parentNode.replaceChild(newFavourInput, favourInput);
  newFavourInput.addEventListener('input', (e) => {
    const v = parseInt(e.target.value, 10);
    if (Number.isNaN(v)) return;
    const clamped = Math.max(favourMin, Math.min(favourMax, v));
    if (clamped !== v) e.target.value = clamped;
    updateRelationship(clamped);
  });

  editCtx = {
    userId,
    snapshot: JSON.parse(JSON.stringify(record)),
  };

  dialog.showModal();
}

function closeEditDialog() {
  const dialog = document.getElementById('edit-dialog');
  if (dialog.open) dialog.close();
  editCtx = null;
}

async function onEditConfirm() {
  if (!editCtx) return;
  const { userId } = editCtx;
  const dialog = document.getElementById('edit-dialog');
  const favourInput = document.getElementById('edit-favour');
  const confirmBtn = document.getElementById('edit-confirm');
  const discardBtn = document.getElementById('edit-discard');

  const favour = parseInt(favourInput.value, 10);
  if (Number.isNaN(favour)) {
    showToast('好感度必须是整数', 'error');
    return;
  }
  const emotions = collectEmotions(document.getElementById('edit-emotion-grid'));

  confirmBtn.disabled = true;
  discardBtn.disabled = true;
  confirmBtn.textContent = '保存中...';

  try {
    const resp = await api.saveRecord({
      persona_id: state.currentPersona,
      user_id: userId,
      favour,
      emotions_absolute: emotions,
    });
    if (!resp.success) {
      showToast('保存失败：' + (resp.error || '未知错误'), 'error');
      return;
    }
    // 用返回 record 更新 state.records
    if (resp.record) {
      const idx = state.records.findIndex(r => r.user_id === userId);
      if (idx >= 0) state.records[idx] = resp.record;
      // 重渲染列表中这一行
      const oldCard = document.querySelector(`.record-card[data-user-id="${CSS.escape(userId)}"]`);
      if (oldCard) {
        const newCard = buildCard(resp.record);
        oldCard.replaceWith(newCard);
      }
    }
    closeEditDialog();
    showToast('已保存', 'success');
  } catch (e) {
    showToast('保存异常：' + (e?.message || e), 'error');
  } finally {
    confirmBtn.disabled = false;
    discardBtn.disabled = false;
    confirmBtn.textContent = '✓ 确认';
  }
}

// ==================== [6] 新增 Dialog ====================

function openCreateDialog() {
  const dialog = document.getElementById('create-dialog');
  const personaSelect = document.getElementById('create-persona');

  personaSelect.innerHTML = state.personas.map(p =>
    `<option value="${escapeHtml(p)}"${p === state.currentPersona ? ' selected' : ''}>${escapeHtml(p)}</option>`
  ).join('');

  document.getElementById('create-user-id').value = '';
  const favourInput = document.getElementById('create-favour');
  const favourMin = state.meta?.favour_min ?? -100;
  const favourMax = state.meta?.favour_max ?? 100;
  favourInput.min = favourMin;
  favourInput.max = favourMax;
  favourInput.value = 0;
  document.getElementById('create-relationship').textContent = '—';

  const grid = document.getElementById('create-emotion-grid');
  buildEmotionGrid(grid, {});
  bindSliderEvents(grid);

  // 折叠 emotions
  document.querySelector('.emotions-collapse').open = false;

  // 关系实时反馈
  const userIdForRel = '__new__';  // 后端用 user_id 算 admin override / 关系区间；新用户不是 admin
  const updateRelationship = debounce(async (favour) => {
    const personaId = personaSelect.value;
    try {
      const resp = await api.getRelationship(personaId, userIdForRel, favour);
      if (!resp.success) return;
      document.getElementById('create-relationship').textContent = resp.name;
    } catch (e) {}
  }, 200);

  const newFavourInput = favourInput.cloneNode(true);
  favourInput.parentNode.replaceChild(newFavourInput, favourInput);
  newFavourInput.addEventListener('input', (e) => {
    const v = parseInt(e.target.value, 10);
    if (Number.isNaN(v)) return;
    const clamped = Math.max(favourMin, Math.min(favourMax, v));
    if (clamped !== v) e.target.value = clamped;
    updateRelationship(clamped);
  });

  // 初始 favour=0 的关系名也拉一下
  updateRelationship(0);

  createCtx = {};
  dialog.showModal();
}

function closeCreateDialog() {
  const dialog = document.getElementById('create-dialog');
  if (dialog.open) dialog.close();
  createCtx = null;
}

async function onCreateSubmit() {
  const userId = document.getElementById('create-user-id').value.trim();
  const personaId = document.getElementById('create-persona').value;
  const favourInput = document.getElementById('create-favour');
  const favour = parseInt(favourInput.value, 10);

  if (!userId) {
    showToast('请填写 user_id', 'error');
    return;
  }
  if (!USERID_RE.test(userId)) {
    showToast('user_id 含非法字符（仅字母数字下划线短横线，1-64 位）', 'error');
    return;
  }
  if (Number.isNaN(favour)) {
    showToast('好感度必须是整数', 'error');
    return;
  }

  const emotions = collectEmotions(document.getElementById('create-emotion-grid'));
  const hasNonZero = Object.values(emotions).some(v => v !== 0);

  const submitBtn = document.getElementById('create-submit');
  const cancelBtn = document.getElementById('create-cancel');
  submitBtn.disabled = true;
  cancelBtn.disabled = true;
  submitBtn.textContent = '创建中...';

  try {
    const resp = await api.saveRecord({
      persona_id: personaId,
      user_id: userId,
      favour,
      emotions_absolute: hasNonZero ? emotions : null,
      create_only: true,
    });
    if (!resp.success) {
      // 409 = 已存在，特殊提示
      const msg = resp.error || '未知错误';
      showToast(msg, 'error');
      return;
    }
    // 如果是当前 persona，把新记录加进列表
    if (resp.record && resp.record.persona_id === state.currentPersona) {
      state.records.push(resp.record);
      // 简单方式：重新渲染整个列表
      renderRecords();
    }
    closeCreateDialog();
    showToast('已创建', 'success');
  } catch (e) {
    showToast('创建异常：' + (e?.message || e), 'error');
  } finally {
    submitBtn.disabled = false;
    cancelBtn.disabled = false;
    submitBtn.textContent = '创建';
  }
}

// ==================== [7] 顶栏 + Dialog 全局事件 ====================

function setupEventListeners() {
  // persona 切换
  document.getElementById('persona-select').addEventListener('change', async (e) => {
    state.currentPersona = e.target.value;
    await loadRecords();
  });

  // 刷新
  document.getElementById('refresh-btn').addEventListener('click', async () => {
    if (!state.currentPersona) {
      await loadPersonas();
      return;
    }
    await loadRecords();
    showToast('已刷新', 'success');
  });

  // 主题切换
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme') || 'light';
    const next = cur === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('astrbot-theme', next); } catch (e) {}
  });

  // 新增按钮
  document.getElementById('create-btn').addEventListener('click', () => {
    if (state.personas.length === 0) {
      showToast('当前实例暂无 persona 数据，无法新增', 'error');
      return;
    }
    openCreateDialog();
  });

  // 排序 chips
  document.querySelectorAll('.sort-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const key = chip.dataset.key;
      if (state.sortKey === key) {
        state.sortOrder = state.sortOrder === 'asc' ? 'desc' : 'asc';
      } else {
        state.sortKey = key;
        // 默认顺序：ID/时间用 desc（最新在前），favour 用 desc（高在前）
        state.sortOrder = 'desc';
      }
      renderRecords();
    });
  });
}

function setupDialogs() {
  // ===== 编辑 =====
  const editDialog = document.getElementById('edit-dialog');
  document.getElementById('edit-confirm').addEventListener('click', onEditConfirm);
  document.getElementById('edit-discard').addEventListener('click', closeEditDialog);
  document.getElementById('edit-close').addEventListener('click', closeEditDialog);

  // Esc / 遮罩点击：原生 dialog 的 close 事件已处理，无需额外
  editDialog.addEventListener('close', () => {
    editCtx = null;
  });
  // 点击 dialog 自身（backdrop 区域）关闭：原生 dialog 的 click outside 检测
  editDialog.addEventListener('click', (e) => {
    if (e.target === editDialog) closeEditDialog();
  });

  // ===== 新增 =====
  const createDialog = document.getElementById('create-dialog');
  document.getElementById('create-submit').addEventListener('click', onCreateSubmit);
  document.getElementById('create-cancel').addEventListener('click', closeCreateDialog);
  document.getElementById('create-close').addEventListener('click', closeCreateDialog);

  createDialog.addEventListener('close', () => {
    createCtx = null;
  });
  createDialog.addEventListener('click', (e) => {
    if (e.target === createDialog) closeCreateDialog();
  });
}

// ==================== [8] 状态显示 ====================

function showLoading() {
  document.getElementById('records-list').innerHTML =
    '<div class="loading-wrap"><span class="spinner"></span> 加载中...</div>';
  document.getElementById('empty-state').hidden = true;
  document.getElementById('error-state').hidden = true;
}

function showEmpty() {
  document.getElementById('records-list').innerHTML = '';
  const emptyEl = document.getElementById('empty-state');
  emptyEl.hidden = false;
  if (state.personas && state.personas.length === 0) {
    emptyEl.innerHTML = `
      <div class="empty-icon">📭</div>
      <div>当前实例下暂无任何印象记录</div>
      <div style="font-size: 12px; color: var(--text-muted); margin-top: 8px;">
        用户需先与 bot 对话产生印象数据后，才能在此页面查看与编辑
      </div>
    `;
  } else {
    emptyEl.innerHTML = `
      <div class="empty-icon">📭</div>
      <div>当前人格下暂无印象记录</div>
      <div style="font-size: 12px; color: var(--text-muted); margin-top: 8px;">
        可点击右上角「+」按钮手动新增
      </div>
    `;
  }
  document.getElementById('error-state').hidden = true;
}

function showError(msg) {
  document.getElementById('records-list').innerHTML = '';
  document.getElementById('empty-state').hidden = true;
  document.getElementById('error-state').hidden = false;
  document.getElementById('error-msg').textContent = msg;
}

// ==================== [9] 启动 ====================

document.addEventListener('DOMContentLoaded', init);
