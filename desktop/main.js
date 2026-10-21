// FX Workspace desktop app (Electron).
//
// The app itself is the Python server (launcher.pyw -> main:app on 127.0.0.1:8000) and its page. This shell
// starts it, shows the page in a real app window - Electron's own Chromium, no Edge or other browser - and
// closes it all again: the launcher runs with FX_SHELL=electron and watches this process (FX_PARENT_PID), so
// when the app quits the server and llama-server stop too.
//
// Works on Windows and Linux (the window, tray, single instance and server start are cross-platform; the
// Python side has Windows-only features - see docs/DESKTOP.md).
'use strict';

const { app, BrowserWindow, Tray, Menu, shell, session, dialog, nativeImage, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');

const APP_URL = 'http://127.0.0.1:8000';
const TITLE = 'FX Workspace';
const IS_WIN = process.platform === 'win32';
const START_TIMEOUT_MS = 180000;             // a first start syncs Python packages and starts llama-server
const TITLEBAR_H = 52;                       // the page's --topbar-h: the window buttons sit in its top bar

// '#1f1e1c', 'rgb(31, 30, 28)', 'rgba(...)' -> '#rrggbb' (the window buttons take plain colours), else null.
function hexColor(c) {
  c = String(c || '').trim();
  if (/^#[0-9a-f]{6}$/i.test(c)) return c;
  if (/^#[0-9a-f]{3}$/i.test(c)) return '#' + c.slice(1).split('').map(x => x + x).join('');
  const m = c.match(/^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)/i);
  return m ? '#' + [m[1], m[2], m[3]].map(n => Math.min(255, +n).toString(16).padStart(2, '0')).join('') : null;
}

let win = null;
let tray = null;
let server = null;                           // the launcher, when this app started it
let quitting = false;

// ---- where the app is --------------------------------------------------------------------------------
// FX_ROOT, else the first folder up from here (or the installed exe) that has launcher.pyw.
function findRoot() {
  const tried = new Set();
  const starts = [process.env.FX_ROOT, __dirname, path.dirname(process.execPath), process.resourcesPath].filter(Boolean);
  for (const s of starts) {
    let d = path.resolve(s);
    for (let i = 0; i < 6; i++) {
      if (tried.has(d)) break;
      tried.add(d);
      if (fs.existsSync(path.join(d, 'launcher.pyw'))) return d;
      const up = path.dirname(d);
      if (up === d) break;
      d = up;
    }
  }
  return null;
}

function pythonFor(root) {
  const candidates = IS_WIN
    ? [path.join(root, '.venv', 'Scripts', 'pythonw.exe'), path.join(root, '.venv', 'Scripts', 'python.exe')]
    : [path.join(root, '.venv', 'bin', 'python3'), path.join(root, '.venv', 'bin', 'python')];
  return candidates.find(p => fs.existsSync(p)) || null;
}

const asset = name => path.join(__dirname, 'assets', name);

// ---- the server ----------------------------------------------------------------------------------------
function ping() {
  return new Promise(resolve => {
    const req = http.get(APP_URL + '/api/settings', { timeout: 1500 }, res => { res.resume(); resolve(res.statusCode === 200); });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

function status(text) {
  if (win && !win.isDestroyed()) win.webContents.executeJavaScript(`window.setStatus && setStatus(${JSON.stringify(text)})`).catch(() => {});
}

async function startServer() {
  if (await ping()) return { ok: true };                      // already running (run.bat console, an earlier start)
  const root = findRoot();
  if (!root) return { ok: false, message: 'The FX Workspace files were not found next to the app.\n\nReinstall it, or set FX_ROOT to the folder with launcher.pyw.' };
  const python = pythonFor(root);
  if (!python) return { ok: false, message: `FX Workspace isn't set up yet: there is no Python environment in\n${path.join(root, '.venv')}\n\nRun the installer (or install.bat) first.` };
  status('Starting the server…');
  server = spawn(python, [path.join(root, 'launcher.pyw')], {
    cwd: root,
    env: { ...process.env, FX_SHELL: 'electron', FX_PARENT_PID: String(process.pid), PYTHONUTF8: '1' },
    // Detached: on Windows a child is otherwise in a job that is killed the moment Electron exits - the launcher
    // would die before it stops llama-server. Detached, it sees this process gone and shuts everything down.
    stdio: 'ignore', windowsHide: true, detached: true,
  });
  let exited = null;
  server.on('exit', code => { exited = code; });
  const started = Date.now();
  let hint = 0;
  while (Date.now() - started < START_TIMEOUT_MS) {
    if (await ping()) return { ok: true };
    if (exited !== null && !(await ping())) {
      return { ok: false, message: `The server stopped while starting (exit code ${exited}).\n\nDetails: ${path.join(root, 'data', 'logs', 'server.log')}` };
    }
    const secs = Math.round((Date.now() - started) / 1000);
    if (secs > 8 && hint === 0) { hint = 1; status('Checking Python packages and starting the local model server…'); }
    if (secs > 40 && hint === 1) { hint = 2; status('Still starting - the first start after an update takes longest…'); }
    await new Promise(r => setTimeout(r, 600));
  }
  return { ok: false, message: `The server didn't answer within ${START_TIMEOUT_MS / 1000} s.\n\nDetails: ${path.join(root, 'data', 'logs', 'server.log')}` };
}

function stopServer() {
  // Nothing to kill: the launcher stops by itself about a second after this process is gone (it watches
  // FX_PARENT_PID) - gracefully, so it stops llama-server too. Killing it here would skip that.
  if (server) server.unref();
}

// ---- the window ----------------------------------------------------------------------------------------
const stateFile = () => path.join(app.getPath('userData'), 'window.json');
function loadBounds() {
  try { return JSON.parse(fs.readFileSync(stateFile(), 'utf8')); } catch (e) { return {}; }
}
function saveBounds() {
  if (!win || win.isDestroyed()) return;
  try { fs.writeFileSync(stateFile(), JSON.stringify({ ...win.getNormalBounds(), maximized: win.isMaximized() })); } catch (e) { /* not important */ }
}

const isApp = url => url === APP_URL || url.startsWith(APP_URL + '/');

function createWindow() {
  const b = loadBounds();
  win = new BrowserWindow({
    title: TITLE, icon: asset(IS_WIN ? 'icon.ico' : 'icon.png'),
    width: b.width || 1440, height: b.height || 920, x: b.x, y: b.y, minWidth: 760, minHeight: 520,
    backgroundColor: '#1f1e1c', show: false, autoHideMenuBar: true,
    // No white system title bar: the app's own top bar is the title bar (it marks itself draggable), and the
    // window buttons are drawn over its right end in the theme's colours (the page sends them: fx-titlebar).
    titleBarStyle: 'hidden', titleBarOverlay: { color: '#1f1e1c', symbolColor: '#ece9e2', height: TITLEBAR_H },
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, spellcheck: true,
                      preload: path.join(__dirname, 'preload.js') },
  });
  if (b.maximized) win.maximize();
  win.once('ready-to-show', () => win.show());
  win.on('close', saveBounds);
  win.on('closed', () => { win = null; });

  // Links to other sites open in the user's own browser; the app's own pop-ups (an image, a preview) stay here.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isApp(url)) return { action: 'allow', overrideBrowserWindowOptions: { icon: asset(IS_WIN ? 'icon.ico' : 'icon.png'), autoHideMenuBar: true, backgroundColor: '#1f1e1c' } };
    if (/^https?:|^mailto:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (!isApp(url) && !url.startsWith('file:')) { e.preventDefault(); if (/^https?:/.test(url)) shell.openExternal(url); }
  });
  // The shortcuts a browser had: reload, zoom, full screen, developer tools.
  win.webContents.on('before-input-event', (e, input) => {
    if (input.type !== 'keyDown') return;
    const mod = input.control || input.meta;
    if (input.key === 'F5' || (mod && input.key.toLowerCase() === 'r')) { win.webContents.reload(); e.preventDefault(); }
    else if (input.key === 'F11') { win.setFullScreen(!win.isFullScreen()); e.preventDefault(); }
    else if (mod && input.shift && input.key.toLowerCase() === 'i') { win.webContents.toggleDevTools(); e.preventDefault(); }
    else if (mod && (input.key === '=' || input.key === '+')) { win.webContents.setZoomLevel(win.webContents.getZoomLevel() + 0.5); e.preventDefault(); }
    else if (mod && input.key === '-') { win.webContents.setZoomLevel(win.webContents.getZoomLevel() - 0.5); e.preventDefault(); }
    else if (mod && input.key === '0') { win.webContents.setZoomLevel(0); e.preventDefault(); }
  });
  win.loadFile(path.join(__dirname, 'splash.html'));
}

function createTray() {
  try {
    tray = new Tray(nativeImage.createFromPath(asset(IS_WIN ? 'icon.ico' : 'tray.png')));
  } catch (e) { return; }                    // some Linux desktops have no tray
  tray.setToolTip(TITLE);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show FX Workspace', click: showWindow },
    { type: 'separator' },
    { label: 'Quit', click: () => { quitting = true; app.quit(); } },
  ]));
  tray.on('click', showWindow);
}

