// ------------- GLOBAL STATE -------------

let activeTab = "chat";
let activeProjectId = null;
let ownerCode = localStorage.getItem("ownerCode") || "";
let eventsByTypeChart = null;
let eventsTimelineChart = null;

// ------------- HELPER: TAB SWITCHING -------------

document.addEventListener("DOMContentLoaded", () => {
    const tabButtons = document.querySelectorAll("#tab-bar button");
    const tabs = document.querySelectorAll(".tab");

    tabButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            const target = btn.dataset.tab;
            activeTab = target;

            tabButtons.forEach(b => b.classList.remove("active"));
            tabs.forEach(t => t.classList.remove("active"));

            btn.classList.add("active");
            document.getElementById(target).classList.add("active");
        });
    });

    // If ownerCode already saved, show status
    updateOwnerBadge();
});

// ------------- OWNER / ADMIN -------------

function setOwnerCode() {
    const input = document.getElementById("owner-code-input");
    const code = input.value.trim();
    if (!code) return;
    ownerCode = code;
    localStorage.setItem("ownerCode", ownerCode);
    updateOwnerBadge();
    const out = document.getElementById("owner-output");
    out.textContent = "Owner code stored locally. Admin + Analytics unlocked.";
}

function updateOwnerBadge() {
    const badge = document.getElementById("owner-badge");
    if (!badge) return;
    if (ownerCode) {
        badge.textContent = "Owner Mode";
        badge.style.background = "linear-gradient(135deg,#4ade80,#22c55e)";
    } else {
        badge.textContent = "Guest Mode";
        badge.style.background = "linear-gradient(135deg,#f97316,#ea580c)";
    }
}

// ------------- DOMAIN HELPER -------------

function makeDomainPrompt(domain, userText) {
    return `[DOMAIN: ${domain}]\n\n${userText}`;
}

// ------------- GENERATION HELPERS -------------

async function callGenerate(prompt, depth = "deep") {
    const body = {
        prompt: prompt,
        mode: "auto",
        depth: depth,
        project_id: activeProjectId
    };

    const headers = {
        "Content-Type": "application/json"
    };
    if (ownerCode) {
        headers["X-Bookworm-Owner-Code"] = ownerCode;
    }

    const res = await fetch("/generate", {
        method: "POST",
        headers,
        body: JSON.stringify(body)
    });

    if (!res.ok) {
        const txt = await res.text();
        throw new Error(`HTTP ${res.status}: ${txt}`);
    }

    const data = await res.json();
    return data.response || "";
}

// ------------- CHAT / BOOK / GAME / LANGUAGE / MUSIC -------------

async function sendChat() {
    const input = document.getElementById("chat-input");
    const output = document.getElementById("chat-output");
    const depth = document.getElementById("chat-depth").value;
    const text = input.value.trim();
    if (!text) return;

    output.textContent = "Thinking...";
    try {
        const prompt = makeDomainPrompt("STORYTELLING", text);
        const ans = await callGenerate(prompt, depth);
        output.textContent = ans;
    } catch (err) {
        output.textContent = `⚠ Error: ${err.message}`;
    }
}

async function sendBook() {
    const input = document.getElementById("book-input");
    const output = document.getElementById("book-output");
    const depth = document.getElementById("book-depth").value;
    const text = input.value.trim();
    if (!text) return;

    output.textContent = "Thinking...";
    try {
        const prompt = makeDomainPrompt("STORYTELLING", text);
        const ans = await callGenerate(prompt, depth);
        output.textContent = ans;
    } catch (err) {
        output.textContent = `⚠ Error: ${err.message}`;
    }
}

async function sendGame() {
    const input = document.getElementById("game-input");
    const output = document.getElementById("game-output");
    const depth = document.getElementById("game-depth").value;
    const text = input.value.trim();
    if (!text) return;

    output.textContent = "Designing...";
    try {
        const prompt = makeDomainPrompt("GAME_DEV", text);
        const ans = await callGenerate(prompt, depth);
        output.textContent = ans;
    } catch (err) {
        output.textContent = `⚠ Error: ${err.message}`;
    }
}

async function sendLanguage() {
    const input = document.getElementById("language-input");
    const output = document.getElementById("language-output");
    const depth = document.getElementById("language-depth").value;
    const text = input.value.trim();
    if (!text) return;

    output.textContent = "Constructing language...";
    try {
        const prompt = makeDomainPrompt("LANGUAGE_LAB", text);
        const ans = await callGenerate(prompt, depth);
        output.textContent = ans;
    } catch (err) {
        output.textContent = `⚠ Error: ${err.message}`;
    }
}

