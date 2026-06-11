from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
SCHEMA_PATH = BASE_DIR / "schema.sql"
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "dgx_access.sqlite"))).resolve()

ACTIVE_ALLOCATION_STATUSES = ("SCHEDULED", "ACTIVE", "EXPIRING")
APP_ROLES = ("USER", "ADMIN", "OBSERVER")
INVENTORY_STATUSES = ("AVAILABLE", "ALLOCATED", "MAINTENANCE", "DISABLED")
SESSION_DAYS = 14
DEFAULT_ACCESS_INSTRUCTIONS = (
    "Access details will be provisioned by the DGX administrator. "
    "Replace this placeholder with VPN, SSH, scheduler, and policy instructions."
)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return to_iso(utcnow())


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    if not value:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Date/time is required.")
    normalized = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"Invalid date/time: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def new_id() -> str:
    return str(uuid.uuid4())


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [row_to_dict(row) for row in rows]


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Password must be at least 8 characters.")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256$120000${salt}${digest.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        int(iterations),
    ).hex()
    return hmac.compare_digest(candidate, digest)


def begin(conn: sqlite3.Connection) -> None:
    pass


def commit(conn: sqlite3.Connection) -> None:
    pass


def rollback(conn: sqlite3.Connection) -> None:
    pass


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        seed_data(conn)
        migrate_inventory_items(conn)


def ensure_role(conn: sqlite3.Connection, role_name: str) -> str:
    row = conn.execute("SELECT id FROM roles WHERE name = ?", (role_name,)).fetchone()
    if row:
        return row["id"]
    role_id = new_id()
    conn.execute("INSERT INTO roles (id, name) VALUES (?, ?)", (role_id, role_name))
    return role_id


def seed_data(conn: sqlite3.Connection) -> None:
    current = now_iso()
    role_ids = {role_name: ensure_role(conn, role_name) for role_name in APP_ROLES}
    user_role = role_ids["USER"]
    admin_role = role_ids["ADMIN"]
    observer_role = role_ids["OBSERVER"]

    admin_exists = conn.execute("SELECT 1 FROM users WHERE email = ?", ("admin@dgx.local",)).fetchone()
    if not admin_exists:
        admin_id = new_id()
        conn.execute(
            """
            INSERT INTO users (id, name, email, department, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                admin_id,
                "DGX Admin",
                "admin@dgx.local",
                "Platform",
                hash_password("admin1234"),
                current,
                current,
            ),
        )
        conn.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)", (admin_id, user_role))
        conn.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)", (admin_id, admin_role))

    demo_exists = conn.execute("SELECT 1 FROM users WHERE email = ?", ("user@dgx.local",)).fetchone()
    if not demo_exists:
        user_id = new_id()
        conn.execute(
            """
            INSERT INTO users (id, name, email, department, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                "Demo User",
                "user@dgx.local",
                "AI Lab",
                hash_password("user1234"),
                current,
                current,
            ),
        )
        conn.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)", (user_id, user_role))

    observer_exists = conn.execute("SELECT 1 FROM users WHERE email = ?", ("observer@dgx.local",)).fetchone()
    if not observer_exists:
        observer_id = new_id()
        conn.execute(
            """
            INSERT INTO users (id, name, email, department, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observer_id,
                "Read Only Observer",
                "observer@dgx.local",
                "Research Ops",
                hash_password("observer1234"),
                current,
                current,
            ),
        )
        conn.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)", (observer_id, observer_role))

    server_exists = conn.execute("SELECT 1 FROM dgx_servers").fetchone()
    if not server_exists:
        server_id = new_id()
        conn.execute(
            """
            INSERT INTO dgx_servers (id, name, location, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (server_id, "DGX-01", "Primary lab", "ACTIVE", current, current),
        )
        seed_pools = [
            ("FULL_GPU", None, "Full GPU", 8, 0),
            ("MIG", "1G.10GB", "MIG 1G.10GB", 56, 0),
            ("MIG", "2G.20GB", "MIG 2G.20GB", 28, 0),
            ("MIG", "3G.40GB", "MIG 3G.40GB", 16, 0),
            ("MIG", "7G.80GB", "MIG 7G.80GB", 8, 0),
        ]
        for resource_type, mig_profile, label, total, reserved in seed_pools:
            conn.execute(
                """
                INSERT INTO resource_pools (
                  id, server_id, resource_type, mig_profile, label,
                  total_capacity, reserved_capacity, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (new_id(), server_id, resource_type, mig_profile, label, total, reserved, current, current),
            )


def get_roles(conn: sqlite3.Connection, user_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT r.name
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = ?
        ORDER BY r.name
        """,
        (user_id,),
    ).fetchall()
    return [row["name"] for row in rows]


def create_session(conn: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    current = utcnow()
    expires = current + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token, user_id, to_iso(expires), to_iso(current)),
    )
    return token


def get_session_user(conn: sqlite3.Connection, token: str | None) -> dict | None:
    if not token:
        return None
    row = conn.execute(
        """
        SELECT u.*
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ? AND s.expires_at > ?
        """,
        (token, now_iso()),
    ).fetchone()
    if not row:
        return None
    user = row_to_dict(row)
    user.pop("password_hash", None)
    user["roles"] = get_roles(conn, user["id"])
    return user


def audit(
    conn: sqlite3.Connection,
    actor_id: str | None,
    actor_type: str,
    action: str,
    entity_type: str,
    entity_id: str,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (
          id, actor_id, actor_type, action, entity_type, entity_id,
          before_json, after_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id(),
            actor_id,
            actor_type,
            action,
            entity_type,
            entity_id,
            json.dumps(before, sort_keys=True) if before else None,
            json.dumps(after, sort_keys=True) if after else None,
            now_iso(),
        ),
    )


def queue_key(resource_type: str, mig_profile: str | None) -> str:
    if resource_type == "MIG":
        return f"MIG:{mig_profile}"
    return "FULL_GPU"


def validate_resource(resource_type: str, mig_profile: str | None, quantity: int) -> None:
    if resource_type not in ("FULL_GPU", "MIG"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Resource type must be FULL_GPU or MIG.")
    if resource_type == "MIG" and not mig_profile:
        raise ApiError(HTTPStatus.BAD_REQUEST, "MIG profile is required for MIG requests.")
    if resource_type == "FULL_GPU" and mig_profile:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Full GPU requests cannot include a MIG profile.")
    if quantity < 1:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Quantity must be at least 1.")


def default_pool_label(resource_type: str, mig_profile: str | None) -> str:
    return "Full GPU inventory" if resource_type == "FULL_GPU" else f"MIG {mig_profile} inventory"


def ensure_resource_pool_for_inventory(
    conn: sqlite3.Connection,
    resource_type: str,
    mig_profile: str | None,
) -> str:
    validate_resource(resource_type, mig_profile, 1)
    if resource_type == "FULL_GPU":
        row = conn.execute(
            """
            SELECT id
            FROM resource_pools
            WHERE resource_type = 'FULL_GPU' AND mig_profile IS NULL
            ORDER BY enabled DESC, created_at ASC
            LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id
            FROM resource_pools
            WHERE resource_type = 'MIG' AND mig_profile = ?
            ORDER BY enabled DESC, created_at ASC
            LIMIT 1
            """,
            (mig_profile,),
        ).fetchone()
    if row:
        return row["id"]

    server = conn.execute(
        "SELECT id FROM dgx_servers WHERE status = 'ACTIVE' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    current = now_iso()
    pool_id = new_id()
    conn.execute(
        """
        INSERT INTO resource_pools (
          id, server_id, resource_type, mig_profile, label,
          total_capacity, reserved_capacity, enabled, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, 0, 1, ?, ?)
        """,
        (
            pool_id,
            server["id"] if server else None,
            resource_type,
            mig_profile,
            default_pool_label(resource_type, mig_profile),
            current,
            current,
        ),
    )
    return pool_id


def sync_resource_pool_capacity(conn: sqlite3.Connection, pool_id: str | None) -> None:
    if not pool_id:
        return
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status != 'DISABLED' THEN 1 ELSE 0 END) AS enabled_count
        FROM inventory_items
        WHERE resource_pool_id = ?
        """,
        (pool_id,),
    ).fetchone()
    total = int(row["total"] or 0)
    enabled_count = int(row["enabled_count"] or 0)
    conn.execute(
        """
        UPDATE resource_pools
        SET total_capacity = ?, reserved_capacity = 0, enabled = ?, updated_at = ?
        WHERE id = ?
        """,
        (total, 1 if enabled_count else 0, now_iso(), pool_id),
    )


def migrate_inventory_items(conn: sqlite3.Connection) -> None:
    current = now_iso()
    pools = rows_to_dicts(conn.execute("SELECT * FROM resource_pools ORDER BY created_at, label").fetchall())
    for pool in pools:
        existing = conn.execute(
            "SELECT COUNT(*) AS count FROM inventory_items WHERE resource_pool_id = ?",
            (pool["id"],),
        ).fetchone()["count"]
        if existing:
            continue
        total = int(pool["total_capacity"] or 0)
        if total <= 0:
            continue
        status = "AVAILABLE" if int(pool["enabled"] or 0) else "DISABLED"
        for index in range(total):
            label = pool["label"] if total == 1 else f"{pool['label']}-{index + 1:02d}"
            conn.execute(
                """
                INSERT INTO inventory_items (
                  id, resource_pool_id, resource_type, mig_profile, label,
                  status, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    pool["id"],
                    pool["resource_type"],
                    pool["mig_profile"],
                    label,
                    status,
                    "Migrated from legacy aggregate inventory pool.",
                    current,
                    current,
                ),
            )

    active_resources = rows_to_dicts(
        conn.execute(
            """
            SELECT ar.allocation_id, ar.resource_pool_id, ar.quantity
            FROM allocation_resources ar
            JOIN allocations a ON a.id = ar.allocation_id
            WHERE a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
            """
        ).fetchall()
    )
    for resource in active_resources:
        linked = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM allocation_inventory_items aii
            JOIN inventory_items ii ON ii.id = aii.inventory_item_id
            WHERE aii.allocation_id = ? AND ii.resource_pool_id = ?
            """,
            (resource["allocation_id"], resource["resource_pool_id"]),
        ).fetchone()["count"]
        needed = int(resource["quantity"] or 0) - int(linked or 0)
        if needed <= 0:
            continue
        items = conn.execute(
            """
            SELECT id
            FROM inventory_items
            WHERE resource_pool_id = ? AND status = 'AVAILABLE'
            ORDER BY label, created_at
            LIMIT ?
            """,
            (resource["resource_pool_id"], needed),
        ).fetchall()
        for item in items:
            conn.execute(
                """
                INSERT OR IGNORE INTO allocation_inventory_items (
                  id, allocation_id, inventory_item_id, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (new_id(), resource["allocation_id"], item["id"], current),
            )
            conn.execute(
                "UPDATE inventory_items SET status = 'ALLOCATED', updated_at = ? WHERE id = ?",
                (current, item["id"]),
            )

    for pool in pools:
        sync_resource_pool_capacity(conn, pool["id"])


def allocation_status_for_window(start_at: str, end_at: str) -> str:
    now = utcnow()
    start = parse_datetime(start_at)
    end = parse_datetime(end_at)
    if end <= now:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Allocation end time must be in the future.")
    if start > now:
        return "SCHEDULED"
    if end <= now + timedelta(days=2):
        return "EXPIRING"
    return "ACTIVE"


def request_status_for_allocation(status: str) -> str:
    if status == "SCHEDULED":
        return "APPROVED"
    if status == "EXPIRING":
        return "EXPIRING"
    return "ACTIVE"


def compatible_pools(conn: sqlite3.Connection, resource_type: str, mig_profile: str | None) -> list[dict]:
    if resource_type == "FULL_GPU":
        rows = conn.execute(
            """
            SELECT rp.*, ds.name AS server_name
            FROM resource_pools rp
            LEFT JOIN dgx_servers ds ON ds.id = rp.server_id
            WHERE rp.enabled = 1 AND rp.resource_type = 'FULL_GPU'
            ORDER BY rp.label
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT rp.*, ds.name AS server_name
            FROM resource_pools rp
            LEFT JOIN dgx_servers ds ON ds.id = rp.server_id
            WHERE rp.enabled = 1 AND rp.resource_type = 'MIG' AND rp.mig_profile = ?
            ORDER BY rp.label
            """,
            (mig_profile,),
        ).fetchall()
    return rows_to_dicts(rows)


