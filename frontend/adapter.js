/* Same-origin connection. Opening index.html directly retains sample preview mode. */
(() => {
  if (!/^https?:$/.test(location.protocol) || new URLSearchParams(location.search).has('preview')) return;
  const base = '/api/dashboard';
  const progress = text => window.dispatchEvent(new CustomEvent('digest-progress', { detail: text }));
  async function request(path, body) {
    const response = await fetch(base + path, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Outlook-Digest': '1' },
      body: JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
    return result;
  }
  async function job(path, body) {
    const { job_id } = await request(path, body);
    // A network error stops polling, not the server job. Never retry a write automatically.
    for (;;) {
      const state = await request('/jobs/' + encodeURIComponent(job_id));
      progress(state.progress || state.status);
      if (state.status === 'failed') throw new Error(state.error || 'Job failed');
      if (state.status === 'complete') return state.result;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }
  function paths(tree) {
    return tree.flatMap(node => [node.path, ...paths(node.children || [])]);
  }
  async function initial() {
    const data = await request('/state');
    const tree = await request('/folders');
    data.folders = [...new Set([...data.folders, ...paths(tree)])];
    data.prompts = window.OutlookDigestSample.prompts.map(prompt => ({ ...prompt }));
    data.messages = await request('/messages/query', { limit: 500 });
    data.chains = await request('/chains?' + new URLSearchParams({ folder: data.folders[0] || '', limit: '500' }));
    return data;
  }
  window.OutlookDigestAdapter = {
    getInitialData: initial,
    queryMessages: filters => request('/messages/query', filters),
    getMessage: key => request('/message?' + new URLSearchParams({ key })),
    getChain: key => request('/chain?' + new URLSearchParams({ key })),
    getChains: folder => request('/chains?' + new URLSearchParams({ folder, limit: '500' })),
    refresh: async options => { await job('/refresh', options); return initial(); },
    refreshFolders: async () => paths(await job('/folders/refresh', {})),
    saveDisclaimers: settings => job('/disclaimers', settings),
    backfill: async settings => { await job('/clean', settings); return initial(); }
  };
})();
