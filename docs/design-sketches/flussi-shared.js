/* Shared data + engine for the SMTrading flow-map sketches (three.js r128, simulated traffic). */
const PAL = { agent:0x7c9cff, core:0x34e0c4, broker:0xffb347, data:0x5fd3ff, ng:0xd78bff, tg:0x4fc3f7, err:0xff4d6d, db:0x9fb4d9 };
const HEX = k => '#' + PAL[k].toString(16).padStart(6, '0');
const NODES = {
  ceo:{n:'CEO', k:'agent', d:'Approva strategie e allocazioni, guida il piano verso l\'obiettivo'},
  pm:{n:'Portfolio Manager', k:'agent', d:'Decide entrate e uscite, invia gli ordini'},
  rm:{n:'Risk Manager', k:'agent', d:'Controlla limiti, stop e rischio di ogni ordine'},
  qr:{n:'Quant Researcher', k:'agent', d:'Cerca e prova strategie sullo storico'},
  ma:{n:'Market Analyst', k:'agent', d:'Analizza mercati, indicatori e livelli'},
  es:{n:'Execution Supervisor', k:'agent', d:'Verifica esecuzioni, stop e obiettivi attivi'},
  na:{n:'News Analyst', k:'agent', d:'Legge le notizie, mette in pausa prima degli eventi'},
  pa:{n:'Performance Auditor', k:'agent', d:'Confronta risultati reali e backtest'},
  nt:{n:'Notifier', k:'agent', d:'Manda i messaggi su Telegram'},
  pc:{n:'Paperclip', k:'agent', d:'Orchestratore: sveglia gli agenti, task e sessioni'},
  mcp:{n:'MCP SMTrading', k:'core', d:'La porta degli agenti: ~70 strumenti, ogni chiamata registrata'},
  eng:{n:'Motore ordini', k:'core', d:'Controlli di rischio, prenotazione cassa, ordini e protezioni'},
  sync:{n:'BrokerSync', k:'core', d:'Invia ai broker, legge esecuzioni, riconcilia'},
  rules:{n:'Regole automatiche', k:'core', d:'Scalping: entra ed esce da solo in meno di 1 s'},
  db:{n:'Database', k:'db', d:'Portafogli, ordini, decisioni, registro attività'},
  alp:{n:'Alpaca', k:'broker', d:'Broker azioni USA e cripto (conti di prova)'},
  eto:{n:'eToro', k:'broker', d:'Broker azioni ed ETF europei (demo)'},
  sim:{n:'Simulato', k:'broker', d:'Esecuzioni simulate dal motore'},
  px:{n:'Prezzi', k:'data', d:'Flussi in tempo reale Alpaca e Kraken, Yahoo, eToro'},
  ng:{n:'Neurograph', k:'ng', d:'Forchetta di rischio GARCH (get_forecast), sulla VPN'},
  tg:{n:'Telegram', k:'tg', d:'Avvisi, riepiloghi, domande al titolare'},
};
// simulated journeys: [path, label per hop]
const JOURNEYS = [
  { who:'pm', path:['pm','pc','mcp','eng','sync','alp','sync','eng','tg'], hops:['task','place_order','controlli rischio','invio','ordine','eseguito','registrazione','Ordine eseguito'] },
  { who:'rm', path:['rm','pc','mcp','ng','mcp','pc'], hops:['valutazione','get_forecast','forchetta 80%','range80','risposta'] },
  { who:'ma', path:['ma','pc','mcp','px','mcp','pc'], hops:['analisi','get_indicators','barre 1d','rsi · atr','risposta'] },
  { who:'qr', path:['qr','pc','mcp','db','mcp','pc'], hops:['ricerca','run_backtest','backtest','risultati','risposta'] },
  { who:'rules', path:['px','rules','eng','sync','alp'], hops:['tick','entrata','bracket','invio'] },
  { who:'nt', path:['nt','pc','mcp','tg'], hops:['messaggio','send_telegram','avviso'] },
  { who:'es', path:['es','pc','mcp','db'], hops:['controllo','list_orders','lettura'] },
  { who:'pm', path:['pm','pc','mcp','eng','sync','eto','sync'], hops:['task','place_order','controlli rischio','invio','ordine','eseguito'] },
  { who:'na', path:['na','pc','mcp','px'], hops:['notizie','get_news','fonti'] },
  { who:'ceo', path:['ceo','pc','mcp','db'], hops:['decisione','log_decision','registrata'] },
  { who:'pa', path:['pa','pc','mcp','db'], hops:['audit','get_performance','lettura'] },
  { who:'px', path:['px','eng','sim'], hops:['prezzo','esecuzione simulata'] },
];
const EDGES = (() => { const s = new Set(); JOURNEYS.forEach(j => j.path.forEach((a, i) => { if (i < j.path.length - 1) s.add([a, j.path[i + 1]].sort().join('|')); })); return [...s].map(e => e.split('|')); })();
const rnd = a => a[Math.floor(Math.random() * a.length)];