def pool_load(
    conn: sqlite3.Connection,
    pool_id: str,
    start_at: str,
    end_at: str,
    exclude_request_id: str | None = None,
    exclude_allocation_id: str | None = None,
) -> int:
    params: list[str] = [pool_id, end_at, start_at]
    allocation_filter = ""
    if exclude_allocation_id:
        allocation_filter = "AND a.id != ?"
        params.append(exclude_allocation_id)
    allocated_items = conn.execute(
        f"""
        SELECT COUNT(DISTINCT aii.inventory_item_id) AS used
        FROM allocation_inventory_items aii
        JOIN inventory_items ii ON ii.id = aii.inventory_item_id
        JOIN allocations a ON a.id = aii.allocation_id
        WHERE ii.resource_pool_id = ?
          AND a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
          AND a.start_at < ?
          AND a.end_at > ?
          {allocation_filter}
        """,
        params,
    ).fetchone()["used"]

    params = [pool_id, end_at, start_at]
    allocation_filter = ""
    if exclude_allocation_id:
        allocation_filter = "AND a.id != ?"
        params.append(exclude_allocation_id)
    allocated_legacy = conn.execute(
        f"""
        SELECT COALESCE(SUM(ar.quantity), 0) AS used
        FROM allocation_resources ar
        JOIN allocations a ON a.id = ar.allocation_id
        WHERE ar.resource_pool_id = ?
          AND a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
          AND a.start_at < ?
          AND a.end_at > ?
          {allocation_filter}
        """,
        params,
    ).fetchone()["used"]

    params = [pool_id, end_at, start_at]
    hold_filter = ""
    if exclude_request_id:
        hold_filter = "AND h.request_id != ?"
        params.append(exclude_request_id)
    held = conn.execute(
        f"""
        SELECT COALESCE(SUM(h.quantity), 0) AS used
        FROM capacity_holds h
        WHERE h.resource_pool_id = ?
          AND h.status = 'HELD'
          AND h.start_at < ?
          AND h.end_at > ?
          {hold_filter}
        """,
        params,
    ).fetchone()["used"]

    allocated = max(int(allocated_items or 0), int(allocated_legacy or 0))
    return allocated + int(held or 0)


def inventory_available_count(
    conn: sqlite3.Connection,
    pool_id: str,
    start_at: str,
    end_at: str,
    exclude_allocation_id: str | None = None,
) -> int:
    params: list[str] = [pool_id, end_at, start_at]
    allocation_filter = ""
    if exclude_allocation_id:
        allocation_filter = "AND a.id != ?"
        params.append(exclude_allocation_id)
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM inventory_items ii
            WHERE ii.resource_pool_id = ?
              AND ii.status = 'AVAILABLE'
              AND NOT EXISTS (
                SELECT 1
                FROM allocation_inventory_items aii
                JOIN allocations a ON a.id = aii.allocation_id
                WHERE aii.inventory_item_id = ii.id
                  AND a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
                  AND a.start_at < ?
                  AND a.end_at > ?
                  {allocation_filter}
              )
            """,
            params,
        ).fetchone()["count"]
        or 0
    )


def inventory_held_count(
    conn: sqlite3.Connection,
    pool_id: str,
    start_at: str,
    end_at: str,
    exclude_request_id: str | None = None,
) -> int:
    params: list[str] = [pool_id, end_at, start_at]
    hold_filter = ""
    if exclude_request_id:
        hold_filter = "AND request_id != ?"
        params.append(exclude_request_id)
    return int(
        conn.execute(
            f"""
            SELECT COALESCE(SUM(quantity), 0) AS held
            FROM capacity_holds
            WHERE resource_pool_id = ?
              AND status = 'HELD'
              AND start_at < ?
              AND end_at > ?
              {hold_filter}
            """,
            params,
        ).fetchone()["held"]
        or 0
    )


def select_available_inventory_items(
    conn: sqlite3.Connection,
    pool_id: str,
    quantity: int,
    start_at: str,
    end_at: str,
    exclude_allocation_id: str | None = None,
) -> list[dict]:
    params: list[str | int] = [pool_id, end_at, start_at]
    allocation_filter = ""
    if exclude_allocation_id:
        allocation_filter = "AND a.id != ?"
        params.append(exclude_allocation_id)
    params.append(quantity)
    rows = conn.execute(
        f"""
        SELECT ii.*
        FROM inventory_items ii
        WHERE ii.resource_pool_id = ?
          AND ii.status = 'AVAILABLE'
          AND NOT EXISTS (
            SELECT 1
            FROM allocation_inventory_items aii
            JOIN allocations a ON a.id = aii.allocation_id
            WHERE aii.inventory_item_id = ii.id
              AND a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
              AND a.start_at < ?
              AND a.end_at > ?
              {allocation_filter}
          )
        ORDER BY ii.label, ii.created_at
        LIMIT ?
        """,
        params,
    ).fetchall()
    return rows_to_dicts(rows)


def find_capacity(
    conn: sqlite3.Connection,
    resource_type: str,
    mig_profile: str | None,
    quantity: int,
    start_at: str,
    end_at: str,
    exclude_request_id: str | None = None,
    exclude_allocation_id: str | None = None,
    preferred_pool_id: str | None = None,
) -> dict | None:
    pools = compatible_pools(conn, resource_type, mig_profile)
    if preferred_pool_id:
        pools = [pool for pool in pools if pool["id"] == preferred_pool_id]
    for pool in pools:
        usable = inventory_available_count(conn, pool["id"], start_at, end_at, exclude_allocation_id)
        held = inventory_held_count(conn, pool["id"], start_at, end_at, exclude_request_id)
        used = pool_load(conn, pool["id"], start_at, end_at, exclude_request_id, exclude_allocation_id)
        pool["usable_capacity"] = usable
        pool["used_capacity"] = used
        pool["held_capacity"] = held
        pool["available_capacity"] = max(0, usable - held)
        if usable - held >= quantity:
            return pool
    return None


def create_hold(
    conn: sqlite3.Connection,
    request_id: str,
    resource_pool_id: str,
    quantity: int,
    start_at: str,
    end_at: str,
) -> str:
    hold_id = new_id()
    current = now_iso()
    conn.execute(
        """
        INSERT INTO capacity_holds (
          id, request_id, resource_pool_id, quantity, start_at, end_at,
          status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'HELD', ?, ?)
        """,
        (hold_id, request_id, resource_pool_id, quantity, start_at, end_at, current, current),
    )
    return hold_id


def release_holds(conn: sqlite3.Connection, request_id: str, status: str = "RELEASED") -> None:
    conn.execute(
        """
        UPDATE capacity_holds
        SET status = ?, updated_at = ?
        WHERE request_id = ? AND status = 'HELD'
        """,
        (status, now_iso(), request_id),
    )


def current_waiting_position(conn: sqlite3.Connection, entry_id: str) -> int | None:
    entry = conn.execute(
        "SELECT * FROM waiting_queue_entries WHERE id = ? AND status = 'WAITING'",
        (entry_id,),
    ).fetchone()
    if not entry:
        return None
    count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM waiting_queue_entries
        WHERE queue_key = ?
          AND status = 'WAITING'
          AND (
            position_created_at < ?
            OR (position_created_at = ? AND id <= ?)
          )
        """,
        (entry["queue_key"], entry["position_created_at"], entry["position_created_at"], entry["id"]),
    ).fetchone()["count"]
    return int(count)


EMAIL_TEMPLATES = {
    "REQUEST_SUBMITTED": (
        "DGX access request received",
        """Hi {{name}},

Your DGX access request has been received.

Request ID: {{request_id}}
Resource: {{resource_label}}
Requested window: {{start_at}} to {{end_at}}

You will receive another email after admin review.
""",
    ),
    "WAITING_LIST": (
        "DGX request added to waiting list",
        """Hi {{name}},

DGX capacity is currently unavailable for your requested resource.

Request ID: {{request_id}}
Resource: {{resource_label}}
Waiting position: {{waiting_position}}

You will be notified when your request is promoted for admin review.
""",
    ),
    "PENDING_ADMIN_REVIEW": (
        "DGX request pending admin approval",
        """Hi {{name}},

Capacity is available and your request is now pending admin approval.

Request ID: {{request_id}}
Requested window: {{start_at}} to {{end_at}}
""",
    ),
    "APPROVED": (
        "DGX access approved",
        """Hi {{name}},

Your DGX access request has been approved.

Allocation ID: {{allocation_id}}
Resource: {{resource_label}}
Start: {{start_at}}
End: {{end_at}}

Access instructions:
{{access_instructions}}

Admin remarks:
{{remarks}}
""",
    ),
    "REJECTED": (
        "DGX access request rejected",
        """Hi {{name}},

Your DGX access request was rejected.

Request ID: {{request_id}}
Reason: {{reason}}
""",
    ),
    "EXPIRY_REMINDER": (
        "DGX access ends on {{end_at}}",
        """Hi {{name}},

Your DGX access is scheduled to end on {{end_at}}.

If you need more time, request an extension in the DGX access app.
""",
    ),
    "EXTENSION_REQUESTED_ADMIN": (
        "DGX extension request needs review",
        """Hi {{admin_name}},

An extension request is pending review.

Allocation ID: {{allocation_id}}
Requested new end: {{requested_end_at}}
Requester: {{requester_name}} <{{requester_email}}>
""",
    ),
    "EXTENSION_APPROVED": (
        "DGX access extension approved",
        """Hi {{name}},

Your DGX access extension has been approved.

Allocation ID: {{allocation_id}}
New end time: {{new_end_at}}
""",
    ),
    "EXTENSION_REJECTED": (
        "DGX access extension rejected",
        """Hi {{name}},

Your DGX access extension request was rejected.

Allocation ID: {{allocation_id}}
Reason: {{reason}}
""",
    ),
    "CANCELLATION": (
        "DGX access cancelled",
        """Hi {{name}},

Your DGX access has been cancelled.

Request/Allocation ID: {{entity_id}}
Cancelled by: {{cancelled_by}}
Cancelled at: {{cancelled_at}}
""",
    ),
    "CANCELLATION_ADMIN": (
        "DGX capacity released after cancellation",
        """Hi {{admin_name}},

DGX allocation {{allocation_id}} was cancelled.

Cancelled by: {{cancelled_by}}
Capacity was released automatically and waiting-list processing was triggered.
""",
    ),
    "ALLOCATION_ENDED": (
        "DGX access ended",
        """Hi {{name}},

Your DGX access ended at {{end_at}}.

Allocation ID: {{allocation_id}}
""",
    ),
}


def render_template(template_key: str, context: dict) -> tuple[str, str]:
    if template_key not in EMAIL_TEMPLATES:
        raise ValueError(f"Unknown email template: {template_key}")
    subject, body = EMAIL_TEMPLATES[template_key]
    for key, value in context.items():
        subject = subject.replace("{{" + key + "}}", str(value or ""))
        body = body.replace("{{" + key + "}}", str(value or ""))
    return subject, body


def enqueue_email(
    conn: sqlite3.Connection,
    recipient_email: str,
    template_key: str,
    context: dict,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
) -> None:
    subject, body = render_template(template_key, context)
    conn.execute(
        """
        INSERT INTO email_notifications (
          id, recipient_email, template_key, subject, body, status,
          related_entity_type, related_entity_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?)
        """,
        (new_id(), recipient_email, template_key, subject, body, related_entity_type, related_entity_id, now_iso()),
    )


