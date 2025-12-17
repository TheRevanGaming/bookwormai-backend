/* Book Worm AI Studio UI
   Backend:
   - /api/settings
   - /auth/register, /auth/login, /auth/logout, /auth/me
   - /generate
   - /canon/save, /canon/list
   - /owner/unlock, /owner/lock
   - /stripe/create-checkout-session
   - /admin/analytics, /admin/users, /admin/subscriptions
*/

const $ = (id) => document.getElementById(id);

let SETTINGS = null;
let ACTIVE_TAB = "chat";
let ACTIVE_PROJECT = "Default";

function show(id){ $(id).classList.remove("hidden"); }
function hide(id){ $(id).classList.add("hidden"); }

function toastHint(el, msg, good=false){
  el.textContent = msg;
  el.style.color = good ? "var(--good)" : "var(--bad)";
  setTimeout(()=>{ el.textContent=""; el.style.color=""; }, 3500);
}

async function api(path, opts={}){
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(opts.headers||{}) },
    ...opts
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch(e) { data = { raw: text }; }
  if (!res.ok){
    const detail = (data && data.detail) ? data.detail : (data && data.raw) ? data.raw : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

function tabLabel(t){
  const map = {
    chat: "Chat",
    writing: "Book Writing",
    gamedev: "Game Dev",
    musicdev: "Music Dev",
    imagelab: "Image Lab",
    voicelab: "Voice Lab",
    gamedesigner: "Game Designer",
  };
  return map[t] || t;
}

function renderTabs(){
  const wrap = $("tabsList");
  wrap.innerHTML = "";
  (SETTINGS?.tabs || []).forEach(t=>{
    const btn = document.createElement("div");
    btn.className = "tabChip" + (t === ACTIVE_TAB ? " active" : "");
    btn.textContent = tabLabel(t);
    btn.onclick = ()=>{
      ACTIVE_TAB = t;
      $("activeTabTitle").textContent = tabLabel(t);
      renderTabs();
      loadHistory();
    };
    wrap.appendChild(btn);
  });
}

function setProjectBadge(){
  $("projectBadge").textContent = `Project: ${ACTIVE_PROJECT}`;
}

function appendMsg(role, text){
  const log = $("chatLog");
  const d = document.createElement("div");
  d.className = "msg " + (role === "user" ? "user" : "assistant");
  d.innerHTML = `<div class="msgRole">${role}</div><div class="msgText"></div>`;
  d.querySelector(".msgText").textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

async function loadHistory(){
  // We don't currently have a dedicated history endpoint; history is used server-side.
  // Keep UI clean on tab switch:
  $("chatLog").innerHTML = "";
}

async function refreshMe(){
  const me = await api("/auth/me");
  if (me.logged_in){
    $("userBadge").textContent = me.email;
    $("userBadge").classList.remove("hidden");
    $("btnLogout").classList.remove("hidden");
  } else {
    $("userBadge").classList.add("hidden");
    $("btnLogout").classList.add("hidden");
  }
  return me;
}

async function refreshSettings(){
  SETTINGS = await api("/api/settings");
  renderTabs();
  $("activeTabTitle").textContent = tabLabel(ACTIVE_TAB);
  setProjectBadge();

  const me = SETTINGS.me || { logged_in:false };
  if (!me.logged_in){
    show("authModal");
  }
}

function openModal(id){ show(id); }
function closeModal(id){ hide(id); }

function wireClosers(){
  document.querySelectorAll("[data-close]").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      closeModal(btn.getAttribute("data-close"));
    });
  });
}

async function doRegister(email, password){
  await api("/auth/register", { method:"POST", body: JSON.stringify({email, password}) });
  await doLogin(email, password);
}

async function doLogin(email, password){
  await api("/auth/login", { method:"POST", body: JSON.stringify({email, password}) });
  closeModal("authModal");
  await refreshSettings();
  await refreshMe();
  appendMsg("assistant", "✅ Logged in. Ready when you are.");
}

async function doLogout(){
  await api("/auth/logout", { method:"POST" });
  await refreshMe();
  show("authModal");
}

async function sendPrompt(){
  const prompt = $("prompt").value.trim();
  if (!prompt) return;
  $("prompt").value = "";

  appendMsg("user", prompt);

  try{
    const out = await api("/generate", {
      method:"POST",
      body: JSON.stringify({ tab: ACTIVE_TAB, prompt, project: ACTIVE_PROJECT })
    });
    appendMsg("assistant", out.response || "⚠ No response");
  }catch(e){
    appendMsg("assistant", `⚠ Backend error: ${e.message}`);
  }
}

async function saveToCanon(){
  const last = Array.from(document.querySelectorAll(".msg.assistant .msgText")).slice(-1)[0];
  if (!last){
    alert("Nothing to save yet.");
    return;
  }
  const title = prompt("Canon title:", `${tabLabel(ACTIVE_TAB)} - ${new Date().toLocaleString()}`);
  if (!title) return;
  const content = last.textContent || "";
  await api("/canon/save", {
    method:"POST",
    body: JSON.stringify({ tab: ACTIVE_TAB, title, content, project: ACTIVE_PROJECT })
  });
  alert("✅ Saved to canon");
}

async function viewCanon(){
  openModal("canonModal");
  $("canonList").innerHTML = "Loading…";
  try{
    const out = await api(`/canon/list?tab=${encodeURIComponent(ACTIVE_TAB)}&project=${encodeURIComponent(ACTIVE_PROJECT)}`);
    const items = out.items || [];
    if (!items.length){
      $("canonList").innerHTML = "<div class='hint'>No canon saved yet.</div>";
      return;
    }
    $("canonList").innerHTML = items.map(it => `
      <div class="kv">
        <div class="k">${it.created_at} • ${it.tab}</div>
        <div class="v">${escapeHtml(it.title)}</div>
        <div class="hint" style="margin-top:8px; white-space:pre-wrap">${escapeHtml(it.content)}</div>
      </div>
    `).join("");
  }catch(e){
    $("canonList").innerHTML = `<div class="hint" style="color:var(--bad)">⚠ ${escapeHtml(e.message)}</div>`;
  }
}

