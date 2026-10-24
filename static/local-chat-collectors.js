/* Explicit Owner entry for local conversation collection. */
(() => {
  const sources = [{id:'dsh',label:'DSH'}, {id:'claude',label:'Claude'}, {id:'cursor',label:'Cursor'},
    {id:'workbuddy',label:'WorkBuddy'}, {id:'opencode',label:'OpenCode'},
    {id:'zcode',label:'ZCode'}, {id:'kimi',label:'Kimi Work'}];
  const fourSources = new Set(['workbuddy','opencode','zcode','kimi']);
  const host = document.getElementById('localChatCollectorCards');
  if (!host) return;
  const safe = value => String(value ?? '').replace(/[&<>"']/g, ch =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[ch]));
  const url = (id, action) => '/api/local-chat-collectors/' + id + '/' + action;
  const states = new Map();
  const discoveries = new Map();
  const fail = error => {
    const raw = String(error?.message || error || '请求失败');
    try { return JSON.parse(raw).detail || raw; } catch { return raw; }
  };
  const localTime = value => {
    if (!value) return '尚未检查';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '时间待核对' : date.toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
  };
  function render(id) {
    const config = sources.find(item => item.id === id);
    const status = states.get(id);
    const discovery = discoveries.get(id);
    const card = document.getElementById('local-chat-' + id);
    if (!card) return;
    if (!status) { card.innerHTML = '<p>状态尚未取得。请刷新。</p>'; return; }
    const found = discovery?.sessions || [];
    const checked = status.history?.archived_messages || 0;
    const enabled = Boolean(status.enabled);
    const wasConnected = Boolean(status.ever_configured);
    const mode = status.policy || 'paused';
    const selectedIds = new Set(status.selected_session_ids || []);
    const profiles = status.available_profiles || [];
    const selectedProfile = discovery?.selected_profile || status.selected_profile || '';
    const issue = status.last_error || (discovery?.current_decode_errors ?
      '当前缓存有 ' + discovery.current_decode_errors + ' 条不可读记录；可读部分可接入，历史覆盖仍不完整。' : '');
    const pending = id === 'cursor' || fourSources.has(id) ? (status.history?.pending_turns || 0) : 0;
    const sourceMissing = enabled && status.source_detected === false;
    const lastCheck = Date.parse(status.last_sync_at || '');
    const stale = enabled && Number.isFinite(lastCheck) && Date.now() - lastCheck > 120000;
    const attention = enabled && (issue || (status.history?.unverified_turns || 0) > 0 ||
      (status.indexed_without_transcript?.in_scope_count || 0) > 0);
    const stateLabel = sourceMissing ? '来源暂不可用 · 设置已保留' :
      attention ? '需要检查' : stale ? '最近检查较久' :
      enabled ? '正在采集' : wasConnected ? '已暂停' : '等待接入';
    const attachments = status.attachments || {};
    const imageNote = attachments.available || attachments.missing ?
      ' · 图片已保存 ' + Number(attachments.available || 0) + ' 张' +
      (attachments.missing ? '，待核对 ' + Number(attachments.missing) + ' 条引用' : '') : '';
    const description = id === 'dsh' ?
      '仅保存用户正文和已完成的最终答复；思考、工具与系统记录不入档。' :
      id === 'cursor' ?
      '已完成的用户正文和最终答复进入标准档案，进度原文另存。来源移除后原档案和采集范围保留。' :
      fourSources.has(id) ?
      '真实读取后采集本机可读聊天；只保存已完成轮次的用户正文和最终答复。' :
      '仅覆盖本机当前可读缓存，不能代表云端全部历史。';
    card.innerHTML =
      '<div class="local-chat-card-head"><h3>' + config.label + '</h3><strong>' +
      safe(stateLabel) + '</strong></div>' +
      '<p class="local-chat-summary">已保存 ' + checked + ' 条消息 · 最近检查 ' + safe(localTime(status.last_sync_at)) +
      (pending ? ' · ' + safe(pending) + ' 个回合未结束' : '') + safe(imageNote) + '</p>' +
      '<details class="local-chat-details"><summary>查看采集范围与待核对项</summary><p>' + safe(description) + '</p>' +
      (issue ? '<p class="local-chat-issue" role="status">' + safe(issue) + '</p>' : '') +
      (pending ? '<p role="status">' + safe(pending) + ' 个回合仍在生成；最终答复尚未归档。</p>' : '') +
      '<p class="local-chat-boundary">' + safe(status.history_coverage || '') + '</p></details>' +
      (id === 'claude' && profiles.length > 1 ?
        '<label>选择 Claude 本机资料目录<select data-profile><option value="">请选择</option>' +
        profiles.map(profile => '<option value="' + safe(profile.id) + '"' +
          (profile.id === selectedProfile ? ' selected' : '') + '>' +
          safe(profile.label) + '</option>').join('') + '</select></label>' : '') +
      '<button type="button" class="copy-button" data-discover="' + id + '">查看本机会话</button>' +
      (enabled ? '<button type="button" class="copy-button" data-sync="' + id + '">立即检查</button>' +
        '<button type="button" class="copy-button" data-pause="' + id + '">暂停采集</button>' : '') +
      (discovery ? '<div class="local-chat-choice"><p>当前本机可读会话：' + found.length +
        (discovery.current_decode_errors ? '；另有 ' + discovery.current_decode_errors + ' 条缓存记录不可读' : '') + '</p>' +
        (found.length ? '<label><input type="radio" name="scope-' + id + '" value="selected"' +
          (mode !== 'all' ? ' checked' : '') + '>只采集勾选会话</label>' +
          '<label><input type="radio" name="scope-' + id + '" value="all"' +
          (mode === 'all' ? ' checked' : '') + '>采集当前及以后发现的本机会话</label>' +
          '<div class="local-chat-session-list">' + found.map(row =>
            '<label><input type="checkbox" data-session-id="' + safe(row.session_id) + '"' +
            (selectedIds.has(row.session_id) ? ' checked' : '') + '>' +
            config.label + ' · ' + safe(row.session_id.slice(0, 8)) +
            ' · 当前可见 ' + safe(row.available_messages ?? row.source_bytes ?? '未知') +
            (row.available_messages == null ? ' 字节' : ' 条缓存消息') + '</label>').join('') + '</div>' +
          '<label class="local-chat-confirm"><input type="checkbox" data-owner-confirm>我确认按上述范围保存本机聊天</label>' +
          '<button type="button" class="primary" data-enable="' + id + '">' +
          (enabled ? '更新采集范围' : '开始归档') + '</button>' : '<p>没有可选择的会话。</p>') +
        '</div>' : '') +
      '<p class="local-chat-feedback" role="status"></p>';
    card.querySelector('[data-discover]')?.addEventListener('click', async buttonEvent => {
      const button = buttonEvent.currentTarget; button.disabled = true;
      const profile = card.querySelector('[data-profile]')?.value || selectedProfile;
      if (id === 'claude' && status.source_ambiguous && !profile) {
        card.querySelector('.local-chat-feedback').textContent = '请先选择一个 Claude 资料目录。';
        button.disabled = false; return;
      }
      try { discoveries.set(id, await api(url(id,'discover') +
        (profile ? '?profile_id=' + encodeURIComponent(profile) : ''), {force:true,ttl:0})); render(id); }
      catch (error) { card.querySelector('.local-chat-feedback').textContent = fail(error); button.disabled = false; }
    });
    card.querySelector('[data-enable]')?.addEventListener('click', async buttonEvent => {
      const current = buttonEvent.currentTarget.closest('.local-chat-card');
      const confirm = current.querySelector('[data-owner-confirm]');
      const selected = [...current.querySelectorAll('[data-session-id]:checked')]
        .map(node => node.dataset.sessionId);
      const policy = current.querySelector('input[name="scope-' + id + '"]:checked')?.value || 'selected';
      if (!confirm?.checked || (policy === 'selected' && !selected.length)) {
        current.querySelector('.local-chat-feedback').textContent = '请勾选会话并确认归档范围。';
        return;
      }
      const button = buttonEvent.currentTarget; button.disabled = true;
      try {
        states.set(id, await apiPost(url(id,'configure'),
          {policy,selected_session_ids:selected,profile_id:discovery?.selected_profile || null,
           owner_confirmed:true}));
        render(id);
      } catch (error) { current.querySelector('.local-chat-feedback').textContent = fail(error); button.disabled = false; }
    });
    card.querySelector('[data-pause]')?.addEventListener('click', async event => {
      const button = event.currentTarget; button.disabled = true;
      try { states.set(id, await apiPost(url(id,'configure'),
        {policy:'paused',selected_session_ids:[],owner_confirmed:true})); render(id); }
      catch (error) { card.querySelector('.local-chat-feedback').textContent = fail(error); button.disabled = false; }
    });
    card.querySelector('[data-sync]')?.addEventListener('click', async event => {
      const button = event.currentTarget; button.disabled = true;
      try { states.set(id, await apiPost(url(id,'sync'),{owner_confirmed:true})); render(id); }
      catch (error) { card.querySelector('.local-chat-feedback').textContent = fail(error); button.disabled = false; }
    });
  }
  async function refresh() {
    let visible;
    try {
      const preference = await api('/api/client-access/visibility?t=' + Date.now(), {force:true,ttl:0});
      const hidden = new Set(preference.hidden_client_types || []);
      visible = sources.filter(source => !hidden.has(source.id));
    } catch {
      host.innerHTML = '<p role="status">暂时无法核对本机归档入口，请刷新重试。</p>';
      return;
    }
    host.innerHTML = visible.map(source =>
      '<article class="local-chat-card" id="local-chat-' + source.id +
      '"><h3>' + source.label + '</h3><p>正在读取状态…</p></article>').join('');
    await Promise.all(visible.map(async source => {
      try { states.set(source.id, await api(url(source.id,'status'),{force:true,ttl:0})); }
      catch { states.delete(source.id); }
      render(source.id);
    }));
  }
  document.getElementById('refreshLocalChatCollectors')?.addEventListener('click', refresh);
  const page = document.getElementById('access');
  if (page) new MutationObserver(() => {
    if (page.classList.contains('active')) refresh();
  }).observe(page, {attributes:true,attributeFilter:['class']});
  document.addEventListener('DOMContentLoaded', refresh, {once:true});
})();