def admin_users(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT DISTINCT u.*
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id
        JOIN roles r ON r.id = ur.role_id
        WHERE r.name = 'ADMIN'
        ORDER BY u.email
        """
    ).fetchall()
    return rows_to_dicts(rows)


def resource_label(request_or_row: dict) -> str:
    base = request_or_row.get("resource_type")
    profile = request_or_row.get("mig_profile")
    quantity = request_or_row.get("quantity")
    if base == "MIG":
        return f"MIG {profile} x {quantity}"
    return f"Full GPU x {quantity}"


def send_email(notification: dict) -> tuple[bool, str | None]:
    smtp_host = os.getenv("SMTP_HOST")
    if not smtp_host:
        print("\n--- DGX EMAIL LOG ---")
        print(f"To: {notification['recipient_email']}")
        print(f"Subject: {notification['subject']}")
        print(notification["body"])
        print("--- END EMAIL LOG ---\n")
        return True, None

    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM", "dgx-access@example.local")
    message["To"] = notification["recipient_email"]
    message["Subject"] = notification["subject"]
    message.set_content(notification["body"])

    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    use_tls = os.getenv("SMTP_TLS", "true").lower() == "true"

    try:
        if use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, port, timeout=15) as smtp:
                smtp.starttls(context=context)
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, port, timeout=15) as smtp:
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
    except Exception as exc:  # pragma: no cover - depends on external SMTP.
        return False, str(exc)
    return True, None


def send_email_queue(limit: int = 50) -> None:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM email_notifications
            WHERE status = 'QUEUED'
            ORDER BY created_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            notification = row_to_dict(row)
            ok, error = send_email(notification)
            if ok:
                conn.execute(
                    """
                    UPDATE email_notifications
                    SET status = 'SENT', sent_at = ?, error_message = NULL
                    WHERE id = ?
                    """,
                    (now_iso(), notification["id"]),
                )
            else:
                conn.execute(
                    """
                    UPDATE email_notifications
                    SET status = 'FAILED', error_message = ?
                    WHERE id = ?
                    """,
                    (error, notification["id"]),
                )


def submit_request(conn: sqlite3.Connection, user: dict, payload: dict) -> dict:
    name = (payload.get("name") or user["name"]).strip()
    email = (payload.get("email") or user["email"]).strip().lower()
    department = (payload.get("department") or user.get("department") or "").strip()
    purpose = (payload.get("purpose") or "").strip()
    urgency = (payload.get("urgency") or "").strip() or None
    notes = (payload.get("notes") or "").strip() or None
    resource_type = payload.get("resource_type")
    mig_profile = payload.get("mig_profile") or None
    quantity = int(payload.get("quantity") or 1)
    duration_hours = float(payload.get("duration_hours") or 0)

    if not name or not email or not purpose:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Name, email, and purpose are required.")
    if duration_hours <= 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Duration must be greater than zero.")
    validate_resource(resource_type, mig_profile, quantity)

    start_dt = parse_datetime(payload.get("requested_start_at"))
    if start_dt < utcnow() - timedelta(minutes=5):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Requested start time cannot be in the past.")
    duration_minutes = int(round(duration_hours * 60))
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    start_at = to_iso(start_dt)
    end_at = to_iso(end_dt)
    current = now_iso()
    request_id = new_id()
    key = queue_key(resource_type, mig_profile)

    begin(conn)
    try:
        pool = find_capacity(conn, resource_type, mig_profile, quantity, start_at, end_at)
        status = "PENDING_ADMIN" if pool else "WAITING"
        waiting_position = None
        if status == "WAITING":
            waiting_position = conn.execute(
                """
                SELECT COUNT(*) + 1 AS position
                FROM waiting_queue_entries
                WHERE queue_key = ? AND status = 'WAITING'
                """,
                (key,),
            ).fetchone()["position"]

        conn.execute(
            """
            INSERT INTO access_requests (
              id, requester_id, status, name, email, department, purpose, urgency,
              requested_start_at, requested_end_at, requested_duration_minutes,
              resource_type, mig_profile, quantity, notes, waiting_queue_key,
              waiting_position_snapshot, submitted_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                user["id"],
                status,
                name,
                email,
                department,
                purpose,
                urgency,
                start_at,
                end_at,
                duration_minutes,
                resource_type,
                mig_profile,
                quantity,
                notes,
                key if status == "WAITING" else None,
                waiting_position,
                current,
                current,
                current,
            ),
        )

        if pool:
            create_hold(conn, request_id, pool["id"], quantity, start_at, end_at)
            enqueue_email(
                conn,
                email,
                "REQUEST_SUBMITTED",
                {
                    "name": name,
                    "request_id": request_id,
                    "resource_label": resource_label(
                        {"resource_type": resource_type, "mig_profile": mig_profile, "quantity": quantity}
                    ),
                    "start_at": start_at,
                    "end_at": end_at,
                },
                "access_request",
                request_id,
            )
            enqueue_email(
                conn,
                email,
                "PENDING_ADMIN_REVIEW",
                {"name": name, "request_id": request_id, "start_at": start_at, "end_at": end_at},
                "access_request",
                request_id,
            )
        else:
            entry_id = new_id()
            conn.execute(
                """
                INSERT INTO waiting_queue_entries (
                  id, request_id, queue_key, status, position_created_at, created_at, updated_at
                )
                VALUES (?, ?, ?, 'WAITING', ?, ?, ?)
                """,
                (entry_id, request_id, key, current, current, current),
            )
            enqueue_email(
                conn,
                email,
                "WAITING_LIST",
                {
                    "name": name,
                    "request_id": request_id,
                    "resource_label": resource_label(
                        {"resource_type": resource_type, "mig_profile": mig_profile, "quantity": quantity}
                    ),
                    "waiting_position": waiting_position,
                },
                "access_request",
                request_id,
            )

        audit(
            conn,
            user["id"],
            "USER",
            "SUBMIT_REQUEST",
            "access_request",
            request_id,
            None,
            {"status": status},
        )
        commit(conn)
    except Exception:
        rollback(conn)
        raise

    return get_request_detail(conn, request_id, user)


