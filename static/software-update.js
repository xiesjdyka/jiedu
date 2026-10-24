(() => {
  const settings = document.getElementById('protection'), main = document.querySelector('body > main');
  if (!settings || !main) return;
  const card = document.createElement('section');
  card.className = 'card software-update-card';
  const heading = document.createElement('h2'); heading.textContent = '关于界渡与更新';
  const version = document.createElement('p'), message = document.createElement('p');
  message.setAttribute('role', 'status'); message.setAttribute('aria-live', 'polite');
  const detail = document.createElement('details'), summary = document.createElement('summary'), notes = document.createElement('p');
  summary.textContent = '版本说明'; detail.append(summary, notes);
  const actions = document.createElement('div'); actions.className = 'ui-update-notice__actions';
  const help = document.createElement('p'); help.className = 'software-update-help';
  help.textContent = '启动后会在后台检查。更新会先核验来源，等当前任务安全完成，再安装并重新打开界渡。';
  card.append(heading, version, message, detail, actions, help); settings.prepend(card);
  const notice = document.createElement('aside'); notice.className = 'ui-update-notice'; notice.hidden = true;
  notice.setAttribute('aria-label', '软件更新');
  const noticeTitle = document.createElement('h2'), noticeMessage = document.createElement('p');
  noticeMessage.setAttribute('role', 'status'); noticeMessage.setAttribute('aria-live', 'polite');
  const noticeActions = document.createElement('div'); noticeActions.className = 'ui-update-notice__actions';
  notice.append(noticeTitle, noticeMessage, noticeActions); main.prepend(notice);
  let state = null, actionPending = false, refreshPending = false, timer = null, wasRestarting = false;
  const activeStates = new Set(['queued', 'checking', 'downloading', 'verifying', 'waiting_idle', 'preparing', 'handoff_ready', 'applying', 'installing', 'restarting']);
  const buttonGroups = [actions, noticeActions].map(host => {
    const group = {};
    for (const [name, label] of [['check', '检查更新'], ['start', '更新并重启'], ['recover', '恢复旧版'], ['remind', '稍后提醒'], ['cancel', '取消本次更新']]) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
      button.className = name === 'start' ? 'primary' : 'secondary';
      button.addEventListener('click', () => perform(name)); host.append(button); group[name] = button;
    }
    return group;
  });
  buttonGroups[1].check.hidden = true;
  function errorText(error) {
    try { const row = JSON.parse(error.message); return typeof row.detail === 'string' ? row.detail : row.message || '暂时无法完成，请稍后重试。'; }
    catch { return error.message || '暂时无法完成，请稍后重试。'; }
  }
  function render(value) {
    const previous = state; state = value;
    const phase = value.phase || value.state;
    const active = activeStates.has(value.state) || activeStates.has(phase);
    const canStart = ['available', 'downloaded', 'failed'].includes(value.state) && !!value.latest;
    version.textContent = '当前版本 ' + (value.version || '读取中') + (value.latest ? ' · 最新版本 ' + value.latest : '');
    const progress = value.progress;
    const fraction = typeof progress === 'number' && Number.isFinite(progress) ? ' · ' + Math.max(0, Math.min(100, Math.round(progress))) + '%' : '';
    const text = (value.message || '更新状态暂不可用') + fraction;
    if (message.textContent !== text) message.textContent = text;
    if (noticeMessage.textContent !== text) noticeMessage.textContent = text;
    notes.textContent = value.notes || ''; detail.hidden = !value.notes;
    const remindAt = typeof value.remind_at === 'number' ? value.remind_at * 1000 : new Date(value.remind_at || 0).getTime();
    const needsRecovery = value.state === 'recovery_required';
    notice.hidden = !needsRecovery && (remindAt > Date.now() || (!canStart && !active && value.state !== 'failed'));
    noticeTitle.textContent = needsRecovery ? '需要恢复旧版' : active ? '正在更新界渡' : value.state === 'failed' ? '更新未完成' : '界渡有新版本';
    notice.dataset.state = value.state || '';
    for (const group of buttonGroups) {
      group.check.disabled = actionPending || active;
      group.start.hidden = !canStart || active;
      group.start.disabled = actionPending;
      group.start.textContent = value.state === 'failed' ? '重试更新' : '更新并重启';
      group.recover.hidden = !needsRecovery;
      group.recover.disabled = actionPending;
      group.remind.hidden = active || !canStart;
      group.remind.disabled = actionPending;
      group.cancel.hidden = !active || ['handoff_ready', 'applying', 'installing', 'restarting'].includes(phase);
      group.cancel.disabled = actionPending;
    }
    buttonGroups[1].check.hidden = true;
    if (value.state === 'restarting' || phase === 'restarting') wasRestarting = true;
    if (wasRestarting && previous && !active && value.version && value.version !== previous.version) {
      wasRestarting = false; window.location.reload();
    }
    schedule(active ? 1500 : 30000);
  }
  function schedule(delay) {
    clearTimeout(timer);
    if (!document.hidden || wasRestarting) timer = setTimeout(refresh, delay);
  }
  async function refresh() {
    if (refreshPending || actionPending) { schedule(1500); return; }
    refreshPending = true;
    try { render(await api('/api/software-update', {ttl: 0, force: true, scope: 'software-update'})); }
    catch (error) {
      if (wasRestarting) {
        message.textContent = noticeMessage.textContent = '界渡正在重新启动，连接恢复后会自动继续。';
        notice.hidden = false; schedule(1500);
      } else { message.textContent = errorText(error); schedule(30000); }
    } finally { refreshPending = false; }
  }
  async function perform(action) {
    if (actionPending) return;
    actionPending = true;
    buttonGroups.forEach(group => Object.values(group).forEach(button => button.disabled = true));
    try { render(await apiPost('/api/software-update/' + action, {})); }
    catch (error) { state = {...state, state: 'failed', phase: '', message: errorText(error)}; }
    finally { actionPending = false; if (state) render(state); else schedule(1500); }
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden && !wasRestarting) clearTimeout(timer); else refresh(); });
  new MutationObserver(() => { if (settings.classList.contains('active')) refresh(); })
    .observe(settings, {attributes: true, attributeFilter: ['class']});
  if (new URLSearchParams(location.search).get('open') === 'software-update') {
    document.querySelector('[data-page="protection"]')?.click();
    card.scrollIntoView({block: 'start'});
  }
  refresh();
})();
