// ----------------------------
// BASIC STATE
// ----------------------------

let authToken = null;
let currentUser = null; // { email, is_owner, plan }
let activePanel = "chat";
let activeDomain = "CHAT";
let depthMode = "deep";
let lastAssistantAnswer = "";
let lastProjectId = null;

// Map panel -> domain label for the prompt
const PANEL_DOMAIN_MAP = {
  chat: "CHAT",
  storytelling: "STORYTELLING",
  gamedev: "GAME_DEV",
  music: "MUSIC_DEV",
  book: "BOOK_WRITING",
  language: "LANGUAGE_LAB",
  image: "IMAGE_LAB",
  voice: "VOICE_LAB",
  coding: "CODING",
  subscription: "CHAT",
  admin: "ADMIN",
};

// ----------------------------
// DOM HELPERS
// ----------------------------

function $(id) {
  return document.getElementById(id);
}

function appendMessage(role, text) {
  const log = $("chat-log");
  if (!log) return;

  const div = document.createElement("div");
  div.classList.add("message");
  if (role === "user") div.classList.add("user");
  else if (role === "assistant") div.classList.add("assistant");
  else div.classList.add("system");

  const safeText = String(text || "").replace(/\n/g, "<br/>");
  div.innerHTML = safeText;

  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function setPlanPill(plan) {
  const pill = $("plan-pill");
  if (!pill) return;

  pill.className = "plan-pill";
  let cls = "plan-free";
  let label = "Free";

  if (plan === "basic") {
    cls = "plan-basic";
    label = "Basic";
  } else if (plan === "pro") {
    cls = "plan-pro";
    label = "Pro";
  } else if (plan === "patron") {
    cls = "plan-patron";
    label = "Patron";
  } else if (plan === "owner") {
    cls = "plan-owner";
    label = "Owner";
  }

  pill.classList.add(cls);
  pill.textContent = `Plan: ${label}`;

  const subPlanLabel = $("subscription-plan-label");
  if (subPlanLabel) subPlanLabel.textContent = label;
}

// ----------------------------
// AUTH STORAGE
// ----------------------------

function loadAuthFromStorage() {
  const token = window.localStorage.getItem("bookworm_token");
  const email = window.localStorage.getItem("bookworm_email");
  const isOwner = window.localStorage.getItem("bookworm_is_owner") === "true";
  const plan = window.localStorage.getItem("bookworm_plan") || "free";

  if (token && email) {
    authToken = token;
    currentUser = { email, is_owner: isOwner, plan };
  }
}

function saveAuthToStorage(token, email, isOwner, plan) {
  if (!token || !email) {
    console.warn("saveAuthToStorage called with missing token/email", {
      token,
      email,
      isOwner,
      plan,
    });
  }

  authToken = token || null;
  currentUser = email
    ? { email, is_owner: !!isOwner, plan: plan || "free" }
    : null;

  if (authToken && currentUser) {
    window.localStorage.setItem("bookworm_token", authToken);
    window.localStorage.setItem("bookworm_email", currentUser.email);
    window.localStorage.setItem(
      "bookworm_is_owner",
      currentUser.is_owner ? "true" : "false"
    );
    window.localStorage.setItem("bookworm_plan", currentUser.plan);
  } else {
    clearAuth();
  }
}

function clearAuth() {
  authToken = null;
  currentUser = null;
  window.localStorage.removeItem("bookworm_token");
  window.localStorage.removeItem("bookworm_email");
  window.localStorage.removeItem("bookworm_is_owner");
  window.localStorage.removeItem("bookworm_plan");
}

// ----------------------------
// UI UPDATE
// ----------------------------

function updateAuthUI() {
  const statusLine = $("account-status-line");
  const topbarLabel = $("topbar-user-label");
  const btnLogout = $("btn-logout");
  const btnLogin = $("btn-login");
  const btnRegister = $("btn-register");
  const emailInput = $("auth-email");
  const passwordInput = $("auth-password");
  const adminTab = $("tab-admin");
  const planDetail = $("subscription-plan-detail");

  if (!currentUser) {
    if (statusLine) statusLine.textContent = "Not signed in";
    if (topbarLabel) topbarLabel.textContent = "Guest session";
    if (btnLogout) btnLogout.style.display = "none";
    if (btnLogin) btnLogin.style.display = "inline-block";
    if (btnRegister) btnRegister.style.display = "inline-block";
    if (adminTab) adminTab.style.display = "none";

    setPlanPill("free");
    if (planDetail) {
      planDetail.textContent =
        "You are in Free mode. Login and upgrade to unlock more power.";
    }
    return;
  }

  const plan = currentUser.plan || "free";

  if (statusLine)
    statusLine.textContent = `Signed in as ${currentUser.email}`;
  if (topbarLabel)
    topbarLabel.textContent = `${currentUser.email} · ${plan.toUpperCase()}`;
  if (btnLogout) btnLogout.style.display = "inline-block";
  if (btnLogin) btnLogin.style.display = "none";
  if (btnRegister) btnRegister.style.display = "none";

  if (emailInput) emailInput.value = "";
  if (passwordInput) passwordInput.value = "";

  if (currentUser.is_owner && adminTab) {
    adminTab.style.display = "block";
  } else if (adminTab) {
    adminTab.style.display = "none";
  }

  setPlanPill(plan);

  if (planDetail) {
    if (plan === "free") {
      planDetail.textContent =
        "Free mode: great for testing the studio. Some limits apply.";
    } else if (plan === "basic") {
      planDetail.textContent =
        "Basic plan: expanded usage for focused writers and devs.";
    } else if (plan === "pro") {
      planDetail.textContent =
        "Pro plan: serious capacity for multi-world, multi-project workflows.";
    } else if (plan === "patron") {
      planDetail.textContent =
        "Patron plan: premium access and direct support for ongoing development.";
    } else if (plan === "owner") {
      planDetail.textContent =
        "Owner mode: this account bypasses subscription limits and has access to admin tools.";
    }
  }
}

// read /me and sync plan/is_owner from backend
async function fetchMe() {
  try {
    const headers = {};
    if (authToken) headers["Authorization"] = `Bearer ${authToken}`;

    const res = await fetch("/me", { headers });
    if (!res.ok) {
      console.warn("fetchMe non-OK", res.status);
      return;
    }

    const data = await res.json();
    console.log("/me response", data);

    // our backend returns anonymous if no auth
    if (!authToken || data.email === "anonymous@example.com") {
      currentUser = null;
      clearAuth();
      updateAuthUI();
      return;
    }

    const email = data.email || currentUser?.email || "unknown@example.com";
    const plan =
      data.plan ||
      data.subscription_plan ||
      currentUser?.plan ||
      "free";
    const isOwner = !!(data.is_owner ?? data.owner ?? false);

    saveAuthToStorage(authToken, email, isOwner, plan);
    updateAuthUI();
  } catch (err) {
    console.error("Error calling /me", err);
  }
}

// ----------------------------
// PANEL / TAB SWITCHING
// ----------------------------

function setActivePanel(panelName) {
  activePanel = panelName;

  const tabs = document.querySelectorAll(".nav-tab");
  tabs.forEach((btn) => {
    if (btn.dataset.panel === panelName) btn.classList.add("active");
    else btn.classList.remove("active");
  });

  const panels = ["studio-panel", "subscription-panel", "admin-panel"];
  panels.forEach((id) => {
    const el = $(id);
    if (!el) return;

    if (id === "studio-panel" && panelName !== "subscription" && panelName !== "admin") {
      el.classList.add("active-panel");
    } else if (id === "subscription-panel" && panelName === "subscription") {
      el.classList.add("active-panel");
    } else if (id === "admin-panel" && panelName === "admin") {
      el.classList.add("active-panel");
    } else {
      el.classList.remove("active-panel");
    }
  });

  if (panelName !== "subscription" && panelName !== "admin") {
    const domain = PANEL_DOMAIN_MAP[panelName] || "CHAT";
    activeDomain = domain;
    const label = $("current-domain-label");
    if (label) label.textContent = domain;
  }
}

// ----------------------------
// AUTH HANDLERS
// ----------------------------

// helper to normalize login/register responses
function normalizeAuthResponse(data, fallbackEmail) {
  console.log("normalizeAuthResponse input", data);
  if (!data || typeof data !== "object") return null;

  const token =
    data.token ||
    data.access_token ||
    data.jwt ||
    data.session_token ||
    null;

  const email =
    data.email ||
    (data.user && data.user.email) ||
    fallbackEmail ||
    null;

  const plan =
    data.plan ||
    data.subscription_plan ||
    (data.user && data.user.plan) ||
    "free";

  const isOwner =
    !!(data.is_owner ?? data.owner ?? (data.user && data.user.is_owner) ?? false);

  return { token, email, plan, isOwner };
}

async function handleRegister() {
  const email = $("auth-email").value.trim();
  const password = $("auth-password").value.trim();
  if (!email || !password) {
    alert("Please fill email and password.");
    return;
  }

  try {
    const res = await fetch("/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    const raw = await res.json().catch(() => ({}));
    console.log("/register raw response:", raw);

    if (!res.ok) {
      alert("Register failed: " + (raw.detail || res.statusText));
      return;
    }

    const norm = normalizeAuthResponse(raw, email);

    if (!norm || !norm.token || !norm.email) {
      appendMessage(
        "assistant",
        "⚠ Register succeeded on server, but no usable auth token/email was returned. You may need to click **Login** next."
      );
      return;
    }

    saveAuthToStorage(norm.token, norm.email, norm.isOwner, norm.plan);
    appendMessage(
      "system",
      `Registered & logged in as <b>${norm.email}</b>. Plan: <b>${norm.plan.toUpperCase()}</b>.`
    );
    updateAuthUI();
  } catch (err) {
    console.error("register error", err);
    alert("Error registering.");
  }
}

async function handleLogin() {
  const email = $("auth-email").value.trim();
  const password = $("auth-password").value.trim();
  if (!email || !password) {
    alert("Please fill email and password.");
    return;
  }

  try {
    const res = await fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    const raw = await res.json().catch(() => ({}));
    console.log("/login raw response:", raw);

    if (!res.ok) {
      alert("Login failed: " + (raw.detail || res.statusText));
      return;
    }

    const norm = normalizeAuthResponse(raw, email);

    if (!norm || !norm.token || !norm.email) {
      appendMessage(
        "assistant",
        "⚠ Login succeeded on server, but no usable auth token/email was returned. Check backend /login response shape."
      );
      return;
    }

    saveAuthToStorage(norm.token, norm.email, norm.isOwner, norm.plan);
    appendMessage(
      "system",
      `Logged in as <b>${norm.email}</b>. Plan: <b>${norm.plan.toUpperCase()}</b>.`
    );
    updateAuthUI();
    fetchMe(); // sync with backend /me
  } catch (err) {
    console.error("login error", err);
    alert("Error logging in.");
  }
}

function handleLogout() {
  clearAuth();
  updateAuthUI();
  appendMessage("system", "You have been logged out locally.");
}

// ----------------------------
// GENERATE HANDLER
// ----------------------------

async function handleChatSubmit(e) {
  e.preventDefault();
  const inputEl = $("prompt-input");
  if (!inputEl) return;
  const rawPrompt = inputEl.value.trim();
  if (!rawPrompt) return;

  appendMessage("user", rawPrompt);
  inputEl.value = "";

  const depthSelect = $("depth-select");
  depthMode = depthSelect ? depthSelect.value : "deep";

  const projectIdInput = $("project-id-input");
  let projectId = null;
  if (projectIdInput && projectIdInput.value.trim() !== "") {
    projectId = parseInt(projectIdInput.value.trim(), 10);
    if (Number.isNaN(projectId)) projectId = null;
  }
  lastProjectId = projectId;

  const promptWithDomain = `[DOMAIN: ${activeDomain}]\n\n${rawPrompt}`;

  const body = {
    prompt: promptWithDomain,
    mode: "auto",
    depth: depthMode,
    project_id: projectId,
  };

  const headers = { "Content-Type": "application/json" };
  if (authToken) headers["Authorization"] = `Bearer ${authToken}`;

  appendMessage("assistant", "Thinking...");

  try {
    const res = await fetch("/generate", {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      appendMessage(
        "assistant",
        `⚠ Backend error: ${JSON.stringify(err)}`
      );
      return;
    }

    const data = await res.json();
    lastAssistantAnswer = data.response || "";
    appendMessage("assistant", lastAssistantAnswer);
  } catch (err) {
    console.error("generate error", err);
    appendMessage("assistant", "⚠ Network error talking to Book Worm backend.");
  }
}

// ----------------------------
// SAVE CANON
// ----------------------------

async function handleSaveCanon() {
  if (!lastAssistantAnswer) {
    alert("No AI answer to save yet.");
    return;
  }
  const projectIdInput = $("project-id-input");
  let projectId = null;
  if (projectIdInput && projectIdInput.value.trim() !== "") {
    projectId = parseInt(projectIdInput.value.trim(), 10);
  }
  if (!projectId || Number.isNaN(projectId)) {
    alert("Please set a valid Project ID before saving canon.");
    return;
  }

  if (!authToken) {
    alert("You must be logged in to save canon.");
    return;
  }

  const title = prompt(
    "Title for this canon doc:",
    `Canon note (${new Date().toLocaleString()})`
  );
  if (!title) return;

  const tags = [activeDomain.toLowerCase(), "canon"];

  try {
    const res = await fetch("/docs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${authToken}`,
      },
      body: JSON.stringify({
        project_id: projectId,
        title,
        body: lastAssistantAnswer,
        tags,
        canon_state: "LOCKED_CANON",
        source: "studio-save",
      }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      appendMessage(
        "assistant",
        `⚠ Error saving canon: ${JSON.stringify(err)}`
      );
      return;
    }

    appendMessage(
      "assistant",
      `📚 Saved this answer as <b>LOCKED CANON</b> in project ${projectId}.`
    );
  } catch (err) {
    console.error("save canon error", err);
    appendMessage("assistant", "⚠ Network error while saving canon.");
  }
}

// ----------------------------
// STRIPE CHECKOUT
// ----------------------------

async function startCheckout(plan) {
  if (!authToken) {
    alert("You need to be logged in to start a subscription.");
    return;
  }

  if (currentUser && currentUser.is_owner) {
    alert("Owner account already bypasses subscription limits.");
    return;
  }

  const origin = window.location.origin;
  const successUrl = `${origin}?checkout=success`;
  const cancelUrl = `${origin}?checkout=cancel`;

  try {
    const res = await fetch("/stripe/create-checkout-session", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${authToken}`,
      },
      body: JSON.stringify({
        plan,
        success_url: successUrl,
        cancel_url: cancelUrl,
      }),
    });

    const raw = await res.json().catch(() => ({}));
    console.log("/stripe/create-checkout-session response:", raw);

    if (!res.ok) {
      alert("Stripe error: " + (raw.detail || res.statusText));
      return;
    }

    if (!raw.checkout_url) {
      alert("Stripe did not return a checkout URL.");
      return;
    }

    window.location.href = raw.checkout_url;
  } catch (err) {
    console.error("stripe checkout error", err);
    alert("Network error during Stripe checkout.");
  }
}

// ----------------------------
// ADMIN
// ----------------------------

async function refreshSubscribers() {
  if (!authToken || !currentUser || !currentUser.is_owner) {
    alert("Owner access only.");
    return;
  }

  try {
    const res = await fetch("/admin/subscribers", {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    const raw = await res.json().catch(() => ({}));
    console.log("/admin/subscribers response:", raw);

    if (!res.ok) {
      alert("Error fetching subscribers: " + (raw.detail || res.statusText));
      return;
    }

    const container = $("admin-subscribers-table");
    if (!container) return;

    if (!raw.data || raw.data.length === 0) {
      container.innerHTML = "<p>No subscribers yet.</p>";
      return;
    }

    let html =
      '<table><thead><tr><th>User ID</th><th>Email</th><th>Plan</th><th>Status</th><th>Created</th></tr></thead><tbody>';
    for (const row of raw.data) {
      html += `<tr>
        <td>${row.user_id}</td>
        <td>${row.email}</td>
        <td>${row.plan}</td>
        <td>${row.status}</td>
        <td>${row.created_at}</td>
      </tr>`;
    }
    html += "</tbody></table>";
    container.innerHTML = html;
  } catch (err) {
    console.error("admin subscribers error", err);
    alert("Network error loading subscribers.");
  }
}

async function refreshUsage() {
  if (!authToken || !currentUser || !currentUser.is_owner) {
    alert("Owner access only.");
    return;
  }

  try {
    const res = await fetch("/admin/usage", {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    const raw = await res.json().catch(() => ({}));
    console.log("/admin/usage response:", raw);

    if (!res.ok) {
      alert("Error fetching usage: " + (raw.detail || res.statusText));
      return;
    }

    const container = $("admin-usage-summary");
    if (!container) return;

    let html = `<p>Total generations: <b>${raw.total_generations}</b></p>`;
    if (raw.events && raw.events.length > 0) {
      html += "<ul>";
      for (const ev of raw.events) {
        html += `<li>${ev.event_type}: ${ev.count}</li>`;
      }
      html += "</ul>";
    } else {
      html += "<p>No usage events recorded yet.</p>";
    }
    container.innerHTML = html;
  } catch (err) {
    console.error("admin usage error", err);
    alert("Network error loading usage.");
  }
}

// ----------------------------
// AUX BUTTONS
// ----------------------------

function handleAuxButtons() {
  const docsBtn = $("btn-open-docs");
  if (docsBtn) {
    docsBtn.addEventListener("click", () => {
      window.open("/docs", "_blank");
    });
  }

  const marketingBtn = $("btn-open-marketing");
  if (marketingBtn) {
    marketingBtn.addEventListener("click", () => {
      window.open(
        "https://therevangaming.github.io/bookwormai-site/",
        "_blank"
      );
    });
  }
}

// ----------------------------
// INIT
// ----------------------------

function init() {
  const btnLogin = $("btn-login");
  const btnRegister = $("btn-register");
  const btnLogout = $("btn-logout");

  if (btnLogin) btnLogin.addEventListener("click", handleLogin);
  if (btnRegister) btnRegister.addEventListener("click", handleRegister);
  if (btnLogout) btnLogout.addEventListener("click", handleLogout);

  const chatForm = $("chat-form");
  if (chatForm) chatForm.addEventListener("submit", handleChatSubmit);

  const depthSelect = $("depth-select");
  if (depthSelect) {
    depthSelect.addEventListener("change", () => {
      depthMode = depthSelect.value;
    });
  }

  const saveCanonBtn = $("btn-save-canon");
  if (saveCanonBtn) saveCanonBtn.addEventListener("click", handleSaveCanon);

  const navTabs = document.querySelectorAll(".nav-tab");
  navTabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      const panel = btn.dataset.panel;
      if (!panel) return;
      setActivePanel(panel);

      if (panel !== "subscription" && panel !== "admin") {
        appendMessage(
          "system",
          `Switched to <b>${PANEL_DOMAIN_MAP[panel] || "CHAT"}</b> domain.`
        );
      }
    });
  });

  const basicBtn = $("btn-basic-plan");
  const proBtn = $("btn-pro-plan");
  const patronBtn = $("btn-patron-plan");
  if (basicBtn) basicBtn.addEventListener("click", () => startCheckout("basic"));
  if (proBtn) proBtn.addEventListener("click", () => startCheckout("pro"));
  if (patronBtn) patronBtn.addEventListener("click", () => startCheckout("patron"));

  const btnSubs = $("btn-refresh-subscribers");
  const btnUsage = $("btn-refresh-usage");
  if (btnSubs) btnSubs.addEventListener("click", refreshSubscribers);
  if (btnUsage) btnUsage.addEventListener("click", refreshUsage);

  handleAuxButtons();

  loadAuthFromStorage();
  updateAuthUI();
  fetchMe();

  setActivePanel("chat");

  appendMessage(
    "assistant",
    "Welcome to <b>Book Worm AI Studio</b>. Sign in (or create an account), pick a tab, and I’ll respond in that domain’s mindset (Storytelling, Game Dev, Music, etc.)."
  );
}

document.addEventListener("DOMContentLoaded", init);