def get_request_detail(conn: sqlite3.Connection, request_id: str, user: dict | None = None) -> dict:
    row = conn.execute(
        """
        SELECT ar.*, u.name AS requester_name, u.email AS requester_email
        FROM access_requests ar
        JOIN users u ON u.id = ar.requester_id
        WHERE ar.id = ?
        """,
        (request_id,),
    ).fetchone()
    if not row:
        raise ApiError(HTTPStatus.NOT_FOUND, "Request not found.")
    request = row_to_dict(row)
    if (
        user
        and "ADMIN" not in user["roles"]
        and "OBSERVER" not in user["roles"]
        and request["requester_id"] != user["id"]
    ):
        raise ApiError(HTTPStatus.FORBIDDEN, "You do not have access to this request.")

    hold = conn.execute(
        """
        SELECT h.*, rp.label AS resource_pool_label
        FROM capacity_holds h
        JOIN resource_pools rp ON rp.id = h.resource_pool_id
        WHERE h.request_id = ? AND h.status = 'HELD'
        ORDER BY h.created_at DESC
        LIMIT 1
        """,
        (request_id,),
    ).fetchone()
    request["hold"] = row_to_dict(hold)

    waiting = conn.execute(
        "SELECT * FROM waiting_queue_entries WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    request["waiting_entry"] = row_to_dict(waiting)
    if request["waiting_entry"] and request["waiting_entry"]["status"] == "WAITING":
        request["waiting_position"] = current_waiting_position(conn, request["waiting_entry"]["id"])
    else:
        request["waiting_position"] = None

    allocation = conn.execute(
        """
        SELECT *
        FROM allocations
        WHERE request_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (request_id,),
    ).fetchone()
    request["allocation"] = row_to_dict(allocation)
    return request


def approve_request(conn: sqlite3.Connection, admin: dict, request_id: str, payload: dict) -> dict:
    start_at = payload.get("start_at")
    end_at = payload.get("end_at")
    pool_id = payload.get("resource_pool_id")
    remarks = (payload.get("remarks") or "").strip() or None
    access_instructions = (payload.get("access_instructions") or os.getenv("ACCESS_INSTRUCTIONS") or DEFAULT_ACCESS_INSTRUCTIONS)

    begin(conn)
    try:
        request_row = conn.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
        if not request_row:
            raise ApiError(HTTPStatus.NOT_FOUND, "Request not found.")
        request = row_to_dict(request_row)
        if request["status"] != "PENDING_ADMIN":
            raise ApiError(HTTPStatus.BAD_REQUEST, "Only pending admin requests can be approved.")

        start_at = to_iso(parse_datetime(start_at or request["requested_start_at"]))
        end_at = to_iso(parse_datetime(end_at or request["requested_end_at"]))
        if parse_datetime(end_at) <= parse_datetime(start_at):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Allocation end must be after start.")
        quantity = int(payload.get("quantity") or request["quantity"])
        validate_resource(request["resource_type"], request["mig_profile"], quantity)

        pool = find_capacity(
            conn,
            request["resource_type"],
            request["mig_profile"],
            quantity,
            start_at,
            end_at,
            exclude_request_id=request_id,
            preferred_pool_id=pool_id,
        )
        if not pool:
            raise ApiError(HTTPStatus.CONFLICT, "Capacity is no longer available for this approval window.")

        allocation_id = new_id()
        allocation_status = allocation_status_for_window(start_at, end_at)
        request_status = request_status_for_allocation(allocation_status)
        current = now_iso()
        conn.execute(
            """
            INSERT INTO allocations (
              id, request_id, user_id, status, start_at, end_at, approved_by,
              approved_at, admin_remarks, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                allocation_id,
                request_id,
                request["requester_id"],
                allocation_status,
                start_at,
                end_at,
                admin["id"],
                current,
                remarks,
                current,
                current,
            ),
        )
        conn.execute(
            """
            INSERT INTO allocation_resources (id, allocation_id, resource_pool_id, quantity)
            VALUES (?, ?, ?, ?)
            """,
            (new_id(), allocation_id, pool["id"], quantity),
        )
        selected_items = select_available_inventory_items(conn, pool["id"], quantity, start_at, end_at)
        if len(selected_items) < quantity:
            raise ApiError(HTTPStatus.CONFLICT, "Inventory items are no longer available for this approval window.")
        for item in selected_items:
            conn.execute(
                """
                INSERT INTO allocation_inventory_items (
                  id, allocation_id, inventory_item_id, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (new_id(), allocation_id, item["id"], current),
            )
            conn.execute(
                "UPDATE inventory_items SET status = 'ALLOCATED', updated_at = ? WHERE id = ?",
                (current, item["id"]),
            )
        release_holds(conn, request_id, "CONVERTED")
        conn.execute(
            """
            UPDATE access_requests
            SET status = ?, requested_start_at = ?, requested_end_at = ?,
                quantity = ?, updated_at = ?
            WHERE id = ?
            """,
            (request_status, start_at, end_at, quantity, current, request_id),
        )

        enqueue_email(
            conn,
            request["email"],
            "APPROVED",
            {
                "name": request["name"],
                "allocation_id": allocation_id,
                "resource_label": resource_label({**request, "quantity": quantity}),
                "start_at": start_at,
                "end_at": end_at,
                "access_instructions": access_instructions,
                "remarks": remarks or "",
            },
            "allocation",
            allocation_id,
        )
        audit(
            conn,
            admin["id"],
            "ADMIN",
            "APPROVE_REQUEST",
            "access_request",
            request_id,
            request,
            {"status": request_status, "allocation_id": allocation_id},
        )
        commit(conn)
    except Exception:
        rollback(conn)
        raise

    return get_allocation_detail(conn, allocation_id)


def reject_request(conn: sqlite3.Connection, admin: dict, request_id: str, payload: dict) -> dict:
    reason = (payload.get("reason") or "").strip()
    if not reason:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Rejection reason is required.")

    begin(conn)
    try:
        row = conn.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            raise ApiError(HTTPStatus.NOT_FOUND, "Request not found.")
        request = row_to_dict(row)
        if request["status"] in ("APPROVED", "ACTIVE", "EXPIRING", "EXTENDED", "ENDED", "CANCELLED", "REJECTED"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "This request cannot be rejected in its current state.")
        current = now_iso()
        release_holds(conn, request_id)
        conn.execute(
            """
            UPDATE waiting_queue_entries
            SET status = 'EXPIRED', updated_at = ?
            WHERE request_id = ? AND status = 'WAITING'
            """,
            (current, request_id),
        )
        conn.execute(
            """
            UPDATE access_requests
            SET status = 'REJECTED', rejected_at = ?, rejected_by = ?,
                rejection_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (current, admin["id"], reason, current, request_id),
        )
        enqueue_email(
            conn,
            request["email"],
            "REJECTED",
            {"name": request["name"], "request_id": request_id, "reason": reason},
            "access_request",
            request_id,
        )
        audit(conn, admin["id"], "ADMIN", "REJECT_REQUEST", "access_request", request_id, request, {"status": "REJECTED"})
        commit(conn)
    except Exception:
        rollback(conn)
        raise

    process_waiting_list()
    return get_request_detail(conn, request_id, admin)


def cancel_request(conn: sqlite3.Connection, actor: dict, request_id: str) -> dict:
    begin(conn)
    try:
        row = conn.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            raise ApiError(HTTPStatus.NOT_FOUND, "Request not found.")
        request = row_to_dict(row)
        is_admin = "ADMIN" in actor["roles"]
        if not is_admin and request["requester_id"] != actor["id"]:
            raise ApiError(HTTPStatus.FORBIDDEN, "You cannot cancel this request.")

        allocation = conn.execute(
            """
            SELECT *
            FROM allocations
            WHERE request_id = ? AND status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        if allocation:
            allocation_id = allocation["id"]
            cancel_active_allocation_in_tx(conn, actor, allocation_id)
            commit(conn)
            process_waiting_list()
            return get_request_detail(conn, request_id, actor)

        if request["status"] in ("CANCELLED", "REJECTED", "ENDED"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "This request is already terminal.")
        if request["status"] not in ("WAITING", "PENDING_ADMIN", "SUBMITTED", "DRAFT"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "This request cannot be cancelled through this endpoint.")

        current = now_iso()
        release_holds(conn, request_id)
        conn.execute(
            """
            UPDATE waiting_queue_entries
            SET status = 'CANCELLED', updated_at = ?
            WHERE request_id = ? AND status = 'WAITING'
            """,
            (current, request_id),
        )
        conn.execute(
            """
            UPDATE access_requests
            SET status = 'CANCELLED', cancelled_at = ?, cancelled_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (current, actor["id"], current, request_id),
        )
        enqueue_email(
            conn,
            request["email"],
            "CANCELLATION",
            {
                "name": request["name"],
                "entity_id": request_id,
                "cancelled_by": actor["email"],
                "cancelled_at": current,
            },
            "access_request",
            request_id,
        )
        audit(
            conn,
            actor["id"],
            "ADMIN" if is_admin else "USER",
            "CANCEL_REQUEST",
            "access_request",
            request_id,
            request,
            {"status": "CANCELLED"},
        )
        commit(conn)
    except Exception:
        rollback(conn)
        raise

    process_waiting_list()
    return get_request_detail(conn, request_id, actor)


def cancel_active_allocation_in_tx(conn: sqlite3.Connection, actor: dict, allocation_id: str) -> None:
    allocation_row = conn.execute("SELECT * FROM allocations WHERE id = ?", (allocation_id,)).fetchone()
    if not allocation_row:
        raise ApiError(HTTPStatus.NOT_FOUND, "Allocation not found.")
    allocation = row_to_dict(allocation_row)
    if allocation["status"] not in ACTIVE_ALLOCATION_STATUSES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Only scheduled, active, or expiring allocations can be cancelled.")

    request = row_to_dict(conn.execute("SELECT * FROM access_requests WHERE id = ?", (allocation["request_id"],)).fetchone())
    is_admin = "ADMIN" in actor["roles"]
    if not is_admin and allocation["user_id"] != actor["id"]:
        raise ApiError(HTTPStatus.FORBIDDEN, "You cannot cancel this allocation.")

    current = now_iso()
    conn.execute(
        """
        UPDATE allocations
        SET status = 'CANCELLED', cancelled_by = ?, cancelled_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (actor["id"], current, current, allocation_id),
    )
    conn.execute(
        """
        UPDATE access_requests
        SET status = 'CANCELLED', cancelled_by = ?, cancelled_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (actor["id"], current, current, allocation["request_id"]),
    )
    enqueue_email(
        conn,
        request["email"],
        "CANCELLATION",
        {
            "name": request["name"],
            "entity_id": allocation_id,
            "cancelled_by": actor["email"],
            "cancelled_at": current,
        },
        "allocation",
        allocation_id,
    )
    for admin in admin_users(conn):
        enqueue_email(
            conn,
            admin["email"],
            "CANCELLATION_ADMIN",
            {
                "admin_name": admin["name"],
                "allocation_id": allocation_id,
                "cancelled_by": actor["email"],
            },
            "allocation",
            allocation_id,
        )
    audit(
        conn,
        actor["id"],
        "ADMIN" if is_admin else "USER",
        "CANCEL_ALLOCATION_AUTO_RELEASE_CAPACITY",
        "allocation",
        allocation_id,
        allocation,
        {"status": "CANCELLED", "capacity_released": True},
    )
    release_allocation_items(conn, allocation_id)


def release_allocation_items(conn: sqlite3.Connection, allocation_id: str) -> None:
    current = now_iso()
    rows = conn.execute(
        """
        SELECT inventory_item_id
        FROM allocation_inventory_items
        WHERE allocation_id = ?
        """,
        (allocation_id,),
    ).fetchall()
    for row in rows:
        still_used = conn.execute(
            """
            SELECT 1
            FROM allocation_inventory_items aii
            JOIN allocations a ON a.id = aii.allocation_id
            WHERE aii.inventory_item_id = ?
              AND aii.allocation_id != ?
              AND a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
            LIMIT 1
            """,
            (row["inventory_item_id"], allocation_id),
        ).fetchone()
        if not still_used:
            conn.execute(
                """
                UPDATE inventory_items
                SET status = 'AVAILABLE', updated_at = ?
                WHERE id = ? AND status = 'ALLOCATED'
                """,
                (current, row["inventory_item_id"]),
            )


def cancel_allocation(conn: sqlite3.Connection, actor: dict, allocation_id: str) -> dict:
    begin(conn)
    try:
        cancel_active_allocation_in_tx(conn, actor, allocation_id)
        commit(conn)
    except Exception:
        rollback(conn)
        raise
    process_waiting_list()
    return get_allocation_detail(conn, allocation_id)


def get_allocation_detail(conn: sqlite3.Connection, allocation_id: str) -> dict:
    row = conn.execute(
        """
        SELECT a.*, u.name AS user_name, u.email AS user_email,
               req.resource_type, req.mig_profile, req.quantity, req.purpose
        FROM allocations a
        JOIN users u ON u.id = a.user_id
        JOIN access_requests req ON req.id = a.request_id
        WHERE a.id = ?
        """,
        (allocation_id,),
    ).fetchone()
    if not row:
        raise ApiError(HTTPStatus.NOT_FOUND, "Allocation not found.")
    allocation = row_to_dict(row)
    resources = conn.execute(
        """
        SELECT ar.*, rp.label AS resource_pool_label, rp.resource_type, rp.mig_profile
        FROM allocation_resources ar
        JOIN resource_pools rp ON rp.id = ar.resource_pool_id
        WHERE ar.allocation_id = ?
        """,
        (allocation_id,),
    ).fetchall()
    allocation["resources"] = rows_to_dicts(resources)
    allocation["inventory_items"] = allocation_item_labels(conn, allocation_id)
    return allocation


def request_extension(conn: sqlite3.Connection, user: dict, allocation_id: str, payload: dict) -> dict:
    duration_hours = float(payload.get("duration_hours") or 0)
    justification = (payload.get("justification") or "").strip()
    if duration_hours <= 0:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Extension duration must be greater than zero.")
    if not justification:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Extension justification is required.")

    begin(conn)
    try:
        allocation = conn.execute("SELECT * FROM allocations WHERE id = ?", (allocation_id,)).fetchone()
        if not allocation:
            raise ApiError(HTTPStatus.NOT_FOUND, "Allocation not found.")
        allocation = row_to_dict(allocation)
        if allocation["user_id"] != user["id"] and "ADMIN" not in user["roles"]:
            raise ApiError(HTTPStatus.FORBIDDEN, "You cannot extend this allocation.")
        if allocation["status"] not in ACTIVE_ALLOCATION_STATUSES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Only scheduled, active, or expiring allocations can be extended.")
        open_extension = conn.execute(
            """
            SELECT 1
            FROM extension_requests
            WHERE allocation_id = ? AND status IN ('SUBMITTED', 'PENDING_ADMIN')
            """,
            (allocation_id,),
        ).fetchone()
        if open_extension:
            raise ApiError(HTTPStatus.CONFLICT, "An extension request is already pending for this allocation.")

        minutes = int(round(duration_hours * 60))
        requested_end = parse_datetime(allocation["end_at"]) + timedelta(minutes=minutes)
        extension_id = new_id()
        current = now_iso()
        conn.execute(
            """
            INSERT INTO extension_requests (
              id, allocation_id, requested_by, requested_duration_minutes, justification,
              status, requested_end_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'PENDING_ADMIN', ?, ?, ?)
            """,
            (extension_id, allocation_id, user["id"], minutes, justification, to_iso(requested_end), current, current),
        )
        request = row_to_dict(
            conn.execute("SELECT * FROM access_requests WHERE id = ?", (allocation["request_id"],)).fetchone()
        )
        for admin in admin_users(conn):
            enqueue_email(
                conn,
                admin["email"],
                "EXTENSION_REQUESTED_ADMIN",
                {
                    "admin_name": admin["name"],
                    "allocation_id": allocation_id,
                    "requested_end_at": to_iso(requested_end),
                    "requester_name": request["name"],
                    "requester_email": request["email"],
                },
                "extension_request",
                extension_id,
            )
        audit(
            conn,
            user["id"],
            "USER",
            "REQUEST_EXTENSION",
            "extension_request",
            extension_id,
            None,
            {"allocation_id": allocation_id, "requested_end_at": to_iso(requested_end)},
        )
        commit(conn)
    except Exception:
        rollback(conn)
        raise

    return get_extension_detail(conn, extension_id)


def get_extension_detail(conn: sqlite3.Connection, extension_id: str) -> dict:
    row = conn.execute(
        """
        SELECT er.*, a.start_at, a.end_at, a.status AS allocation_status,
               u.name AS requester_name, u.email AS requester_email
        FROM extension_requests er
        JOIN allocations a ON a.id = er.allocation_id
        JOIN users u ON u.id = er.requested_by
        WHERE er.id = ?
        """,
        (extension_id,),
    ).fetchone()
    if not row:
        raise ApiError(HTTPStatus.NOT_FOUND, "Extension request not found.")
    return row_to_dict(row)


def approve_extension(conn: sqlite3.Connection, admin: dict, extension_id: str) -> dict:
    begin(conn)
    try:
        extension = conn.execute("SELECT * FROM extension_requests WHERE id = ?", (extension_id,)).fetchone()
        if not extension:
            raise ApiError(HTTPStatus.NOT_FOUND, "Extension request not found.")
        extension = row_to_dict(extension)
        if extension["status"] != "PENDING_ADMIN":
            raise ApiError(HTTPStatus.BAD_REQUEST, "Only pending extension requests can be approved.")
        allocation = row_to_dict(
            conn.execute("SELECT * FROM allocations WHERE id = ?", (extension["allocation_id"],)).fetchone()
        )
        if allocation["status"] not in ACTIVE_ALLOCATION_STATUSES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Allocation is no longer extendable.")

        added_start = allocation["end_at"]
        added_end = extension["requested_end_at"]
        if parse_datetime(added_end) <= parse_datetime(added_start):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Requested end time must be after current end time.")

        resources = conn.execute(
            "SELECT * FROM allocation_resources WHERE allocation_id = ?",
            (allocation["id"],),
        ).fetchall()
        for resource in resources:
            available_linked_items = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM allocation_inventory_items aii
                JOIN inventory_items ii ON ii.id = aii.inventory_item_id
                WHERE aii.allocation_id = ?
                  AND ii.resource_pool_id = ?
                  AND ii.status IN ('AVAILABLE', 'ALLOCATED')
                  AND NOT EXISTS (
                    SELECT 1
                    FROM allocation_inventory_items other_aii
                    JOIN allocations other_a ON other_a.id = other_aii.allocation_id
                    WHERE other_aii.inventory_item_id = aii.inventory_item_id
                      AND other_aii.allocation_id != ?
                      AND other_a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
                      AND other_a.start_at < ?
                      AND other_a.end_at > ?
                  )
                """,
                (allocation["id"], resource["resource_pool_id"], allocation["id"], added_end, added_start),
            ).fetchone()["count"]
            if int(available_linked_items or 0) < int(resource["quantity"]):
                raise ApiError(HTTPStatus.CONFLICT, "Capacity is not available for the requested extension.")

        current = now_iso()
        new_allocation_status = allocation_status_for_window(allocation["start_at"], added_end)
        conn.execute(
            """
            UPDATE allocations
            SET end_at = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (added_end, new_allocation_status, current, allocation["id"]),
        )
        conn.execute(
            """
            UPDATE access_requests
            SET requested_end_at = ?, status = 'EXTENDED', updated_at = ?
            WHERE id = ?
            """,
            (added_end, current, allocation["request_id"]),
        )
        conn.execute(
            """
            UPDATE extension_requests
            SET status = 'APPROVED', approved_by = ?, approved_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (admin["id"], current, current, extension_id),
        )
        request = row_to_dict(conn.execute("SELECT * FROM access_requests WHERE id = ?", (allocation["request_id"],)).fetchone())
        enqueue_email(
            conn,
            request["email"],
            "EXTENSION_APPROVED",
            {"name": request["name"], "allocation_id": allocation["id"], "new_end_at": added_end},
            "extension_request",
            extension_id,
        )
        audit(
            conn,
            admin["id"],
            "ADMIN",
            "APPROVE_EXTENSION",
            "extension_request",
            extension_id,
            extension,
            {"status": "APPROVED", "new_end_at": added_end},
        )
        commit(conn)
    except Exception:
        rollback(conn)
        raise

    return get_extension_detail(conn, extension_id)


def reject_extension(conn: sqlite3.Connection, admin: dict, extension_id: str, payload: dict) -> dict:
    reason = (payload.get("reason") or "").strip()
    if not reason:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Rejection reason is required.")
    begin(conn)
    try:
        extension = conn.execute("SELECT * FROM extension_requests WHERE id = ?", (extension_id,)).fetchone()
        if not extension:
            raise ApiError(HTTPStatus.NOT_FOUND, "Extension request not found.")
        extension = row_to_dict(extension)
        if extension["status"] != "PENDING_ADMIN":
            raise ApiError(HTTPStatus.BAD_REQUEST, "Only pending extension requests can be rejected.")
        allocation = row_to_dict(
            conn.execute("SELECT * FROM allocations WHERE id = ?", (extension["allocation_id"],)).fetchone()
        )
        request = row_to_dict(conn.execute("SELECT * FROM access_requests WHERE id = ?", (allocation["request_id"],)).fetchone())
        current = now_iso()
        conn.execute(
            """
            UPDATE extension_requests
            SET status = 'REJECTED', rejected_by = ?, rejected_at = ?,
                rejection_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (admin["id"], current, reason, current, extension_id),
        )
        enqueue_email(
            conn,
            request["email"],
            "EXTENSION_REJECTED",
            {"name": request["name"], "allocation_id": allocation["id"], "reason": reason},
            "extension_request",
            extension_id,
        )
        audit(conn, admin["id"], "ADMIN", "REJECT_EXTENSION", "extension_request", extension_id, extension, {"status": "REJECTED"})
        commit(conn)
    except Exception:
        rollback(conn)
        raise

    return get_extension_detail(conn, extension_id)


