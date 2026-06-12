# DGX Access Manager

A dependency-free full-stack web app for managing DGX GPU access requests with:

- User request intake
- Admin approval and allocation
- Timed allocation windows
- Separate FIFO queues per compatible resource type
- Capacity holds to avoid over-promoting or overbooking pending requests
- MIG partition and full GPU inventory
- Observer read-only analytics role
- Analytics dashboard with utilization, request flow, queue, extension, user, and department charts
- Extension requests
- Cancellation with automatic capacity release
- Email notification templates and delivery log
- Scheduler jobs for reminders, activation, expiry, and waiting-list processing
- Audit log for user, admin, and system actions

## Run

Use the bundled Python runtime in this Codex workspace:

```powershell
& "C:\Users\kulpr\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" app.py
```

Then open:

```text
http://127.0.0.1:8000
```


## Roles

The app seeds three roles in the `roles` table:

```text
USER       Requester workflow
ADMIN      Approvals, allocations, inventory, jobs, role management
OBSERVER   Strict read-only analytics and operations visibility
```

Observers can view dashboards, request lists, waiting queues, active allocations, extensions, inventory, and audit logs. Mutating endpoints reject observer-only accounts with `403 Forbidden`.

## Email

By default, email notifications are logged to stdout and recorded in the `email_notifications` table.

Set these environment variables to send real SMTP email:

```powershell
$env:SMTP_HOST="smtp.example.com"
$env:SMTP_PORT="587"
$env:SMTP_FROM="dgx-access@example.com"
$env:SMTP_USERNAME="..."
$env:SMTP_PASSWORD="..."
$env:SMTP_TLS="true"
```

## Cancellation Capacity Policy

When a user cancels an active allocation, the app automatically marks the allocation as `CANCELLED`, which immediately removes it from capacity calculations. It writes an audit event, sends user/admin email notifications, and triggers FIFO waiting-list processing.

This is the recommended default because the system already owns allocation windows and capacity math. Manual release would waste GPU time, create stale inventory, and delay FIFO users. Admins can still reserve capacity through inventory if cleanup, maintenance, or reconfiguration is needed.

## State Machine

```text
DRAFT
  -> SUBMITTED
      -> PENDING_ADMIN   capacity available, hold created
      -> WAITING         capacity unavailable, FIFO entry created

WAITING
  -> PENDING_ADMIN       FIFO processor promotes earliest compatible request
  -> CANCELLED
  -> REJECTED

PENDING_ADMIN
  -> APPROVED            scheduled allocation
  -> ACTIVE              immediate allocation
  -> EXPIRING            allocation ends within 2 days
  -> CANCELLED
  -> REJECTED

APPROVED
  -> ACTIVE
  -> EXPIRING
  -> ENDED
  -> CANCELLED

ACTIVE
  -> EXPIRING
  -> EXTENDED
  -> ENDED
  -> CANCELLED

EXPIRING
  -> EXTENDED
  -> ENDED
  -> CANCELLED

EXTENDED
  -> EXPIRING
  -> ENDED
  -> CANCELLED

REJECTED, ENDED, CANCELLED are terminal.
```

## API Summary

Auth:

```text
POST /api/auth/register
POST /api/auth/login
POST /api/auth/logout
GET  /api/me
```

Requester:

```text
POST  /api/requests
GET   /api/requests/mine
GET   /api/requests/:id
PATCH /api/requests/:id/cancel
PATCH /api/allocations/:id/cancel
POST  /api/allocations/:id/extensions
```

Admin:

```text
GET    /api/admin/dashboard
GET    /api/admin/inventory
GET    /api/admin/users
PATCH  /api/admin/users/:id/role
POST   /api/admin/inventory
PATCH  /api/admin/inventory/:id
DELETE /api/admin/inventory/:id
POST   /api/admin/requests/:id/approve
POST   /api/admin/requests/:id/reject
POST   /api/admin/extensions/:id/approve
POST   /api/admin/extensions/:id/reject
GET    /api/admin/audit
GET    /api/admin/emails
POST   /api/system/jobs/run
```

Analytics, readable by `ADMIN` and `OBSERVER`:

```text
GET /api/analytics/summary
GET /api/analytics/utilization?range=30d
GET /api/analytics/requests?groupBy=day
GET /api/analytics/waiting
GET /api/analytics/extensions
```

## UI Enhancements

- DGX hero asset: `public/images/dgx-hero.png`
- Analytics dashboard with KPI cards, SVG utilization chart, donut charts, and bar charts
- Observer-safe read-only console
- Admin user-role management
- Dark mode toggle
- Animated status pills, cards, charts, toasts, empty states, and loading skeletons
- `prefers-reduced-motion` support

## Verification Checklist

- Observer can sign in and view dashboard charts.
- Observer can view requests, waiting list, active allocations, extensions, inventory, and audit logs.
- Observer receives `403` for request submission, cancellation, allocation cancellation, extension request, inventory update, approvals, manual jobs, and role changes.
- Admin can promote/demote users among `USER`, `OBSERVER`, and `ADMIN`.
- Admin workflow for approvals, inventory, extensions, FIFO jobs, and cancellation remains available.

## Scheduler Jobs

The app starts a background scheduler that runs every 30 seconds:

- Activates scheduled allocations once `start_at <= now`
- Sends expiry reminders when allocation end time is within 2 days
- Ends allocations once `end_at <= now`
- Processes FIFO waiting lists
- Sends queued email notifications

## FIFO Rules

Queues are separate by compatibility key:

```text
FULL_GPU
MIG:1G.10GB
MIG:2G.20GB
MIG:3G.40GB
MIG:7G.80GB
```

When capacity opens, the processor scans each queue by:

```text
ORDER BY position_created_at ASC, id ASC
```

The earliest compatible request is promoted to `PENDING_ADMIN` and receives a capacity hold. The hold prevents later requests from consuming the same capacity while admin review is pending.

## Files

```text
app.py              Backend API, scheduler, auth, FIFO, email, audit
schema.sql          SQLite schema
public/index.html   App shell
public/app.js       Frontend behavior
public/styles.css   Frontend styles
```
"# DGX-Request-App" 
"# DGX-Request-App" 
"# DGX-Request-App" 
