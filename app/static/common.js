/* Shared helpers for the three pages. No framework, no build step. */
const $ = (sel, root = document) => root.querySelector(sel);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* storage can be blocked */ } },
  del(k) { try { sessionStorage.removeItem(k); } catch (e) { /* ignore */ } },
};

class AuthError extends Error {}

/* One place that talks to the server. role = which login token to send. */
async function call(role, path, method = "GET", body) {
  const token = role ? store.get("campus.token." + role) : null;
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    throw new Error("Cannot reach the server");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* not json */ }
  if (res.status === 401 && role) {
    store.del("campus.token." + role);
    throw new AuthError((data && data.error) || "Please log in");
  }
  if (!res.ok) {
    const msg = (data && data.error) || (data && data.detail && "That input is not valid") || "Error " + res.status;
    throw new Error(msg);
  }
  return data;
}

let toastTimer = null;
function toast(msg, bad = false) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}

const IST = "Asia/Kolkata";
function fmtTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: IST });
}
function fmtDay(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: IST });
}
const rupees = (n) => "₹" + Number(n || 0).toLocaleString("en-IN");
/* Same amount with a smaller rupee sign, for the big numerals. */
const rupeesHtml = (n) => '<span class="cur">₹</span>' + Number(n || 0).toLocaleString("en-IN");
function fmtHM(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: IST });
}
function dayLabel(ts) {
  const key = (t) => new Date(t * 1000).toLocaleDateString("en-CA", { timeZone: IST });
  const now = Date.now() / 1000;
  if (key(ts) === key(now)) return "Today";
  if (key(ts) === key(now - 86400)) return "Yesterday";
  return new Date(ts * 1000).toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: IST });
}
const ICONS = {
  payment: '<path d="M7 17 17 7M9 7h8v8"/>',
  topup: '<path d="M12 5v14M5 12h14"/>',
  attendance: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  meal: '<circle cx="12" cy="12" r="6.5"/><circle cx="12" cy="12" r="2.5"/>',
};
const icon = (type) => `<span class="ico" aria-hidden="true"><svg viewBox="0 0 24 24">${ICONS[type] || ICONS.attendance}</svg></span>`;

/* Run fn now and then every ms. Pauses while the tab is hidden; a login error stops it. */
function every(ms, fn) {
  let stopped = false;
  async function tick() {
    if (stopped) return;
    if (!document.hidden) {
      try { await fn(); } catch (e) { if (e instanceof AuthError) { stopped = true; return; } /* keep trying */ }
    }
    setTimeout(tick, ms);
  }
  tick();
  return () => { stopped = true; };
}

/* Login box. cfg: {role, title, identLabel, identValue, needsIdent, onDone} */
function mountLogin(cfg) {
  const box = $("#login");
  box.innerHTML = `
    <div class="login">
      <div class="mark" aria-hidden="true"></div>
      <h1>${esc(cfg.title)}</h1>
      <p class="muted small" style="margin:0 0 22px">${esc(cfg.hint || "")}</p>
      <form id="lf" autocomplete="off">
        ${cfg.needsIdent === false ? "" : `<label class="field">${esc(cfg.identLabel)}<input class="input" id="lf-id" autocapitalize="off" spellcheck="false" required></label>`}
        <label class="field">${cfg.needsIdent === false ? "Password" : "PIN"}<input class="input" id="lf-pin" type="password" inputmode="${cfg.needsIdent === false ? "text" : "numeric"}" required></label>
        <button class="btn primary block" type="submit">Sign in</button>
        <p id="lf-err" class="err" role="alert"></p>
      </form>
    </div>`;
  $("#lf").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("#lf button");
    btn.disabled = true;
    try {
      const out = await call(null, "/api/login", "POST", {
        role: cfg.role, ident: cfg.needsIdent === false ? "" : $("#lf-id").value, pin: $("#lf-pin").value,
      });
      store.set("campus.token." + cfg.role, out.token);
      store.set("campus.name." + cfg.role, out.name);
      cfg.onDone(out);
    } catch (e) {
      $("#lf-err").textContent = e.message.charAt(0).toUpperCase() + e.message.slice(1);
    } finally {
      btn.disabled = false;
    }
  });
  const first = $("#lf-id") || $("#lf-pin");
  if (first) first.focus();
}

function logout(role, show) {
  store.del("campus.token." + role);
  store.del("campus.name." + role);
  show();
}
