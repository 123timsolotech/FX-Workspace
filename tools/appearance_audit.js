/* Appearance audit (1.4.3): every surface in every appearance, light and dark, screenshot and checked.

   node tools/appearance_audit.js <out-dir> [--url http://127.0.0.1:8010] [--only clay,mono] [--surfaces home,chat]

   Needs: a running dev server (the fx-dev launch config: port 8010, awareness and diagnostics off), Google Chrome,
   and playwright-core (npm i playwright-core in any folder, then NODE_PATH=<that folder>\node_modules).
   It changes the dev server's appearance settings while it runs and puts them back at the end.

   For each appearance x scheme x surface it saves a screenshot and checks:
   - contrast: each piece of text against what's behind it (WCAG AA: 4.5:1, or 3:1 for large text);
   - focus: keyboard focus (real Tab presses) shows a ring that stands out 3:1 from its background;
   - leaks: colours on screen that are one of Classic's own tokens but not this appearance's, so a rule fell
     back to Classic;
   - layout: no sideways scroll, and no text cut off without an ellipsis.
   The out-dir gets report.json and contact.html - one row per surface, a column per appearance and scheme,
   each cell the screenshot with its issue count. */
const fs = require('fs'), path = require('path');
const { chromium } = require('playwright-core');

const args = process.argv.slice(2);
const OUT = path.resolve(args[0] || 'appearance-audit');
const opt = name => { const i = args.indexOf('--' + name); return i > 0 ? args[i + 1] : null; };
const URL = opt('url') || 'http://127.0.0.1:8010';
const CHROME = opt('chrome') || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const APPS = (opt('only') || 'classic,sunrise,terminal,eclipse,aurora,clay,mono,void,prism').split(',');
const SCHEMES = (opt('schemes') || 'light,dark').split(',');

// A chat with every kind of content a reply can have.
const REPLY = [
  '## A heading in the reply', '',
  'Plain text with **bold**, *italic*, `inline code` and a [link](https://example.com). A longer sentence follows so the line wraps and the reading width shows: the quick brown fox jumps over the lazy dog, twice.', '',
  '- a list item', '- another, with `code`', '  - a nested one', '', '1. first', '2. second', '',
  '> A quote, set apart from the reply.', '',
  '| Name | Size | Note |', '|---|---:|---|', '| alpha.gguf | 2.4 GB | the starter |', '| beta.gguf | 7.1 GB | needs 8 GB VRAM |', '',
  '```python', 'def fib(n):', '    # the n-th Fibonacci number', '    a, b = 0, 1', '    for _ in range(n):', '        a, b = b, a + b', '    return a', '```', '',
  '### A smaller heading', 'And a final paragraph after the code.',
].join('\n');
const CHAT = { id: 'tmp-audit', temporary: true, title: 'Audit chat', transcript: [
  { r: 'u', t: 'Show me every kind of reply content, please.' },
  { r: 'a', t: REPLY },
  { r: 'u', t: 'A short follow-up question?' },
  { r: 'a', t: 'A short answer.' },
] };
const APPROVAL = { id: 'audit-ap', title: 'Allow this action?', context: 'In this chat', actions: [
  { tool: 'write_file', label: 'Write notes.txt', tier: 'input', justification: 'You asked for a summary in a file.', undo_text: 'Can be undone', undoable: true,
    preview: { kind: 'diff', text: '@@ -1,2 +1,3 @@\n line one\n-line two\n+line two, changed\n+line three' } },
  { tool: 'delete_file', label: 'Delete old.txt', tier: 'destructive', justification: 'It is replaced by notes.txt.', warning: 'This cannot be undone.' },
] };

