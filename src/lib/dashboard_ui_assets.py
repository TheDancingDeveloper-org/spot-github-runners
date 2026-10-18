"""Single-page Web UI Dashboard HTML, CSS, and interactive JavaScript application."""

from __future__ import annotations


import html


def render_dashboard_html(
    project: str = "homelab-424523",
    org: str = "swack-tools",
) -> str:
    """Return the complete HTML document for the Spot Fleet & Rust Builder Web UI Dashboard."""
    safe_project = html.escape(str(project or "homelab-424523"), quote=True)
    safe_org = html.escape(str(org or "swack-tools"), quote=True)
    return DASHBOARD_HTML.replace(
        '<span id="hdr-project-org">GCP Project: homelab-424523 · GitHub Org: swack-tools</span>',
        f'<span id="hdr-project-org">GCP Project: {safe_project} · GitHub Org: {safe_org}</span>',
        1,
    )


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Spot GitHub Runners &amp; Rust Builders — Control Plane Dashboard</title>
  <style>
    :root {
      --bg-base: #0b0f19;
      --bg-panel: #111827;
      --bg-card: #161f33;
      --bg-card-hover: #1e293b;
      --bg-input: #0f172a;
      --border: #26334d;
      --border-hi: #3b82f6;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --cyan: #06b6d4;
      --emerald: #10b981;
      --amber: #f59e0b;
      --rose: #f43f5e;
      --purple: #8b5cf6;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg-base);
      color: var(--text-main);
      font-family: var(--font-sans);
      font-size: 13.5px;
      line-height: 1.45;
      min-height: 100vh;
    }
    a { color: #60a5fa; text-decoration: none; }
    a:hover { text-decoration: underline; }

    /* Top Control Header */
    .topbar {
      background: linear-gradient(180deg, #111827 0%, #0d1322 100%);
      border-bottom: 1px solid var(--border);
      padding: 12px 20px;
      position: sticky;
      top: 0;
      z-index: 100;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.45);
    }
    .topbar-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      max-width: 1960px;
      margin: 0 auto;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand-logo {
      width: 36px;
      height: 36px;
      border-radius: 9px;
      background: linear-gradient(135deg, #3b82f6, #06b6d4);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 17px;
      color: #fff;
      box-shadow: 0 0 14px rgba(59, 130, 246, 0.45);
    }
    .brand-title {
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.01em;
    }
    .brand-sub {
      font-size: 11.5px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .tz-pill {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: rgba(6, 182, 212, 0.14);
      color: #67e8f9;
      border: 1px solid rgba(6, 182, 212, 0.35);
      padding: 1px 8px;
      border-radius: 999px;
      font-family: var(--font-mono);
      font-size: 11px;
    }

    /* Live Budget Strip */
    .budget-bar {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      background: var(--bg-card);
      border: 1px solid var(--border);
      padding: 7px 12px;
      border-radius: 10px;
    }
    .budget-bar label {
      font-size: 11px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      font-weight: 600;
    }
    .budget-input-group {
      display: flex;
      align-items: center;
      gap: 4px;
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 3px 7px;
    }
    .budget-input-group span {
      color: var(--text-muted);
      font-size: 12px;
      font-family: var(--font-mono);
    }
    .budget-input-group input {
      width: 64px;
      background: transparent;
      border: none;
      color: var(--text-main);
      font-family: var(--font-mono);
      font-size: 12.5px;
      font-weight: 600;
      outline: none;
    }

    /* Navigation Tabs */
    .nav-tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      max-width: 1960px;
      margin: 10px auto 0;
      padding-bottom: 2px;
    }
    .tab-btn {
      background: rgba(22, 31, 51, 0.7);
      color: var(--text-muted);
      border: 1px solid var(--border);
      padding: 7px 13px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 12.5px;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
      white-space: nowrap;
    }
    .tab-btn:hover {
      background: var(--bg-card-hover);
      color: var(--text-main);
    }
    .tab-btn.active {
      background: linear-gradient(135deg, rgba(59, 130, 246, 0.25), rgba(6, 182, 212, 0.2));
      color: #fff;
      border-color: var(--accent);
      box-shadow: 0 0 12px rgba(59, 130, 246, 0.25);
    }
    .tab-badge {
      background: rgba(255, 255, 255, 0.12);
      color: #e2e8f0;
      padding: 1px 6px;
      border-radius: 99px;
      font-size: 10.5px;
      font-family: var(--font-mono);
    }

    /* Main Content Container */
    .main-container {
      max-width: 1960px;
      margin: 0 auto;
      padding: 18px 20px 48px;
    }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }
    /* Keeps owner-only controls (the Admins tab) hidden despite the display rules above. */
    [hidden] { display: none !important; }

    /* Grid & Cards */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .kpi-card {
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 13px 15px;
    }
    .kpi-label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      margin-bottom: 4px;
    }
    .kpi-value {
      font-size: 21px;
      font-weight: 700;
      font-family: var(--font-mono);
      color: var(--text-main);
    }
    .kpi-sub {
      font-size: 11.5px;
      color: var(--text-muted);
      margin-top: 3px;
    }

    .panel {
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px 18px;
      margin-bottom: 16px;
    }
    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }
    .panel-title {
      font-size: 15px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .panel-subtitle {
      font-size: 12px;
      color: var(--text-muted);
    }

    .two-col {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }
    .map-layout {
      display: grid;
      grid-template-columns: 1.55fr 1fr;
      gap: 16px;
      align-items: start;
    }
    @media (max-width: 1150px) {
      .two-col, .map-layout { grid-template-columns: 1fr; }
    }

    /* Buttons & Inputs */
    .btn {
      background: var(--accent);
      color: #fff;
      border: 1px solid transparent;
      padding: 6px 12px;
      border-radius: 7px;
      font-size: 12.5px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s;
    }
    .btn:hover { background: var(--accent-hover); }
    .btn-sm { padding: 4px 9px; font-size: 11.5px; border-radius: 6px; }
    .btn-secondary {
      background: var(--bg-card);
      color: var(--text-main);
      border-color: var(--border);
    }
    .btn-secondary:hover { background: var(--bg-card-hover); border-color: #475569; }
    .btn-emerald { background: #059669; }
    .btn-emerald:hover { background: #047857; }
    .btn-danger { background: #e11d48; }
    .btn-danger:hover { background: #be123c; }
    .btn-purple { background: #7c3aed; }
    .btn-purple:hover { background: #6d28d9; }

    input[type="text"], input[type="number"], select, textarea {
      background: var(--bg-input);
      color: var(--text-main);
      border: 1px solid var(--border);
      border-radius: 7px;
      padding: 6px 10px;
      font-size: 12.5px;
      font-family: inherit;
      outline: none;
    }
    input[type="text"]:focus, input[type="number"]:focus, select:focus, textarea:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
    }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 11px;
      margin-bottom: 12px;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .field label {
      font-size: 11.5px;
      color: var(--text-muted);
      font-weight: 600;
    }
    .toggle-label {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      cursor: pointer;
      font-size: 12.5px;
      user-select: none;
    }
    .toggle-label input[type="checkbox"] {
      width: 15px;
      height: 15px;
      accent-color: var(--accent);
    }

    /* World Map Canvas */
    .world-map-box {
      position: relative;
      background: radial-gradient(circle at 50% 45%, #132038 0%, #09101d 100%);
      border: 1px solid var(--border);
      border-radius: 11px;
      overflow: hidden;
      min-height: 450px;
    }
    .world-svg {
      width: 100%;
      height: 450px;
      display: block;
    }
    .continent-shape {
      fill: #1c2c4c;
      stroke: #2e4775;
      stroke-width: 1.2;
    }
    .map-grid-line {
      stroke: rgba(148, 163, 184, 0.08);
      stroke-width: 1;
      stroke-dasharray: 3 3;
    }
    .dc-pin {
      cursor: pointer;
      transition: transform 0.15s ease;
    }
    .dc-pin:hover {
      filter: brightness(1.25);
    }
    .pin-pulse {
      animation: pulseRing 2.2s infinite ease-out;
      transform-origin: center;
      transform-box: fill-box;
    }
    @keyframes pulseRing {
      0% { transform: scale(0.9); opacity: 0.85; }
      70% { transform: scale(2.1); opacity: 0; }
      100% { transform: scale(2.1); opacity: 0; }
    }

    /* SSH Code Block */
    .ssh-block {
      background: #070b12;
      border: 1px solid #1e293b;
      border-radius: 8px;
      padding: 9px 11px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font-family: var(--font-mono);
      font-size: 12px;
      color: #38bdf8;
      margin-top: 8px;
    }
    .ssh-block code {
      overflow-x: auto;
      white-space: nowrap;
    }

    /* Tables */
    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 9px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
    }
    th, td {
      padding: 9px 11px;
      text-align: left;
      border-bottom: 1px solid rgba(38, 51, 77, 0.65);
    }
    th {
      background: #0f172a;
      color: var(--text-muted);
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
    }
    th:hover { color: #fff; background: #162238; }
    tr:hover td { background: rgba(30, 41, 59, 0.45); }
    .mono { font-family: var(--font-mono); }

    /* Status & Security Badges */
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      white-space: nowrap;
    }
    .badge-running { background: rgba(16, 185, 129, 0.16); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); }
    .badge-deleted { background: rgba(244, 63, 94, 0.15); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.35); }
    .badge-runner { background: rgba(59, 130, 246, 0.16); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.35); }
    .badge-builder { background: rgba(139, 92, 246, 0.18); color: #c4b5fd; border: 1px solid rgba(139, 92, 246, 0.35); }
    .badge-warn { background: rgba(245, 158, 11, 0.16); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.35); }
    .badge-tor { background: rgba(244, 63, 94, 0.2); color: #fda4af; border: 1px solid rgba(244, 63, 94, 0.5); }
    .badge-vpn { background: rgba(245, 158, 11, 0.18); color: #fcd34d; border: 1px solid rgba(245, 158, 11, 0.45); }
    .badge-direct { background: rgba(16, 185, 129, 0.14); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.3); }

    /* Cloud-Init Visualizer Stages */
    .stage-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .stage-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      padding: 10px 12px;
    }
    .stage-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-weight: 700;
      font-size: 13px;
      margin-bottom: 4px;
    }
    .stage-items {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 6px;
    }
    .stage-chip {
      background: #0b1120;
      border: 1px solid #28354e;
      border-radius: 5px;
      padding: 2px 7px;
      font-family: var(--font-mono);
      font-size: 11px;
      color: #cbd5e1;
    }

    /* Toast feedback */
    #toast-container {
      position: fixed;
      bottom: 20px;
      right: 20px;
      z-index: 9999;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .toast {
      background: #0f172a;
      color: #f8fafc;
      border: 1px solid var(--accent);
      border-left: 4px solid var(--emerald);
      padding: 10px 14px;
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
      font-size: 12.5px;
      font-weight: 600;
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-row">
      <div class="brand">
        <div class="brand-logo">⚡</div>
        <div>
          <div class="brand-title">Spot GitHub Runners &amp; Rust Remote Builders Control Plane</div>
          <div class="brand-sub">
            <span id="hdr-project-org">GCP Project: homelab-424523 · GitHub Org: swack-tools</span>
            <span class="tz-pill" id="browser-tz-pill">🕒 TZ: Detecting...</span>
          </div>
        </div>
      </div>

      <!-- On-the-Fly Billing Budget Editor -->
      <div class="budget-bar" id="live-budget-bar">
        <label>Live Max Budget:</label>
        <div class="budget-input-group" title="Maximum scale-up budget per hour">
          <span>$/hr</span>
          <input type="number" id="top-budget-hour" step="0.25" min="0.05" value="5.00" />
        </div>
        <div class="budget-input-group" title="Maximum scale-up budget per day">
          <span>$/day</span>
          <input type="number" id="top-budget-day" step="0.50" min="0.20" value="10.00" />
        </div>
        <div class="budget-input-group" title="Maximum scale-up budget per month">
          <span>$/mo</span>
          <input type="number" id="top-budget-month" step="5.00" min="1.00" value="50.00" />
        </div>
        <label class="toggle-label" style="text-transform:none; color:var(--text-main);">
          <input type="checkbox" id="top-budget-disk" checked />
          Incl. Disk &amp; Tax
        </label>
        <button class="btn btn-sm btn-emerald" onclick="applyTopBudget()">Apply Budget On-the-Fly</button>
        <button class="btn btn-sm btn-secondary" id="top-auth-btn" onclick="configureDashboardAuthToken()" title="Configure Bearer / Session Authentication Token">🔒 Auth Token</button>
      </div>
    </div>

    <!-- Navigation Tabs -->
    <nav class="nav-tabs" role="tablist">
      <button class="tab-btn active" data-tab="tab-map" onclick="switchTab('tab-map')">
        🌍 World Map &amp; Fleet Inspector <span class="tab-badge" id="badge-map-count">5 live</span>
      </button>
      <button class="tab-btn" data-tab="tab-instances" onclick="switchTab('tab-instances')">
        📋 Instances &amp; Cost History <span class="tab-badge" id="badge-instances-count">9</span>
      </button>
      <button class="tab-btn" data-tab="tab-sizing" onclick="switchTab('tab-sizing')">
        ⚙️ Sizing &amp; Benchmark Qualifier <span class="tab-badge" id="badge-qual-count">Spot</span>
      </button>
      <button class="tab-btn" data-tab="tab-scaling" onclick="switchTab('tab-scaling')">
        📈 Scaling &amp; OOM Deep-Links <span class="tab-badge" id="badge-oom-count">2 OOM</span>
      </button>
      <button class="tab-btn" data-tab="tab-cloudinit" onclick="switchTab('tab-cloudinit')">
        📜 Cloud-Init &amp; VM Templates <span class="tab-badge" id="badge-tpl-count">3</span>
      </button>
      <button class="tab-btn" data-tab="tab-regions" onclick="switchTab('tab-regions')">
        🌐 Regions &amp; Latency Filter <span class="tab-badge" id="badge-reg-count">14</span>
      </button>
      <button class="tab-btn" data-tab="tab-rust" onclick="switchTab('tab-rust')">
        🦀 Rust Builders Telemetry &amp; IP Intel <span class="tab-badge" id="badge-rust-count">5 builds</span>
      </button>
      <button class="tab-btn" data-tab="tab-patches" onclick="switchTab('tab-patches')">
        🧩 Runner Patches <span class="tab-badge" id="badge-patches">config repo</span>
      </button>
      <button class="tab-btn" data-tab="tab-admins" id="tab-btn-admins" onclick="switchTab('tab-admins')" hidden>
        🔑 Admins <span class="tab-badge" id="badge-admins">owner</span>
      </button>
    </nav>
  </header>

  <main class="main-container">
    <!-- Top KPI Summary Row -->
    <section class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Active Spot Fleet Burn Rate</div>
        <div class="kpi-value" id="kpi-burn-rate">$1.12/hr · $26.88/day</div>
        <div class="kpi-sub" id="kpi-burn-sub">Monthly projected: $817.60/mo (incl. disks &amp; tax)</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Online CI/CD GitHub Runners</div>
        <div class="kpi-value" id="kpi-runners-online">10 Runners (3 VMs)</div>
        <div class="kpi-sub" id="kpi-runners-sub">Min floor: 4 runners · 3 queued jobs · 14 runs/1h</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Dedicated Rust 1.99 Builders</div>
        <div class="kpi-value" id="kpi-builders-online">2 Builders Online</div>
        <div class="kpi-sub" id="kpi-builders-sub">Min benchmark: 1,400 · Peak RAM floor: 26.1 GiB</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Qualifying Spot Offers (Live)</div>
        <div class="kpi-value" id="kpi-qualifying-offers">28 Price-Qualifying</div>
        <div class="kpi-sub" id="kpi-qualifying-sub">Meets vCPU, RAM, &gt;=1000 CPU benchmark &amp; Spot cap</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Scaling &amp; OOM Alerts</div>
        <div class="kpi-value" id="kpi-scaling-status" style="color: #fbbf24;">SCALE UP (+2)</div>
        <div class="kpi-sub" id="kpi-scaling-sub">GitHub CI OOM + Majority CPU &gt; 50%</div>
      </div>
    </section>

    <!-- ===================================================================== -->
    <!-- TAB 1: WORLD MAP & DATA CENTER INSPECTOR                              -->
    <!-- ===================================================================== -->
    <section id="tab-map" class="tab-pane active">
      <div class="map-layout">
        <div class="panel">
          <div class="panel-header">
            <div>
              <div class="panel-title">🌍 Global Spot Fleet &amp; Data Center Map</div>
              <div class="panel-subtitle">Click any data center pin (e.g. <strong>South Africa — africa-south1</strong>, Dallas, Belgium, Netherlands) to inspect running runners, toggle disk/tax pricing, scale instances, or copy the <code>gcloud compute ssh</code> command.</div>
            </div>
            <div style="display:flex; gap:8px; align-items:center;">
              <label class="toggle-label">
                <input type="checkbox" id="map-price-with-disk-tax" checked onchange="renderMapInspector()" />
                Show Prices With Disk &amp; Taxes
              </label>
            </div>
          </div>

          <div class="world-map-box">
            <svg id="world-map-svg" class="world-svg" viewBox="0 0 1000 500" preserveAspectRatio="xMidYMid meet">
              <!-- Latitude / Longitude Grid -->
              <line x1="0" y1="125" x2="1000" y2="125" class="map-grid-line" />
              <line x1="0" y1="250" x2="1000" y2="250" class="map-grid-line" />
              <line x1="0" y1="375" x2="1000" y2="375" class="map-grid-line" />
              <line x1="250" y1="0" x2="250" y2="500" class="map-grid-line" />
              <line x1="500" y1="0" x2="500" y2="500" class="map-grid-line" />
              <line x1="750" y1="0" x2="750" y2="500" class="map-grid-line" />

              <!-- Stylized Continental Polygons -->
              <!-- North America -->
              <polygon class="continent-shape" points="70,65 260,55 320,120 285,205 210,235 165,215 120,165 75,120" />
              <!-- South America -->
              <polygon class="continent-shape" points="265,255 345,270 395,335 360,445 305,465 280,375" />
              <!-- Europe -->
              <polygon class="continent-shape" points="445,70 585,60 610,145 555,185 455,180 435,130" />
              <!-- Africa -->
              <polygon class="continent-shape" points="450,200 585,200 635,285 595,410 535,415 495,335 445,265" />
              <!-- Asia -->
              <polygon class="continent-shape" points="595,65 895,75 925,195 845,295 725,290 635,215 600,145" />
              <!-- Australia -->
              <polygon class="continent-shape" points="775,335 905,335 920,425 815,435 765,390" />

              <!-- Dynamic Region Pins Group -->
              <g id="map-pins-group"></g>
            </svg>
          </div>

          <div style="display:flex; gap:14px; flex-wrap:wrap; margin-top:10px; font-size:12px; color:var(--text-muted);">
            <span>🟢 <strong>Emerald Pin:</strong> Active Running Spot Instances (Click to inspect)</span>
            <span>🔵 <strong>Blue Pin:</strong> Enabled Region (0 running)</span>
            <span>⚪ <strong>Gray Pin:</strong> Region Deselected by Operator Filter</span>
          </div>
        </div>

        <!-- Selected Data Center / Instance Popup Card -->
        <div class="panel" id="map-inspector-panel">
          <!-- Populated dynamically by renderMapInspector() -->
        </div>
      </div>
    </section>

    <!-- ===================================================================== -->
    <!-- TAB 2: INSTANCE LIST & COST HISTORY (WITH DELETED & TIME WINDOW)      -->
    <!-- ===================================================================== -->
    <section id="tab-instances" class="tab-pane">
      <div class="panel">
        <div class="panel-header">
          <div>
            <div class="panel-title">📋 Fleet Instances &amp; Historical Spend Ledger</div>
            <div class="panel-subtitle">Inspect running and deleted instances, filter or sort by any column (including <strong>Total Price Paid</strong>), adjust the time window (24h, 50h, 30d, or custom), and view timestamps in your browser's local timezone (<span id="instances-tz-label" class="mono"></span>).</div>
          </div>
          <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
            <label class="toggle-label">
              <input type="checkbox" id="inst-show-deleted" checked onchange="refreshInstancesTable()" />
              <strong>Show Deleted Instances</strong>
            </label>
            <label class="toggle-label">
              <input type="checkbox" id="inst-include-disk-tax" checked onchange="refreshInstancesTable()" />
              Include Disk &amp; Taxes
            </label>
          </div>
        </div>

        <!-- Controls Toolbar -->
        <div style="display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:space-between; margin-bottom:14px; background:var(--bg-card); padding:10px 12px; border-radius:9px; border:1px solid var(--border);">
          <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
            <span style="font-size:12px; color:var(--text-muted); font-weight:600;">Time Scale:</span>
            <button class="btn btn-sm btn-secondary" onclick="setInstanceWindowHours(24)">Last 24h</button>
            <button class="btn btn-sm btn-secondary" onclick="setInstanceWindowHours(50)">Last 50h</button>
            <button class="btn btn-sm btn-secondary" onclick="setInstanceWindowHours(168)">Last 7d</button>
            <button class="btn btn-sm btn-secondary" onclick="setInstanceWindowHours(720)">Last 30d</button>
            <div style="display:inline-flex; align-items:center; gap:5px; margin-left:4px;">
              <input type="number" id="inst-custom-window-val" value="24" min="1" max="744" style="width:72px;" onchange="applyCustomWindow()" />
              <select id="inst-custom-window-unit" onchange="applyCustomWindow()">
                <option value="hours" selected>Hours</option>
                <option value="days">Days</option>
              </select>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <select id="inst-role-filter" onchange="refreshInstancesTable()">
              <option value="all">All Roles (Runners + Builders)</option>
              <option value="cicd_runner">CI/CD Runners Only</option>
              <option value="rust_builder">Rust Builders Only</option>
            </select>
            <input type="text" id="inst-search-input" placeholder="Filter by instance, region, spec, trigger..." style="width:240px;" oninput="refreshInstancesTable()" />
            <button class="btn btn-sm" onclick="sortInstancesBy('total_cost_in_window_usd', 'desc')">Sort by Most Expensive ($)</button>
          </div>
        </div>

        <div id="inst-window-summary" style="margin-bottom:10px; font-size:12.5px; color:var(--text-muted);"></div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th onclick="toggleInstanceSort('name')">Instance (X) ↕</th>
                <th onclick="toggleInstanceSort('status')">Status ↕</th>
                <th onclick="toggleInstanceSort('role')">Role ↕</th>
                <th onclick="toggleInstanceSort('display_location')">Location (Y) ↕</th>
                <th onclick="toggleInstanceSort('machine_type')">Specs &amp; Disk (V) ↕</th>
                <th onclick="toggleInstanceSort('benchmark_score')">CPU Bench ↕</th>
                <th onclick="toggleInstanceSort('runners_count')">Workload (M) ↕</th>
                <th onclick="toggleInstanceSort('runtime_hours')">Runtime ↕</th>
                <th onclick="toggleInstanceSort('active_hourly_usd')">Rate ($/hr) ↕</th>
                <th onclick="toggleInstanceSort('total_cost_in_window_usd')">Total Cost Paid ($YY.XX) ↕</th>
                <th onclick="toggleInstanceSort('started_epoch')">Started / Deleted (Local TZ) ↕</th>
                <th onclick="toggleInstanceSort('deletion_trigger')">Lifecycle / Deletion Trigger ↕</th>
                <th>Actions &amp; SSH</th>
              </tr>
            </thead>
            <tbody id="instances-table-body"></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- ===================================================================== -->
    <!-- TAB 3: SIZING, DISK & CPU BENCHMARK QUALIFICATION                     -->
    <!-- ===================================================================== -->
    <section id="tab-sizing" class="tab-pane">
      <div class="two-col">
        <!-- Left: CI/CD Runners Profile -->
        <div class="panel">
          <div class="panel-header">
            <div>
              <div class="panel-title">⚙️ CI/CD GitHub Runners Sizing &amp; Benchmark Requirements</div>
              <div class="panel-subtitle">Set per-runner vCPU &amp; RAM, minimum runner count, CPU benchmark floor, Hyperdisk vs Normal PD drives, VM template, and Cloud-Init template.</div>
            </div>
            <span class="badge badge-runner">CI/CD Runner Fleet</span>
          </div>

          <div class="form-grid">
            <div class="field">
              <label>Minimum # of GitHub Runners</label>
              <input type="number" id="runner-min-runners" min="1" max="200" value="4" oninput="previewQualification('cicd_runner')" />
            </div>
            <div class="field">
              <label>Per-Runner vCPU Requirement</label>
              <input type="number" id="runner-unit-vcpu" min="1" max="64" value="4" oninput="previewQualification('cicd_runner')" />
            </div>
            <div class="field">
              <label>Per-Runner RAM (GiB)</label>
              <input type="number" id="runner-unit-ram" min="1" max="256" value="16" oninput="previewQualification('cicd_runner')" />
            </div>
            <div class="field">
              <label>Min CPU Benchmark Score</label>
              <input type="number" id="runner-min-bench" min="100" max="6000" step="50" value="1000" oninput="previewQualification('cicd_runner')" />
            </div>
            <div class="field">
              <label>Min Host vCPU (e.g. 2 runners = 8)</label>
              <input type="number" id="runner-min-inst-vcpu" min="2" max="128" value="8" oninput="previewQualification('cicd_runner')" />
            </div>
            <div class="field">
              <label>Min Host RAM GiB (e.g. 2 runners = 32)</label>
              <input type="number" id="runner-min-inst-ram" min="4" max="512" value="32" oninput="previewQualification('cicd_runner')" />
            </div>
            <div class="field">
              <label>Drive Category (Hyperdrive vs Normal)</label>
              <select id="runner-disk-cat" onchange="previewQualification('cicd_runner')">
                <option value="hyperdrive">Hyperdrives Only (Hyperdisk NVMe)</option>
                <option value="normal">Normal Drives (PD-SSD / PD-Balanced)</option>
                <option value="any">Any Compatible Drive</option>
              </select>
            </div>
            <div class="field">
              <label>Preferred Data Disk Type</label>
              <select id="runner-disk-type" onchange="previewQualification('cicd_runner')">
                <option value="hyperdisk-balanced">hyperdisk-balanced (NVMe)</option>
                <option value="hyperdisk-throughput">hyperdisk-throughput</option>
                <option value="pd-ssd">pd-ssd</option>
                <option value="pd-balanced">pd-balanced</option>
                <option value="pd-standard">pd-standard</option>
              </select>
            </div>
            <div class="field">
              <label>Data Disk Size (GiB)</label>
              <input type="number" id="runner-disk-gb" min="40" max="4096" value="100" oninput="previewQualification('cicd_runner')" />
            </div>
            <div class="field">
              <label>Max Spot Budget ($/Hour / $/Day / $/Mo)</label>
              <div style="display:flex; gap:4px;">
                <input type="number" id="runner-max-hour" step="0.25" value="5.00" title="Max $/hr" style="width:33%;" oninput="previewQualification('cicd_runner')" />
                <input type="number" id="runner-max-day" step="0.50" value="10.00" title="Max $/day" style="width:33%;" oninput="previewQualification('cicd_runner')" />
                <input type="number" id="runner-max-month" step="5.0" value="50.00" title="Max $/mo" style="width:34%;" oninput="previewQualification('cicd_runner')" />
              </div>
            </div>
            <div class="field">
              <label>Cloud-Init Template Dropdown</label>
              <select id="runner-cloudinit-select"></select>
            </div>
            <div class="field">
              <label>VM Instance Template</label>
              <div style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" id="runner-use-vmtpl" checked title="Use VM Template" />
                <select id="runner-vmtpl-select" style="flex:1;"></select>
              </div>
            </div>
          </div>

          <div id="runner-sizing-math-banner" style="background:var(--bg-card); border:1px solid var(--border); padding:9px 12px; border-radius:8px; margin-bottom:12px; font-size:12px; color:#93c5fd;"></div>

          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn" onclick="saveProfile('cicd_runner')">Save CI/CD Runner Profile</button>
            <button class="btn btn-secondary" onclick="openSelectedCloudInit('cicd_runner')">Edit Selected Cloud-Init Template</button>
          </div>
        </div>

        <!-- Right: Rust Remote Builders Profile -->
        <div class="panel">
          <div class="panel-header">
            <div>
              <div class="panel-title">🦀 Rust Compilers / Remote Builders Sizing &amp; Benchmark</div>
              <div class="panel-subtitle">Independently configure minimum instances, CPU benchmark score, Hyperdisk vs Normal drives, VM template, and Cloud-Init for Rust builders.</div>
            </div>
            <span class="badge badge-builder">Rust Remote Builder</span>
          </div>

          <div class="form-grid">
            <div class="field">
              <label>Minimum # of Builder Instances</label>
              <input type="number" id="builder-min-instances" min="1" max="32" value="1" oninput="previewQualification('rust_builder')" />
            </div>
            <div class="field">
              <label>Builder vCPU Requirement</label>
              <input type="number" id="builder-unit-vcpu" min="4" max="64" value="16" oninput="previewQualification('rust_builder')" />
            </div>
            <div class="field">
              <label>Builder Container RAM (GiB)</label>
              <input type="number" id="builder-unit-ram" min="8" max="256" value="28" oninput="previewQualification('rust_builder')" />
            </div>
            <div class="field">
              <label>Min CPU Benchmark Score (Rust)</label>
              <input type="number" id="builder-min-bench" min="100" max="6000" step="50" value="1400" oninput="previewQualification('rust_builder')" />
            </div>
            <div class="field">
              <label>Min Host vCPU</label>
              <input type="number" id="builder-min-inst-vcpu" min="4" max="128" value="16" oninput="previewQualification('rust_builder')" />
            </div>
            <div class="field">
              <label>Min Host RAM (GiB)</label>
              <input type="number" id="builder-min-inst-ram" min="8" max="512" value="32" oninput="previewQualification('rust_builder')" />
            </div>
            <div class="field">
              <label>Drive Category (Hyperdrive vs Normal)</label>
              <select id="builder-disk-cat" onchange="previewQualification('rust_builder')">
                <option value="hyperdrive">Hyperdrives Only (Hyperdisk NVMe)</option>
                <option value="normal">Normal Drives (PD-SSD / PD-Balanced)</option>
                <option value="any">Any Compatible Drive</option>
              </select>
            </div>
            <div class="field">
              <label>Preferred Cache Disk Type</label>
              <select id="builder-disk-type" onchange="previewQualification('rust_builder')">
                <option value="hyperdisk-balanced">hyperdisk-balanced (NVMe)</option>
                <option value="hyperdisk-throughput">hyperdisk-throughput</option>
                <option value="pd-ssd">pd-ssd</option>
                <option value="pd-balanced">pd-balanced</option>
              </select>
            </div>
            <div class="field">
              <label>Cache Disk Size (GiB)</label>
              <input type="number" id="builder-disk-gb" min="40" max="4096" value="100" oninput="previewQualification('rust_builder')" />
            </div>
            <div class="field">
              <label>Max Spot Budget ($/Hour / $/Day / $/Mo)</label>
              <div style="display:flex; gap:4px;">
                <input type="number" id="builder-max-hour" step="0.25" value="3.50" style="width:33%;" oninput="previewQualification('rust_builder')" />
                <input type="number" id="builder-max-day" step="0.50" value="12.00" style="width:33%;" oninput="previewQualification('rust_builder')" />
                <input type="number" id="builder-max-month" step="5.0" value="80.00" style="width:34%;" oninput="previewQualification('rust_builder')" />
              </div>
            </div>
            <div class="field">
              <label>Cloud-Init Template Dropdown</label>
              <select id="builder-cloudinit-select"></select>
            </div>
            <div class="field">
              <label>VM Instance Template</label>
              <div style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" id="builder-use-vmtpl" checked />
                <select id="builder-vmtpl-select" style="flex:1;"></select>
              </div>
            </div>
          </div>

          <div id="builder-sizing-math-banner" style="background:var(--bg-card); border:1px solid var(--border); padding:9px 12px; border-radius:8px; margin-bottom:12px; font-size:12px; color:#c4b5fd;"></div>

          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn btn-purple" onclick="saveProfile('rust_builder')">Save Rust Builder Profile</button>
            <button class="btn btn-secondary" onclick="openSelectedCloudInit('rust_builder')">Edit Selected Cloud-Init Template</button>
          </div>
        </div>
      </div>

      <!-- Qualification Results Panel -->
      <div class="panel">
        <div class="panel-header">
          <div>
            <div class="panel-title">🔍 Qualifying Instances Matrix (Hardware + Benchmark vs. Current Spot Price)</div>
            <div class="panel-subtitle" id="qual-summary-subtitle">Showing instances that meet vCPU, RAM, CPU benchmark score, and enabled region filters, and highlighting which currently qualify under Spot pricing.</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button class="btn btn-sm" id="qual-role-btn-runner" onclick="switchQualRole('cicd_runner')">CI/CD Runner Candidates</button>
            <button class="btn btn-sm btn-secondary" id="qual-role-btn-builder" onclick="switchQualRole('rust_builder')">Rust Builder Candidates</button>
            <label class="toggle-label">
              <input type="checkbox" id="qual-only-spot-affordable" onchange="renderQualificationTable()" />
              Show Only Spot-Price-Qualifying
            </label>
          </div>
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Machine Shape</th>
                <th>Region / Zone</th>
                <th>CPU Platform</th>
                <th>CPU Benchmark</th>
                <th>Host vCPU / RAM</th>
                <th>Runners / Host</th>
                <th>Hosts for Floor</th>
                <th>Disk Type</th>
                <th>Compute ($/hr · $/day)</th>
                <th>All-In w/ Disk ($/hr · $/day · $/mo)</th>
                <th>Spot Price Qualifies?</th>
              </tr>
            </thead>
            <tbody id="qualification-table-body"></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- ===================================================================== -->
    <!-- TAB 4: SCALING RULES, HOURLY DEMAND & OOM DEEP-LINKS                  -->
    <!-- ===================================================================== -->
    <section id="tab-scaling" class="tab-pane">
      <div class="two-col">
        <!-- Left: Scale-Up & Scale-Down Policy Configuration -->
        <div class="panel">
          <div class="panel-header">
            <div>
              <div class="panel-title">📈 Autoscaling Triggers, Hourly Demand &amp; Budget Caps</div>
              <div class="panel-subtitle">Configure how CI/CD runners scale up and scale down based on queued jobs, active provisioned jobs, hourly run volume, CPU utilization across all/majority of runners, and OOM events.</div>
            </div>
          </div>

          <div class="form-grid">
            <div class="field">
              <label>Minimum CI/CD Runners</label>
              <input type="number" id="sc-min-runners" min="1" max="100" value="4" />
            </div>
            <div class="field">
              <label>Maximum CI/CD Runners</label>
              <input type="number" id="sc-max-runners" min="1" max="200" value="24" />
            </div>
            <div class="field">
              <label>Scale-Up Queued Jobs Waiting (&gt;=)</label>
              <input type="number" id="sc-queued-thresh" min="1" max="100" value="2" />
            </div>
            <div class="field">
              <label>Scale-Up Provisioned Jobs (&gt;=)</label>
              <input type="number" id="sc-prov-thresh" min="1" max="100" value="6" />
            </div>
            <div class="field">
              <label>Scale-Up Runs in Past Hour (&gt;=)</label>
              <input type="number" id="sc-hourly-up-thresh" min="1" max="500" value="20" />
            </div>
            <div class="field">
              <label>CPU Scale-Up Threshold (%)</label>
              <input type="number" id="sc-cpu-thresh" min="10" max="99" value="50" />
            </div>
            <div class="field">
              <label>CPU Trigger Fleet Scope</label>
              <select id="sc-cpu-scope">
                <option value="majority">Majority of Runners/Instances (&gt;50%)</option>
                <option value="all">ALL Runners/Instances (100%)</option>
                <option value="any">Any Single Instance</option>
              </select>
            </div>
            <div class="field">
              <label>Scale-Down If Runs in Past Hour (&lt;=)</label>
              <input type="number" id="sc-hourly-down-max" min="0" max="100" value="5" />
            </div>
            <div class="field">
              <label>Scale-Down Target CI/CD Runners</label>
              <input type="number" id="sc-hourly-down-target" min="1" max="50" value="2" />
            </div>
            <div class="field">
              <label>Max Scale-Up Budget ($/hr · $/day · $/mo)</label>
              <div style="display:flex; gap:4px;">
                <input type="number" id="sc-budget-hr" step="0.25" value="5.00" style="width:33%;" />
                <input type="number" id="sc-budget-day" step="0.50" value="10.00" style="width:33%;" />
                <input type="number" id="sc-budget-mo" step="5.00" value="50.00" style="width:34%;" />
              </div>
            </div>
          </div>

          <div style="display:flex; gap:14px; flex-wrap:wrap; margin: 10px 0 14px;">
            <label class="toggle-label"><input type="checkbox" id="sc-en-queued" checked /> Queued Jobs Trigger</label>
            <label class="toggle-label"><input type="checkbox" id="sc-en-prov" checked /> Provisioned Jobs Trigger</label>
            <label class="toggle-label"><input type="checkbox" id="sc-en-hourly-up" checked /> Past-Hour Runs Scale-Up</label>
            <label class="toggle-label"><input type="checkbox" id="sc-en-hourly-down" checked /> Past-Hour Low Runs Scale-Down (e.g. &lt;=5 jobs -&gt; 2 runners)</label>
            <label class="toggle-label"><input type="checkbox" id="sc-en-cpu" checked /> CPU &gt;50% (All/Majority) Trigger</label>
            <label class="toggle-label"><input type="checkbox" id="sc-en-journal-oom" checked /> VM Journal OOM Trigger</label>
            <label class="toggle-label"><input type="checkbox" id="sc-en-gh-oom" checked /> GitHub CI OOM Run Trigger</label>
          </div>

          <!-- Live Telemetry Simulator Bar -->
          <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:9px; padding:11px 13px; margin-bottom:12px;">
            <div style="font-weight:700; font-size:12px; margin-bottom:8px; color:#93c5fd;">Live Telemetry &amp; Scenario Simulator (Test Scale-Up or Scale-Down Live)</div>
            <div class="form-grid" style="margin-bottom:8px;">
              <div class="field">
                <label>Jobs Ran in Past Hour</label>
                <input type="number" id="sim-jobs-hour" min="0" max="500" value="14" />
              </div>
              <div class="field">
                <label>Currently Provisioned Jobs</label>
                <input type="number" id="sim-jobs-prov" min="0" max="100" value="7" />
              </div>
              <div class="field">
                <label>Queued Jobs Waiting</label>
                <input type="number" id="sim-jobs-queued" min="0" max="100" value="3" />
              </div>
            </div>
            <div style="display:flex; gap:8px; flex-wrap:wrap;">
              <button class="btn btn-sm" onclick="saveAndEvaluateScaling()">Save Policy &amp; Evaluate Scaling</button>
              <button class="btn btn-sm btn-secondary" onclick="simulateLowDemandScaleDown()">Simulate &lt;=5 Jobs/hr Scale-Down (5 jobs -&gt; 2 runners)</button>
            </div>
          </div>

          <div id="scaling-decision-box" style="background:#0c1424; border:1px solid var(--border); border-radius:9px; padding:12px; margin-bottom:14px;"></div>

          <!-- Rust Remote Builders Autoscaling & Idle Scale-Down Policy -->
          <div style="border-top:1px solid var(--border); padding-top:14px; margin-top:6px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
              <div>
                <div style="font-weight:700; font-size:14px; color:#c4b5fd;">🦀 Rust Remote Builders Autoscaling &amp; Idle Scale-Down</div>
                <div style="font-size:12px; color:var(--text-muted);">Configure dedicated Rust builder VM floor/ceiling, queue depth, CPU utilization, OOM triggers, and idle build scale-down.</div>
              </div>
              <span class="badge badge-builder">rust_builder</span>
            </div>
            <div class="form-grid">
              <div class="field">
                <label>Min Builder Instances</label>
                <input type="number" id="sc-builder-min-inst" min="1" max="32" value="1" />
              </div>
              <div class="field">
                <label>Max Builder Instances</label>
                <input type="number" id="sc-builder-max-inst" min="1" max="64" value="6" />
              </div>
              <div class="field">
                <label>Scale-Up Queued Builds (&gt;=)</label>
                <input type="number" id="sc-builder-queued-thresh" min="1" max="50" value="2" />
              </div>
              <div class="field">
                <label>CPU Scale-Up Threshold (%)</label>
                <input type="number" id="sc-builder-cpu-thresh" min="10" max="99" value="60" />
              </div>
              <div class="field">
                <label>Scale-Down If Builds/1h (&lt;=)</label>
                <input type="number" id="sc-builder-down-max" min="0" max="100" value="2" />
              </div>
              <div class="field">
                <label>Scale-Down Target Builders</label>
                <input type="number" id="sc-builder-down-target" min="1" max="32" value="1" />
              </div>
              <div class="field">
                <label>Max Builder Scale-Up Budget ($/hr · $/day · $/mo)</label>
                <div style="display:flex; gap:4px;">
                  <input type="number" id="sc-builder-budget-hr" step="0.25" value="3.50" style="width:33%;" />
                  <input type="number" id="sc-builder-budget-day" step="0.50" value="12.00" style="width:33%;" />
                  <input type="number" id="sc-builder-budget-mo" step="5.00" value="80.00" style="width:34%;" />
                </div>
              </div>
              <div class="field">
                <label>Live Builds (Past 1h / Active / Queued)</label>
                <div style="display:flex; gap:4px;">
                  <input type="number" id="sim-builds-hour" min="0" max="500" value="6" title="Builds ran in past hour" style="width:33%;" />
                  <input type="number" id="sim-builds-active" min="0" max="100" value="1" title="Active builds" style="width:33%;" />
                  <input type="number" id="sim-builds-queued" min="0" max="100" value="0" title="Queued builds" style="width:34%;" />
                </div>
              </div>
            </div>
            <div style="display:flex; gap:14px; flex-wrap:wrap; margin: 10px 0;">
              <label class="toggle-label"><input type="checkbox" id="sc-builder-en-queue" checked /> Queued Builds Trigger</label>
              <label class="toggle-label"><input type="checkbox" id="sc-builder-en-cpu" checked /> Builder CPU Trigger</label>
              <label class="toggle-label"><input type="checkbox" id="sc-builder-en-oom" checked /> Builder Journal OOM Trigger</label>
              <label class="toggle-label"><input type="checkbox" id="sc-builder-en-down" checked /> Idle Builds Scale-Down</label>
            </div>
            <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
              <button class="btn btn-sm btn-purple" onclick="saveAndEvaluateBuilderScaling()">Save &amp; Evaluate Rust Builder Scaling</button>
            </div>
            <div id="builder-scaling-decision-box" style="background:#0c1424; border:1px solid var(--border); border-radius:9px; padding:12px;"></div>
          </div>
        </div>

        <!-- Right: GitHub Actions CI OOM Deep-Links & VM Journal OOM Feed -->
        <div class="panel">
          <div class="panel-header">
            <div>
              <div class="panel-title">🚨 OOM Scaling Triggers: GitHub CI Exact Step/Line Deep-Links &amp; VM Journal</div>
              <div class="panel-subtitle">Click any failing GitHub Actions CI run below to jump directly to the exact build step and log line where the OOM occurred.</div>
            </div>
          </div>

          <div style="font-weight:700; font-size:12.5px; margin-bottom:8px; color:#fda4af;">GitHub Actions CI Runs Dying from OOM (Exact Step &amp; Line Deep-Links)</div>
          <div id="github-oom-runs-list" style="display:flex; flex-direction:column; gap:10px; margin-bottom:16px;"></div>

          <div style="font-weight:700; font-size:12.5px; margin-bottom:8px; color:#fcd34d;">VM Instance Journal (<code>journalctl</code>) Kernel / Cgroup OOM Events</div>
          <div id="journal-oom-events-list" style="display:flex; flex-direction:column; gap:10px; margin-bottom:16px;"></div>

          <!-- Add / Test Custom GitHub OOM Run Deep-Link -->
          <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:9px; padding:11px 13px;">
            <div style="font-weight:700; font-size:12px; margin-bottom:8px;">Record / Test GitHub Actions OOM Deep-Link</div>
            <div class="form-grid" style="margin-bottom:8px;">
              <div class="field">
                <label>Repository</label>
                <input type="text" id="new-oom-repo" placeholder="owner/repo" />
              </div>
              <div class="field">
                <label>Run ID</label>
                <input type="number" id="new-oom-runid" min="1" />
              </div>
              <div class="field">
                <label>Job ID</label>
                <input type="number" id="new-oom-jobid" min="1" />
              </div>
              <div class="field">
                <label>Step # : Line #</label>
                <div style="display:flex; gap:4px;">
                  <input type="number" id="new-oom-step" min="1" placeholder="step" style="width:50%;" />
                  <input type="number" id="new-oom-line" min="1" placeholder="line" style="width:50%;" />
                </div>
              </div>
              <div class="field">
                <label>Runner Name</label>
                <input type="text" id="new-oom-runner" placeholder="as shown in the job log (optional)" />
              </div>
              <div class="field">
                <label>Failing Log Line</label>
                <input type="text" id="new-oom-excerpt" placeholder="copied from the job log" />
              </div>
            </div>
            <button class="btn btn-sm btn-danger" onclick="addGithubOomRun()">Add OOM CI Run Deep-Link</button>
          </div>
        </div>
      </div>
    </section>

    <!-- ===================================================================== -->
    <!-- TAB 5: CLOUD-INIT & VM INITIALIZATION STUDIO                          -->
    <!-- ===================================================================== -->
    <section id="tab-cloudinit" class="tab-pane">
      <div class="two-col">
        <!-- Left: Cloud-Init YAML Editor & Save / Save As -->
        <div class="panel">
          <div class="panel-header">
            <div>
              <div class="panel-title">📜 Cloud-Init &amp; VM Initialization Template Editor</div>
              <div class="panel-subtitle">Select, edit, validate, and save <code>#cloud-config</code> templates for GitHub Actions runners or Rust compilers. Use <strong>Save As New Template</strong> to create a new selectable template.</div>
            </div>
          </div>

          <div class="form-grid">
            <div class="field">
              <label>Select Cloud-Init Template</label>
              <select id="ci-editor-select" onchange="loadSelectedCloudInitIntoEditor()"></select>
            </div>
            <div class="field">
              <label>Target Workload Role</label>
              <select id="ci-editor-role">
                <option value="cicd_runner">CI/CD GitHub Runner</option>
                <option value="rust_builder">Rust Remote Builder</option>
              </select>
            </div>
            <div class="field">
              <label>Template Name (for Save / Save As)</label>
              <input type="text" id="ci-editor-name" placeholder="e.g. Custom Wget Docker Runner" />
            </div>
          </div>

          <div class="field" style="margin-bottom:12px;">
            <label>Cloud-Init Configuration (<code>#cloud-config</code> YAML / Startup Script)</label>
            <textarea id="ci-editor-textarea" rows="22" class="mono" style="font-size:12px; line-height:1.45; width:100%;" oninput="liveParseCloudInit()"></textarea>
          </div>

          <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
            <button class="btn" onclick="saveCloudInit('save')">💾 Save Current Template</button>
            <button class="btn btn-emerald" onclick="saveCloudInit('save_as')">✨ Save As New Template &amp; Assign</button>
            <label class="toggle-label">
              <input type="checkbox" id="ci-assign-active" checked />
              Set as Active Template for Role
            </label>
          </div>
        </div>

        <!-- Right: Visual Provisioning Pipeline & VM Template Library -->
        <div>
          <div class="panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">🔍 Live Cloud-Init Provisioning Pipeline Visualizer</div>
                <div class="panel-subtitle">Visual breakdown of users, groups, packages, environment files, and boot <code>runcmd</code> execution steps.</div>
              </div>
              <span id="ci-valid-badge" class="badge badge-running">Valid #cloud-config</span>
            </div>
            <div id="ci-warnings-box" style="margin-bottom:10px;"></div>
            <div id="ci-visualizer-stages" class="stage-list"></div>
          </div>

          <div class="panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">🖥️ Saved VM Instance Templates</div>
                <div class="panel-subtitle">Reusable VM hardware + disk + Cloud-Init templates created from running instances.</div>
              </div>
            </div>
            <div style="display:flex; gap:8px; margin-bottom:12px; flex-wrap:wrap;">
              <select id="vmtpl-source-instance" style="flex:1;"></select>
              <input type="text" id="vmtpl-new-name" placeholder="New VM template name..." style="flex:1;" />
              <button class="btn btn-sm btn-purple" onclick="createVmTemplateFromSelected()">+ Create VM Template from Instance</button>
            </div>
            <div id="vm-templates-list" style="display:flex; flex-direction:column; gap:8px;"></div>
          </div>
        </div>
      </div>
    </section>

    <!-- ===================================================================== -->
    <!-- TAB 6: WORLD REGIONS & DATA CENTER LATENCY / CAPABILITY FILTER        -->
    <!-- ===================================================================== -->
    <section id="tab-regions" class="tab-pane">
      <div class="panel">
        <div class="panel-header">
          <div>
            <div class="panel-title">🌐 World Deployment Regions &amp; Data Center Filter</div>
            <div class="panel-subtitle">Deselect or filter areas of the world based on web-backend-to-datacenter latency (ms), geographic continent, offered instance families, CPU platforms, and disk types.</div>
          </div>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn btn-sm btn-secondary" onclick="probeRegionLatencies()">⚡ Probe Live Backend-to-Region Latency</button>
            <button class="btn btn-sm btn-danger" onclick="deselectHighLatencyRegions()">Deselect Regions Above Max Latency</button>
            <button class="btn btn-sm btn-emerald" onclick="setAllFilteredRegions(true)">Enable All Shown</button>
            <button class="btn btn-sm btn-secondary" onclick="setAllFilteredRegions(false)">Deselect All Shown</button>
          </div>
        </div>

        <div class="form-grid" style="background:var(--bg-card); padding:12px; border-radius:9px; border:1px solid var(--border); margin-bottom:14px;">
          <div class="field">
            <label>Max Allowed Latency (ms): <span id="reg-lat-val" class="mono">220 ms</span></label>
            <input type="range" id="reg-max-latency" min="20" max="250" value="220" oninput="document.getElementById('reg-lat-val').textContent=this.value+' ms'; renderRegionsTable();" />
          </div>
          <div class="field">
            <label>Continent / World Area</label>
            <select id="reg-continent-filter" onchange="renderRegionsTable()">
              <option value="">All World Areas</option>
              <option value="North America">North America</option>
              <option value="South America">South America</option>
              <option value="Europe">Europe</option>
              <option value="Africa">Africa (South Africa)</option>
              <option value="Middle East">Middle East</option>
              <option value="Asia Pacific">Asia Pacific</option>
            </select>
          </div>
          <div class="field">
            <label>Required Instance Family</label>
            <select id="reg-family-filter" onchange="renderRegionsTable()">
              <option value="">Any Instance Family</option>
              <option value="c4d">c4d (AMD Turin Hyperdisk)</option>
              <option value="n4d">n4d (AMD Turin)</option>
              <option value="c4">c4 (Intel Granite/Emerald)</option>
              <option value="n4">n4 (Intel Emerald Rapids)</option>
              <option value="c2d">c2d (AMD Milan)</option>
              <option value="n2d">n2d (AMD Milan)</option>
              <option value="n2">n2 (Intel Ice Lake)</option>
              <option value="e2">e2 (Cost-Optimized)</option>
            </select>
          </div>
          <div class="field">
            <label>Required CPU Platform</label>
            <select id="reg-cpu-filter" onchange="renderRegionsTable()">
              <option value="">Any CPU Platform</option>
              <option value="AMD Turin">AMD Turin</option>
              <option value="Intel Granite Rapids">Intel Granite Rapids</option>
              <option value="Intel Emerald Rapids">Intel Emerald Rapids</option>
              <option value="AMD Milan">AMD Milan</option>
              <option value="Intel Ice Lake">Intel Ice Lake</option>
              <option value="Intel Cascade Lake">Intel Cascade Lake</option>
            </select>
          </div>
          <div class="field">
            <label>Required Disk Type</label>
            <select id="reg-disk-filter" onchange="renderRegionsTable()">
              <option value="">Any Disk Type</option>
              <option value="hyperdisk-balanced">hyperdisk-balanced</option>
              <option value="hyperdisk-throughput">hyperdisk-throughput</option>
              <option value="pd-ssd">pd-ssd</option>
              <option value="pd-balanced">pd-balanced</option>
            </select>
          </div>
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Deploy Enabled</th>
                <th>Region &amp; Zones</th>
                <th>Location / Continent</th>
                <th>Backend Latency</th>
                <th>Offered Machine Series</th>
                <th>Offered CPU Platforms</th>
                <th>Supported Disk Types</th>
                <th>Active VMs</th>
              </tr>
            </thead>
            <tbody id="regions-table-body"></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- ===================================================================== -->
    <!-- TAB 7: RUST REMOTE BUILDERS TELEMETRY, GEOLOCATION & VPN/TOR LOOKUP   -->
    <!-- ===================================================================== -->
    <section id="tab-rust" class="tab-pane">
      <div class="kpi-grid" id="rust-kpi-grid"></div>

      <div class="panel">
        <div class="panel-header">
          <div>
            <div class="panel-title">🦀 Rust Remote Builder Executions, Origin IP Geolocation, VPN/Tor Lookup &amp; CPU Points</div>
            <div class="panel-subtitle">Tracks build requested/finished/delivered timestamps (in browser timezone), crate cache hits, builder instance, origin IP &amp; hostname, geolocation, ProtonVPN / Tor / Mullvad exit-node detection, peak active RAM (OOM floor), CPU time, and <strong>CPU Points = CPU Time × Instance Benchmark Score</strong>.</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <input type="text" id="rust-search-input" placeholder="Filter by IP, ProtonVPN, Tor, instance, crate..." style="width:250px;" oninput="renderRustBuildsTable()" />
          </div>
        </div>

        <div class="table-wrap" style="margin-bottom:16px;">
          <table>
            <thead>
              <tr>
                <th>Build ID / Target</th>
                <th>Requested / Finished / Delivered (Local TZ)</th>
                <th>Total Duration</th>
                <th>Cache Hits</th>
                <th>Instance Ran On &amp; Bench</th>
                <th>Origin IP &amp; Hostname</th>
                <th>IP Geolocation</th>
                <th>VPN / Proxy / Tor Lookup</th>
                <th>Peak Active RAM (OOM Floor)</th>
                <th>CPU Time</th>
                <th>CPU Points (Time × Bench)</th>
              </tr>
            </thead>
            <tbody id="rust-builds-table-body"></tbody>
          </table>
        </div>

        <!-- Record / Test New Rust Build Telemetry & IP VPN/Tor Lookup -->
        <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:10px; padding:14px;">
          <div style="font-weight:700; font-size:13px; margin-bottom:10px; color:#c4b5fd;">🧪 Record / Simulate New Rust Remote Build (With Live IP Geolocation &amp; ProtonVPN / Tor Lookup)</div>
          <div class="form-grid">
            <div class="field">
              <label>Project / Crate</label>
              <input type="text" id="rb-new-project" value="oxidex" />
            </div>
            <div class="field">
              <label>Origin IP Preset or Custom</label>
              <select id="rb-new-ip" onchange="document.getElementById('rb-custom-ip').value=this.value">
                <option value="185.159.157.88">185.159.157.88 (ProtonVPN Exit Node — Zurich)</option>
                <option value="185.220.101.55">185.220.101.55 (Tor Exit Node — Germany)</option>
                <option value="193.138.218.40">193.138.218.40 (Mullvad VPN — Sweden)</option>
                <option value="73.162.44.19">73.162.44.19 (Direct Workstation — Austin, TX)</option>
              </select>
            </div>
            <div class="field">
              <label>Origin IP Address</label>
              <input type="text" id="rb-custom-ip" value="185.159.157.88" />
            </div>
            <div class="field">
              <label>Builder Instance</label>
              <select id="rb-new-instance">
                <option value="builder-oxidex-turin-01">builder-oxidex-turin-01 (c4d-highcpu-16 · Bench 2485)</option>
                <option value="builder-oxidex-eu-01">builder-oxidex-eu-01 (n4-highcpu-16 · Bench 2215)</option>
              </select>
            </div>
            <div class="field">
              <label>Cache Hits / Total Crates</label>
              <div style="display:flex; gap:4px;">
                <input type="number" id="rb-new-hits" value="418" style="width:50%;" />
                <input type="number" id="rb-new-total" value="430" style="width:50%;" />
              </div>
            </div>
            <div class="field">
              <label>Peak Active RAM (GiB)</label>
              <input type="number" id="rb-new-ram" step="0.1" value="22.4" />
            </div>
            <div class="field">
              <label>CPU Time (Seconds)</label>
              <input type="number" id="rb-new-cputime" step="10" value="960" />
            </div>
          </div>
          <button class="btn btn-purple" onclick="submitNewRustBuild()">+ Record Rust Build &amp; Compute CPU Points</button>
        </div>
      </div>
    </section>

    <section id="tab-patches" class="tab-pane">
      <div class="panel">
        <div class="panel-header">
          <div>
            <div class="panel-title">🧩 Runner Patches (spot-runner-patch-v1)</div>
            <div class="panel-subtitle">Per-organization <code class="mono">defaults.yaml</code> and per-repository <code class="mono">repos/&lt;owner&gt;/&lt;repo&gt;.yaml</code> manifests from the config repository. Saving opens a pull request; it never writes to the default branch.</div>
          </div>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn btn-sm btn-secondary" id="rp-refresh">Load manifests</button>
          </div>
        </div>
        <p class="mono" id="rp-source" style="font-size:12px;"></p>
        <p id="rp-status" role="status" aria-live="polite" style="font-size:12.5px;"></p>
        <div style="display:grid; grid-template-columns:minmax(180px,240px) 1fr; gap:14px;">
          <ul id="rp-list" style="list-style:none; padding:0; margin:0;"></ul>
          <div>
            <div class="mono" id="rp-path" style="font-size:12.5px; margin-bottom:6px;"></div>
            <textarea id="rp-editor" rows="18" class="mono" style="font-size:12px; line-height:1.45; width:100%;" aria-label="Manifest editor"></textarea>
            <ul id="rp-errors" style="color:#fda4af; font-size:12.5px;"></ul>
            <div class="field">
              <label for="rp-message">Pull request title</label>
              <input type="text" id="rp-message" placeholder="Update runner patch" />
            </div>
            <div style="display:flex; gap:8px; flex-wrap:wrap; margin:8px 0;">
              <button class="btn btn-sm btn-secondary" id="rp-validate">Validate</button>
              <button class="btn btn-sm btn-secondary" id="rp-preview">Preview rollout impact</button>
              <button class="btn btn-sm btn-emerald" id="rp-save">Save as pull request</button>
            </div>
            <div class="mono" id="rp-effective-sha" style="font-size:12px;"></div>
            <pre class="mono" id="rp-effective" style="font-size:11.5px; max-height:220px; overflow:auto;"></pre>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Runner host</th><th>Zone</th><th>Current patch</th><th>Rollout impact</th><th>Detail</th></tr></thead>
                <tbody id="rp-hosts"></tbody>
              </table>
            </div>
            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:10px;">
              <label style="font-size:12.5px;"><input type="checkbox" id="rp-confirm" /> I confirm hot-patching the running hosts listed above</label>
              <button class="btn btn-sm btn-danger" id="rp-hotpatch" disabled>Hot-patch running fleet</button>
            </div>
            <p id="rp-executor" style="font-size:12px;"></p>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-admins" class="tab-pane" hidden>
      <div class="panel">
        <div class="panel-header">
          <div>
            <div class="panel-title">🔑 Dashboard administrators</div>
            <div class="panel-subtitle">Owners are set at deploy time and can't be removed here. Administrators you add can sign in with GitHub; they're matched by numeric GitHub user ID, so a renamed account keeps access. Removing one ends their sessions on their next request. Only owners can see or change this list.</div>
          </div>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn btn-sm btn-secondary" id="adm-refresh">Reload</button>
          </div>
        </div>
        <p id="adm-status" role="status" aria-live="polite" style="font-size:12.5px;"></p>
        <p id="adm-exempt" style="font-size:12.5px;"></p>
        <h3 style="font-size:14px; margin:12px 0 6px;">Owners</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>GitHub user</th><th>User ID</th><th>Per-target checks</th></tr></thead>
            <tbody id="adm-owners"></tbody>
          </table>
        </div>
        <h3 style="font-size:14px; margin:16px 0 6px;">Added administrators</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>GitHub user (when added)</th><th>User ID</th><th>Added by</th><th>Added at</th><th></th></tr></thead>
            <tbody id="adm-admins"></tbody>
          </table>
        </div>
        <form id="adm-add-form" style="display:flex; gap:8px; align-items:flex-end; flex-wrap:wrap; margin-top:12px;">
          <div class="field" style="margin:0;">
            <label for="adm-login">Add GitHub username</label>
            <input type="text" id="adm-login" autocomplete="off" spellcheck="false" maxlength="40" placeholder="octocat" />
          </div>
          <button type="submit" class="btn btn-sm btn-emerald" id="adm-add">Add administrator</button>
        </form>
        <h3 style="font-size:14px; margin:16px 0 6px;">Recent changes</h3>
        <ul id="adm-history" style="font-size:12.5px; padding-left:18px;"></ul>
      </div>
    </section>
  </main>

  <div id="toast-container"></div>

  <script>
    const BROWSER_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    let APP_STATE = null;
    let SELECTED_MAP_REGION = "africa-south1";
    let INST_WINDOW_HOURS = 24;
    let INST_SORT_BY = "total_cost_in_window_usd";
    let INST_SORT_DIR = "desc";
    let QUAL_ACTIVE_ROLE = "cicd_runner";
    let LAST_QUAL_DATA = { cicd_runner: null, rust_builder: null };
    const AUTH_STORAGE_KEY = "dashboard_auth_token";
    const NATIVE_FETCH = window.fetch.bind(window);

    function getDashboardAuthToken() {
      try {
        return sessionStorage.getItem(AUTH_STORAGE_KEY) || localStorage.getItem(AUTH_STORAGE_KEY) || "";
      } catch (_err) {
        return "";
      }
    }

    function storeDashboardAuthToken(token) {
      const clean = String(token || "").trim();
      try {
        if (clean) {
          sessionStorage.setItem(AUTH_STORAGE_KEY, clean);
        } else {
          sessionStorage.removeItem(AUTH_STORAGE_KEY);
          localStorage.removeItem(AUTH_STORAGE_KEY);
        }
      } catch (_err) {
        // Storage unavailable in restricted browsing contexts.
      }
      const btn = document.getElementById("top-auth-btn");
      if (btn) {
        btn.textContent = clean ? "🔒 Auth: Active" : "🔒 Auth Token";
      }
      return clean;
    }

    async function authenticateDashboardToken(token) {
      const clean = storeDashboardAuthToken(token);
      if (!clean) return false;
      const res = await NATIVE_FETCH("/api/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${clean}`,
          "X-Dashboard-Token": clean
        },
        body: JSON.stringify({ token: clean })
      });
      return res.ok;
    }

    async function logoutDashboardSession() {
      try {
        await NATIVE_FETCH("/api/auth/logout", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({})
        });
      } catch (_err) {
        // Ignore network errors during logout cleanup.
      }
    }

    async function configureDashboardAuthToken() {
      const current = getDashboardAuthToken();
      const entered = window.prompt(
        "Enter Dashboard Bearer / Session Token (leave blank to clear):",
        current
      );
      if (entered === null) return;
      const clean = storeDashboardAuthToken(entered);
      if (!clean) {
        await logoutDashboardSession();
        showToast("Cleared saved dashboard auth token");
        return;
      }
      const ok = await authenticateDashboardToken(clean);
      if (ok) {
        showToast("Authenticated dashboard session");
        await loadOverview();
      } else {
        showToast("Authentication failed: invalid dashboard token");
      }
    }

    (function bootstrapUrlAuthToken() {
      try {
        const url = new URL(window.location.href);
        const hadQueryToken = url.searchParams.has("token");
        if (hadQueryToken) {
          url.searchParams.delete("token");
        }
        const hashParams = new URLSearchParams((url.hash || "").replace(/^#/, ""));
        const hashToken = hashParams.get("token");
        if (hashToken) {
          storeDashboardAuthToken(hashToken);
          hashParams.delete("token");
          url.hash = hashParams.toString() ? `#${hashParams.toString()}` : "";
        }
        if (hadQueryToken || hashToken) {
          window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
        }
      } catch (_err) {
        // Ignore URL parsing errors in non-standard environments.
      }
    })();

    const CONTROL_VM_UNAVAILABLE = "control_vm_unavailable";
    const CONTROL_VM_POLL_MS = 5000;
    const CONTROL_VM_BANNER_TEXT = "The control VM is restarting or being replaced — waiting. Your change will be retried automatically.";
    let CONTROL_VM_WAIT = null;

    function newIdempotencyKey() {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
      }
      const bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }

    function setControlVmBanner(visible) {
      let banner = document.getElementById("control-vm-banner");
      if (!visible) {
        if (banner) banner.hidden = true;
        return;
      }
      if (!banner) {
        banner = document.createElement("div");
        banner.id = "control-vm-banner";
        banner.setAttribute("role", "status");
        banner.setAttribute("aria-live", "polite");
        banner.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:10000;padding:10px 16px;" +
          "background:#7c2d12;color:#fff7ed;font-weight:600;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.35);";
        banner.textContent = CONTROL_VM_BANNER_TEXT;
        (document.body || document.documentElement).appendChild(banner);
      }
      banner.hidden = false;
    }

    function waitForControlVm() {
      // One shared wait: every blocked request resumes when /api/health returns 200.
      if (!CONTROL_VM_WAIT) {
        setControlVmBanner(true);
        CONTROL_VM_WAIT = (async () => {
          for (;;) {
            await new Promise(resolve => setTimeout(resolve, CONTROL_VM_POLL_MS));
            try {
              const health = await NATIVE_FETCH("/api/health", { cache: "no-store", credentials: "same-origin" });
              if (health.status === 200) return;
            } catch (_err) {
              // Still unreachable; keep polling.
            }
          }
        })().finally(() => {
          CONTROL_VM_WAIT = null;
          setControlVmBanner(false);
        });
      }
      return CONTROL_VM_WAIT;
    }

    async function isControlVmUnavailable(res) {
      if (!res || res.status !== 503) return false;
      try {
        const data = await res.clone().json();
        return Boolean(data && data.error === CONTROL_VM_UNAVAILABLE);
      } catch (_err) {
        return false;
      }
    }

    async function oauthSignInNeeded() {
      // True when GitHub sign-in is enabled and this browser has no usable session: an expired session
      // (401) or one that a target-policy change no longer covers (403). Waits out a control VM outage.
      try {
        const res = await fetchWithControlVmRetry("/api/auth/status", { cache: "no-store", credentials: "same-origin" });
        const data = res.ok ? await res.json() : {};
        return Boolean(data && data.oauth && data.oauth.enabled && !data.authenticated);
      } catch (_err) {
        return false;
      }
    }

    async function externalChangePendingSeconds(res) {
      if (!res || res.status !== 202) return null;
      try {
        const data = await res.clone().json();
        if (!data || data.status !== "pending" || data.pending_retry !== true) return null;
        return Math.min(30, Math.max(1, Number(data.retry_after_seconds) || 3));
      } catch (_err) {
        return null;
      }
    }

    // Delays (seconds) between automatic retries of a change whose outcome GitHub did not confirm.
    const SAME_KEY_RETRY_DELAYS = [2, 5, 10];

    async function sameKeyRetryRequested(res) {
      if (!res || res.status !== 424) return false;
      try {
        const data = await res.clone().json();
        return Boolean(data && data.retry_same_key === true);
      } catch (_err) {
        return false;
      }
    }

    async function fetchWithControlVmRetry(input, reqInit) {
      // Retries the SAME request (same Idempotency-Key) after the control VM comes back.
      let sameKeyRetries = 0;
      for (;;) {
        let res;
        try {
          res = await NATIVE_FETCH(input, reqInit);
        } catch (err) {
          if (err instanceof TypeError) {
            await waitForControlVm();
            continue;
          }
          throw err;
        }
        if (await isControlVmUnavailable(res)) {
          await waitForControlVm();
          continue;
        }
        const pendingSeconds = await externalChangePendingSeconds(res);
        if (pendingSeconds !== null) {
          // A change with this Idempotency-Key is still running (for example the edge timed out a long save or
          // hot-patch): keep asking with the same key until the stored result comes back.
          await new Promise(resolve => setTimeout(resolve, pendingSeconds * 1000));
          continue;
        }
        if (sameKeyRetries < SAME_KEY_RETRY_DELAYS.length && reqInit && reqInit.headers &&
            reqInit.headers.has && reqInit.headers.has("Idempotency-Key") && await sameKeyRetryRequested(res)) {
          // GitHub did not confirm the outcome: the same key lets the dashboard reconcile instead of repeating.
          await new Promise(resolve => setTimeout(resolve, SAME_KEY_RETRY_DELAYS[sameKeyRetries] * 1000));
          sameKeyRetries += 1;
          continue;
        }
        return res;
      }
    }

    window.fetch = async function dashboardAuthorizedFetch(input, init = {}) {
      const method = String(init.method || (input && input.method) || "GET").toUpperCase();
      const reqInit = { ...init, method, credentials: init.credentials || "same-origin" };
      const headers = new Headers(init.headers || {});
      const token = getDashboardAuthToken();
      if (token && !headers.has("Authorization")) {
        headers.set("Authorization", `Bearer ${token}`);
      }
      if (token && !headers.has("X-Dashboard-Token")) {
        headers.set("X-Dashboard-Token", token);
      }
      if (method !== "GET" && method !== "HEAD" && !headers.has("Idempotency-Key")) {
        headers.set("Idempotency-Key", newIdempotencyKey());
      }
      reqInit.headers = headers;
      let res = await fetchWithControlVmRetry(input, reqInit);
      if (res.status === 401 && token && !new Headers(init.headers || {}).has("Authorization")) {
        // A stored bearer token is checked before the session cookie, so a stale one masks a valid
        // GitHub or token session: drop it and retry once (same Idempotency-Key) with the cookie alone.
        storeDashboardAuthToken("");
        const cookieOnly = new Headers(headers);
        cookieOnly.delete("Authorization");
        cookieOnly.delete("X-Dashboard-Token");
        res = await fetchWithControlVmRetry(input, { ...reqInit, headers: cookieOnly });
      }
      if ((res.status === 401 || res.status === 403) && await oauthSignInNeeded()) {
        // The login gate at "/" offers every sign-in method (including the dashboard token).
        window.location.assign("/");
        return res;
      }
      if (res.status === 401) {
        if (typeof window.prompt === "function") {
          const prompted = window.prompt("Dashboard authentication required. Enter your dashboard token:");
          if (prompted && prompted.trim()) {
            const ok = await authenticateDashboardToken(prompted.trim());
            if (ok) {
              const retryHeaders = new Headers(headers);
              const fresh = getDashboardAuthToken();
              retryHeaders.set("Authorization", `Bearer ${fresh}`);
              retryHeaders.set("X-Dashboard-Token", fresh);
              res = await fetchWithControlVmRetry(input, { ...reqInit, headers: retryHeaders });
            }
          }
        }
      }
      return res;
    };

    function showToast(msg) {
      const box = document.getElementById("toast-container");
      const el = document.createElement("div");
      el.className = "toast";
      el.textContent = msg;
      box.appendChild(el);
      setTimeout(() => el.remove(), 3400);
    }

    function esc(val) {
      return String(val ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function formatLocalTime(isoOrEpoch) {
      if (!isoOrEpoch) return "—";
      const d = typeof isoOrEpoch === "number" ? new Date(isoOrEpoch * 1000) : new Date(isoOrEpoch);
      if (isNaN(d.getTime())) return String(isoOrEpoch);
      return d.toLocaleString(undefined, {
        timeZone: BROWSER_TZ,
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false
      });
    }

    async function copyTextToClipboard(text, label = "SSH command") {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement("textarea");
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          ta.remove();
        }
        showToast(`✓ Copied ${label} to paste buffer: ${text}`);
      } catch (err) {
        showToast(`Copied: ${text}`);
      }
    }

    function switchTab(tabId) {
      document.querySelectorAll(".tab-pane").forEach(el => el.classList.remove("active"));
      document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-tab") === tabId);
      });
      const target = document.getElementById(tabId);
      if (target) target.classList.add("active");
    }

    async function loadOverview() {
      const showDeleted = document.getElementById("inst-show-deleted").checked;
      const inclDiskTax = document.getElementById("inst-include-disk-tax").checked;
      const search = encodeURIComponent(document.getElementById("inst-search-input").value || "");
      const role = encodeURIComponent(document.getElementById("inst-role-filter").value || "all");
      const url = `/api/overview?include_deleted=${showDeleted}&window_hours=${INST_WINDOW_HOURS}&include_disk_and_tax=${inclDiskTax}&sort_by=${INST_SORT_BY}&sort_dir=${INST_SORT_DIR}&search=${search}&role=${role}`;
      const res = await fetch(url);
      APP_STATE = await res.json();
      LAST_QUAL_DATA.cicd_runner = APP_STATE.qualifications.cicd_runner;
      LAST_QUAL_DATA.rust_builder = APP_STATE.qualifications.rust_builder;
      renderAll();
    }

    function renderAll() {
      if (!APP_STATE) return;
      const hdrProjOrg = document.getElementById("hdr-project-org");
      if (hdrProjOrg && APP_STATE.project && APP_STATE.org) {
        hdrProjOrg.textContent = `GCP Project: ${APP_STATE.project} · GitHub Org: ${APP_STATE.org}`;
      }
      document.getElementById("browser-tz-pill").textContent = `🕒 Browser TZ: ${BROWSER_TZ}`;
      document.getElementById("instances-tz-label").textContent = BROWSER_TZ;

      // Top bar budget
      const gb = APP_STATE.global_budget;
      document.getElementById("top-budget-hour").value = Number(gb.max_hourly_usd).toFixed(2);
      document.getElementById("top-budget-day").value = Number(gb.max_daily_usd).toFixed(2);
      document.getElementById("top-budget-month").value = Number(gb.max_monthly_usd).toFixed(2);
      document.getElementById("top-budget-disk").checked = Boolean(gb.include_disk_costs);

      // KPIs (fleet-wide running totals independent of table filters)
      const iv = APP_STATE.instances_view;
      const fs = APP_STATE.fleet_summary || iv;
      document.getElementById("kpi-burn-rate").textContent = `$${fs.active_hourly_burn_usd.toFixed(2)}/hr · $${fs.active_daily_burn_usd.toFixed(2)}/day`;
      document.getElementById("kpi-burn-sub").textContent = `Projected monthly: $${fs.active_monthly_burn_usd.toFixed(2)}/mo · Window spend (${iv.window_hours}h): $${iv.total_window_spend_usd.toFixed(2)}`;

      const allRunning = APP_STATE.regions
        ? APP_STATE.regions.flatMap(r => r.running_instances || [])
        : APP_STATE.instances_view.instances.filter(i => i.status === "RUNNING");
      const runningRunners = allRunning.filter(i => i.role === "cicd_runner");
      const totalRunnerSlots = fs.running_runner_slots !== undefined
        ? fs.running_runner_slots
        : runningRunners.reduce((acc, i) => acc + i.runners_count, 0);
      const runningRunnerVms = fs.running_runner_vms !== undefined ? fs.running_runner_vms : runningRunners.length;
      const unconfirmed = fs.unconfirmed_runner_hosts || 0;
      document.getElementById("kpi-runners-online").textContent = `${totalRunnerSlots} Runners (${runningRunnerVms} VMs)${unconfirmed ? ` · ${unconfirmed} host(s) not confirmed` : ""}`;
      const st = APP_STATE.scaling_telemetry;
      document.getElementById("kpi-runners-sub").textContent = `Min floor: ${APP_STATE.profiles.cicd_runner.min_runners} · ${st.queued_jobs} queued · ${st.jobs_ran_past_hour} runs in past 1h`;

      const runningBuilders = allRunning.filter(i => i.role === "rust_builder");
      const runningBuilderVms = fs.running_builder_vms !== undefined ? fs.running_builder_vms : runningBuilders.length;
      document.getElementById("kpi-builders-online").textContent = `${runningBuilderVms} Builders Online`;
      document.getElementById("kpi-builders-sub").textContent = `Min benchmark: ${APP_STATE.profiles.rust_builder.min_benchmark_score} · Peak RAM floor: ${APP_STATE.rust_builds.max_peak_ram_gib} GiB`;

      const cq = APP_STATE.qualifications.cicd_runner;
      document.getElementById("kpi-qualifying-offers").textContent = `${cq.spot_price_qualifying_count} Spot-Qualifying`;
      document.getElementById("kpi-qualifying-sub").textContent = `${cq.hardware_qualifying_count} hardware/benchmark match (>=${cq.requirements.min_benchmark_score} pts)`;

      const se = APP_STATE.scaling_evaluation;
      const statusLabel = se.direction === "scale_up" ? `SCALE UP (${se.current_runners} → ${se.desired_runners})` : (se.direction === "scale_down" ? `SCALE DOWN (${se.current_runners} → ${se.desired_runners})` : `HOLD (${se.current_runners} runners)`);
      document.getElementById("kpi-scaling-status").textContent = statusLabel;
      document.getElementById("kpi-scaling-sub").textContent = (se.scale_up_reasons[0]?.summary || se.scale_down_reasons[0]?.summary || "Fleet within target bounds");

      // Badges
      const totalRunning = APP_STATE.regions.reduce((s, r) => s + r.running_instances.length, 0);
      document.getElementById("badge-map-count").textContent = `${totalRunning} live`;
      document.getElementById("badge-instances-count").textContent = `${iv.instances.length}`;
      document.getElementById("badge-qual-count").textContent = `${cq.spot_price_qualifying_count} offers`;
      const confirmedOoms = st.github_oom_runs.filter(r => r.is_oom !== false).length;
      document.getElementById("badge-oom-count").textContent = `${confirmedOoms} OOM`;
      document.getElementById("badge-tpl-count").textContent = `${APP_STATE.cloud_init_templates.length}`;
      const enabledRegs = APP_STATE.regions.filter(r => r.enabled).length;
      document.getElementById("badge-reg-count").textContent = `${enabledRegs}/${APP_STATE.regions.length}`;
      document.getElementById("badge-rust-count").textContent = `${APP_STATE.rust_builds.total_builds} builds`;

      renderWorldMapPins();
      renderMapInspector();
      renderInstancesTableRows();
      populateSizingForms();
      renderQualificationTable();
      populateScalingView();
      populateCloudInitStudio();
      renderRegionsTable();
      renderRustBuildsView();
    }

    // =========================================================================
    // WORLD MAP & DATA CENTER INSPECTOR
    // =========================================================================
    function renderWorldMapPins() {
      const g = document.getElementById("map-pins-group");
      g.innerHTML = "";
      APP_STATE.regions.forEach(reg => {
        const cx = (reg.map_x / 100) * 1000;
        const cy = (reg.map_y / 100) * 500;
        const runningCount = reg.running_instances.length;
        const isSelected = reg.region === SELECTED_MAP_REGION;
        const color = runningCount > 0 ? "#10b981" : (reg.enabled ? "#3b82f6" : "#64748b");

        const pinGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
        pinGroup.setAttribute("class", "dc-pin");
        pinGroup.setAttribute("data-region", reg.region);
        pinGroup.onclick = () => {
          SELECTED_MAP_REGION = reg.region;
          renderWorldMapPins();
          renderMapInspector();
        };

        if (runningCount > 0) {
          const pulse = document.createElementNS("http://www.w3.org/2000/svg", "circle");
          pulse.setAttribute("cx", cx);
          pulse.setAttribute("cy", cy);
          pulse.setAttribute("r", isSelected ? 16 : 12);
          pulse.setAttribute("fill", "rgba(16, 185, 129, 0.35)");
          pulse.setAttribute("class", "pin-pulse");
          pinGroup.appendChild(pulse);
        }

        if (isSelected) {
          const ring = document.createElementNS("http://www.w3.org/2000/svg", "circle");
          ring.setAttribute("cx", cx);
          ring.setAttribute("cy", cy);
          ring.setAttribute("r", 14);
          ring.setAttribute("fill", "none");
          ring.setAttribute("stroke", "#fbbf24");
          ring.setAttribute("stroke-width", "2.5");
          pinGroup.appendChild(ring);
        }

        const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        dot.setAttribute("cx", cx);
        dot.setAttribute("cy", cy);
        dot.setAttribute("r", runningCount > 0 ? 8.5 : 5.5);
        dot.setAttribute("fill", color);
        dot.setAttribute("stroke", "#0b0f19");
        dot.setAttribute("stroke-width", "2");
        pinGroup.appendChild(dot);

        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", cx + 11);
        label.setAttribute("y", cy + 4);
        label.setAttribute("fill", isSelected ? "#fde047" : (runningCount > 0 ? "#f8fafc" : "#94a3b8"));
        label.setAttribute("font-size", isSelected || runningCount > 0 ? "11.5" : "10");
        label.setAttribute("font-weight", isSelected || runningCount > 0 ? "700" : "500");
        label.textContent = `${reg.region}${runningCount > 0 ? " (" + runningCount + ")" : ""}`;
        pinGroup.appendChild(label);

        g.appendChild(pinGroup);
      });
    }

    function renderMapInspector() {
      const panel = document.getElementById("map-inspector-panel");
      const reg = APP_STATE.regions.find(r => r.region === SELECTED_MAP_REGION) || APP_STATE.regions[0];
      const withDiskTax = document.getElementById("map-price-with-disk-tax").checked;
      const running = reg.running_instances || [];

      let instancesHtml = "";
      if (running.length === 0) {
        instancesHtml = `
          <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:9px; padding:14px; margin-bottom:12px;">
            <div style="color:var(--text-muted); margin-bottom:10px;">No running instances currently in <strong>${reg.display_name} (${reg.region})</strong>.</div>
            <div style="display:flex; gap:8px; flex-wrap:wrap;">
              <button class="btn btn-sm btn-emerald" onclick="scaleRegion('${esc(reg.region)}', 'cicd_runner', 1)">Plan 1 CI/CD Runner Host Here</button>
              <button class="btn btn-sm btn-purple" onclick="scaleRegion('${esc(reg.region)}', 'rust_builder', 1)">Plan 1 Rust Builder Host Here</button>
            </div>
          </div>
        `;
      } else {
        instancesHtml = running.map(inst => {
          const p = inst.pricing;
          const hr = withDiskTax ? p.total_hourly_usd : p.compute_hourly_usd;
          const day = withDiskTax ? p.total_daily_usd : p.compute_daily_usd;
          const mo = withDiskTax ? p.total_monthly_usd : p.compute_monthly_usd;
          const modeNote = withDiskTax
            ? `Includes ${inst.boot_disk_gb}GB ${inst.boot_disk_type} + ${inst.data_disk_gb}GB ${inst.data_disk_type} ($${p.disk_daily_usd.toFixed(2)}/day) & ${p.tax_rate_percent}% tax`
            : `Compute Spot price only (excludes disk & taxes)`;

          return `
            <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:10px; padding:14px; margin-bottom:12px;">
              <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:8px;">
                <div>
                  <div style="font-size:14.5px; font-weight:700; color:#fff;" class="mono">${esc(inst.name)}</div>
                  <div style="font-size:12px; color:var(--text-muted);">Zone: <span class="mono">${esc(inst.zone)}</span> · Instance Type: <strong class="mono">${esc(inst.machine_type)}</strong> (${inst.vcpu} vCPU / ${inst.ram_gib} GiB RAM)</div>
                </div>
                <span class="badge ${inst.role === 'cicd_runner' ? 'badge-runner' : 'badge-builder'}">${inst.role === 'cicd_runner' ? 'CI/CD Runner Host' : 'Rust Builder'}</span>
              </div>

              <div style="background:rgba(16, 185, 129, 0.11); border:1px solid rgba(16, 185, 129, 0.3); border-radius:8px; padding:9px 11px; margin-bottom:10px; color:#a7f3d0; font-weight:600; font-size:12.5px;">
                ⚡ ${esc(inst.workload_summary)}
              </div>

              <div style="display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin-bottom:8px;">
                <div style="background:#0b1120; border:1px solid var(--border); border-radius:7px; padding:8px;">
                  <div style="font-size:10.5px; color:var(--text-muted);">SPOT PRICE / HOUR</div>
                  <div class="mono" style="font-size:16px; font-weight:700; color:#38bdf8;">$${hr.toFixed(4)}/hr</div>
                </div>
                <div style="background:#0b1120; border:1px solid var(--border); border-radius:7px; padding:8px;">
                  <div style="font-size:10.5px; color:var(--text-muted);">SPOT PRICE / DAY</div>
                  <div class="mono" style="font-size:16px; font-weight:700; color:#34d399;">$${day.toFixed(2)}/day</div>
                </div>
                <div style="background:#0b1120; border:1px solid var(--border); border-radius:7px; padding:8px;">
                  <div style="font-size:10.5px; color:var(--text-muted);">SPOT PRICE / MONTH</div>
                  <div class="mono" style="font-size:16px; font-weight:700; color:#c4b5fd;">$${mo.toFixed(2)}/mo</div>
                </div>
              </div>
              <div style="font-size:11.5px; color:var(--text-muted); margin-bottom:10px;">
                💡 ${esc(modeNote)} · CPU: <strong>${esc(inst.cpu_platform)}</strong> (Benchmark: <strong>${inst.benchmark_score}</strong>)
              </div>

              <div style="font-size:11.5px; font-weight:600; color:var(--text-muted);">SSH Into GCloud Instance (Terminal Command):</div>
              <div class="ssh-block">
                <code id="ssh-code-${esc(inst.instance_id)}">${esc(inst.ssh_command)}</code>
                <button class="btn btn-sm" onclick="copyTextToClipboard('${esc(inst.ssh_command)}', 'gcloud SSH command')">📋 Copy SSH</button>
              </div>

              <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; padding-top:10px; border-top:1px solid var(--border);">
                ${canTemplate(inst) ? `<button class="btn btn-sm btn-purple" onclick="makeTemplateFromInstance('${esc(inst.instance_id)}', '${esc(inst.name)}')">🖥️ Create VM Template</button>` : ""}
              </div>
            </div>
          `;
        }).join("");
      }

      const runnerCountInReg = running.filter(i => i.role === "cicd_runner").length;
      const builderCountInReg = running.filter(i => i.role === "rust_builder").length;
      panel.innerHTML = `
        <div class="panel-header">
          <div>
            <div class="panel-title">📍 ${esc(reg.display_name)} (<span class="mono">${esc(reg.region)}</span>)</div>
            <div class="panel-subtitle">Continent: ${esc(reg.continent)} · Backend Latency: <strong class="mono">${reg.latency_ms} ms</strong> · Status: ${reg.enabled ? '<span style="color:#34d399;">Enabled</span>' : '<span style="color:#fb7185;">Deselected</span>'}</div>
          </div>
        </div>

        <div style="display:flex; flex-direction:column; gap:8px; background:var(--bg-card); border:1px solid var(--border); padding:10px 12px; border-radius:9px; margin-bottom:12px;">
          <div style="font-size:11.5px; color:var(--text-muted);">Plan only: these controls check a scale change against budgets and caps and report what it would launch or retire. The dashboard never creates or deletes VMs; the autoscaler and the provisioning tools do.</div>
          <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px;">
            <div style="font-size:12.5px; font-weight:600;">CI/CD Runner Hosts (<span class="mono">${esc(reg.region)}</span>):</div>
            <div style="display:flex; align-items:center; gap:6px;">
              <button class="btn btn-sm btn-secondary" onclick="scaleRegion('${esc(reg.region)}', 'cicd_runner', Math.max(0, ${runnerCountInReg} - 1))">−1</button>
              <input type="number" id="map-scale-input-${esc(reg.region)}" value="${runnerCountInReg}" min="0" max="20" style="width:58px; text-align:center;" />
              <button class="btn btn-sm btn-secondary" onclick="scaleRegion('${esc(reg.region)}', 'cicd_runner', ${runnerCountInReg} + 1)">+1</button>
              <button class="btn btn-sm btn-emerald" onclick="scaleRegion('${esc(reg.region)}', 'cicd_runner', parseInt(document.getElementById('map-scale-input-${esc(reg.region)}').value || '0', 10))">Plan Runner Scale</button>
            </div>
          </div>
          <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; border-top:1px solid var(--border); padding-top:8px;">
            <div style="font-size:12.5px; font-weight:600; color:#c4b5fd;">🦀 Rust Builder Hosts (<span class="mono">${esc(reg.region)}</span>):</div>
            <div style="display:flex; align-items:center; gap:6px;">
              <button class="btn btn-sm btn-secondary" onclick="scaleRegion('${esc(reg.region)}', 'rust_builder', Math.max(0, ${builderCountInReg} - 1))">−1</button>
              <input type="number" id="map-scale-builder-input-${esc(reg.region)}" value="${builderCountInReg}" min="0" max="20" style="width:58px; text-align:center;" />
              <button class="btn btn-sm btn-secondary" onclick="scaleRegion('${esc(reg.region)}', 'rust_builder', ${builderCountInReg} + 1)">+1</button>
              <button class="btn btn-sm btn-purple" onclick="scaleRegion('${esc(reg.region)}', 'rust_builder', parseInt(document.getElementById('map-scale-builder-input-${esc(reg.region)}').value || '0', 10))">Plan Builder Scale</button>
            </div>
          </div>
        </div>

        ${instancesHtml}
      `;
    }

    // A VM template copies one shape: records whose per-runner limits or disks it cannot describe cannot be sources.
    function canTemplate(inst) {
      return inst.kind !== "disk" && !(inst.shared_attachments || []).length && inst.runner_limits_known !== false && inst.disk_types_known !== false
        && !inst.regional_disks && !inst.unpriced_disk_performance && !(inst.local_ssd_gb > 0)
        && Object.keys(inst.data_disks || {}).length <= 1;
    }

    async function scaleRegion(region, role, targetCount) {
      const res = await fetch("/api/instances/scale-region", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ region, role, target_instances: targetCount })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Plan for ${region} rejected: ${data.error || res.statusText}`);
        return;
      }
      const launch = (data.would_launch || []).length;
      const retire = (data.would_retire || []).length;
      showToast(`Plan only, nothing changed: ${region} (${role}) ${data.current_count} → ${data.planned_count} instance(s); would launch ${launch}, retire ${retire}. ${data.message || ""}`);
    }

    async function makeTemplateFromInstance(instanceId, name) {
      const res = await fetch("/api/vm-templates/from-instance", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          instance_id: instanceId,
          instance_name: name,
          template_name: `Template from ${name}`
        })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error creating VM Template: ${data.error || res.statusText}`);
        return;
      }
      if (data.vm_template) {
        showToast(`Created VM Template "${data.vm_template.name}" from ${name}`);
        await loadOverview();
      }
    }

    // =========================================================================
    // TAB 2: INSTANCE LIST & COST HISTORY
    // =========================================================================
    function setInstanceWindowHours(hours) {
      INST_WINDOW_HOURS = hours;
      document.getElementById("inst-custom-window-val").value = hours;
      document.getElementById("inst-custom-window-unit").value = "hours";
      refreshInstancesTable();
    }

    function applyCustomWindow() {
      const val = parseFloat(document.getElementById("inst-custom-window-val").value || "24");
      const unit = document.getElementById("inst-custom-window-unit").value;
      INST_WINDOW_HOURS = unit === "days" ? val * 24 : val;
      refreshInstancesTable();
    }

    function toggleInstanceSort(field) {
      if (INST_SORT_BY === field) {
        INST_SORT_DIR = INST_SORT_DIR === "desc" ? "asc" : "desc";
      } else {
        INST_SORT_BY = field;
        INST_SORT_DIR = "desc";
      }
      refreshInstancesTable();
    }

    function sortInstancesBy(field, dir) {
      INST_SORT_BY = field;
      INST_SORT_DIR = dir;
      refreshInstancesTable();
    }

    async function refreshInstancesTable() {
      await loadOverview();
    }

    function renderInstancesTableRows() {
      const iv = APP_STATE.instances_view;
      const summary = document.getElementById("inst-window-summary");
      const fi = iv.fleet_inventory || APP_STATE.fleet_inventory || {};
      const inventoryNote = fi.source === "gce"
        ? `Source: live Compute Engine inventory${fi.last_success_at ? ", last listed " + esc(formatLocalTime(fi.last_success_at)) : ", not listed yet"}${(fi.stale_reason || fi.last_error) ? ' · <span style="color:#fda4af;">' + esc(fi.stale_reason || fi.last_error) + "</span>" : ""}`
        : `Source: live Compute Engine inventory is not configured on this dashboard (set DASHBOARD_FLEET_INVENTORY=gce); no instances are shown`;
      summary.innerHTML = `${inventoryNote}<br>Showing <strong>${iv.instances.length}</strong> instance(s) in the last <strong>${iv.window_hours} hours</strong> (${iv.running_count} running, ${iv.deleted_count} stopped or deleted) · Total Window Spend: <strong class="mono" style="color:#34d399;">$${iv.total_window_spend_usd.toFixed(2)}</strong> · Sorted by <code class="mono">${esc(iv.sort_by)} (${esc(iv.sort_dir.toUpperCase())})</code> · Timezone: <code class="mono">${esc(BROWSER_TZ)}</code>`;

      const tbody = document.getElementById("instances-table-body");
      tbody.innerHTML = iv.instances.map(inst => {
        const isRunning = inst.status === "RUNNING";
        return `
          <tr>
            <td>
              <div class="mono" style="font-weight:700; color:#f8fafc;">${esc(inst.name)}</div>
              <div class="mono" style="font-size:11px; color:var(--text-muted);">ID: ${esc(inst.instance_id)}</div>
            </td>
            <td>
              <span class="badge ${isRunning ? 'badge-running' : 'badge-deleted'}">${esc(inst.status)}</span>
            </td>
            <td>
              <span class="badge ${inst.role === 'cicd_runner' ? 'badge-runner' : 'badge-builder'}">${inst.role === 'cicd_runner' ? 'CI/CD Runner' : 'Rust Builder'}</span>
            </td>
            <td>
              <div style="font-weight:600;">${esc(inst.display_location)}</div>
              <div class="mono" style="font-size:11px; color:var(--text-muted);">${esc(inst.zone)}</div>
            </td>
            <td>
              <div class="mono" style="font-weight:600;">${esc(inst.machine_type)} (${inst.vcpu} vCPU / ${inst.ram_gib} GiB)</div>
              <div style="font-size:11px; color:var(--text-muted);">${esc(inst.cpu_platform)} · ${inst.data_disk_gb}GB ${esc(inst.data_disk_type)} (${esc(inst.disk_category)})</div>
            </td>
            <td class="mono" style="font-weight:700; color:#38bdf8;">${inst.benchmark_score}</td>
            <td>
              <div style="font-weight:600;">${inst.role === 'cicd_runner' ? inst.runners_count + ' GitHub Runners' : '1 Rust Builder'}</div>
              <div style="font-size:11px; color:var(--text-muted);">${inst.runner_limits_known === false ? "per-runner limits not recorded" : `${inst.runner_vcpu} vCPU / ${inst.runner_ram_gib} GiB each`}</div>
            </td>
            <td class="mono">
              <div>${inst.runtime_hours}h total</div>
              <div style="font-size:11px; color:var(--text-muted);">${inst.hours_in_window}h in window</div>
            </td>
            <td class="mono">
              <div>$${inst.active_hourly_usd.toFixed(4)}/hr</div>
              <div style="font-size:11px; color:var(--text-muted);">$${inst.active_daily_usd.toFixed(2)}/day</div>
            </td>
            <td class="mono" style="font-size:14px; font-weight:700; color:#34d399;">
              $${inst.total_cost_in_window_usd.toFixed(2)}
            </td>
            <td style="font-size:11.5px;">
              <div><strong>Start:</strong> ${esc(formatLocalTime(inst.started_at))}</div>
              <div><strong>End:</strong> ${inst.deleted_at ? esc(formatLocalTime(inst.deleted_at)) : inst.status === "DETACHED" ? '<span style="color:#fcd34d;">Present (storage still billed)</span>' : inst.stopped_at ? 'Stopped ' + esc(formatLocalTime(inst.stopped_at)) : isRunning ? '<span style="color:#34d399;">Present (Running)</span>' : esc(inst.status)}</div>
            </td>
            <td style="max-width:260px; font-size:11.5px; color:${isRunning ? '#a7f3d0' : '#fda4af'};">
              ${esc(inst.deletion_trigger)}
            </td>
            <td>
              <div style="display:flex; gap:5px; flex-wrap:wrap;">
                <button class="btn btn-sm btn-secondary" onclick="copyTextToClipboard('${esc(inst.ssh_command)}', 'SSH command')">📋 SSH</button>
                ${canTemplate(inst) ? `<button class="btn btn-sm btn-purple" onclick="makeTemplateFromInstance('${esc(inst.instance_id)}', '${esc(inst.name)}')">Template</button>` : ""}
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    // =========================================================================
    // TAB 3: SIZING & BENCHMARK QUALIFICATION
    // =========================================================================
    function populateSizingForms() {
      const rp = APP_STATE.profiles.cicd_runner;
      document.getElementById("runner-min-runners").value = rp.min_runners;
      document.getElementById("runner-unit-vcpu").value = rp.runner_vcpu;
      document.getElementById("runner-unit-ram").value = rp.runner_ram_gib;
      document.getElementById("runner-min-bench").value = rp.min_benchmark_score;
      document.getElementById("runner-min-inst-vcpu").value = rp.min_instance_vcpu;
      document.getElementById("runner-min-inst-ram").value = rp.min_instance_ram_gib;
      document.getElementById("runner-disk-cat").value = rp.disk_category;
      document.getElementById("runner-disk-type").value = rp.disk_type;
      document.getElementById("runner-disk-gb").value = rp.data_disk_gb;
      document.getElementById("runner-max-hour").value = rp.budget.max_hourly_usd;
      document.getElementById("runner-max-day").value = rp.budget.max_daily_usd;
      document.getElementById("runner-max-month").value = rp.budget.max_monthly_usd;
      document.getElementById("runner-use-vmtpl").checked = Boolean(rp.use_vm_template);

      const bp = APP_STATE.profiles.rust_builder;
      document.getElementById("builder-min-instances").value = bp.min_instances;
      document.getElementById("builder-unit-vcpu").value = bp.runner_vcpu;
      document.getElementById("builder-unit-ram").value = bp.runner_ram_gib;
      document.getElementById("builder-min-bench").value = bp.min_benchmark_score;
      document.getElementById("builder-min-inst-vcpu").value = bp.min_instance_vcpu;
      document.getElementById("builder-min-inst-ram").value = bp.min_instance_ram_gib;
      document.getElementById("builder-disk-cat").value = bp.disk_category;
      document.getElementById("builder-disk-type").value = bp.disk_type;
      document.getElementById("builder-disk-gb").value = bp.data_disk_gb;
      document.getElementById("builder-max-hour").value = bp.budget.max_hourly_usd;
      document.getElementById("builder-max-day").value = bp.budget.max_daily_usd;
      document.getElementById("builder-max-month").value = bp.budget.max_monthly_usd;
      document.getElementById("builder-use-vmtpl").checked = Boolean(bp.use_vm_template);

      // Populate Cloud-Init and VM template dropdowns filtered by workload role
      const ciSelectRunner = document.getElementById("runner-cloudinit-select");
      const ciSelectBuilder = document.getElementById("builder-cloudinit-select");
      ciSelectRunner.innerHTML = APP_STATE.cloud_init_templates
        .filter(t => (t.role || "cicd_runner") === "cicd_runner")
        .map(t => `<option value="${esc(t.id)}" ${t.id === rp.cloud_init_template_id ? 'selected' : ''}>${esc(t.name)}</option>`)
        .join("");
      ciSelectBuilder.innerHTML = APP_STATE.cloud_init_templates
        .filter(t => t.role === "rust_builder")
        .map(t => `<option value="${esc(t.id)}" ${t.id === bp.cloud_init_template_id ? 'selected' : ''}>${esc(t.name)}</option>`)
        .join("");

      const vmSelectRunner = document.getElementById("runner-vmtpl-select");
      const vmSelectBuilder = document.getElementById("builder-vmtpl-select");
      vmSelectRunner.innerHTML = APP_STATE.vm_templates
        .filter(t => (t.role || "cicd_runner") === "cicd_runner")
        .map(t => `<option value="${esc(t.id)}" ${t.id === rp.vm_template_id ? 'selected' : ''}>${esc(t.name)} (${esc(t.machine_type)})</option>`)
        .join("");
      vmSelectBuilder.innerHTML = APP_STATE.vm_templates
        .filter(t => t.role === "rust_builder")
        .map(t => `<option value="${esc(t.id)}" ${t.id === bp.vm_template_id ? 'selected' : ''}>${esc(t.name)} (${esc(t.machine_type)})</option>`)
        .join("");

      updateSizingMathBanners();
    }

    function updateSizingMathBanners() {
      const rv = parseInt(document.getElementById("runner-unit-vcpu").value || "4", 10);
      const rr = parseFloat(document.getElementById("runner-unit-ram").value || "16");
      const rmin = parseInt(document.getElementById("runner-min-runners").value || "4", 10);
      const rbench = parseInt(document.getElementById("runner-min-bench").value || "1000", 10);
      document.getElementById("runner-sizing-math-banner").innerHTML =
        `📐 <strong>Sizing Rule:</strong> Each runner requires <strong>${rv} vCPU</strong> &amp; <strong>${rr} GiB RAM</strong> (CPU benchmark &gt;= <strong>${rbench}</strong>). ` +
        `An instance holding <strong>2 runners</strong> requires &gt;= <strong>${rv * 2} vCPU</strong> &amp; <strong>${rr * 2} GiB RAM</strong> (${Math.ceil(rmin / 2)} hosts for ${rmin} runners); ` +
        `an instance holding <strong>4 runners</strong> requires &gt;= <strong>${rv * 4} vCPU</strong> &amp; <strong>${rr * 4} GiB RAM</strong>.`;

      const bv = parseInt(document.getElementById("builder-unit-vcpu").value || "16", 10);
      const br = parseFloat(document.getElementById("builder-unit-ram").value || "28");
      const bbench = parseInt(document.getElementById("builder-min-bench").value || "1400", 10);
      document.getElementById("builder-sizing-math-banner").innerHTML =
        `📐 <strong>Builder Rule:</strong> Each dedicated Rust builder requires &gt;= <strong>${bv} vCPU</strong>, <strong>${br} GiB</strong> container memory cap, and CPU benchmark score &gt;= <strong>${bbench}</strong>.`;
    }

    function collectProfileForm(role) {
      const curBudget = (APP_STATE && APP_STATE.profiles && APP_STATE.profiles[role] && APP_STATE.profiles[role].budget)
        ? APP_STATE.profiles[role].budget
        : {};
      if (role === "cicd_runner") {
        return {
          role: "cicd_runner",
          min_runners: parseInt(document.getElementById("runner-min-runners").value || "4", 10),
          runner_vcpu: parseInt(document.getElementById("runner-unit-vcpu").value || "4", 10),
          runner_ram_gib: parseFloat(document.getElementById("runner-unit-ram").value || "16"),
          min_benchmark_score: parseInt(document.getElementById("runner-min-bench").value || "1000", 10),
          min_instance_vcpu: parseInt(document.getElementById("runner-min-inst-vcpu").value || "8", 10),
          min_instance_ram_gib: parseFloat(document.getElementById("runner-min-inst-ram").value || "32"),
          disk_category: document.getElementById("runner-disk-cat").value,
          disk_type: document.getElementById("runner-disk-type").value,
          data_disk_gb: parseInt(document.getElementById("runner-disk-gb").value || "100", 10),
          use_vm_template: document.getElementById("runner-use-vmtpl").checked,
          vm_template_id: document.getElementById("runner-vmtpl-select").value,
          cloud_init_template_id: document.getElementById("runner-cloudinit-select").value,
          budget: {
            ...curBudget,
            active_period: curBudget.active_period || "day",
            max_hourly_usd: parseFloat(document.getElementById("runner-max-hour").value || String(curBudget.max_hourly_usd ?? 5)),
            max_daily_usd: parseFloat(document.getElementById("runner-max-day").value || String(curBudget.max_daily_usd ?? 10)),
            max_monthly_usd: parseFloat(document.getElementById("runner-max-month").value || String(curBudget.max_monthly_usd ?? 50)),
            include_disk_costs: curBudget.include_disk_costs !== undefined ? Boolean(curBudget.include_disk_costs) : true,
            include_taxes: curBudget.include_taxes !== undefined ? Boolean(curBudget.include_taxes) : false,
            tax_rate_percent: curBudget.tax_rate_percent !== undefined ? Number(curBudget.tax_rate_percent) : 8.25
          }
        };
      }
      return {
        role: "rust_builder",
        min_runners: parseInt(document.getElementById("builder-min-instances").value || "1", 10),
        min_instances: parseInt(document.getElementById("builder-min-instances").value || "1", 10),
        runner_vcpu: parseInt(document.getElementById("builder-unit-vcpu").value || "16", 10),
        runner_ram_gib: parseFloat(document.getElementById("builder-unit-ram").value || "28"),
        min_benchmark_score: parseInt(document.getElementById("builder-min-bench").value || "1400", 10),
        min_instance_vcpu: parseInt(document.getElementById("builder-min-inst-vcpu").value || "16", 10),
        min_instance_ram_gib: parseFloat(document.getElementById("builder-min-inst-ram").value || "32"),
        disk_category: document.getElementById("builder-disk-cat").value,
        disk_type: document.getElementById("builder-disk-type").value,
        data_disk_gb: parseInt(document.getElementById("builder-disk-gb").value || "100", 10),
        use_vm_template: document.getElementById("builder-use-vmtpl").checked,
        vm_template_id: document.getElementById("builder-vmtpl-select").value,
        cloud_init_template_id: document.getElementById("builder-cloudinit-select").value,
        budget: {
          ...curBudget,
          active_period: curBudget.active_period || "day",
          max_hourly_usd: parseFloat(document.getElementById("builder-max-hour").value || String(curBudget.max_hourly_usd ?? 3.5)),
          max_daily_usd: parseFloat(document.getElementById("builder-max-day").value || String(curBudget.max_daily_usd ?? 12)),
          max_monthly_usd: parseFloat(document.getElementById("builder-max-month").value || String(curBudget.max_monthly_usd ?? 80)),
          include_disk_costs: curBudget.include_disk_costs !== undefined ? Boolean(curBudget.include_disk_costs) : true,
          include_taxes: curBudget.include_taxes !== undefined ? Boolean(curBudget.include_taxes) : false,
          tax_rate_percent: curBudget.tax_rate_percent !== undefined ? Number(curBudget.tax_rate_percent) : 8.25
        }
      };
    }

    async function previewQualification(role) {
      updateSizingMathBanners();
      QUAL_ACTIVE_ROLE = role;
      const payload = collectProfileForm(role);
      const res = await fetch("/api/qualify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error previewing qualification: ${data.error || res.statusText}`);
        return;
      }
      LAST_QUAL_DATA[role] = data;
      renderQualificationTable();
    }

    async function saveProfile(role) {
      const payload = collectProfileForm(role);
      const res = await fetch(`/api/profile/${role}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error saving profile: ${data.error || res.statusText}`);
        return;
      }
      LAST_QUAL_DATA[role] = data.qualification;
      showToast(`Saved ${role === 'cicd_runner' ? 'CI/CD Runner' : 'Rust Builder'} profile (${data.qualification.spot_price_qualifying_count} Spot offers qualify)`);
      await loadOverview();
    }

    function switchQualRole(role) {
      QUAL_ACTIVE_ROLE = role;
      document.getElementById("qual-role-btn-runner").className = role === "cicd_runner" ? "btn btn-sm" : "btn btn-sm btn-secondary";
      document.getElementById("qual-role-btn-builder").className = role === "rust_builder" ? "btn btn-sm" : "btn btn-sm btn-secondary";
      renderQualificationTable();
    }

    function renderQualificationTable() {
      const q = LAST_QUAL_DATA[QUAL_ACTIVE_ROLE] || APP_STATE.qualifications[QUAL_ACTIVE_ROLE];
      if (!q) return;
      const onlyAffordable = document.getElementById("qual-only-spot-affordable").checked;
      const rows = onlyAffordable ? q.spot_price_qualifying : q.hardware_qualifying;
      document.getElementById("qual-summary-subtitle").innerHTML =
        `Role: <strong>${QUAL_ACTIVE_ROLE === 'cicd_runner' ? 'CI/CD GitHub Runners' : 'Rust Remote Builders'}</strong> · ` +
        `<strong>${q.hardware_qualifying_count}</strong> instances qualify by hardware &amp; CPU benchmark (&gt;=${q.requirements.min_benchmark_score}) · ` +
        `<strong style="color:#34d399;">${q.spot_price_qualifying_count}</strong> currently qualify under Spot pricing ($${q.requirements.budget.max_hourly_usd}/hr · $${q.requirements.budget.max_daily_usd}/day · $${q.requirements.budget.max_monthly_usd}/mo).`;

      const tbody = document.getElementById("qualification-table-body");
      tbody.innerHTML = rows.slice(0, 45).map(r => `
        <tr>
          <td class="mono" style="font-weight:700; color:#fff;">${esc(r.machine_type)}</td>
          <td>
            <div style="font-weight:600;">${esc(r.display_location)}</div>
            <div class="mono" style="font-size:11px; color:var(--text-muted);">${esc(r.zone)} (${r.latency_ms} ms)</div>
          </td>
          <td>${esc(r.cpu_platform)}</td>
          <td class="mono" style="font-weight:700; color:#38bdf8;">${r.benchmark_score}</td>
          <td class="mono">${r.vcpu} vCPU / ${r.ram_gib} GiB</td>
          <td class="mono"><strong>${r.units_per_instance}</strong> (${r.unit_vcpu}c/${r.unit_ram_gib}G each)</td>
          <td class="mono">${r.instances_needed_for_floor} VM(s) (${r.total_units_at_floor} total)</td>
          <td class="mono">${r.data_disk_gb}GB ${esc(r.data_disk_type)}</td>
          <td class="mono">$${r.pricing.compute_hourly_usd.toFixed(4)}/hr · $${r.pricing.compute_daily_usd.toFixed(2)}/d</td>
          <td class="mono" style="font-weight:700;">$${r.evaluated_hourly_usd.toFixed(4)}/hr · $${r.evaluated_daily_usd.toFixed(2)}/d · $${r.evaluated_monthly_usd.toFixed(2)}/mo</td>
          <td>
            ${r.price_qualifies
              ? '<span class="badge badge-running">✓ Qualifies on Spot</span>'
              : '<span class="badge badge-deleted">Exceeds Budget Cap</span>'}
          </td>
        </tr>
      `).join("");
    }

    // =========================================================================
    // TAB 4: SCALING & OOM DEEP-LINKS
    // =========================================================================
    function populateScalingView() {
      const sc = APP_STATE.scaling.cicd_runner;
      const st = APP_STATE.scaling_telemetry;
      document.getElementById("sc-min-runners").value = sc.min_runners;
      document.getElementById("sc-max-runners").value = sc.max_runners;
      document.getElementById("sc-queued-thresh").value = sc.queued_jobs_threshold;
      document.getElementById("sc-prov-thresh").value = sc.provisioned_jobs_threshold;
      document.getElementById("sc-hourly-up-thresh").value = sc.hourly_runs_scale_up_threshold;
      document.getElementById("sc-cpu-thresh").value = sc.cpu_threshold_percent;
      document.getElementById("sc-cpu-scope").value = sc.cpu_scope;
      document.getElementById("sc-hourly-down-max").value = sc.scale_down_max_jobs_past_hour;
      document.getElementById("sc-hourly-down-target").value = sc.scale_down_target_runners;
      document.getElementById("sc-budget-hr").value = sc.max_scale_up_hourly_usd;
      document.getElementById("sc-budget-day").value = sc.max_scale_up_daily_usd;
      document.getElementById("sc-budget-mo").value = sc.max_scale_up_monthly_usd;

      document.getElementById("sc-en-queued").checked = Boolean(sc.enable_queued_jobs_trigger);
      document.getElementById("sc-en-prov").checked = Boolean(sc.enable_provisioned_jobs_trigger);
      document.getElementById("sc-en-hourly-up").checked = Boolean(sc.enable_hourly_runs_scale_up);
      document.getElementById("sc-en-hourly-down").checked = Boolean(sc.enable_hourly_runs_scale_down);
      document.getElementById("sc-en-cpu").checked = Boolean(sc.enable_cpu_utilization_trigger);
      document.getElementById("sc-en-journal-oom").checked = Boolean(sc.enable_journal_oom_trigger);
      document.getElementById("sc-en-gh-oom").checked = Boolean(sc.enable_github_ci_oom_trigger);

      document.getElementById("sim-jobs-hour").value = st.jobs_ran_past_hour;
      document.getElementById("sim-jobs-prov").value = st.provisioned_jobs;
      document.getElementById("sim-jobs-queued").value = st.queued_jobs;

      const ev = APP_STATE.scaling_evaluation;
      const reasons = [...ev.scale_up_reasons, ...ev.scale_down_reasons];
      document.getElementById("scaling-decision-box").innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
          <div style="font-weight:700; font-size:14px;">Live Controller Evaluation: <span style="color:#38bdf8;">${esc(ev.direction.toUpperCase())}</span> (${ev.current_runners} → ${ev.desired_runners} CI/CD Runners / ${ev.desired_instances} VMs)</div>
          <span class="mono" style="font-size:12px; color:#34d399;">Projected: $${ev.projected_hourly_usd}/hr · $${ev.projected_daily_usd}/day · $${ev.projected_monthly_usd}/mo</span>
        </div>
        ${ev.budget_capped ? `<div style="color:#fbbf24; font-size:12px; margin-bottom:6px;">⚠️ ${esc(ev.budget_cap_note)}</div>` : ""}
        <div style="display:flex; flex-direction:column; gap:5px;">
          ${reasons.map(r => `<div style="font-size:12px; color:#e2e8f0;">• <strong>[${esc(r.trigger)}]</strong> ${esc(r.summary)} ${r.deep_link ? `<a href="${esc(r.deep_link)}" target="_blank" rel="noopener" class="mono">[Open Exact Build Step &amp; Line ↗]</a>` : ''}</div>`).join("") || '<div style="font-size:12px; color:var(--text-muted);">No scale change required.</div>'}
        </div>
      `;

      // Populate Rust Builder scaling policy & evaluation
      const bsc = APP_STATE.scaling.rust_builder || {};
      document.getElementById("sc-builder-min-inst").value = bsc.min_instances ?? 1;
      document.getElementById("sc-builder-max-inst").value = bsc.max_instances ?? 6;
      document.getElementById("sc-builder-queued-thresh").value = bsc.queued_builds_threshold ?? 2;
      document.getElementById("sc-builder-cpu-thresh").value = bsc.cpu_threshold_percent ?? 60;
      document.getElementById("sc-builder-down-max").value = bsc.scale_down_max_builds_past_hour ?? 2;
      document.getElementById("sc-builder-down-target").value = bsc.scale_down_target_instances ?? 1;
      document.getElementById("sc-builder-budget-hr").value = bsc.max_scale_up_hourly_usd ?? 3.5;
      document.getElementById("sc-builder-budget-day").value = bsc.max_scale_up_daily_usd ?? 12.0;
      document.getElementById("sc-builder-budget-mo").value = bsc.max_scale_up_monthly_usd ?? 80.0;
      document.getElementById("sc-builder-en-queue").checked = bsc.enable_queue_trigger !== false;
      document.getElementById("sc-builder-en-cpu").checked = bsc.enable_cpu_utilization_trigger !== false;
      document.getElementById("sc-builder-en-oom").checked = bsc.enable_journal_oom_trigger !== false;
      document.getElementById("sc-builder-en-down").checked = bsc.enable_low_builds_scale_down !== false;
      document.getElementById("sim-builds-hour").value = st.builds_ran_past_hour ?? 6;
      document.getElementById("sim-builds-active").value = st.active_builds ?? 1;
      document.getElementById("sim-builds-queued").value = st.queued_builds ?? 0;

      const bev = (APP_STATE.scaling_evaluations && APP_STATE.scaling_evaluations.rust_builder) || APP_STATE.builder_scaling_evaluation;
      if (bev) {
        const breasons = [...(bev.scale_up_reasons || []), ...(bev.scale_down_reasons || [])];
        document.getElementById("builder-scaling-decision-box").innerHTML = `
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <div style="font-weight:700; font-size:13.5px;">Rust Builder Evaluation: <span style="color:#c4b5fd;">${esc(bev.direction.toUpperCase())}</span> (${bev.current_instances} → ${bev.desired_instances} Builder VMs)</div>
            <span class="mono" style="font-size:12px; color:#34d399;">Projected: $${bev.projected_hourly_usd}/hr · $${bev.projected_daily_usd}/day · $${bev.projected_monthly_usd}/mo</span>
          </div>
          ${bev.budget_capped ? `<div style="color:#fbbf24; font-size:12px; margin-bottom:6px;">⚠️ ${esc(bev.budget_cap_note)}</div>` : ""}
          <div style="display:flex; flex-direction:column; gap:5px;">
            ${breasons.map(r => `<div style="font-size:12px; color:#e2e8f0;">• <strong>[${esc(r.trigger)}]</strong> ${esc(r.summary)}</div>`).join("") || '<div style="font-size:12px; color:var(--text-muted);">No builder scale change required.</div>'}
          </div>
        `;
      }

      // Render GitHub CI Runs (distinguishes confirmed OOMs from Timeouts) with exact step/line clickable links
      document.getElementById("github-oom-runs-list").innerHTML = st.github_oom_runs.map(run => {
        const isOom = run.is_oom !== false;
        const borderCol = isOom ? "rgba(244, 63, 94, 0.4)" : "rgba(245, 158, 11, 0.4)";
        const leftCol = isOom ? "#f43f5e" : "#f59e0b";
        const textCol = isOom ? "#fda4af" : "#fde68a";
        const badgeCls = isOom ? "badge-deleted" : "badge-vpn";
        const badgeLabel = run.badge_text || (isOom ? "OOM Killed (Exit 137)" : "Timeout (Not OOM — Ignored)");
        const btnCls = isOom ? "btn-danger" : "btn-secondary";
        return `
        <div style="background:var(--bg-card); border:1px solid ${borderCol}; border-left:4px solid ${leftCol}; border-radius:8px; padding:11px 13px;">
          <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:4px;">
            <div style="font-weight:700; color:#fff;">${esc(run.repository)} — ${esc(run.workflow_name || 'CI Workflow')} (<span class="mono">${esc(run.job_name || 'job')}</span>)</div>
            <span class="badge ${badgeCls}">${esc(badgeLabel)}</span>
          </div>
          <div class="mono" style="font-size:11.5px; color:${textCol}; margin-bottom:6px;">${esc(run.error_excerpt)}</div>
          <div style="font-size:12px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
            <span>Runner: <code class="mono">${esc(run.runner_name || 'not reported')}</code> · Run <code class="mono">#${esc(run.run_id)}</code> · Job <code class="mono">${esc(run.job_id)}</code> · Step <code class="mono">${esc(run.step_number)}</code> · Line <code class="mono">${esc(run.line_number)}</code> · ${esc(formatLocalTime(run.failed_at))}</span>
            <a href="${esc(run.deep_link)}" target="_blank" rel="noopener" class="btn btn-sm ${btnCls} mono" style="text-decoration:none;">
              🔗 ${esc(run.deep_link)}
            </a>
          </div>
        </div>
        `;
      }).join("");

      // Render VM Journal OOM events
      document.getElementById("journal-oom-events-list").innerHTML = st.journal_oom_events.map(evt => `
        <div style="background:var(--bg-card); border:1px solid rgba(245, 158, 11, 0.4); border-left:4px solid #f59e0b; border-radius:8px; padding:10px 12px;">
          <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
            <strong>Host: <span class="mono">${esc(evt.instance_name)}</span> (${esc(evt.zone)}) — Process <span class="mono">${esc(evt.process)}</span> (PID ${esc(evt.pid)})</strong>
            <span class="mono" style="font-size:11px; color:var(--text-muted);">${esc(formatLocalTime(evt.observed_at))}</span>
          </div>
          <div class="mono" style="font-size:11.5px; color:#fde68a; background:#090d16; padding:6px 8px; border-radius:6px;">${esc(evt.journal_line)}</div>
        </div>
      `).join("");
    }

    async function saveAndEvaluateScaling() {
      const curSc = APP_STATE?.scaling?.cicd_runner || {};
      const nextHourlyUsd = parseFloat(document.getElementById("sc-budget-hr").value || String(curSc.max_scale_up_hourly_usd ?? 5));
      const nextDailyUsd = parseFloat(document.getElementById("sc-budget-day").value || String(curSc.max_scale_up_daily_usd ?? 10));
      const nextMonthlyUsd = parseFloat(document.getElementById("sc-budget-mo").value || String(curSc.max_scale_up_monthly_usd ?? 50));
      const budgetCapsEdited = (
        Math.abs(nextHourlyUsd - Number(curSc.max_scale_up_hourly_usd ?? 5)) > 1e-6 ||
        Math.abs(nextDailyUsd - Number(curSc.max_scale_up_daily_usd ?? 10)) > 1e-6 ||
        Math.abs(nextMonthlyUsd - Number(curSc.max_scale_up_monthly_usd ?? 50)) > 1e-6
      );
      const policy = {
        min_runners: parseInt(document.getElementById("sc-min-runners").value || "4", 10),
        max_runners: parseInt(document.getElementById("sc-max-runners").value || "24", 10),
        queued_jobs_threshold: parseInt(document.getElementById("sc-queued-thresh").value || "2", 10),
        provisioned_jobs_threshold: parseInt(document.getElementById("sc-prov-thresh").value || "6", 10),
        hourly_runs_scale_up_threshold: parseInt(document.getElementById("sc-hourly-up-thresh").value || "20", 10),
        cpu_threshold_percent: parseFloat(document.getElementById("sc-cpu-thresh").value || "50"),
        cpu_scope: document.getElementById("sc-cpu-scope").value,
        scale_down_max_jobs_past_hour: parseInt(document.getElementById("sc-hourly-down-max").value || "5", 10),
        scale_down_target_runners: parseInt(document.getElementById("sc-hourly-down-target").value || "4", 10),
        ...(budgetCapsEdited ? {
          max_scale_up_hourly_usd: nextHourlyUsd,
          max_scale_up_daily_usd: nextDailyUsd,
          max_scale_up_monthly_usd: nextMonthlyUsd,
          shared_global_budget: false
        } : {}),
        enable_queued_jobs_trigger: document.getElementById("sc-en-queued").checked,
        enable_provisioned_jobs_trigger: document.getElementById("sc-en-prov").checked,
        enable_hourly_runs_scale_up: document.getElementById("sc-en-hourly-up").checked,
        enable_hourly_runs_scale_down: document.getElementById("sc-en-hourly-down").checked,
        enable_cpu_utilization_trigger: document.getElementById("sc-en-cpu").checked,
        enable_journal_oom_trigger: document.getElementById("sc-en-journal-oom").checked,
        enable_github_ci_oom_trigger: document.getElementById("sc-en-gh-oom").checked
      };
      const telemetry = {
        jobs_ran_past_hour: parseInt(document.getElementById("sim-jobs-hour").value || "14", 10),
        provisioned_jobs: parseInt(document.getElementById("sim-jobs-prov").value || "7", 10),
        queued_jobs: parseInt(document.getElementById("sim-jobs-queued").value || "3", 10)
      };
      const res = await fetch("/api/scaling", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: "cicd_runner", policy, telemetry })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error updating scaling policy: ${data.error || res.statusText}`);
        return false;
      }
      showToast("Updated scaling policy and evaluated live telemetry");
      await loadOverview();
      return true;
    }

    async function saveAndEvaluateBuilderScaling() {
      const curBsc = APP_STATE?.scaling?.rust_builder || {};
      const nextHourlyUsd = parseFloat(document.getElementById("sc-builder-budget-hr").value || String(curBsc.max_scale_up_hourly_usd ?? 3.5));
      const nextDailyUsd = parseFloat(document.getElementById("sc-builder-budget-day").value || String(curBsc.max_scale_up_daily_usd ?? 12));
      const nextMonthlyUsd = parseFloat(document.getElementById("sc-builder-budget-mo").value || String(curBsc.max_scale_up_monthly_usd ?? 80));
      const budgetCapsEdited = (
        Math.abs(nextHourlyUsd - Number(curBsc.max_scale_up_hourly_usd ?? 3.5)) > 1e-6 ||
        Math.abs(nextDailyUsd - Number(curBsc.max_scale_up_daily_usd ?? 12)) > 1e-6 ||
        Math.abs(nextMonthlyUsd - Number(curBsc.max_scale_up_monthly_usd ?? 80)) > 1e-6
      );
      const policy = {
        min_instances: parseInt(document.getElementById("sc-builder-min-inst").value || "1", 10),
        max_instances: parseInt(document.getElementById("sc-builder-max-inst").value || "6", 10),
        queued_builds_threshold: parseInt(document.getElementById("sc-builder-queued-thresh").value || "2", 10),
        cpu_threshold_percent: parseFloat(document.getElementById("sc-builder-cpu-thresh").value || "60"),
        cpu_scope: curBsc.cpu_scope || "majority",
        scale_down_max_builds_past_hour: parseInt(document.getElementById("sc-builder-down-max").value || "2", 10),
        scale_down_target_instances: parseInt(document.getElementById("sc-builder-down-target").value || "1", 10),
        ...(budgetCapsEdited ? {
          max_scale_up_hourly_usd: nextHourlyUsd,
          max_scale_up_daily_usd: nextDailyUsd,
          max_scale_up_monthly_usd: nextMonthlyUsd,
          shared_global_budget: false
        } : {}),
        enable_queue_trigger: document.getElementById("sc-builder-en-queue").checked,
        enable_cpu_utilization_trigger: document.getElementById("sc-builder-en-cpu").checked,
        enable_journal_oom_trigger: document.getElementById("sc-builder-en-oom").checked,
        enable_low_builds_scale_down: document.getElementById("sc-builder-en-down").checked
      };
      const telemetry = {
        builds_ran_past_hour: parseInt(document.getElementById("sim-builds-hour").value || "6", 10),
        active_builds: parseInt(document.getElementById("sim-builds-active").value || "1", 10),
        queued_builds: parseInt(document.getElementById("sim-builds-queued").value || "0", 10)
      };
      const res = await fetch("/api/scaling", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: "rust_builder", policy, telemetry })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error updating Rust builder scaling policy: ${data.error || res.statusText}`);
        return false;
      }
      showToast("Updated Rust builder scaling policy and evaluated live telemetry");
      await loadOverview();
      return true;
    }

    async function simulateLowDemandScaleDown() {
      document.getElementById("sim-jobs-hour").value = 5;
      document.getElementById("sim-jobs-prov").value = 0;
      document.getElementById("sim-jobs-queued").value = 0;
      document.getElementById("sc-en-gh-oom").checked = false;
      document.getElementById("sc-en-journal-oom").checked = false;
      document.getElementById("sc-en-cpu").checked = false;
      const ok = await saveAndEvaluateScaling();
      if (ok) {
        showToast("Simulated <=5 CI/CD jobs in past hour -> scaled down target to minimum runner floor");
      }
    }

    async function addGithubOomRun() {
      // Send only what the operator entered; the server rejects a run without its location or log line.
      const field = id => document.getElementById(id).value.trim();
      const repo = field("new-oom-repo");
      const run_id = field("new-oom-runid");
      const job_id = field("new-oom-jobid");
      const step_number = field("new-oom-step");
      const line_number = field("new-oom-line");
      const runner_name = field("new-oom-runner");
      const error_excerpt = field("new-oom-excerpt");
      const res = await fetch("/api/scaling", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          add_github_oom_run: {
            repository: repo,
            run_id,
            job_id,
            step_number,
            line_number,
            ...(runner_name ? { runner_name } : {}),
            error_excerpt
          }
        })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error adding GitHub OOM deep-link: ${data.error || res.statusText}`);
        return;
      }
      showToast(`Added GitHub OOM deep-link for ${repo} run #${run_id}`);
      await loadOverview();
    }

    // =========================================================================
    // TAB 5: CLOUD-INIT & VM TEMPLATE STUDIO
    // =========================================================================
    function populateCloudInitStudio() {
      const sel = document.getElementById("ci-editor-select");
      const currentVal = sel.value || APP_STATE.cloud_init_templates[0]?.id;
      sel.innerHTML = APP_STATE.cloud_init_templates.map(t =>
        `<option value="${esc(t.id)}" ${t.id === currentVal ? 'selected' : ''}>${esc(t.name)} (${t.role === 'cicd_runner' ? 'Runner' : 'Rust Builder'})</option>`
      ).join("");
      loadSelectedCloudInitIntoEditor();

      // Source instance dropdown for VM Template creation
      const srcSel = document.getElementById("vmtpl-source-instance");
      srcSel.innerHTML = APP_STATE.instances_view.instances.map(i =>
        `<option value="${esc(i.instance_id)}">${esc(i.name)} (${esc(i.machine_type)} · ${esc(i.zone)})</option>`
      ).join("");

      // VM Templates list
      document.getElementById("vm-templates-list").innerHTML = APP_STATE.vm_templates.map(tpl => `
        <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:8px; padding:10px 12px; display:flex; justify-content:space-between; align-items:center; gap:8px;">
          <div>
            <div style="font-weight:700; color:#fff;">${esc(tpl.name)} <span class="mono" style="font-size:11px; color:var(--text-muted);">(${esc(tpl.id)})</span></div>
            <div style="font-size:11.5px; color:var(--text-muted);">Shape: <code class="mono">${esc(tpl.machine_type)}</code> (${tpl.vcpu} vCPU / ${tpl.ram_gib} GiB · ${esc(tpl.cpu_platform)} · Bench ${tpl.benchmark_score}) · Disk: <code class="mono">${tpl.data_disk_gb}GB ${esc(tpl.data_disk_type)}</code> (${esc(tpl.disk_category)}) · ${tpl.source_instance ? `Source: <code class="mono">${esc(tpl.source_instance)}</code>` : 'Preset shape'}</div>
          </div>
          <span class="badge ${tpl.role === 'cicd_runner' ? 'badge-runner' : 'badge-builder'}">${tpl.role === 'cicd_runner' ? 'Runner Template' : 'Builder Template'}</span>
        </div>
      `).join("");
    }

    function loadSelectedCloudInitIntoEditor() {
      const id = document.getElementById("ci-editor-select").value;
      const tpl = APP_STATE.cloud_init_templates.find(t => t.id === id) || APP_STATE.cloud_init_templates[0];
      if (!tpl) return;
      document.getElementById("ci-editor-role").value = tpl.role;
      document.getElementById("ci-editor-name").value = tpl.name;
      document.getElementById("ci-editor-textarea").value = tpl.content;
      renderCloudInitVisualizer(tpl.parsed);
    }

    function openSelectedCloudInit(role) {
      const selectId = role === "cicd_runner" ? "runner-cloudinit-select" : "builder-cloudinit-select";
      const tplId = document.getElementById(selectId).value;
      switchTab("tab-cloudinit");
      document.getElementById("ci-editor-select").value = tplId;
      loadSelectedCloudInitIntoEditor();
    }

    async function liveParseCloudInit() {
      const content = document.getElementById("ci-editor-textarea").value;
      const res = await fetch("/api/cloud-init/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content })
      });
      const parsed = await res.json();
      renderCloudInitVisualizer(parsed);
    }

    function renderCloudInitVisualizer(parsed) {
      if (!parsed) return;
      const badge = document.getElementById("ci-valid-badge");
      const errList = parsed.errors || [];
      const warnList = parsed.warnings || [];
      badge.className = parsed.valid ? "badge badge-running" : "badge badge-warn";
      badge.textContent = parsed.valid
        ? `✓ Valid ${parsed.format} (${parsed.line_count} lines)`
        : `⚠️ ${errList.length + warnList.length} Syntax / Advisory Issue(s)`;

      const warnBox = document.getElementById("ci-warnings-box");
      const errHtml = errList.map(e =>
        `<div style="background:rgba(244, 63, 94, 0.16); border:1px solid rgba(244, 63, 94, 0.45); color:#fda4af; padding:7px 10px; border-radius:7px; font-size:12px; margin-bottom:6px;">❌ ${esc(e)}</div>`
      );
      const warnHtml = warnList.map(w =>
        `<div style="background:rgba(245, 158, 11, 0.14); border:1px solid rgba(245, 158, 11, 0.4); color:#fde68a; padding:7px 10px; border-radius:7px; font-size:12px; margin-bottom:6px;">⚠️ ${esc(w)}</div>`
      );
      warnBox.innerHTML = [...errHtml, ...warnHtml].join("");

      const stagesBox = document.getElementById("ci-visualizer-stages");
      stagesBox.innerHTML = (parsed.stages || []).map(st => `
        <div class="stage-card">
          <div class="stage-card-header">
            <span>Stage ${esc(st.stage)}: ${esc(st.title)}</span>
            <span class="badge badge-runner">${esc(st.summary)}</span>
          </div>
          <div class="stage-items">
            ${(st.items || []).map(item => `<span class="stage-chip">${esc(item)}</span>`).join("") || '<span style="font-size:11.5px; color:var(--text-muted);">No items in this stage</span>'}
          </div>
        </div>
      `).join("");
    }

    async function saveCloudInit(mode) {
      const id = document.getElementById("ci-editor-select").value;
      const role = document.getElementById("ci-editor-role").value;
      let name = document.getElementById("ci-editor-name").value.trim();
      if (mode === "save_as" && !name.toLowerCase().includes("custom") && name === (APP_STATE.cloud_init_templates.find(t => t.id === id)?.name || "")) {
        name = name + " (Custom Copy)";
      }
      const content = document.getElementById("ci-editor-textarea").value;
      const assign = document.getElementById("ci-assign-active").checked;
      const res = await fetch("/api/cloud-init/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, mode, role, name, content, assign_to_profile: assign })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error saving Cloud-Init template: ${data.error || res.statusText}`);
        return;
      }
      if (data.template) {
        showToast(`${mode === 'save_as' ? 'Saved new Cloud-Init template' : 'Updated Cloud-Init template'}: "${data.template.name}"`);
        await loadOverview();
        document.getElementById("ci-editor-select").value = data.template.id;
        loadSelectedCloudInitIntoEditor();
      }
    }

    async function createVmTemplateFromSelected() {
      const instance_id = document.getElementById("vmtpl-source-instance").value;
      const template_name = document.getElementById("vmtpl-new-name").value.trim();
      const res = await fetch("/api/vm-templates/from-instance", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance_id, template_name })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error creating VM Template: ${data.error || res.statusText}`);
        return;
      }
      document.getElementById("vmtpl-new-name").value = "";
      showToast("Created new VM Template from instance");
      await loadOverview();
    }

    // =========================================================================
    // TAB 6: WORLD REGIONS & LATENCY FILTER
    // =========================================================================
    function getFilteredRegions() {
      const maxLat = parseFloat(document.getElementById("reg-max-latency").value || "250");
      const cont = document.getElementById("reg-continent-filter").value;
      const fam = document.getElementById("reg-family-filter").value;
      const cpu = document.getElementById("reg-cpu-filter").value;
      const disk = document.getElementById("reg-disk-filter").value;

      return APP_STATE.regions.filter(r => {
        if (r.latency_ms > maxLat) return false;
        if (cont && r.continent !== cont) return false;
        if (fam && !r.families.includes(fam)) return false;
        if (cpu && !r.cpu_platforms.includes(cpu)) return false;
        if (disk && !r.disk_types.includes(disk)) return false;
        return true;
      });
    }

    function renderRegionsTable() {
      const filtered = getFilteredRegions();
      const tbody = document.getElementById("regions-table-body");
      tbody.innerHTML = filtered.map(r => `
        <tr>
          <td>
            <label class="toggle-label">
              <input type="checkbox" ${r.enabled ? "checked" : ""} onchange="toggleSingleRegion('${esc(r.region)}', this.checked)" />
              <span>${r.enabled ? "Enabled" : "Deselected"}</span>
            </label>
          </td>
          <td class="mono" style="font-weight:700; color:#fff;">
            ${esc(r.region)}
            <div style="font-size:11px; color:var(--text-muted); font-weight:400;">${esc(r.zones.join(", "))}</div>
          </td>
          <td>
            <div style="font-weight:600;">${esc(r.display_name)}</div>
            <div style="font-size:11px; color:var(--text-muted);">${esc(r.continent)}</div>
          </td>
          <td class="mono" style="font-weight:700; color:${r.latency_ms < 60 ? '#34d399' : (r.latency_ms < 140 ? '#38bdf8' : '#fbbf24')};">
            ${r.latency_ms} ms
          </td>
          <td class="mono" style="font-size:11.5px;">${esc(r.families.join(", "))}</td>
          <td style="font-size:11.5px;">${esc(r.cpu_platforms.join(", "))}</td>
          <td class="mono" style="font-size:11px;">${esc(r.disk_types.join(", "))}</td>
          <td class="mono">${r.running_instances.length} running</td>
        </tr>
      `).join("");
    }

    async function toggleSingleRegion(region, enabled) {
      const res = await fetch("/api/regions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ regions_enabled: { [region]: enabled } })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error updating region ${region}: ${data.error || res.statusText}`);
        await loadOverview();
        return;
      }
      showToast(`${enabled ? 'Enabled' : 'Deselected'} region ${region}`);
      await loadOverview();
    }

    async function setAllFilteredRegions(enabled) {
      const filtered = getFilteredRegions();
      const map = {};
      filtered.forEach(r => { map[r.region] = enabled; });
      const res = await fetch("/api/regions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ regions_enabled: map })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error updating regions: ${data.error || res.statusText}`);
        return;
      }
      showToast(`${enabled ? 'Enabled' : 'Deselected'} ${filtered.length} matching region(s)`);
      await loadOverview();
    }

    async function deselectHighLatencyRegions() {
      const maxLat = parseFloat(document.getElementById("reg-max-latency").value || "150");
      const res = await fetch("/api/regions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ deselect_matching: { min_latency_ms_to_disable: maxLat } })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error deselecting regions: ${data.error || res.statusText}`);
        return;
      }
      showToast(`Deselected all regions with latency > ${maxLat} ms`);
      await loadOverview();
    }

    async function probeRegionLatencies() {
      showToast("Probing live TCP latency from backend to GCP regions...");
      const res = await fetch("/api/regions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ measure_latency: true })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error probing latencies: ${data.error || res.statusText}`);
        return;
      }
      await loadOverview();
      showToast("Updated live regional latencies");
    }

    // =========================================================================
    // TAB 7: RUST REMOTE BUILDERS TELEMETRY & IP GEOLOCATION / VPN / TOR
    // =========================================================================
    function renderRustBuildsView() {
      const rb = APP_STATE.rust_builds;
      document.getElementById("rust-kpi-grid").innerHTML = `
        <div class="kpi-card">
          <div class="kpi-label">Total Rust Remote Builds</div>
          <div class="kpi-value">${rb.total_builds} Builds</div>
          <div class="kpi-sub">Avg build duration: ${rb.average_duration_seconds}s</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Crate Cache Hits &amp; Rate</div>
          <div class="kpi-value" style="color:#34d399;">${rb.total_cache_hits.toLocaleString()} Hits (${rb.overall_cache_hit_rate_percent}%)</div>
          <div class="kpi-sub">Persistent NVMe /mnt/runner-data/remote-build</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Peak Active Memory (OOM Floor)</div>
          <div class="kpi-value" style="color:#fbbf24;">${rb.max_peak_ram_gib} GiB</div>
          <div class="kpi-sub">Active RAM required without OOM killing</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Total CPU Time &amp; CPU Points</div>
          <div class="kpi-value" style="color:#38bdf8;">${rb.total_cpu_points.toLocaleString()} pts</div>
          <div class="kpi-sub">${rb.total_cpu_time_seconds.toLocaleString()}s CPU time × Instance Bench</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">VPN / Proxy / Tor Origins</div>
          <div class="kpi-value" style="color:#fda4af;">${rb.vpn_or_tor_count} Flagged</div>
          <div class="kpi-sub">ProtonVPN, Tor Exit Node &amp; Mullvad detected</div>
        </div>
      `;
      renderRustBuildsTable();
    }

    function renderRustBuildsTable() {
      const q = (document.getElementById("rust-search-input").value || "").toLowerCase().trim();
      const builds = (APP_STATE.rust_builds.builds || []).filter(b => {
        if (!q) return true;
        const hay = `${b.build_id} ${b.project_name} ${b.instance_name} ${b.origin_ip} ${b.origin_hostname} ${b.geolocation.city} ${b.geolocation.country} ${b.network_security.provider_label} ${b.network_security.badge_text}`.toLowerCase();
        return hay.includes(q);
      });

      const tbody = document.getElementById("rust-builds-table-body");
      tbody.innerHTML = builds.map(b => {
        const ns = b.network_security;
        const badgeClass = ns.is_tor ? "badge-tor" : (ns.is_vpn ? "badge-vpn" : "badge-direct");
        return `
          <tr>
            <td>
              <div class="mono" style="font-weight:700; color:#fff;">${esc(b.build_id)}</div>
              <div style="font-size:11.5px; color:var(--text-muted);">${esc(b.project_name)} (<code class="mono">--${esc(b.profile)}</code>) · <code class="mono">${esc((b.commit_sha || "").slice(0, 8))}</code></div>
            </td>
            <td style="font-size:11.5px;">
              <div><strong>Req:</strong> ${esc(formatLocalTime(b.requested_at))}</div>
              <div><strong>Fin:</strong> ${esc(formatLocalTime(b.finished_at))}</div>
              <div><strong>Del:</strong> ${esc(formatLocalTime(b.delivered_at))}</div>
            </td>
            <td class="mono">
              <div style="font-weight:700; color:#fff;">${b.total_duration_seconds}s total</div>
              <div style="font-size:11px; color:var(--text-muted);">${b.compile_duration_seconds}s compile + ${b.delivery_duration_seconds}s delivery</div>
            </td>
            <td class="mono">
              <div style="font-weight:700; color:#34d399;">${b.cache_hits} / ${b.total_crates} (${b.cache_hit_rate_percent}%)</div>
              <div style="font-size:11px; color:var(--text-muted);">${b.cache_misses} misses</div>
            </td>
            <td>
              <div class="mono" style="font-weight:700; color:#c4b5fd;">${esc(b.instance_name)}</div>
              <div style="font-size:11px; color:var(--text-muted);">${esc(b.machine_type)} (${esc(b.zone)}) · Bench: <strong class="mono">${b.instance_benchmark_score}</strong></div>
            </td>
            <td class="mono">
              <div style="font-weight:700; color:#f8fafc;">${esc(b.origin_ip)}</div>
              <div style="font-size:11px; color:#93c5fd;">${esc(b.origin_hostname)}</div>
            </td>
            <td>
              <div style="font-weight:600;">${esc(b.geolocation.city)}, ${esc(b.geolocation.country)} (${esc(b.geolocation.country_code)})</div>
              <div style="font-size:11px; color:var(--text-muted);">${esc(b.geolocation.asn)} · ${esc(b.geolocation.isp)}</div>
            </td>
            <td>
              <span class="badge ${badgeClass}">${esc(ns.badge_text)}</span>
            </td>
            <td class="mono">
              <div style="font-weight:700; color:#fbbf24;">${b.peak_ram_gib.toFixed(2)} GiB active</div>
              <div style="font-size:10.5px; color:var(--text-muted);">OOM floor (cap ${b.container_ram_cap_gib} GiB)</div>
            </td>
            <td class="mono" style="font-weight:700;">
              ${b.cpu_time_seconds.toLocaleString()} CPU-s
            </td>
            <td class="mono">
              <div style="font-size:13.5px; font-weight:700; color:#38bdf8;">${b.cpu_points.toLocaleString()} pts</div>
              <div style="font-size:10.5px; color:var(--text-muted);">${b.cpu_time_seconds}s × ${b.instance_benchmark_score} bench</div>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function submitNewRustBuild() {
      const instName = document.getElementById("rb-new-instance").value;
      const isEu = instName.includes("eu");
      const payload = {
        project_name: document.getElementById("rb-new-project").value.trim() || "oxidex",
        origin_ip: document.getElementById("rb-custom-ip").value.trim() || "185.159.157.88",
        instance_name: instName,
        zone: isEu ? "europe-west4-a" : "us-south1-b",
        machine_type: isEu ? "n4-highcpu-16" : "c4d-highcpu-16",
        cpu_platform: isEu ? "Intel Emerald Rapids" : "AMD Turin",
        vcpu: 16,
        cache_hits: parseInt(document.getElementById("rb-new-hits").value || "418", 10),
        total_crates: parseInt(document.getElementById("rb-new-total").value || "430", 10),
        peak_ram_gib: parseFloat(document.getElementById("rb-new-ram").value || "22.4"),
        cpu_time_seconds: parseFloat(document.getElementById("rb-new-cputime").value || "960"),
        total_duration_seconds: 88.5
      };
      const res = await fetch("/api/rust-builds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error recording Rust build: ${data.error || res.statusText}`);
        return;
      }
      if (data.build) {
        showToast(`Recorded Rust build ${data.build.build_id}: ${data.build.network_security.badge_text} · ${data.build.cpu_points.toLocaleString()} CPU points`);
        await loadOverview();
      }
    }

    // =========================================================================
    // ON-THE-FLY BUDGET BAR
    // =========================================================================
    async function applyTopBudget() {
      const payload = {
        target: "global",
        active_period: "day",
        max_hourly_usd: parseFloat(document.getElementById("top-budget-hour").value || "5.00"),
        max_daily_usd: parseFloat(document.getElementById("top-budget-day").value || "10.00"),
        max_monthly_usd: parseFloat(document.getElementById("top-budget-month").value || "50.00"),
        include_disk_costs: document.getElementById("top-budget-disk").checked,
        include_taxes: document.getElementById("top-budget-disk").checked,
        tax_rate_percent: 8.25
      };
      const res = await fetch("/api/budget", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showToast(`Error updating budget: ${data.error || res.statusText}`);
        return;
      }
      showToast(`Updated live billing budget: $${payload.max_hourly_usd.toFixed(2)}/hr · $${payload.max_daily_usd.toFixed(2)}/day · $${payload.max_monthly_usd.toFixed(2)}/mo`);
      await loadOverview();
    }

    // --- runner patches panel ---
    // Renders server data with textContent and createElement only (no HTML parsing). Requests go through the
    // dashboard fetch wrapper, so every POST carries an Idempotency-Key and waits out control VM restarts.
    const RunnerPatches = (function () {
      const S = { list: null, current: null, preview: null, dirty: false, pendingExternal: null };
      function el(id) { return document.getElementById(id); }
      function node(tag, text) {
        const n = document.createElement(tag);
        if (text !== undefined && text !== null) n.textContent = String(text);
        return n;
      }
      function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }
      function setStatus(text) { el("rp-status").textContent = text || ""; }
      async function api(url, init) {
        const res = await fetch(url, init || {});
        let data = {};
        try { data = await res.json(); } catch (_e) { data = {}; }
        return [res, data || {}];
      }
      function postJson(url, body, extraHeaders) {
        return api(url, { method: "POST", headers: { "Content-Type": "application/json", ...(extraHeaders || {}) },
                          body: JSON.stringify(body) });
      }
      // One Idempotency-Key per pending external change: reused for the same payload (also when the user clicks
      // again) until a final result arrives, so the dashboard can reconcile instead of opening a second PR.
      async function postExternal(url, body) {
        const fingerprint = url + "\n" + JSON.stringify(body);
        if (!S.pendingExternal || S.pendingExternal.fingerprint !== fingerprint) {
          S.pendingExternal = { fingerprint, key: newIdempotencyKey() };
        }
        const result = await postJson(url, body, { "Idempotency-Key": S.pendingExternal.key });
        const [res, d] = result;
        if (!(d && (d.retry_same_key === true || d.pending_retry === true)) && res.status !== 0) {
          S.pendingExternal = null;  // a final answer: the next change gets a new key
        }
        return result;
      }
      function renderErrors(errors) {
        const box = el("rp-errors");
        clear(box);
        (errors || []).forEach(e => box.appendChild(node("li", `${e.field}: ${e.message}`)));
      }
      function renderEffective(data) {
        el("rp-effective-sha").textContent = data && data.patch_sha256 ? `Effective patch sha256: ${data.patch_sha256}` : "";
        el("rp-effective").textContent = data && data.effective ? JSON.stringify(data.effective, null, 2) : "";
      }
      function updateHotPatchButton() {
        const p = S.preview;
        el("rp-hotpatch").disabled = !(p && p.hot_patch_allowed === true && !p.source.draft && !S.dirty && el("rp-confirm").checked);
      }
      function renderPreview(data) {
        S.preview = data;
        const body = el("rp-hosts");
        clear(body);
        (data.hosts || []).forEach(h => {
          const tr = node("tr");
          [h.name, h.zone, h.current_patch_sha256 ? h.current_patch_sha256.slice(0, 12) : "none",
           h.result ? `${h.classification} -> ${h.result}` : h.classification, h.detail || ""].forEach(v => tr.appendChild(node("td", v)));
          body.appendChild(tr);
        });
        if (!(data.hosts || []).length) {
          const tr = node("tr");
          tr.appendChild(node("td", "No running runner hosts are recorded for this repository."));
          body.appendChild(tr);
        }
        const ex = data.executor || {};
        el("rp-executor").textContent = ex.available ? "" : (ex.reason || "");
        updateHotPatchButton();
      }
      function renderList(data) {
        S.list = data;
        el("rp-source").textContent = `${data.config_repo} @ ${data.default_branch} ${String(data.head_sha || "").slice(0, 12)}`;
        const list = el("rp-list");
        clear(list);
        const entries = [{ label: "defaults.yaml" + (data.defaults && data.defaults.present ? "" : " (new)"), path: "defaults.yaml" }]
          .concat((data.repositories || []).map(r => ({ label: r.repository, path: r.path })));
        entries.forEach(entry => {
          const li = node("li");
          const b = node("button", entry.label);
          b.className = "btn btn-sm btn-secondary";
          b.addEventListener("click", () => load(entry.path));
          li.appendChild(b);
          list.appendChild(li);
        });
        el("rp-executor").textContent = data.executor && !data.executor.available ? (data.executor.reason || "") : "";
      }
      async function refresh() {
        setStatus("Loading manifests...");
        const [res, d] = await api("/api/patches");
        if (!res.ok) { setStatus(d.error || `Could not list manifests (HTTP ${res.status})`); return; }
        renderList(d);
        setStatus("");
      }
      async function load(path) {
        setStatus(`Loading ${path}...`);
        const [res, d] = await api(`/api/patches/manifest?path=${encodeURIComponent(path)}`);
        if (!res.ok) { setStatus(d.error || `Could not load ${path} (HTTP ${res.status})`); return; }
        S.current = d; S.dirty = false; S.preview = null;
        el("rp-path").textContent = `${d.path} @ ${String(d.base_sha || "").slice(0, 12)}${d.exists ? "" : " (new file)"}`;
        el("rp-editor").value = d.content || "";
        renderErrors(d.errors);
        renderEffective(d);
        clear(el("rp-hosts"));
        updateHotPatchButton();
        setStatus("");
      }
      async function validate() {
        if (!S.current) return;
        const [res, d] = await postJson("/api/patches/validate", { path: S.current.path, content: el("rp-editor").value });
        if (!res.ok) { setStatus(d.error || `Validation failed (HTTP ${res.status})`); return; }
        renderErrors(d.errors);
        renderEffective(d);
        setStatus(d.valid ? (d.warning || "The manifest is valid.") : "The manifest has errors.");
      }
      async function preview() {
        if (!S.current || !S.current.repository) { setStatus("Rollout impact applies to repository manifests."); return; }
        const body = { repository: S.current.repository };
        if (S.dirty) body.content = el("rp-editor").value;
        const [res, d] = await postJson("/api/patches/preview", body);
        if (!res.ok) { renderErrors(d.errors); setStatus(d.error || `Preview failed (HTTP ${res.status})`); return; }
        renderPreview(d);
        setStatus(d.source && d.source.draft ? "Preview of your unsaved draft; save it and merge the pull request before hot-patching." : "");
      }
      async function save() {
        if (!S.current) return;
        const [res, d] = await postExternal("/api/patches/save", {
          path: S.current.path, content: el("rp-editor").value, base_sha: S.current.base_sha,
          message: el("rp-message").value,
        });
        if (d.retry_same_key === true) {
          setStatus((d.error || "GitHub did not confirm the save.") + " Click Save again to finish it safely.");
          return;
        }
        if (res.status === 409) { setStatus(d.error || "The config repository changed; reload the manifest."); return; }
        if (!res.ok) { renderErrors(d.errors); setStatus(d.error || `Save failed (HTTP ${res.status})`); return; }
        renderErrors([]);
        const status = el("rp-status");
        clear(status);
        status.appendChild(node("span", `Opened pull request #${d.pull_request && d.pull_request.number} on ${d.branch}. `));
        const url = d.pull_request && d.pull_request.url;
        if (typeof url === "string" && url.startsWith("https://github.com/")) {
          const a = node("a", "Review it on GitHub");
          a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
          status.appendChild(a);
        }
      }
      async function hotPatch() {
        updateHotPatchButton();
        if (el("rp-hotpatch").disabled || !S.preview) return;
        el("rp-hotpatch").disabled = true;
        const [res, d] = await postExternal("/api/patches/hot-patch", {
          repository: S.preview.repository, patch_sha256: S.preview.patch_sha256, confirm: true,
        });
        el("rp-confirm").checked = false;
        if (d.hosts) renderPreview({ ...S.preview, hosts: d.hosts, hot_patch_allowed: false });
        setStatus(res.ok ? `Hot-patch ${d.status}.` : (d.error || `Hot-patch refused (HTTP ${res.status})`));
        updateHotPatchButton();
      }
      function init() {
        el("rp-refresh").addEventListener("click", refresh);
        el("rp-validate").addEventListener("click", validate);
        el("rp-preview").addEventListener("click", preview);
        el("rp-save").addEventListener("click", save);
        el("rp-hotpatch").addEventListener("click", hotPatch);
        el("rp-confirm").addEventListener("change", updateHotPatchButton);
        el("rp-editor").addEventListener("input", () => { S.dirty = true; updateHotPatchButton(); });
      }
      return { init, refresh, load, validate, preview, save, hotPatch, renderErrors, renderPreview, updateHotPatchButton, state: S };
    })();
    window.RunnerPatches = RunnerPatches;
    // --- end runner patches panel ---

    // --- dashboard administrators panel (owners only; /api/admins answers 403 to everyone else) ---
    // Renders server data with textContent and createElement only. Each change needs a confirmation, carries
    // "confirm": true, and reuses its Idempotency-Key until a final answer arrives.
    const DashboardAdmins = (function () {
      const S = { data: null, pending: null, owner: false };
      function el(id) { return document.getElementById(id); }
      function node(tag, text) {
        const n = document.createElement(tag);
        if (text !== undefined && text !== null) n.textContent = String(text);
        return n;
      }
      function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }
      function setStatus(text) { el("adm-status").textContent = text || ""; }
      async function api(url, init) {
        const res = await fetch(url, init || {});
        let data = {};
        try { data = await res.json(); } catch (_e) { data = {}; }
        return [res, data || {}];
      }
      async function postChange(url, body) {
        const fingerprint = url + "\n" + JSON.stringify(body);
        if (!S.pending || S.pending.fingerprint !== fingerprint) {
          S.pending = { fingerprint, key: newIdempotencyKey() };
        }
        const result = await api(url, { method: "POST", body: JSON.stringify(body),
          headers: { "Content-Type": "application/json", "Idempotency-Key": S.pending.key } });
        const [res, d] = result;
        if (!(d && (d.retry_same_key === true || d.pending_retry === true)) && res.status !== 0) {
          S.pending = null;  // a final answer: the next change gets a new key
        }
        return result;
      }
      function render(data) {
        S.data = data;
        const owners = el("adm-owners");
        clear(owners);
        (data.owners || []).forEach(o => {
          const tr = node("tr");
          [o.login, o.uid, o.global_admin ? "Skipped (global administrator)" : "Required"].forEach(v => tr.appendChild(node("td", v)));
          owners.appendChild(tr);
        });
        const admins = el("adm-admins");
        clear(admins);
        (data.admins || []).forEach(a => {
          const tr = node("tr");
          [a.login, a.uid, a.added_by || "", a.added_at ? formatLocalTime(a.added_at) : ""].forEach(v => tr.appendChild(node("td", v)));
          const td = node("td");
          const b = node("button", "Remove");
          b.type = "button";
          b.className = "btn btn-sm btn-danger";
          b.addEventListener("click", () => remove(a));
          td.appendChild(b);
          tr.appendChild(td);
          admins.appendChild(tr);
        });
        if (!(data.admins || []).length) {
          const tr = node("tr");
          const td = node("td", "No administrators added yet.");
          td.colSpan = 5;
          tr.appendChild(td);
          admins.appendChild(tr);
        }
        el("adm-exempt").textContent = data.added_admins_exempt
          ? "Added administrators skip the per-target administrator checks, like the owner."
          : "DASHBOARD_GLOBAL_ADMINS is none: added administrators must administer every controlled target, like the owner.";
        const history = el("adm-history");
        clear(history);
        (data.history || []).slice().reverse().forEach(h => {
          history.appendChild(node("li", `${h.at || ""} ${h.actor || ""} ${h.action || ""} ${h.login || ""} (ID ${h.uid})`));
        });
        el("badge-admins").textContent = String((data.admins || []).length);
        if (data.error) setStatus(data.error);
      }
      async function refresh() {
        if (!S.owner) return;
        setStatus("Loading administrators...");
        const [res, d] = await api("/api/admins");
        if (!res.ok) { setStatus(d.error || `Could not load administrators (HTTP ${res.status})`); return; }
        render(d);
        if (!d.error) setStatus("");
      }
      async function add(ev) {
        if (ev) ev.preventDefault();
        const login = el("adm-login").value.trim().replace(/^@/, "");
        if (!/^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/.test(login)) {
          setStatus("Enter a GitHub username: letters, digits, or hyphens, up to 39 characters.");
          return;
        }
        if (!window.confirm(`Add GitHub user ${login} as a dashboard administrator? They will be able to sign in and change the fleet.`)) return;
        el("adm-add").disabled = true;
        try {
          setStatus(`Looking up ${login} on GitHub...`);
          const [res, d] = await postChange("/api/admins/add", { login, confirm: true });
          if (d.retry_same_key === true) {
            setStatus((d.error || "GitHub did not answer.") + " Click Add again to retry.");
            return;
          }
          if (!res.ok) { setStatus(d.error || `Could not add ${login} (HTTP ${res.status})`); return; }
          el("adm-login").value = "";
          render(d);
          setStatus(`Added ${d.admin.login} (GitHub user ID ${d.admin.uid}).`);
        } finally {
          el("adm-add").disabled = false;
        }
      }
      async function remove(admin) {
        if (!window.confirm(`Remove ${admin.login} (GitHub user ID ${admin.uid})? Their sessions stop working immediately.`)) return;
        const [res, d] = await postChange("/api/admins/remove", { uid: admin.uid, confirm: true });
        if (!res.ok) { setStatus(d.error || `Could not remove ${admin.login} (HTTP ${res.status})`); return; }
        render(d);
        setStatus(`Removed ${d.admin.login} (GitHub user ID ${d.admin.uid}).`);
      }
      async function init() {
        el("adm-refresh").addEventListener("click", refresh);
        el("adm-add-form").addEventListener("submit", add);
        try {
          const res = await fetch("/api/auth/status", { cache: "no-store" });
          const d = res.ok ? await res.json() : {};
          S.owner = Boolean(d && d.owner === true);
        } catch (_e) {
          S.owner = false;
        }
        el("tab-btn-admins").hidden = !S.owner;
        el("tab-admins").hidden = !S.owner;
        if (S.owner) await refresh();
      }
      return { init, refresh, add, remove, render, state: S };
    })();
    window.DashboardAdmins = DashboardAdmins;
    // --- end dashboard administrators panel ---

    window.addEventListener("DOMContentLoaded", async () => {
      RunnerPatches.init();
      DashboardAdmins.init();
      const params = new URLSearchParams(window.location.search);
      if (params.get("show_deleted") === "1") {
        const cb = document.getElementById("inst-show-deleted");
        if (cb) cb.checked = true;
      }
      if (params.get("window")) {
        INST_WINDOW_HOURS = parseFloat(params.get("window")) || 24;
      }
      await loadOverview();
      const requestedTab = params.get("tab");
      if (requestedTab) {
        const normalized = requestedTab === "rustbuilders"
          ? "tab-rust"
          : (requestedTab.startsWith("tab-") ? requestedTab : `tab-${requestedTab}`);
        switchTab(normalized);
      }
    });
  </script>
</body>
</html>
"""
