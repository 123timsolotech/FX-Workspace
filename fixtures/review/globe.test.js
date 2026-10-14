// Runs the real page's Globe module (MossadGPT's world globe) with stubbed DOM and API: how the assistant's
// world_monitor results and spoken replies move the globe, and how your own moves pause that.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../../index.html'), 'utf8');
const globeSource = html.slice(html.indexOf('const Globe = (() => {'), html.indexOf('/* ---- Settings > Diagnostics (diagnostics.py)'));
const voiceSource = html.slice(html.indexOf('const Voice = (() => {'), html.indexOf('// Settings > Appearance: show or hide one panel'));
const tick = () => new Promise(resolve => setImmediate(resolve));

function element() {
  const attrs = {}, classes = new Set();
  return {hidden: true, innerHTML: '', textContent: '', dataset: {}, style: {}, tabIndex: -1,
    classList: {add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x), toggle: (x, on) => (on ?? !classes.has(x)) ? classes.add(x) : classes.delete(x)},
    setAttribute(k, v) { attrs[k] = String(v); }, getAttribute: k => attrs[k] ?? null, removeAttribute(k) { delete attrs[k]; },
    querySelectorAll: () => [], addEventListener() {}};
}
function harness({mentions = [], runId = 1} = {}) {
  const els = new Map(), calls = [], saved = new Map();
  const $ = id => { if (!els.has(id)) els.set(id, element()); return els.get(id); };
  const s = {
    document: {documentElement: {dataset: {secret: 'mossad'}, classList: {contains: () => false, toggle() {}}}, addEventListener() {}, hidden: false},
    motionStill: () => true, store: {get: (k, d) => saved.has(k) ? saved.get(k) : d, set: (k, v) => saved.set(k, v)}, tabStore: {get: (k, d) => d, set() {}},
    actState: {runId}, S: {settings: {}}, $, esc: x => String(x), icon: () => '', safeUrl: u => /^https?:/.test(u) ? u : '#', toast() {},
    api: async (url, body) => { calls.push({url, body}); return url === '/api/osint/mentions' ? {items: mentions} : {}; },
    performance: {now: () => Date.now()}, requestAnimationFrame: () => 1, cancelAnimationFrame() {}, setTimeout: () => 1, clearTimeout() {},
    getComputedStyle: () => ({getPropertyValue: () => ''}), Date, Math, Number, String, Object, Array, JSON, Promise, Set, Map,
  };
  vm.createContext(s);
  vm.runInContext(globeSource + '\nglobalThis.Globe = Globe;', s);
  s.Globe._test.setMode('ops');
  return {s, G: s.Globe, $, calls, state: () => s.Globe._test.state()};
}

test('a spoken sentence that names a place moves the globe there and marks it', async () => {
  const h = harness({mentions: [{name: 'Haifa', lat: 32.81, lon: 35.0, radius_km: 60, kind: 'city'}]});
  await h.G.follow('Two aircraft are circling over Haifa right now.');
  assert.equal(h.calls[0].url, '/api/osint/mentions');
  const st = h.state();
  assert.equal(st.mark.name, 'Haifa');
  assert.ok(Math.abs(st.lat0 - 32.81) < 1e-6 && Math.abs(st.lon0 - 35.0) < 1e-6);   // motion off: it jumps there
  assert.equal(h.$('ops-focus').hidden, false);
  assert.match(h.$('ops-focus').innerHTML, /HAIFA/);
});

test('a tracked aircraft from world_monitor gets the reticle on the aircraft itself', () => {
  const h = harness();
  h.s.Globe._test.setData('military', {t: Date.now() / 1000, items: [['ae1234', 32.6, 35.3, 9000, 90, 700, 'RCH123', '']]});
  h.G.onWorldEvent({id: 7, kind: 'tool', tool: 'world_monitor', status: 'done', ok: true,
    world: {action: 'track', at: 1, evidence: [{n: 1, url: 'https://globe.adsb.lol/?icao=ae1234', title: 'RCH123'}], layers: [],
            focus: {t: 5, lat: 32.6, lon: 35.3, label: 'RCH123', radius_km: 150, kind: 'aircraft', layer: 'military', id: 'ae1234'}}}, 1);
  const st = h.state();
  assert.equal(st.sel.layer, 'military');
  assert.equal(st.sel.item[0], 'ae1234');
  assert.equal(st.mark.ring, null);                                  // an object carries the target, no area ring
  assert.equal(h.$('ops-evidence').hidden, false);
  assert.match(h.$('ops-evidence').innerHTML, /data-source="1"/);
});

