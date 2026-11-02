// Live interface bandwidth for one router.
// The server returns byte counters; rate = Δbytes × 8 / Δseconds between two polls.
(() => {
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const HIST = 60;
  // Ports that carry real traffic in/out of the box. Bridges, VLANs, tunnels etc.
  // are left out of the total because they repeat traffic already on these ports.
  const PHYSICAL = /^(ether|wlan|wifi|wifiwave2|lte|w60g)$/;                 // samples kept per interface for the chart
  const dlg = $("#trafDlg"), body = $("#trafRows"), tip = $("#trafTip");

  let rid = null, gen = 0, timer = null, prev = null;
  let hist = {};                   // name -> [{t, rx, tx}] (bits/s)
  let rowEls = {};                 // name -> {tr, cells…, canvas}

  const fmtBps = b => {
    if (b == null) return "…";
    const u = ["bps","kbps","Mbps","Gbps","Tbps"]; let i = 0;
    while (b >= 1000 && i < u.length-1) { b /= 1000; i++; }
    return (i ? b.toFixed(b < 10 ? 2 : b < 100 ? 1 : 0) : Math.round(b)) + " " + u[i];
  };
  const fmtBytes = b => {
    const u = ["B","KB","MB","GB","TB","PB"]; let i = 0;
    while (b >= 1024 && i < u.length-1) { b /= 1024; i++; }
    return (i ? b.toFixed(1) : b) + " " + u[i];
  };
  const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  // ---------------------------------------------------------------- open/close
  window.openTraffic = (router) => {
    rid = router.id; gen++; prev = null; hist = {}; rowEls = {};
    body.innerHTML = "";
    $("#trafTitle").textContent = `Traffic — ${router.name}`;
    $("#trafMeta").textContent = "connecting…";
    $("#trafEmpty").hidden = true;
    ["#totRx","#totTx"].forEach(s => $(s).textContent = "…");
    ["#totRxB","#totTxB","#totNote"].forEach(s => $(s).textContent = "");
    $("#totPeak").textContent = "collecting…";
    setView();
    if (!dlg.open) dlg.showModal();
    draw(document.querySelector('canvas.spark.big'), [], null);
    poll(gen);
  };
  dlg.addEventListener("close", () => { gen++; clearTimeout(timer); tip.hidden = true; });
  $("#trafClose").onclick = () => dlg.close();
  $("#trafMax").onclick = () => dlg.classList.toggle("max");
  $("#trafInterval").onchange = () => { clearTimeout(timer); poll(gen); };
  $("#trafHide").onchange = () => layout();
  $("#trafSort").onchange = () => layout();
  $("#trafView").onchange = () => { setView(); layout(); };
  // remember controls
  try {
    const s = JSON.parse(localStorage.getItem("trafPrefs") || "{}");
    if (s.interval) $("#trafInterval").value = s.interval;
    if (s.hide != null) $("#trafHide").checked = s.hide;
    if (s.sort) $("#trafSort").value = s.sort;
    if (s.view) $("#trafView").value = s.view;
  } catch {}
  ["#trafInterval","#trafHide","#trafSort","#trafView"].forEach(id => $(id).addEventListener("change", () => {
    try { localStorage.setItem("trafPrefs", JSON.stringify({
      interval: $("#trafInterval").value, view: $("#trafView").value, hide: $("#trafHide").checked, sort: $("#trafSort").value })); } catch {}
  }));

  // ---------------------------------------------------------------- polling
  async function poll(g) {
    if (g !== gen) return;
    try {
      const r = await fetch(`/api/routers/${rid}/traffic`);
      const d = await r.json();
      if (g !== gen) return;                       // router switched / closed meanwhile
      if (!r.ok) throw new Error(d.detail || r.statusText);
      ingest(d);
      $("#trafMeta").textContent = `updated ${new Date().toLocaleTimeString()}`;
      $("#trafMeta").classList.remove("err");
    } catch (e) {
      if (g !== gen) return;
      $("#trafMeta").textContent = e.message;
      $("#trafMeta").classList.add("err");
    }
    if (g === gen) timer = setTimeout(() => poll(g), +$("#trafInterval").value * 1000);
  }

  function ingest(d) {
    const pm = prev ? Object.fromEntries(prev.interfaces.map(i => [i.name, i])) : {};
    const dt = prev ? d.t - prev.t : 0;
    for (const i of d.interfaces) {
      const p = pm[i.name];
      let rx = null, tx = null;
      if (p && dt > 0) {
        // a negative delta means the counter was reset; show 0 for that sample
        rx = Math.max(0, (i.rx - p.rx) * 8 / dt);
        tx = Math.max(0, (i.tx - p.tx) * 8 / dt);
        (hist[i.name] ||= []).push({t: d.t, rx, tx});
        if (hist[i.name].length > HIST) hist[i.name].shift();
      }
      i.rxRate = rx; i.txRate = tx;
    }
    // router total = sum of enabled physical ports
    const phys = d.interfaces.filter(i => PHYSICAL.test(i.type) && !i.disabled);
    d.total = {rx: phys.reduce((s,i)=>s+i.rx,0), tx: phys.reduce((s,i)=>s+i.tx,0), n: phys.length};
    if (prev && prev.total && dt > 0) {
      const rx = Math.max(0, (d.total.rx - prev.total.rx) * 8 / dt);
      const tx = Math.max(0, (d.total.tx - prev.total.tx) * 8 / dt);
      d.total.rxRate = rx; d.total.txRate = tx;
      (hist.__total__ ||= []).push({t: d.t, rx, tx});
      if (hist.__total__.length > HIST) hist.__total__.shift();
    }
    prev = d;
    layout();
  }

  // ---------------------------------------------------------------- table
  function rowFor(i) {
    if (rowEls[i.name]) return rowEls[i.name];
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><div class="if-name"></div><div class="hint if-cmt"></div></td>
      <td><span class="tag if-type"></span></td>
      <td class="if-st"></td>
      <td class="num if-rx"></td>
      <td class="num if-tx"></td>
      <td class="spark-cell"><canvas class="spark"></canvas></td>
      <td class="num hint if-trx"></td>
      <td class="num hint if-ttx"></td>`;
    const q = s => tr.querySelector(s);
    const el = {tr, name:q(".if-name"), cmt:q(".if-cmt"), type:q(".if-type"), st:q(".if-st"),
                rx:q(".if-rx"), tx:q(".if-tx"), trx:q(".if-trx"), ttx:q(".if-ttx"), canvas:q("canvas")};
    el.canvas.dataset.name = i.name;
    return rowEls[i.name] = el;
  }

  function setView() {
    const v = $("#trafView").value;
    dlg.classList.toggle("v-total", v === "total");
    dlg.classList.toggle("v-iface", v !== "total");
  }

  function layoutTotal() {
    const t = prev.total;
    $("#totRx").textContent = fmtBps(t.rxRate);
    $("#totTx").textContent = fmtBps(t.txRate);
    $("#totRxB").textContent = fmtBytes(t.rx) + " total";
    $("#totTxB").textContent = fmtBytes(t.tx) + " total";
    $("#totNote").textContent = `Sum of ${t.n} physical port${t.n===1?"":"s"} (bridges, VLANs and tunnels excluded so traffic isn't counted twice)`;
    const h = hist.__total__ || [];
    const peak = h.length ? Math.max(...h.map(s => Math.max(s.rx, s.tx))) : null;
    $("#totPeak").textContent = peak != null ? `peak ${fmtBps(peak)}` : "collecting…";
    $("#totSpan").textContent = `${Math.round((HIST-1) * +$("#trafInterval").value / 60 * 10) / 10} min ago`;
    draw(document.querySelector('canvas.spark.big'), h, null);
  }

  function layout() {
    if (!prev) return;
    setView();
    if ($("#trafView").value === "total") return layoutTotal();
    let list = prev.interfaces.slice();
    if ($("#trafHide").checked) list = list.filter(i => i.running && !i.disabled);
    if ($("#trafSort").value === "busy")
      list.sort((a,b) => ((b.rxRate||0)+(b.txRate||0)) - ((a.rxRate||0)+(a.txRate||0)));
    $("#trafEmpty").hidden = list.length > 0;
    const keep = new Set(list.map(i => i.name));
    for (const [n, el] of Object.entries(rowEls)) if (!keep.has(n)) el.tr.remove();
    for (const i of list) {
      const el = rowFor(i);
      el.name.textContent = i.name;
      el.cmt.textContent = i.comment; el.cmt.hidden = !i.comment;
      el.type.textContent = i.type;
      el.st.innerHTML = i.disabled ? `<span class="st st-off">○ disabled</span>`
        : i.running ? `<span class="st st-up">● up</span>` : `<span class="st st-down">▼ down</span>`;
      el.rx.textContent = fmtBps(i.rxRate);
      el.tx.textContent = fmtBps(i.txRate);
      el.trx.textContent = fmtBytes(i.rx);
      el.ttx.textContent = fmtBytes(i.tx);
      body.appendChild(el.tr);                   // also applies the sort order
      draw(el.canvas, hist[i.name] || [], null);
    }
  }

  // ---------------------------------------------------------------- sparkline
  // Rx and Tx share one y-scale (one axis); scale = this interface's peak.
  function draw(cv, h, hoverIdx) {
    const W = cv.clientWidth || 180, H = cv.clientHeight || 34, dpr = devicePixelRatio || 1;
    if (cv.width !== W*dpr || cv.height !== H*dpr) { cv.width = W*dpr; cv.height = H*dpr; }
    const c = cv.getContext("2d");
    c.setTransform(dpr,0,0,dpr,0,0);
    c.clearRect(0,0,W,H);
    c.strokeStyle = css("--line"); c.lineWidth = 1;
    c.beginPath(); c.moveTo(0,H-0.5); c.lineTo(W,H-0.5); c.stroke();   // baseline
    if (h.length < 2) return;
    const max = Math.max(1, ...h.map(s => Math.max(s.rx, s.tx)));
    const x = k => (k + HIST - h.length) / (HIST - 1) * (W - 2) + 1;
    const y = v => H - 2 - v / max * (H - 5);
    for (const [key, col] of [["tx", css("--tx")], ["rx", css("--rx")]]) {
      c.strokeStyle = col; c.lineWidth = 2; c.lineJoin = c.lineCap = "round";
      c.beginPath();
      h.forEach((s, k) => k ? c.lineTo(x(k), y(s[key])) : c.moveTo(x(k), y(s[key])));
      c.stroke();
    }
    if (hoverIdx != null) {
      const X = x(hoverIdx);
      c.strokeStyle = css("--muted"); c.lineWidth = 1;
      c.beginPath(); c.moveTo(X, 0); c.lineTo(X, H); c.stroke();
      for (const [key, col] of [["rx", css("--rx")], ["tx", css("--tx")]]) {
        c.fillStyle = col; c.strokeStyle = css("--panel"); c.lineWidth = 2;
        c.beginPath(); c.arc(X, y(h[hoverIdx][key]), 4, 0, 7); c.fill(); c.stroke();
      }
    }
  }

  // hover: crosshair + tooltip with that sample's values
  dlg.addEventListener("mousemove", e => {
    const cv = e.target.closest?.("canvas.spark");
    if (!cv) return hideTip();
    const h = hist[cv.dataset.name] || [];
    if (h.length < 2) return hideTip();
    const r = cv.getBoundingClientRect();
    const pos = (e.clientX - r.left - 1) / (r.width - 2) * (HIST - 1) - (HIST - h.length);
    const k = Math.max(0, Math.min(h.length - 1, Math.round(pos)));
    draw(cv, h, k);
    const s = h[k], ago = Math.round(prev.t - s.t);
    tip.innerHTML = `<div class="hint">${ago ? ago + "s ago" : "now"}</div>
      <div><i class="sw" style="background:var(--rx)"></i>Rx <b>${fmtBps(s.rx)}</b></div>
      <div><i class="sw" style="background:var(--tx)"></i>Tx <b>${fmtBps(s.tx)}</b></div>`;
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let L = e.clientX + 14, T = cv.classList.contains("big") ? e.clientY - th - 12 : r.top - th - 6;
    if (L + tw > innerWidth - 8) L = e.clientX - tw - 14;
    if (T < 8) T = r.bottom + 6;
    tip.style.left = L + "px"; tip.style.top = T + "px";
    tip._cv = cv;
  });
  dlg.addEventListener("mouseleave", hideTip);
  function hideTip() {
    if (tip._cv) { draw(tip._cv, hist[tip._cv.dataset.name] || [], null); tip._cv = null; }
    tip.hidden = true;
  }
})();
