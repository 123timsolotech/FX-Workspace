"""Isolated settings preview: real CSS/controller, memory-only settings, no app services.

Run with the project's Python, then open the printed URL. ?theme=aurora&motion=off
selects a palette/motion setting. Never imports the live API or writes user data.
"""
from __future__ import annotations

import html
import json
import pathlib
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import system_messages as sm


def page(query):
    source = (ROOT / 'index.html').read_text(encoding='utf-8')
    css = source.split('<style>', 1)[1].split('</style>', 1)[0]
    modal = source[source.index('<div class="overlay hidden" id="settings"'):source.index('<!-- ================= Help')]
    modal = modal.replace('class="overlay hidden"', 'class="overlay"', 1)
    navigation = source[source.index('const SETTINGS_PAGES = ['):source.index('// What is on each page,')]
    helpers = '\n'.join(re.findall(r'^const (?:esc|rowHtml) = .*$', source, re.M))
    theme = query.get('theme', ['dark'])[0]
    tone = {'termdark': 'lite-dark', 'termlight': 'lite-light', 'eclipselight': 'light', 'auroralight': 'light', 'mossad': 'mossad'}.get(theme, '')
    skin = {'light': 'classic', 'dark': 'classic', 'termlight': 'terminal', 'termdark': 'terminal', 'eclipselight': 'eclipse',
            'auroralight': 'aurora', 'mossad': 'terminal'}.get(theme, theme)
    scheme = 'light' if theme in ('light', 'dawn', 'termlight', 'eclipselight', 'auroralight', 'judaism') else 'dark'
    motion = query.get('motion', ['full'])[0]
    if motion not in ('full', 'reduced', 'off'):
        motion = 'full'
    secret = ' data-secret="' + theme + '"' if theme in ('judaism', 'mossad') else ''
    stub_names = ('renderAppearance', 'renderModelsTab', 'renderProactive', 'renderPermissions', 'renderPrivacy',
                  'renderMCP', 'renderDiagnostics', 'renderNetwork', 'renderWebSearch', 'renderVoice', 'renderImages')
    stubs = '\n'.join(f'function {name}() {{}}' for name in stub_names)
    boot = r"""
const $ = id => document.getElementById(id);
const S = {settings: SETTINGS, settingsTab:'systemmessage', mode:'local'};
const store = {set(){}};
const TAB_ALIAS = {};
function applyAppearance() {}
function closeSettings() {}
function renderMemory() { $('settings-body').innerHTML='<h3>Memory</h3>'; }
function renderAgent() { $('settings-body').innerHTML='<h3>How it answers</h3><button class="btn" onclick="settingsTab(\'systemmessage\')">System message</button>'; }
async function api(url, data) {
  const response = await fetch(url, data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const body = await response.json();
  if(!response.ok) throw new Error(body.detail || 'Preview request failed');
  return body;
}
"""
    return (f'<!doctype html><html lang="en" data-look="studio" data-theme="{scheme}" data-scheme="{scheme}" '
            f'data-skin="{html.escape(skin, quote=True)}" data-tone="{html.escape(tone, quote=True)}" data-motion="{motion}"{secret}>'
            '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Isolated system message preview</title><style>' + css + '</style></head><body>' + modal
            + '<script>const SETTINGS=' + json.dumps(Preview.settings) + ';' + boot + helpers + stubs + navigation
            + '</script><script src="/system-messages.js"></script><script>settingsTab("systemmessage");</script></body></html>')


class Preview(BaseHTTPRequestHandler):
    settings = {**sm.DEFAULT_SETTINGS, 'response_mode': 'enhanced'}

    def reply(self, body, status=200, content_type='application/json'):
        data = body.encode('utf-8') if isinstance(body, str) else json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        request = urlparse(self.path)
        if request.path == '/':
            self.reply(page(parse_qs(request.query)), content_type='text/html')
        elif request.path == '/api/system-messages':
            self.reply(sm.catalog())
        elif request.path == '/system-messages.js':
            self.reply((ROOT / 'static/system-messages.js').read_text(encoding='utf-8'), content_type='text/javascript')
        else:
            self.reply({'detail': 'Not part of this isolated preview'}, 404)

    def do_POST(self):
        if self.path != '/api/settings':
            return self.reply({'detail': 'Not part of this isolated preview'}, 404)
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        try:
            sm.validate_updates(self.settings, body)
        except ValueError as e:
            return self.reply({'detail': str(e)}, 422)
        self.settings.update(sm.clean_settings({**self.settings, **body}))
        self.reply({'settings': self.settings})

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 0), Preview)
    print(f'PREVIEW http://127.0.0.1:{server.server_port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