async function sendMusic() {
    const input = document.getElementById("music-input");
    const output = document.getElementById("music-output");
    const depth = document.getElementById("music-depth").value;
    const text = input.value.trim();
    if (!text) return;

    output.textContent = "Composing...";
    try {
        const prompt = makeDomainPrompt("MUSIC_DEV", text);
        const ans = await callGenerate(prompt, depth);
        output.textContent = ans;
    } catch (err) {
        output.textContent = `⚠ Error: ${err.message}`;
    }
}

// ------------- PROJECTS -------------

async function loadProjects() {
    const listEl = document.getElementById("project-list");
    listEl.textContent = "Loading projects...";

    try {
        const res = await fetch("/projects");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!Array.isArray(data) || data.length === 0) {
            listEl.textContent = "No projects yet.";
            return;
        }

        let html = "<ul>";
        data.forEach(p => {
            html += `<li>
                <button onclick="selectProject(${p.id})">${p.name}</button>
                <span class="project-desc">${p.description || ""}</span>
            </li>`;
        });
        html += "</ul>";
        listEl.innerHTML = html;
    } catch (err) {
        listEl.textContent = `⚠ Failed to load projects: ${err.message}`;
    }
}

function selectProject(id) {
    activeProjectId = id;
    const out = document.getElementById("project-list");
    out.insertAdjacentHTML("beforeend", `<p>Active project set to #${id}</p>`);
}

async function createProject() {
    const nameInput = document.getElementById("project-name");
    const descInput = document.getElementById("project-desc");
    const name = nameInput.value.trim();
    const description = descInput.value.trim();

    if (!name) return;

    try {
        const res = await fetch("/projects", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name, description})
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        activeProjectId = data.id;
        nameInput.value = "";
        descInput.value = "";
        await loadProjects();
    } catch (err) {
        const listEl = document.getElementById("project-list");
        listEl.textContent = `⚠ Failed to create project: ${err.message}`;
    }
}

// Load projects on visiting projects tab (lazy)
document.addEventListener("click", (e) => {
    const btn = e.target;
    if (btn.matches("#tab-bar button[data-tab='projects']")) {
        loadProjects();
    }
});

// ------------- ANALYTICS -------------

async function loadAnalyticsSummary() {
    const box = document.getElementById("analytics-summary");
    box.textContent = "Loading summary...";

    const headers = {};
    if (ownerCode) headers["X-Bookworm-Owner-Code"] = ownerCode;

    try {
        const res = await fetch("/admin/stats/summary", { headers });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        box.innerHTML = `
            <p><b>Total Events:</b> ${data.total_events}</p>
            <p><b>Last 24h Events:</b> ${data.last_24h_events}</p>
            <p><b>Total Generations:</b> ${data.total_generates}</p>
            <p><b>Total Image Gens:</b> ${data.total_image_generates}</p>
        `;

        // Chart 1: Events by type
        const types = Object.keys(data.by_type || {});
        const counts = types.map(k => data.by_type[k]);

        const ctx1 = document.getElementById("eventsByTypeChart").getContext("2d");

        if (eventsByTypeChart) {
            eventsByTypeChart.destroy();
        }

        eventsByTypeChart = new Chart(ctx1, {
            type: "bar",
            data: {
                labels: types,
                datasets: [{
                    label: "Events by Type",
                    data: counts
                }]
            },
            options: {
                responsive: true,
                scales: {
                    y: {
                        beginAtZero: true
                    }
                }
            }
        });

        // Chart 2: Basic timeline from last_24h_events (we just treat as a single point for now)
        const ctx2 = document.getElementById("eventsTimelineChart").getContext("2d");
        if (eventsTimelineChart) {
            eventsTimelineChart.destroy();
        }
        eventsTimelineChart = new Chart(ctx2, {
            type: "line",
            data: {
                labels: ["Last 24h"],
                datasets: [{
                    label: "Events",
                    data: [data.last_24h_events]
                }]
            },
            options: {
                responsive: true,
                scales: {
                    y: {beginAtZero: true}
                }
            }
        });

    } catch (err) {
        box.innerHTML = `<p style="color:#f97373;">⚠ Failed to load analytics: ${err.message}</p>`;
    }
}

async function loadRecentEvents() {
    const box = document.getElementById("analytics-events");
    box.textContent = "Loading events...";

    const headers = {};
    if (ownerCode) headers["X-Bookworm-Owner-Code"] = ownerCode;

    try {
        const res = await fetch("/admin/events/recent?limit=50", { headers });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        if (!Array.isArray(data) || data.length === 0) {
            box.textContent = "No events logged yet.";
            return;
        }

        let html = "";
        data.forEach(ev => {
            html += `
                <div class="analytics-event">
                    <p><b>[${ev.event_type}]</b> at ${ev.created_at}</p>
                    <pre>${JSON.stringify(ev.metadata, null, 2)}</pre>
                    <hr>
                </div>
            `;
        });
        box.innerHTML = html;
    } catch (err) {
        box.innerHTML = `<p style="color:#f97373;">⚠ Failed to load events: ${err.message}</p>`;
    }
}
