#!/usr/bin/env python3
"""
import-bloop-export.py — imports Bloop VK Cloud CSV export into self-hosted Postgres.

Pre-conditions:
  1. Docker stack is running (Postgres exposed on host localhost:5433).
  2. admin@local has signed in at least once so the user + personal org exist.

What it does:
  - Sets the personal org's issue_prefix to 'DOM' (matches the export's DOM-N scheme).
  - Creates each unique project from projects.csv under the personal org.
  - Creates the 6 default statuses per project (Backlog, To do, In progress, In review, Done, Cancelled).
  - Temporarily disables the simple_id trigger, inserts issues with their original DOM-N numbers.
  - Second pass: sets parent_issue_id for issues that had a parent.
  - Raises every project's issue_counter to the global max so new issues get a fresh DOM-N.

What it skips:
  - Attachments (Azurite not running in this profile).
  - Assignees and creators (everything attributed to admin@local).
  - Comments (not in the export).
  - Tags, followers, relationships (not in the export).

Usage:
  python3 ~/vibe-kanban-data/import-bloop-export.py
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
from pathlib import Path

# Override any of these via environment variables before running.
EXPORT_DIR = Path(
    os.environ.get(
        "BLOOP_EXPORT_DIR",
        str(Path.home() / "Downloads" / "vibe-kanban-export"),
    )
)
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@local")
# Cloud-side org prefix you want to preserve for issue simple_ids (e.g. DOM-1, DOM-2).
# Look at the "Issue ID" column of issues.csv to find yours.
ORG_ISSUE_PREFIX = os.environ.get("ORG_ISSUE_PREFIX", "DOM")

PROJECTS_CSV = EXPORT_DIR / "projects.csv"
ISSUES_CSV = EXPORT_DIR / "issues.csv"

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

STATUS_MAP = {
    "To do": "To do",
    "In progress": "In progress",
    "In review": "In review",
    "Done": "Done",
    "Backlog": "Backlog",
    "Cancelled": "Cancelled",
}

PRIORITY_MAP = {
    "Urgent": "urgent",
    "High": "high",
    "Medium": "medium",
    "Low": "low",
    "": None,
}


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


def load_projects() -> list[str]:
    """Return deduped list of project names, preserving first-seen order."""
    seen: dict[str, None] = {}
    with PROJECTS_CSV.open() as f:
        for row in csv.DictReader(f):
            name = row["Name"].strip()
            if name and name not in seen:
                seen[name] = None
    return list(seen.keys())


def load_issues() -> list[dict]:
    with ISSUES_CSV.open() as f:
        return list(csv.DictReader(f))


def parse_issue_num(issue_id: str) -> int:
    m = re.match(r"^DOM-(\d+)$", issue_id.strip())
    if not m:
        raise ValueError(f"unexpected issue id: {issue_id!r}")
    return int(m.group(1))


def main():
    if not PROJECTS_CSV.exists() or not ISSUES_CSV.exists():
        sys.exit(f"missing CSVs under {EXPORT_DIR}")

    print("[1/6] Sanity checks: admin user + personal org exist...")
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
        sys.exit(
            f"no personal org found for {ADMIN_EMAIL}. "
            "Sign in to the app once to bootstrap the user + org, then re-run."
        )
    user_id, org_id = out.split("|")
    print(f"      user={user_id}  org={org_id}")

    existing_issue_count = run_sql(
        f"""
        SELECT COUNT(*)::text FROM issues i
        JOIN projects p ON p.id = i.project_id
        WHERE p.organization_id = '{org_id}';
        """
    )
    if int(existing_issue_count) > 0:
        sys.exit(
            f"abort: {existing_issue_count} issues already exist for this org. "
            "Re-running would duplicate. Wipe issues first with:\n"
            f"  PGPASSWORD=remote psql -h localhost -p 5433 -U remote remote "
            f"-c \"DELETE FROM issues i USING projects p WHERE i.project_id = p.id AND p.organization_id = '{org_id}';\""
        )

    projects = load_projects()
    issues = load_issues()
    print(
        f"[2/6] Parsed CSVs: {len(projects)} unique projects, {len(issues)} issues."
    )
    global_max = max(parse_issue_num(i["Issue ID"]) for i in issues)
    print(f"      global max issue number = {global_max}")

    print("[3/6] Setting org issue_prefix + creating projects + default statuses...")
    sql = [f"BEGIN;"]
    sql.append(
        f"UPDATE organizations SET issue_prefix = {q(ORG_ISSUE_PREFIX)} WHERE id = '{org_id}';"
    )
    for name in projects:
        sql.append(
            f"""
            INSERT INTO projects (id, organization_id, name)
            SELECT gen_random_uuid(), '{org_id}', {q(name)}
            WHERE NOT EXISTS (
                SELECT 1 FROM projects
                WHERE organization_id = '{org_id}' AND name = {q(name)}
            );
            """
        )
    # Ensure default statuses exist for every project that doesn't have them yet.
    sql.append(
        f"""
        INSERT INTO project_statuses (project_id, name, color, sort_order, hidden)
        SELECT p.id, s.name, s.color, s.sort_order, s.hidden
        FROM projects p
        CROSS JOIN (VALUES
            ('Backlog',      '220 9% 46%', 0, TRUE),
            ('To do',        '217 91% 60%', 1, FALSE),
            ('In progress',  '38 92% 50%',  2, FALSE),
            ('In review',    '258 90% 66%', 3, FALSE),
            ('Done',         '142 71% 45%', 4, FALSE),
            ('Cancelled',    '0 84% 60%',   5, TRUE)
        ) AS s(name, color, sort_order, hidden)
        WHERE p.organization_id = '{org_id}'
          AND NOT EXISTS (
            SELECT 1 FROM project_statuses ps
            WHERE ps.project_id = p.id AND ps.name = s.name
          );
        """
    )
    sql.append("COMMIT;")
    run_sql("\n".join(sql))

    # Build name→project_id map
    rows = run_sql(
        f"SELECT name, id::text FROM projects WHERE organization_id = '{org_id}';"
    )
    proj_name_to_id = {}
    for line in rows.splitlines():
        name, pid = line.split("|", 1)
        proj_name_to_id[name] = pid
    print(f"      project rows in DB: {len(proj_name_to_id)}")

    # Build (project_id, status_name) → status_id
    rows = run_sql(
        f"""
        SELECT ps.project_id::text, ps.name, ps.id::text
        FROM project_statuses ps
        JOIN projects p ON p.id = ps.project_id
        WHERE p.organization_id = '{org_id}';
        """
    )
    status_lookup: dict[tuple[str, str], str] = {}
    for line in rows.splitlines():
        pid, name, sid = line.split("|", 2)
        status_lookup[(pid, name)] = sid

    print("[4/6] Inserting issues (trigger disabled, explicit issue_number/simple_id)...")
    sql = [
        "BEGIN;",
        "ALTER TABLE issues DISABLE TRIGGER trg_issues_simple_id;",
    ]
    missing_project = []
    inserted = 0
    for row in issues:
        iid = row["Issue ID"].strip()
        num = parse_issue_num(iid)
        title = row["Title"].strip()[:255]
        desc = row["Description"]
        project = row["Project"].strip()
        status_name = STATUS_MAP.get(row["Status"].strip(), "Backlog")
        priority = PRIORITY_MAP.get(row["Priority"].strip(), None)

        if project not in proj_name_to_id:
            missing_project.append((iid, project))
            continue
        pid = proj_name_to_id[project]
        sid = status_lookup.get((pid, status_name))
        if sid is None:
            sys.exit(f"status {status_name!r} missing for project {project}")

        completed = row["Completed"] or None
        created = row["Created"] or None
        updated = row["Updated"] or None
        start_date = row["Start Date"] or None
        target_date = row["Due Date"] or None

        priority_literal = (
            f"{q(priority)}::issue_priority" if priority else "NULL"
        )

        sql.append(
            f"""
            INSERT INTO issues (
                project_id, issue_number, simple_id, status_id,
                title, description, priority,
                start_date, target_date, completed_at,
                sort_order, parent_issue_id, extension_metadata,
                creator_user_id, created_at, updated_at
            ) VALUES (
                '{pid}', {num}, {q(iid)}, '{sid}',
                {q(title)}, {q(desc) if desc else "NULL"}, {priority_literal},
                {qts(start_date)}, {qts(target_date)}, {qts(completed)},
                {num}, NULL, '{{}}'::jsonb,
                '{user_id}', {qts(created) if created else "NOW()"}, {qts(updated) if updated else "NOW()"}
            );
            """
        )
        inserted += 1

    sql.append("ALTER TABLE issues ENABLE TRIGGER trg_issues_simple_id;")
    sql.append("COMMIT;")
    run_sql("\n".join(sql))
    print(f"      queued {inserted} inserts (skipped {len(missing_project)} w/ unknown project)")
    if missing_project:
        for iid, p in missing_project[:10]:
            print(f"      - {iid} references unknown project {p!r}")

    print("[5/6] Resolving parent issues...")
    sql = ["BEGIN;"]
    parent_updates = 0
    for row in issues:
        parent = row["Parent Issue"].strip()
        if not parent:
            continue
        iid = row["Issue ID"].strip()
        project = row["Project"].strip()
        if project not in proj_name_to_id:
            continue
        pid = proj_name_to_id[project]
        # Parent may live in a different project; we look up by simple_id across the org.
        sql.append(
            f"""
            UPDATE issues c
            SET parent_issue_id = p.id
            FROM issues p
            JOIN projects pp ON pp.id = p.project_id
            WHERE pp.organization_id = '{org_id}'
              AND p.simple_id = {q(parent)}
              AND c.project_id = '{pid}'
              AND c.simple_id = {q(iid)};
            """
        )
        parent_updates += 1
    sql.append("COMMIT;")
    run_sql("\n".join(sql))
    print(f"      {parent_updates} parent-child links processed")

    print(f"[6/6] Setting org issue_counter to {global_max} (global max)...")
    run_sql(
        f"UPDATE organizations SET issue_counter = {global_max} WHERE id = '{org_id}';"
    )

    print("Done.")
    counts = run_sql(
        f"""
        SELECT p.name, COUNT(i.id)
        FROM projects p LEFT JOIN issues i ON i.project_id = p.id
        WHERE p.organization_id = '{org_id}'
        GROUP BY p.name ORDER BY COUNT(i.id) DESC, p.name;
        """
    )
    print("Per-project issue counts after import:")
    for line in counts.splitlines():
        name, count = line.split("|", 1)
        print(f"  {count:>4}  {name}")


if __name__ == "__main__":
    main()
