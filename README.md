# SRS: Frappe Observability (`frappe_observability`) v1.0

Target: Frappe Framework + ERPNext **16.5 only**. DB: MariaDB. Self-hosted bench.
Status: draft for client review.

<img width="1536" height="1024" alt="Frappe Observability Architecture Diagram" src="https://github.com/user-attachments/assets/a2742cbe-2710-452e-a097-f031e7d16ed7" />

## 1. Introduction
1.1 **Purpose.** A complete in-Desk metrics system, plus a log-file browser, for self-hosted ERPNext sites that have no monitoring today.
1.2 **Scope (in).** Usage, traffic, bandwidth, active users and IPs, host, DB, Redis, jobs, backup and storage metrics; alerts; log file browser; optional Prometheus export.
1.3 **Out of scope.** Log aggregation or search across hosts (Loki, ELK), log shipping, distributed tracing (Tempo, OTLP), alerting when ERPNext itself is down, Frappe Cloud, PostgreSQL, log delete/edit.
1.4 **Reuse (not rebuilt).** Error Log, RQ Job, Recorder, System Health Report, Monitor (`monitor.json.log`). The app links to them.

## 2. Overall description
2.1 **One package, one core, one toggle.** A single app. Collectors write to Redis. Mode `internal` | `external` | `both` decides the sinks. Sink A stores 7 days in the ERPNext DB. Sink B exposes `/metrics` for an external Prometheus and Grafana. There is no separate exporter package.
2.2 **Users.** System Manager (site data, site logs), Observability Admin (new role), Administrator (bench-level host metrics and bench logs), Prometheus scraper (token, `/metrics` only).
2.3 **Constraints.** No per-sample DocType inserts via `doc.insert()`. No DB writes in the request path. Frappe 16.5 only; CI against the exact 16.5 revisions.
2.4 **Assumptions.** One bench host in v1. Scheduler enabled. Redis cache and queue reachable. The bench user can read `logs/` and `sites/*/logs/`.

### 2.5 Architecture diagram

```mermaid
flowchart TB
    subgraph SRC["Sources (bench host)"]
        REQ["Web requests<br/>before/after_request hooks"]
        JOB["Background jobs<br/>before/after_job hooks"]
        HOST["Host: psutil<br/>CPU, RAM, disk, net"]
        DBS["DB: SHOW STATUS, sizes"]
        RDS["Redis: memory, clients,<br/>hit rate, evictions"]
        APPH["App health: Error Log rate,<br/>Email Queue, backups, storage"]
        LOGF["Log files on disk<br/>bench/logs, sites/site/logs"]
    end

    subgraph APP["frappe_observability (single app)"]
        COL["Request/job collectors<br/>traffic, bandwidth, active users/IPs<br/>fail-open, Redis only"]
        SAMP["Sampler job (1 min)<br/>host, DB, Redis, app health"]
        RED[("Redis<br/>per-minute buckets, HLL,<br/>top-N sets, cumulative counters")]
        MODE{"Mode toggle<br/>internal / external / both"}
        FLUSH["Flush job (1 min)<br/>bulk insert to DB, collect errors"]
        PRUNE["Prune job (daily)<br/>chunked delete past retention"]
        EVAL["Alert evaluator<br/>runs after each flush"]

        subgraph SINKA["Sink A: inside ERPNext"]
            DB[("ERPNext DB, 7-day retention<br/>Metric Bucket, Usage Top,<br/>Alert Rule, Alert Event,<br/>Log Access Entry")]
            LOGB["Log browser<br/>path, size, mtime<br/>tail, download, gzip<br/>path guard, redaction, audit"]
            WS["Desk Workspace: Observability<br/>Overview, Traffic, Users and IPs,<br/>Server, Jobs and DB, Storage,<br/>Logs, Alerts, Diagnostics, Settings"]
        end

        subgraph SINKB["Sink B: external endpoint"]
            MET["/api/method/frappe_observability.api.metrics<br/>bearer token (hashed) + CIDR allowlist<br/>metric allowlist frappe_obs_*"]
        end
    end

    subgraph REUSE["Reused Frappe features (linked, not rebuilt)"]
        EL["Error Log"]
        RQ["RQ Job"]
        REC["Recorder"]
        SHR["System Health Report"]
        MON["Monitor: monitor.json.log"]
    end

    subgraph EXTS["External stack (optional, self-hosted)"]
        PROM["Prometheus"]
        GRAF["Grafana<br/>dashboards + alerts.yml from repo"]
    end

    USER(["System Manager /<br/>Observability Admin /<br/>Administrator"])
    NOTIFY(["Recipients<br/>Frappe Notification + email"])

    REQ --> COL
    JOB --> COL
    HOST --> SAMP
    DBS --> SAMP
    RDS --> SAMP
    APPH --> SAMP
    COL --> RED
    SAMP --> RED

    RED --> MODE
    MODE -->|internal / both| FLUSH
    MODE -->|external / both| MET
    FLUSH -->|write only| DB
    PRUNE --> DB
    FLUSH --> EVAL
    DB -->|rules| EVAL
    EVAL --> DB
    EVAL --> NOTIFY

    DB -->|read| WS
    RED -->|live values, last flush| WS
    LOGF -->|seek + stream, audit| LOGB
    LOGB --> WS
    WS --> USER

    WS -.links.-> EL
    WS -.links.-> RQ
    WS -.links.-> REC
    WS -.links.-> SHR
    MON -.file listed.-> LOGB

    RED -->|cumulative counters| MET
    PROM -->|scrape over HTTPS| MET
    PROM --> GRAF
```