def process_waiting_list() -> dict:
    promoted: list[str] = []
    with connect() as conn:
        queue_keys = [
            row["queue_key"]
            for row in conn.execute(
                """
                SELECT DISTINCT queue_key
                FROM waiting_queue_entries
                WHERE status = 'WAITING'
                ORDER BY queue_key
                """
            ).fetchall()
        ]
        for key in queue_keys:
            begin(conn)
            try:
                entries = conn.execute(
                    """
                    SELECT wqe.*, ar.resource_type, ar.mig_profile, ar.quantity,
                           ar.requested_start_at, ar.requested_end_at, ar.email, ar.name
                    FROM waiting_queue_entries wqe
                    JOIN access_requests ar ON ar.id = wqe.request_id
                    WHERE wqe.queue_key = ? AND wqe.status = 'WAITING' AND ar.status = 'WAITING'
                    ORDER BY wqe.position_created_at ASC, wqe.id ASC
                    """,
                    (key,),
                ).fetchall()
                for entry_row in entries:
                    entry = row_to_dict(entry_row)
                    pool = find_capacity(
                        conn,
                        entry["resource_type"],
                        entry["mig_profile"],
                        int(entry["quantity"]),
                        entry["requested_start_at"],
                        entry["requested_end_at"],
                    )
                    if not pool:
                        break

                    current = now_iso()
                    create_hold(
                        conn,
                        entry["request_id"],
                        pool["id"],
                        int(entry["quantity"]),
                        entry["requested_start_at"],
                        entry["requested_end_at"],
                    )
                    conn.execute(
                        """
                        UPDATE access_requests
                        SET status = 'PENDING_ADMIN', waiting_queue_key = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (current, entry["request_id"]),
                    )
                    conn.execute(
                        """
                        UPDATE waiting_queue_entries
                        SET status = 'PROMOTED', promoted_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (current, current, entry["id"]),
                    )
                    enqueue_email(
                        conn,
                        entry["email"],
                        "PENDING_ADMIN_REVIEW",
                        {
                            "name": entry["name"],
                            "request_id": entry["request_id"],
                            "start_at": entry["requested_start_at"],
                            "end_at": entry["requested_end_at"],
                        },
                        "access_request",
                        entry["request_id"],
                    )
                    audit(
                        conn,
                        None,
                        "SYSTEM",
                        "PROMOTE_FROM_WAITLIST",
                        "access_request",
                        entry["request_id"],
                        {"status": "WAITING"},
                        {"status": "PENDING_ADMIN", "held_pool_id": pool["id"]},
                    )
                    promoted.append(entry["request_id"])
                commit(conn)
            except Exception:
                rollback(conn)
                raise
    return {"promoted": promoted, "count": len(promoted)}


def run_activation_job() -> int:
    count = 0
    with connect() as conn:
        begin(conn)
        try:
            current = now_iso()
            rows = conn.execute(
                """
                SELECT *
                FROM allocations
                WHERE status = 'SCHEDULED' AND start_at <= ? AND end_at > ?
                """,
                (current, current),
            ).fetchall()
            for row in rows:
                allocation = row_to_dict(row)
                new_status = "EXPIRING" if parse_datetime(allocation["end_at"]) <= utcnow() + timedelta(days=2) else "ACTIVE"
                request_status = request_status_for_allocation(new_status)
                conn.execute(
                    "UPDATE allocations SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, current, allocation["id"]),
                )
                conn.execute(
                    "UPDATE access_requests SET status = ?, updated_at = ? WHERE id = ?",
                    (request_status, current, allocation["request_id"]),
                )
                audit(
                    conn,
                    None,
                    "SYSTEM",
                    "ACTIVATE_ALLOCATION",
                    "allocation",
                    allocation["id"],
                    allocation,
                    {"status": new_status},
                )
                count += 1
            commit(conn)
        except Exception:
            rollback(conn)
            raise
    return count


def run_expiry_reminder_job() -> int:
    count = 0
    with connect() as conn:
        begin(conn)
        try:
            current = now_iso()
            soon = to_iso(utcnow() + timedelta(days=2))
            rows = conn.execute(
                """
                SELECT a.*, ar.name, ar.email
                FROM allocations a
                JOIN access_requests ar ON ar.id = a.request_id
                WHERE a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
                  AND a.end_at > ?
                  AND a.end_at <= ?
                """,
                (current, soon),
            ).fetchall()
            for row in rows:
                allocation = row_to_dict(row)
                already_sent = conn.execute(
                    """
                    SELECT 1
                    FROM email_notifications
                    WHERE template_key = 'EXPIRY_REMINDER'
                      AND related_entity_type = 'allocation'
                      AND related_entity_id = ?
                      AND status IN ('QUEUED', 'SENT')
                    """,
                    (allocation["id"],),
                ).fetchone()
                if already_sent:
                    continue
                enqueue_email(
                    conn,
                    allocation["email"],
                    "EXPIRY_REMINDER",
                    {"name": allocation["name"], "end_at": allocation["end_at"]},
                    "allocation",
                    allocation["id"],
                )
                if allocation["status"] == "ACTIVE":
                    conn.execute(
                        "UPDATE allocations SET status = 'EXPIRING', updated_at = ? WHERE id = ?",
                        (current, allocation["id"]),
                    )
                    conn.execute(
                        "UPDATE access_requests SET status = 'EXPIRING', updated_at = ? WHERE id = ?",
                        (current, allocation["request_id"]),
                    )
                audit(
                    conn,
                    None,
                    "SYSTEM",
                    "SEND_EXPIRY_REMINDER",
                    "allocation",
                    allocation["id"],
                    None,
                    {"end_at": allocation["end_at"]},
                )
                count += 1
            commit(conn)
        except Exception:
            rollback(conn)
            raise
    return count


def run_expiration_job() -> int:
    ended = 0
    with connect() as conn:
        begin(conn)
        try:
            current = now_iso()
            rows = conn.execute(
                """
                SELECT a.*, ar.name, ar.email
                FROM allocations a
                JOIN access_requests ar ON ar.id = a.request_id
                WHERE a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING') AND a.end_at <= ?
                """,
                (current,),
            ).fetchall()
            for row in rows:
                allocation = row_to_dict(row)
                conn.execute(
                    "UPDATE allocations SET status = 'ENDED', updated_at = ? WHERE id = ?",
                    (current, allocation["id"]),
                )
                conn.execute(
                    "UPDATE access_requests SET status = 'ENDED', updated_at = ? WHERE id = ?",
                    (current, allocation["request_id"]),
                )
                release_allocation_items(conn, allocation["id"])
                enqueue_email(
                    conn,
                    allocation["email"],
                    "ALLOCATION_ENDED",
                    {"name": allocation["name"], "allocation_id": allocation["id"], "end_at": allocation["end_at"]},
                    "allocation",
                    allocation["id"],
                )
                audit(conn, None, "SYSTEM", "END_ALLOCATION", "allocation", allocation["id"], allocation, {"status": "ENDED"})
                ended += 1
            commit(conn)
        except Exception:
            rollback(conn)
            raise
    if ended:
        process_waiting_list()
    return ended


def run_jobs_once() -> dict:
    activated = run_activation_job()
    reminded = run_expiry_reminder_job()
    ended = run_expiration_job()
    waiting = process_waiting_list()
    send_email_queue()
    return {
        "activated": activated,
        "reminded": reminded,
        "ended": ended,
        "waiting": waiting,
    }


def scheduler_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            run_jobs_once()
        except Exception as exc:
            print(f"Scheduler error: {exc}")
        stop_event.wait(30)


def inventory_with_load(conn: sqlite3.Connection) -> list[dict]:
    items = rows_to_dicts(
        conn.execute(
            """
            SELECT ii.*, rp.label AS pool_label, ds.name AS server_name
            FROM inventory_items ii
            LEFT JOIN resource_pools rp ON rp.id = ii.resource_pool_id
            LEFT JOIN dgx_servers ds ON ds.id = rp.server_id
            ORDER BY ii.resource_type, ii.mig_profile, ii.label
            """
        ).fetchall()
    )
    current = now_iso()
    for item in items:
        allocation = conn.execute(
            """
            SELECT a.id, a.status, a.start_at, a.end_at, u.name AS user_name, u.email AS user_email
            FROM allocation_inventory_items aii
            JOIN allocations a ON a.id = aii.allocation_id
            JOIN users u ON u.id = a.user_id
            WHERE aii.inventory_item_id = ?
              AND a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
            ORDER BY a.start_at ASC
            LIMIT 1
            """,
            (item["id"],),
        ).fetchone()
        item["allocation"] = row_to_dict(allocation)
        item["in_use"] = bool(allocation)
        item["effective_status"] = "ALLOCATED" if allocation else item["status"]
        item["updatedAt"] = item["updated_at"]
        item["createdAt"] = item["created_at"]
    return items


