PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  department TEXT,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS user_roles (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, role_id)
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dgx_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  location TEXT,
  status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'MAINTENANCE', 'RETIRED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_pools (
  id TEXT PRIMARY KEY,
  server_id TEXT REFERENCES dgx_servers(id) ON DELETE SET NULL,
  resource_type TEXT NOT NULL CHECK (resource_type IN ('FULL_GPU', 'MIG')),
  mig_profile TEXT,
  label TEXT NOT NULL,
  total_capacity INTEGER NOT NULL CHECK (total_capacity >= 0),
  reserved_capacity INTEGER NOT NULL DEFAULT 0 CHECK (reserved_capacity >= 0),
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (
    (resource_type = 'FULL_GPU' AND mig_profile IS NULL)
    OR
    (resource_type = 'MIG' AND mig_profile IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS inventory_items (
  id TEXT PRIMARY KEY,
  resource_pool_id TEXT REFERENCES resource_pools(id) ON DELETE SET NULL,
  resource_type TEXT NOT NULL CHECK (resource_type IN ('FULL_GPU', 'MIG')),
  mig_profile TEXT,
  label TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'ALLOCATED', 'MAINTENANCE', 'DISABLED')),
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (
    (resource_type = 'FULL_GPU' AND mig_profile IS NULL)
    OR
    (resource_type = 'MIG' AND mig_profile IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_inventory_items_capacity
  ON inventory_items(resource_pool_id, resource_type, mig_profile, status, label);

CREATE TABLE IF NOT EXISTS access_requests (
  id TEXT PRIMARY KEY,
  requester_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (
    status IN (
      'DRAFT',
      'SUBMITTED',
      'WAITING',
      'PENDING_ADMIN',
      'APPROVED',
      'ACTIVE',
      'EXPIRING',
      'EXTENDED',
      'ENDED',
      'CANCELLED',
      'REJECTED'
    )
  ),
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  department TEXT,
  purpose TEXT NOT NULL,
  urgency TEXT,
  requested_start_at TEXT NOT NULL,
  requested_end_at TEXT NOT NULL,
  requested_duration_minutes INTEGER NOT NULL,
  resource_type TEXT NOT NULL CHECK (resource_type IN ('FULL_GPU', 'MIG')),
  mig_profile TEXT,
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  notes TEXT,
  waiting_queue_key TEXT,
  waiting_position_snapshot INTEGER,
  submitted_at TEXT,
  cancelled_at TEXT,
  cancelled_by TEXT REFERENCES users(id) ON DELETE SET NULL,
  rejected_at TEXT,
  rejected_by TEXT REFERENCES users(id) ON DELETE SET NULL,
  rejection_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS waiting_queue_entries (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES access_requests(id) ON DELETE CASCADE,
  queue_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('WAITING', 'PROMOTED', 'CANCELLED', 'EXPIRED')),
  position_created_at TEXT NOT NULL,
  promoted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_waiting_queue_fifo
  ON waiting_queue_entries(queue_key, status, position_created_at, id);

CREATE TABLE IF NOT EXISTS capacity_holds (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES access_requests(id) ON DELETE CASCADE,
  resource_pool_id TEXT NOT NULL REFERENCES resource_pools(id) ON DELETE CASCADE,
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('HELD', 'RELEASED', 'CONVERTED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capacity_holds_pool_time
  ON capacity_holds(resource_pool_id, status, start_at, end_at);

CREATE TABLE IF NOT EXISTS allocations (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES access_requests(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('SCHEDULED', 'ACTIVE', 'EXPIRING', 'ENDED', 'CANCELLED')),
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  approved_by TEXT NOT NULL REFERENCES users(id),
  approved_at TEXT NOT NULL,
  admin_remarks TEXT,
  cancelled_by TEXT REFERENCES users(id) ON DELETE SET NULL,
  cancelled_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_allocations_status_time
  ON allocations(status, start_at, end_at);

CREATE TABLE IF NOT EXISTS allocation_resources (
  id TEXT PRIMARY KEY,
  allocation_id TEXT NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
  resource_pool_id TEXT NOT NULL REFERENCES resource_pools(id) ON DELETE CASCADE,
  quantity INTEGER NOT NULL CHECK (quantity > 0)
);

CREATE TABLE IF NOT EXISTS allocation_inventory_items (
  id TEXT PRIMARY KEY,
  allocation_id TEXT NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
  inventory_item_id TEXT NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  UNIQUE (allocation_id, inventory_item_id)
);

CREATE INDEX IF NOT EXISTS idx_allocation_inventory_items_item
  ON allocation_inventory_items(inventory_item_id, allocation_id);

CREATE TABLE IF NOT EXISTS extension_requests (
  id TEXT PRIMARY KEY,
  allocation_id TEXT NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
  requested_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  requested_duration_minutes INTEGER NOT NULL CHECK (requested_duration_minutes > 0),
  justification TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('SUBMITTED', 'PENDING_ADMIN', 'APPROVED', 'REJECTED', 'CANCELLED')),
  requested_end_at TEXT NOT NULL,
  approved_by TEXT REFERENCES users(id) ON DELETE SET NULL,
  approved_at TEXT,
  rejected_by TEXT REFERENCES users(id) ON DELETE SET NULL,
  rejected_at TEXT,
  rejection_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extension_requests_status
  ON extension_requests(status, created_at);

CREATE TABLE IF NOT EXISTS email_notifications (
  id TEXT PRIMARY KEY,
  recipient_email TEXT NOT NULL,
  template_key TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('QUEUED', 'SENT', 'FAILED')),
  related_entity_type TEXT,
  related_entity_id TEXT,
  sent_at TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_email_notifications_queue
  ON email_notifications(status, created_at);

CREATE TABLE IF NOT EXISTS audit_logs (
  id TEXT PRIMARY KEY,
  actor_id TEXT,
  actor_type TEXT NOT NULL CHECK (actor_type IN ('USER', 'ADMIN', 'SYSTEM')),
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_entity
  ON audit_logs(entity_type, entity_id, created_at);