## 3. Functional requirements

### 3.1 Collection
- FR-C1 Per request, record: site, normalized endpoint, status class, duration, bytes in and out, user, IP. Redis only.
- FR-C2 Latency histogram, buckets in ms: 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000+.
- FR-C3 Active users and active IPs per minute and per hour using HyperLogLog (counts only).
- FR-C4 Top 50 users, IPs and endpoints per hour (sorted set).
- FR-C5 Jobs: duration and failures per method, queue depth per queue, failed-job count, scheduler last tick and lag.
- FR-C6 Host: CPU %, load, RAM, swap, disk used/free per mount, disk IO, network bytes in/out.
- FR-C7 DB: connections vs max, threads running, slow-query count, buffer-pool hit ratio, DB size, top 10 tables by size (hourly), long-running query count.
- FR-C8 Redis (cache and queue): memory vs maxmemory, clients, hit rate, evictions.
- FR-C9 App health: Error Log rows/min, failed logins/min, Email Queue pending and error counts, last backup age and size per site, public/private file storage size (hourly).
- FR-C10 Fail-open: a collector exception never affects the request or job. Collector errors are counted and shown.
- FR-C11 Labels limited to site, host, normalized endpoint (top 50 then `other`), queue, status class. User and IP are never metric labels.

### 3.2 Storage
- FR-S1 Redis holds per-minute buckets and cumulative counters.
- FR-S2 `flush` (every minute) bulk-inserts into `Metric Bucket` and `Usage Top` via `frappe.db.bulk_insert`. No versioning or timeline.
- FR-S3 Retention default 7 days, configurable 1–30. `prune` is a separate daily job using chunked deletes (`LIMIT` loop). Flush does not prune.
- FR-S4 In `external` mode the DB flush is skipped. Cumulative Redis counters still run.
- FR-S5 Show "last flush" and collector-error count on the workspace.

### 3.3 Desk workspace (single place)
- FR-U1 Workspace **Observability** with pages: Overview, Traffic, Users & IPs, Server, Jobs & DB, Storage & Backups, Logs, Alerts, Diagnostics, Settings.
- FR-U2 Overview Number Cards: req/min, error %, p95, active users, active IPs, bandwidth, CPU, RAM, disk, queue depth, backup age, last flush.
- FR-U3 Time ranges 15m, 1h, 6h, 24h, 7d. Site and host filters.
- FR-U4 Diagnostics page links to Error Log, RQ Job, Recorder, System Health Report.
- FR-U5 Bench-level pages (Server, bench logs) require Administrator or the bench-level flag.
- FR-U6 In `external` mode the workspace shows live values from Redis only (no history charts). History lives in Grafana.

### 3.4 Log file browser
- FR-L1 Discover files in `<bench>/logs/` and `<bench>/sites/<site>/logs/`. Extra roots can be added in Settings by an admin.
- FR-L2 One list view. Columns: name, **absolute path**, scope (bench/site), **size (human and bytes)**, modified time, compressed/rotated flag. Totals: log directory size, disk free.
- FR-L3 Size is visible before any action. Downloads above a threshold (default 50 MB) require confirmation.
- FR-L4 Actions per file: View tail, Download, Download gzip, Copy path. All on the row.
- FR-L5 Viewer: last N lines (default 500), max 1 MB per request, page backward by offset. Filter within the loaded chunk. Optional live follow (poll 3 s) in v1.1.
- FR-L6 Files are read by seek and streamed in 64 KB chunks. Never loaded whole into memory.
- FR-L7 Security: `realpath` must stay inside an allowed root. Symlink escape, `..`, device files and non-regular files are rejected. Name allowlist: `*.log`, `*.log.N`, `*.json.log`, `*.gz`.
- FR-L8 Every view and download is audited (user, IP, file, bytes, time) and follows retention.
- FR-L9 View applies optional redaction (Authorization, password, token, secret, cookie patterns). Raw download shows a warning that the file may contain secrets and personal data.
- FR-L10 Read-only in v1: no delete, truncate or edit.
- FR-L11 Rotation or removal during a read returns a clear error, not a crash.
- FR-L12 Alerts on log directory total size and single-file size thresholds.