// [name, how to show it (runs in the page), viewport]
const SURFACES = [
  ['home', `showThread(false)`],
  ['chat', `showChat(${JSON.stringify(CHAT)}); renderApproval(${JSON.stringify(APPROVAL)})`],
  ['context', `showChat(${JSON.stringify(CHAT)}); await openContextMenu(document.getElementById('ctx-btn'))`],
  ['palette', `Palette.open()`],
  ['toast', `showThread(false); toast('Saved - this is a toast.')`],
  ...['appearance', 'agent', 'models', 'permissions', 'data', 'voice', 'websearch', 'network', 'about'].map(t => ['settings-' + t, `openSettings('${t}')`]),
  ...['images', 'videos', 'music', 'notes', 'docs', 'mail', 'compare', 'projects', 'routines', 'labs'].map(p => ['place-' + p, `showPlace('${p}')`]),
  ['phone-home', `showThread(false)`, { width: 360, height: 780 }],
  ['phone-chat', `showChat(${JSON.stringify(CHAT)})`, { width: 360, height: 780 }],
  ['phone-settings', `openSettings('appearance')`, { width: 360, height: 780 }],
  ['wide-chat', `showChat(${JSON.stringify(CHAT)})`, { width: 1920, height: 1080 }],
  ['mini', null, { width: 420, height: 520 }, '/?hud=1'],
];
const ONLY_S = opt('surfaces') ? opt('surfaces').split(',') : null;

