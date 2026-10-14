"""Isolated browser checks for the real context and effort controls.

All settings and fake task states live in page memory. No live API/model imports.
Use ?panel=context|effort|systemmessage, theme=dark, motion=full.
"""
from __future__ import annotations

import json
import re
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import system_messages_preview as base
import effort


def page(query):
    if query.get('panel', ['context'])[0] == 'systemmessage':
        return base.page(query)
    source = (base.ROOT / 'index.html').read_text(encoding='utf-8')
    head = base.page(query).split('</head>', 1)[0].replace('Isolated system message preview', 'Isolated context and effort preview')

    def block(start, end):
        return source[source.index(start):source.index(end, source.index(start))]

    helpers = '\n'.join(re.findall(r'^const esc = .*$', source, re.M))
    metadata = block('const EFFORT_INFO = {', 'S.effort = null;')
    levels = re.search(r'^const EFFORT_LEVELS = .*$', source, re.M).group()
    popover = block('let popEl = null;', '// The Setup button\'s label:')
    context = block('const fmtTok = n =>', 'function renderUsage(u)')
    controls = block('function sliderState(key) {', 'function renderAgent() {')
    profiles = [{'id': key, 'limits': value.limits()} for key, value in effort.PROFILES.items()]
    panel = query.get('panel', ['context'])[0]
    if panel not in ('context', 'effort'):
        panel = 'context'
    boot = r'''
const $ = id => document.getElementById(id), store = {get:(key, fallback)=>fallback, set(){}};
const S = {settings:{effort:'extra',cross_check:false,prompt_prep:'clarify',context_auto_compress:'95',max_revisions:'auto'},mode:'local',pinned:'Ornith-1.5-35B-A3B-Q4_K_M',busy:false,compressing:false};
const contextData = {used:34901,limit:56889,percent:61.35,fixed:8889,history:26012,window:65536,local:true,model:'Ornith-1.5-35B-A3B-Q4_K_M',history_limit:48000,effort_limit:48000,effort:'extra',reply_reserve:8647,auto_at:95,compressible:true};
let scenario='normal';
function icon(){return '';}
function motionStill(){return ['reduced','off'].includes(document.documentElement.dataset.motion);}
function toast(message){$('preview-status').textContent=message;}
function schedulePoll(){}
function fmtAgo(){return 'just now';}
function renderAgentControls(){}
function applyAppearance(){}
function openSettings(){location.href='?panel=systemmessage';}
function openEngineMenu(anchor){openPopover(anchor,'<div class="pop-desc">Models are not loaded in this isolated check.</div>','engine-pop');}
function openPersonaMenu(anchor){openPopover(anchor,'<div class="pop-desc">Personas are not loaded in this isolated check.</div>','persona-pop');}
async function refreshEffort(){
  const requested=S.effort?.requested||S.settings.effort, applied=requested==='auto'?'normal':requested;
  S.effort={...S.effort,requested,applied,local:true,source:S.effort?.source||'model',model:S.pinned,levels:PROFILES,unsupported:scenario==='small'?{extra:'This fixture model has an 8K window.',max:'This fixture model has an 8K window.',ultra:'This fixture model has no structured tool calls.'}:{}};
  if(S.effort.unsupported[applied]){S.effort.applied='high';S.effort.fallback=S.effort.unsupported[applied];}else S.effort.fallback=null;
  S.prep={...S.prep,mode:S.prep?.mode||S.settings.prompt_prep,source:S.prep?.source||'setting'};
  S.effort.cross_check={enabled:S.settings.cross_check,available:scenario==='cross',reason:scenario==='cross'?'Uses a different model to review the result.':'Unavailable locally: this backend runs one model at a time.'};
  syncAgentMenu();
}
async function saveSettings(update, target){
  Object.assign(S.settings,update);
  return {settings:S.settings};
}
async function api(url, data){
  if(url.startsWith('/api/context?'))return {status:S.context||{...contextData}};
  if(url==='/api/context/compress'){
    await new Promise(resolve=>setTimeout(resolve,1500));
    const status={...S.context,used:22000,history:12000,summary_tokens:1000,percent:38.67};
    return {ok:true,message:'Isolated check: compression finished.',status};
  }
  if(url==='/api/settings'){
    if(scenario==='failed')throw new Error('Isolated check: the settings file could not be saved.');
    return saveSettings(data);
  }
  if(url==='/api/effort/session'){
    S.effort.requested=data.level||S.settings.effort;S.effort.session=data.level;S.effort.source=data.level?'session':'model';
    return {status:'success',session_effort:S.effort.session};
  }
  if(url==='/api/prep/session'){
    S.prep.mode=data.mode||S.settings.prompt_prep;S.prep.session=data.mode;S.prep.source=data.mode?'session':'setting';
    return {status:'success',session_prep:S.prep.session};
  }
  throw new Error('Not part of the isolated check: '+url);
}
function setScenario(value){
  scenario=value;S.busy=value==='busy';S.compressing=value==='compressing';
  S.context={...contextData};
  if(value==='summary')Object.assign(S.context,{fixed:10889,summary_tokens:2000,used:36901,percent:64.86,last:{auto:true,turns:4,at:1,method:'model',before_percent:93,after_percent:65}});
  if(value==='unknown')Object.assign(S.context,{model:null,local:false,window:null,unknown_model:true,compressible:false});
  if(value==='long')S.context.model='Ornith-1.5-35B-A3B-Q4_K_M-with-a-very-long-model-name-that-must-wrap-without-moving-the-counts';
  S.project=value==='long'?{name:'A long project name that should wrap cleanly at narrow widths',effort:'high'}:null;
  if(S.effort)S.effort.source=value==='long'?'project':'model';
  renderContextMeter(S.context);refreshEffort();$('preview-status').textContent='';
}
'''
    body = r'''
<body><style>
.preview-main{max-width:840px;margin:36px auto;padding:0 24px}.preview-main h1{font-size:22px;margin:0 0 8px}.preview-main p{font-size:14px;color:var(--text-2)}
.preview-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:16px}.preview-controls label{font-size:13px}.preview-controls .input{max-width:220px}
.preview-composer{position:fixed;left:24px;bottom:24px;width:min(660px,calc(100vw - 48px));padding:12px;border:1px solid var(--border);border-radius:12px;background:var(--surface);display:flex;gap:8px;align-items:center}
@media(max-width:440px){.preview-main{padding:0 16px;margin:20px auto}.preview-composer{left:12px;bottom:12px;width:calc(100vw - 24px)}}
</style><main class="preview-main"><h1>Context &amp; effort</h1><p>Isolated UI check. These controls use the application's real markup and behavior, with simulated state kept in memory.</p>
<div class="preview-controls"><label for="scenario">State</label><select class="input" id="scenario" onchange="setScenario(this.value)"><option value="normal">Ready</option><option value="compressing">Compressing</option><option value="busy">Task running</option><option value="summary">Summary present</option><option value="unknown">No model loaded</option><option value="small">Unsupported effort levels</option><option value="long">Long names</option><option value="failed">Setting save fails</option><option value="cross">Second model available</option></select></div>
<p id="preview-status" role="status" aria-live="polite"></p><output id="drag-trace" hidden></output></main>
<div class="preview-composer"><button type="button" class="tool-btn ctx-btn" id="ctx-btn" data-level="ok" onclick="popEl?closePopover():openContextMenu(this)"><svg class="ctx-ring" viewBox="0 0 20 20" aria-hidden="true"><circle class="bg" cx="10" cy="10" r="7"/><circle class="fg" id="ctx-fg" cx="10" cy="10" r="7"/></svg><span id="ctx-label">Context</span></button><button type="button" class="tool-btn" id="setup-btn" onclick="popEl?closePopover():openSetup(this,'effort')">Model &amp; effort</button><button type="button" class="tool-btn" onclick="location.href='?panel=systemmessage'">System message</button></div>
'''
    start = 'setScenario("normal");' + ('openContextMenu($("ctx-btn"));' if panel == 'context' else 'openSetup($("setup-btn"),"effort");')
    trace = r'''
const dragTrace=[];
for(const type of ['pointerdown','input','pointerup','change'])document.addEventListener(type,event=>{
  const el=event.target;if(!el.classList?.contains('lvl'))return;
  dragTrace.push({type,trusted:event.isTrusted,key:el.dataset.key,value:Number(el.value),dragging:!!el._dragging,confirmed:S.settings[el.dataset.key]});
  if(dragTrace.length>80)dragTrace.shift();
  $('drag-trace').textContent=JSON.stringify(dragTrace);
});
'''
    return head + '</head>' + body + '<script>const PROFILES=' + json.dumps(profiles) + ';' + boot + helpers + levels + metadata + popover + context + controls + trace + start + '</script></body></html>'


class Preview(base.Preview):
    def do_GET(self):
        request = urlparse(self.path)
        if request.path == '/':
            self.reply(page(parse_qs(request.query)), content_type='text/html')
        else:
            super().do_GET()


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 0), Preview)
    print(f'PREVIEW http://127.0.0.1:{server.server_port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
