/* global frappe */

frappe.pages["observability-dashboard"].on_page_load = function (wrapper) {
  const page = frappe.ui.make_app_page({
    parent: wrapper,
    title: __("Observability"),
    single_column: true,
  });
  const state = { windowMinutes: 60, tab: "overview", data: null, charts: [], logFiles: [], logThreshold: 0 };
  const esc = (value) => frappe.utils.escape_html(String(value ?? "—"));
  const number = (value, digits = 1) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toLocaleString(undefined, { maximumFractionDigits: digits }) : "—";
  };
  const bytes = (value) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "—";
    if (parsed < 1024) return `${number(parsed, 0)} B`;
    const units = ["KiB", "MiB", "GiB", "TiB"];
    let amount = parsed;
    let unit = -1;
    do { amount /= 1024; unit += 1; } while (amount >= 1024 && unit < units.length - 1);
    return `${number(amount, 1)} ${units[unit]}`;
  };
  const ageText = (value) => {
    if (value == null || value === "") return __("No sample");
    const age = Number(value);
    if (!Number.isFinite(age)) return __("No sample");
    if (age < 60) return `${number(age, 0)} s`;
    if (age < 3600) return `${number(age / 60, 1)} min`;
    return `${number(age / 3600, 1)} h`;
  };
  const last = (items) => Array.isArray(items) && items.length ? Number(items[items.length - 1].value) : 0;

  page.set_primary_action(__("Refresh"), () => loadData(), "refresh");
  page.main.html(`
    <section class="frappe-observability container-fluid">
      <div class="observability-toolbar">
        <div class="btn-group observability-tabs" role="tablist">
          <button class="btn btn-default active" data-tab="overview">${__("Overview")}</button>
          <button class="btn btn-default" data-tab="traffic">${__("Traffic")}</button>
          <button class="btn btn-default" data-tab="users">${__("Users & IPs")}</button>
          <button class="btn btn-default" data-tab="server">${__("Server")}</button>
          <button class="btn btn-default" data-tab="jobs">${__("Jobs & DB")}</button>
          <button class="btn btn-default" data-tab="storage">${__("Storage")}</button>
          <button class="btn btn-default" data-tab="logs">${__("Logs")}</button>
          <button class="btn btn-default" data-tab="alerts">${__("Alerts")}</button>
          <button class="btn btn-default" data-tab="diagnostics">${__("Diagnostics")}</button>
          <button class="btn btn-default" data-tab="settings">${__("Settings")}</button>
        </div>
        <label class="observability-range">${__("Range")}
          <select class="form-control" data-window>
            <option value="15">${__("Last 15 minutes")}</option>
            <option value="60" selected>${__("Last hour")}</option>
            <option value="360">${__("Last 6 hours")}</option>
            <option value="1440">${__("Last 24 hours")}</option>
            <option value="10080">${__("Last 7 days")}</option>
          </select>
        </label>
      </div>
      <div class="observability-mode text-muted" data-mode></div>
      <div class="observability-kpis row" data-kpis></div>
      <div class="observability-content" data-content><div class="text-muted">${__("Loading observability data…")}</div></div>
    </section>
    <style>
      .frappe-observability { padding: 12px 20px 30px; }
      .observability-toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; margin:4px 0 14px; }
      .observability-tabs { display:flex; flex-wrap:wrap; gap:4px; }
      .observability-tabs .btn.active { background:#e8f1fb; border-color:#9dbddd; color:#174b79; }
      .observability-range { display:flex; align-items:center; gap:8px; white-space:nowrap; margin:0; }
      .observability-range select { width:auto; min-width:150px; }
      .observability-mode { margin:8px 0; min-height:20px; }
      .observability-kpis { margin:0 -7px 10px; }
      .observability-kpi { padding:7px; }
      .observability-kpi-card, .observability-panel { background:var(--card-bg,#fff); border:1px solid var(--border-color,#d1d8dd); border-radius:8px; padding:14px 16px; height:100%; box-shadow:0 1px 2px rgba(0,0,0,.035); }
      .observability-kpi-label { color:var(--text-muted,#687178); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
      .observability-kpi-value { font-size:22px; font-weight:600; margin-top:5px; overflow-wrap:anywhere; }
      .observability-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:14px; margin:14px 0; }
      .observability-panel h4 { margin:0 0 12px; }
      .observability-chart { min-height:260px; }
      .observability-table { overflow:auto; }
      .observability-table table { margin-bottom:0; }
      .observability-log-tail { max-height:60vh; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; background:#101820; color:#e8edf0; padding:14px; border-radius:6px; }
      .observability-actions { display:flex; flex-wrap:wrap; gap:6px; }
      @media (max-width:767px) { .observability-grid { grid-template-columns:1fr; } .observability-kpi-value { font-size:18px; } }
    </style>
  `);

  page.main.on("click", "[data-tab]", function () {
    state.tab = this.getAttribute("data-tab");
    page.main.find("[data-tab]").removeClass("active");
    this.classList.add("active");
    renderTab();
  });
  page.main.find("[data-window]").on("change", function () {
    state.windowMinutes = Number(this.value) || 60;
    loadData();
  });
  page.main.on("click", "[data-ack]", function () {
    const eventName = this.getAttribute("data-ack");
    frappe.call({ method: "frappe_observability.dashboard.acknowledge_alert", args: { name: eventName }, callback: () => loadAlerts() });
  });
  page.main.on("click", "[data-tail]", function () { showTail(this.getAttribute("data-tail")); });
  page.main.on("click", "[data-download]", function () {
    const fileId = this.getAttribute("data-download");
    const gzip = this.getAttribute("data-gzip") === "1";
    const file = state.logFiles.find((item) => item.id === fileId);
    if (!file) return;
    const open = (confirmed) => {
      const method = gzip ? "download_log_gzip" : "download_log";
      const confirm = confirmed ? "&confirm=1" : "";
      window.open(`/api/method/frappe_observability.log_browser.${method}?file_id=${encodeURIComponent(fileId)}${confirm}`, "_blank", "noopener");
    };
    if (Number(file.size_bytes || 0) > state.logThreshold) {
      frappe.confirm(__("This raw log download is large and may contain secrets or personal data. Continue?"), () => open(true));
    } else open(false);
  });
  page.main.on("click", "[data-copy-path]", async function () {
    try { await navigator.clipboard.writeText(this.getAttribute("data-copy-path")); frappe.show_alert({ message: __("Path copied"), indicator: "green" }); }
    catch (_error) { frappe.msgprint(__("Clipboard access is unavailable in this browser.")); }
  });
  page.main.on("click", "[data-rotate-token]", () => rotateToken());
  page.main.on("click", "[data-settings]", () => frappe.set_route("Form", "Observability Settings", "Observability Settings"));

  function panel(title, body) {
    return `<div class="observability-panel"><h4>${esc(title)}</h4>${body}</div>`;
  }

  function chartMarkup(id, title) {
    return panel(title, `<div class="observability-chart" id="${esc(id)}"></div>`);
  }

  function card(label, value, key) {
    return `<div class="col-xl-2 col-lg-3 col-md-4 col-6 observability-kpi" data-kpi="${esc(key)}"><div class="observability-kpi-card"><div class="observability-kpi-label">${esc(label)}</div><div class="observability-kpi-value">${esc(value)}</div></div></div>`;
  }

  function drawChart(id, title, data, type = "line", colors = ["#2490ef", "#e24c4b"]) {
    const target = page.main.find(`#${id}`)[0];
    if (!target) return;
    const labels = (data || []).map((point) => String(point.ts || "").slice(5, 16));
    const values = (data || []).map((point) => Number.isFinite(Number(point.value)) ? Number(point.value) : 0);
    if (!labels.length) {
      target.innerHTML = `<p class="text-muted">${esc(__("No samples in this time range yet."))}</p>`;
      return;
    }
    if (typeof frappe.Chart !== "function") {
      target.innerHTML = `<p class="text-muted">${esc(__("Frappe Charts is not available on this Desk build."))}</p>`;
      return;
    }
    try {
      const chart = new frappe.Chart(target, {
        title,
        data: { labels, datasets: [{ name: title, values }] },
        type,
        height: 250,
        colors,
        lineOptions: { regionFill: 1, hideDots: labels.length > 80 },
        axisOptions: { xAxisMode: "tick", xIsSeries: 1 },
      });
      state.charts.push(chart);
    } catch (error) {
      target.innerHTML = `<p class="text-muted">${esc(error.message || __("Chart could not be rendered."))}</p>`;
    }
  }

  function rateSeries(cumulative) {
    const points = cumulative || [];
    return points.map((point, index) => {
      if (index === 0) return { ts: point.ts, value: 0 };
      const previous = points[index - 1];
      const currentTime = new Date(String(point.ts || "").replace(" ", "T")).getTime();
      const previousTime = new Date(String(previous.ts || "").replace(" ", "T")).getTime();
      const seconds = Number.isFinite(currentTime - previousTime) && currentTime > previousTime
        ? (currentTime - previousTime) / 1000 : 60;
      const current = Number(point.value) || 0;
      const prior = Number(previous.value) || 0;
      return { ts: point.ts, value: Math.max(0, current >= prior ? current - prior : current) / seconds };
    });
  }

  function table(headers, rows) {
    if (!rows || !rows.length) return `<p class="text-muted">${esc(__("No records available."))}</p>`;
    const head = headers.map((header) => `<th>${esc(header.label)}</th>`).join("");
    const body = rows.map((row) => `<tr>${headers.map((header) => `<td>${header.render ? header.render(row) : esc(row[header.key])}</td>`).join("")}</tr>`).join("");
    return `<div class="observability-table table-responsive"><table class="table table-bordered"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function renderKpis() {
    const data = state.data;
    if (!data) return;
    const summary = data.summary || {};
    const live = data.live || {};
    const staleAfter = Number(summary.scheduler_stale_after_seconds) || 180;
    const sampleIsStale = Number(summary.last_sample_age_seconds) > staleAfter;
    const reqPerMinute = Number(summary.requests || 0) / Math.max(1, Number(data.window_minutes || 60));
    const p95 = last(data.series?.p95_ms);
    const items = [
      [__("Requests / min"), number(reqPerMinute, 1), "requests"],
      [__("HTTP errors"), `${number(summary.error_rate_percent, 2)}%`, "errors"],
      [__("P95 latency"), `${number(p95, 0)} ms`, "p95"],
      [__("Active users"), summary.active_users ?? "—", "active-users"],
      [__("Active IPs"), summary.active_ips ?? "—", "active-ips"],
      [__("Bandwidth"), `${bytes(summary.bytes_in)} in / ${bytes(summary.bytes_out)} out`, "bandwidth"],
      [__("Open alerts"), summary.open_alerts ?? 0, "alerts"],
      [__("Collector errors"), summary.collector_errors ?? 0, "collector-errors"],
      [__("Last scheduler sample"), `${ageText(summary.last_sample_age_seconds)}${sampleIsStale ? ` · ${__("STALE")}` : ""}`, "scheduler"],
    ];
    if (data.can_view_host) {
      items.push(
        [__("CPU"), `${number(live.host_cpu_percent)}%`, "cpu"],
        [__("Memory"), `${number(live.host_memory_percent)}%`, "memory"],
        [__("Root disk"), `${number(live.host_root_disk_percent)}%`, "disk"],
        [__("Queue depth"), number(live.queue_depth_total, 0), "queue"],
        [__("Backup age"), live.backup_age_hours == null ? "—" : `${number(live.backup_age_hours)} h`, "backup"],
      );
      if (data.mode !== "external") items.push([__("Last database flush"), summary.last_flush_age_seconds == null ? "—" : ageText(summary.last_flush_age_seconds), "flush"]);
    }
    page.main.find("[data-kpis]").html(items.map((item) => card(item[0], item[1], item[2])).join(""));
    const age = last(data.series?.root_disk_percent);
    page.main.find('[data-kpi="disk"] .observability-kpi-value').text(live.host_root_disk_percent == null && age ? `${number(age)}%` : (live.host_root_disk_percent == null ? "—" : `${number(live.host_root_disk_percent)}%`));
  }

  function renderTab() {
    const data = state.data;
    if (!data) return;
    const series = data.series || {};
    const live = data.live || {};
    state.charts.forEach((chart) => { try { chart.destroy?.(); } catch (_error) {} });
    state.charts = [];
    let html = "";

    if (state.tab === "overview") {
      html = `<div class="observability-grid">${chartMarkup("obs-requests", __("Requests per interval"))}${chartMarkup("obs-errors", __("HTTP errors per interval"))}${chartMarkup("obs-p95", __("P95 latency (ms)"))}${chartMarkup("obs-bandwidth-in", __("Request bytes per interval"))}${panel(__("Recent alerts"), `<div data-alert-list>${esc(__("Loading alerts…"))}</div>`)}</div>`;
      page.main.find("[data-content]").html(html);
      drawChart("obs-requests", __("Requests"), series.requests);
      drawChart("obs-errors", __("HTTP errors"), series.errors, "bar", ["#e24c4b"]);
      drawChart("obs-p95", __("P95 latency (ms)"), series.p95_ms, "line", ["#8f5db7"]);
      drawChart("obs-bandwidth-in", __("Request bytes in"), series.bytes_in, "bar", ["#2c9c69"]);
      loadAlerts();
    } else if (state.tab === "traffic") {
      const statuses = series.http_status || {};
      const statusCards = Object.entries(statuses).map(([status, values]) => `<div class="col-md-2 col-4 observability-kpi"><div class="observability-kpi-card"><div class="observability-kpi-label">${esc(status)}</div><div class="observability-kpi-value">${number(values.reduce((sum, item) => sum + Number(item.value || 0), 0), 0)}</div></div></div>`).join("");
      const endpoints = (series.endpoint_requests || []).map((row) => ({ label: row.label, requests: number(row.requests, 0) }));
      html = `<div class="row">${statusCards}</div><div class="observability-grid">${chartMarkup("obs-traffic-requests", __("Request volume"))}${chartMarkup("obs-traffic-errors", __("Error rate (%)"))}${chartMarkup("obs-traffic-p95", __("P95 latency (ms)"))}${chartMarkup("obs-traffic-in", __("Request bytes in"))}${chartMarkup("obs-traffic-out", __("Response bytes out"))}${panel(__("Top endpoint groups (latest bucket)"), table([{ key: "label", label: __("Endpoint group") }, { key: "requests", label: __("Requests") }], endpoints))}</div>`;
      page.main.find("[data-content]").html(html);
      drawChart("obs-traffic-requests", __("Requests"), series.requests);
      drawChart("obs-traffic-errors", __("Error rate (%)"), series.error_rate_percent, "line", ["#e24c4b"]);
      drawChart("obs-traffic-p95", __("P95 latency (ms)"), series.p95_ms, "line", ["#8f5db7"]);
      drawChart("obs-traffic-in", __("Request bytes in"), series.bytes_in, "bar", ["#2c9c69"]);
      drawChart("obs-traffic-out", __("Response bytes out"), series.bytes_out, "bar", ["#4d87c7"]);
    } else if (state.tab === "users") {
      if (!data.can_manage) {
        html = panel(__("Restricted data"), `<p>${esc(__("Top user and IP summaries are visible to System Managers and Observability Administrators only."))}</p>`);
      } else {
        const tops = data.top_usage || {};
        const usageRows = (items) => (items || []).map((row) => ({ ...row, bytes_human: bytes(row.bytes) }));
        html = `<div class="observability-grid">${panel(__("Top users"), table([{ key: "key", label: __("User") }, { key: "requests", label: __("Requests") }, { key: "bytes_human", label: __("Bytes") }, { key: "errors", label: __("Errors") }], usageRows(tops.user)))}${panel(__("Top IPs (masked by default)"), table([{ key: "key", label: __("IP / network") }, { key: "requests", label: __("Requests") }, { key: "bytes_human", label: __("Bytes") }, { key: "errors", label: __("Errors") }], usageRows(tops.ip)))}${panel(__("Top endpoint groups"), table([{ key: "key", label: __("Endpoint group") }, { key: "requests", label: __("Requests") }, { key: "bytes_human", label: __("Bytes") }, { key: "errors", label: __("Errors") }], usageRows(tops.endpoint)))}</div>`;
      }
      page.main.find("[data-content]").html(html);
    } else if (state.tab === "server") {
      html = data.can_view_host
        ? `<div class="observability-grid">${chartMarkup("obs-server-cpu", __("CPU utilization (%)"))}${chartMarkup("obs-server-memory", __("Memory utilization (%)"))}${chartMarkup("obs-server-disk", __("Root disk utilization (%)"))}${chartMarkup("obs-server-load", __("Database pressure (%)"))}${chartMarkup("obs-server-disk-read", __("Disk read throughput (B/s)"))}${chartMarkup("obs-server-disk-write", __("Disk write throughput (B/s)"))}${chartMarkup("obs-server-network-in", __("Network receive throughput (B/s)"))}${chartMarkup("obs-server-network-out", __("Network send throughput (B/s)"))}${panel(__("Current host metrics"), keyValueTable(live, ["host_cpu_percent", "host_memory_used_bytes", "host_memory_available_bytes", "host_swap_used_bytes", "host_swap_percent", "host_load_1m", "host_load_5m", "host_load_15m", "host_root_disk_used_bytes", "host_root_disk_free_bytes", "scheduler_lag_seconds", "flush_age_seconds"]))}${panel(__("Filesystem mount points"), mountTable(live))}</div>`
        : panel(__("Restricted data"), `<p>${esc(__("Host and database resource metrics require System Manager or Observability Administrator access."))}</p>`);
      page.main.find("[data-content]").html(html);
      if (data.can_view_host) {
        drawChart("obs-server-cpu", __("CPU (%)"), series.cpu_percent);
        drawChart("obs-server-memory", __("Memory (%)"), series.memory_percent, "line", ["#d88529"]);
        drawChart("obs-server-disk", __("Root disk (%)"), series.root_disk_percent, "line", ["#e24c4b"]);
        drawChart("obs-server-load", __("DB connections (%)"), series.db_connections_percent, "line", ["#2c9c69"]);
        drawChart("obs-server-disk-read", __("Disk read (B/s)"), rateSeries(series.disk_read_bytes_total), "line", ["#2490ef"]);
        drawChart("obs-server-disk-write", __("Disk write (B/s)"), rateSeries(series.disk_write_bytes_total), "line", ["#2c9c69"]);
        drawChart("obs-server-network-in", __("Network receive (B/s)"), rateSeries(series.network_in_bytes_total), "line", ["#d88529"]);
        drawChart("obs-server-network-out", __("Network send (B/s)"), rateSeries(series.network_out_bytes_total), "line", ["#8f5db7"]);
      }
    } else if (state.tab === "jobs") {
      html = data.can_view_host
        ? `<div class="observability-grid">${chartMarkup("obs-job-queue", __("Background queue depth"))}${chartMarkup("obs-job-failures", __("Job failures"))}${chartMarkup("obs-db-connections", __("MariaDB connections used (%)"))}${chartMarkup("obs-redis-memory", __("Cache Redis maxmemory used (%)"))}${chartMarkup("obs-queue-redis-memory", __("RQ Redis maxmemory used (%)"))}${panel(__("RQ queues"), keyValueTable(live, ["queue_short_depth", "queue_default_depth", "queue_long_depth", "queue_short_failed", "queue_default_failed", "queue_long_failed", "queue_depth_total", "queue_failed_total"]))}${panel(__("Database and cache Redis"), keyValueTable(live, ["database_connections", "database_max_connections", "database_connections_percent", "database_threads_running", "database_long_queries", "database_slow_queries_total", "database_buffer_pool_hit_percent", "database_size_bytes", "redis_memory_used_bytes", "redis_memory_max_bytes", "redis_memory_percent", "redis_clients_connected", "redis_cache_hit_percent", "redis_evicted_keys_total"]))}${panel(__("Queue Redis"), keyValueTable(live, ["redis_queue_memory_used_bytes", "redis_queue_memory_max_bytes", "redis_queue_memory_percent", "redis_queue_clients_connected", "redis_queue_cache_hit_percent", "redis_queue_evicted_keys_total"]))}</div>`
        : panel(__("Restricted data"), `<p>${esc(__("Job queue, database, and Redis telemetry requires elevated access."))}</p>`);
      page.main.find("[data-content]").html(html);
      if (data.can_view_host) {
        drawChart("obs-job-queue", __("Queue depth"), series.queue_depth, "bar", ["#2490ef"]);
        drawChart("obs-job-failures", __("Job failures"), series.job_failures, "bar", ["#e24c4b"]);
        drawChart("obs-db-connections", __("DB connections (%)"), series.db_connections_percent);
        drawChart("obs-redis-memory", __("Redis memory (%)"), series.redis_memory_percent, "line", ["#d88529"]);
        drawChart("obs-queue-redis-memory", __("RQ Redis memory (%)"), series.redis_queue_memory_percent, "line", ["#8f5db7"]);
      }
    } else if (state.tab === "storage") {
      html = data.can_view_host
        ? `<div class="observability-grid">${chartMarkup("obs-storage-db", __("Site database size (bytes)"))}${chartMarkup("obs-storage-files", __("Site file storage (bytes)"))}${chartMarkup("obs-storage-logs", __("Log directory size (bytes)"))}${panel(__("Largest database tables (hourly)"), table([{ key: "table", label: __("Table") }, { key: "size", label: __("Size") }], largestTableRows(live)))}${panel(__("Backups"), keyValueTable(live, ["backup_age_hours", "backup_latest_size_bytes", "backup_count"]))}${panel(__("Email and application health"), keyValueTable(live, ["email_queue_pending", "email_queue_errors", "application_error_logs_per_minute", "collector_errors_total"]))}${panel(__("Storage totals"), keyValueTable(live, ["storage_public_bytes", "storage_public_files", "storage_private_bytes", "storage_private_files", "log_directory_bytes", "log_directory_files"]))}</div>`
        : panel(__("Restricted data"), `<p>${esc(__("Storage and backup metrics require elevated access."))}</p>`);
      page.main.find("[data-content]").html(html);
      if (data.can_view_host) {
        drawChart("obs-storage-db", __("Database size (bytes)"), series.database_size_bytes, "line", ["#8f5db7"]);
        drawChart("obs-storage-files", __("Public storage (bytes)"), series.public_storage_bytes);
        drawChart("obs-storage-logs", __("Log directory size (bytes)"), series.log_directory_bytes, "line", ["#d88529"]);
      }
    } else if (state.tab === "logs") {
      if (!data.can_manage) {
        page.main.find("[data-content]").html(panel(__("Restricted data"), `<p>${esc(__("The read-only log browser requires System Manager or Observability Administrator access."))}</p>`));
      } else {
        html = `${panel(__("Read-only log browser"), `<p>${esc(__("Viewing applies configured redaction. Raw downloads may contain secrets and personal data; large files require confirmation."))}</p><div data-log-list>${esc(__("Loading log files…"))}</div>`)}${panel(__("Log tail"), `<div data-log-tail class="observability-log-tail" hidden></div>`)}`;
        page.main.find("[data-content]").html(html);
        loadLogs();
      }
    } else if (state.tab === "alerts") {
      html = panel(__("Alert events"), `<div data-alert-list>${esc(__("Loading alerts…"))}</div>`);
      page.main.find("[data-content]").html(html);
      loadAlerts();
    } else if (state.tab === "diagnostics") {
      html = panel(__("Frappe diagnostics"), `<p>${esc(__("These panels use the standard Frappe diagnostic records; this app does not duplicate them."))}</p><div class="observability-actions"><button class="btn btn-default" data-route="Error Log">${__("Error Log")}</button><button class="btn btn-default" data-route="RQ Job">${__("RQ Job")}</button><button class="btn btn-default" data-route="Recorder">${__("Recorder")}</button><button class="btn btn-default" data-route="System Health Report">${__("System Health Report")}</button><button class="btn btn-default" data-route="Scheduled Job Log">${__("Scheduled Job Log")}</button></div>`);
      page.main.find("[data-content]").html(html);
      page.main.off("click", "[data-route]").on("click", "[data-route]", function () {
        const doctype = this.getAttribute("data-route");
        if (doctype === "System Health Report") frappe.set_route("query-report", doctype);
        else frappe.set_route("List", doctype);
      });
    } else if (state.tab === "settings") {
      html = data.can_manage
        ? panel(__("App settings"), `<p>${esc(__("Collection, retention, privacy, export, log roots, redaction, and alert recipients are configured in the singleton settings DocType."))}</p><div class="observability-actions"><button class="btn btn-primary" data-settings>${__("Open Observability Settings")}</button><button class="btn btn-default" data-rotate-token>${__("Rotate optional Prometheus token")}</button></div><div data-token-result></div><p class="text-muted" style="margin-top:12px">${esc(__("All dashboards and charts are part of this Frappe app. Prometheus export is optional; Grafana is not required."))}</p>`)
        : panel(__("Restricted settings"), `<p>${esc(__("Only System Managers and Observability Administrators can change app settings."))}</p>`);
      page.main.find("[data-content]").html(html);
    }
  }

  function keyValueTable(source, keys) {
    const rows = keys.filter((key) => source[key] != null).map((key) => {
      let value = source[key];
      if (key.endsWith("_bytes")) value = `${bytes(value)} (${number(value, 0)} B)`;
      else if (key.endsWith("_percent")) value = `${number(value)}%`;
      else if (key.endsWith("_hours")) value = `${number(value)} h`;
      else value = number(value, 2);
      return { key, value };
    });
    return table([{ key: "key", label: __("Metric") }, { key: "value", label: __("Current value") }], rows);
  }

  function mountTable(live) {
    const mounts = Object.keys(live).filter((key) => key.startsWith("host_disk_percent|")).map((key) => {
      const mount = key.split("|", 2)[1];
      return { mount, percent: `${number(live[key])}%`, used: bytes(live[`host_disk_used_bytes|${mount}`]), free: bytes(live[`host_disk_free_bytes|${mount}`]) };
    });
    return table([{ key: "mount", label: __("Mount") }, { key: "percent", label: __("Used") }, { key: "used", label: __("Used bytes") }, { key: "free", label: __("Free") }], mounts);
  }

  function largestTableRows(live) {
    return Object.keys(live).filter((key) => key.startsWith("database_table_size_bytes|")).map((key) => {
      const value = Number(live[key]) || 0;
      return { table: key.split("|", 2)[1], size: `${bytes(value)} (${number(value, 0)} B)`, raw: value };
    }).sort((left, right) => right.raw - left.raw).slice(0, 10);
  }

  function loadAlerts() {
    frappe.call({
      method: "frappe_observability.dashboard.get_recent_alerts",
      args: { limit: 20 },
      callback(response) {
        const alerts = response.message || [];
        const headers = [
          { key: "fired_at", label: __("Time") },
          { key: "severity", label: __("Severity") },
          { key: "status", label: __("Status") },
          { key: "message", label: __("Message") },
        ];
        if (state.data?.can_manage) headers.push({ label: __("Action"), render: (row) => row.status === "Open" ? `<button class="btn btn-xs btn-default" data-ack="${esc(row.name)}">${__("Acknowledge")}</button>` : "—" });
        const content = table(headers, alerts);
        page.main.find("[data-alert-list]").html(content);
        if (state.tab === "overview") page.main.find(".observability-alerts-placeholder").remove();
      },
      error() { page.main.find("[data-alert-list]").text(__("Alert data could not be loaded.")); },
    });
  }

  function loadLogs() {
    frappe.call({
      method: "frappe_observability.log_browser.list_logs",
      callback(response) {
        const result = response.message || {};
        if (!result.ok) {
          page.main.find("[data-log-list]").text(result.error?.message || __("Log listing unavailable."));
          return;
        }
        state.logFiles = result.files || [];
        state.logThreshold = Number(result.download_confirmation_threshold_bytes || 0);
        const files = state.logFiles.map((file) => {
          const fileId = esc(file.id);
          const compressed = String(file.name || "").toLowerCase().endsWith(".gz");
          const tail = compressed ? `<span class="text-muted">${__("Download to inspect")}</span>` : `<button class="btn btn-xs btn-default" data-tail="${fileId}">${__("Tail")}</button>`;
          const gzip = compressed ? "" : `<button class="btn btn-xs btn-default" data-download="${fileId}" data-gzip="1">${__("Gzip")}</button>`;
          return { ...file, human_size: file.size_human, actions: `<div class="observability-actions">${tail}<button class="btn btn-xs btn-default" data-download="${fileId}">${__("Download")}</button>${gzip}<button class="btn btn-xs btn-default" data-copy-path="${esc(file.path)}">${__("Copy path")}</button></div>` };
        });
        page.main.find("[data-log-list]").html(`${table([{ key: "name", label: __("File") }, { key: "scope", label: __("Scope") }, { key: "path", label: __("Absolute path") }, { key: "human_size", label: __("Size") }, { key: "modified", label: __("Modified") }, { key: "actions", label: __("Actions"), render: (row) => row.actions }], files)}<p class="text-muted">${esc(__("Directory totals"))}: ${esc(result.totals?.site?.size_human || "0 B")} ${esc(__("site"))}, ${esc(result.totals?.bench?.size_human || "0 B")} ${esc(__("bench"))}.</p><p class="text-warning">${esc(result.raw_download_warning || "")}</p>`);
      },
      error() { page.main.find("[data-log-list]").text(__("Log browser unavailable for this role or server.")); },
    });
  }

  function showTail(fileId) {
    const box = page.main.find("[data-log-tail]");
    box.prop("hidden", false).text(__("Loading redacted log tail…"));
    frappe.call({
      method: "frappe_observability.log_browser.tail_log",
      args: { file_id: fileId, limit: 1000000 },
      callback(response) {
        const result = response.message || {};
        if (!result.ok) box.text(result.error?.message || __("Log tail unavailable."));
        else box.text(result.text || "");
      },
      error() { box.text(__("Log tail unavailable.")); },
    });
  }

  function rotateToken() {
    frappe.confirm(__("Rotate the optional Prometheus bearer token? The old token will stop working; the new secret is shown only once."), () => {
      frappe.call({
        method: "frappe_observability.api.rotate_export_token",
        callback(response) {
          const token = response.message?.token;
          if (!token) return;
          page.main.find("[data-token-result]").html(`<p class="text-warning">${esc(__("Copy this token now. It is not stored in plaintext."))}</p><code data-secret></code><button class="btn btn-xs btn-default" data-copy-secret>${__("Copy token")}</button>`);
          page.main.find("[data-secret]").text(token);
          page.main.find("[data-copy-secret]").on("click", async () => {
            try { await navigator.clipboard.writeText(token); frappe.show_alert({ message: __("Token copied"), indicator: "green" }); }
            catch (_error) { frappe.msgprint(__("Clipboard access is unavailable. Select and copy the token.")); }
          });
        },
      });
    });
  }

  function loadData() {
    page.main.find("[data-content]").html(`<p class="text-muted">${esc(__("Loading observability data…"))}</p>`);
    frappe.call({
      method: "frappe_observability.dashboard.get_dashboard_data",
      args: { window_minutes: state.windowMinutes },
      callback(response) {
        state.data = response.message || {};
        const modeMessage = state.data.mode === "external"
          ? __("External mode: live values only; database history is not being written.")
          : __("In-app Frappe charts · storage mode: ") + (state.data.mode || "internal");
        const sampleAge = Number(state.data.summary?.last_sample_age_seconds);
        const staleAfter = Number(state.data.summary?.scheduler_stale_after_seconds) || 180;
        page.main.find("[data-mode]").text(modeMessage + (Number.isFinite(sampleAge) && sampleAge > staleAfter ? ` · ${__("WARNING: scheduled sample is stale")} (${ageText(sampleAge)})` : ""));
        renderKpis();
        renderTab();
      },
      error() {
        page.main.find("[data-kpis]").empty();
        page.main.find("[data-content]").html(`<div class="alert alert-danger">${esc(__("Observability data could not be loaded. Check app permissions, scheduler, Redis, and database migration."))}</div>`);
      },
    });
  }

  loadData();
};
