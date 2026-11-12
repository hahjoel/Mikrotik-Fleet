const $ = s => document.querySelector(s);
let routers = [], status = {}, selected = new Set(), editing = null, timer = null;

const ACTIONS = [
  ["interfaces","Interfaces"],["addresses","IP addresses"],["routes","Routes"],
  ["dhcp-leases","DHCP leases"],["log","Log"],["export","Export (SSH)"],
  ["backup","Save backup"],["check-updates","Check updates"],["reboot","Reboot"],
];

async function api(path, opts={}) {
  const r = await fetch(path, {headers:{"Content-Type":"application/json"}, ...opts});
  if (!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || r.statusText);
  return r.json();
}
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function bar(v){ if(v==null||v==="") return "—"; v=+v;
  return `<span class="bar"><i class="${v>80?'hi':''}" style="width:${Math.min(v,100)}%"></i></span>${v}%`; }

function fwCell(s){
  // Current RouterBOOT the board is running (= "current-firmware").
  if (!s.firmware) return `<span class="hint" title="No RouterBOOT (CHR/x86) or not readable">—</span>`;
  const up = s.firmware_upgrade && s.firmware_upgrade !== s.firmware;
  return esc(s.firmware) + (up ? ` <span style="color:var(--warn);cursor:help"
    title="Upgrade available: installed RouterOS bundles RouterBOOT ${esc(s.firmware_upgrade)}. Use Update firmware (RouterBOOT)…">⬆</span>` : "");
}

function render(){
  const g = $("#groupFilter").value;
  const groups = [...new Set(routers.map(r=>r.group).filter(Boolean))].sort();
  $("#groupFilter").innerHTML = `<option value="">All groups</option>` +
    groups.map(x=>`<option ${x===g?"selected":""}>${esc(x)}</option>`).join("");
  const list = routers.filter(r=>!g || r.group===g);
  $("#empty").hidden = routers.length > 0;
  $("#rows").innerHTML = list.map(r=>{
    const s = status[r.id];
    const dot = !s ? "" : s.online ? "on" : "off";
    const rest = !s ? `<td colspan="8" class="hint">checking…</td>` :
      s.online ? `<td>${esc(s.identity)}</td><td>${esc(s.version)}</td><td>${esc(s.board)}</td><td>${fwCell(s)}</td>
        <td>${esc(s.uptime)}</td><td>${bar(s.cpu)}</td><td>${bar(s.mem)}</td><td class="num">${s.latency_ms}</td>`
      : `<td colspan="8"><span class="err" title="${esc(s.error)}">${esc(s.error)}</span></td>`;
    return `<tr>
      <td><input type="checkbox" data-sel="${r.id}" ${selected.has(r.id)?"checked":""}></td>
      <td><span class="dot ${dot}"></span></td>
      <td>${esc(r.name)} ${r.group?`<span class="tag">${esc(r.group)}</span>`:""}</td>
      <td style="font-family:var(--mono)">${esc(r.host)}</td>
      <td><span class="tag">${r.method==="api"?(r.api_ssl?"API-SSL":"API"):"SSH"}</span></td>
      ${rest}
      <td><button class="link" data-traf="${r.id}">traffic</button><button class="link" data-edit="${r.id}">edit</button></td></tr>`;
  }).join("");
  const on = Object.values(status).filter(s=>s.online).length;
  $("#sum").textContent = `${routers.length} routers · ${on} online · ${routers.length-on} down`;
  $("#selCount").textContent = selected.size;
  $("#selAll").checked = list.length && list.every(r=>selected.has(r.id));
}

async function loadRouters(){ routers = await api("/api/routers"); render(); }
async function loadStatus(){
  $("#refresh").disabled = true;
  try { status = await api("/api/status"); } catch(e){ alert(e.message); }
  $("#refresh").disabled = false; render();
}

let lastResults = null;

function openOut(label){
  $("#outTitle").textContent = label;
  $("#outMeta").textContent = "";
  $("#out").innerHTML = `<div class="hint" style="padding:16px 0"><span class="spin"></span>Running on ${selected.size} router(s)…</div>`;
  if (!$("#outDlg").open) $("#outDlg").showModal();
}

function show(results, label){
  lastResults = {results, label, time: new Date().toLocaleTimeString()};
  $("#lastOut").disabled = false;
  render_out();
  if (!$("#outDlg").open) $("#outDlg").showModal();
}