function glowTexture(inner = 'rgba(255,255,255,1)', mid = 'rgba(255,255,255,0.35)') {
  const c = document.createElement('canvas'); c.width = c.height = 128; const g = c.getContext('2d');
  const r = g.createRadialGradient(64, 64, 0, 64, 64, 64); r.addColorStop(0, inner); r.addColorStop(0.25, mid); r.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = r; g.fillRect(0, 0, 128, 128); const t = new THREE.CanvasTexture(c); return t;
}

/* World: renderer + camera + orbit + labels + nodes + arcs + particles + feed + info */
function makeWorld(host, opt = {}) {
  const W = () => host.clientWidth, H = () => host.clientHeight;
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, devicePixelRatio)); renderer.setSize(W(), H()); host.appendChild(renderer.domElement);
  const scene = new THREE.Scene(); if (opt.fog) scene.fog = new THREE.FogExp2(opt.fog, opt.fogD || 0.01);
  const camera = new THREE.PerspectiveCamera(opt.fov || 45, W() / H(), 0.1, 2000);
  const root = new THREE.Group(); scene.add(root);
  const GLOW = glowTexture();
  const labels = document.createElement('div'); labels.style.cssText = 'position:absolute;inset:0;pointer-events:none;overflow:hidden'; host.appendChild(labels);
  const nodes = {}; const arcs = {}; const parts = []; const tick = [];
  const st = { theta: opt.theta ?? 0.6, phi: opt.phi ?? 1.05, dist: opt.dist ?? 80, target: new THREE.Vector3(...(opt.target || [0, 0, 0])), auto: opt.auto ?? 0.05, drag: false };
  const placeCam = () => { const s = st; camera.position.set(s.target.x + s.dist * Math.sin(s.phi) * Math.sin(s.theta), s.target.y + s.dist * Math.cos(s.phi), s.target.z + s.dist * Math.sin(s.phi) * Math.cos(s.theta)); camera.lookAt(s.target); };
  let lx = 0, ly = 0, moved = 0;
  renderer.domElement.addEventListener('pointerdown', e => { st.drag = true; lx = e.clientX; ly = e.clientY; moved = 0; });
  addEventListener('pointerup', () => { st.drag = false; });
  addEventListener('pointermove', e => { if (!st.drag) return; moved += Math.abs(e.clientX - lx) + Math.abs(e.clientY - ly); st.theta -= (e.clientX - lx) * 0.005; st.phi = Math.min(1.45, Math.max(0.25, st.phi - (e.clientY - ly) * 0.004)); lx = e.clientX; ly = e.clientY; });
  renderer.domElement.addEventListener('wheel', e => { e.preventDefault(); st.dist = Math.min(220, Math.max(25, st.dist * (1 + e.deltaY * 0.001))); }, { passive: false });

  function addNode(key, pos, o = {}) {
    const N = NODES[key]; const col = new THREE.Color(PAL[N.k]); const g = new THREE.Group(); g.position.copy(pos);
    const body = o.mesh || new THREE.Mesh(new THREE.SphereGeometry(o.r || 1.6, 40, 20), new THREE.MeshBasicMaterial({ color: col }));
    g.add(body);
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color: col, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, opacity: 0.85 }));
    halo.scale.setScalar((o.r || 1.6) * (o.halo || 7)); g.add(halo);
    root.add(g);
    const lab = document.createElement('div');
    lab.style.cssText = `position:absolute;transform:translate(-50%,${o.labelBelow ? '60%' : '-160%'});font:600 12px/1.2 system-ui,sans-serif;color:#eaf0ff;white-space:nowrap;text-shadow:0 0 10px #000,0 0 3px #000;letter-spacing:.02em;transition:opacity .2s`;
    lab.innerHTML = `<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${HEX(N.k)};box-shadow:0 0 8px ${HEX(N.k)};margin-right:6px;vertical-align:1px"></span>${N.n}`;
    labels.appendChild(lab);
    nodes[key] = { g, body, halo, lab, base: halo.scale.x, heat: 0, key };
    return nodes[key];
  }
  function addArc(a, b, o = {}) {
    const A = nodes[a].g.position, B = nodes[b].g.position; const mid = A.clone().add(B).multiplyScalar(0.5);
    const lift = o.lift ?? A.distanceTo(B) * 0.25; const up = o.up ? o.up.clone() : new THREE.Vector3(0, 1, 0); mid.add(up.multiplyScalar(lift));
    const curve = new THREE.QuadraticBezierCurve3(A.clone(), mid, B.clone());
    const geo = new THREE.BufferGeometry().setFromPoints(curve.getPoints(48));
    const mat = new THREE.LineBasicMaterial({ color: o.color || 0x34507f, transparent: true, opacity: o.opacity ?? 0.45, blending: THREE.AdditiveBlending, depthWrite: false });
    const line = new THREE.Line(geo, mat); root.add(line);
    arcs[[a, b].sort().join('|')] = { curve, line, mat, a, base: mat.opacity };
  }
  function curveFor(a, b) { const k = [a, b].sort().join('|'); const arc = arcs[k]; if (!arc) return null; return { c: arc.curve, rev: arc.a !== a, arc }; }
  function spark(color, size) { const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false })); s.scale.setScalar(size); root.add(s); return s; }
  function shoot(j, o = {}) {
    const err = Math.random() < (o.errRate ?? 0.05); let i = 0; const speed = o.speed || 1.4;
    const hop = () => {
      if (i >= j.path.length - 1) return; const a = j.path[i], b = j.path[i + 1]; const cv = curveFor(a, b); if (!cv) { i++; return hop(); }
      const last = i === j.path.length - 2; const col = new THREE.Color(err && last ? PAL.err : PAL[NODES[a].k]);
      const head = spark(col, o.head || 2.6); const trail = []; for (let t = 0; t < (o.trail ?? 7); t++) trail.push(spark(col, (o.head || 2.6) * (1 - t / 9)));
      parts.push({ head, trail, cv, t: 0, speed, col, done: () => { [head, ...trail].forEach(s => { root.remove(s); s.material.dispose(); }); nodes[b].heat = 1; cv.arc.mat.opacity = Math.min(1, cv.arc.base + 0.5); i++; hop(); } });
      if (o.onHop) o.onHop(j, i, err && last);
    };
    hop(); if (o.feed) o.feed(j, err);
  }
  const tmp = new THREE.Vector3();
  let lastT = performance.now();
  function frame(t) {
    const dt = Math.min(0.05, (t - lastT) / 1000); lastT = t;
    if (!st.drag && st.auto) st.theta += dt * st.auto; placeCam();
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i]; p.t += dt * p.speed; const u = Math.min(1, p.t);
      const at = s => { const v = Math.max(0, Math.min(1, s)); return p.cv.c.getPoint(p.cv.rev ? 1 - v : v); };
      p.head.position.copy(at(u)); p.trail.forEach((s, k) => s.position.copy(at(u - (k + 1) * 0.035)));
      if (p.t >= 1) { parts.splice(i, 1); p.done(); }
    }
    Object.values(nodes).forEach(n => { n.heat *= 0.94; n.halo.scale.setScalar(n.base * (1 + n.heat * 0.6)); });
    Object.values(arcs).forEach(a => { a.mat.opacity += (a.base - a.mat.opacity) * 0.04; });
    tick.forEach(f => f(dt, t));
    renderer.render(scene, camera);
    const w = W(), h = H();
    Object.values(nodes).forEach(n => { n.g.getWorldPosition(tmp); tmp.project(camera); const vis = tmp.z < 1 && tmp.z > -1; n.lab.style.left = ((tmp.x + 1) / 2 * w) + 'px'; n.lab.style.top = ((1 - tmp.y) / 2 * h) + 'px'; n.lab.style.opacity = vis ? 1 : 0; });
    requestAnimationFrame(frame);
  }
  addEventListener('resize', () => { renderer.setSize(W(), H()); camera.aspect = W() / H(); camera.updateProjectionMatrix(); });
  // hover info
  const ray = new THREE.Raycaster(); const mouse = new THREE.Vector2();
  renderer.domElement.addEventListener('pointermove', e => {
    const r = renderer.domElement.getBoundingClientRect(); mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); ray.setFromCamera(mouse, camera);
    const hit = ray.intersectObjects(Object.values(nodes).map(n => n.body), false)[0]; const key = hit && Object.values(nodes).find(n => n.body === hit.object)?.key;
    if (opt.onHover) opt.onHover(key || null);
    Object.entries(arcs).forEach(([k, a]) => { a.base = !key ? (opt.arcOpacity ?? 0.45) : (k.split('|').includes(key) ? 0.95 : 0.08); });
  });
  requestAnimationFrame(frame);
  return { scene, camera, renderer, root, nodes, arcs, addNode, addArc, shoot, tick, st, GLOW, spark };
}