function showWindow() {
  if (!win) { createWindow(); win.loadURL(APP_URL); return; }
  if (win.isMinimized()) win.restore();
  win.show(); win.focus();
}

// ---- permissions: the voice mode's microphone, notifications, the clipboard - for the app's own page only --
function permissions() {
  const allowed = new Set(['media', 'notifications', 'clipboard-read', 'clipboard-sanitized-write', 'fullscreen']);
  session.defaultSession.setPermissionRequestHandler((wc, permission, callback, details) => {
    callback(allowed.has(permission) && isApp(details.requestingUrl || wc.getURL()));
  });
  session.defaultSession.setPermissionCheckHandler((wc, permission, origin) => allowed.has(permission) && isApp(origin || ''));
}

// ---- start ---------------------------------------------------------------------------------------------
if (!app.requestSingleInstanceLock()) {
  app.quit();                                // one app: a second start shows the first one's window
} else {
  app.on('second-instance', showWindow);
  // After a native confirm()/alert() on Windows, Chromium keeps the caret but drops keystrokes until the
  // window loses and regains focus - the page asks for that round trip when one closes.
  ipcMain.on('fx-refocus', e => {
    const w = BrowserWindow.fromWebContents(e.sender);
    if (w && !w.isDestroyed()) { w.blur(); w.focus(); e.sender.focus(); }
  });
  ipcMain.on('fx-titlebar', (e, { color, symbolColor, height } = {}) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    const bg = hexColor(color), fg = hexColor(symbolColor);
    if (w && bg && fg && typeof w.setTitleBarOverlay === 'function') {
      try { w.setTitleBarOverlay({ color: bg, symbolColor: fg, height: Math.max(28, Math.min(64, Math.round(height) || TITLEBAR_H)) }); } catch (err) { /* not supported here */ }
    }
  });
  app.setAppUserModelId('com.fuxion.fxworkspace');
  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);
    permissions();
    createWindow();
    createTray();
    const r = await startServer();
    if (!r.ok) {
      dialog.showErrorBox(TITLE, r.message);
      quitting = true;
      app.quit();
      return;
    }
    if (win) win.loadURL(APP_URL);
  });
  // Closing the window closes the app (and with it the server and the model), as the browser window did.
  app.on('window-all-closed', () => { quitting = true; app.quit(); });
  app.on('before-quit', () => { quitting = true; saveBounds(); stopServer(); });
}