function render_out(){
  const {results, label, time} = lastResults;
  const entries = Object.entries(results);
  const ok = entries.filter(([,r])=>r.ok).length;
  $("#outTitle").textContent = label;
  $("#outMeta").textContent = `${time} · ${ok}/${entries.length} succeeded`;
  $("#out").innerHTML = entries.map(([id,res])=>{
    const r = routers.find(x=>x.id===id) || {name:id};
    return `<div class="res ${res.ok?"":"fail"}"><div class="h">
      <span class="dot ${res.ok?"on":"off"}"></span><b>${esc(r.name)}</b>
      <span class="tag">${esc(r.host||"")}</span></div>
      <pre>${esc(res.ok?(res.result?.text ?? res.result):res.error)}${res.ok&&res.result?.download?
        `\n<a class="dl" href="${esc(res.result.download)}" download>⬇ Download backup</a>`:""}</pre></div>`;
  }).join("");
}

function fail(e){
  $("#outMeta").textContent = "";
  $("#out").innerHTML = `<div class="res fail"><pre>${esc(e.message)}</pre></div>`;
}

function needSel(){ if(!selected.size){ alert("Select at least one router."); return false;} return true; }

async function runCmd(){
  const command = $("#cmd").value.trim();
  if (!command || !needSel()) return;
  $("#runBtn").disabled = true;
  openOut(command);
  try { show(await api("/api/exec",{method:"POST",body:JSON.stringify({router_ids:[...selected],command})}), command); }
  catch(e){ fail(e); }
  $("#runBtn").disabled = false;
}

async function doAction(a,label){
  if (!needSel()) return;
  if (a==="reboot" && !confirm(`Reboot ${selected.size} router(s)?`)) return;
  openOut(label);
  try { show(await api("/api/action",{method:"POST",body:JSON.stringify({router_ids:[...selected],action:a})}), label); }
  catch(e){ fail(e); }
  if (a==="backup") loadBackups();
}

function openDlg(r){
  editing = r?.id || null;
  const f = $("#frm");
  f.reset();
  if (r) for (const [k,v] of Object.entries(r)) {
    const el = f.elements[k]; if (!el) continue;
    if (el.type==="checkbox") el.checked = !!v; else el.value = v ?? "";
  }
  f.elements.password.placeholder = r ? "unchanged" : "";
  if (r) f.elements.password.value = "";
  $("#delBtn").hidden = !r;
  $("#dlg").showModal();
}