/* UI chrome: title, legend, live diary, info card, stats */
function chrome(host, title, subtitle) {
  const css = document.createElement('style'); css.textContent = `
  .ui{position:absolute;pointer-events:none;font:13px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:#dfe7ff}
  .ui .glass{background:rgba(8,14,28,.62);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border:1px solid rgba(140,170,255,.16);border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.35)}
  .ui-title{left:22px;top:18px;padding:12px 16px}.ui-title h1{margin:0;font-size:18px;letter-spacing:.02em}.ui-title p{margin:2px 0 0;color:#8ea0c9;font-size:12.5px}
  .ui-legend{left:22px;bottom:18px;padding:10px 14px;display:flex;gap:14px;flex-wrap:wrap;max-width:60%}.ui-legend span{white-space:nowrap}.ui-legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:0}
  .ui-feed{right:18px;top:18px;width:300px;padding:10px 12px;font:12px/1.6 ui-monospace,Consolas,monospace;color:#8ea0c9}.ui-feed b{color:#eaf0ff;font-weight:600}.ui-feed .e{color:#ff6b86}
  .ui-feed h4,.ui-info h4{margin:0 0 6px;font:600 11px system-ui;letter-spacing:.14em;text-transform:uppercase;color:#6f83b6}
  .ui-info{right:18px;bottom:18px;width:300px;padding:12px 14px;opacity:0;transition:opacity .2s}.ui-info b{font-size:14px}
  .ui-stats{left:50%;top:18px;transform:translateX(-50%);padding:8px 16px;display:flex;gap:18px;font-size:12px;color:#8ea0c9}.ui-stats b{color:#eaf0ff;font-size:15px;margin-right:4px}
  .ui-hint{left:50%;bottom:20px;transform:translateX(-50%);color:#6f83b6;font-size:12px}
  .sim{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:20px;background:#ffb34722;color:#ffcf8a;font-size:11px;vertical-align:2px}`;
  document.head.appendChild(css);
  const add = (cls, html) => { const d = document.createElement('div'); d.className = 'ui ' + cls; d.innerHTML = html; host.appendChild(d); return d; };
  add('ui-title glass', `<h1>${title}<span class="sim">traffico simulato</span></h1><p>${subtitle}</p>`);
  add('ui-legend glass', [['agent', 'agenti'], ['core', 'SMTrading'], ['db', 'database'], ['broker', 'broker'], ['data', 'prezzi'], ['ng', 'Neurograph'], ['tg', 'Telegram'], ['err', 'errore']].map(([k, n]) => `<span><i style="background:${HEX(k)};box-shadow:0 0 8px ${HEX(k)}"></i>${n}</span>`).join(''));
  const feed = add('ui-feed glass', '<h4>Chiamate in diretta</h4><div></div>'); const fl = [];
  const stats = add('ui-stats glass', '<span><b id="s1">0</b>chiamate/min</span><span><b id="s2">0</b>ordini</span><span><b id="s3">0</b>errori</span><span><b id="s4">—</b>tempo medio</span>');
  const info = add('ui-info glass', ''); add('ui-hint', 'trascina per girare · rotella per lo zoom · passa sopra un nodo');
  let calls = [], orders = 0, errs = 0, lat = [];
  function log(j, err) {
    const now = new Date().toTimeString().slice(0, 8); const tool = j.hops[1] || j.hops[0]; const ms = Math.round(40 + Math.random() * (tool === 'get_forecast' ? 900 : 300));
    calls.push(Date.now()); lat.push(ms); if (lat.length > 40) lat.shift(); if (tool === 'place_order' || j.who === 'rules') orders++; if (err) errs++;
    fl.unshift(`${now} <b>${NODES[j.who].n}</b> ${tool}${err ? ' <span class="e">✕ errore</span>' : ` · ${ms} ms`}`); fl.length = Math.min(fl.length, 10);
    feed.lastChild.innerHTML = fl.join('<br>');
    calls = calls.filter(t => Date.now() - t < 60000);
    stats.querySelector('#s1').textContent = calls.length; stats.querySelector('#s2').textContent = orders; stats.querySelector('#s3').textContent = errs;
    stats.querySelector('#s4').textContent = Math.round(lat.reduce((a, b) => a + b, 0) / lat.length) + ' ms';
  }
  function hover(key) { if (!key) { info.style.opacity = 0; return; } const N = NODES[key]; info.innerHTML = `<h4>${{ agent: 'Agente', core: 'SMTrading', db: 'SMTrading', broker: 'Broker', data: 'Dati', ng: 'Rete', tg: 'Messaggi' }[N.k]}</h4><b>${N.n}</b><div style="color:#9fb0d8;margin-top:4px">${N.d}</div>`; info.style.opacity = 1; }
  return { log, hover };
}

/* Page boot: full-screen stage, chrome, world, traffic loop. */
function bootPage(title, subtitle, worldOpt, setup, trafficOpt = {}) {
  document.body.style.cssText = `margin:0;height:100vh;overflow:hidden;background:${worldOpt.bg || 'radial-gradient(1400px 800px at 50% 0%,#14244a 0%,#070b14 60%,#04060c 100%)'}`;
  const host = document.createElement('div'); host.style.cssText = 'position:absolute;inset:0'; document.body.appendChild(host);
  const ui = chrome(host, title, subtitle);
  const w = makeWorld(host, Object.assign({ onHover: ui.hover }, worldOpt));
  setup(w);
  const go = () => { w.shoot(rnd(JOURNEYS), Object.assign({ feed: ui.log }, trafficOpt)); };
  for (let i = 0; i < 3; i++) setTimeout(go, i * 300);
  setInterval(go, trafficOpt.every || 700);
  return w;
}
