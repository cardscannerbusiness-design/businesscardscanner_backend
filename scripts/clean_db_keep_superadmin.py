#!/usr/bin/env python3
"""Wipe all application data in PostgreSQL — keep only SuperAdmin credentials.

Keeps:
  - roles / permissions / role_permissions
  - the SuperAdmin user (SUPERADMIN_EMAIL from .env)

Deletes:
  - contacts, invitations, offline queue
  - sessions, refresh tokens, password/email tokens, audit logs
  - all non–SuperAdmin users
  - all companies

Usage (from BusinessCardScanner_Backend):
  python scripts/clean_db_keep_superadmin.py --yes

Optional:
  SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD in .env (re-seeds SuperAdmin if missing)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.env_loader import load_env  # noqa: E402

load_env()


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (name,),
    )
    return cur.fetchone() is not None


def _count(cur, table: str, where: str = "TRUE", params: tuple | list = ()) -> int:
    if not _table_exists(cur, table):
        return 0
    cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)
    row = cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def _delete(cur, table: str, where: str = "TRUE", params: tuple | list = ()) -> int:
    if not _table_exists(cur, table):
        print(f"  skip (missing table): {table}")
        return 0
    before = _count(cur, table, where, params)
    cur.execute(f"DELETE FROM {table} WHERE {where}", params)
    print(f"  deleted {before:>6} from {table}")
    return before


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean all DB data except SuperAdmin credentials.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required confirmation flag to run the wipe.",
    )
    args = parser.parse_args()

    if not args.yes and os.getenv("CONFIRM", "").strip().upper() != "YES":
        print(
            "Refusing to run without confirmation.\n"
            "  python scripts/clean_db_keep_superadmin.py --yes",
            file=sys.stderr,
        )
        return 1

    if not os.getenv("DATABASE_URL", "").strip():
        print("DATABASE_URL is not set in .env", file=sys.stderr)
        return 1

    from db.pool import close_pool, db_cursor, init_pool
    from db.seed import run_seed

    email = (os.getenv("SUPERADMIN_EMAIL") or "superadmin@ulavi.com").strip().lower()

    init_pool()
    try:
        with db_cursor(commit=True) as cur:
            # Resolve SuperAdmin id(s) to keep
            keep_ids: list[str] = []
            if _table_exists(cur, "users") and _table_exists(cur, "roles"):
                cur.execute(
                    """
                    SELECT u.id::text AS id, u.email, r.name AS role_name
                    FROM users u
                    JOIN roles r ON r.id = u.role_id
                    WHERE u.deleted_at IS NULL
                      AND (
                        lower(u.email) = %s
                        OR upper(r.name) = 'SUPER_ADMIN'
                      )
                    """,
                    (email,),
                )
                rows = cur.fetchall() or []
                for row in rows:
                    row = dict(row)
                    keep_ids.append(str(row["id"]))
                    print(
                        f"Keeping SuperAdmin: {row.get('email')} "
                        f"(role={row.get('role_name')}, id={row['id']})",
                    )

            if not keep_ids:
                print(
                    f"No SuperAdmin found for {email!r} yet — "
                    "will wipe data then re-seed SuperAdmin from .env.",
                )

            print("\nCleaning application data…")

            # Child / transactional tables first (FK-safe)
            _delete(cur, "contacts")
            _delete(cur, "offline_queue_records")
            _delete(cur, "invitations")
            _delete(cur, "admin_registration_requests")
            _delete(cur, "phone_verification_otps")
            _delete(cur, "payment_intents")
            _delete(cur, "usage_events")
            _delete(cur, "managed_events")
            _delete(cur, "sessions")
            _delete(cur, "refresh_tokens")
            _delete(cur, "password_reset_tokens")
            _delete(cur, "email_verification_tokens")
            _delete(cur, "audit_logs")

            # Detach company ↔ admin links before removing users/companies
            if _table_exists(cur, "companies"):
                cur.execute("UPDATE companies SET admin_id = NULL")
            if _table_exists(cur, "users"):
                cur.execute(
                    """
                    UPDATE users
                    SET company_id = NULL, admin_id = NULL, created_by = NULL, updated_by = NULL
                    """,
                )

            # Remove every user except SuperAdmin keep-list
            if keep_ids:
                placeholders = ", ".join(["%s"] * len(keep_ids))
                _delete(
                    cur,
                    "users",
                    f"id::text NOT IN ({placeholders})",
                    keep_ids,
                )
            else:
                _delete(cur, "users")

            _delete(cur, "companies")

            # Ensure kept SuperAdmin is active/verified and unlinked from companies
            if keep_ids:
                placeholders = ", ".join(["%s"] * len(keep_ids))
                cur.execute(
                    f"""
                    UPDATE users
                    SET is_active = TRUE,
                        is_verified = TRUE,
                        deleted_at = NULL,
                        company_id = NULL,
                        admin_id = NULL,
                        failed_login_attempts = 0,
                        locked_until = NULL
                    WHERE id::text IN ({placeholders})
                    """,
                    keep_ids,
                )

        # Re-seed roles/permissions + SuperAdmin if missing
        print("\nRe-seeding roles / permissions / SuperAdmin…")
        run_seed()

        with db_cursor(commit=False) as cur:
            remaining_users = _count(cur, "users")
            remaining_companies = _count(cur, "companies")
            remaining_contacts = _count(cur, "contacts")
            print("\nDone.")
            print(f"  users left:     {remaining_users}")
            print(f"  companies left: {remaining_companies}")
            print(f"  contacts left:  {remaining_contacts}")
            print(f"  SuperAdmin login email: {email}")
            print("  Use SUPERADMIN_PASSWORD from .env to sign in.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Clean failed: {exc}", file=sys.stderr)
        return 1
    finally:
        close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