$("#frm").addEventListener("submit", async e=>{
  e.preventDefault();
  const f = e.target, d = Object.fromEntries(new FormData(f));
  d.api_ssl = f.elements.api_ssl.checked; d.verify_ssl = f.elements.verify_ssl.checked;
  d.port = d.port ? +d.port : null;
  d.ssh_port = d.ssh_port ? +d.ssh_port : null;
  try {
    await api(editing?`/api/routers/${editing}`:"/api/routers",{method:editing?"PUT":"POST",body:JSON.stringify(d)});
    $("#dlg").close(); await loadRouters(); loadStatus();
  } catch(err){ alert(err.message); }
});
$("#delBtn").onclick = async ()=>{
  if (!confirm("Delete this router?")) return;
  await api(`/api/routers/${editing}`,{method:"DELETE"}); selected.delete(editing);
  $("#dlg").close(); delete status[editing]; loadRouters();
};
$("#cancel").onclick = ()=>$("#dlg").close();
$("#addBtn").onclick = ()=>openDlg(null);
$("#refresh").onclick = loadStatus;
$("#runBtn").onclick = runCmd;
// Popup size: drag the bottom-right corner to resize; size is remembered
(() => {
  const d = $("#outDlg");
  try { const s = JSON.parse(localStorage.getItem("outSize")||"null");
        if (s) { d.style.width = s.w+"px"; d.style.height = s.h+"px"; } } catch {}
  new ResizeObserver(() => {
    if (!d.open || d.classList.contains("max")) return;
    try { localStorage.setItem("outSize", JSON.stringify({w:d.offsetWidth, h:d.offsetHeight})); } catch {}
  }).observe(d);
  $("#outMax").onclick = () => d.classList.toggle("max");
})();
$("#outClose").onclick = ()=>$("#outDlg").close();
// Close on a genuine backdrop click only: press AND release outside the popup,
// so finishing a resize drag doesn't close it
(() => {
  const d = $("#outDlg");
  const outside = e => { const r = d.getBoundingClientRect();
    return e.clientX<r.left || e.clientX>r.right || e.clientY<r.top || e.clientY>r.bottom; };
  let downOutside = false;
  d.addEventListener("mousedown", e => { downOutside = e.target === d && outside(e); });
  d.addEventListener("click", e => { if (downOutside && e.target === d && outside(e)) d.close(); downOutside = false; });
})();
$("#lastOut").onclick = ()=>{ if (lastResults){ render_out(); $("#outDlg").showModal(); } };
$("#outCopy").onclick = async ()=>{
  if (!lastResults) return;
  const txt = Object.entries(lastResults.results).map(([id,res])=>{
    const r = routers.find(x=>x.id===id) || {name:id};
    return `=== ${r.name} (${r.host||""}) ===\n${res.ok?(res.result?.text ?? res.result):"ERROR: "+res.error}`;
  }).join("\n\n");
  let ok = false;
  try { await navigator.clipboard.writeText(txt); ok = true; }
  catch { // clipboard API needs HTTPS; fall back for plain http://IP access
    const t = document.createElement("textarea"); t.value = txt;
    $("#outDlg").appendChild(t); t.select();
    try { ok = document.execCommand("copy"); } catch {}
    t.remove();
  }
  $("#outCopy").textContent = ok ? "copied" : "copy failed";
  setTimeout(()=>$("#outCopy").textContent="copy all", 1500);
};
$("#groupFilter").onchange = render;
$("#cmd").addEventListener("keydown", e=>{ if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)) runCmd(); });
$("#rows").addEventListener("click", e=>{
  const s = e.target.dataset.sel, ed = e.target.dataset.edit;
  if (s) { e.target.checked ? selected.add(s) : selected.delete(s); render(); }
  if (ed) openDlg(routers.find(r=>r.id===ed));
  const tf = e.target.dataset.traf;
  if (tf) openTraffic(routers.find(r=>r.id===tf));
});
$("#selAll").onchange = e=>{
  const g = $("#groupFilter").value;
  routers.filter(r=>!g||r.group===g).forEach(r=> e.target.checked?selected.add(r.id):selected.delete(r.id));
  render();
};
$("#qa").innerHTML = ACTIONS.map(([a,l])=>`<button class="${a==="reboot"?"danger":""}" data-a="${a}">${l}</button>`).join("");
$("#qa").addEventListener("click", e=>{ const a=e.target.dataset.a; if(a) doAction(a, e.target.textContent); });
const kb = n => n>1048576 ? (n/1048576).toFixed(1)+" MB" : Math.max(1,Math.round(n/1024))+" KB";
async function loadBackups(){
  let list=[]; try { list = await api("/api/backups"); } catch(e){}
  $("#bk").innerHTML = list.length ? list.map(b=>`<div class="it">
    <div class="nm"><b>${esc(b.router)}</b><div title="${esc(b.file)}">${esc(b.file)}</div>
      <span class="hint">${new Date(b.mtime*1000).toLocaleString()} · ${kb(b.size)}</span></div>
    <a class="dl" href="${esc(b.url)}" download>Download</a>
    <button class="link" data-delbk="${esc(b.url)}">✕</button></div>`).join("")
    : `<div class="hint" style="padding:12px 14px">No backups yet. Select routers and click <b>Save backup</b>.</div>`;
}
$("#bk").addEventListener("click", async e=>{
  const u = e.target.dataset.delbk; if(!u || !confirm("Delete this backup from the server?")) return;
  await api(u,{method:"DELETE"}); loadBackups();
});
$("#bkRefresh").onclick = loadBackups;