def inventory_status_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT resource_type, status, COUNT(*) AS count
        FROM inventory_items
        GROUP BY resource_type, status
        """
    ).fetchall()
    counts: dict[str, dict[str, int]] = {
        "FULL_GPU": {status: 0 for status in INVENTORY_STATUSES},
        "MIG": {status: 0 for status in INVENTORY_STATUSES},
    }
    for row in rows:
        counts[row["resource_type"]][row["status"]] = int(row["count"] or 0)
    return counts


def held_capacity_by_type(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT rp.resource_type, COALESCE(SUM(h.quantity), 0) AS held
        FROM capacity_holds h
        JOIN resource_pools rp ON rp.id = h.resource_pool_id
        WHERE h.status = 'HELD'
        GROUP BY rp.resource_type
        """
    ).fetchall()
    held = {"FULL_GPU": 0, "MIG": 0}
    for row in rows:
        held[row["resource_type"]] = int(row["held"] or 0)
    return held


def active_inventory_used_by_type(conn: sqlite3.Connection) -> dict[str, int]:
    current = now_iso()
    rows = conn.execute(
        """
        SELECT ii.resource_type, COUNT(DISTINCT ii.id) AS used
        FROM allocation_inventory_items aii
        JOIN inventory_items ii ON ii.id = aii.inventory_item_id
        JOIN allocations a ON a.id = aii.allocation_id
        WHERE a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
          AND a.start_at <= ?
          AND a.end_at > ?
        GROUP BY ii.resource_type
        """,
        (current, current),
    ).fetchall()
    used = {"FULL_GPU": 0, "MIG": 0}
    for row in rows:
        used[row["resource_type"]] = int(row["used"] or 0)
    return used


def allocation_item_labels(conn: sqlite3.Connection, allocation_id: str) -> list[dict]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT ii.id, ii.label, ii.resource_type, ii.mig_profile, ii.status
            FROM allocation_inventory_items aii
            JOIN inventory_items ii ON ii.id = aii.inventory_item_id
            WHERE aii.allocation_id = ?
            ORDER BY ii.label
            """,
            (allocation_id,),
        ).fetchall()
    )


def waiting_list(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT wqe.*, ar.name, ar.email, ar.department, ar.purpose, ar.resource_type,
               ar.mig_profile, ar.quantity, ar.requested_start_at, ar.requested_end_at
        FROM waiting_queue_entries wqe
        JOIN access_requests ar ON ar.id = wqe.request_id
        WHERE wqe.status = 'WAITING'
        ORDER BY wqe.queue_key, wqe.position_created_at, wqe.id
        """
    ).fetchall()
    result = rows_to_dicts(rows)
    positions: dict[str, int] = {}
    for item in result:
        positions[item["queue_key"]] = positions.get(item["queue_key"], 0) + 1
        item["waiting_position"] = positions[item["queue_key"]]
    return result


def pending_requests(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ar.*, h.resource_pool_id AS held_resource_pool_id, rp.label AS held_resource_pool_label
        FROM access_requests ar
        LEFT JOIN capacity_holds h ON h.request_id = ar.id AND h.status = 'HELD'
        LEFT JOIN resource_pools rp ON rp.id = h.resource_pool_id
        WHERE ar.status = 'PENDING_ADMIN'
        ORDER BY ar.submitted_at
        """
    ).fetchall()
    return rows_to_dicts(rows)


def active_allocations(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.*, u.name AS user_name, u.email AS user_email,
               ar.resource_type, ar.mig_profile, ar.quantity, ar.purpose
        FROM allocations a
        JOIN users u ON u.id = a.user_id
        JOIN access_requests ar ON ar.id = a.request_id
        WHERE a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
        ORDER BY a.end_at
        """
    ).fetchall()
    result = rows_to_dicts(rows)
    for allocation in result:
        allocation["resources"] = rows_to_dicts(
            conn.execute(
                """
                SELECT ar.*, rp.label AS resource_pool_label
                FROM allocation_resources ar
                JOIN resource_pools rp ON rp.id = ar.resource_pool_id
                WHERE ar.allocation_id = ?
                """,
                (allocation["id"],),
            ).fetchall()
        )
        allocation["inventory_items"] = allocation_item_labels(conn, allocation["id"])
    return result


def expiring_allocations(conn: sqlite3.Connection) -> list[dict]:
    current = now_iso()
    soon = to_iso(utcnow() + timedelta(days=2))
    rows = conn.execute(
        """
        SELECT a.*, u.name AS user_name, u.email AS user_email,
               ar.resource_type, ar.mig_profile, ar.quantity
        FROM allocations a
        JOIN users u ON u.id = a.user_id
        JOIN access_requests ar ON ar.id = a.request_id
        WHERE a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
          AND a.end_at > ?
          AND a.end_at <= ?
        ORDER BY a.end_at
        """,
        (current, soon),
    ).fetchall()
    return rows_to_dicts(rows)


def extension_list(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    params: list[str] = []
    status_sql = ""
    if status:
        status_sql = "WHERE er.status = ?"
        params.append(status)
    rows = conn.execute(
        f"""
        SELECT er.*, a.start_at, a.end_at, u.name AS requester_name, u.email AS requester_email
        FROM extension_requests er
        JOIN allocations a ON a.id = er.allocation_id
        JOIN users u ON u.id = er.requested_by
        {status_sql}
        ORDER BY er.created_at DESC
        """,
        params,
    ).fetchall()
    return rows_to_dicts(rows)


def my_requests(conn: sqlite3.Connection, user: dict) -> dict:
    requests = rows_to_dicts(
        conn.execute(
            """
            SELECT *
            FROM access_requests
            WHERE requester_id = ?
            ORDER BY created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    )
    for request in requests:
        waiting = conn.execute(
            "SELECT * FROM waiting_queue_entries WHERE request_id = ?",
            (request["id"],),
        ).fetchone()
        if waiting and waiting["status"] == "WAITING":
            request["waiting_position"] = current_waiting_position(conn, waiting["id"])
        else:
            request["waiting_position"] = None

    allocations = rows_to_dicts(
        conn.execute(
            """
            SELECT a.*, ar.resource_type, ar.mig_profile, ar.quantity, ar.purpose
            FROM allocations a
            JOIN access_requests ar ON ar.id = a.request_id
            WHERE a.user_id = ?
            ORDER BY a.created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    )
    extensions = rows_to_dicts(
        conn.execute(
            """
            SELECT er.*
            FROM extension_requests er
            JOIN allocations a ON a.id = er.allocation_id
            WHERE a.user_id = ?
            ORDER BY er.created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    )
    return {"requests": requests, "allocations": allocations, "extensions": extensions}


def all_requests(conn: sqlite3.Connection, limit: int = 300) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ar.*, u.name AS requester_name, u.email AS requester_email
        FROM access_requests ar
        JOIN users u ON u.id = ar.requester_id
        ORDER BY ar.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    requests = rows_to_dicts(rows)
    for request in requests:
        waiting = conn.execute(
            "SELECT id, status FROM waiting_queue_entries WHERE request_id = ?",
            (request["id"],),
        ).fetchone()
        if waiting and waiting["status"] == "WAITING":
            request["waiting_position"] = current_waiting_position(conn, waiting["id"])
        else:
            request["waiting_position"] = None
    return requests


def users_with_roles(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT u.id, u.name, u.email, u.department, u.created_at, u.updated_at,
               GROUP_CONCAT(r.name) AS roles_csv
        FROM users u
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        LEFT JOIN roles r ON r.id = ur.role_id
        GROUP BY u.id, u.name, u.email, u.department, u.created_at, u.updated_at
        ORDER BY u.email
        """
    ).fetchall()
    users = rows_to_dicts(rows)
    for user in users:
        roles = [role for role in (user.pop("roles_csv") or "").split(",") if role]
        user["roles"] = sorted(roles)
        if "ADMIN" in roles:
            user["primary_role"] = "ADMIN"
        elif "OBSERVER" in roles:
            user["primary_role"] = "OBSERVER"
        else:
            user["primary_role"] = "USER"
    return users


def set_user_role(conn: sqlite3.Connection, admin: dict, user_id: str, payload: dict) -> dict:
    role_name = (payload.get("role") or "").strip().upper()
    if role_name not in APP_ROLES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Role must be USER, ADMIN, or OBSERVER.")
    if admin["id"] == user_id and role_name != "ADMIN":
        raise ApiError(HTTPStatus.CONFLICT, "You cannot demote your own admin account.")

    begin(conn)
    try:
        target = row_to_dict(conn.execute("SELECT id, name, email, department FROM users WHERE id = ?", (user_id,)).fetchone())
        if not target:
            raise ApiError(HTTPStatus.NOT_FOUND, "User not found.")
        before = {**target, "roles": get_roles(conn, user_id)}
        if "ADMIN" in before["roles"] and role_name != "ADMIN":
            admin_count = conn.execute(
                """
                SELECT COUNT(DISTINCT ur.user_id) AS count
                FROM user_roles ur
                JOIN roles r ON r.id = ur.role_id
                WHERE r.name = 'ADMIN'
                """
            ).fetchone()["count"]
            if int(admin_count) <= 1:
                raise ApiError(HTTPStatus.CONFLICT, "At least one admin account must remain.")

        role_id = ensure_role(conn, role_name)
        current = now_iso()
        conn.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
        conn.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)", (user_id, role_id))
        conn.execute("UPDATE users SET updated_at = ? WHERE id = ?", (current, user_id))
        after = {**target, "roles": get_roles(conn, user_id)}
        audit(conn, admin["id"], "ADMIN", "SET_USER_ROLE", "user", user_id, before, after)
        commit(conn)
    except Exception:
        rollback(conn)
        raise
    return after


def capacity_snapshot(conn: sqlite3.Connection) -> list[dict]:
    status_counts = inventory_status_counts(conn)
    held_counts = held_capacity_by_type(conn)
    used_counts = active_inventory_used_by_type(conn)
    snapshot = {
        "FULL_GPU": {
            "resource_type": "FULL_GPU",
            "label": "Full GPU",
            "total": 0,
            "reserved": 0,
            "usable": 0,
            "used": 0,
            "held": 0,
            "available": 0,
        },
        "MIG": {
            "resource_type": "MIG",
            "label": "MIG partitions",
            "total": 0,
            "reserved": 0,
            "usable": 0,
            "used": 0,
            "held": 0,
            "available": 0,
        },
    }
    for resource_type, bucket in snapshot.items():
        counts = status_counts[resource_type]
        bucket["total"] = sum(counts.values())
        bucket["reserved"] = counts["MAINTENANCE"] + counts["DISABLED"]
        bucket["usable"] = counts["AVAILABLE"] + counts["ALLOCATED"]
        bucket["used"] = used_counts[resource_type]
        bucket["held"] = held_counts[resource_type]
        bucket["available"] = max(0, counts["AVAILABLE"] - bucket["held"])
        bucket["utilization_percent"] = round((bucket["used"] / bucket["usable"]) * 100, 1) if bucket["usable"] else 0
    return list(snapshot.values())


def analytics_summary(conn: sqlite3.Connection) -> dict:
    pending = pending_requests(conn)
    waiting = waiting_list(conn)
    allocations = active_allocations(conn)
    expiring_2 = expiring_allocations(conn)
    extensions = extension_list(conn)
    pending_extensions = [extension for extension in extensions if extension["status"] == "PENDING_ADMIN"]
    current = now_iso()
    next_7 = to_iso(utcnow() + timedelta(days=7))
    ending_7_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM allocations
        WHERE status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
          AND end_at > ?
          AND end_at <= ?
        """,
        (current, next_7),
    ).fetchone()["count"]

    by_status = rows_to_dicts(
        conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM access_requests
            GROUP BY status
            ORDER BY count DESC, status
            """
        ).fetchall()
    )
    top_users = rows_to_dicts(
        conn.execute(
            """
            SELECT u.name, u.email, COUNT(ar.id) AS request_count
            FROM access_requests ar
            JOIN users u ON u.id = ar.requester_id
            GROUP BY u.id, u.name, u.email
            ORDER BY request_count DESC, u.email
            LIMIT 8
            """
        ).fetchall()
    )
    departments = rows_to_dicts(
        conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(department), ''), 'Unspecified') AS department,
                   COUNT(*) AS request_count
            FROM access_requests
            GROUP BY COALESCE(NULLIF(TRIM(department), ''), 'Unspecified')
            ORDER BY request_count DESC, department
            LIMIT 10
            """
        ).fetchall()
    )
    capacity = capacity_snapshot(conn)
    return {
        "counts": {
            "pending": len(pending),
            "waiting": len(waiting),
            "active": len(allocations),
            "expiring": len(expiring_2),
            "extensions": len(pending_extensions),
            "ending_next_7_days": int(ending_7_count or 0),
        },
        "capacity": capacity,
        "requests_by_status": by_status,
        "top_users": top_users,
        "departments": departments,
    }


def parse_range_days(value: str | None) -> int:
    raw = (value or "30d").strip().lower()
    if raw.endswith("d"):
        raw = raw[:-1]
    try:
        days = int(raw)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Range must be a day count like 30d.") from exc
    return max(1, min(days, 120))


def analytics_utilization(conn: sqlite3.Connection, days: int = 30) -> dict:
    capacity = {item["resource_type"]: item for item in capacity_snapshot(conn)}
    today = utcnow().date()
    series = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        used_rows = conn.execute(
            """
            SELECT rp.resource_type, COALESCE(SUM(ar.quantity), 0) AS used
            FROM allocation_resources ar
            JOIN resource_pools rp ON rp.id = ar.resource_pool_id
            JOIN allocations a ON a.id = ar.allocation_id
            WHERE a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING', 'ENDED')
              AND a.start_at < ?
              AND a.end_at > ?
            GROUP BY rp.resource_type
            """,
            (to_iso(day_end), to_iso(day_start)),
        ).fetchall()
        used_by_type = {row["resource_type"]: int(row["used"] or 0) for row in used_rows}
        full_used = used_by_type.get("FULL_GPU", 0)
        mig_used = used_by_type.get("MIG", 0)
        full_usable = capacity["FULL_GPU"]["usable"]
        mig_usable = capacity["MIG"]["usable"]
        series.append(
            {
                "date": day.isoformat(),
                "full_gpu_used": full_used,
                "full_gpu_utilization": round((full_used / full_usable) * 100, 1) if full_usable else 0,
                "mig_used": mig_used,
                "mig_utilization": round((mig_used / mig_usable) * 100, 1) if mig_usable else 0,
            }
        )
    return {"range_days": days, "series": series}


def analytics_requests(conn: sqlite3.Connection, group_by: str = "day") -> dict:
    group_by = group_by if group_by in ("day", "week") else "day"
    rows = rows_to_dicts(conn.execute("SELECT status, created_at FROM access_requests ORDER BY created_at").fetchall())
    periods: dict[str, int] = {}
    for row in rows:
        created = parse_datetime(row["created_at"]).date()
        if group_by == "week":
            year, week, _ = created.isocalendar()
            key = f"{year}-W{week:02d}"
        else:
            key = created.isoformat()
        periods[key] = periods.get(key, 0) + 1
    by_status = rows_to_dicts(
        conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM access_requests
            GROUP BY status
            ORDER BY count DESC, status
            """
        ).fetchall()
    )
    return {
        "group_by": group_by,
        "series": [{"period": period, "count": count} for period, count in sorted(periods.items())],
        "by_status": by_status,
    }


