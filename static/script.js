/* Book Worm AI Studio UI (Settings + Owner Admin + Canon + Pricing)
   Works with:
   - /auth/me, /auth/login, /auth/register, /auth/logout
   - /owner/unlock, /owner/lock
   - /canon/save, /canon/list
   - /stripe/create-checkout-session
*/

(() => {
  "use strict";

  // ---------- helpers ----------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const show = (el) => el && el.classList.remove("hidden");
  const hide = (el) => el && el.classList.add("hidden");

  const modalOpen = (id) => show(document.getElementById(id));
  const modalClose = (id) => hide(document.getElementById(id));

  const toast = (msg) => {
    // tiny non-invasive toast
    console.log(msg);
    const t = document.createElement("div");
    t.textContent = msg;
    t.style.position = "fixed";
    t.style.bottom = "16px";
    t.style.left = "16px";
    t.style.padding = "10px 12px";
    t.style.borderRadius = "10px";
    t.style.background = "rgba(0,0,0,.8)";
    t.style.color = "white";
    t.style.zIndex = "9999";
    t.style.fontSize = "14px";
    t.style.maxWidth = "60vw";
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2400);
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(opts.headers || {}),
      },
      ...opts,
    });

    // attempt JSON always
    let data = null;
    const text = await res.text();
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }

    if (!res.ok) {
      const detail = (data && (data.detail || data.error)) ? (data.detail || data.error) : `HTTP ${res.status}`;
      const err = new Error(detail);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // ---------- state ----------
  const state = {
    me: null,
    settings: null,
    projectId: null,
    tab: "chat",
    ownerUnlocked: false,
  };

  const TAB_IDS = ["chat", "writing", "gamedev", "musicdev", "imagelab", "voicelab", "gamedesigner"];

  // ---------- elements ----------
  const elUserBadge = $("#userBadge");
  const btnSettings = $("#btnSettings");
  const btnLogout = $("#btnLogout");

  const settingsModal = $("#settingsModal");
  const settingsEmail = $("#settingsEmail");
  const settingsPlan = $("#settingsPlan");
  const settingsOwner = $("#settingsOwner");
  const btnOpenPricing = $("#btnOpenPricing");
  const ownerCode = $("#ownerCode");
  const btnOwnerLogin = $("#btnOwnerLogin");
  const btnOwnerLogout = $("#btnOwnerLogout");
  const ownerHint = $("#ownerHint");
  const adminCard = $("#adminCard");

  const authModal = $("#authModal");
  const authModeLogin = $("#authModeLogin");
  const authModeRegister = $("#authModeRegister");
  const authEmail = $("#authEmail");
  const authPassword = $("#authPassword");
  const btnAuthSubmit = $("#btnAuthSubmit");
  const authHint = $("#authHint");

  const canonModal = $("#canonModal");
  const canonList = $("#canonList");

  const pricingModal = $("#pricingModal");

  const btnSend = $("#btnSend");
  const promptInput = $("#prompt");
  const output = $("#output");

  const btnNewProject = $("#btnNewProject");
  const btnSwitchProject = $("#btnSwitchProject");

  // ---------- UI rendering ----------
  function setBadge() {
    if (!elUserBadge) return;
    if (state.me?.logged_in && state.me?.email) {
      elUserBadge.textContent = state.me.email;
      show(elUserBadge);
      show(btnLogout);
    } else {
      hide(elUserBadge);
      hide(btnLogout);
    }
  }

  function setOwnerUI() {
    // admin card appears only when owner unlocked
    if (state.ownerUnlocked) show(adminCard);
    else hide(adminCard);

    if (settingsOwner) settingsOwner.textContent = state.ownerUnlocked ? "Yes" : "No";
  }

  function setSettingsUI() {
    if (!state.settings) return;
    if (settingsEmail) settingsEmail.textContent = state.settings.me?.email || "—";
    if (settingsPlan) settingsPlan.textContent = state.settings.me?.plan || "—";
    state.ownerUnlocked = !!state.settings.me?.is_owner;
    setOwnerUI();
  }

  function appendMessage(role, text) {
    if (!output) return;
    const row = document.createElement("div");
    row.className = `msg ${role}`;
    row.textContent = text;
    output.appendChild(row);
    output.scrollTop = output.scrollHeight;
  }

  // ---------- auth ----------
  let authMode = "login"; // or register
  function setAuthMode(mode) {
    authMode = mode;
    if (!authHint) return;
    authHint.textContent = "";
    if (authModeLogin) authModeLogin.classList.toggle("active", mode === "login");
    if (authModeRegister) authModeRegister.classList.toggle("active", mode === "register");
  }

  async function refreshMe() {
    try {
      state.me = await api("/auth/me", { method: "GET" });
    } catch {
      state.me = { logged_in: false };
    }
    setBadge();
  }

  async function ensureLoggedIn() {
    await refreshMe();
    if (!state.me?.logged_in) {
      modalOpen("authModal");
      throw new Error("Not logged in");
    }
  }

  // ---------- settings ----------
  async function loadSettings() {
    state.settings = await api("/api/settings", { method: "GET" });
    setSettingsUI();
  }

  async function openSettings() {
    modalOpen("settingsModal");
    try {
      await loadSettings();
    } catch (e) {
      toast(`Settings error: ${e.message}`);
    }
  }

  // ---------- canon ----------
  async function saveToCanon() {
    await ensureLoggedIn();

    const content = (promptInput?.value || "").trim();
    if (!content) {
      toast("Type something first, then Save to Canon.");
      return;
    }

    const title = `Saved (${state.tab})`;
    await api("/canon/save", {
      method: "POST",
      body: JSON.stringify({
        tab: state.tab,
        title,
        content,
      }),
    });
    toast("Saved to canon ✅");
  }

  async function viewCanon() {
    await ensureLoggedIn();
    modalOpen("canonModal");
    canonList.innerHTML = "Loading…";
    try {
      const data = await api("/canon/list", { method: "GET" });
      const items = data?.items || [];
      if (!items.length) {
        canonList.innerHTML = "<div class='hint'>No canon entries yet.</div>";
        return;
      }
      canonList.innerHTML = items.map((it) => {
        const t = (it.title || "Untitled").replaceAll("<", "&lt;");
        const c = (it.content || "").replaceAll("<", "&lt;");
        const when = (it.created_at || "").replaceAll("<", "&lt;");
        return `
          <div class="canonItem">
            <div class="canonTitle">${t}</div>
            <div class="canonMeta">${when} • ${it.tab || ""}</div>
            <div class="canonBody">${c}</div>
          </div>
        `;
      }).join("");
    } catch (e) {
      canonList.innerHTML = `<div class="hint">Canon error: ${e.message}</div>`;
    }
  }

  // ---------- stripe ----------
  async function openPricing() {
    modalOpen("pricingModal");
  }

  async function checkout(plan) {
    await ensureLoggedIn();
    try {
      const data = await api("/stripe/create-checkout-session", {
        method: "POST",
        body: JSON.stringify({ plan }),
      });
      if (data?.url) {
        window.location.href = data.url;
      } else {
        toast("Stripe error: no checkout URL returned.");
      }
    } catch (e) {
      toast(`Stripe error: ${e.message}`);
    }
  }

  // ---------- owner/admin ----------
  async function ownerUnlock() {
    await ensureLoggedIn();
    const code = (ownerCode?.value || "").trim();
    if (!code) {
      ownerHint.textContent = "Enter your owner code.";
      return;
    }
    ownerHint.textContent = "Checking…";
    try {
      const data = await api("/owner/unlock", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      ownerHint.textContent = data?.ok ? "Owner unlocked ✅" : "Owner unlock failed.";
      await loadSettings();
      toast("Admin unlocked ✅");
    } catch (e) {
      ownerHint.textContent = `Error: ${e.message}`;
    }
  }

  async function ownerLock() {
    await ensureLoggedIn();
    ownerHint.textContent = "Locking…";
    try {
      await api("/owner/lock", { method: "POST" });
      ownerHint.textContent = "Admin locked.";
      await loadSettings();
      toast("Admin locked ✅");
    } catch (e) {
      ownerHint.textContent = `Error: ${e.message}`;
    }
  }

  async function adminAnalytics() {
    await ensureLoggedIn();
    if (!state.ownerUnlocked) return toast("Owner mode required.");
    toast("Analytics tab coming from backend route (admin).");
    // If your backend has /admin/analytics you can wire it here
  }

  // ---------- generate ----------
  async function sendPrompt() {
    await ensureLoggedIn();
    const prompt = (promptInput?.value || "").trim();
    if (!prompt) return;

    appendMessage("user", prompt);
    promptInput.value = "";

    try {
      const data = await api("/generate", {
        method: "POST",
        body: JSON.stringify({ tab: state.tab, prompt }),
      });
      appendMessage("assistant", data?.response || "⚠ No response");
    } catch (e) {
      appendMessage("assistant", `⚠ Backend error: ${e.message}`);
    }
  }

  // ---------- tab switching ----------
  function normalizeTab(t) {
    const x = (t || "").toLowerCase().trim();
    if (TAB_IDS.includes(x)) return x;
    return "chat";
  }

  function setActiveTab(tab) {
    state.tab = normalizeTab(tab);

    // Highlight tab buttons if they exist
    $$(".tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === state.tab);
    });

    // Optional: show current tab somewhere
    const tabLabel = $("#tabLabel");
    if (tabLabel) tabLabel.textContent = state.tab;
  }

  // ---------- init wiring ----------
  function wireClosers() {
    $$("[data-close]").forEach((btn) => {
      btn.addEventListener("click", () => {
        modalClose(btn.dataset.close);
      });
    });
  }

  function wireSettings() {
    if (btnSettings) btnSettings.addEventListener("click", openSettings);
    if (btnLogout) btnLogout.addEventListener("click", async () => {
      try { await api("/auth/logout", { method: "POST" }); } catch {}
      await refreshMe();
      toast("Logged out.");
    });

    if (btnOpenPricing) btnOpenPricing.addEventListener("click", openPricing);
    if (btnOwnerLogin) btnOwnerLogin.addEventListener("click", ownerUnlock);
    if (btnOwnerLogout) btnOwnerLogout.addEventListener("click", ownerLock);
  }

  function wireAuth() {
    if (authModeLogin) authModeLogin.addEventListener("click", () => setAuthMode("login"));
    if (authModeRegister) authModeRegister.addEventListener("click", () => setAuthMode("register"));

    if (btnAuthSubmit) btnAuthSubmit.addEventListener("click", async () => {
      const email = (authEmail?.value || "").trim();
      const password = (authPassword?.value || "").trim();
      authHint.textContent = "";

      if (!email || !password) {
        authHint.textContent = "Enter email + password.";
        return;
      }

      try {
        if (authMode === "register") {
          await api("/auth/register", {
            method: "POST",
            body: JSON.stringify({ email, password }),
          });
        }
        await api("/auth/login", {
          method: "POST",
          body: JSON.stringify({ email, password }),
        });

        modalClose("authModal");
        await refreshMe();
        await loadSettings();
        toast("Logged in ✅");
      } catch (e) {
        authHint.textContent = e.message;
      }
    });
  }

  function wireTools() {
    $$(".btn.tool").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const tool = btn.dataset.tool;

        if (tool === "save_to_canon") return saveToCanon();
        if (tool === "view_canon") return viewCanon();
        if (tool === "pricing") return openPricing();

        if (tool === "admin_analytics") return adminAnalytics();
        if (tool === "admin_users") return toast("Users admin view: wire to backend admin route if enabled.");
        if (tool === "admin_subs") return toast("Subs admin view: wire to backend admin route if enabled.");

        toast(`Tool not wired: ${tool}`);
      });
    });

    $$("[data-price-plan]").forEach((b) => {
      b.addEventListener("click", () => checkout(b.dataset.pricePlan));
    });
  }

  function wireChat() {
    if (btnSend) btnSend.addEventListener("click", sendPrompt);
    if (promptInput) {
      promptInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          sendPrompt();
        }
      });
    }
  }

  function wireTabs() {
    // If you have tab buttons with class .tab and data-tab
    $$(".tab").forEach((b) => {
      b.addEventListener("click", () => setActiveTab(b.dataset.tab));
    });
  }

  function wireProjects() {
    if (btnNewProject) btnNewProject.addEventListener("click", async () => {
      await ensureLoggedIn();
      toast("New Project: wire to /api/projects/create if your UI flow is ready.");
    });

    if (btnSwitchProject) btnSwitchProject.addEventListener("click", async () => {
      await ensureLoggedIn();
      toast("Switch Project: wire to /api/projects/list + select UI when ready.");
    });
  }

  async function boot() {
    wireClosers();
    wireSettings();
    wireAuth();
    wireTools();
    wireChat();
    wireTabs();
    wireProjects();

    setAuthMode("login");
    setActiveTab("chat");

    await refreshMe();
    try { await loadSettings(); } catch {}
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
