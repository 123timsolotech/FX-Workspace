// Tells the page it runs in the desktop app (the page hides browser-only options like "Open a window"), and lets
// it colour the window's own buttons (minimise / maximise / close) to match its theme - the page is the title bar.
'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('fxDesktop', {
  shell: 'electron', platform: process.platform,
  setTitleBar: (color, symbolColor, height) => ipcRenderer.send('fx-titlebar', { color: String(color), symbolColor: String(symbolColor), height: Number(height) || 0 }),
  refocus: () => ipcRenderer.send('fx-refocus'),
  // The mini window's "open the app" button.
  showMain: () => ipcRenderer.send('fx-show-main'),
  // A reply is ready or needs approval: a Windows notification when the app isn't in front (clicking it opens chatId).
  notify: (title, body, chatId) => ipcRenderer.send('fx-notify', { title: String(title || ''), body: String(body || ''), chatId: String(chatId || '') }),
  onOpenChat: fn => ipcRenderer.on('fx-open-chat', (_e, chatId) => fn(chatId)),
});
window.addEventListener('DOMContentLoaded', () => document.documentElement.classList.add('fx-desktop'));