def analytics_waiting(conn: sqlite3.Connection) -> dict:
    waiting = waiting_list(conn)
    by_queue: dict[str, int] = {}
    positions = []
    for item in waiting:
        by_queue[item["queue_key"]] = by_queue.get(item["queue_key"], 0) + 1
        positions.append({"queue_key": item["queue_key"], "position": item["waiting_position"]})

    promoted_rows = conn.execute(
        """
        SELECT position_created_at, promoted_at
        FROM waiting_queue_entries
        WHERE promoted_at IS NOT NULL
        """
    ).fetchall()
    wait_hours = []
    for row in promoted_rows:
        start = parse_datetime(row["position_created_at"])
        end = parse_datetime(row["promoted_at"])
        wait_hours.append((end - start).total_seconds() / 3600)

    current_wait_hours = []
    for item in waiting:
        start = parse_datetime(item["position_created_at"])
        current_wait_hours.append((utcnow() - start).total_seconds() / 3600)

    return {
        "current_count": len(waiting),
        "by_queue": [{"queue_key": key, "count": count} for key, count in sorted(by_queue.items())],
        "average_wait_hours": round(sum(wait_hours) / len(wait_hours), 2) if wait_hours else 0,
        "current_average_wait_hours": round(sum(current_wait_hours) / len(current_wait_hours), 2)
        if current_wait_hours
        else 0,
        "position_distribution": positions,
    }


def analytics_extensions(conn: sqlite3.Connection) -> dict:
    by_status = rows_to_dicts(
        conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM extension_requests
            GROUP BY status
            ORDER BY count DESC, status
            """
        ).fetchall()
    )
    return {
        "by_status": by_status,
        "recent": extension_list(conn)[:20],
    }


def analytics_bundle(conn: sqlite3.Connection) -> dict:
    return {
        "summary": analytics_summary(conn),
        "utilization": analytics_utilization(conn, 30),
        "requests": analytics_requests(conn, "day"),
        "waiting": analytics_waiting(conn),
        "extensions": analytics_extensions(conn),
    }


def dashboard(conn: sqlite3.Connection) -> dict:
    pending = pending_requests(conn)
    waiting = waiting_list(conn)
    allocations = active_allocations(conn)
    expiring = expiring_allocations(conn)
    extensions = extension_list(conn)
    pending_extensions = [extension for extension in extensions if extension["status"] == "PENDING_ADMIN"]
    return {
        "counts": {
            "pending": len(pending),
            "waiting": len(waiting),
            "active": len(allocations),
            "expiring": len(expiring),
            "extensions": len(pending_extensions),
        },
        "pending": pending,
        "requests": all_requests(conn),
        "waiting": waiting,
        "active": allocations,
        "expiring": expiring,
        "extensions": extensions,
        "inventory": inventory_with_load(conn),
        "analytics": analytics_bundle(conn),
    }


def normalize_inventory_payload(payload: dict, existing: dict | None = None) -> dict:
    resource_type = payload.get("resource_type", existing["resource_type"] if existing else None)
    mig_profile = payload.get("mig_profile", existing["mig_profile"] if existing else None) or None
    if resource_type == "FULL_GPU":
        mig_profile = None
    validate_resource(resource_type, mig_profile, 1)

    label = (payload.get("label", existing["label"] if existing else "") or "").strip()
    if not label:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Inventory label is required.")

    status = (payload.get("status", existing["status"] if existing else "AVAILABLE") or "AVAILABLE").upper()
    if status not in INVENTORY_STATUSES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Inventory status must be AVAILABLE, ALLOCATED, MAINTENANCE, or DISABLED.")

    notes = (payload.get("notes", existing["notes"] if existing else "") or "").strip() or None
    return {
        "resource_type": resource_type,
        "mig_profile": mig_profile,
        "label": label,
        "status": status,
        "notes": notes,
    }


def inventory_active_allocation(conn: sqlite3.Connection, item_id: str) -> dict | None:
    return row_to_dict(
        conn.execute(
            """
            SELECT a.id, a.status, a.start_at, a.end_at, u.email AS user_email
            FROM allocation_inventory_items aii
            JOIN allocations a ON a.id = aii.allocation_id
            JOIN users u ON u.id = a.user_id
            WHERE aii.inventory_item_id = ?
              AND a.status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING')
            ORDER BY a.start_at ASC
            LIMIT 1
            """,
            (item_id,),
        ).fetchone()
    )


def get_inventory_item(conn: sqlite3.Connection, item_id: str) -> dict:
    row = conn.execute(
        """
        SELECT ii.*, rp.label AS pool_label, ds.name AS server_name
        FROM inventory_items ii
        LEFT JOIN resource_pools rp ON rp.id = ii.resource_pool_id
        LEFT JOIN dgx_servers ds ON ds.id = rp.server_id
        WHERE ii.id = ?
        """,
        (item_id,),
    ).fetchone()
    if not row:
        raise ApiError(HTTPStatus.NOT_FOUND, "Inventory item not found.")
    item = row_to_dict(row)
    allocation = inventory_active_allocation(conn, item_id)
    item["allocation"] = allocation
    item["in_use"] = bool(allocation)
    item["effective_status"] = "ALLOCATED" if allocation else item["status"]
    item["updatedAt"] = item["updated_at"]
    item["createdAt"] = item["created_at"]
    return item


def inventory_response(conn: sqlite3.Connection) -> dict:
    return {"inventory": inventory_with_load(conn), "summary": capacity_snapshot(conn)}


def create_inventory_item(conn: sqlite3.Connection, actor: dict, payload: dict) -> dict:
    data = normalize_inventory_payload(payload)
    begin(conn)
    try:
        current = now_iso()
        pool_id = ensure_resource_pool_for_inventory(conn, data["resource_type"], data["mig_profile"])
        item_id = new_id()
        conn.execute(
            """
            INSERT INTO inventory_items (
              id, resource_pool_id, resource_type, mig_profile, label,
              status, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                pool_id,
                data["resource_type"],
                data["mig_profile"],
                data["label"],
                data["status"],
                data["notes"],
                current,
                current,
            ),
        )
        sync_resource_pool_capacity(conn, pool_id)
        after = get_inventory_item(conn, item_id)
        audit(conn, actor["id"], "ADMIN", "CREATE_INVENTORY_ITEM", "inventory_item", item_id, None, after)
        commit(conn)
    except Exception:
        rollback(conn)
        raise

    if data["status"] == "AVAILABLE":
        process_waiting_list()
    return after