// ----------------------------------------------------------------- manage users
const DEFAULT_SSHKEY = "";   // optionally pre-fill a public key here
function usersBody(apply){
  return {
    router_ids: [...selected],
    keep: $("#uKeep").value.split(",").map(s=>s.trim()).filter(Boolean),
    ensure_name: $("#uName").value.trim(),
    ensure_password: $("#uPass").value,
    ensure_group: $("#uGroup").value.trim() || "full",
    ensure_sshkey: $("#uKey").value.trim(),
    apply,
  };
}
function usersRender(results){
  const el = $("#uResult"); el.hidden = false;
  el.innerHTML = Object.entries(results).map(([id,res])=>{
    const r = routers.find(x=>x.id===id) || {name:id};
    return `<div class="res ${res.ok?"":"fail"}"><div class="h">
      <span class="dot ${res.ok?"on":"off"}"></span><b>${esc(r.name)}</b>
      <span class="tag">${esc(r.host||"")}</span></div>
      <pre>${esc(res.ok?(res.result?.text ?? res.result):res.error)}</pre></div>`;
  }).join("");
}
async function usersCall(apply){
  if (!selected.size) { alert("Select at least one router first."); return; }
  const btns = ["#uPreview","#uApply","#uCancel"]; btns.forEach(b=>$(b).disabled=true);
  $("#uResult").hidden = false;
  $("#uResult").innerHTML = `<div class="hint" style="padding:12px">${apply?"Applying":"Checking"} on ${selected.size} router(s)…</div>`;
  try {
    const res = await api("/api/users/cleanup",{method:"POST",body:JSON.stringify(usersBody(apply))});
    usersRender(res);
    // enable Apply only after a successful preview
    $("#uApply").disabled = apply;
  } catch(e){ $("#uResult").innerHTML = `<div class="res fail"><pre>${esc(e.message)}</pre></div>`; }
  $("#uPreview").disabled = false; $("#uCancel").disabled = false;
}
$("#usersBtn").onclick = ()=>{
  if (!selected.size) { alert("Select the routers to manage first."); return; }
  $("#usersCount").textContent = selected.size;
  $("#uResult").hidden = true; $("#uResult").innerHTML = ""; $("#uApply").disabled = true;
  if (DEFAULT_SSHKEY && !$("#uKey").value) $("#uKey").value = DEFAULT_SSHKEY;
  $("#usersDlg").showModal();
};
$("#uCancel").onclick = ()=>$("#usersDlg").close();
$("#uPreview").onclick = ()=>usersCall(false);
// re-preview required if the form changes after a preview
["#uKeep","#uName","#uGroup","#uPass","#uKey"].forEach(id=>$(id).addEventListener("input",()=>$("#uApply").disabled=true));
$("#uApply").onclick = async ()=>{
  const n = selected.size;
  if (!confirm(`Apply to ${n} router(s)? This permanently removes every account not in the keep list.`)) return;
  await usersCall(true);
  loadStatus();
};

// ----------------------------------------------------------------- update RouterOS
function updRender(results){
  const el = $("#updResult"); el.hidden = false;
  let avail = 0;
  el.innerHTML = Object.entries(results).map(([id,res])=>{
    const r = routers.find(x=>x.id===id) || {name:id};
    if (res.ok && res.result?.available) avail++;
    const tag = !res.ok ? "" : res.result?.available
      ? `<span class="tag" style="color:var(--warn)">update available</span>`
      : (res.result?.action ? "" : `<span class="tag" style="color:var(--ok)">up to date</span>`);
    return `<div class="res ${res.ok?"":"fail"}"><div class="h">
      <span class="dot ${res.ok?"on":"off"}"></span><b>${esc(r.name)}</b>${tag}</div>
      <pre>${esc(res.ok?(res.result?.text ?? res.result):res.error)}</pre></div>`;
  }).join("");
  return avail;
}
async function updCall(apply){
  if (!selected.size) { alert("Select at least one router first."); return; }
  ["#updCheck","#updApply","#updCancel"].forEach(b=>$(b).disabled=true);
  $("#updResult").hidden = false;
  $("#updResult").innerHTML = `<div class="hint" style="padding:12px">${apply?"Installing updates — affected routers will reboot":"Checking"} on ${selected.size} router(s)… this can take a moment.</div>`;
  try {
    const res = await api("/api/updates",{method:"POST",body:JSON.stringify({router_ids:[...selected],apply})});
    const avail = updRender(res);
    $("#updApply").disabled = apply || avail===0;
    $("#updApply").textContent = avail ? `Install updates (${avail})` : "Install updates";
  } catch(e){ $("#updResult").innerHTML = `<div class="res fail"><pre>${esc(e.message)}</pre></div>`; }
  $("#updCheck").disabled = false; $("#updCancel").disabled = false;
}
$("#updBtn").onclick = ()=>{
  if (!selected.size) { alert("Select the routers to update first."); return; }
  $("#updCount").textContent = selected.size;
  $("#updResult").hidden = true; $("#updResult").innerHTML = "";
  $("#updApply").disabled = true; $("#updApply").textContent = "Install updates";
  $("#updDlg").showModal();
};
$("#updCancel").onclick = ()=>$("#updDlg").close();
$("#updCheck").onclick = ()=>updCall(false);
$("#updApply").onclick = async ()=>{
  if (!confirm("Install updates now? Routers that are out of date will download the new RouterOS and reboot.")) return;
  await updCall(true);
  loadStatus();
};

