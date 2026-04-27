#!/usr/bin/env python3
"""
import-workspace-links.py — imports workspace-links.csv (dumped from the Bloop
cloud via dump-workspace-links.js) into the self-hosted Postgres workspaces table.

Pre-conditions:
  1. Docker stack running (Postgres on localhost:5433).
  2. admin@local signed in (user + personal org exist).
  3. Issues already imported (import-bloop-export.py was run) so we can resolve
     simple_id -> issue_id.
  4. workspace-links.csv sitting in ~/Downloads/ (or pass path as argv[1]).

What it does:
  - Resolves each row's Project name -> project_id and Issue ID (simple_id) -> issue_id.
  - INSERTs a row into `workspaces` with preserved `local_workspace_id` so the
    user's desktop app (which has the matching UUID in its SQLite) can attach.
  - owner_user_id = admin@local (we don't carry over the original Bloop user).

Usage:
  python3 ~/vibe-kanban-data/import-workspace-links.py [path/to/workspace-links.csv]
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_CSV = Path.home() / "Downloads" / "workspace-links.csv"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@local")

PG_HOST = os.environ.get("PGHOST", "localhost")
PG_PORT = os.environ.get("PGPORT", "5433")
PG_USER = os.environ.get("PGUSER", "remote")
PG_DB = os.environ.get("PGDATABASE", "remote")
PG_ENV = {**os.environ, "PGPASSWORD": os.environ.get("PGPASSWORD", "remote")}
PG_ARGS = [
    "psql",
    "-h", PG_HOST,
    "-p", PG_PORT,
    "-U", PG_USER,
    "-d", PG_DB,
    "-v", "ON_ERROR_STOP=1",
    "-tAX",
]


def q(s: str | None) -> str:
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def qts(s: str | None) -> str:
    if not s:
        return "NULL"
    return q(s) + "::timestamptz"


def run_sql(sql: str) -> str:
    res = subprocess.run(
        PG_ARGS, env=PG_ENV, input=sql, text=True, capture_output=True
    )
    if res.returncode != 0:
        sys.stderr.write(f"psql failed ({res.returncode}):\n{res.stderr}\n")
        sys.exit(1)
    return res.stdout.strip()


def main():
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not csv_path.exists():
        sys.exit(f"missing csv: {csv_path}")

    out = run_sql(
        f"""
        SELECT u.id::text, o.id::text
        FROM users u
        JOIN organization_member_metadata m ON m.user_id = u.id
        JOIN organizations o ON o.id = m.organization_id
        WHERE u.email = {q(ADMIN_EMAIL)} AND o.is_personal = TRUE
        ORDER BY o.created_at ASC
        LIMIT 1;
        """
    )
    if not out:
        sys.exit(f"no personal org found for {ADMIN_EMAIL}")
    user_id, org_id = out.split("|")
    print(f"user={user_id}  org={org_id}")

    proj_rows = run_sql(
        f"SELECT name, id::text FROM projects WHERE organization_id = '{org_id}';"
    )
    proj_name_to_id: dict[str, str] = {}
    for line in proj_rows.splitlines():
        name, pid = line.split("|", 1)
        proj_name_to_id[name] = pid

    issue_rows = run_sql(
        f"""
        SELECT i.simple_id, i.id::text
        FROM issues i
        JOIN projects p ON p.id = i.project_id
        WHERE p.organization_id = '{org_id}';
        """
    )
    simple_to_issue_id: dict[str, str] = {}
    for line in issue_rows.splitlines():
        sid, iid = line.split("|", 1)
        simple_to_issue_id[sid] = iid

    with csv_path.open() as f:
        rows = list(csv.DictReader(f))

    print(f"Parsed {len(rows)} workspace rows")

    missing_project: list[tuple[str, str]] = []
    missing_issue: list[tuple[str, str]] = []
    missing_local_id: list[str] = []
    sql = ["BEGIN;"]
    inserted = 0

    for row in rows:
        local_id = row.get("Local Workspace ID", "").strip()
        name = row.get("Name", "").strip()
        project = row.get("Project", "").strip()
        issue_simple = row.get("Issue ID", "").strip()
        archived = (row.get("Archived", "").strip().lower() == "true")
        files_changed = row.get("Files Changed", "").strip() or None
        lines_added = row.get("Lines Added", "").strip() or None
        lines_removed = row.get("Lines Removed", "").strip() or None
        created = row.get("Created", "").strip() or None
        updated = row.get("Updated", "").strip() or None

        if not local_id:
            missing_local_id.append(name or "<unnamed>")
            continue

        if project not in proj_name_to_id:
            missing_project.append((local_id, project))
            continue
        pid = proj_name_to_id[project]

        issue_id_literal = "NULL"
        if issue_simple:
            iid = simple_to_issue_id.get(issue_simple)
            if iid is None:
                missing_issue.append((local_id, issue_simple))
                continue
            issue_id_literal = f"'{iid}'"

        sql.append(
            f"""
            INSERT INTO workspaces (
                project_id, owner_user_id, issue_id, local_workspace_id,
                name, archived, files_changed, lines_added, lines_removed,
                created_at, updated_at
            ) VALUES (
                '{pid}', '{user_id}', {issue_id_literal}, '{local_id}',
                {q(name) if name else "NULL"}, {str(archived).lower()},
                {files_changed or "NULL"}, {lines_added or "NULL"}, {lines_removed or "NULL"},
                {qts(created) if created else "NOW()"}, {qts(updated) if updated else "NOW()"}
            )
            ON CONFLICT (local_workspace_id) DO UPDATE SET
                issue_id = EXCLUDED.issue_id,
                name = EXCLUDED.name,
                archived = EXCLUDED.archived,
                files_changed = EXCLUDED.files_changed,
                lines_added = EXCLUDED.lines_added,
                lines_removed = EXCLUDED.lines_removed,
                updated_at = EXCLUDED.updated_at;
            """
        )
        inserted += 1

    sql.append("COMMIT;")
    run_sql("\n".join(sql))
    print(f"INSERT/UPSERT queued for {inserted} workspaces")
    for lid, p in missing_project[:10]:
        print(f"  - local_id={lid}: project {p!r} not in DB")
    for lid, si in missing_issue[:10]:
        print(f"  - local_id={lid}: issue {si} not in DB (skipped)")
    for n in missing_local_id[:10]:
        print(f"  - no Local Workspace ID: {n!r}")

    count = run_sql(
        f"""
        SELECT COUNT(*)::text FROM workspaces w
        JOIN projects p ON p.id = w.project_id
        WHERE p.organization_id = '{org_id}';
        """
    )
    print(f"Total workspaces in DB now: {count}")


if __name__ == "__main__":
    main()
