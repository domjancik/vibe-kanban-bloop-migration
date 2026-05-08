# Migrating off Bloop's hosted Vibe Kanban

Bloop AI announced the shutdown of `vibekanban.com` in April 2026. This repo
documents the practical migration path I used to move my organisation off the
hosted cloud onto a self-hosted single-user instance, including a workaround
for a gap in the official export.

If you're staring at the same problem, the scripts and console snippet here
should save you a few hours. They are **not polished tools** — they're what I
ran. Read each one before pasting it into your own setup.

## Background

Vibe Kanban's hosted "Cloud" stores the canonical state of projects, issues,
and the metadata of every workspace ever opened against an issue. The desktop
client owns the actual git worktrees and execution state in a local SQLite,
and pushes a small `workspaces` row to the cloud per worktree to keep the
two halves linked.

Bloop ships a built-in **Export** button that writes a ZIP with:

| file | contents |
|---|---|
| `projects.csv` | name + timestamps |
| `issues.csv` | simple_id, title, description, status, priority, parent, dates, assignees |
| `users.csv` | email + name |
| `attachments.csv` + files | issue attachments |

That's enough to recreate your kanban — but **the `workspaces` table is not
in the export**, so every issue↔workspace link is lost. If you don't care
about that linkage, only step 1 below applies. If you do (and the work was
real), step 2 closes the gap with no cooperation needed from Bloop.

## Architecture this assumes

```
self-hosted Postgres  ─┐
self-hosted ElectricSQL ├─ Vibe Kanban "remote" (the cloud half)
self-hosted remote-server ─┘                  http://localhost:3000
                       ↑
                       │  VK_SHARED_API_BASE
                       │
              desktop app on your Mac (the host that runs workspaces)
```