def update_inventory_item(conn: sqlite3.Connection, actor: dict, item_id: str, payload: dict) -> dict:
    begin(conn)
    try:
        before = get_inventory_item(conn, item_id)
        data = normalize_inventory_payload(payload, before)
        active_allocation = before["allocation"]
        type_changed = data["resource_type"] != before["resource_type"] or data["mig_profile"] != before["mig_profile"]
        status_changed = data["status"] != before["status"]
        if active_allocation and (type_changed or status_changed):
            raise ApiError(
                HTTPStatus.CONFLICT,
                "This inventory item is assigned to an active allocation. Edit label/notes only, or cancel/end the allocation first.",
            )

        current = now_iso()
        old_pool_id = before["resource_pool_id"]
        new_pool_id = old_pool_id
        if type_changed:
            new_pool_id = ensure_resource_pool_for_inventory(conn, data["resource_type"], data["mig_profile"])

        conn.execute(
            """
            UPDATE inventory_items
            SET resource_pool_id = ?, resource_type = ?, mig_profile = ?,
                label = ?, status = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                new_pool_id,
                data["resource_type"],
                data["mig_profile"],
                data["label"],
                data["status"],
                data["notes"],
                current,
                item_id,
            ),
        )
        sync_resource_pool_capacity(conn, old_pool_id)
        if new_pool_id != old_pool_id:
            sync_resource_pool_capacity(conn, new_pool_id)
        after = get_inventory_item(conn, item_id)
        audit(conn, actor["id"], "ADMIN", "UPDATE_INVENTORY_ITEM", "inventory_item", item_id, before, after)
        commit(conn)
    except Exception:
        rollback(conn)
        raise

    if data["status"] == "AVAILABLE" and before["status"] != "AVAILABLE":
        process_waiting_list()
    return after


def delete_inventory_item(conn: sqlite3.Connection, actor: dict, item_id: str) -> dict:
    before = get_inventory_item(conn, item_id)
    if before["allocation"]:
        raise ApiError(
            HTTPStatus.CONFLICT,
            "This inventory item is assigned to an active allocation and cannot be deleted.",
        )
    pool_id = before["resource_pool_id"]
    print(f"[DELETE] item_id={item_id}, pool_id={pool_id}")
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM inventory_items WHERE id = ?", (item_id,))
        print(f"[DELETE] Deleted item from inventory_items")
        sync_resource_pool_capacity(conn, pool_id)
        audit(conn, actor["id"], "ADMIN", "DELETE_INVENTORY_ITEM", "inventory_item", item_id, before, None)
        # Clean up orphaned pool if it has no more items
        remaining = conn.execute("SELECT COUNT(*) as count FROM inventory_items WHERE resource_pool_id = ?", (pool_id,)).fetchone()
        print(f"[DELETE] Remaining items in pool {pool_id}: {remaining['count']}")
        if remaining["count"] == 0:
            print(f"[DELETE] Deleting orphaned pool {pool_id}")
            conn.execute("DELETE FROM resource_pools WHERE id = ?", (pool_id,))
        conn.execute("COMMIT")
        print(f"[DELETE] Transaction committed")
    except Exception as e:
        print(f"[DELETE] Exception: {e}")
        conn.execute("ROLLBACK")
        raise
    return {"id": item_id, "success": True}


class AppHandler(BaseHTTPRequestHandler):
    server_version = "DGXAccess/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.set_common_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PATCH(self) -> None:
        self.dispatch("PATCH")

    def do_DELETE(self) -> None:
        print(f"[HTTP] DELETE {self.path}")
        self.dispatch("DELETE")

    def set_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")

    def json_response(self, data: dict | list, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.set_common_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error_response(self, status: int, message: str) -> None:
        self.json_response({"error": message}, status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid JSON body.") from exc

    def bearer_token(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return header[7:].strip()
        return None

    def require_user(self, conn: sqlite3.Connection) -> dict:
        user = get_session_user(conn, self.bearer_token())
        if not user:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Authentication required.")
        return user

    def require_admin(self, conn: sqlite3.Connection) -> dict:
        user = self.require_user(conn)
        if "ADMIN" not in user["roles"]:
            raise ApiError(HTTPStatus.FORBIDDEN, "Admin access required.")
        return user

    def require_admin_or_observer(self, conn: sqlite3.Connection) -> dict:
        user = self.require_user(conn)
        if "ADMIN" not in user["roles"] and "OBSERVER" not in user["roles"]:
            raise ApiError(HTTPStatus.FORBIDDEN, "Admin or observer access required.")
        return user

    def require_mutating_user(self, conn: sqlite3.Connection) -> dict:
        user = self.require_user(conn)
        if "OBSERVER" in user["roles"] and "ADMIN" not in user["roles"]:
            raise ApiError(HTTPStatus.FORBIDDEN, "Observer accounts are read-only.")
        return user

    def dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if method == "DELETE":
            print(f"[DISPATCH-ALL-DELETE] {method} {path}")
        if path.startswith("/api/") and "DELETE" in method.upper():
            print(f"\n[DISPATCH] {method} {path}")
        try:
            if path.startswith("/api/"):
                self.handle_api(method, path, parse_qs(parsed.query))
            else:
                self.handle_static(path)
        except ApiError as exc:
            print(f"[DISPATCH] ApiError: {exc.message}")
            self.error_response(exc.status, exc.message)
        except Exception as exc:
            print(f"[DISPATCH] Unhandled error: {exc}")
            self.error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error.")

    def handle_static(self, path: str) -> None:
        if path in ("/", "/dashboard", "/admin/dashboard", "/observer/dashboard"):
            path = "/index.html"
        requested = (PUBLIC_DIR / path.lstrip("/")).resolve()
        public_root = PUBLIC_DIR.resolve()
        if not str(requested).startswith(str(public_root)) or not requested.exists() or requested.is_dir():
            self.error_response(HTTPStatus.NOT_FOUND, "Not found.")
            return
        body = requested.read_bytes()
        content_type, _ = mimetypes.guess_type(str(requested))
        self.send_response(HTTPStatus.OK)
        self.set_common_headers()
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api(self, method: str, path: str, query: dict) -> None:
        if "inventory" in path and method == "DELETE":
            print(f"[EARLY-DEBUG] DELETE {path}")
        with connect() as conn:
            if method == "POST" and path == "/api/auth/register":
                payload = self.read_json()
                name = (payload.get("name") or "").strip()
                email = (payload.get("email") or "").strip().lower()
                department = (payload.get("department") or "").strip() or None
                password = payload.get("password") or ""
                if not name or not email:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Name and email are required.")
                current = now_iso()
                begin(conn)
                try:
                    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                        raise ApiError(HTTPStatus.CONFLICT, "A user with this email already exists.")
                    user_id = new_id()
                    conn.execute(
                        """
                        INSERT INTO users (id, name, email, department, password_hash, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (user_id, name, email, department, hash_password(password), current, current),
                    )
                    role_id = conn.execute("SELECT id FROM roles WHERE name = 'USER'").fetchone()["id"]
                    conn.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)", (user_id, role_id))
                    token = create_session(conn, user_id)
                    audit(conn, user_id, "USER", "REGISTER", "user", user_id, None, {"email": email})
                    commit(conn)
                except Exception:
                    rollback(conn)
                    raise
                user = get_session_user(conn, token)
                self.json_response({"token": token, "user": user}, HTTPStatus.CREATED)
                return

            if method == "POST" and path == "/api/auth/login":
                payload = self.read_json()
                email = (payload.get("email") or "").strip().lower()
                password = payload.get("password") or ""
                row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if not row or not verify_password(row["password_hash"], password):
                    raise ApiError(HTTPStatus.UNAUTHORIZED, "Invalid email or password.")
                token = create_session(conn, row["id"])
                user = get_session_user(conn, token)
                self.json_response({"token": token, "user": user})
                return

            if method == "POST" and path == "/api/auth/logout":
                token = self.bearer_token()
                if token:
                    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                self.json_response({"ok": True})
                return

            if method == "GET" and path == "/api/me":
                user = self.require_user(conn)
                self.json_response({"user": user})
                return

            if method == "POST" and path == "/api/requests":
                user = self.require_mutating_user(conn)
                result = submit_request(conn, user, self.read_json())
                self.json_response(result, HTTPStatus.CREATED)
                return

            if method == "GET" and path == "/api/requests/mine":
                user = self.require_user(conn)
                self.json_response(my_requests(conn, user))
                return

            if method == "GET" and path.startswith("/api/requests/"):
                user = self.require_user(conn)
                request_id = path.split("/")[-1]
                self.json_response(get_request_detail(conn, request_id, user))
                return

            if method == "PATCH" and path.startswith("/api/requests/") and path.endswith("/cancel"):
                user = self.require_mutating_user(conn)
                request_id = path.split("/")[-2]
                self.json_response(cancel_request(conn, user, request_id))
                return

            if method == "PATCH" and path.startswith("/api/allocations/") and path.endswith("/cancel"):
                user = self.require_mutating_user(conn)
                allocation_id = path.split("/")[-2]
                self.json_response(cancel_allocation(conn, user, allocation_id))
                return

            if method == "POST" and path.startswith("/api/allocations/") and path.endswith("/extensions"):
                user = self.require_mutating_user(conn)
                allocation_id = path.split("/")[-2]
                self.json_response(request_extension(conn, user, allocation_id, self.read_json()), HTTPStatus.CREATED)
                return

            if method == "GET" and path == "/api/admin/dashboard":
                self.require_admin_or_observer(conn)
                self.json_response(dashboard(conn))
                return

            if method == "GET" and path in ("/api/inventory", "/api/admin/inventory"):
                self.require_admin_or_observer(conn)
                self.json_response(inventory_response(conn))
                return

            if method == "GET" and path == "/api/admin/users":
                self.require_admin(conn)
                self.json_response({"users": users_with_roles(conn), "roles": list(APP_ROLES)})
                return

            if method == "PATCH" and path.startswith("/api/admin/users/") and path.endswith("/role"):
                admin = self.require_admin(conn)
                user_id = path.split("/")[-2]
                self.json_response(set_user_role(conn, admin, user_id, self.read_json()))
                return

            if method == "POST" and path in ("/api/inventory", "/api/admin/inventory"):
                admin = self.require_admin(conn)
                self.json_response(create_inventory_item(conn, admin, self.read_json()), HTTPStatus.CREATED)
                return

            if method == "PATCH" and (path.startswith("/api/inventory/") or path.startswith("/api/admin/inventory/")):
                admin = self.require_admin(conn)
                item_id = path.split("/")[-1]
                self.json_response(update_inventory_item(conn, admin, item_id, self.read_json()))
                return

            if method == "DELETE" and (path.startswith("/api/inventory/") or path.startswith("/api/admin/inventory/")):
                print(f"[DEBUG-DELETE] Handler triggered for {path}")
                admin = self.require_admin(conn)
                item_id = path.split("/")[-1]
                print(f"[DEBUG-DELETE] Calling delete_inventory_item for {item_id}")
                response = delete_inventory_item(conn, admin, item_id)
                print(f"[DEBUG-DELETE] Got response: {response}")
                self.json_response(response)
                print(f"[DEBUG-DELETE] Sent JSON response")
                return

            if method == "POST" and path.startswith("/api/admin/requests/") and path.endswith("/approve"):
                admin = self.require_admin(conn)
                request_id = path.split("/")[-2]
                self.json_response(approve_request(conn, admin, request_id, self.read_json()))
                return

            if method == "POST" and path.startswith("/api/admin/requests/") and path.endswith("/reject"):
                admin = self.require_admin(conn)
                request_id = path.split("/")[-2]
                self.json_response(reject_request(conn, admin, request_id, self.read_json()))
                return

            if method == "POST" and path.startswith("/api/admin/extensions/") and path.endswith("/approve"):
                admin = self.require_admin(conn)
                extension_id = path.split("/")[-2]
                self.json_response(approve_extension(conn, admin, extension_id))
                return

            if method == "POST" and path.startswith("/api/admin/extensions/") and path.endswith("/reject"):
                admin = self.require_admin(conn)
                extension_id = path.split("/")[-2]
                self.json_response(reject_extension(conn, admin, extension_id, self.read_json()))
                return

            if method == "GET" and path == "/api/admin/audit":
                self.require_admin_or_observer(conn)
                rows = conn.execute(
                    """
                    SELECT al.*, u.email AS actor_email
                    FROM audit_logs al
                    LEFT JOIN users u ON u.id = al.actor_id
                    ORDER BY al.created_at DESC
                    LIMIT 200
                    """
                ).fetchall()
                self.json_response({"audit": rows_to_dicts(rows)})
                return

            if method == "GET" and path == "/api/analytics/summary":
                self.require_admin_or_observer(conn)
                self.json_response(analytics_summary(conn))
                return

            if method == "GET" and path == "/api/analytics/utilization":
                self.require_admin_or_observer(conn)
                days = parse_range_days((query.get("range") or ["30d"])[0])
                self.json_response(analytics_utilization(conn, days))
                return

            if method == "GET" and path == "/api/analytics/requests":
                self.require_admin_or_observer(conn)
                group_by = (query.get("groupBy") or ["day"])[0]
                self.json_response(analytics_requests(conn, group_by))
                return

            if method == "GET" and path == "/api/analytics/waiting":
                self.require_admin_or_observer(conn)
                self.json_response(analytics_waiting(conn))
                return

            if method == "GET" and path == "/api/analytics/extensions":
                self.require_admin_or_observer(conn)
                self.json_response(analytics_extensions(conn))
                return

            if method == "GET" and path == "/api/admin/emails":
                self.require_admin(conn)
                rows = conn.execute(
                    """
                    SELECT *
                    FROM email_notifications
                    ORDER BY created_at DESC
                    LIMIT 200
                    """
                ).fetchall()
                self.json_response({"emails": rows_to_dicts(rows)})
                return

            if method == "POST" and path == "/api/system/jobs/run":
                self.require_admin(conn)
                self.json_response(run_jobs_once())
                return

            raise ApiError(HTTPStatus.NOT_FOUND, "API endpoint not found.")


def main() -> None:
    init_db()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    stop_event = threading.Event()
    scheduler = threading.Thread(target=scheduler_loop, args=(stop_event,), daemon=True)
    scheduler.start()
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"DGX Access app running at http://{host}:{port}")
    print("Seed admin: admin@dgx.local / admin1234")
    print("Seed user:  user@dgx.local / user1234")
    print("Seed observer: observer@dgx.local / observer1234")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