### 3.5 Alerts
- FR-A1 `Alert Rule` and `Alert Event` DocTypes. Evaluated after each flush. Cooldown per rule. Acknowledge supported.
- FR-A2 Default rules: disk >85% (warn) and >95% (critical), backup older than 48 h, queue backlog, error rate, DB connections >80%, Redis memory >90%, flush stale >3 min, log directory size.
- FR-A3 Notify through Frappe Notification and email.
- FR-A4 Stated limit: no alert is possible if ERPNext is down.
- FR-A5 In `external` mode (no flush) the in-app evaluator runs on a 1-minute schedule against live Redis values. Prometheus-side alerts use the shipped `alerts.yml`.

### 3.6 External export (Sink B)
- FR-E1 Toggle in Settings (mode `external` or `both`). Endpoint `/api/method/frappe_observability.api.metrics`.
- FR-E2 Bearer token (stored hashed) plus optional CIDR allowlist. Not public by default.
- FR-E3 Prometheus text format rendered from the cumulative Redis counters (no multiprocess client needed). Not read from the DB.
- FR-E4 Metric allowlist in Settings. Prefix `frappe_obs_`.
- FR-E5 Repo ships `grafana/*.json` dashboards and `alerts.yml`.

### 3.7 Settings (single DocType)
Mode, retention days, flush interval, IP storage (full or /24-masked), metric allowlist, export token and CIDRs, log roots, redaction patterns, download-confirm threshold, alert recipients.

## 4. Data
- `Observability Settings` (single).
- `Metric Bucket`: ts, metric, host, label, count, sum, max. Index (metric, ts).
- `Usage Top`: hour, dimension (user/ip/endpoint), key, requests, bytes, errors.
- `Alert Rule`, `Alert Event`, `Log Access Entry`.
- Redis: per-minute hashes, HLL keys, top-N sorted sets, cumulative counters. All keys site- or bench-prefixed with TTL.
- Expected DB size: ~0.5–1M rows per 7 days at ~100 series (to be measured).

## 5. Non-functional
- NFR-1 Added request overhead target: p95 ≤ 1 ms, one pipelined Redis round-trip. Measure before release.
- NFR-2 `flush` completes in <5 s for a typical minute. Prune never holds long locks.
- NFR-3 Settings changes take effect within 60 s without restart.
- NFR-4 IPs are personal data: masking option, admin-only top-N, audit trail.
- NFR-5 Compatibility: Frappe/ERPNext 16.5 only, `[tool.bench.frappe-dependencies]` pinned. Verify hook names (`before_request`, `after_request`, `before_job`, `after_job`) and the Log Settings clear hook on 16.5.
- NFR-6 Multi-site isolation: a site's System Manager sees only that site's data and logs.

## 6. Install and operations
1. `bench get-app <git-url> --branch <16.5 branch>`
2. `bench --site <site> install-app frappe_observability`
3. `bench --site <site> migrate`
4. `bench restart` (production: supervisor or systemd restart)
5. `bench --site <site> enable-scheduler`, then configure Settings.

Uninstall: `bench --site <site> uninstall-app frappe_observability`. Host logrotate is a prerequisite.

## 7. Acceptance criteria
- AC-1 Install, migrate and uninstall succeed on a clean 16.5 bench.
- AC-2 Under load, request, bandwidth, active-user and active-IP figures match a reference generator within 2%.
- AC-3 Rows older than retention are removed within 24 h. Table size stays bounded.
- AC-4 Log browser lists files with correct path and size. Tail and download work on a >1 GB file with flat memory use.
- AC-5 Path traversal and symlink-escape attempts are rejected and audited.
- AC-6 A site System Manager cannot see bench logs or another site's data.
- AC-7 Stopping the scheduler raises the "flush stale" state on the workspace.
- AC-8 `/metrics` rejects a missing or bad token and serves valid Prometheus text with the token.
- AC-9 Overhead measured and reported against NFR-1.
- AC-10 In `external` mode no rows are written to `Metric Bucket`; `/metrics` still serves current values.

## 8. Open items
Multi-host sampler process, 5-minute downsampling after 24 h, live-follow, PostgreSQL support, optional log truncate/rotate action, optional Loki/Tempo integration (post-v1).
