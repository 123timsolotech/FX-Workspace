// Runs the real page's interactive-reply code (D50) in a sandbox: formulas compile without eval and refuse what
// isn't math, a formula chart draws with its sliders, choices become buttons, a widget is sealed off.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../../index.html'), 'utf8');
const slice = (from, to) => html.slice(html.indexOf(from), html.indexOf(to));
const chartHelpers = slice('const CHART_COLORS = [', 'function chartBlock(src) {');
const interactive = slice('function chartLegend(series, C) {', 'window.addEventListener(\'message\', e => {');

function sandbox() {
  const s = {
    esc: x => String(x).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
    inlineMd: x => x, getComputedStyle: () => ({ getPropertyValue: () => '' }),
    document: { documentElement: { dataset: { scheme: 'dark' } } }, Math, Number, String, Object, Array, JSON, Map, Set, Error,
  };
  vm.createContext(s);
  vm.runInContext(chartHelpers + interactive + '\nObject.assign(globalThis, {MathExpr, fnChart, fnSpec, choicesBlock, widgetBlock, MD});', s);
  return s;
}

test('formulas compile without eval, with implicit multiplication and the usual functions', () => {
  const { MathExpr } = sandbox();
  const f = MathExpr.compile('2x^2 - 3(x+1) + sin(pi/2) + a', ['x', 'a']);
  assert.equal(f({ x: 2, a: 10 }), 2 * 4 - 9 + 1 + 10);
  assert.equal(MathExpr.compile('-x^2', ['x'])({ x: 3 }), -9);           // the power binds first
  assert.equal(MathExpr.compile('2^3^2', [])({}), 512);                   // right to left
  assert.equal(MathExpr.compile('sqrt x', ['x'])({ x: 16 }), 4);
  assert.equal(MathExpr.compile('max(1, x, 3)', ['x'])({ x: 7 }), 7);
});

test('anything that is not math is refused, not run', () => {
  const { MathExpr } = sandbox();
  for (const bad of ['alert(1)', 'constructor', 'constructor(1)', '__proto__', 'toString(x)', 'x.constructor', 'this', '"a"',
                     'process.exit()', 'x;y', '(x', 'x)'])
    assert.throws(() => MathExpr.compile(bad, ['x']), Error, bad);
});

test('a formula chart draws its curves and a slider for each parameter', () => {
  const { fnChart } = sandbox();
  const out = fnChart({ type: 'function', title: 'Waves', functions: [{ name: 'wave', expr: 'a*sin(b*x)' }],
    params: { a: [2, 0, 5], b: [1, 0.1, 4] }, x: [-6, 6] });
  assert.match(out, /class="chart fn-chart"/);
  assert.match(out, /<path class="fn-line s0" d="M[\d.]+,[\d.]+L/);
  assert.equal((out.match(/type="range"/g) || []).length, 2);
  assert.match(out, /data-k="a"/);
  const broken = fnChart({ type: 'function', functions: [{ expr: 'x +* 2' }, { expr: 'x' }] });
  assert.match(broken, /Couldn't draw/);
});

test('choices become buttons that carry the option', () => {
  const { choicesBlock } = sandbox();
  const out = choicesBlock('- Battery life\n- Camera\n2. Price\n- Camera');
  assert.equal((out.match(/class="choice"/g) || []).length, 3);
  assert.match(out, /data-v="Price"/);
  assert.equal(choicesBlock(''), null);
});

test('a widget runs sealed off: scripts only, no network, and waits while the reply streams', () => {
  const s = sandbox();
  const out = s.widgetBlock('<button>Next</button><script>fetch("https://x")</script>');
  assert.match(out, /sandbox="allow-scripts"/);
  assert.doesNotMatch(out, /allow-same-origin/);
  assert.match(out, /default-src &#39;none&#39;|default-src 'none'/);
  s.MD.live = true;
  assert.match(s.widgetBlock('<b>x</b>'), /widget-wait/);
});
