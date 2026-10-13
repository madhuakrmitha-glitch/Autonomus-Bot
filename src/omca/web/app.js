/* OMCA web UI client. No framework, no external requests: everything talks to this coordinator. */
(() => {
  "use strict";

  // ---------------------------------------------------------------------------------------- icons
  const ICONS = {
    plus: ["M12 5v14M5 12h14"],
    search: [{ c: [11, 11, 7] }, "m20 20-3.5-3.5"],
    x: ["M18 6 6 18M6 6l12 12"],
    send: ["M22 2 11 13", "M22 2 15 22l-4-9-9-4 20-7z"],
    paperclip: ["m21.4 11.1-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"],
    sun: [{ c: [12, 12, 4] }, "M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"],
    moon: ["M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"],
    monitor: [{ r: [2, 3, 20, 14, 2] }, "M8 21h8M12 17v4"],
    bell: ["M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9", "M10.3 21a1.9 1.9 0 0 0 3.4 0"],
    "bell-off": ["M6 8a6 6 0 0 1 9.3-5M18 8c0 7 3 9 3 9H9", "M10.3 21a1.9 1.9 0 0 0 3.4 0", "M2 2l20 20"],
    command: ["M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3"],
    settings: ["M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"],
    menu: ["M4 6h16M4 12h16M4 18h16"],
    panel: [{ r: [3, 3, 18, 18, 2] }, "M15 3v18"],
    "arrow-down": ["M12 5v14M19 12l-7 7-7-7"],
    "arrow-left": ["M19 12H5M12 19l-7-7 7-7"],
    upload: ["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"],
    download: ["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"],
    book: ["M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5v14z", "M20 17v4H6.5A2.5 2.5 0 0 1 4 18.5"],
    video: [{ r: [2, 5, 14, 14, 2] }, "M22 8l-6 4 6 4V8z"],
    globe: [{ c: [12, 12, 10] }, "M2 12h20M12 2a15 15 0 0 1 4 10 15 15 0 0 1-4 10 15 15 0 0 1-4-10 15 15 0 0 1 4-10z"],
    briefcase: [{ r: [2, 7, 20, 14, 2] }, "M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"],
    sparkles: ["M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z", "M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"],
    pause: ["M7 4h3v16H7zM14 4h3v16h-3z"],
    play: ["M6 4l14 8-14 8V4z"],
    stop: [{ c: [12, 12, 10] }, "M15 9l-6 6M9 9l6 6"],
    shield: ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"],
    check: ["M20 6 9 17l-5-5"],
    clock: [{ c: [12, 12, 10] }, "M12 6v6l4 2"],
    reply: ["M9 17l-5-5 5-5", "M20 18v-2a4 4 0 0 0-4-4H4"],
    file: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z", "M14 2v6h6"],
    info: [{ c: [12, 12, 10] }, "M12 16v-4M12 8h.01"],
    wallet: ["M20 12V8H6a2 2 0 0 1 0-4h12v4", "M4 6v12a2 2 0 0 0 2 2h14v-4", "M18 12a2 2 0 0 0 0 4h4v-4z"],
    external: ["M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6", "M15 3h6v6M10 14 21 3"],
    list: ["M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"],
    alert: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4M12 17h.01"],
    calendar: [{ r: [3, 4, 18, 18, 2] }, "M16 2v4M8 2v4M3 10h18"],
    help: [{ c: [12, 12, 10] }, "M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"],
    "message-square": ["M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"],
  };
  const SVGNS = "http://www.w3.org/2000/svg";
  function icon(name) {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "i");
    svg.setAttribute("aria-hidden", "true");
    for (const part of ICONS[name] || ICONS.info) {
      let el;
      if (typeof part === "string") { el = document.createElementNS(SVGNS, "path"); el.setAttribute("d", part); }
      else if (part.c) { el = document.createElementNS(SVGNS, "circle"); ["cx", "cy", "r"].forEach((k, i) => el.setAttribute(k, part.c[i])); }
      else { el = document.createElementNS(SVGNS, "rect"); ["x", "y", "width", "height", "rx"].forEach((k, i) => el.setAttribute(k, part.r[i])); }
      svg.appendChild(el);
    }
    return svg;
  }
  function hydrateIcons(root = document) {
    root.querySelectorAll("[data-icon]").forEach((el) => {
      if (el.querySelector(":scope > svg.i")) el.querySelector(":scope > svg.i").remove();
      el.prepend(icon(el.dataset.icon));
    });
  }

  // ---------------------------------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "html") el.innerHTML = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
    return el;
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const short = (id) => (id || "").slice(-6);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

  function relTime(iso) {
    const d = new Date(iso); if (isNaN(d)) return "";
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    if (s < 604800) return `${Math.round(s / 86400)}d ago`;
    return d.toLocaleDateString([], { day: "numeric", month: "short" });
  }
  const clock = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
  const fullTime = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); };
  function dayLabel(iso) {
    const d = new Date(iso), today = new Date();
    const y = new Date(); y.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "Today";
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  }
  function fmtBytes(n) { return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`; }

  // Small, safe markdown: escape everything, then add a few structures.
  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<]+[^\s<.,;:)'"])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  }
  function markdown(text) {
    const out = []; const lines = esc(text || "").split("\n");
    let list = null, para = [], code = null;
    const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
    const flushList = () => { if (list) { out.push(`<ul>${list.map((li) => `<li>${inline(li)}</li>`).join("")}</ul>`); list = null; } };
    for (const line of lines) {
      if (code !== null) {
        if (line.trim().startsWith("```")) { out.push(`<pre>${code.join("\n")}</pre>`); code = null; } else code.push(line);
        continue;
      }
      if (line.trim().startsWith("```")) { flushPara(); flushList(); code = []; continue; }
      const li = line.match(/^\s*(?:[-•*]|\d+\.)\s+(.*)$/);
      if (li) { flushPara(); (list = list || []).push(li[1]); continue; }
      if (!line.trim()) { flushPara(); flushList(); continue; }
      if (/^[A-Z][A-Za-z /]{1,30}:$/.test(line.trim())) { flushPara(); flushList(); out.push(`<h4>${line.trim().slice(0, -1)}</h4>`); continue; }
      flushList(); para.push(line);
    }
    if (code !== null) out.push(`<pre>${code.join("\n")}</pre>`);
    flushPara(); flushList();
    return h("div", { class: "md", html: out.join("") });
  }

  // ---------------------------------------------------------------------------------------- meta
  const STATUS = {
    QUEUED: ["Queued", "run"], RUNNING: ["Running", "run"], VERIFYING: ["Verifying", "run"],
    AWAITING_APPROVAL: ["Needs approval", "wait"], AWAITING_USER: ["Needs answer", "wait"], BLOCKED: ["Blocked", "wait"],
    PAUSED: ["Paused", ""], COMPLETED: ["Done", "ok"], PARTIAL: ["Partial", "wait"], FAILED: ["Failed", "bad"], CANCELLED: ["Cancelled", ""],
  };
  const ACTIVE = new Set(["QUEUED", "RUNNING", "VERIFYING", "AWAITING_APPROVAL", "AWAITING_USER", "BLOCKED", "PAUSED"]);
  const WORKING = new Set(["QUEUED", "RUNNING", "VERIFYING"]);
  const WORKFLOWS = {
    research_report: ["Research", "book"], video_analysis: ["Video", "video"], browser_task: ["Browse", "globe"],
    desktop_workflow: ["Office", "briefcase"], chat_workflow: ["Chat", "message-square"],
  };
  const wf = (w) => WORKFLOWS[w] || ["Task", "sparkles"];
  const pill = (status) => { const [label, cls] = STATUS[status] || [status, ""]; return h("span", { class: `pill ${cls}` }, label); };
  const MODES = [
    { prefix: "", label: "Auto", icon: "sparkles", hint: "Describe a task…" },
    { prefix: "/research ", label: "Research", icon: "book", hint: "What should I research? I'll write a cited Word report." },
    { prefix: "/video ", label: "Video", icon: "video", hint: "Paste a video URL, then an optional question" },
    { prefix: "/browse ", label: "Browse", icon: "globe", hint: "What should I do in the browser?" },
    { prefix: "/office ", label: "Office", icon: "briefcase", hint: "Spreadsheet, email or calendar goal" },
  ];
  const COMMANDS = [
    ["/research", "<topic>", "Cited Word report on a topic"], ["/video", "<url> [question]", "Timestamped video analysis"],
    ["/browse", "<goal>", "Operate the browser"], ["/office", "<goal>", "Spreadsheet, email or calendar"],
    ["/tasks", "", "List recent tasks"], ["/status", "<id>", "Progress of a task"], ["/pause", "<id|all>", "Pause a task"],
    ["/resume", "<id> [+budget]", "Resume a task"], ["/cancel", "<id>", "Cancel a task"], ["/steer", "<id> <text>", "Redirect a running task"],
    ["/answer", "<id> <text>", "Answer a task's question"], ["/budget", "", "Model spend this month"],
    ["/schedules", "", "List schedules"], ["/schedule", "add|pause|resume|run …", "Manage schedules"],
    ["/prefs", "[set <k> <v> | del <k>]", "Saved preferences"], ["/help", "", "All commands"],
  ];

  // ---------------------------------------------------------------------------------------- state
  const TOKEN_KEY = "omca.ownerToken", CONV_KEY = "omca.conversation";
  // A chat is a conversation: its messages and tasks share a conversation id, and the assistant only sees that chat's history.
  // Tasks without one (older rows, Telegram) are their own one-task chat, keyed by task id.
  const newConvId = () => "cnv_" + Array.from(crypto.getRandomValues(new Uint8Array(14)), (b) => (b % 36).toString(36)).join("");
  const convOfTask = (t) => t.conversation_id || t.task_id;
  const convOfMsg = (m) => m.conversation_id || m.task_id || null;
  const S = {
    token: null, rev: 0, messages: new Map(), els: new Map(), tasks: [], taskMap: new Map(), selected: null,
    conv: store.get(CONV_KEY) || newConvId(), unreadConv: new Set(),
    detail: null, filter: "all", query: "", mode: 0, files: [], replyTo: null, info: null, budget: null,
    approvals: new Map(), blobs: new Map(), unread: 0, notify: store.get("omca.notify") === "1",
    inspectorOpen: store.get("omca.inspector") !== "0", connected: false, firstLoad: true, sending: false,
  };
  (function readToken() {
    const t = new URLSearchParams(location.hash.slice(1)).get("token");
    if (t) { store.set(TOKEN_KEY, t); history.replaceState(null, "", location.pathname + location.search); }
    S.token = t || store.get(TOKEN_KEY);
  })();

  class AuthError extends Error {}
  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), Authorization: `Bearer ${S.token}` } });
    if (res.status === 401) { showLogin(true); throw new AuthError("unauthorized"); }
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = j.detail || j.error?.message || detail; } catch { /* not json */ }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res;
  }
  const getJSON = async (p) => (await api(p)).json();
  const postJSON = async (p, body) => (await api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })).json();

  function toast(text, kind = "") {
    const el = h("div", { class: `toast ${kind}`, role: "status" }, text);
    $("toasts").appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; setTimeout(() => el.remove(), 320); }, 3800);
  }

  // ---------------------------------------------------------------------------------------- auth
  function showLogin(show) {
    $("login").hidden = !show;
    if (show) { $("loginError").hidden = !S.token; setTimeout(() => $("tokenInput").focus(), 30); }
  }
  $("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    S.token = $("tokenInput").value.trim();
    try { await getJSON("/ui/api/info"); store.set(TOKEN_KEY, S.token); showLogin(false); start(); }
    catch { $("loginError").hidden = false; }
  });

  // ---------------------------------------------------------------------------------------- theme & notifications
  function applyTheme() {
    const t = store.get("omca.theme") || "system";
    if (t === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
    const b = $("themeBtn"); b.dataset.icon = { system: "monitor", light: "sun", dark: "moon" }[t]; b.title = `Theme: ${t}`;
    hydrateIcons(b.parentElement);
  }
  function cycleTheme() {
    const order = ["system", "light", "dark"]; const cur = store.get("omca.theme") || "system";
    const next = order[(order.indexOf(cur) + 1) % 3]; store.set("omca.theme", next); applyTheme(); toast(`Theme: ${next}`);
  }
  function renderNotifyBtn() {
    const b = $("notifyBtn"); const on = S.notify && "Notification" in window && Notification.permission === "granted";
    b.dataset.icon = on ? "bell" : "bell-off"; b.classList.toggle("on", on); b.title = on ? "Notifications on" : "Notifications off";
    hydrateIcons(b.parentElement);
  }
  async function toggleNotify() {
    if (!("Notification" in window)) { toast("This browser does not support notifications"); return; }
    if (S.notify) { S.notify = false; store.set("omca.notify", "0"); renderNotifyBtn(); toast("Notifications off"); return; }
    const p = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    S.notify = p === "granted"; store.set("omca.notify", S.notify ? "1" : "0"); renderNotifyBtn();
    toast(S.notify ? "You'll be notified about approvals and finished tasks" : "Notifications are blocked in this browser");
  }
  function notify(title, body, taskId) {
    if (!S.notify || !document.hidden || !("Notification" in window) || Notification.permission !== "granted") return;
    try {
      const n = new Notification(title, { body: body.slice(0, 180), tag: taskId || undefined });
      n.onclick = () => { window.focus(); if (taskId) selectTask(taskId); n.close(); };
    } catch { /* some browsers need a service worker */ }
  }
  function setUnread(n) { S.unread = n; document.title = n ? `(${n}) OMCA Assistant` : "OMCA Assistant"; }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) setUnread(0); });

  // ---------------------------------------------------------------------------------------- feed
  const feed = $("feed");
  let inner = h("div", { class: "feed-inner" });
  feed.appendChild(inner);

  function isApproval(m) { return (m.buttons || []).flat().some((b) => /^omca:apr:/.test(b[1])); }
  function approvalId(m) {
    for (const [, data] of (m.buttons || []).flat()) { const x = /^omca:apr:(apr_[A-Z0-9]+)/.exec(data); if (x) return x[1]; }
    const t = /(apr_[A-Z0-9]+)/.exec(m.text || ""); return t ? t[1] : null;
  }
  const isStatusLine = (m) => m.kind === "text" && !m.buttons && /^Task [A-Z0-9]{6} /.test(m.text) && m.text.length < 260;

  function taskChip(taskId) {
    if (!taskId) return null;
    return h("button", { class: "task-chip", title: "Open task", onclick: () => selectTask(taskId) }, "#" + short(taskId));
  }
  function cardHead(m, extra) {
    return h("div", { class: "head" }, taskChip(m.task_id), extra, h("time", { datetime: m.created_at, title: fullTime(m.created_at) }, clock(m.created_at)));
  }

  function approvalCard(m) {
    const id = approvalId(m);
    const fields = {};
    for (const line of m.text.split("\n")) { const x = /^(Action|Operation|Destination|Consequence|Expires):\s*(.*)$/.exec(line); if (x) fields[x[1]] = x[2]; }
    const decided = /Decision:\s*(approved|denied)/i.exec(m.text);
    const card = h("div", { class: "card approval warn" }, cardHead(m));
    card.appendChild(h("div", { class: "title" }, icon("shield"), decided ? "Approval" : "Approval needed"));
    if (Object.keys(fields).length) {
      const dl = h("dl");
      for (const k of ["Action", "Destination", "Consequence"]) if (fields[k] && fields[k] !== "-") dl.append(h("dt", {}, k), h("dd", {}, fields[k]));
      card.appendChild(dl);
    } else card.appendChild(markdown(m.text.replace(/\n*Decision:.*$/s, "")));
    const row = h("div", { class: "row" });
    if (decided || !m.buttons) {
      const ok = decided && /approved/i.test(decided[1]);
      row.appendChild(h("span", { class: `decided ${decided ? (ok ? "ok" : "bad") : ""}` }, icon(ok ? "check" : "stop"),
        decided ? (ok ? "Approved once" : "Denied") : "Closed"));
      card.classList.remove("warn");
    } else {
      for (const [label, data] of m.buttons.flat()) {
        const deny = /deny/i.test(label);
        row.appendChild(h("button", { class: `btn ${deny ? "danger" : "ok"}`, onclick: (e) => press(data, row, e.currentTarget) }, icon(deny ? "x" : "check"), label));
      }
      const cd = h("span", { class: "countdown", dataset: { approval: id || "" } }, icon("clock"), "…");
      row.appendChild(cd);
      if (id) loadApproval(id).then(() => tickCountdowns());
    }
    card.appendChild(row);
    return card;
  }
  async function loadApproval(id) {
    if (S.approvals.has(id)) return S.approvals.get(id);
    try { const ap = await getJSON(`/approvals/${id}`); S.approvals.set(id, ap); return ap; } catch { return null; }
  }
  function tickCountdowns() {
    document.querySelectorAll(".countdown[data-approval]").forEach((el) => {
      const ap = S.approvals.get(el.dataset.approval); if (!ap) return;
      const left = Math.max(0, Math.round((new Date(ap.expires_at) - Date.now()) / 1000));
      el.lastChild.textContent = left ? `Expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "Expired";
      if (!left) el.closest(".row")?.querySelectorAll("button").forEach((b) => (b.disabled = true));
    });
  }
  async function press(data, row, btn) {
    row.querySelectorAll("button").forEach((b) => (b.disabled = true));
    btn.classList.add("busy");
    try { const r = await postJSON("/ui/api/callback", { data }); if (r.reply) toast(r.reply); refreshTasks(); }
    catch (e) { if (!(e instanceof AuthError)) toast(e.message, "bad"); row.querySelectorAll("button").forEach((b) => (b.disabled = false)); }
  }

  async function blobUrl(key, path) {
    if (S.blobs.has(key)) return S.blobs.get(key);
    const p = api(path).then((r) => r.blob()).then((b) => URL.createObjectURL(b));
    S.blobs.set(key, p);
    try { return await p; } catch (e) { S.blobs.delete(key); throw e; }
  }
  async function download(key, path, name) {
    try { const url = await blobUrl(key, path); const a = h("a", { href: url, download: name }); document.body.appendChild(a); a.click(); a.remove(); }
    catch (e) { if (!(e instanceof AuthError)) toast(`Download failed: ${e.message}`, "bad"); }
  }
  function openLightbox(url) { $("lightboxImg").src = url; $("lightbox").hidden = false; }

  function documentCard(m) {
    const ext = (m.filename || "").split(".").pop().toLowerCase();
    const card = h("div", { class: "card" }, cardHead(m));
    card.appendChild(h("div", { class: "doc" },
      h("div", { class: "doc-icon" }, ext.toUpperCase().slice(0, 4) || "FILE"),
      h("div", { class: "doc-info" }, h("div", { class: "doc-name", title: m.filename }, m.filename || "file"), h("div", { class: "doc-sub" }, "Delivered file")),
      h("button", { class: "btn sm", onclick: () => download(`msg:${m.id}`, `/ui/api/files/${m.id}`, m.filename) }, icon("download"), "Download")));
    if (m.text) card.appendChild(h("div", { style: "margin-top:8px" }, markdown(m.text)));
    if (["png", "jpg", "jpeg", "gif", "webp"].includes(ext)) {
      const img = h("img", { class: "thumb", alt: m.filename, loading: "lazy" });
      blobUrl(`msg:${m.id}`, `/ui/api/files/${m.id}`).then((u) => { img.src = u; img.onclick = () => openLightbox(u); }).catch(() => img.remove());
      card.appendChild(img);
    }
    return card;
  }

  function renderMessage(m) {
    if (m.role === "owner") {
      const attach = /\n\[attached: (.+)\]$/.exec(m.text);
      const text = attach ? m.text.slice(0, attach.index) : m.text;
      const bubble = h("div", { class: "bubble" });
      if (text) bubble.appendChild(markdown(text));
      if (attach) bubble.appendChild(h("div", { style: "margin-top:6px;opacity:.85;font-size:12.5px;display:flex;gap:6px;align-items:center" }, icon("paperclip"), attach[1]));
      return h("div", { class: "msg owner", dataset: { id: m.id } },
        h("div", {}, bubble, h("div", { class: "msg-time", title: fullTime(m.created_at) }, clock(m.created_at))));
    }
    let card;
    if (m.kind === "document") card = documentCard(m);
    else if (isApproval(m) || /^Approval needed/.test(m.text) || /Decision: (approved|denied)/.test(m.text)) card = approvalCard(m);
    else if (isStatusLine(m)) {
      card = h("div", { class: "card status compact" }, cardHead(m), markdown(m.text));
    } else {
      const completed = /^Task [A-Z0-9]{6} completed/.test(m.text);
      card = h("div", { class: `card ${completed ? "ok-card" : ""}` }, cardHead(m), markdown(m.text));
      if (m.buttons) {
        const row = h("div", { class: "row", style: "display:flex;gap:8px;margin-top:10px;flex-wrap:wrap" });
        for (const [label, data] of m.buttons.flat()) row.appendChild(h("button", { class: "btn sm", onclick: (e) => press(data, row, e.currentTarget) }, label));
        card.appendChild(row);
      }
    }
    return h("div", { class: "msg assistant", dataset: { id: m.id } }, h("div", { class: "avatar logo" }), h("div", { class: "bubble" }, card));
  }

  function visibleMessages() {
    return [...S.messages.values()].filter((m) => convOfMsg(m) === S.conv).sort((a, b) => a.id - b.id);
  }

  // ---------------------------------------------------------------------------------------- conversations
  const NEEDS_YOU = new Set(["AWAITING_APPROVAL", "AWAITING_USER", "BLOCKED"]);
  const convTasks = (key = S.conv) => S.tasks.filter((t) => convOfTask(t) === key);
  function conversations() {
    const groups = new Map();  // S.tasks is newest first, so the map is too
    for (const t of S.tasks) { const k = convOfTask(t); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(t); }
    return [...groups].map(([key, tasks]) => {
      const first = tasks[tasks.length - 1], latest = tasks[0];
      const work = tasks.find((t) => t.workflow !== "chat_workflow");
      const lead = tasks.find((t) => NEEDS_YOU.has(t.status)) || tasks.find((t) => WORKING.has(t.status)) || latest;
      return {
        key, tasks, title: first.goal, latest, work, status: lead.status, workflow: (work || latest).workflow,
        updated_at: tasks.reduce((m, t) => (t.updated_at > m ? t.updated_at : m), ""), active: tasks.some((t) => ACTIVE.has(t.status)),
        needs: tasks.some((t) => NEEDS_YOU.has(t.status)), activeWork: tasks.find((t) => t.workflow !== "chat_workflow" && ACTIVE.has(t.status)),
      };
    });
  }
  const getConv = (key = S.conv) => conversations().find((c) => c.key === key) || null;

  function welcome() {
    const ex = [
      [1, "Research a topic", "Rooftop solar adoption in India, with sources", "rooftop solar adoption in India"],
      [3, "Do something on a website", "Find a page and pull out the details you need", "Go to https://en.wikipedia.org and find the population of Bengaluru"],
      [2, "Analyse a video", "Summarise with timestamps or answer a question", ""],
      [4, "Office work", "Draft an email, update a spreadsheet, plan a meeting", "Draft an email to the team summarising this week's progress"],
    ];
    const wrap = h("div", { class: "welcome" }, h("div", { class: "logo big" }), h("h2", {}, greeting()),
      h("p", {}, "Tell me what to work on. I'll keep you posted here and ask before anything important."));
    const grid = h("div", { class: "examples" });
    for (const [mode, title, sub, text] of ex) {
      grid.appendChild(h("button", { class: "example", onclick: () => { setMode(mode); setInput(text); } },
        h("span", { class: "ex-icon" }, icon(MODES[mode].icon)), h("div", {}, h("strong", {}, title), h("span", {}, sub))));
    }
    wrap.appendChild(grid);
    const notes = [];
    if (S.info && S.info.search_adapter === "firecrawl") notes.push("Web search needs a Firecrawl key or SearXNG");
    if (S.info && !S.info.browser_authorized) notes.push("the browser profile isn't authorized yet");
    if (notes.length) wrap.appendChild(h("div", { class: "setup-note" }, "Setup: " + notes.join(" · ") + "."));
    return wrap;
  }
  function greeting() {
    const hr = new Date().getHours();
    return hr < 5 ? "Working late?" : hr < 12 ? "Good morning" : hr < 17 ? "Good afternoon" : "Good evening";
  }

  function workingRow() {
    const working = convTasks().filter((x) => WORKING.has(x.status));
    const t = working[0];
    if (!t) return null;
    const more = working.length > 1 ? ` (+${working.length - 1} more)` : "";
    const label = t.workflow === "chat_workflow" ? `Thinking…${more}`
      : `${STATUS[t.status][0]}${t.phase ? " · " + t.phase : ""} — ${t.goal.slice(0, 70)}${t.goal.length > 70 ? "…" : ""}${more}`;
    return h("div", { class: "msg assistant", id: "workingRow" }, h("div", { class: "avatar logo" }),
      h("div", { class: "bubble" }, h("div", { class: "card status compact" }, h("div", { class: "working" },
        h("span", { class: "typing" }, h("span"), h("span"), h("span")), h("span", {}, label)))));
  }

  function renderFeed({ scroll = false } = {}) {
    const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
    const msgs = visibleMessages();
    const next = h("div", { class: "feed-inner" });
    if (!msgs.length) next.appendChild(welcome());
    let lastDay = null, prev = null;
    for (const m of msgs) {
      const day = new Date(m.created_at).toDateString();
      if (day !== lastDay) { next.appendChild(h("div", { class: "day" }, dayLabel(m.created_at))); lastDay = day; prev = null; }
      const key = `${m.id}:${m.rev}`;
      let el = S.els.get(m.id);
      if (!el || el.dataset.key !== key) { el = renderMessage(m); el.dataset.key = key; S.els.set(m.id, el); }
      const cont = prev && prev.role === m.role && m.role === "assistant" && (new Date(m.created_at) - new Date(prev.created_at)) < 120000;
      el.classList.toggle("cont", !!cont);
      next.appendChild(el); prev = m;
    }
    const w = workingRow(); if (w) next.appendChild(w);
    feed.replaceChild(next, inner); inner = next;
    if (scroll || atBottom || S.firstLoad) {
      feed.scrollTop = feed.scrollHeight; $("jump").hidden = true;
      requestAnimationFrame(() => { feed.scrollTop = feed.scrollHeight; });  // after images and fonts settle
    }
    tickCountdowns();
  }
  feed.addEventListener("scroll", () => { if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120) $("jump").hidden = true; });

  // ---------------------------------------------------------------------------------------- live updates
  function setConn(ok) {
    S.connected = ok; const c = $("conn");
    c.className = `conn ${ok ? "ok" : "bad"}`; c.lastChild.textContent = ok ? "Live" : "Reconnecting…"; c.title = ok ? "Live" : "Connection lost, retrying";
  }
  let pollGen = 0;
  async function pollLoop() {
    const gen = ++pollGen; let backoff = 1000;
    while (gen === pollGen) {
      try {
        const wait = S.firstLoad ? 0 : 25;
        const data = await getJSON(`/ui/api/messages?after=${S.rev}&wait=${wait}`);
        if (gen !== pollGen) return;
        setConn(true); backoff = 1000;
        if (data.messages.length) onMessages(data.messages);
        S.rev = Math.max(S.rev, data.rev);
        if (S.firstLoad) { S.firstLoad = false; renderFeed({ scroll: true }); }
      } catch (e) {
        if (e instanceof AuthError) return;
        setConn(false); await sleep(backoff); backoff = Math.min(backoff * 2, 15000);
      }
    }
  }
  function onMessages(items) {
    const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
    let fresh = 0;
    for (const m of items) {
      const isNew = !S.messages.has(m.id);
      S.messages.set(m.id, m);
      if (isApproval(m)) S.approvals.delete(approvalId(m));
      if (!S.firstLoad && isNew && m.role === "assistant") {
        if (convOfMsg(m) && convOfMsg(m) !== S.conv) { S.unreadConv.add(convOfMsg(m)); renderTasks(); }
        else fresh++;
        const t = m.task_id ? S.taskMap.get(m.task_id) : null;
        if (isApproval(m)) notify("Approval needed", m.text.split("\n").find((l) => l.startsWith("Action:")) || m.text, m.task_id);
        else if (/^Task [A-Z0-9]{6} (completed|partial|failed)/.test(m.text)) notify(m.text.split("\n")[0], t ? t.goal : "", m.task_id);
        else if (m.kind === "document") notify("New file", m.filename || "", m.task_id);
      }
    }
    if (fresh && document.hidden) setUnread(S.unread + fresh);
    if (!S.firstLoad) {
      renderFeed();
      if (fresh && !atBottom && visibleMessages().some((m) => items.some((i) => i.id === m.id))) $("jump").hidden = false;
      refreshTasksSoon();
    }
  }

  // ---------------------------------------------------------------------------------------- tasks
  async function refreshTasks() {
    try {
      const tasks = await getJSON("/tasks?limit=100");
      S.tasks = tasks; S.taskMap = new Map(tasks.map((t) => [t.task_id, t]));
      if (S.replyTo && !ACTIVE.has(S.taskMap.get(S.replyTo)?.status)) setReply(null);
      else if (S.replyTo) setReply(S.replyTo);  // status may have changed between steering and answering
      renderTasks(); renderTop();
      if (S.selected) loadDetail(S.selected);
      if (!S.firstLoad) { const w = document.getElementById("workingRow"); const n = workingRow(); if (w && n) w.replaceWith(n); else if (!!w !== !!n) renderFeed(); }
    } catch { /* connection state is shown by the poll loop */ }
  }
  const refreshTasksSoon = debounce(refreshTasks, 300);
  async function refreshBudget() {
    try {
      const b = await getJSON("/budget"); S.budget = b;
      const used = b.ceiling - b.remaining; const pct = b.ceiling ? Math.min(100, (used / b.ceiling) * 100) : 0;
      $("budgetText").textContent = `${cur(b.currency)}${used.toFixed(2)} of ${cur(b.currency)}${b.ceiling.toFixed(0)}`;
      const fill = $("budgetFill"); fill.style.width = `${Math.max(pct, used > 0 ? 2 : 0)}%`;
      fill.className = `meter-fill ${pct >= 90 ? "bad" : pct >= 70 ? "warn" : ""}`;
    } catch { /* ignore */ }
  }
  const cur = (c) => ({ INR: "₹", USD: "$", EUR: "€", GBP: "£" }[c] || (c ? c + " " : ""));

  function renderTasks() {
    const list = $("taskList"); list.replaceChildren();
    const q = S.query.trim().toLowerCase();
    const convs = conversations();
    const items = convs.filter((c) => {
      if (S.filter === "active" && !c.active) return false;
      if (S.filter === "done" && c.active) return false;
      return !q || c.tasks.some((t) => t.goal.toLowerCase().includes(q) || t.task_id.toLowerCase().includes(q)) || wf(c.workflow)[0].toLowerCase().includes(q);
    });
    const draft = !convs.some((c) => c.key === S.conv) && S.filter !== "done" && !q;
    if (draft) {
      list.appendChild(h("button", { class: "task-item selected", onclick: () => $("app").classList.remove("sidebar-open") },
        h("span", { class: "wf" }, icon("message-square")), h("span", { class: "goal" }, "New chat"), h("span", { class: "when" }),
        h("span", { class: "meta" }, "Nothing sent yet")));
    }
    if (!items.length) {
      if (!draft) list.appendChild(h("div", { class: "task-empty" }, convs.length ? "No matching chats" : "No chats yet. Start one below."));
      return;
    }
    const needs = items.filter((c) => c.needs);
    const rest = items.filter((c) => !c.needs);
    const today = new Date().toDateString();
    const groups = [["Needs you", needs], ["Today", rest.filter((c) => new Date(c.latest.created_at).toDateString() === today)],
      ["Earlier", rest.filter((c) => new Date(c.latest.created_at).toDateString() !== today)]];
    for (const [label, cs] of groups) {
      if (!cs.length) continue;
      list.appendChild(h("div", { class: "group-label" }, label));
      for (const c of cs) {
        const [wlabel, wicon] = wf(c.workflow); const [slabel, scls] = STATUS[c.status] || [c.status, ""];
        list.appendChild(h("button", { class: `task-item ${S.conv === c.key ? "selected" : ""} ${S.unreadConv.has(c.key) ? "unread" : ""}`,
          onclick: () => openConv(c.key), title: c.title },
          h("span", { class: "wf" }, icon(wicon)),
          h("span", { class: "goal" }, c.title),
          h("span", { class: "when" }, relTime(c.updated_at)),
          h("span", { class: "meta" }, h("span", { class: `dot ${scls}` }), slabel, " · ", wlabel,
            c.tasks.length > 1 ? ` · ${c.tasks.length} requests` : "")));
      }
    }
  }

  function renderTop() {
    const top = $("topTitle"); top.replaceChildren();
    const c = getConv();
    $("inspectorBtn").hidden = !(c && c.work);
    if (!c) { top.append(h("h1", {}, "New chat"), h("span", { class: "sub" }, "A fresh conversation with its own context")); return; }
    top.append(h("h1", { title: c.title }, c.title),
      h("span", { class: "sub" }, pill(c.status), `${wf(c.workflow)[0]}${c.tasks.length > 1 ? ` · ${c.tasks.length} requests` : ` · #${short(c.latest.task_id)}`}`));
  }

  // Open a chat: its own messages, its own reply target, its own details pane.
  function openConv(key) {
    S.conv = key; store.set(CONV_KEY, key); S.unreadConv.delete(key);
    S.selected = null; S.detail = null;
    $("app").classList.remove("sidebar-open");
    const c = getConv(key);
    setReply(c && c.activeWork ? c.activeWork.task_id : null);
    if (c && c.work) focusTask(c.work.task_id); else openInspector(false, true);
    renderTasks(); renderTop(); renderFeed({ scroll: true });
  }
  function newChat() {
    if (!visibleMessages().length) { setReply(null); return; }  // the open chat is already empty
    openConv(newConvId());
  }
  // Show one task's details without leaving the chat.
  function focusTask(id, force) {
    S.selected = id; S.detail = null;
    if (!id) return;
    if (force || (window.innerWidth > 1180 && S.inspectorOpen)) openInspector(true, true);
    loadDetail(id);
  }
  function selectTask(id) {
    const t = S.taskMap.get(id); if (!t) return;
    if (convOfTask(t) !== S.conv) openConv(convOfTask(t));
    focusTask(id, true);
  }

  // ---------------------------------------------------------------------------------------- inspector
  function openInspector(open, keepPref) {
    const ins = $("inspector"); ins.hidden = !open || !S.selected;
    $("app").classList.toggle("with-inspector", !ins.hidden);
    if (!keepPref && S.selected) { S.inspectorOpen = open; store.set("omca.inspector", open ? "1" : "0"); }
    if (!ins.hidden && S.detail) renderInspector();
    else if (!ins.hidden) ins.replaceChildren(h("div", { class: "insp-section" }, h("div", { class: "working" }, h("span", { class: "typing" }, h("span"), h("span"), h("span")), "Loading task…")));
  }
  async function loadDetail(id) {
    try {
      const d = await getJSON(`/ui/api/tasks/${encodeURIComponent(id)}`);
      if (S.selected !== id) return;
      S.detail = d; if (!$("inspector").hidden) renderInspector();
    } catch { /* keep the old view */ }
  }
  function stepsFor(d) {
    const s = d.status;
    const order = ["Queued", "Working", "Verifying", "Finished"];
    const at = { QUEUED: 0, RUNNING: 1, AWAITING_APPROVAL: 1, AWAITING_USER: 1, BLOCKED: 1, PAUSED: 1, VERIFYING: 2 }[s];
    const final = ["COMPLETED", "PARTIAL", "FAILED", "CANCELLED"].includes(s);
    return order.map((label, i) => {
      let cls = "pending", note = null;
      if (final) { cls = i < 3 ? "done" : ({ FAILED: "fail", CANCELLED: "fail", PARTIAL: "partial" }[s] || "done"); if (i === 3) note = STATUS[s][0]; }
      else if (i < at) cls = "done";
      else if (i === at) { cls = "now"; note = s === "RUNNING" ? (d.phase || null) : STATUS[s][0]; }
      if (i === 1 && cls === "now" && d.blocking_reason) note = `${note ? note + " — " : ""}${d.blocking_reason}`;
      return h("li", { class: cls }, h("span", { class: "node" }), h("div", {}, label, note ? h("small", {}, note) : null));
    });
  }
  function renderInspector() {
    const d = S.detail; const ins = $("inspector"); if (!d) return;
    const [wlabel, wicon] = wf(d.workflow);
    const res = d.result || {}; const det = res.details || {};
    ins.replaceChildren();
    ins.appendChild(h("div", { class: "insp-head" },
      h("div", { class: "t" }, h("div", { class: "ids" }, icon(wicon), `${wlabel} · #${short(d.task_id)}`, pill(d.status)), h("h2", {}, d.goal)),
      h("button", { class: "icon-btn", "aria-label": "Close details", onclick: () => openInspector(false) }, icon("x"))));

    const acts = h("div", { class: "insp-actions" });
    const act = (label, ic, fn, cls = "") => acts.appendChild(h("button", { class: `btn sm ${cls}`, onclick: fn }, icon(ic), label));
    if (d.pending_approval_id) act("Review approval", "shield", () => { const el = [...document.querySelectorAll(".approval")].pop(); el?.scrollIntoView({ behavior: "smooth", block: "center" }); }, "primary");
    if (ACTIVE.has(d.status)) {
      act("Reply", "reply", () => { setReply(d.task_id); $("input").focus(); });
      if (d.status === "PAUSED") act("Resume", "play", () => taskAction(d.task_id, "resume"), "primary");
      else act("Pause", "pause", () => taskAction(d.task_id, "pause"));
      act("Cancel", "stop", () => confirmCancel(d), "danger");
    }
    act("Status report", "list", () => sendText(`/status ${short(d.task_id)}`));
    ins.appendChild(h("div", { class: "insp-section" }, acts));

    if (ACTIVE.has(d.status) && d.status !== "PAUSED") {
      const answer = d.status === "AWAITING_USER";
      const input = h("input", { placeholder: answer ? "Answer the task's question…" : "Steer: e.g. focus on Kerala only", "aria-label": answer ? "Answer" : "Steer" });
      const form = h("form", { class: "inline-form", onsubmit: async (e) => {
        e.preventDefault(); const v = input.value.trim(); if (!v) return;
        await sendText(`/${answer ? "answer" : "steer"} ${short(d.task_id)} ${v}`); input.value = "";
      } }, input, h("button", { class: "btn sm primary", type: "submit" }, answer ? "Answer" : "Steer"));
      ins.lastChild.appendChild(form);
    }

    ins.appendChild(h("div", { class: "insp-section" }, h("h3", {}, "Progress"), h("ol", { class: "steps" }, stepsFor(d))));

    if (res.summary || (res.limitations || []).length) {
      const sec = h("div", { class: "insp-section" }, h("h3", {}, "Result"));
      if (res.summary) sec.appendChild(markdown(res.summary));
      if ((res.limitations || []).length) {
        sec.appendChild(h("div", { style: "margin-top:10px;font-size:12px;font-weight:600;color:var(--text-3)" }, "Limitations"));
        sec.appendChild(h("ul", { class: "limitations" }, res.limitations.map((l) => h("li", {}, l))));
      }
      ins.appendChild(sec);
    }

    if (det.screenshot_artifact_id) {
      const img = h("img", { class: "thumb", alt: "Final page", style: "margin-top:0" });
      blobUrl(`art:${det.screenshot_artifact_id}`, `/artifacts/${det.screenshot_artifact_id}`).then((u) => { img.src = u; img.onclick = () => openLightbox(u); }).catch(() => img.remove());
      ins.appendChild(h("div", { class: "insp-section" }, h("h3", {}, "Final page"), img,
        det.final_url ? h("div", { style: "margin-top:8px;font-size:12.5px" }, h("a", { href: det.final_url, target: "_blank", rel: "noopener noreferrer" }, det.final_url)) : null));
    }

    if ((d.artifacts || []).length) {
      const sec = h("div", { class: "insp-section" }, h("h3", {}, "Files"));
      for (const a of d.artifacts) {
        const ext = (a.name || "").split(".").pop().toUpperCase().slice(0, 4);
        sec.appendChild(h("button", { class: "file-row", onclick: () => download(`art:${a.artifact_id}`, `/artifacts/${a.artifact_id}`, a.name) },
          h("span", { class: "doc-icon" }, ext || "FILE"), h("span", { class: "n" }, a.name), a.bytes ? h("small", {}, fmtBytes(a.bytes)) : null, icon("download")));
      }
      ins.appendChild(sec);
    }

    if ((det.urls || []).length) {
      ins.appendChild(h("div", { class: "insp-section" }, h("h3", {}, `Pages visited (${det.urls.length})`),
        h("ol", { class: "url-list" }, det.urls.map((u) => h("li", {}, h("a", { href: u, target: "_blank", rel: "noopener noreferrer" }, u))))));
    }

    const facts = h("dl", { class: "facts" });
    const fact = (k, v) => { if (v != null && v !== "") facts.append(h("dt", {}, k), h("dd", {}, v)); };
    fact("Created", fullTime(d.created_at)); fact("Updated", fullTime(d.updated_at));
    fact("Model spend", `${cur(d.currency)}${(d.spend || 0).toFixed(2)}`);
    if (det.engine) fact("Engine", det.engine === "browser_use" ? "Browser Use" : det.engine);
    if (det.steps != null) fact("Steps", String(det.steps));
    fact("Worker", d.worker_id); fact("Task id", h("code", {}, d.task_id));
    ins.appendChild(h("div", { class: "insp-section" }, h("h3", {}, "Details"), facts));
  }
  async function taskAction(id, action) {
    try { await postJSON(`/tasks/${id}/${action}`); toast(`Task ${action === "resume" ? "resumed" : action === "pause" ? "paused" : "cancelled"}`); refreshTasks(); }
    catch (e) { if (!(e instanceof AuthError)) toast(e.message, "bad"); }
  }
  function confirmCancel(d) {
    showModal("Cancel this task?", h("div", {},
      h("p", { style: "margin:0 0 16px;color:var(--text-2)" }, "Work already finished is kept. Nothing new will start."),
      h("div", { style: "display:flex;gap:8px;justify-content:flex-end" },
        h("button", { class: "btn", onclick: closeModal }, "Keep running"),
        h("button", { class: "btn danger", onclick: () => { closeModal(); taskAction(d.task_id, "cancel"); } }, icon("stop"), "Cancel task"))));
  }

  // ---------------------------------------------------------------------------------------- composer
  const input = $("input"), send = $("send");
  function renderModes() {
    const box = $("modes"); box.replaceChildren();
    MODES.forEach((m, i) => box.appendChild(h("button", {
      class: "mode", role: "radio", "aria-checked": String(i === S.mode), title: m.label,
      onclick: () => { setMode(i); input.focus(); },
    }, icon(m.icon), h("span", { class: "lbl" }, m.label))));
  }
  function setMode(i) { S.mode = i; renderModes(); updatePlaceholder(); }
  function updatePlaceholder() {
    const t = S.replyTo && S.taskMap.get(S.replyTo);
    input.placeholder = t ? (t.status === "AWAITING_USER" ? "Answer the task's question…" : "Steer this task, or press Esc to start a new one…") : MODES[S.mode].hint;
  }
  function setInput(text) { input.value = text; autosize(); updateSend(); input.focus(); input.setSelectionRange(text.length, text.length); }
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 240) + "px"; }
  function updateSend() { send.disabled = S.sending || (!input.value.trim() && !S.files.length); }
  function setReply(id) {
    S.replyTo = id; const chip = $("replyChip"); chip.replaceChildren();
    const t = id && S.taskMap.get(id);
    chip.hidden = !t;
    if (t) chip.append(icon("reply"), h("span", {}, `${t.status === "AWAITING_USER" ? "Answering" : "Steering"} #${short(id)} — ${t.goal}`),
      h("button", { type: "button", "aria-label": "Stop replying", onclick: () => { setReply(null); input.focus(); } }, icon("x")));
    updatePlaceholder();
  }
  function renderFiles() {
    const box = $("attachments"); box.replaceChildren();
    S.files.forEach((f, i) => box.appendChild(h("span", { class: "att" }, icon("file"), h("span", { title: f.name }, f.name), h("small", {}, fmtBytes(f.size)),
      h("button", { type: "button", "aria-label": `Remove ${f.name}`, onclick: () => { S.files.splice(i, 1); renderFiles(); updateSend(); } }, icon("x")))));
  }
  function addFiles(list) {
    for (const f of list) {
      if (f.size > 20 * 1024 * 1024) { toast(`${f.name} is larger than 20 MB`, "bad"); continue; }
      if (S.files.length >= 10) { toast("Up to 10 files per message", "bad"); break; }
      S.files.push(f);
    }
    renderFiles(); updateSend();
  }
  function composeText() {
    const raw = input.value.trim();
    if (raw.startsWith("/")) return raw;
    const t = S.replyTo && S.taskMap.get(S.replyTo);
    if (t && raw && ACTIVE.has(t.status)) return `/${t.status === "AWAITING_USER" ? "answer" : "steer"} ${short(t.task_id)} ${raw}`;
    return raw ? MODES[S.mode].prefix + raw : raw;
  }
  async function submit() {
    if (send.disabled) return;
    const text = composeText(); const files = S.files.slice();
    input.value = ""; S.files = []; renderFiles(); autosize(); closeSlash();
    const ok = await sendText(text, files);
    if (!ok) { input.value = text; autosize(); S.files = files; renderFiles(); }
    updateSend();
  }
  async function sendText(text, files = []) {
    const conv = S.conv;
    const fd = new FormData(); fd.append("text", text); fd.append("conversation_id", conv); for (const f of files) fd.append("files", f, f.name);
    S.sending = true; send.classList.add("busy"); updateSend();
    try {
      const r = await (await api("/ui/api/messages", { method: "POST", body: fd })).json();
      await refreshTasks();
      const t = r.task_id && S.taskMap.get(r.task_id);
      // a long-running task started here becomes this chat's reply target; plain chat answers need no tracking
      if (t && S.conv === conv && t.workflow !== "chat_workflow" && ACTIVE.has(t.status) && !/^\/(steer|answer|pause|resume|cancel|status)/.test(text)) {
        setReply(t.task_id); focusTask(t.task_id);
      }
      renderTasks(); renderTop(); renderFeed({ scroll: true });
      return true;
    } catch (e) {
      if (!(e instanceof AuthError)) toast(`Not sent: ${e.message}`, "bad");
      return false;
    } finally { S.sending = false; send.classList.remove("busy"); updateSend(); }
  }

  // slash menu
  let slashIdx = 0, slashItems = [];
  function openSlash() {
    const v = input.value;
    if (!/^\/\S*$/.test(v)) { closeSlash(); return; }
    slashItems = COMMANDS.filter(([c]) => c.startsWith(v.toLowerCase()));
    if (!slashItems.length) { closeSlash(); return; }
    slashIdx = Math.min(slashIdx, slashItems.length - 1);
    const menu = $("slashMenu"); menu.replaceChildren(); menu.hidden = false;
    slashItems.forEach(([c, args, desc], i) => menu.appendChild(h("button", {
      class: "slash-item", role: "option", "aria-selected": String(i === slashIdx), type: "button",
      onmousedown: (e) => { e.preventDefault(); pickSlash(i); },
    }, h("code", {}, `${c} ${args}`.trim()), h("span", {}, desc))));
    menu.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }
  function closeSlash() { $("slashMenu").hidden = true; slashIdx = 0; }
  function pickSlash(i) { const [c, args] = slashItems[i]; setInput(args ? `${c} ` : c); closeSlash(); if (!args) submit(); }

  input.addEventListener("input", () => { autosize(); updateSend(); openSlash(); });
  input.addEventListener("keydown", (e) => {
    const menuOpen = !$("slashMenu").hidden;
    if (menuOpen && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); slashIdx = (slashIdx + (e.key === "ArrowDown" ? 1 : -1) + slashItems.length) % slashItems.length; openSlash(); return; }
    if (menuOpen && (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey))) { e.preventDefault(); pickSlash(slashIdx); return; }
    if (e.key === "Escape") { if (menuOpen) closeSlash(); else if (S.replyTo) setReply(null); else input.blur(); return; }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  input.addEventListener("paste", (e) => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); addFiles(fs); } });
  send.addEventListener("click", submit);
  $("fileInput").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });

  let dragDepth = 0;
  const mainEl = document.querySelector(".main");
  mainEl.addEventListener("dragenter", (e) => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); dragDepth++; mainEl.classList.add("dragging"); } });
  mainEl.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("Files")) e.preventDefault(); });
  mainEl.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; mainEl.classList.remove("dragging"); } });
  mainEl.addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; mainEl.classList.remove("dragging"); addFiles([...e.dataTransfer.files]); input.focus(); });

  // ---------------------------------------------------------------------------------------- palette & modals
  let palIdx = 0, palItems = [];
  function paletteCommands() {
    const cmds = [
      ["New chat", "plus", () => newTask(), "N"],
      ["Research a topic", "book", () => { newTask(); setMode(1); }],
      ["Browse a website", "globe", () => { newTask(); setMode(3); }],
      ["Analyse a video", "video", () => { newTask(); setMode(2); }],
      ["Office task", "briefcase", () => { newTask(); setMode(4); }],
      ["Budget", "wallet", () => showBudget()],
      ["Schedules", "calendar", () => sendText("/schedules")],
      ["Toggle theme", "sun", () => cycleTheme()],
      [S.notify ? "Turn notifications off" : "Turn notifications on", "bell", () => toggleNotify()],
      ["About this assistant", "settings", () => showSettings()],
      ["Keyboard shortcuts", "help", () => showShortcuts(), "?"],
      ["Sign out of this browser", "x", () => { store.set(TOKEN_KEY, null); S.token = null; pollGen++; showLogin(true); }],
    ];
    {
      const t = (getConv() || {}).activeWork;
      if (t) {
        cmds.unshift([`Cancel #${short(t.task_id)}`, "stop", () => confirmCancel(t)]);
        cmds.unshift([t.status === "PAUSED" ? `Resume #${short(t.task_id)}` : `Pause #${short(t.task_id)}`, t.status === "PAUSED" ? "play" : "pause",
          () => taskAction(t.task_id, t.status === "PAUSED" ? "resume" : "pause")]);
      }
    }
    return cmds;
  }
  function openPalette() { $("palette").hidden = false; $("paletteInput").value = ""; palIdx = 0; renderPalette(); setTimeout(() => $("paletteInput").focus(), 10); }
  function closePalette() { $("palette").hidden = true; }
  function renderPalette() {
    const q = $("paletteInput").value.trim().toLowerCase();
    const cmds = paletteCommands().filter(([l]) => !q || l.toLowerCase().includes(q)).map(([label, ic, run, hint]) => ({ group: "Commands", label, ic, run, hint }));
    const tasks = conversations().filter((c) => !q || c.tasks.some((t) => t.goal.toLowerCase().includes(q) || t.task_id.toLowerCase().includes(q))).slice(0, q ? 12 : 6)
      .map((c) => ({ group: "Chats", label: c.title, ic: wf(c.workflow)[1], run: () => openConv(c.key), hint: (STATUS[c.status] || [c.status])[0] }));
    palItems = q ? [...tasks, ...cmds] : [...cmds, ...tasks];
    palIdx = Math.min(palIdx, Math.max(0, palItems.length - 1));
    const list = $("paletteList"); list.replaceChildren();
    let group = null;
    palItems.forEach((it, i) => {
      if (it.group !== group) { group = it.group; list.appendChild(h("div", { class: "p-group" }, group)); }
      list.appendChild(h("button", { class: "p-item", role: "option", "aria-selected": String(i === palIdx), onclick: () => runPalette(i), onmousemove: () => { if (palIdx !== i) { palIdx = i; renderPalette(); } } },
        icon(it.ic), h("span", { class: "lbl" }, it.label), it.hint ? h("span", { class: "hint" }, it.hint) : null));
    });
    if (!palItems.length) list.appendChild(h("div", { class: "task-empty" }, "Nothing found"));
    list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }
  function runPalette(i) { const it = palItems[i]; closePalette(); if (it) it.run(); }
  $("paletteInput").addEventListener("input", () => { palIdx = 0; renderPalette(); });
  $("paletteInput").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); palIdx = (palIdx + (e.key === "ArrowDown" ? 1 : -1) + palItems.length) % Math.max(1, palItems.length); renderPalette(); }
    else if (e.key === "Enter") { e.preventDefault(); runPalette(palIdx); }
    else if (e.key === "Escape") closePalette();
  });
  $("palette").addEventListener("mousedown", (e) => { if (e.target === $("palette")) closePalette(); });

  function showModal(title, body) {
    const m = $("modalBody"); m.replaceChildren(
      h("div", { class: "modal-head" }, h("h2", {}, title), h("button", { class: "icon-btn", "aria-label": "Close", onclick: closeModal }, icon("x"))),
      h("div", { class: "modal-body" }, body));
    $("modal").hidden = false;
  }
  function closeModal() { $("modal").hidden = true; }
  $("modal").addEventListener("mousedown", (e) => { if (e.target === $("modal")) closeModal(); });

  async function showBudget() {
    await refreshBudget(); const b = S.budget; if (!b) return;
    const used = b.ceiling - b.remaining;
    const facts = h("dl", { class: "facts" });
    const f = (k, v) => facts.append(h("dt", {}, k), h("dd", {}, v));
    f("Period", `${b.period} (${b.period_timezone})`); f("Committed", `${cur(b.currency)}${used.toFixed(2)} of ${cur(b.currency)}${b.ceiling.toFixed(2)} (${Math.round(b.utilisation * 100)}%)`);
    f("Settled", `${cur(b.currency)}${b.settled.toFixed(2)}`); f("Reserved", `${cur(b.currency)}${b.reserved.toFixed(2)}`);
    f("Unknown", `${cur(b.currency)}${b.unknown.toFixed(2)}`); f("Per-task cap", `${cur(b.currency)}${b.per_task_ceiling.toFixed(2)}`);
    f("Models in use", (b.hosted_routes_enabled || []).join(", ") || "none");
    showModal("Model budget", h("div", {}, h("div", { class: "meter", style: "height:8px;margin-bottom:16px" }, h("div", { class: `meter-fill ${b.utilisation >= .9 ? "bad" : b.utilisation >= .7 ? "warn" : ""}`, style: `width:${Math.min(100, b.utilisation * 100)}%` })), facts));
  }
  async function showSettings() {
    try { S.info = await getJSON("/ui/api/info"); } catch { /* use cached */ }
    const i = S.info; if (!i) return;
    const facts = h("dl", { class: "facts", style: "grid-template-columns:140px 1fr" });
    const f = (k, v) => facts.append(h("dt", {}, k), h("dd", {}, v));
    for (const [role, r] of Object.entries(i.roles)) f(role[0].toUpperCase() + role.slice(1), r.model ? h("code", {}, r.model) : "not configured");
    f("Browser engine", i.browser_engine === "browser_use" ? "Browser Use" : "Built-in step loop");
    f("Browser approvals", i.browser_engine === "browser_use" ? (i.browser_approval ? "Once per run" : "Off") : "Per action");
    f("Browser profile", i.browser_authorized ? "Authorized" : "Not authorized");
    f("Web search", i.search_adapter); f("Telegram", i.telegram_connected ? `Connected (${i.telegram_mode})` : `Not connected (${i.telegram_mode})`);
    f("Timezone", i.timezone); f("Version", i.version);
    showModal("About this assistant", h("div", {}, facts, h("p", { style: "margin:16px 0 0;font-size:12.5px;color:var(--text-3)" }, "Change these in config/application.yaml and restart the assistant.")));
  }
  function showShortcuts() {
    const rows = [["⌘K / Ctrl K", "Command palette"], ["N", "New chat"], ["/", "Focus the message box"], ["Esc", "Close, or stop replying to a task"],
      ["Enter", "Send"], ["Shift Enter", "New line"], ["Alt ↑ / Alt ↓", "Previous / next chat"], ["?", "This list"]];
    const facts = h("dl", { class: "facts", style: "grid-template-columns:150px 1fr" });
    for (const [k, v] of rows) facts.append(h("dt", {}, h("kbd", {}, k)), h("dd", {}, v));
    showModal("Keyboard shortcuts", facts);
  }
  function newTask() { newChat(); setMode(0); input.value = ""; autosize(); updateSend(); input.focus(); }

  // ---------------------------------------------------------------------------------------- global events
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-action]"); if (!el) return;
    const a = el.dataset.action;
    if (a === "new-task") newTask();
    else if (a === "palette") openPalette();
    else if (a === "theme") cycleTheme();
    else if (a === "notifications") toggleNotify();
    else if (a === "settings") showSettings();
    else if (a === "budget") showBudget();
    else if (a === "attach") $("fileInput").click();
    else if (a === "open-sidebar") $("app").classList.add("sidebar-open");
    else if (a === "close-sidebar") $("app").classList.remove("sidebar-open");
    else if (a === "toggle-inspector") {
      const c = getConv();
      if ($("inspector").hidden && !S.selected && c && c.work) focusTask(c.work.task_id);
      openInspector($("inspector").hidden);
    }
    else if (a === "jump") { feed.scrollTo({ top: feed.scrollHeight, behavior: "smooth" }); $("jump").hidden = true; }
    else if (a === "close-lightbox") $("lightbox").hidden = true;
  });
  $("taskFilter").addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter]"); if (!b) return;
    S.filter = b.dataset.filter; $("taskFilter").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", String(x === b))); renderTasks();
  });
  $("taskSearch").addEventListener("input", (e) => { S.query = e.target.value; renderTasks(); });
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("palette").hidden ? openPalette() : closePalette(); return; }
    if (e.key === "Escape") {
      if (!$("lightbox").hidden) $("lightbox").hidden = true;
      else if (!$("modal").hidden) closeModal();
      else if (!$("palette").hidden) closePalette();
      else if ($("app").classList.contains("sidebar-open")) $("app").classList.remove("sidebar-open");
      else if (!typing && !$("inspector").hidden && window.innerWidth <= 1180) openInspector(false);
      return;
    }
    if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown") && S.tasks.length) {
      e.preventDefault();
      const keys = conversations().map((c) => c.key);
      if (!keys.includes(S.conv)) keys.unshift(S.conv);  // the empty chat being drafted sits above the list
      const i = keys.indexOf(S.conv);
      openConv(keys[Math.max(0, Math.min(keys.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))]); return;
    }
    if (typing || e.metaKey || e.ctrlKey || e.altKey || !$("palette").hidden || !$("modal").hidden) return;
    if (e.key === "/") { e.preventDefault(); input.focus(); }
    else if (e.key === "n" || e.key === "N") { e.preventDefault(); newTask(); }
    else if (e.key === "?") { e.preventDefault(); showShortcuts(); }
  });
  window.addEventListener("resize", debounce(() => { if (S.selected && window.innerWidth > 1180 && S.inspectorOpen && $("inspector").hidden) openInspector(true, true); }, 150));

  // ---------------------------------------------------------------------------------------- start
  async function start() {
    if (!S.token) { showLogin(true); return; }
    try { S.info = await getJSON("/ui/api/info"); } catch (e) { if (e instanceof AuthError) return; }
    showLogin(false);
    await Promise.all([refreshTasks(), refreshBudget()]);
    openConv(S.conv);
    pollLoop();
  }
  hydrateIcons(); applyTheme(); renderNotifyBtn(); renderModes(); updatePlaceholder(); updateSend(); renderFeed();
  setInterval(tickCountdowns, 1000);
  setInterval(() => { if (S.token && !document.hidden) refreshTasks(); }, 6000);
  setInterval(() => { if (S.token && !document.hidden) refreshBudget(); }, 30000);
  setInterval(() => { if (!document.hidden) renderTasks(); }, 60000);
  window.addEventListener("hashchange", () => {
    const t = new URLSearchParams(location.hash.slice(1)).get("token");
    if (t) { store.set(TOKEN_KEY, t); S.token = t; history.replaceState(null, "", location.pathname); start(); }
  });
  start();
})();
