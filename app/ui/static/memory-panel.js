(() => {
  const get = (id) => document.getElementById(id);
  const summary = get('memorySummary');
  const date = (timestamp) => new Date(timestamp * 1000).toLocaleString('zh-CN');
  async function request(path, options) {
    const response = await fetch('/api/memory' + path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '操作失败，请重试');
    return result;
  }
  const line = (text, className = 'block-note') => {
    const element = document.createElement('p');
    element.className = className;
    element.textContent = text;
    return element;
  };
  async function refresh() {
    try {
      const result = await request('');
      summary.textContent = `${result.stats.facts} 条事实 · ${result.stats.indexed}/${result.stats.total} 段历史可检索` +
        (result.message ? ` · ${result.message}` : '');
      get('memoryFacts').replaceChildren(...result.facts.map((fact) => {
        const row = document.createElement('div');
        row.className = 'memory-fact';
        row.append(
          line(`${fact.key}：${fact.value}`, 'memory-fact-value'),
          line(`${date(fact.updated)} · ${fact.manual ? '你已确认' : '来自你的原话'}`, 'memory-fact-meta'),
        );
        if (fact.quote) row.append(line(`来源：“${fact.quote}”`, 'memory-fact-source'));
        const edit = document.createElement('button');
        edit.type = 'button'; edit.className = 'quiet-button'; edit.textContent = '修改';
        edit.onclick = () => { get('memoryKey').value = fact.key; get('memoryKey').readOnly = true; get('memoryValue').value = fact.value; get('memoryValue').focus(); };
        const remove = document.createElement('button');
        remove.type = 'button'; remove.className = 'quiet-button'; remove.textContent = '移出记忆';
        remove.onclick = async () => {
          try { await request(`/facts/${fact.id}`, {method: 'DELETE'}); await refresh(); }
          catch (error) { summary.textContent = error.message; }
        };
        const actions = document.createElement('div');
        actions.className = 'memory-fact-actions';
        actions.append(edit, remove);
        row.append(actions); return row;
      }));
    } catch (error) { summary.textContent = error.message; }
  }
  function clear() { get('memoryForm').reset(); get('memoryKey').readOnly = false; }
  get('cancelMemoryEdit').onclick = clear;
  get('refreshMemory').onclick = refresh;
  get('settingsButton').addEventListener('click', refresh);
  get('memoryForm').onsubmit = async (event) => {
    event.preventDefault();
    try {
      await request('/facts', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key: get('memoryKey').value, value: get('memoryValue').value})});
      clear(); await refresh();
    } catch (error) { summary.textContent = error.message; }
  };
  get('memorySearchForm').onsubmit = async (event) => {
    event.preventDefault();
    const results = get('memoryResults'); results.replaceChildren(line('正在查找…'));
    try {
      const data = await request('/search?q=' + encodeURIComponent(get('memoryQuery').value));
      results.replaceChildren(...(data.results.length ? data.results.flatMap((r) => [
        line(date(r.created)), line('你：' + r.user), line('她：' + r.assistant),
      ]) : [line('没有找到足够相关的记录。')]));
    } catch (error) { results.replaceChildren(line(error.message)); }
  };
})();
