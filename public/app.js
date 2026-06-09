const state = {
  token: localStorage.getItem("dgx_token"),
  theme: localStorage.getItem("dgx_theme") || "dark",
  authMode: "login",
  user: null,
  view: "dashboard",
  data: null,
  users: null,
  audit: null,
  emails: null,
  loading: false,
};

const root = document.getElementById("app");

const STATUS_LABELS = {
  DRAFT: "Draft",
  SUBMITTED: "Submitted",
  WAITING: "Waiting",
  PENDING_ADMIN: "Pending admin",
  APPROVED: "Approved",
  ACTIVE: "Active",
  EXPIRING: "Expiring",
  EXTENDED: "Extended",
  ENDED: "Ended",
  CANCELLED: "Cancelled",
  REJECTED: "Rejected",
  SCHEDULED: "Scheduled",
  QUEUED: "Queued",
  SENT: "Sent",
  FAILED: "Failed",
};

// inventory items cache (separate from dashboard payload)
state.inventory = null;

const CHART_COLORS = ["#2dd4bf", "#60a5fa", "#f59e0b", "#f43f5e", "#8b5cf6", "#22c55e", "#64748b"];

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function hasRole(role) {
  return Boolean(state.user?.roles?.includes(role));
}

function isOpsRole() {
  return hasRole("ADMIN") || hasRole("OBSERVER");
}

function isObserverOnly() {
  return hasRole("OBSERVER") && !hasRole("ADMIN");
}

function roleLabel() {
  if (hasRole("ADMIN")) return "Admin";
  if (hasRole("OBSERVER")) return "Observer";
  return "Requester";
}