test('moving the globe yourself pauses following until the next question; Resume goes to the latest target', async () => {
  const h = harness({mentions: [{name: 'Gaza', lat: 31.42, lon: 34.38, radius_km: 35, kind: 'region'}]});
  h.G.zoomBy(2);                                                     // your own move
  const before = h.state();
  await h.G.follow('Reports of strikes in Gaza.');
  assert.equal(h.state().mark, null);                                // not pulled away
  assert.equal(h.state().lastFocus.name, 'Gaza');                    // but remembered
  assert.equal(h.$('ops-follow').hidden, false);
  assert.match(h.$('ops-follow').innerHTML, /Resume/);
  assert.equal(h.state().zoom, before.zoom);
  h.G.resume();
  assert.equal(h.state().mark.name, 'Gaza');
  h.G.zoomBy(2);
  h.s.actState.runId = 2;                                            // your next question
  await h.G.follow('Gaza again.');
  assert.equal(h.state().mark.name, 'Gaza');
  assert.equal(h.state().manualRun, 1);
});

test('turning following off keeps the globe still; an interrupted run cannot move it', async () => {
  const h = harness({mentions: [{name: 'Beirut', lat: 33.89, lon: 35.5, radius_km: 100, kind: 'city'}]});
  h.G.setFollow(false);
  await h.G.follow('Quiet in Beirut.');
  assert.equal(h.state().mark, null);
  assert.match(h.$('ops-follow').innerHTML, /Not following/);
  h.G.setFollow(true);                                               // goes to what it missed
  assert.equal(h.state().mark.name, 'Beirut');
  h.G.cancelFollow(true);                                            // voice interrupted during run 1
  h.G.onWorldEvent({id: 9, status: 'done', ok: true, world: {action: 'near', focus: {lat: 10, lon: 10, name: 'Elsewhere'}}}, 1);
  assert.equal(h.state().mark.name, 'Beirut');
});

test('the night side follows the sun', () => {
  const h = harness();
  const [lon, lat] = h.G._test.sunPoint(Date.UTC(2026, 2, 20, 12, 0, 0));          // March equinox, noon UTC
  assert.ok(Math.abs(lon) < 0.01 && Math.abs(lat) < 2, `${lon}, ${lat}`);
  const [, june] = h.G._test.sunPoint(Date.UTC(2026, 5, 21, 0, 0, 0));
  assert.ok(june > 23 && june < 23.5, String(june));
  const ring = h.G._test.smallCircle(0, 0, 111.2);                                   // ~1 degree around 0,0
  assert.ok(Math.abs(Math.hypot(ring[0], ring[1]) - 1) < 0.01);
});

test('voice: each spoken sentence is handed to the globe as it starts playing (MossadGPT)', async () => {
  const followed = [], highlighted = [], timers = [];
  const graph = () => ({connect() {}, disconnect() {}, gain: {value: 1}});
  const ctx = {currentTime: 2, sampleRate: 16000, state: 'running', destination: {}, createAnalyser: graph, createGain: graph,
    decodeAudioData: async () => ({duration: 1}), createBufferSource() { return {connect() {}, start() {}, stop() {}}; }};
  const el = () => ({hidden: false, dataset: {}, style: {}, textContent: '', classList: {add() {}, remove() {}, toggle() {}, contains: () => false},
    setAttribute() {}, addEventListener() {}, querySelector: () => el(), querySelectorAll: () => []});
  const s = {S: {settings: {voice_conversation: false}, tools: []}, document: {documentElement: {dataset: {secret: 'mossad'}}, addEventListener() {}},
    window: {}, navigator: {}, AudioContext: function () { return ctx; }, Uint8Array, Int16Array, Float32Array, ArrayBuffer, DataView, AbortController, Date, Math,
    performance: {now: () => Date.now()}, addEventListener() {}, requestAnimationFrame: () => 0, cancelAnimationFrame() {}, motionStill: () => true, motionOff: () => false,
    setTimeout: (fn, ms) => { timers.push(fn); return timers.length; }, clearTimeout() {}, store: {get: (k, d) => d, set() {}}, esc: String, icon: () => '',
    fetch: async () => ({ok: true, arrayBuffer: async () => new ArrayBuffer(1)}), api: async () => ({}),
    Globe: {follow: t => followed.push(t), highlightEvidence: r => highlighted.push(r), cancelFollow() {}, voiceFocus() {}}};
  vm.createContext(s);
  vm.runInContext(voiceSource.replace('return { open, close,', `return { _seed(v) { stage = v.stage; ctx = v.ctx; vol = ctx.createGain(); outA = ctx.createAnalyser(); state = 'thinking'; }, _pump: () => pump(), open, close,`) + '\nglobalThis.Voice = Voice;', s);
  s.Voice._seed({stage: el(), ctx});
  s.Voice.beginTurn('What is over Haifa?', {voice: true, voiceLang: 'en'});
  s.Voice.finish({status: 'success', message: 'Two military aircraft are near Haifa right now, both broadcasting [2].'});
  for (let i = 0; i < 5; i++) { await s.Voice._pump(); await tick(); }
  timers.forEach(fn => fn());
  // The first piece ends at its first clause (it starts talking sooner); each piece goes to the globe as it plays.
  assert.deepEqual(followed, ['Two military aircraft are near Haifa right now,', 'both broadcasting.']);
  assert.deepEqual(highlighted.map(r => Array.from(r)), [[2]]);                    // only the piece that cites [2]
});