You'll need this stack running before importing anything. The simplest path
I've seen is the bootstrap repo at
[`domjancik/vibe-kanban-starter`](https://github.com/domjancik/vibe-kanban-starter)
— `make start` and you're done. Otherwise
follow [Vibe Kanban's own self-hosting docs](https://github.com/BloopAI/vibe-kanban/tree/main/docs/self-hosting).

The scripts here assume the self-hosted Postgres is reachable at
`localhost:5433` with user `remote`, database `remote`, password `remote`
(the defaults from `crates/remote/docker-compose.yml`). All four are
overridable via `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, `PGPASSWORD`.

You'll also need to have signed in once to your self-hosted instance using
the bootstrap local-auth credentials — that creates the `users` row and the
personal organization the importers attach to.

## Step 1: import projects + issues from the Bloop CSV

Hit Export in vibekanban.com (Settings → Export), unzip it somewhere
predictable, then:

```bash
BLOOP_EXPORT_DIR=~/Downloads/vibe-kanban-export \
ORG_ISSUE_PREFIX=DOM \
python3 import-bloop-export.py
```

Replace `DOM` with the prefix that appears at the start of your `Issue ID`
column (`grep -m1 ',' issues.csv` shows the second row).

What it does:

1. Looks up your `admin@local` user and personal org in the self-hosted DB.
2. Sets `organizations.issue_prefix` so future issues continue your numbering.
3. Inserts each unique project name (deduping if you happen to have repeats —
   I had two `cf-mono` rows from old org migrations).
4. Backfills the six default project statuses (`Backlog`, `To do`, …).
5. Disables the `set_issue_simple_id` trigger and inserts each issue with the
   original `issue_number` and `simple_id` preserved. Status, priority,
   description, dates, completion all carry over.
6. Second pass walks rows that had a `Parent Issue` and resolves the FK to the
   imported parent.
7. Bumps `organizations.issue_counter` to your global max so newly created
   issues get fresh numbers without colliding with imported ones.

The script bails out if any issues already exist for your org (so it's safe
to re-run while you're still figuring out the prefix).

**Doesn't touch:** assignees, comments, tags, followers, attachments. Those
are either not in the CSV (comments) or would need extra wiring (attachments
need an Azurite/blob backend).

## Step 2: dump workspace↔issue links from Bloop's API

The hardest part of the migration. Approach:

1. Launch the locally running Vibe Kanban desktop app against Bloop's hosted
   service: `VK_SHARED_API_BASE=https://vibekanban.com npx vibe-kanban`.
2. Sign into vibekanban.com through that local app.
3. Open DevTools → Console in the app window.
4. Paste the contents of `dump-workspace-links.js` and hit Enter.
5. The app downloads `workspace-links.csv`.

The snippet:

1. Mints a short-lived access token via `/api/auth/token` (which uses your
   refresh-token cookie). Refreshes mid-loop because tokens expire after ~2
   minutes — long iterations otherwise 401.
2. Lists your organisations (via the local proxy at `/api/organizations`).
3. For each org, hits the cloud's ElectricSQL shape proxy directly at
   `https://api.vibekanban.com/v1/shape/projects?organization_id=...&offset=-1`
   to enumerate cloud project UUIDs and names.
4. For each project, fetches `/v1/shape/project/{id}/issues?offset=-1` to
   build an `issue_uuid → simple_id` map (the missing piece — issues.csv has
   simple_ids but not UUIDs, workspaces have UUIDs but not simple_ids).
5. Fetches `/v1/shape/user/workspaces?offset=-1` plus
   `/v1/shape/project/{id}/workspaces?offset=-1` per project, dedupes on
   workspace UUID, joins to project name + simple_id.
6. Writes a CSV with columns `Local Workspace ID`, `Name`, `Project`,
   `Issue ID` (the simple_id), `Archived`, diff stats, timestamps.

`Local Workspace ID` is the UUID the desktop client stores in its SQLite as
`workspaces.id`, which the cloud's `workspaces.local_workspace_id` references
as a UUID FK. Preserving it is what keeps the desktop ↔ cloud bridge intact
after the migration.

### Caveats with the dump

- **Owner filter.** `/v1/shape/user/workspaces` is filtered to your user.
  The script unions with `/v1/shape/project/{id}/workspaces` (no owner filter)
  to catch any workspaces created by teammates. In a single-user account
  this makes no difference.
- **Not every local workspace shows up.** The Bloop client only pushes a
  workspace row to the cloud once it's been wired to a task. In my data,
  267 local workspaces yielded 193 cloud rows — the gap is workspaces that
  were never task-linked, not data loss.
- **Confirm a few "missing" ones really are missing** with
  `HEAD https://api.vibekanban.com/v1/workspaces/exists/<local_uuid>`.
  404 on those local-only IDs confirms they were genuinely never synced.

### Then import the links

```bash
python3 import-workspace-links.py ~/Downloads/workspace-links.csv
```

What it does:

1. Looks up admin user + personal org in self-hosted DB.
2. Builds `name → project_id` and `simple_id → issue_id` lookups against the
   already-imported data.
3. UPSERTs each row into `workspaces` keyed on `local_workspace_id`. Issue
   linkage resolved via simple_id; project via name.
4. `owner_user_id` is set to the local admin (the original Bloop user UUID
   isn't carried over).

Re-runnable: the `ON CONFLICT (local_workspace_id) DO UPDATE` clause keeps
it idempotent if you re-dump and re-run.

## After import

Force ElectricSQL to re-publish shapes so the desktop UI sees the bulk
inserts (without this you may see an empty UI even though the DB is full):

```bash
cd /path/to/vibe-kanban/crates/remote
docker compose --env-file ../../.env.remote restart electric
```

Reload the desktop app fully (Cmd+R or quit/relaunch) to invalidate its
client-side shape cache.

## Files

| file | purpose |
|---|---|
| [`dump-workspace-links.js`](dump-workspace-links.js) | DevTools console snippet that walks Bloop's shape API and downloads `workspace-links.csv`. |
| [`import-bloop-export.py`](import-bloop-export.py) | CSV → Postgres importer for projects + issues. |
| [`import-workspace-links.py`](import-workspace-links.py) | Workspace-links CSV → Postgres importer. |

## Schema gotchas worth knowing

- The cloud's `issue_counter` migrated from `projects.issue_counter` to
  `organizations.issue_counter` somewhere around early 2026 (migration
  `20260313000000_fix-short-id-counter.sql`). Simple_ids are now globally
  unique per org; the script targets the new schema.
- The `set_issue_simple_id` trigger is disabled inside a transaction during
  the import so we can keep the original `DOM-N` numbers and not jumble them
  into trigger-assigned new ones.
- Statuses are per-project rows, not enum values. The import creates the
  six standard ones; if your cloud project had custom statuses, those don't
  carry through this CSV format and are remapped to defaults by name.

## License

MIT. Take it, fork it, fix the bits I left rough.