// ------------------------------------------------------- RouterBOOT firmware
function fwRender(results){
  const el = $("#fwResult"); el.hidden = false;
  let avail = 0;
  el.innerHTML = Object.entries(results).map(([id,res])=>{
    const r = routers.find(x=>x.id===id) || {name:id};
    const x = res.ok ? res.result : null;
    if (x?.available && !x.action) avail++;
    const tag = !x ? "" :
      x.action==="rebooting" ? `<span class="tag" style="color:var(--warn)">upgraded · rebooting</span>` :
      x.action==="pending-reboot" ? `<span class="tag" style="color:var(--warn)">upgraded · reboot pending</span>` :
      x.action==="unconfirmed" ? `<span class="tag" style="color:var(--bad)">not confirmed</span>` :
      !x.routerboard ? `<span class="tag">no RouterBOOT</span>` :
      x.available ? `<span class="tag" style="color:var(--warn)">firmware update</span>` :
      `<span class="tag" style="color:var(--ok)">current</span>`;
    return `<div class="res ${res.ok&&x.action!=="unconfirmed"?"":"fail"}"><div class="h">
      <span class="dot ${res.ok?"on":"off"}"></span><b>${esc(r.name)}</b>${tag}</div>
      <pre>${esc(res.ok?x.text:res.error)}</pre></div>`;
  }).join("");
  return avail;
}
async function fwCall(apply){
  if (!selected.size) { alert("Select at least one router first."); return; }
  ["#fwCheck","#fwApply","#fwCancel"].forEach(b=>$(b).disabled=true);
  $("#fwResult").hidden = false;
  $("#fwResult").innerHTML = `<div class="hint" style="padding:12px"><span class="spin"></span>${apply?"Upgrading firmware (waits for each router to confirm before rebooting)":"Checking firmware"} on ${selected.size} router(s)…</div>`;
  try {
    const res = await api("/api/firmware",{method:"POST",
      body:JSON.stringify({router_ids:[...selected], apply, reboot:$("#fwReboot").checked})});
    const avail = fwRender(res);
    $("#fwApply").disabled = apply || avail===0;
    $("#fwApply").textContent = avail && !apply ? `Upgrade firmware (${avail})` : "Upgrade firmware";
  } catch(e){ $("#fwResult").innerHTML = `<div class="res fail"><pre>${esc(e.message)}</pre></div>`; }
  $("#fwCheck").disabled = false; $("#fwCancel").disabled = false;
}
$("#fwBtn").onclick = ()=>{
  if (!selected.size) { alert("Select the routers to update first."); return; }
  $("#fwCount").textContent = selected.size;
  $("#fwResult").hidden = true; $("#fwResult").innerHTML = "";
  $("#fwApply").disabled = true; $("#fwApply").textContent = "Upgrade firmware";
  $("#fwDlg").showModal();
  fwCall(false);   // checking is read-only, so show the current state right away
};
$("#fwCancel").onclick = ()=>$("#fwDlg").close();
$("#fwCheck").onclick = ()=>fwCall(false);
$("#fwApply").onclick = async ()=>{
  const rb = $("#fwReboot").checked;
  if (!confirm(`Upgrade RouterBOOT firmware on the out-of-date routers${rb?" and reboot them":""}?\n\nDon't power them off during the upgrade.`)) return;
  await fwCall(true);
  loadStatus();
};

function setThemeIcon(){
  const light = document.documentElement.dataset.theme === "light";
  $("#themeBtn").textContent = light ? "☾" : "☀";
  $("#themeBtn").title = light ? "Switch to dark mode" : "Switch to light mode";
}
$("#themeBtn").onclick = () => {
  const t = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch {}
  setThemeIcon();
};
setThemeIcon();
function schedule(){ clearInterval(timer); if($("#auto").checked) timer=setInterval(loadStatus,30000); }
$("#auto").onchange = schedule;

loadRouters().then(loadStatus); loadBackups(); schedule();