function formatDate(value) {
  if (!value) return "Not set";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDuration(minutes) {
  const total = Number(minutes || 0);
  if (!total) return "0h";
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  return mins ? `${hours}h ${mins}m` : `${hours}h`;
}

function toDateTimeLocal(date = new Date(Date.now() + 60 * 60 * 1000)) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function normalizeDateTimeLocal(value) {
  if (!value) return value;
  return new Date(value).toISOString();
}

function resourceLabel(item) {
  const type = item.resource_type === "MIG" ? `MIG ${item.mig_profile || ""}`.trim() : "Full GPU";
  const quantity = Number(item.quantity || 0);
  return `${type}${quantity ? ` x${quantity}` : ""}`;
}

function statusChip(status) {
  const label = STATUS_LABELS[status] || status || "Unknown";
  return `<span class="status" data-status="${escapeHtml(status || "UNKNOWN")}">${escapeHtml(label)}</span>`;
}

function toast(message, type = "success") {
  let container = document.querySelector(".notice-container");
  if (!container) {
    container = document.createElement("div");
    container.className = "notice-container";
    document.body.appendChild(container);
  }
  const note = document.createElement("div");
  note.className = `notice ${type}`;
  note.textContent = message;
  container.appendChild(note);
  window.setTimeout(() => note.remove(), 4200);
}

async function api(path, options = {}) {
  const { auth = true, ...requestOptions } = options;
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (auth && state.token) headers.Authorization = `Bearer ${state.token}`;

  if (requestOptions.method === "DELETE") {
    console.log(`[API] Sending ${requestOptions.method} ${path}`);
  }

  const response = await fetch(path, { ...requestOptions, headers });
  async function api(path, options = {}) {
  const { auth = true, ...requestOptions } = options;
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (auth && state.token) headers.Authorization = `Bearer ${state.token}`;

  console.log('[API]', requestOptions.method || 'GET', path); // ← add this

  const response = await fetch(path, { ...requestOptions, headers });
  const text = await response.text();
  const body = text ? JSON.parse(text) : {};

  if (requestOptions.method === "DELETE") {
    console.log(`[API] Response status: ${response.status}`, body);
  }

  if (response.status === 401) {
    if (auth) {
      clearSession();
      throw new Error("Your session expired. Please sign in again.");
    }
  }
  if (!response.ok) {
    throw new Error(body.error || "Request failed.");
  }
  return body;
}

function clearSession() {
  state.token = null;
  state.user = null;
  state.data = null;
  state.users = null;
  state.audit = null;
  state.emails = null;
  localStorage.removeItem("dgx_token");
}

async function submitForm(event, handler, form) {
  event.preventDefault();
  const button = event.submitter;
  const original = button?.textContent;
  try {
    if (button) {
      button.disabled = true;
      button.textContent = "Working...";
    }
    await handler(new FormData(form), form);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

async function loadData(options = {}) {
  if (!state.user) return;
  state.loading = true;
  if (!options.silent) renderApp();
  try {
    if (isOpsRole()) {
      state.data = await api("/api/admin/dashboard");
      if (!["dashboard", "requests", "waiting", "allocations", "extensions", "inventory", "audit", "users", "emails"].includes(state.view)) {
        state.view = "dashboard";
      }
    } else {
      state.data = await api("/api/requests/mine");
      state.view = "request";
    }
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.loading = false;
    renderApp();
  }
}

async function loadUsers() {
  if (!hasRole("ADMIN")) return;
  state.users = null;
  renderApp();
  try {
    const result = await api("/api/admin/users");
    state.users = result.users;
    renderApp();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadAudit() {
  if (!isOpsRole()) return;
  state.audit = null;
  renderApp();
  try {
    const result = await api("/api/admin/audit");
    state.audit = result.audit;
    renderApp();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadEmails() {
  if (!hasRole("ADMIN")) return;
  state.emails = null;
  renderApp();
  try {
    const result = await api("/api/admin/emails");
    state.emails = result.emails;
    renderApp();
  } catch (error) {
    toast(error.message, "error");
  }
}

function setToken(token, user) {
  state.token = token;
  state.user = user;
  localStorage.setItem("dgx_token", token);
  state.view = isOpsRole() ? "dashboard" : "request";
}

async function handleLogin(formData) {
  const result = await api("/api/auth/login", {
    method: "POST",
    auth: false,
    body: JSON.stringify(Object.fromEntries(formData)),
  });
  setToken(result.token, result.user);
  toast("Signed in successfully.");
  await loadData({ silent: true });
}

async function handleRegister(formData) {
  const result = await api("/api/auth/register", {
    method: "POST",
    auth: false,
    body: JSON.stringify(Object.fromEntries(formData)),
  });
  setToken(result.token, result.user);
  toast("Account created. You can submit a request now.");
  await loadData({ silent: true });
}

async function handleLogout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    // Local logout still succeeds if the session was already gone.
  }
  clearSession();
  renderApp();
}

async function handleCreateRequest(formData) {
  const payload = Object.fromEntries(formData);
  payload.quantity = Number(payload.quantity || 1);
  payload.duration_hours = Number(payload.duration_hours || 1);
  payload.requested_start_at = normalizeDateTimeLocal(payload.requested_start_at);
  if (payload.resource_type === "FULL_GPU") payload.mig_profile = null;
  await api("/api/requests", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  toast("Request submitted. Capacity and FIFO checks are running.");
  await loadData({ silent: true });
}

async function handleApprove(requestId) {
  await api(`/api/admin/requests/${requestId}/approve`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  toast("Request approved and allocation created.");
  await loadData({ silent: true });
}

async function handleReject(requestId) {
  const reason = window.prompt("Reason for rejection?");
  if (!reason) return;
  await api(`/api/admin/requests/${requestId}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
  toast("Request rejected.");
  await loadData({ silent: true });
}

async function handleCancelRequest(requestId) {
  if (!window.confirm("Cancel this request?")) return;
  await api(`/api/requests/${requestId}/cancel`, { method: "PATCH" });
  toast("Request cancelled.");
  await loadData({ silent: true });
}

async function handleCancelAllocation(allocationId) {
  if (!window.confirm("Cancel this allocation and release capacity?")) return;
  await api(`/api/allocations/${allocationId}/cancel`, { method: "PATCH" });
  toast("Allocation cancelled. Waiting queues were reprocessed.");
  await loadData({ silent: true });
}

async function handleRequestExtension(allocationId, formData) {
  const payload = Object.fromEntries(formData);
  payload.duration_hours = Number(payload.duration_hours || 0);
  await api(`/api/allocations/${allocationId}/extensions`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  toast("Extension request sent for admin review.");
  await loadData({ silent: true });
}

async function handleApproveExtension(extensionId) {
  await api(`/api/admin/extensions/${extensionId}/approve`, { method: "POST" });
  toast("Extension approved.");
  await loadData({ silent: true });
}

async function handleRejectExtension(extensionId) {
  const reason = window.prompt("Reason for rejection?");
  if (!reason) return;
  await api(`/api/admin/extensions/${extensionId}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
  toast("Extension rejected.");
  await loadData({ silent: true });
}

async function handleInventoryCreate(formData, form) {
  const payload = Object.fromEntries(formData);
  payload.total_capacity = Number(payload.total_capacity || 0);
  payload.reserved_capacity = Number(payload.reserved_capacity || 0);
  payload.enabled = formData.get("enabled") === "on";
  if (payload.resource_type === "FULL_GPU") payload.mig_profile = null;
  await api("/api/admin/inventory", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  form.reset();
  toast("Inventory pool added.");
  await loadData({ silent: true });
}

async function loadInventoryItems() {
  if (!isOpsRole()) return;
  state.inventory = null;
  renderApp();
  try {
    const result = await api('/api/admin/inventory');
    state.inventory = result.inventory || [];
    renderApp();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function handleInventoryCreateItem(formData, form) {
  const payload = Object.fromEntries(formData);
  if (payload.resource_type === 'FULL_GPU') payload.mig_profile = null;
  if (!payload.label || !payload.resource_type) {
    toast('Type and label are required.', 'error');
    return;
  }
  try {
    await api('/api/admin/inventory', { method: 'POST', body: JSON.stringify(payload) });
    form.reset();
    toast('Inventory item created.');
    await loadInventoryItems();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function handleInventoryEdit(itemId) {
  const item = state.inventory?.find((i) => i.id === itemId);
  if (!item) return toast('Item not found.', 'error');
  if (isObserverOnly()) return toast('Read-only observer.', 'error');
  const label = window.prompt('Label', item.label);
  if (label === null) return;
  const notes = window.prompt('Notes (empty to clear)', item.notes || '');
  if (notes === null) return;
  const status = window.prompt('Status (AVAILABLE, MAINTENANCE, DISABLED)', item.status || 'AVAILABLE');
  if (status === null) return;
  try {
    await api(`/api/admin/inventory/${itemId}`, { method: 'PATCH', body: JSON.stringify({ label: label.trim(), notes: (notes || '').trim() || null, status: status.trim().toUpperCase() }) });
    toast('Inventory item updated.');
    await loadInventoryItems();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function handleInventoryDelete(itemId) {
  console.log("[DELETE] Delete button clicked for item:", itemId);
  if (isObserverOnly()) {
    console.log("[DELETE] Observer only - returning");
    return toast('Read-only observer.', 'error');
  }
  if (!window.confirm('Delete this inventory item? This cannot be undone.')) {
    console.log("[DELETE] User cancelled confirm");
    return;
  }
  try {
    console.log("[DELETE] Making DELETE request to /api/admin/inventory/" + itemId);
    const response = await api(`/api/admin/inventory/${itemId}`, { method: 'DELETE' });
    console.log("[DELETE] Response received:", response);

    // Optimistically remove from local state
    if (state.inventory) {
      state.inventory = state.inventory.filter(item => item.id !== itemId);
      console.log("[DELETE] Removed from local inventory, now has " + state.inventory.length + " items");
      renderApp();
    }

    toast('Inventory item deleted.');
    console.log("[DELETE] Loading inventory items to sync...");
    await loadInventoryItems();
    console.log("[DELETE] Inventory reloaded from server");
  } catch (err) {
    console.error("[DELETE] Error:", err.message);
    toast(err.message, 'error');
    console.log("[DELETE] Reloading inventory due to error");
    await loadInventoryItems();
  }
}

async function handleDisablePool(poolId) {
  if (!window.confirm("Disable this inventory pool?")) return;
  await api(`/api/admin/inventory/${poolId}`, { method: "DELETE" });
  toast("Inventory pool disabled.");
  await loadData({ silent: true });
}

async function handleRoleUpdate(userId, formData) {
  await api(`/api/admin/users/${userId}/role`, {
    method: "PATCH",
    body: JSON.stringify({ role: formData.get("role") }),
  });
  toast("User role updated.");
  await loadUsers();
}

async function runJobs() {
  const result = await api("/api/system/jobs/run", { method: "POST" });
  toast(`Jobs complete: ${result.promoted_waiting_requests || 0} waiting request(s) promoted.`);
  await loadData({ silent: true });
}

function navigate(view) {
  state.view = view;
  if (view === "audit" && state.audit === null) loadAudit();
  if (view === "users" && state.users === null) loadUsers();
  if (view === "emails" && state.emails === null) loadEmails();
  if (view === "inventory") loadInventoryItems();
  renderApp();
}

function toggleTheme() {
  state.theme = state.theme === "dark" ? "light" : "dark";
  localStorage.setItem("dgx_theme", state.theme);
  applyTheme();
}

function renderSkeleton() {
  return `
    <div class="metric-grid">
      <div class="skeleton"></div>
      <div class="skeleton"></div>
      <div class="skeleton"></div>
      <div class="skeleton"></div>
    </div>
    <div class="chart-grid">
      <div class="skeleton tall"></div>
      <div class="skeleton tall"></div>
    </div>`;
}

function renderAuth() {
  const isLogin = state.authMode === "login";
  root.innerHTML = `
    <main class="auth-layout entrance">
      <section class="auth-hero" aria-label="DGX system image">
        <div class="auth-hero-copy">
          <p class="eyebrow">DGX Access Portal</p>
          <h1>Request, track, and manage DGX compute access</h1>
          <p>FIFO queues, timed allocations, extensions, inventory, and read-only analytics for observers.</p>
        </div>
      </section>
      <section class="auth-panel">
        <form class="surface auth-card stack-form" data-form="${isLogin ? "login" : "register"}">
          <div>
            <p class="eyebrow">${isLogin ? "Welcome back" : "Create account"}</p>
            <h2>${isLogin ? "Sign in" : "Register requester"}</h2>
          </div>
          ${
            isLogin
              ? ""
              : `<label>Name<input name="name" autocomplete="name" required /></label>
                 <label>Department<input name="department" autocomplete="organization" /></label>`
          }
          <label>Email<input type="email" name="email" autocomplete="email" required /></label>
          <label>Password<input type="password" name="password" autocomplete="${isLogin ? "current-password" : "new-password"}" minlength="8" required /></label>
          <button class="primary" type="submit">${isLogin ? "Sign in" : "Create account"}</button>
          <button class="ghost" type="button" data-action="toggle-auth">
            ${isLogin ? "Need an account?" : "Already have an account?"}
          </button>
          <div class="seed-note">
            <span><strong>Admin:</strong> admin@dgx.local / admin1234</span>
            <span><strong>Observer:</strong> observer@dgx.local / observer1234</span>
            <span><strong>User:</strong> user@dgx.local / user1234</span>
          </div>
        </form>
      </section>
    </main>`;
}

function renderShell() {
  const ops = isOpsRole();
  root.innerHTML = `
    <div class="app-shell entrance">
      <header class="topbar">
        <div class="topbar-inner">
          <div class="brand-row">
            <div class="brand-mark">DGX</div>
            <div>
              <h1>DGX Access Portal</h1>
              <div class="subline">
                <span class="role-badge">${roleLabel()}</span>
                ${isObserverOnly() ? '<span class="read-only-pill">Read-only observer</span>' : ""}
                <span>${escapeHtml(state.user?.email)}</span>
              </div>
            </div>
          </div>
          <div class="top-actions">
            <button type="button" data-action="theme">${state.theme === "dark" ? "Light" : "Dark"}</button>
            <button type="button" data-action="refresh">Refresh</button>
            <button type="button" data-action="logout">Sign out</button>
          </div>
        </div>
      </header>
      <main class="content-container">
        ${renderNav()}
        ${state.loading ? renderSkeleton() : ops ? renderOpsView() : renderRequesterView()}
      </main>
    </div>`;
}

function renderNav() {
  if (!isOpsRole()) return "";
  const items = [
    ["dashboard", "Dashboard"],
    ["requests", "Requests"],
    ["waiting", "Waiting list"],
    ["allocations", "Allocations"],
    ["extensions", "Extensions"],
    ["inventory", "Inventory"],
    ["audit", "Audit"],
  ];
  if (hasRole("ADMIN")) {
    items.push(["users", "Users"], ["emails", "Email log"]);
  }
  return `
    <nav class="tabs" aria-label="Primary">
      ${items
        .map(
          ([view, label]) =>
            `<button type="button" class="${state.view === view ? "active" : ""}" data-nav="${view}">${label}</button>`
        )
        .join("")}
    </nav>`;
}

function renderOpsView() {
  switch (state.view) {
    case "requests":
      return renderRequestsTable(state.data?.requests || [], { mode: "ops" });
    case "waiting":
      return renderWaitingView();
    case "allocations":
      return renderAllocationsView();
    case "extensions":
      return renderExtensionsView();
    case "inventory":
      return renderInventoryView();
    case "audit":
      return renderAuditView();
    case "users":
      return hasRole("ADMIN") ? renderUsersView() : renderForbidden();
    case "emails":
      return hasRole("ADMIN") ? renderEmailsView() : renderForbidden();
    case "dashboard":
    default:
      return renderDashboardView();
  }
}

function renderDashboardView() {
  const analytics = state.data?.analytics || {};
  const summary = analytics.summary || {};
  const counts = summary.counts || state.data?.counts || {};
  const capacity = summary.capacity || [];
  return `
    <section class="dashboard-hero">
      <div>
        <p class="eyebrow">Live operations</p>
        <h2>DGX compute access at a glance</h2>
        <p>Capacity, FIFO health, request flow, extensions, and top usage patterns for admins and observers.</p>
      </div>
      ${isObserverOnly() ? '<span class="read-only-pill">Mutation controls hidden</span>' : '<button type="button" data-action="run-jobs">Run jobs now</button>'}
    </section>
    ${renderKpis([
      ["Pending", counts.pending ?? 0, "Need approval"],
      ["Waiting", counts.waiting ?? 0, "FIFO queue"],
      ["Active", counts.active ?? 0, "Running now"],
      ["Expiring", counts.expiring ?? 0, "Ending within 2 days"],
      ["Extensions", counts.extensions ?? 0, "Pending decisions"],
      ["Ending 7d", counts.ending_next_7_days ?? 0, "Capacity returning soon"],
    ])}
    ${renderCapacitySnapshot(capacity)}
    <section class="chart-grid">
      ${renderUtilizationChart(analytics.utilization?.series || [])}
      ${renderDonutChart(summary.requests_by_status || [], "Requests by status", "status", "count")}
      ${renderBarChart(analytics.requests?.series || [], "Requests over time", "period", "count")}
      ${renderBarChart(summary.departments || [], "Department usage", "department", "request_count")}
      ${renderDonutChart(analytics.extensions?.by_status || [], "Extensions", "status", "count")}
      ${renderWaitingChart(analytics.waiting)}
    </section>
    <section class="split-grid">
      ${renderTopUsers(summary.top_users || [])}
      ${renderRequestsTable((state.data?.requests || []).slice(0, 8), { mode: "compact", title: "Recent requests" })}
    </section>`;
}

function renderKpis(cards) {
  return `
    <section class="metric-grid">
      ${cards
        .map(
          ([label, value, hint], index) => `
        <article class="metric" style="animation-delay:${index * 35}ms">
          <span>${escapeHtml(value)}</span>
          <strong>${escapeHtml(label)}</strong>
          <small>${escapeHtml(hint)}</small>
        </article>`
        )
        .join("")}
    </section>`;
}

function renderCapacitySnapshot(capacity) {
  return `
    <section class="capacity-grid">
      ${capacity
        .map((item) => {
          const pct = Math.max(0, Math.min(100, Number(item.utilization_percent || 0)));
          return `
            <article class="surface capacity-card">
              <div class="section-heading">
                <div>
                  <p class="eyebrow">${escapeHtml(item.resource_type)}</p>
                  <h3>${escapeHtml(item.label)}</h3>
                </div>
                <strong>${pct}%</strong>
              </div>
              <div class="capacity-bar" aria-label="${escapeHtml(item.label)} utilization">
                <span style="width:${pct}%"></span>
              </div>
              <div class="capacity-stats">
                <span>Total <b>${escapeHtml(item.total)}</b></span>
                <span>Used <b>${escapeHtml(item.used)}</b></span>
                <span>Held <b>${escapeHtml(item.held)}</b></span>
                <span>Available <b>${escapeHtml(item.available)}</b></span>
              </div>
            </article>`;
        })
        .join("")}
    </section>`;
}

function renderUtilizationChart(series) {
  const width = 640;
  const height = 220;
  const pad = 28;
  const values = series.length ? series : [{ full_gpu_utilization: 0, mig_utilization: 0, date: "" }];
  const pointsFor = (key) =>
    values
      .map((row, index) => {
        const x = pad + (index * (width - pad * 2)) / Math.max(1, values.length - 1);
        const y = height - pad - (Math.min(100, Number(row[key] || 0)) / 100) * (height - pad * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  const latest = values[values.length - 1] || {};
  return `
    <article class="surface chart-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">30 day trend</p>
          <h3>Utilization</h3>
        </div>
        <div class="legend"><span class="full"></span>Full GPU <span class="mig"></span>MIG</div>
      </div>
      <svg class="line-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Full GPU and MIG utilization over time">
        <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" />
        <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" />
        <polyline class="full-line" points="${pointsFor("full_gpu_utilization")}" />
        <polyline class="mig-line" points="${pointsFor("mig_utilization")}" />
      </svg>
      <p class="chart-note">Latest: Full GPU ${escapeHtml(latest.full_gpu_utilization || 0)}%, MIG ${escapeHtml(latest.mig_utilization || 0)}%</p>
    </article>`;
}

function renderDonutChart(items, title, labelKey, valueKey) {
  const total = items.reduce((sum, item) => sum + Number(item[valueKey] || 0), 0);
  let offset = 25;
  const circles = total
    ? items
        .map((item, index) => {
          const value = Number(item[valueKey] || 0);
          const pct = (value / total) * 100;
          const circle = `<circle r="15.9" cx="18" cy="18" stroke="${CHART_COLORS[index % CHART_COLORS.length]}" stroke-dasharray="${pct} ${100 - pct}" stroke-dashoffset="${offset}" />`;
          offset -= pct;
          return circle;
        })
        .join("")
    : '<circle r="15.9" cx="18" cy="18" stroke="#64748b" stroke-dasharray="100 0" />';
  return `
    <article class="surface chart-card donut-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Breakdown</p>
          <h3>${escapeHtml(title)}</h3>
        </div>
        <strong>${total}</strong>
      </div>
      <div class="donut-layout">
        <svg class="donut" viewBox="0 0 36 36" role="img" aria-label="${escapeHtml(title)}">${circles}</svg>
        <ul class="chart-list">
          ${
            items.length
              ? items
                  .map(
                    (item, index) => `
              <li><span style="background:${CHART_COLORS[index % CHART_COLORS.length]}"></span>${escapeHtml(STATUS_LABELS[item[labelKey]] || item[labelKey])}<b>${escapeHtml(item[valueKey])}</b></li>`
                  )
                  .join("")
              : '<li><span></span>No data<b>0</b></li>'
          }
        </ul>
      </div>
    </article>`;
}

function renderBarChart(items, title, labelKey, valueKey) {
  const max = Math.max(1, ...items.map((item) => Number(item[valueKey] || 0)));
  return `
    <article class="surface chart-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Volume</p>
          <h3>${escapeHtml(title)}</h3>
        </div>
      </div>
      <div class="bar-list">
        ${
          items.length
            ? items
                .slice(-10)
                .map((item) => {
                  const value = Number(item[valueKey] || 0);
                  return `
                    <div class="bar-row">
                      <span>${escapeHtml(item[labelKey])}</span>
                      <div><i style="width:${(value / max) * 100}%"></i></div>
                      <b>${value}</b>
                    </div>`;
                })
                .join("")
            : '<div class="empty small">No chart data yet.</div>'
        }
      </div>
    </article>`;
}

function renderWaitingChart(waiting) {
  const items = waiting?.by_queue || [];
  return `
    <article class="surface chart-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Queues</p>
          <h3>Waiting list</h3>
        </div>
        <strong>${escapeHtml(waiting?.current_count || 0)}</strong>
      </div>
      <p class="chart-note">Average promoted wait: ${escapeHtml(waiting?.average_wait_hours || 0)}h. Current average: ${escapeHtml(waiting?.current_average_wait_hours || 0)}h.</p>
      ${renderBarChartInner(items, "queue_key", "count")}
    </article>`;
}

function renderBarChartInner(items, labelKey, valueKey) {
  const max = Math.max(1, ...items.map((item) => Number(item[valueKey] || 0)));
  if (!items.length) return '<div class="empty small">No waiting requests.</div>';
  return `
    <div class="bar-list">
      ${items
        .map(
          (item) => `
        <div class="bar-row">
          <span>${escapeHtml(item[labelKey])}</span>
          <div><i style="width:${(Number(item[valueKey] || 0) / max) * 100}%"></i></div>
          <b>${escapeHtml(item[valueKey])}</b>
        </div>`
        )
        .join("")}
    </div>`;
}

function renderTopUsers(users) {
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Usage</p>
          <h3>Top requesters</h3>
        </div>
      </div>
      ${
        users.length
          ? `<div class="table-wrap"><table><thead><tr><th>User</th><th>Email</th><th>Requests</th></tr></thead><tbody>
              ${users
                .map(
                  (user) => `<tr><td>${escapeHtml(user.name)}</td><td>${escapeHtml(user.email)}</td><td>${escapeHtml(user.request_count)}</td></tr>`
                )
                .join("")}
            </tbody></table></div>`
          : '<div class="empty">No requester data yet.</div>'
      }
    </section>`;
}

function renderRequestsTable(requests, options = {}) {
  const title = options.title || "Requests";
  const mode = options.mode || "ops";
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">${mode === "compact" ? "Recent" : "Read-only for observers"}</p>
          <h3>${escapeHtml(title)}</h3>
        </div>
      </div>
      ${
        requests.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>Requester</th><th>Resource</th><th>Window</th><th>Status</th><th>Queue</th><th>Actions</th></tr></thead>
              <tbody>
                ${requests
                  .map(
                    (request) => `
                  <tr>
                    <td><strong>${escapeHtml(request.name || request.requester_name)}</strong><br><span class="muted">${escapeHtml(request.email || request.requester_email)}</span></td>
                    <td>${escapeHtml(resourceLabel(request))}<br><span class="muted">${escapeHtml(request.department || "No department")}</span></td>
                    <td>${formatDate(request.requested_start_at)}<br><span class="muted">${formatDuration(request.requested_duration_minutes)}</span></td>
                    <td>${statusChip(request.status)}</td>
                    <td>${request.waiting_position ? `#${escapeHtml(request.waiting_position)}` : '<span class="muted">-</span>'}</td>
                    <td>${renderRequestActions(request)}</td>
                  </tr>`
                  )
                  .join("")}
              </tbody>
            </table></div>`
          : '<div class="empty">No requests yet.</div>'
      }
    </section>`;
}

function renderRequestActions(request) {
  if (isObserverOnly()) return '<span class="muted">Read-only</span>';
  if (hasRole("ADMIN") && request.status === "PENDING_ADMIN") {
    return `
      <div class="row-actions">
        <button type="button" class="success" data-action="approve-request" data-id="${escapeHtml(request.id)}">Approve</button>
        <button type="button" class="danger" data-action="reject-request" data-id="${escapeHtml(request.id)}">Reject</button>
      </div>`;
  }
  if (["WAITING", "PENDING_ADMIN", "SUBMITTED", "DRAFT"].includes(request.status)) {
    return `<button type="button" class="danger" data-action="cancel-request" data-id="${escapeHtml(request.id)}">Cancel</button>`;
  }
  return '<span class="muted">No action</span>';
}

function renderWaitingView() {
  const waiting = state.data?.waiting || [];
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">FIFO</p>
          <h3>Waiting list</h3>
        </div>
      </div>
      ${
        waiting.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>Queue</th><th>Position</th><th>Requester</th><th>Resource</th><th>Requested window</th></tr></thead>
              <tbody>
                ${waiting
                  .map(
                    (item) => `<tr>
                      <td>${escapeHtml(item.queue_key)}</td>
                      <td>#${escapeHtml(item.waiting_position)}</td>
                      <td>${escapeHtml(item.name)}<br><span class="muted">${escapeHtml(item.email)}</span></td>
                      <td>${escapeHtml(resourceLabel(item))}</td>
                      <td>${formatDate(item.requested_start_at)} to ${formatDate(item.requested_end_at)}</td>
                    </tr>`
                  )
                  .join("")}
              </tbody>
            </table></div>`
          : '<div class="empty">No waiting requests. Capacity is keeping up.</div>'
      }
    </section>`;
}

function renderAllocationsView() {
  const allocations = state.data?.active || [];
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Current capacity</p>
          <h3>Active allocations</h3>
        </div>
      </div>
      ${
        allocations.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>User</th><th>Resource</th><th>Start</th><th>End</th><th>Status</th><th>Actions</th></tr></thead>
              <tbody>${allocations.map(renderAllocationRow).join("")}</tbody>
            </table></div>`
          : '<div class="empty">No active or scheduled allocations.</div>'
      }
    </section>`;
}

function renderAllocationRow(allocation) {
  return `
    <tr>
      <td>${escapeHtml(allocation.user_name)}<br><span class="muted">${escapeHtml(allocation.user_email)}</span></td>
      <td>${escapeHtml(resourceLabel(allocation))}</td>
      <td>${formatDate(allocation.start_at)}</td>
      <td>${formatDate(allocation.end_at)}</td>
      <td>${statusChip(allocation.status)}</td>
      <td>
        ${
          isObserverOnly()
            ? '<span class="muted">Read-only</span>'
            : `<button type="button" class="danger" data-action="cancel-allocation" data-id="${escapeHtml(allocation.id)}">Cancel</button>`
        }
      </td>
    </tr>`;
}

function renderExtensionsView() {
  const extensions = state.data?.extensions || [];
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Extension flow</p>
          <h3>Extension requests</h3>
        </div>
      </div>
      ${
        extensions.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>Requester</th><th>Allocation</th><th>Requested end</th><th>Status</th><th>Actions</th></tr></thead>
              <tbody>
                ${extensions
                  .map(
                    (extension) => `
                  <tr>
                    <td>${escapeHtml(extension.requester_name)}<br><span class="muted">${escapeHtml(extension.requester_email)}</span></td>
                    <td>${escapeHtml(extension.allocation_id)}<br><span class="muted">${formatDuration(extension.requested_duration_minutes)}</span></td>
                    <td>${formatDate(extension.requested_end_at)}</td>
                    <td>${statusChip(extension.status)}</td>
                    <td>${renderExtensionActions(extension)}</td>
                  </tr>`
                  )
                  .join("")}
              </tbody>
            </table></div>`
          : '<div class="empty">No extension requests yet.</div>'
      }
    </section>`;
}

function renderExtensionActions(extension) {
  if (isObserverOnly()) return '<span class="muted">Read-only</span>';
  if (hasRole("ADMIN") && extension.status === "PENDING_ADMIN") {
    return `
      <div class="row-actions">
        <button type="button" class="success" data-action="approve-extension" data-id="${escapeHtml(extension.id)}">Approve</button>
        <button type="button" class="danger" data-action="reject-extension" data-id="${escapeHtml(extension.id)}">Reject</button>
      </div>`;
  }
  return '<span class="muted">No action</span>';
}

function renderInventoryView() {
  const items = state.inventory ?? state.data?.inventory ?? [];
  return `
    ${hasRole("ADMIN") ? renderInventoryItemForm() : ""}
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Inventory</p>
          <h3>Inventory items</h3>
        </div>
      </div>
      ${
        items.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>Type</th><th>Label</th><th>Status</th><th>Notes</th><th>Updated</th><th>Actions</th></tr></thead>
              <tbody>
                ${items
                  .map(
                    (it) => `
                  <tr>
                    <td>${escapeHtml(it.resource_type)} ${escapeHtml(it.mig_profile || "")}</td>
                    <td>${escapeHtml(it.label)}</td>
                    <td>${statusChip(it.effective_status || it.status)}</td>
                    <td>${escapeHtml(it.notes || "")}</td>
                    <td>${escapeHtml(it.updatedAt || it.updated_at || it.updated)}</td>
                    <td>${
                      isObserverOnly()
                        ? '<span class="muted">Read-only</span>'
                        : `<div class="row-actions"><button type="button" class="ghost" data-action="edit-item" data-id="${escapeHtml(it.id)}">Edit</button><button type="button" class="danger" data-action="delete-item" data-id="${escapeHtml(it.id)}">Delete</button></div>`
                    }</td>
                  </tr>`
                  )
                  .join("")}
              </tbody>
            </table></div>`
          : '<div class="empty">No inventory items yet.</div>'
      }
    </section>`;
}

function renderInventoryItemForm() {
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Admin</p>
          <h3>Add inventory item</h3>
        </div>
      </div>
      <form class="form-grid inventory-item-form" data-form="inventory-item">
        <label>Type
          <select name="resource_type" required>
            <option value="FULL_GPU">Full GPU</option>
            <option value="MIG">MIG</option>
          </select>
        </label>
        <label>MIG profile<input name="mig_profile" placeholder="1G.10GB" /></label>
        <label>Label<input name="label" placeholder="GPU-1 or MIG-1" required /></label>
        <label>Status
          <select name="status">
            <option value="AVAILABLE">AVAILABLE</option>
            <option value="MAINTENANCE">MAINTENANCE</option>
            <option value="DISABLED">DISABLED</option>
          </select>
        </label>
        <label>Notes<input name="notes" /></label>
        <button class="primary" type="submit">Add item</button>
      </form>
    </section>`;
}

function renderInventoryForm() {
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Admin</p>
          <h3>Add inventory pool</h3>
        </div>
      </div>
      <form class="form-grid inventory-form" data-form="inventory">
        <label>Type
          <select name="resource_type" required>
            <option value="FULL_GPU">Full GPU</option>
            <option value="MIG">MIG</option>
          </select>
        </label>
        <label>MIG profile<input name="mig_profile" placeholder="1G.10GB" /></label>
        <label>Label<input name="label" placeholder="DGX H100 full GPU" /></label>
        <label>Total<input type="number" min="0" name="total_capacity" required /></label>
        <label>Reserved<input type="number" min="0" name="reserved_capacity" value="0" required /></label>
        <label class="checkbox-row"><input type="checkbox" name="enabled" checked /> Enabled</label>
        <button class="primary" type="submit">Add pool</button>
      </form>
    </section>`;
}

function renderAuditView() {
  if (state.audit === null) return renderSkeleton();
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Read-only</p>
          <h3>Audit log</h3>
        </div>
      </div>
      ${
        state.audit.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Entity</th><th>ID</th></tr></thead>
              <tbody>
                ${state.audit
                  .map(
                    (row) => `<tr>
                      <td>${formatDate(row.created_at)}</td>
                      <td>${escapeHtml(row.actor_email || row.actor_type)}</td>
                      <td>${escapeHtml(row.action)}</td>
                      <td>${escapeHtml(row.entity_type)}</td>
                      <td><span class="muted">${escapeHtml(row.entity_id)}</span></td>
                    </tr>`
                  )
                  .join("")}
              </tbody>
            </table></div>`
          : '<div class="empty">No audit activity yet.</div>'
      }
    </section>`;
}

function renderUsersView() {
  if (state.users === null) return renderSkeleton();
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Admin only</p>
          <h3>User roles</h3>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>User</th><th>Department</th><th>Current role</th><th>Change role</th></tr></thead>
          <tbody>
            ${state.users
              .map(
                (user) => `<tr>
                  <td><strong>${escapeHtml(user.name)}</strong><br><span class="muted">${escapeHtml(user.email)}</span></td>
                  <td>${escapeHtml(user.department || "Unspecified")}</td>
                  <td>${user.roles.map((role) => `<span class="role-badge">${escapeHtml(role)}</span>`).join(" ")}</td>
                  <td>
                    <form class="inline-form" data-form="role" data-user-id="${escapeHtml(user.id)}">
                      <select name="role">
                        ${["USER", "OBSERVER", "ADMIN"]
                          .map((role) => `<option value="${role}" ${user.primary_role === role ? "selected" : ""}>${role}</option>`)
                          .join("")}
                      </select>
                      <button type="submit" class="primary">Save</button>
                    </form>
                  </td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
    </section>`;
}

function renderEmailsView() {
  if (state.emails === null) return renderSkeleton();
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Admin only</p>
          <h3>Email notifications</h3>
        </div>
      </div>
      ${
        state.emails.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>Time</th><th>Status</th><th>Recipient</th><th>Subject</th><th>Error</th></tr></thead>
              <tbody>
                ${state.emails
                  .map(
                    (email) => `<tr>
                      <td>${formatDate(email.created_at)}</td>
                      <td>${statusChip(email.status)}</td>
                      <td>${escapeHtml(email.recipient_email)}</td>
                      <td>${escapeHtml(email.subject)}</td>
                      <td>${escapeHtml(email.error_message || "")}</td>
                    </tr>`
                  )
                  .join("")}
              </tbody>
            </table></div>`
          : '<div class="empty">No email notifications yet.</div>'
      }
    </section>`;
}

function renderRequesterView() {
  const data = state.data || {};
  const requests = data.requests || [];
  const allocations = data.allocations || [];
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Requester</p>
          <h3>New DGX request</h3>
        </div>
      </div>
      <form class="form-grid" data-form="request">
        <label>Name<input name="name" value="${escapeHtml(state.user?.name)}" required /></label>
        <label>Email<input type="email" name="email" value="${escapeHtml(state.user?.email)}" required /></label>
        <label>Department<input name="department" value="${escapeHtml(state.user?.department || "")}" /></label>
        <label>Resource
          <select name="resource_type" required>
            <option value="FULL_GPU">Full GPU</option>
            <option value="MIG">MIG partition</option>
          </select>
        </label>
        <label>MIG profile
          <select name="mig_profile">
            <option value="">Full GPU only</option>
            <option value="1G.10GB">1G.10GB</option>
            <option value="2G.20GB">2G.20GB</option>
            <option value="3G.40GB">3G.40GB</option>
            <option value="7G.80GB">7G.80GB</option>
          </select>
        </label>
        <label>Quantity<input type="number" min="1" name="quantity" value="1" required /></label>
        <label>Duration days<input type="number" min="0.5" step="0.5" name="duration_hours" value="1" required /></label>
        <label>Requested start<input type="datetime-local" name="requested_start_at" value="${toDateTimeLocal()}" required /></label>
        <label>Purpose<textarea name="purpose" rows="3" required placeholder="Training run, inference test, migration validation..."></textarea></label>
        <label>Urgency<input name="urgency" placeholder="Normal, deadline, incident..." /></label>
        <label>Notes<textarea name="notes" rows="2"></textarea></label>
        <button class="primary" type="submit">Submit request</button>
      </form>
    </section>

    ${renderRequestsTable(requests, { mode: "mine", title: "My requests" })}
    ${renderMyAllocations(allocations)}
  `;
}

function renderMyAllocations(allocations) {
  return `
    <section class="surface">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Requester</p>
          <h3>My allocations</h3>
        </div>
      </div>
      ${
        allocations.length
          ? `<div class="table-wrap"><table>
              <thead><tr><th>Resource</th><th>End</th><th>Status</th><th>Extension</th><th>Actions</th></tr></thead>
              <tbody>
                ${allocations
                  .map(
                    (allocation) => `<tr>
                      <td>${escapeHtml(resourceLabel(allocation))}</td>
                      <td>${formatDate(allocation.end_at)}</td>
                      <td>${statusChip(allocation.status)}</td>
                      <td>
                        ${
                          ["SCHEDULED", "ACTIVE", "EXPIRING"].includes(allocation.status)
                            ? `<form class="inline-form wide" data-form="extension" data-allocation-id="${escapeHtml(allocation.id)}">
                                <input type="number" min="0.5" step="0.5" name="duration_hours" placeholder="Days" required />
                                <input name="justification" placeholder="Justification" required />
                                <button class="primary" type="submit">Request</button>
                              </form>`
                            : '<span class="muted">Unavailable</span>'
                        }
                      </td>
                      <td>
                        ${
                          ["SCHEDULED", "ACTIVE", "EXPIRING"].includes(allocation.status)
                            ? `<button type="button" class="danger" data-action="cancel-allocation" data-id="${escapeHtml(allocation.id)}">Cancel</button>`
                            : '<span class="muted">No action</span>'
                        }
                      </td>
                    </tr>`
                  )
                  .join("")}
              </tbody>
            </table></div>`
          : '<div class="empty">No allocations yet.</div>'
      }
    </section>`;
}

function renderForbidden() {
  return `
    <section class="surface forbidden-card">
      <p class="eyebrow">403</p>
      <h3>Forbidden</h3>
      <p>This account cannot access that area.</p>
    </section>`;
}

function renderApp() {
  applyTheme();
  if (!state.user) {
    renderAuth();
    return;
  }
  renderShell();
}

root.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-form]");
  if (!form) return;
  const type = form.dataset.form;
  const handlers = {
    login: handleLogin,
    register: handleRegister,
    request: handleCreateRequest,
    inventory: handleInventoryCreate,
    'inventory-item': handleInventoryCreateItem,
    role: (formData) => handleRoleUpdate(form.dataset.userId, formData),
    extension: (formData) => handleRequestExtension(form.dataset.allocationId, formData),
  };
  if (handlers[type]) submitForm(event, handlers[type], form);
});

root.addEventListener("click", (event) => {
  const nav = event.target.closest("[data-nav]");
  if (nav) {
    navigate(nav.dataset.nav);
    return;
  }

  const action = event.target.closest("[data-action]");
  if (!action) return;

  const id = action.dataset.id;
  console.log(`[CLICK] Action clicked: ${action.dataset.action}, ID: ${id}`);

  const actions = {
    "toggle-auth": () => {
      state.authMode = state.authMode === "login" ? "register" : "login";
      renderApp();
    },
    theme: toggleTheme,
    refresh: () => loadData({ silent: true }),
    logout: handleLogout,
    "run-jobs": runJobs,
    "approve-request": () => handleApprove(id),
    "reject-request": () => handleReject(id),
    "cancel-request": () => handleCancelRequest(id),
    "cancel-allocation": () => handleCancelAllocation(id),
    "approve-extension": () => handleApproveExtension(id),
    "reject-extension": () => handleRejectExtension(id),
    "disable-pool": () => handleDisablePool(id),
    "edit-item": () => handleInventoryEdit(id),
    "delete-item": () => {
      console.log(`[CLICK] DELETE-ITEM action triggered for ID: ${id}`);
      handleInventoryDelete(id);
    },
  };
  if (actions[action.dataset.action]) {
    console.log(`[CLICK] Executing action: ${action.dataset.action}`);
    actions[action.dataset.action]();
  } else {
    console.log(`[CLICK] Unknown action: ${action.dataset.action}`);
  }
});

async function init() {
  applyTheme();
  if (!state.token) {
    renderApp();
    return;
  }
  try {
    const result = await api("/api/me");
    state.user = result.user;
    state.view = isOpsRole() ? "dashboard" : "request";
    await loadData({ silent: true });
  } catch {
    clearSession();
    renderApp();
  }
}

window.addEventListener("DOMContentLoaded", init);
