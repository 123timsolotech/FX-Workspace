// Tells the page it runs in the desktop app (the page hides browser-only options like "Open a window"), and lets
// it colour the window's own buttons (minimise / maximise / close) to match its theme - the page is the title bar.
'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('fxDesktop', {
  shell: 'electron', platform: process.platform,
  setTitleBar: (color, symbolColor, height) => ipcRenderer.send('fx-titlebar', { color: String(color), symbolColor: String(symbolColor), height: Number(height) || 0 }),
  refocus: () => ipcRenderer.send('fx-refocus'),
});
window.addEventListener('DOMContentLoaded', () => document.documentElement.classList.add('fx-desktop'));