// ---- In the page: the checks ---------------------------------------------------------------------------------
function pageAudit(classicTokens) {
  const probe = document.createElement('i'); document.body.appendChild(probe);
  const parse = c => {
    const n = (c.match(/[\d.]+/g) || []).map(Number);
    if (/^color\(srgb/.test(c)) return [n[0] * 255, n[1] * 255, n[2] * 255, n.length > 3 ? n[3] : 1];
    return [n[0] || 0, n[1] || 0, n[2] || 0, n.length > 3 ? n[3] : 1];
  };
  const resolve = v => { probe.style.color = ''; probe.style.color = v; return getComputedStyle(probe).color; };
  const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
  const over = (top, under) => { const a = top[3]; return [0, 1, 2].map(i => top[i] * a + under[i] * (1 - a)).concat(1); };
  const key = c => parse(c).slice(0, 3).map(Math.round).join(',') + (parse(c)[3] < 1 ? '/' + parse(c)[3].toFixed(2) : '');
  // This appearance's own token colours.
  const rs = getComputedStyle(document.documentElement), own = new Set();
  for (const name of Array.from(rs).filter(p => p.startsWith('--'))) { const v = rs.getPropertyValue(name).trim(); if (/^(#|rgb|hsl|color|oklch|lab)/.test(v)) own.add(key(resolve(v))); }
  const tokens = [...own];
  // What's behind an element: the backgrounds of it and its ancestors, composed. null: an image or gradient is behind it.
  const behind = el => {
    const stack = [];
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.backgroundImage && cs.backgroundImage !== 'none' && !/^url\(.*\.svg/.test(cs.backgroundImage)) return null;
      const bg = parse(cs.backgroundColor); if (bg[3] > 0) stack.push(bg);
      if (bg[3] >= 1) break;
    }
    let c = [255, 255, 255, 1]; if (!stack.length || stack[stack.length - 1][3] < 1) c = parse(getComputedStyle(document.body).backgroundColor);
    for (let i = stack.length - 1; i >= 0; i--) c = over(stack[i], c);
    return c;
  };
  const opacity = el => { let o = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement) o *= +getComputedStyle(e).opacity; return o; };
  const visible = el => { const r = el.getBoundingClientRect(); if (r.width < 1 || r.height < 1 || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return false;
    const cs = getComputedStyle(el); return cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const label = el => { const t = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40); return `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : ''} "${t}"`; };
  const issues = [];
  const seen = new Set();
  // Text contrast
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.nodeValue.trim() || /^[\s·•|/–—-]+$/.test(n.nodeValue)) continue;      // separators are decoration, not text
    const el = n.parentElement; if (!el || seen.has(el)) continue; seen.add(el);
    if (el.closest('[aria-hidden="true"], .sr-only, script, style, noscript, svg, canvas, #orb-sheet, [disabled], .is-disabled') || !visible(el)) continue;
    const cs = getComputedStyle(el); if (cs.color === 'rgba(0, 0, 0, 0)' || /transparent/.test(cs.webkitTextFillColor || '')) continue;
    const o = opacity(el); if (o < .15) continue;
    const bg = behind(el); if (!bg) continue;
    const fg = parse(cs.color); const fgA = [fg[0], fg[1], fg[2], fg[3] * o];
    const r = ratio(over(fgA, bg), bg), size = parseFloat(cs.fontSize), bold = +cs.fontWeight >= 700;
    const need = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    if (r < need - .05) issues.push({ kind: 'contrast', what: label(el), ratio: +r.toFixed(2), need, fg: key(cs.color), bg: bg.slice(0, 3).map(Math.round).join(',') });
  }
  // Leaks: Classic's own token colours that aren't this appearance's.
  if (classicTokens) {
    const classic = new Set(classicTokens), plain = new Set(['0,0,0', '255,255,255', '0,0,0/0.00']);
    const leaked = new Map();
    for (const el of document.querySelectorAll('body *')) {
      // The appearance cards and swatches show other appearances' colours on purpose.
      if (el.closest('#orb-sheet, svg, canvas, .th-pair, .th, .swatch, .lk') || !visible(el)) continue;
      const cs = getComputedStyle(el);
      for (const prop of ['color', 'backgroundColor', 'borderTopColor', 'borderLeftColor']) {
        if (prop.startsWith('border') && parseFloat(cs[prop.replace('Color', 'Width')]) === 0) continue;
        if (prop === 'color' && !(el.textContent || '').trim()) continue;
        const k = key(cs[prop]);
        if (classic.has(k) && !own.has(k) && !plain.has(k) && !leaked.has(k + prop)) leaked.set(k + prop, { kind: 'leak', what: label(el), prop, color: k });
      }
    }
    issues.push(...leaked.values());
  }
  // Layout
  const doc = document.scrollingElement;
  if (doc.scrollWidth > innerWidth + 1) issues.push({ kind: 'layout', what: 'the page scrolls sideways', width: doc.scrollWidth });
  for (const el of document.querySelectorAll('body *')) {
    if (!visible(el) || el.closest('#orb-sheet, pre, code, .action-preview, .action-args, table, [data-clip-ok]')) continue;
    const cs = getComputedStyle(el);
    if (!(el.scrollWidth > el.clientWidth + 2) || !['hidden', 'clip'].includes(cs.overflowX) || cs.textOverflow === 'ellipsis') continue;
    if (![...el.childNodes].some(c => c.nodeType === 3 && c.nodeValue.trim())) continue;
    issues.push({ kind: 'layout', what: label(el), cut: el.scrollWidth - el.clientWidth });
  }
  probe.remove();
  return { issues, tokens };
}
// Keyboard focus: is the ring visible against what's behind it?
function focusCheck() {
  const el = document.activeElement; if (!el || el === document.body) return null;
  const cs = getComputedStyle(el), r = el.getBoundingClientRect();
  if (r.width < 1 || r.bottom < 0 || r.top > innerHeight) return null;
  const parse = c => (c.match(/[\d.]+/g) || []).map(Number);
  const ring = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0 ? cs.outlineColor
    : cs.boxShadow !== 'none' ? (cs.boxShadow.match(/rgba?\([^)]+\)|color\([^)]+\)/) || [null])[0] : null;
  const t = (el.textContent || el.getAttribute('aria-label') || el.placeholder || '').trim().slice(0, 30);
  // A text box often shows focus on the frame around it (the composer's border and glow, :focus-within). That counts
  // only if the frame really changes when the box takes focus: measured focused and blurred (1.4.4).
  if (!ring && /^(INPUT|TEXTAREA)$/.test(el.tagName)) {
    const frames = []; for (let a = el.parentElement, i = 0; a && i < 4; a = a.parentElement, i++) frames.push(a);
    const look = a => { const s = getComputedStyle(a); return [s.borderColor, s.boxShadow, s.outlineStyle, s.outlineColor].join('|'); };
    const on = frames.map(look); el.blur(); const off = frames.map(look); el.focus({ preventScroll: true });
    const i = on.findIndex((v, k) => v !== off[k]);
    if (i >= 0) return { what: `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''} "${t}"`, ring: 'frame', outline: 'frame', shadow: on[i] };
  }
  return { what: `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}.${[...el.classList].slice(0, 2).join('.')} "${t}"`, ring, outline: cs.outlineStyle + ' ' + cs.outlineWidth, shadow: cs.boxShadow };
}

