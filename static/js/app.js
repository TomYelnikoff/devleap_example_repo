/* Bedrock Chat front end.
 *
 * Conversations are stored on the server in SQLite and loaded through the
 * /api/conversations endpoints. Sending a message posts only the new text;
 * the server adds the saved history, calls Bedrock's converse_stream, and
 * streams the reply back as NDJSON.
 *
 * Only the chat settings (system prompt, temperature, max tokens) are kept
 * in this browser's localStorage.
 */
(() => {
  "use strict";

  const CONFIG = window.APP_CONFIG || { modelName: "Claude", defaults: {} };
  const DEFAULTS = {
    system: CONFIG.defaults.system || "",
    temperature: Number(CONFIG.defaults.temperature ?? 0.5),
    maxTokens: Number(CONFIG.defaults.maxTokens ?? 4096),
    contextMessages: Number(CONFIG.defaults.contextMessages ?? 3),
  };
  const MAX_TOKENS_LIMIT = Number(CONFIG.defaults.maxTokensLimit ?? 32000);
  const MAX_CONTEXT_MESSAGES = Number(CONFIG.defaults.contextMessagesLimit ?? 50);
  const SETTINGS_KEY = "bedrock-chat:settings:v1";
  const NEW_TITLE = "New chat";

  /* ---------- DOM ---------- */

  const $ = (id) => document.getElementById(id);
  const els = {
    app: $("app"),
    sidebar: $("sidebar"),
    scrim: $("scrim"),
    menuBtn: $("menuBtn"),
    sidebarClose: $("sidebarClose"),
    newChat: $("newChat"),
    historyList: $("historyList"),
    historyEmpty: $("historyEmpty"),
    connection: $("connection"),
    connectionText: $("connectionText"),
    chatTitle: $("chatTitle"),
    thread: $("thread"),
    welcome: $("welcome"),
    starters: $("starters"),
    messages: $("messages"),
    composer: $("composer"),
    prompt: $("prompt"),
    sendBtn: $("sendBtn"),
    sendLabel: document.querySelector("#sendBtn .send-label"),
    settingsBtn: $("settingsBtn"),
    settingsDialog: $("settingsDialog"),
    systemPrompt: $("systemPrompt"),
    temperature: $("temperature"),
    temperatureValue: $("temperatureValue"),
    maxTokens: $("maxTokens"),
    contextMessages: $("contextMessages"),
    resetSettings: $("resetSettings"),
    toast: $("toast"),
    plan: $("plan"),
    planName: $("planName"),
    planCount: $("planCount"),
    planMeter: $("planMeter"),
    planMeterFill: $("planMeterFill"),
    planUpgrade: $("planUpgrade"),
    limitNotice: $("limitNotice"),
    limitText: $("limitText"),
  };

  const ICONS = {
    copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/></svg>',
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    retry: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.3-4.9L4 8M4 4v4h4M4 13a8 8 0 0 0 14.3 4.9L20 16M20 20v-4h-4"/></svg>',
    trash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/></svg>',
    mark: '<svg viewBox="0 0 24 24" aria-hidden="true">' +
      '<rect x="3" y="5" width="14" height="3.5" rx="1.75" fill="currentColor" stroke="none"/>' +
      '<rect x="6" y="10.25" width="15" height="3.5" rx="1.75" fill="currentColor" stroke="none" opacity=".7"/>' +
      '<rect x="3" y="15.5" width="12" height="3.5" rx="1.75" fill="currentColor" stroke="none" opacity=".45"/></svg>',
  };

  /* ---------- Settings storage (per browser, best effort) ---------- */

  function loadSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
      return { ...DEFAULTS, ...(saved && typeof saved === "object" ? saved : {}) };
    } catch {
      return { ...DEFAULTS };
    }
  }

  function saveSettings(settings) {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    } catch {
      /* storage blocked: settings last for this visit only */
    }
  }

  /* ---------- State ---------- */

  const state = {
    conversations: [],     // sidebar summaries: {id, title, createdAt, updatedAt}
    loaded: new Map(),     // id -> full conversation {id, title, messages}
    activeId: null,
    historyError: "",
    busy: false,           // creating a conversation or streaming a reply
    streamingConvoId: null,
    controller: null,
    stopRequest: null,     // pending /stop call; resolves to the saved reply
    account: CONFIG.account || null,  // {plan, messagesUsed, messageLimit, messagesLeft, price}
    openRequest: 0,        // ignores stale responses when switching chats quickly
    settings: loadSettings(),
  };

  const activeConversation = () => (state.activeId && state.loaded.get(state.activeId)) || null;

  let tempIds = 0;
  const tempId = () => `local-${++tempIds}`;

  function makeTitle(text) {
    const flat = text.replace(/\s+/g, " ").trim();
    if (flat.length <= 60) return flat;
    const cut = flat.slice(0, 60);
    const space = cut.lastIndexOf(" ");
    return (space > 30 ? cut.slice(0, space) : cut) + "…";
  }

  /* ---------- API ---------- */

  async function api(path, options = {}) {
    let res;
    try {
      res = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      });
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw new Error("Couldn't reach the chat server. Check that the Flask app is still running.");
    }
    if (!res.ok) {
      // Signed out (for example, the session expired): go to the sign-in page.
      if (res.status === 401) window.location.assign("/login");
      const data = await res.json().catch(() => ({}));
      const error = new Error(data.error || `The server returned an error (HTTP ${res.status}).`);
      error.status = res.status;
      error.data = data;
      throw error;
    }
    return res.status === 204 ? null : res;
  }

  const apiJson = async (path, options) => (await api(path, options)).json();
  const conversationUrl = (id, suffix = "") => `/api/conversations/${encodeURIComponent(id)}${suffix}`;

  /* ---------- Formatting ---------- */

  const numberFmt = new Intl.NumberFormat();

  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Escape, then render `inline code` (used for server error messages).
  const formatNote = (text) => escapeHtml(text).replace(/`([^`]+)`/g, "<code>$1</code>");

  const hasMarkdown = typeof window.marked !== "undefined" && typeof window.DOMPurify !== "undefined";
  if (hasMarkdown) {
    window.marked.use({ gfm: true, breaks: false });
    window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A") {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    });
  }

  function renderMarkdown(text) {
    if (hasMarkdown) {
      return window.DOMPurify.sanitize(window.marked.parse(text));
    }
    // Fallback if the CDN scripts didn't load: plain paragraphs.
    return escapeHtml(text)
      .split(/\n{2,}/)
      .map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`)
      .join("");
  }

  // Add copy buttons to code blocks and make wide tables scroll.
  function enhanceProse(prose) {
    prose.querySelectorAll("table").forEach((table) => {
      if (table.parentElement.classList.contains("table-wrap")) return;
      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      table.replaceWith(wrap);
      wrap.appendChild(table);
    });

    prose.querySelectorAll("pre").forEach((pre) => {
      if (pre.parentElement.classList.contains("code-block")) return;
      const wrap = document.createElement("div");
      wrap.className = "code-block";
      pre.replaceWith(wrap);
      wrap.appendChild(pre);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "code-copy";
      btn.textContent = "Copy";
      btn.setAttribute("aria-label", "Copy code");
      btn.addEventListener("click", () => {
        copyText(pre.innerText).then((ok) => {
          btn.textContent = ok ? "Copied" : "Copy failed";
          setTimeout(() => (btn.textContent = "Copy"), 1600);
        });
      });
      wrap.appendChild(btn);
    });
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch { ok = false; }
      area.remove();
      return ok;
    }
  }

  let toastTimer = 0;
  function showToast(message) {
    els.toast.textContent = message;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (els.toast.hidden = true), 5000);
  }

  /* ---------- Rendering ---------- */

  function renderHistory() {
    const list = els.historyList;
    list.replaceChildren();

    if (state.historyError) {
      els.historyEmpty.textContent = state.historyError;
      els.historyEmpty.hidden = false;
    } else {
      els.historyEmpty.textContent = "Your conversations will appear here.";
      els.historyEmpty.hidden = state.conversations.length > 0;
    }

    for (const convo of state.conversations) {
      const title = convo.title || NEW_TITLE;
      const li = document.createElement("li");
      li.className = "history-item" + (convo.id === state.activeId ? " is-active" : "");

      const open = document.createElement("button");
      open.type = "button";
      open.className = "history-open";
      open.textContent = title;
      open.title = title;
      if (convo.id === state.activeId) open.setAttribute("aria-current", "true");
      open.addEventListener("click", () => openConversation(convo.id));

      const del = document.createElement("button");
      del.type = "button";
      del.className = "history-delete";
      del.innerHTML = ICONS.trash;
      del.setAttribute("aria-label", `Delete conversation: ${title}`);
      del.title = "Delete conversation";
      del.addEventListener("click", () => deleteConversation(convo.id));

      li.append(open, del);
      list.appendChild(li);
    }
  }

  function renderThread() {
    const convo = activeConversation();
    const messages = convo ? convo.messages : [];

    els.chatTitle.textContent = convo ? convo.title : NEW_TITLE;
    document.title = convo ? `${convo.title} | Bedrock Chat` : "Bedrock Chat";
    els.welcome.hidden = messages.length > 0;

    els.messages.replaceChildren(...messages.map((m) => buildMessage(m, convo)));
  }

  function buildMessage(msg, convo) {
    const li = document.createElement("li");
    li.dataset.id = String(msg.id);

    if (msg.role === "user") {
      li.className = "msg msg-user";
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = msg.content;
      li.appendChild(bubble);
      return li;
    }

    li.className = "msg msg-assistant";
    li.innerHTML =
      `<div class="avatar">${ICONS.mark}</div>` +
      '<div class="msg-body"><div class="prose"></div></div>';
    updateAssistant(li, msg, convo);
    return li;
  }

  // Re-render an assistant message in place (called repeatedly while streaming).
  function updateAssistant(li, msg, convo) {
    const body = li.querySelector(".msg-body");
    const prose = body.querySelector(".prose");

    li.classList.toggle("is-pending", msg.status === "pending");
    li.classList.toggle("is-streaming", msg.status === "streaming");

    prose.innerHTML = msg.content ? renderMarkdown(msg.content) : "";
    prose.hidden = !msg.content;
    enhanceProse(prose);

    body.querySelectorAll(".pending-text, .msg-note, .msg-meta").forEach((n) => n.remove());

    if (msg.status === "pending") {
      const p = document.createElement("p");
      p.className = "pending-text";
      p.textContent = `${CONFIG.modelName} is writing…`;
      body.appendChild(p);
      return;
    }
    if (msg.status === "streaming") return;

    const note = noteFor(msg);
    if (note) {
      const p = document.createElement("p");
      p.className = "msg-note" + (msg.status === "error" ? " is-error" : "");
      p.innerHTML = formatNote(note);
      body.appendChild(p);
    }

    const meta = document.createElement("div");
    meta.className = "msg-meta";

    if (msg.content) {
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "meta-btn";
      copy.innerHTML = `${ICONS.copy}<span>Copy</span>`;
      copy.addEventListener("click", () => {
        copyText(msg.content).then((ok) => {
          copy.innerHTML = ok ? `${ICONS.check}<span>Copied</span>` : `${ICONS.copy}<span>Copy failed</span>`;
          setTimeout(() => (copy.innerHTML = `${ICONS.copy}<span>Copy</span>`), 1600);
        });
      });
      meta.appendChild(copy);
    }

    const isLast = convo && convo.messages[convo.messages.length - 1] === msg;
    if (isLast && (msg.status === "error" || msg.status === "stopped")) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "meta-btn";
      retry.innerHTML = `${ICONS.retry}<span>Retry</span>`;
      retry.addEventListener("click", () => retryLast(convo.id));
      meta.appendChild(retry);
    }

    if (msg.usage && (msg.usage.inputTokens != null || msg.usage.outputTokens != null)) {
      const usage = document.createElement("span");
      const parts = [];
      if (msg.usage.inputTokens != null) parts.push(`${numberFmt.format(msg.usage.inputTokens)} input tokens`);
      if (msg.usage.outputTokens != null) parts.push(`${numberFmt.format(msg.usage.outputTokens)} output tokens`);
      if (msg.usage.latencyMs != null) parts.push(`${(msg.usage.latencyMs / 1000).toFixed(1)} s`);
      usage.textContent = parts.join(", ");
      meta.appendChild(usage);
    }

    if (meta.childElementCount) body.appendChild(meta);
  }

  function noteFor(msg) {
    if (msg.status === "error") return msg.error || "Something went wrong. Retry the message.";
    if (msg.status === "stopped") return msg.content ? "Stopped. The answer above is incomplete." : "Stopped before a reply arrived.";
    if (msg.stopReason === "max_tokens") {
      return "This answer reached the maximum response length. Raise it in Chat settings to allow longer answers.";
    }
    if (msg.stopReason === "content_filtered" || msg.stopReason === "guardrail_intervened") {
      return "Part of this response was blocked by a content filter.";
    }
    return "";
  }

  function renderAll() {
    renderHistory();
    renderThread();
  }

  /* ---------- Scrolling ---------- */

  const isNearBottom = () => {
    const t = els.thread;
    return t.scrollHeight - t.scrollTop - t.clientHeight < 120;
  };
  const scrollToBottom = () => els.thread.scrollTo({ top: els.thread.scrollHeight, behavior: "instant" });

  /* ---------- URL ---------- */

  function setUrl(id, { replace = false } = {}) {
    const path = id ? `/c/${encodeURIComponent(id)}` : "/";
    if (window.location.pathname === path) return;
    window.history[replace ? "replaceState" : "pushState"](null, "", path);
  }

  function idFromUrl() {
    const match = window.location.pathname.match(/^\/c\/([^/]+)$/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  window.addEventListener("popstate", () => {
    const id = idFromUrl();
    if (id) openConversation(id, { updateUrl: false });
    else startNewChat({ updateUrl: false });
  });

  /* ---------- Conversations ---------- */

  async function loadConversationList() {
    try {
      const data = await apiJson("/api/conversations");
      state.conversations = data.conversations;
      state.historyError = "";
    } catch (err) {
      state.historyError = `Couldn't load your conversations. ${err.message}`;
    }
    renderHistory();
  }

  function touchSummary(convo) {
    const now = Date.now();
    let summary = state.conversations.find((c) => c.id === convo.id);
    if (!summary) {
      summary = { id: convo.id, title: convo.title, createdAt: now, updatedAt: now };
      state.conversations.push(summary);
    }
    summary.updatedAt = now;
    state.conversations.sort((a, b) => b.updatedAt - a.updatedAt);
  }

  async function openConversation(id, { updateUrl = true } = {}) {
    closeSidebar();
    const request = ++state.openRequest;

    // Keep the in-memory copy while its reply is streaming; otherwise reload it.
    if (!(state.streamingConvoId === id && state.loaded.has(id))) {
      try {
        const convo = await apiJson(conversationUrl(id));
        if (request !== state.openRequest) return;
        state.loaded.set(id, convo);
      } catch (err) {
        if (request !== state.openRequest) return;
        if (err.status === 404) {
          state.conversations = state.conversations.filter((c) => c.id !== id);
          state.loaded.delete(id);
        }
        showToast(err.message);
        startNewChat({ updateUrl: false });
        setUrl(null, { replace: true });
        return;
      }
    }

    state.activeId = id;
    if (updateUrl) setUrl(id);
    renderAll();
    scrollToBottom();
    focusPrompt();
  }

  function startNewChat({ updateUrl = true } = {}) {
    state.openRequest++;
    state.activeId = null;
    if (updateUrl) setUrl(null);
    renderAll();
    closeSidebar();
    focusPrompt();
  }

  async function deleteConversation(id) {
    const summary = state.conversations.find((c) => c.id === id);
    if (!summary) return;
    if (!window.confirm(`Delete "${summary.title}"? This can't be undone.`)) return;

    if (state.streamingConvoId === id) stopStreaming();
    try {
      await api(conversationUrl(id), { method: "DELETE" });
    } catch (err) {
      if (err.status !== 404) {
        showToast(`Couldn't delete the conversation. ${err.message}`);
        return;
      }
    }
    state.conversations = state.conversations.filter((c) => c.id !== id);
    state.loaded.delete(id);
    if (state.activeId === id) {
      startNewChat({ updateUrl: false });
      setUrl(null, { replace: true });
    } else {
      renderHistory();
    }
  }

  /* ---------- Plan and message allowance ---------- */

  const limitReached = () =>
    !!state.account && state.account.plan !== "pro" && state.account.messagesLeft <= 0;

  function renderAccount() {
    const acct = state.account;
    if (!acct) return;
    const pro = acct.plan === "pro";
    const limit = acct.messageLimit;
    const used = Math.min(acct.messagesUsed, limit ?? acct.messagesUsed);
    const out = limitReached();

    els.plan.classList.toggle("is-out", out);
    els.plan.classList.toggle("is-low", !out && !pro && acct.messagesLeft <= 3);
    els.planName.textContent = pro ? "Pro plan" : "Free plan";
    els.planCount.textContent = pro ? "Unlimited messages" : `${used} of ${limit} messages used`;

    els.planMeter.hidden = pro;
    if (!pro) {
      els.planMeter.setAttribute("aria-valuemax", String(limit));
      els.planMeter.setAttribute("aria-valuenow", String(used));
      els.planMeterFill.style.width = `${Math.min(100, (used / limit) * 100)}%`;
    }
    els.planUpgrade.hidden = pro;
    els.planUpgrade.textContent = `Upgrade to Pro, ${acct.price}`;

    els.limitNotice.hidden = !out;
    if (out) {
      els.limitText.textContent =
        `You've used all ${limit} free messages. Pro gives you unlimited messages for ${acct.price}.`;
    }
    updateSendState();
  }

  async function refreshAccount() {
    try {
      state.account = await apiJson("/api/account");
      renderAccount();
    } catch {
      /* keep showing the last known allowance */
    }
  }

  // Payment isn't connected yet, so the Upgrade buttons intentionally do nothing.
  // When a payment provider is added, start its checkout from this function.
  function startCheckout() {}

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-upgrade]")) startCheckout();
  });

  /* ---------- Sending & streaming ---------- */

  function setBusy(on) {
    state.busy = on;
    els.sendBtn.classList.toggle("is-stop", on);
    els.sendLabel.textContent = on ? "Stop" : "Send";
    els.sendBtn.setAttribute("aria-label", on ? "Stop generating" : "Send message");
    els.messages.setAttribute("aria-busy", on ? "true" : "false");
    updateSendState();
  }

  function updateSendState() {
    els.sendBtn.disabled = !state.busy && (els.prompt.value.trim() === "" || limitReached());
  }

  function stopStreaming() {
    if (!state.controller) return;
    // Ask the server to stop; it answers with the reply as saved, which
    // streamReply() then shows. Drop the connection so the text stops now.
    const convoId = state.streamingConvoId;
    state.stopRequest = convoId
      ? fetch(conversationUrl(convoId, "/stop"), { method: "POST" })
          .then((res) => (res.status === 200 ? res.json() : null))
          .catch(() => null)
      : Promise.resolve(null);
    state.controller.abort();
  }

  async function sendMessage(text) {
    setBusy(true);
    let convo = activeConversation();

    if (!convo) {
      try {
        const created = await apiJson("/api/conversations", {
          method: "POST",
          body: JSON.stringify({ title: makeTitle(text) }),
        });
        convo = { ...created, messages: [] };
      } catch (err) {
        setBusy(false);
        els.prompt.value = text; // don't lose what they typed
        autoGrow();
        showToast(`Couldn't start a new conversation. ${err.message}`);
        return;
      }
      state.loaded.set(convo.id, convo);
      state.activeId = convo.id;
      touchSummary(convo);
      setUrl(convo.id);
    }

    convo.messages.push({ id: tempId(), role: "user", content: text });
    await streamReply(convo, conversationUrl(convo.id, "/messages"), { content: text, settings: state.settings });
  }

  async function retryLast(convoId) {
    if (state.busy) return;
    const convo = state.loaded.get(convoId);
    if (!convo) return;
    const last = convo.messages[convo.messages.length - 1];
    if (last && last.role === "assistant") convo.messages.pop();
    setBusy(true);
    await streamReply(convo, conversationUrl(convo.id, "/retry"), { settings: state.settings });
  }

  async function streamReply(convo, url, body) {
    const reply = { id: tempId(), role: "assistant", content: "", status: "pending" };
    convo.messages.push(reply);
    touchSummary(convo);

    renderAll();
    scrollToBottom();

    const controller = new AbortController();
    state.controller = controller;
    state.streamingConvoId = convo.id;

    // Find the live node each time: the user may switch chats mid-stream.
    const liveNode = () =>
      state.activeId === convo.id ? els.messages.querySelector(`[data-id="${reply.id}"]`) : null;

    let frame = 0;
    const scheduleRender = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const node = liveNode();
        if (!node) return;
        const stick = isNearBottom();
        updateAssistant(node, reply, convo);
        if (stick) scrollToBottom();
      });
    };

    const handleEvent = (event) => {
      switch (event.type) {
        case "delta":
          reply.content += event.text;
          reply.status = "streaming";
          scheduleRender();
          break;
        case "stop":
          reply.stopReason = event.stopReason;
          break;
        case "usage":
          reply.usage = { inputTokens: event.inputTokens, outputTokens: event.outputTokens, latencyMs: event.latencyMs };
          break;
        case "error":
          reply.status = "error";
          reply.error = event.message;
          break;
      }
    };

    let refused = false; // the server said the free messages are used up

    try {
      const res = await api(url, {
        method: "POST",
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let newline;
        while ((newline = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, newline).trim();
          buffer = buffer.slice(newline + 1);
          if (line) handleEvent(JSON.parse(line));
        }
      }
      if (buffer.trim()) handleEvent(JSON.parse(buffer));

      if (reply.status !== "error") {
        reply.status = reply.content ? "done" : "error";
        if (!reply.content) reply.error = "The model returned an empty response. Retry the message.";
      }
    } catch (err) {
      if (err.name === "AbortError") {
        reply.status = "stopped";
        // Show what the server saved: the reply may have finished just before Stop.
        const saved = await (state.stopRequest || Promise.resolve(null));
        state.stopRequest = null;
        const message = saved && saved.message;
        if (message && message.role === "assistant") {
          Object.assign(reply, {
            content: message.content,
            status: message.status,
            error: message.error,
            stopReason: message.stopReason,
            usage: message.usage,
          });
        }
      } else if (err.status === 402) {
        refused = true;
        if (err.data && err.data.account) state.account = err.data.account;
        showToast(err.message);
      } else {
        reply.status = "error";
        reply.error = err instanceof TypeError
          ? "The connection to the chat server was interrupted. Retry the message."
          : err.message;
      }
    } finally {
      if (frame) cancelAnimationFrame(frame);
      state.controller = null;
      state.streamingConvoId = null;
      setBusy(false);

      if (refused) {
        // The server didn't save or answer this message: take it back out of the
        // thread and put the text back in the box so nothing is lost.
        convo.messages.pop(); // the pending reply
        const sent = convo.messages[convo.messages.length - 1];
        if (sent && sent.role === "user" && String(sent.id).startsWith("local-")) {
          convo.messages.pop();
          els.prompt.value = body.content || "";
          autoGrow();
        }
        if (convo.messages.length === 0) {
          // A brand-new chat that never got a message: don't leave an empty one behind.
          api(conversationUrl(convo.id), { method: "DELETE" }).catch(() => {});
          state.conversations = state.conversations.filter((c) => c.id !== convo.id);
          state.loaded.delete(convo.id);
          if (state.activeId === convo.id) {
            state.activeId = null;
            setUrl(null, { replace: true });
          }
        }
        renderAll();
        renderAccount();
      } else {
        touchSummary(convo);
        renderHistory();
        const node = liveNode();
        if (node) {
          const stick = isNearBottom();
          updateAssistant(node, reply, convo);
          if (stick) scrollToBottom();
        }
        refreshAccount(); // the allowance changed (or was refunded)
      }
    }
  }

  // Leaving the page mid-reply: ask the server to stop and keep the partial answer.
  window.addEventListener("pagehide", () => {
    if (state.streamingConvoId && navigator.sendBeacon) {
      navigator.sendBeacon(conversationUrl(state.streamingConvoId, "/stop"));
    }
  });

  /* ---------- Composer ---------- */

  function autoGrow() {
    const ta = els.prompt;
    ta.style.height = "auto";
    ta.style.height = `${ta.scrollHeight}px`;
    updateSendState();
  }

  const isMobile = window.matchMedia("(max-width: 860px)");

  function focusPrompt() {
    if (!isMobile.matches) els.prompt.focus();
  }

  els.composer.addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.busy) {
      if (e.submitter === els.sendBtn) stopStreaming();
      return;
    }
    const text = els.prompt.value.trim();
    if (!text) return;
    if (limitReached()) {
      showToast("You've used all your free messages. Upgrade to Pro to keep chatting.");
      return;
    }
    els.prompt.value = "";
    autoGrow();
    sendMessage(text);
  });

  els.prompt.addEventListener("input", autoGrow);
  els.prompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!state.busy) els.composer.requestSubmit();
    } else if (e.key === "Escape" && state.busy) {
      stopStreaming();
    }
  });

  els.starters.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-prompt]");
    if (!btn) return;
    els.prompt.value = btn.dataset.prompt;
    autoGrow();
    els.prompt.focus();
    els.prompt.setSelectionRange(els.prompt.value.length, els.prompt.value.length);
  });

  els.newChat.addEventListener("click", () => {
    els.prompt.value = "";
    autoGrow();
    startNewChat();
  });

  /* ---------- Sidebar (mobile drawer) ---------- */

  function setSidebar(open) {
    els.app.classList.toggle("sidebar-open", open);
    els.scrim.hidden = !open;
    els.menuBtn.setAttribute("aria-expanded", String(open));
    els.sidebar.inert = isMobile.matches && !open;
    if (open) els.newChat.focus();
  }
  const closeSidebar = () => {
    if (els.app.classList.contains("sidebar-open")) {
      setSidebar(false);
    } else {
      els.sidebar.inert = isMobile.matches;
    }
  };

  els.menuBtn.addEventListener("click", () => setSidebar(true));
  els.sidebarClose.addEventListener("click", () => { setSidebar(false); els.menuBtn.focus(); });
  els.scrim.addEventListener("click", () => setSidebar(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && els.app.classList.contains("sidebar-open")) {
      setSidebar(false);
      els.menuBtn.focus();
    }
  });
  isMobile.addEventListener("change", () => setSidebar(false));

  /* ---------- Settings ---------- */

  function fillSettings(values) {
    els.systemPrompt.value = values.system;
    els.temperature.value = String(values.temperature);
    els.temperatureValue.textContent = Number(values.temperature).toFixed(1);
    els.maxTokens.value = String(values.maxTokens);
    els.contextMessages.value = String(values.contextMessages);
  }

  els.settingsBtn.addEventListener("click", () => {
    fillSettings(state.settings);
    els.settingsDialog.returnValue = ""; // Esc must not re-apply a previous "save"
    els.settingsDialog.showModal();
  });

  els.temperature.addEventListener("input", () => {
    els.temperatureValue.textContent = Number(els.temperature.value).toFixed(1);
  });

  els.resetSettings.addEventListener("click", () => fillSettings(DEFAULTS));

  els.settingsDialog.addEventListener("close", () => {
    if (els.settingsDialog.returnValue !== "save") return;
    const maxTokens = Math.round(Number(els.maxTokens.value));
    const contextMessages = Math.round(Number(els.contextMessages.value));
    state.settings = {
      system: els.systemPrompt.value.trim(),
      temperature: Math.min(1, Math.max(0, Number(els.temperature.value))),
      maxTokens: Number.isFinite(maxTokens) ? Math.min(MAX_TOKENS_LIMIT, Math.max(256, maxTokens)) : DEFAULTS.maxTokens,
      contextMessages: Number.isFinite(contextMessages)
        ? Math.min(MAX_CONTEXT_MESSAGES, Math.max(1, contextMessages))
        : DEFAULTS.contextMessages,
    };
    saveSettings(state.settings);
  });

  /* ---------- AWS connection status ---------- */

  async function checkConnection() {
    try {
      const data = await apiJson("/api/status");
      if (data.connected) {
        els.connection.className = "connection is-ok";
        els.connectionText.textContent = `Connected to AWS account ${data.account} in ${data.region}`;
        els.connection.title = data.arn;
      } else {
        els.connection.className = "connection is-error";
        els.connectionText.innerHTML = formatNote(data.error || "AWS connection failed.");
      }
    } catch {
      els.connection.className = "connection is-error";
      els.connectionText.textContent = "Couldn't reach the chat server.";
    }
  }

  /* ---------- Start ---------- */

  async function start() {
    renderAll();
    renderAccount();
    els.sidebar.inert = isMobile.matches;
    autoGrow();
    focusPrompt();
    checkConnection();
    await loadConversationList();
    const initialId = CONFIG.initialConversationId || idFromUrl();
    if (initialId) await openConversation(initialId, { updateUrl: false });
  }

  start();
})();
