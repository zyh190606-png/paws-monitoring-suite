(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "paws-monitor-selected-case-v2";
  let refreshSeconds = 5;
  let timer = null;
  let busy = false;
  let selectedCase = loadSelection();

  const stateNames = {
    running: "运行中", completed: "已完成", failed: "失败", stopped: "已停止",
    paused: "已暂停", not_started: "未启动", unknown: "状态未知",
  };

  function loadSelection() {
    try {
      const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      return value && value.monitor_id && value.case_dir ? value : null;
    } catch (_) { return null; }
  }

  function saveSelection(value) {
    selectedCase = value;
    if (value) localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    else localStorage.removeItem(STORAGE_KEY);
    renderTarget();
  }

  function text(id, value) {
    const node = $(id);
    if (node) node.textContent = value == null || value === "" ? "—" : String(value);
  }

  function fmtNumber(value, digits = 2) {
    return value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(digits);
  }

  function fmtBytes(value) {
    const n = Number(value || 0);
    if (!n) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  function fmtAge(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return "—";
    const n = Math.max(0, Math.round(Number(seconds)));
    if (n < 60) return `${n} 秒`;
    if (n < 3600) return `${Math.floor(n / 60)} 分钟`;
    return `${(n / 3600).toFixed(1)} 小时`;
  }

  function fmtDate(value) {
    if (!value) return "—";
    return String(value).replace("T", " ").replace(/([+-]\d\d:\d\d)$/, "");
  }

  function renderTarget(snapshot = null) {
    if (!selectedCase) {
      text("target-name", "尚未选择 PAWS 程序");
      text("target-path", "点击右侧按钮，从当前运行进程中选择");
      return;
    }
    const name = snapshot?.runtime_case_id || selectedCase.runtime_case_id || "PAWS";
    const pid = snapshot?.process?.pid || selectedCase.pid || "—";
    const backend = snapshot?.process_backend || selectedCase.process_backend || "windows";
    const distro = snapshot?.wsl_distro || selectedCase.wsl_distro || "";
    const prefix = backend === "wsl2" ? `WSL/${distro || "Linux"} · ` : "Windows · ";
    text("target-name", `${prefix}${name} · PID ${pid}`);
    text("target-path", snapshot?.case_dir || selectedCase.case_dir);
  }

  function setSignal(state, process, warnings) {
    const live = Boolean(process && process.alive);
    const healthy = live && !warnings?.some((item) => item.level === "critical");
    const dot = $("signal-dot");
    dot.style.background = healthy ? "var(--cyan)" : live ? "var(--amber)" : "var(--red)";
    dot.style.boxShadow = healthy ? "0 0 0 0 rgba(98,213,209,.7)" : "none";
    text("signal-status", stateNames[state] || state || "等待选择");
    text("signal-detail", live ? `指定 PID ${process.pid} 正在运行` : selectedCase ? "未检测到所选 PAWS 进程" : "请先选择运行中的 PAWS");
  }

  function renderWarnings(items) {
    const root = $("warnings");
    root.replaceChildren();
    if (!items?.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = selectedCase ? "暂无告警 · 输出与进程状态正常" : "等待选择监控目标";
      root.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = `warning ${item.level === "critical" ? "critical" : ""}`;
      const code = document.createElement("span");
      code.className = "warning-code";
      code.textContent = item.code || "WARNING";
      const message = document.createElement("span");
      message.textContent = item.message || "未说明的告警";
      row.append(code, message);
      root.appendChild(row);
    });
  }

  function renderFiles(files) {
    const body = $("files-body");
    body.replaceChildren();
    const existing = (files || []).filter((file) => file.exists);
    text("file-summary", files?.length ? `${existing.length}/${files.length} 个文件存在` : "—");
    (files || []).forEach((file) => {
      const row = document.createElement("tr");
      const values = [file.name, file.exists ? fmtBytes(file.bytes) : "不存在", file.mtime ? fmtDate(file.mtime) : "—", file.exists ? fmtAge(file.age_seconds) : "—"];
      values.forEach((value, index) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        if (index === 3 && file.exists) cell.className = Number(file.age_seconds) < 120 ? "age-fresh" : Number(file.age_seconds) > 1800 ? "age-old" : "";
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
  }

  function renderChart(history) {
    const points = (history || []).filter((point) => point.progress_pct != null);
    const line = $("history-line"), area = $("history-area"), dots = $("history-points");
    dots.replaceChildren();
    if (!points.length) { line.setAttribute("d", ""); area.setAttribute("d", ""); text("chart-first", "—"); text("chart-last", "—"); return; }
    const x0 = 48, x1 = 700, y0 = 200, y1 = 20;
    const coords = points.map((point, index) => {
      const x = points.length === 1 ? x1 : x0 + ((x1 - x0) * index / (points.length - 1));
      const pct = Math.max(0, Math.min(100, Number(point.progress_pct)));
      return { x, y: y0 - ((y0 - y1) * pct / 100) };
    });
    const path = coords.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
    line.setAttribute("d", path);
    area.setAttribute("d", `${path} L ${coords.at(-1).x.toFixed(1)} ${y0} L ${coords[0].x.toFixed(1)} ${y0} Z`);
    coords.filter((_, i) => coords.length <= 55 || i % Math.ceil(coords.length / 55) === 0).forEach((p) => {
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("cx", p.x.toFixed(1)); circle.setAttribute("cy", p.y.toFixed(1)); circle.setAttribute("r", "2.2");
      dots.appendChild(circle);
    });
    text("chart-first", fmtDate(points[0].wall_time)); text("chart-last", fmtDate(points.at(-1).wall_time));
  }

  function render(snapshot) {
    const progress = snapshot.progress || {}, speed = snapshot.speed || {}, process = snapshot.process || {}, health = snapshot.health || {};
    const pct = progress.progress_pct == null ? 0 : Math.max(0, Math.min(100, Number(progress.progress_pct)));
    text("state-label", stateNames[snapshot.state] || snapshot.state || "连接中");
    text("sampled-at", fmtDate(snapshot.sampled_at));
    text("sim-date", progress.current ? fmtDate(progress.current) : "等待输出");
    text("model-start", fmtDate(snapshot.model?.start).slice(0, 10)); text("model-end", fmtDate(snapshot.model?.end).slice(0, 10));
    text("progress-percent", fmtNumber(progress.progress_pct, 2));
    text("elapsed-sim", progress.sim_elapsed_days == null ? "— 模拟日已完成" : `${fmtNumber(progress.sim_elapsed_days, 1)} 模拟日已完成`);
    text("remaining-sim", progress.remaining_days == null ? "— 模拟日剩余" : `${fmtNumber(progress.remaining_days, 1)} 模拟日剩余`);
    text("clock-source", progress.source ? `· ${progress.source}` : "· waiting");
    $("progress-fill").style.width = `${pct}%`; $("progress-marker").style.left = `${pct}%`; $("progress-track").setAttribute("aria-valuenow", String(pct));
    text("pid", process.pid); text("memory", process.working_set_bytes ? fmtBytes(process.working_set_bytes) : "—");
    text("stderr-size", health.stderr_bytes ? fmtBytes(health.stderr_bytes) : "0 B"); text("progress-age", fmtAge(progress.last_update_age_seconds));
    text("avg-speed", fmtNumber(speed.average_sim_days_per_wall_hour, 2)); text("recent-speed", fmtNumber(speed.recent_sim_days_per_wall_hour, 2));
    text("eta", speed.eta_text); text("wall-elapsed", `墙钟运行 ${speed.wall_elapsed_text || "—"}`); text("finish-at", `预计完成 ${fmtDate(speed.expected_finish_at)}`);
    text("case-label", snapshot.case_dir); renderTarget(snapshot); setSignal(snapshot.state, process, snapshot.warnings || []);
    renderWarnings(snapshot.warnings || []); renderFiles(snapshot.files || []); renderChart(snapshot.history || []);
    $("stdout-log").textContent = (snapshot.logs?.stdout || []).join("\n") || "等待输出…";
    $("stderr-log").textContent = (snapshot.logs?.stderr || []).join("\n") || "空";
    document.title = `${snapshot.runtime_case_id} · ${fmtNumber(progress.progress_pct, 1)}% · PAWS`;
  }

  function renderUnselected(message = "请点击上方按钮选择运行中的 PAWS") {
    text("state-label", "等待选择"); text("sampled-at", "—"); text("sim-date", "等待选择");
    text("model-start", "—"); text("model-end", "—"); text("progress-percent", "—");
    text("elapsed-sim", "— 模拟日已完成"); text("remaining-sim", "— 模拟日剩余"); text("clock-source", "· manual selection");
    $("progress-fill").style.width = "0%"; $("progress-marker").style.left = "0%";
    ["pid", "memory", "stderr-size", "progress-age", "avg-speed", "recent-speed", "eta"].forEach((id) => text(id, "—"));
    text("wall-elapsed", "墙钟运行 —"); text("finish-at", "预计完成 —"); text("case-label", "未选择");
    text("signal-status", "等待选择"); text("signal-detail", message); renderWarnings([]); renderFiles([]); renderChart([]);
    $("stdout-log").textContent = "选择目标后显示日志"; $("stderr-log").textContent = "—"; renderTarget(); document.title = "PAWS · 选择监控目标";
  }

  async function refresh() {
    if (busy || !selectedCase) { if (!selectedCase) renderUnselected(); return; }
    busy = true; $("refresh-button").classList.add("is-busy");
    try {
      const params = new URLSearchParams({
        case_id: selectedCase.monitor_id,
        case_dir: selectedCase.case_dir,
        process_backend: selectedCase.process_backend || "windows",
        wsl_distro: selectedCase.wsl_distro || "",
        pid: selectedCase.pid || "",
      });
      const response = await fetch(`/api/status?${params}`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      render(payload);
    } catch (error) {
      text("state-label", "目标不可用"); text("signal-status", "目标不可用"); text("signal-detail", error.message || "无法读取所选 PAWS");
      $("signal-dot").style.background = "var(--red)"; $("signal-dot").style.animation = "none";
    } finally { busy = false; $("refresh-button").classList.remove("is-busy"); }
  }

  function closeModal() { $("target-modal").hidden = true; }

  function choose(row) {
    if (!row.selectable || !row.case_dir) return;
    saveSelection({
      monitor_id: row.monitor_id,
      case_dir: row.case_dir,
      runtime_case_id: row.runtime_case_id,
      pid: row.pid,
      process_backend: row.process_backend || "windows",
      wsl_distro: row.wsl_distro || null,
    });
    closeModal(); refresh();
  }

  function renderChoices(rows) {
    const root = $("case-list"); root.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("div"); empty.className = "case-empty"; empty.textContent = "当前没有找到可确认运行目录的 Windows/WSL PAWS"; root.appendChild(empty);
    }
    rows.forEach((row) => {
      const button = document.createElement("button"); button.type = "button"; button.className = "case-choice";
      if (!row.selectable) button.disabled = true;
      if (selectedCase?.monitor_id === row.monitor_id) button.classList.add("selected");
      const led = document.createElement("span"); led.className = "case-led"; led.textContent = row.process_alive ? "LIVE" : "—";
      const main = document.createElement("span"); main.className = "case-main";
      const strong = document.createElement("strong"); strong.textContent = row.label;
      const path = document.createElement("span"); path.textContent = row.case_dir || row.reason || "未识别运行目录"; main.append(strong, path);
      const now = document.createElement("span"); now.className = "case-now"; now.innerHTML = `<b>${row.current ? fmtDate(row.current) : "等待日期"}</b>${row.selectable ? "点击选择" : "不可选择"}`;
      button.append(led, main, now); button.addEventListener("click", () => choose(row)); root.appendChild(button);
    });
    const selectable = rows.filter((row) => row.selectable).length;
    text("case-count", `发现 ${rows.length} 个 PAWS 进程，${selectable} 个可选择`);
  }

  async function loadChoices() {
    $("case-list").innerHTML = '<div class="case-loading">正在读取运行中的 PAWS…</div>';
    text("case-count", "正在读取");
    try {
      const response = await fetch("/api/cases", { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      renderChoices(payload.cases || []);
    } catch (error) {
      const root = $("case-list"); root.replaceChildren();
      const empty = document.createElement("div"); empty.className = "case-empty"; empty.textContent = `读取失败：${String(error.message || error)}`; root.appendChild(empty);
      text("case-count", "读取失败");
    }
  }

  async function openModal() { $("target-modal").hidden = false; await loadChoices(); }

  async function boot() {
    try {
      const response = await fetch("/api/bootstrap", { cache: "no-store" }); const bootstrap = await response.json();
      refreshSeconds = Math.max(2, Number(bootstrap.refresh_seconds || 5)); text("refresh-label", `每 ${refreshSeconds} 秒刷新`);
    } catch (_) { /* status/选择器会显示具体错误 */ }
    renderTarget(); if (selectedCase) await refresh(); else renderUnselected();
    timer = setInterval(refresh, refreshSeconds * 1000);
  }

  $("refresh-button").addEventListener("click", () => selectedCase ? refresh() : openModal());
  $("choose-target").addEventListener("click", openModal); $("close-target-modal").addEventListener("click", closeModal); $("scan-again").addEventListener("click", loadChoices);
  $("target-modal").addEventListener("click", (event) => { if (event.target === $("target-modal")) closeModal(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeModal(); });
  boot();
})();