function escapeHtml(s){
  return (s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

async function openPricing(){
  openModal("pricingModal");
}

async function checkout(plan){
  try{
    const out = await api("/stripe/create-checkout-session", {
      method:"POST",
      body: JSON.stringify({ plan })
    });
    if (out.url) window.open(out.url, "_blank");
  }catch(e){
    alert(`Stripe error: ${e.message}`);
  }
}

async function openSettings(){
  openModal("settingsModal");
  const me = await api("/auth/me");
  $("settingsEmail").textContent = me.logged_in ? me.email : "—";
  $("settingsOwner").textContent = me.is_owner ? "Yes" : "No";
  $("settingsPlan").textContent = "—"; // can be expanded by reading subscriptions
  $("adminCard").classList.toggle("hidden", !me.is_owner);
}

async function ownerUnlock(){
  const code = $("ownerCode").value;
  const hint = $("ownerHint");
  try{
    await api("/owner/unlock", { method:"POST", body: JSON.stringify({ code })});
    toastHint(hint, "✅ Admin unlocked", true);
    $("ownerCode").value = "";
    const me = await refreshMe();
    $("adminCard").classList.toggle("hidden", !me.is_owner);
    $("settingsOwner").textContent = "Yes";
  }catch(e){
    toastHint(hint, `⚠ ${e.message}`, false);
  }
}

async function ownerLock(){
  const hint = $("ownerHint");
  try{
    await api("/owner/lock", { method:"POST" });
    toastHint(hint, "✅ Admin locked", true);
    const me = await refreshMe();
    $("adminCard").classList.toggle("hidden", !me.is_owner);
    $("settingsOwner").textContent = "No";
  }catch(e){
    toastHint(hint, `⚠ ${e.message}`, false);
  }
}

async function adminFetch(tool){
  openModal("canonModal");
  $("canonList").innerHTML = "Loading…";
  try{
    if (tool === "admin_analytics"){
      const out = await api("/admin/analytics");
      $("canonList").innerHTML = `<pre class="hint">${escapeHtml(JSON.stringify(out, null, 2))}</pre>`;
    }
    if (tool === "admin_users"){
      const out = await api("/admin/users");
      $("canonList").innerHTML = `<pre class="hint">${escapeHtml(JSON.stringify(out, null, 2))}</pre>`;
    }
    if (tool === "admin_subs"){
      const out = await api("/admin/subscriptions");
      $("canonList").innerHTML = `<pre class="hint">${escapeHtml(JSON.stringify(out, null, 2))}</pre>`;
    }
  }catch(e){
    $("canonList").innerHTML = `<div class="hint" style="color:var(--bad)">⚠ ${escapeHtml(e.message)}</div>`;
  }
}

function wireUI(){
  wireClosers();

  $("btnSend").addEventListener("click", sendPrompt);
  $("prompt").addEventListener("keydown", (e)=>{
    if (e.key === "Enter" && !e.shiftKey){
      e.preventDefault();
      sendPrompt();
    }
  });

  $("btnSettings").addEventListener("click", openSettings);
  $("btnLogout").addEventListener("click", doLogout);

  // Auth modal mode switch
  let mode = "login";
  $("authModeLogin").onclick = ()=>{ mode="login"; $("authModeLogin").classList.add("active"); $("authModeRegister").classList.remove("active"); };
  $("authModeRegister").onclick = ()=>{ mode="register"; $("authModeRegister").classList.add("active"); $("authModeLogin").classList.remove("active"); };

  $("btnAuthSubmit").onclick = async ()=>{
    const email = $("authEmail").value.trim();
    const password = $("authPassword").value;
    const hint = $("authHint");
    try{
      if (mode === "register") await doRegister(email, password);
      else await doLogin(email, password);
    }catch(e){
      toastHint(hint, `⚠ ${e.message}`, false);
    }
  };

  // Tools
  document.querySelectorAll("[data-tool]").forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      const tool = btn.getAttribute("data-tool");
      if (tool === "save_to_canon") return saveToCanon();
      if (tool === "view_canon") return viewCanon();
      if (tool === "pricing") return openPricing();
      if (tool.startsWith("admin_")) return adminFetch(tool);
    });
  });

  $("btnOpenPricing").addEventListener("click", openPricing);
  $("btnOwnerLogin").addEventListener("click", ownerUnlock);
  $("btnOwnerLogout").addEventListener("click", ownerLock);

  document.querySelectorAll("[data-price-plan]").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      checkout(btn.getAttribute("data-price-plan"));
    });
  });

  // Projects (simple)
  $("btnNewProject").addEventListener("click", ()=>{
    const name = prompt("New project name:", "");
    if (!name) return;
    ACTIVE_PROJECT = name.trim();
    setProjectBadge();
    loadHistory();
  });
  $("btnSwitchProject").addEventListener("click", ()=>{
    const name = prompt("Switch to project name:", ACTIVE_PROJECT);
    if (!name) return;
    ACTIVE_PROJECT = name.trim();
    setProjectBadge();
    loadHistory();
  });
}

window.addEventListener("DOMContentLoaded", async ()=>{
  wireUI();
  try{
    await refreshSettings();
    await refreshMe();
    loadHistory();
  }catch(e){
    appendMsg("assistant", `⚠ Startup error: ${e.message}`);
  }
});
