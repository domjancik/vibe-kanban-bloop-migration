/**
 * Paste in the DevTools Console of the Vibe Kanban desktop app (or vibekanban.com
 * browser) while signed into your Bloop cloud account.
 *
 * Dumps cloud workspace↔issue links by hitting api.vibekanban.com's shape
 * endpoints (used by the app's real-time sync). Writes workspace-links.csv
 * into your Downloads folder.
 */

(async () => {
  const CLOUD = 'https://api.vibekanban.com';

  // Access tokens are short-lived (~2 min). Cache + refresh on demand.
  let cached = { token: null, exp: 0 };
  const getToken = async () => {
    if (cached.token && Date.now() < cached.exp - 15_000) return cached.token;
    const j = await (
      await fetch('/api/auth/token', { credentials: 'include' })
    ).json();
    const t = j?.data?.access_token || j?.access_token;
    if (!t) throw new Error('no access token — are you signed in?');
    const expAt = j?.data?.expires_at || j?.expires_at;
    cached = {
      token: t,
      exp: expAt ? new Date(expAt).getTime() : Date.now() + 60_000,
    };
    return t;
  };
  const authHeaders = async () => ({ Authorization: `Bearer ${await getToken()}` });

  // Shape responses are arrays of {key, value, headers}.  Filter insert ops.
  const fetchShape = async (path) => {
    const r = await fetch(`${CLOUD}${path}${path.includes('?') ? '&' : '?'}offset=-1`, {
      headers: await authHeaders(),
    });
    if (!r.ok) throw new Error(`${path} → ${r.status}`);
    const entries = await r.json();
    return entries
      .filter((e) => e.headers?.operation === 'insert')
      .map((e) => e.value);
  };

  // Orgs (via local proxy — works fine, returned your Org earlier)
  const orgRes = await (
    await fetch('/api/organizations', { headers: await authHeaders() })
  ).json();
  const organizations = (orgRes.data?.organizations || orgRes.organizations || orgRes.data || orgRes);
  if (!organizations?.length) throw new Error('no organizations');
  console.log(`orgs: ${organizations.map((o) => o.name).join(', ')}`);

  // Cloud projects (shape — org-scoped)
  const projectById = {};
  for (const org of organizations) {
    const projects = await fetchShape(`/v1/shape/projects?organization_id=${org.id}`);
    for (const p of projects) projectById[p.id] = p;
  }
  console.log(`projects: ${Object.keys(projectById).length}`);

  // Cloud issues per project (shape — project-scoped)
  const issueSimpleById = {};
  for (const pid of Object.keys(projectById)) {
    const issues = await fetchShape(`/v1/shape/project/${pid}/issues`);
    for (const i of issues) issueSimpleById[i.id] = i.simple_id;
  }
  console.log(`issues: ${Object.keys(issueSimpleById).length}`);

  // Cloud workspaces — union of user-scoped + per-project (catches workspaces
  // from teammates or with a different owner_user_id). Dedupe on workspace UUID.
  const byId = new Map();
  const userWs = await fetchShape('/v1/shape/user/workspaces');
  for (const w of userWs) byId.set(w.id, w);
  for (const pid of Object.keys(projectById)) {
    const ws = await fetchShape(`/v1/shape/project/${pid}/workspaces`);
    for (const w of ws) if (!byId.has(w.id)) byId.set(w.id, w);
  }
  const workspaces = [...byId.values()];
  console.log(
    `workspaces: ${workspaces.length} (user-owned: ${userWs.length}, total across all projects: ${workspaces.length})`
  );
  const archivedCount = workspaces.filter(
    (w) => String(w.archived).toLowerCase() === 'true'
  ).length;
  console.log(`  archived: ${archivedCount}, active: ${workspaces.length - archivedCount}`);

  const header = [
    'Local Workspace ID',
    'Name',
    'Project',
    'Issue ID',
    'Archived',
    'Files Changed',
    'Lines Added',
    'Lines Removed',
    'Created',
    'Updated',
  ];
  const rows = [header];
  for (const w of workspaces) {
    rows.push([
      w.local_workspace_id || '',
      w.name || '',
      projectById[w.project_id]?.name || '',
      w.issue_id ? issueSimpleById[w.issue_id] || '' : '',
      // Shape values are all strings; `archived` comes through as "true"/"false".
      String(w.archived).toLowerCase() === 'true' ? 'true' : 'false',
      w.files_changed ?? '',
      w.lines_added ?? '',
      w.lines_removed ?? '',
      w.created_at,
      w.updated_at,
    ]);
  }

  const escape = (v) => {
    const s = String(v ?? '');
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = rows.map((r) => r.map(escape).join(',')).join('\n');

  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'workspace-links.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  console.log(`wrote workspace-links.csv with ${workspaces.length} rows`);
})();