(async () => {
  fs.mkdirSync(path.join(OUT, 'shots'), { recursive: true });
  const browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--force-color-profile=srgb'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  await ctx.addInitScript(() => { try { localStorage.setItem('aw.welcomed', 'true'); localStorage.setItem('aw.motion', '"off"'); } catch (_) {} });
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(String(e).slice(0, 300)));
  const errors = [];
  await page.goto(URL, { waitUntil: 'load' });
  await page.waitForFunction('typeof S !== "undefined" && S.settings && Object.keys(S.settings).length > 5', null, { timeout: 30000 });
  const before = await page.evaluate(() => ({ appearance: S.settings.appearance, color_mode: S.settings.color_mode }));
  const set = (app, scheme) => page.evaluate(async ([a, s]) => { await changeAppearance({ appearance: a }); await setColorMode(s); }, [app, scheme]);
  // Classic's own token colours, light and dark, for the leak check.
  const classic = {};
  for (const s of ['light', 'dark']) { await set('classic', s); await page.waitForTimeout(300); classic[s] = (await page.evaluate(pageAudit, null)).tokens; }
  // --switch-test: every appearance/scheme pair to every other one (18 x 17 switches). After each switch the page must
  // look exactly like that pair does when switched to from Classic: the same tokens, the same <html> attributes and
  // the same card selected in Settings › Appearance - nothing of the old pair left behind.
  if (args.includes('--switch-test')) {
    const pairs = APPS.flatMap(a => SCHEMES.map(s => [a, s]));
    const snap = () => page.evaluate(() => {
      const r = document.documentElement, cs = getComputedStyle(r);
      const tokens = ['--bg', '--surface', '--surface-2', '--text', '--muted', '--faint', '--accent', '--accent-text', '--border', '--glow', '--radius'].map(t => cs.getPropertyValue(t).trim());
      const ds = { ...r.dataset }; delete ds.act; delete ds.state;
      const d = Object.fromEntries(Object.entries(ds).sort());                      // order doesn't matter, content does
      const style = (r.getAttribute('style') || '').split(';').map(x => x.trim()).filter(Boolean).sort();
      const card = [...document.querySelectorAll('.theme-card.on b')].map(b => b.textContent.trim());
      return JSON.stringify({ tokens, d, style, card });
    });
    await page.evaluate(() => openSettings('appearance'));
    const base = {};
    for (const [a, s] of pairs) { await set('classic', 'light'); await set(a, s); await page.evaluate(() => renderAppearance()); await page.waitForTimeout(150); base[a + '/' + s] = await snap(); }
    const bad = [];
    for (const [a, s] of pairs) for (const [b, t] of pairs) {
      if (a === b && s === t) continue;
      await set(a, s); await set(b, t); await page.evaluate(() => renderAppearance()); await page.waitForTimeout(60);
      const got = await snap();
      if (got !== base[b + '/' + t]) bad.push({ from: a + '/' + s, to: b + '/' + t, got: JSON.parse(got), want: JSON.parse(base[b + '/' + t]) });
    }
    await page.evaluate(async b => { await changeAppearance({ appearance: b.appearance }); await setColorMode(b.color_mode); closeSettings(); }, before);
    fs.writeFileSync(path.join(OUT, 'switch.json'), JSON.stringify({ switches: pairs.length * (pairs.length - 1), bad }, null, 1));
    console.log(`${pairs.length * (pairs.length - 1)} switches, ${bad.length} left something behind`);
    for (const x of bad.slice(0, 5)) console.log(x.from, '->', x.to, JSON.stringify(x.got).slice(0, 300), '\n   want', JSON.stringify(x.want).slice(0, 300));
    await browser.close();
    return;
  }
  const report = { url: URL, when: new Date().toISOString(), cells: [], errors };
  for (const app of APPS) for (const scheme of SCHEMES) {
    await set(app, scheme); await page.waitForTimeout(400);
    for (const [name, show, vp, route] of SURFACES) {
      if (ONLY_S && !ONLY_S.includes(name)) continue;
      const size = vp || { width: 1440, height: 900 };
      let p = page;
      if (route) { p = await ctx.newPage(); await p.setViewportSize(size); await p.goto(URL + route, { waitUntil: 'load' }); await p.waitForTimeout(1200); }
      else {
        await page.setViewportSize(size);
        await page.evaluate(() => { document.querySelectorAll('.modal.open, .popover, #palette').forEach(e => e.classList.remove('open')); try { closeSettings(); } catch (_) {} try { closePopover(); } catch (_) {} try { Palette.close(); } catch (_) {} });
        try { await page.evaluate(`(async () => { ${show} })()`); } catch (e) { errors.push(`${app}/${scheme}/${name}: ${String(e).slice(0, 200)}`); }
        await page.waitForTimeout(500);
      }
      const shot = `${app}-${scheme}-${name}.jpg`;
      await p.screenshot({ path: path.join(OUT, 'shots', shot), type: 'jpeg', quality: 62 });
      const { issues } = await p.evaluate(pageAudit, app === 'classic' ? null : classic[scheme]);
      // Keyboard focus: twelve real Tab presses, each focused control's ring checked.
      const focus = [];
      if (!route && !(vp && vp.width < 400)) {
        await p.mouse.click(5, size.height - 5).catch(() => {});
        for (let i = 0; i < 12; i++) { await p.keyboard.press('Tab'); const f = await p.evaluate(focusCheck); if (f && !f.ring) focus.push({ kind: 'focus', what: f.what, outline: f.outline }); }
      }
      const all = issues.concat(focus.filter((f, i) => focus.findIndex(g => g.what === f.what) === i));
      report.cells.push({ app, scheme, surface: name, shot, issues: all });
      if (route) await p.close();
      process.stdout.write(`${app}/${scheme}/${name}: ${all.length}\n`);
    }
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.evaluate(async b => { await changeAppearance({ appearance: b.appearance }); await setColorMode(b.color_mode); }, before);
  await browser.close();
  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 1));
  // The contact sheet
  const cols = APPS.flatMap(a => SCHEMES.map(s => `${a}-${s}`));
  const rows = [...new Set(report.cells.map(c => c.surface))];
  const cell = (c) => { if (!c) return '<td></td>'; const n = c.issues.length;
    const kinds = Object.entries(c.issues.reduce((m, i) => (m[i.kind] = (m[i.kind] || 0) + 1, m), {})).map(([k, v]) => `${k} ${v}`).join(', ');
    const list = c.issues.slice(0, 30).map(i => `<li><b>${i.kind}</b> ${String(i.what).replace(/</g, '&lt;')}${i.ratio ? ` ${i.ratio}:1 (needs ${i.need})` : ''}${i.color ? ` ${i.prop} ${i.color}` : ''}</li>`).join('');
    return `<td class="${n ? 'bad' : 'ok'}"><a href="shots/${c.shot}" target="_blank"><img loading="lazy" src="shots/${c.shot}"></a><div class="n">${n ? kinds : 'clean'}</div>${n ? `<details><summary>${n} issue${n === 1 ? '' : 's'}</summary><ul>${list}</ul></details>` : ''}</td>`; };
  const html = `<!doctype html><meta charset="utf-8"><title>Appearance audit</title><style>
    body{font:13px system-ui;margin:16px;background:#f6f6f4;color:#222} table{border-collapse:collapse} th,td{border:1px solid #ddd;padding:4px;vertical-align:top;background:#fff}
    th{position:sticky;top:0;background:#eee;z-index:1} td img{width:220px;display:block} td.bad{background:#fff3f0} .n{font-size:12px;margin-top:3px} ul{margin:4px 0;padding-left:16px;max-width:220px;font-size:11px}
    th.row{position:sticky;left:0;text-align:left}</style>
    <h1>Appearance audit</h1><p>${report.when} · ${report.cells.length} screens · ${report.cells.reduce((n, c) => n + c.issues.length, 0)} issues${errors.length ? ` · ${errors.length} page errors` : ''}</p>
    <table><tr><th></th>${cols.map(c => `<th>${c}</th>`).join('')}</tr>
    ${rows.map(r => `<tr><th class="row">${r}</th>${cols.map(col => cell(report.cells.find(c => `${c.app}-${c.scheme}` === col && c.surface === r))).join('')}</tr>`).join('\n')}</table>
    ${errors.length ? `<h2>Page errors</h2><pre>${errors.map(e => e.replace(/</g, '&lt;')).join('\n')}</pre>` : ''}`;
  fs.writeFileSync(path.join(OUT, 'contact.html'), html);
  console.log(`\n${report.cells.length} screens, ${report.cells.reduce((n, c) => n + c.issues.length, 0)} issues -> ${path.join(OUT, 'contact.html')}`);
})().catch(e => { console.error(e); process.exit(1); });
