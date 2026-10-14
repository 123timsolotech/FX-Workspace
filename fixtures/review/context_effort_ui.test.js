// Execute the shipped controls with deterministic state/network/DOM events.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict'), {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../../index.html'), 'utf8');
const slice = (start,end) => html.slice(html.indexOf(start), html.indexOf(end,html.indexOf(start)));
const metadata = slice('const EFFORT_INFO = {', 'S.effort = null;');
const controls = slice('function sliderState(key) {', 'function syncAgentMenu() {');
const context = slice('const fmtTok = n =>', 'function renderUsage(u)');
const escape = x => String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const tick = () => new Promise(resolve=>setImmediate(resolve));
const deferred = () => {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

function element() {
  const classes = new Set(), attrs = {}, handlers = {};
  return {dataset:{},value:'0',innerHTML:'',textContent:'',disabled:false,hidden:false,title:'',checked:false,
    style:{values:{},setProperty(k,v){this.values[k]=v;}},
    classList:{add(...xs){xs.forEach(x=>classes.add(x));},remove(...xs){xs.forEach(x=>classes.delete(x));},contains:x=>classes.has(x),toggle(x,on){on?classes.add(x):classes.delete(x);}},
    setAttribute(k,v){attrs[k]=v;},getAttribute:k=>attrs[k]??null,
    addEventListener(k,fn){(handlers[k]||=[]).push(fn);},async emit(k,ev={}){for(const fn of handlers[k]||[])await fn(ev);},
    focus(){this.focused=true;},querySelector(){return null;},querySelectorAll(){return [];},contains(){return false;}};
}

function harness() {
  const nodes={},requests=[],saves=[],toasts=[];
  const names=['auto','low','normal','high','extra','max','ultra'];
  const S={settings:{effort:'normal',cross_check:false,prompt_prep:'clarify',context_auto_compress:'95',max_revisions:'auto'},mode:'local',pinned:'TestModel',busy:false,
    effort:{requested:'normal',applied:'normal',source:'model',model:'TestModel',local:true,unsupported:{},cross_check:{available:false,reason:'Unavailable with one local model.'},levels:[{id:'normal',limits:{steps_local:24,tool_calls:24,time_s:600,revisions:1}}]},
    prep:{mode:'clarify',source:'setting'}};
  const pop=element();pop.classList.add('agent-pop');
  pop.querySelector=q=>nodes[q.replace(/^#/,'')]||null;
  for(const key of ['effort','prompt_prep']){
    const wrap=element(),el=element(),value=element();el.dataset.key=key;el.closest=()=>wrap;
    const buttons=key==='effort'?[Object.assign(element(),{dataset:{level:'auto'}})]:[];
    const stops=(key==='effort'?names.slice(1):['off','clarify','expand','plan']).map(stop=>Object.assign(element(),{dataset:{stop}}));
    wrap.querySelector=q=>q==='.lv-value'?value:null;wrap.querySelectorAll=q=>q==='[data-stop]'?stops:buttons;
    nodes['lvl-'+key]=el;nodes['lvb-'+key]=wrap;nodes['lv-'+key]=value;nodes['ls-'+key]=element();nodes['ln-'+key]=element();nodes['lp-'+key]=element();wrap.buttons=buttons;wrap.stops=stops;
  }
  for(const id of ['lvl-session','lvl-trade','lvl-trade-heading','ctx-btn','ctx-fg','ctx-label','agent-cross-check','agent-cross-check-desc','settings-cross-check','settings-cross-check-desc'])nodes[id]=element();
  const h={nodes,S,pop,requests,saves,toasts};
  const s={S,popEl:pop,EFFORT_LEVELS:names,esc:escape,$:id=>nodes[id],fmtAgo:()=> 'just now',
    document:{activeElement:element()},placePopover(){},schedulePoll(){},toast:x=>toasts.push(x),
    store:{set(){}},applyAppearance(){},
    async api(url,data){
      requests.push({url,data});
      if(url==='/api/settings'){
        const {for_mode,for_model,...update}=data;
        saves.push({data:update,target:{for_mode,for_model}});
      }
      if(h.reply)return h.reply(url,data);
      if(url==='/api/effort/session')return {status:'success',session_effort:data.level};
      if(url==='/api/prep/session')return {status:'success',session_prep:data.mode};
      return {status:'success',settings:{...S.settings,...data}};
    },
    async refreshEffort(){s.syncSlider('effort');s.syncSlider('prompt_prep');s.syncCrossCheck();},
    syncAgentMenu(){s.syncSlider('effort');s.syncSlider('prompt_prep');s.syncCrossCheck();},renderAgentControls(){},
    openPopover(){},URLSearchParams,
  };
  vm.createContext(s);
  vm.runInContext(metadata+controls+context+'\nObject.assign(globalThis,{sliderHtml,sliderLevels,syncSlider,wireSlider,pickLevel,setSessionOnly,setCrossCheck,syncCrossCheck,crossCheckDescription,contextMenuBody,setAutoCompress,renderContextMeter});',s);
  h.s=s;
  for(const key of ['effort','prompt_prep']){s.wireSlider(nodes['lvl-'+key]);s.syncSlider(key);}
  return h;
}
function realRefreshHarness() {
  const h=harness(),pending=[];let renders=0,syncs=0;
  h.s.api=url=>{const reply=deferred();pending.push({url,reply});return reply.promise;};
  h.s.renderAgentControls=()=>{renders++;};
  h.s.syncAgentMenu=()=>{syncs++;};
  vm.runInContext(slice('async function refreshEffort() {','// The (i) dot'),h.s);
  return {h,pending,get renders(){return renders;},get syncs(){return syncs;}};
}
const c = {used:34901,limit:56889,percent:61.35,fixed:8889,history:26012,window:65536,local:true,model:'Ornith-1.5-35B-A3B-Q4_K_M',history_limit:48000,effort_limit:48000,effort:'extra',auto_at:95,compressible:true};

test('continuous sliders show one selected label, six effort stops, and no grid or dropdown',()=>{
  const {s}=harness(),markup=s.sliderHtml('effort');
  assert.match(markup,/type="range"[^>]+max="5"/);
  assert.match(markup,/step="any"/);assert.match(s.sliderHtml('prompt_prep'),/step="any"/);
  assert.match(markup,/aria-label="Auto effort"/);
  assert.equal((markup.match(/class="lv-stop"/g)||[]).length,6);
  assert.match(markup,/<span>Faster<\/span><span>More thorough<\/span>/);
  assert.doesNotMatch(markup,/canvas|lv-options|lv-option|listbox|<select/);
  assert.equal((s.sliderHtml('prompt_prep').match(/class="lv-stop"/g)||[]).length,4);
  assert.match(html,/<details class="lvl-details"><summary>More options<\/summary>/);
  for(const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(match[1]);
});
test('track clicks glide but grabbing or moving follows immediately; cancellation cannot rearm motion',async()=>{
  const {nodes}=harness(),el=nodes['lvl-effort'],wrap=nodes['lvb-effort'];
  const original=wrap.querySelector;
  wrap.querySelector=q=>q==='.lv-thumb'?{getBoundingClientRect:()=>({left:100,right:118})}:original(q);
  await el.emit('pointerdown',{clientX:250});
  assert.equal(wrap.classList.contains('is-dragging'),false);
  el.value='3.8';await el.emit('input');assert.equal(el.value,'3.8');
  await el.emit('pointermove');assert.equal(wrap.classList.contains('is-dragging'),true);
  await el.emit('pointercancel');await el.emit('pointermove');
  assert.equal(wrap.classList.contains('is-dragging'),false);
  await el.emit('pointerdown',{clientX:109});
  assert.equal(wrap.classList.contains('is-dragging'),true);
});

test('compression status has its own row and model names cannot collide with counts',()=>{
  const {s,S}=harness();S.compressing=true;
  const out=s.contextMenuBody({...c,model:'<script>bad</script>'});
  assert.match(out,/<dl class="ctx-lines">/);
  assert.match(out,/<dt>Model window<\/dt><dd>65,536<\/dd>/);
  assert.match(out,/<div class="ctx-model">Model <span>&lt;script&gt;bad/);
  assert.match(out,/<\/button>\s*<p class="ctx-note" id="ctx-status" role="status"/);
  assert.match(out,/Writing a summary of the older requests/);
  assert.match(out,/id="ctx-now"[^>]*disabled/);
});
test('empty contexts have finite meters; summaries and unloaded models remain readable',()=>{
  const {s}=harness();
  const empty=s.contextMenuBody({...c,limit:0,percent:NaN,fixed:0,history:0,unknown_model:true,local:false,model:null});
  assert.doesNotMatch(empty,/NaN|Infinity/);assert.match(empty,/Loads with your message/);
  const summary=s.contextMenuBody({...c,fixed:10889,summary_tokens:2000});
  assert.match(summary,/class="ctx-summary"/);assert.match(summary,/Earlier message summary<\/dt><dd>2,000/);
});
test('Auto and fixed fallback states keep truthful labels and capability hints',()=>{
  const {s,S,nodes}=harness();Object.assign(S.effort,{requested:'auto',applied:'normal'});s.syncSlider('effort');
  assert.match(nodes['lv-effort'].innerHTML,/>Auto</);assert.doesNotMatch(nodes['lv-effort'].innerHTML,/Runs as/);
  assert.equal(nodes['lvl-effort'].value,'1');assert.match(nodes['lvl-effort'].getAttribute('aria-valuetext'),/Auto selected/);
  Object.assign(S.effort,{requested:'max',applied:'normal',fallback:'Small window',unsupported:{max:'Small window'}});s.syncSlider('effort');
  assert.match(nodes['lv-effort'].innerHTML,/Runs as Balanced/);assert.match(nodes['ln-effort'].innerHTML,/Small window/);
  assert.equal(nodes['lvb-effort'].stops.find(b=>b.dataset.stop==='max').classList.contains('unavailable'),true);
});
test('keyboard steps skip unsupported levels, with Home and End selecting supported endpoints',async()=>{
  const {S,nodes,saves}=harness(),el=nodes['lvl-effort'];S.effort.unsupported={high:'Unavailable',ultra:'Unavailable'};
  await el.emit('keydown',{key:'ArrowRight',preventDefault(){}});await tick();assert.equal(S.effort.requested,'extra');
  await el.emit('keydown',{key:'End',preventDefault(){}});await tick();assert.equal(S.effort.requested,'max');
  await el.emit('keydown',{key:'Home',preventDefault(){}});await tick();assert.equal(S.effort.requested,'low');
  assert.equal(saves.length,3);
});
test('fractional dragging follows the pointer while labels preview a discrete level and polling preserves position',async()=>{
  const {s,nodes,saves,S}=harness(),el=nodes['lvl-effort'],wrap=nodes['lvb-effort'];
  await el.emit('pointerdown');el.value='2.34';await el.emit('input');s.syncSlider('effort');
  assert.equal(el.value,'2.34');assert.equal(Number(wrap.style.values['--pos']),2.34/5);
  assert.equal(el._preview,2);assert.match(nodes['lv-effort'].innerHTML,/>Thorough</);assert.equal(saves.length,0);
  el.value='2.61';await el.emit('input');s.syncSlider('effort');s.syncSlider('effort');
  assert.equal(el.value,'2.61');assert.equal(Number(wrap.style.values['--pos']),2.61/5);
  assert.equal(el._preview,3);assert.match(nodes['lv-effort'].innerHTML,/>Rigorous</);
  assert.match(el.getAttribute('aria-valuetext'),/^Rigorous/);assert.match(nodes['ln-effort'].innerHTML,/review/i);
  assert.equal(S.effort.requested,'normal');assert.equal(saves.length,0);
  await el.emit('pointerup');assert.equal(saves.length,0);assert.equal(el.value,'2.61');
  await el.emit('change');assert.equal(saves.length,1);assert.equal(S.effort.requested,'extra');assert.equal(el.value,'3');
});

test('fractional movement repaints immediately without rewriting details until its preview level changes',async()=>{
  for(const [key,positions,next,preview,name] of [
    ['effort',['2.10','2.18','2.34'],'2.62',2,'Thorough'],
    ['prompt_prep',['1.10','1.24','1.35'],'1.61',1,'Clarify'],
  ]){
    const {nodes,S,saves,requests}=harness(),el=nodes['lvl-'+key],wrap=nodes['lvb-'+key];
    const watchWrites=node=>{
      let content=node.innerHTML,writes=0;
      Object.defineProperty(node,'innerHTML',{configurable:true,get:()=>content,set:value=>{content=value;writes++;}});
      return ()=>writes;
    };
    const noteWrites=watchWrites(nodes['ln-'+key]),tradeWrites=watchWrites(nodes['lvl-trade']);
    await el.emit('pointerdown');el.value=positions[0];await el.emit('input');
    assert.equal(el._preview,preview,key);assert.match(nodes['lv-'+key].innerHTML,new RegExp('>'+name+'<'));
    const firstNote=nodes['ln-'+key].innerHTML,firstNoteWrites=noteWrites(),firstTradeWrites=tradeWrites();
    assert.equal(firstNoteWrites,1,key+' initial preview updates its description');
    if(key==='effort')assert.equal(firstTradeWrites,1,'initial effort preview refreshes its limits');
    for(const raw of positions.slice(1)){
      el.value=raw;await el.emit('input');
      assert.equal(el.value,raw,key+' native position');
      assert.equal(Number(wrap.style.values['--pos']),Number(raw)/(key==='effort'?5:3),key+' visual position');
      assert.equal(el._preview,preview,key);assert.equal(nodes['ln-'+key].innerHTML,firstNote,key);
      assert.equal(noteWrites(),firstNoteWrites,key+' description is not rebuilt for fractional motion');
      assert.equal(tradeWrites(),firstTradeWrites,key+' limits are not rebuilt for fractional motion');
    }
    el.value=next;await el.emit('input');
    assert.equal(el.value,next,key);assert.equal(el._preview,preview+1,key);
    assert.equal(noteWrites(),firstNoteWrites+1,key+' new preview updates its description');
    assert.notEqual(nodes['ln-'+key].innerHTML,firstNote,key+' new description is visible');
    if(key==='effort')assert.equal(tradeWrites(),firstTradeWrites+1,'new effort preview refreshes its limits');
    assert.equal(requests.length,0,key);assert.equal(saves.length,0,key);
    assert.equal(S.effort.requested,'normal');assert.equal(S.prep.mode,'clarify');
    await el.emit('pointercancel');
    assert.equal(el._preview,null,key);assert.equal(el.value,'1',key+' cancellation restores the confirmed level');
    await el.emit('change');assert.equal(requests.length,0,key+' cancelled release stays unsaved');
  }
});

test('same-level movement refreshes accessible availability after a capability poll without rebuilding details',async()=>{
  const {s,nodes,S,requests}=harness(),el=nodes['lvl-effort'],wrap=nodes['lvb-effort'];
  const watchWrites=node=>{
    let content=node.innerHTML,writes=0;
    Object.defineProperty(node,'innerHTML',{configurable:true,get:()=>content,set:value=>{content=value;writes++;}});
    return ()=>writes;
  };
  const noteWrites=watchWrites(nodes['ln-effort']),tradeWrites=watchWrites(nodes['lvl-trade']);
  await el.emit('pointerdown');el.value='2.61';await el.emit('input');
  assert.equal(el.getAttribute('aria-valuetext'),'Rigorous');
  S.effort.unsupported.extra='Capability changed';s.syncSlider('effort');
  assert.match(nodes['ln-effort'].innerHTML,/Rigorous isn't available/);
  const polledNoteWrites=noteWrites(),polledTradeWrites=tradeWrites();
  el.value='2.70';await el.emit('input');
  assert.equal(el.value,'2.7');assert.equal(Number(wrap.style.values['--pos']),2.7/5);
  assert.equal(el.getAttribute('aria-valuetext'),'Rigorous, not available');
  assert.equal(noteWrites(),polledNoteWrites,'availability input does not rebuild the polled description');
  assert.equal(tradeWrites(),polledTradeWrites,'availability input does not rebuild the polled limits');
  delete S.effort.unsupported.extra;s.syncSlider('effort');
  const availableNoteWrites=noteWrites(),availableTradeWrites=tradeWrites();
  el.value='2.76';await el.emit('input');
  assert.equal(el.getAttribute('aria-valuetext'),'Rigorous');
  assert.equal(noteWrites(),availableNoteWrites);assert.equal(tradeWrites(),availableTradeWrites);
  assert.equal(requests.length,0);assert.equal(S.effort.requested,'normal');
  await el.emit('pointercancel');await el.emit('change');assert.equal(requests.length,0);
});

test('fractional Prompt Prep movement remains unsaved until release and does not change effort',async()=>{
  const {s,nodes,saves,S}=harness(),el=nodes['lvl-prompt_prep'],wrap=nodes['lvb-prompt_prep'];
  await el.emit('pointerdown');el.value='1.72';await el.emit('input');s.syncSlider('prompt_prep');
  assert.equal(el.value,'1.72');assert.equal(Number(wrap.style.values['--pos']),1.72/3);
  assert.equal(el._preview,2);assert.match(nodes['lv-prompt_prep'].innerHTML,/>Expand</);assert.equal(saves.length,0);
  await el.emit('change');assert.equal(S.prep.mode,'expand');assert.equal(el.value,'2');
  assert.equal(saves.length,1);assert.equal(S.effort.requested,'normal');
});

test('release uses raw distance to available stops and resolves a half-step toward its preview',async()=>{
  for(const [raw,unsupported,expected,index] of [
    ['2.1',{high:'Small context'},'extra','3'],
    ['1.9',{high:'Small context'},'normal','1'],
    ['2.5',{},'extra','3'],
  ]){
    const {nodes,S,saves}=harness(),el=nodes['lvl-effort'];S.effort.unsupported=unsupported;
    await el.emit('pointerdown');el.value=raw;await el.emit('input');await el.emit('change');
    assert.equal(S.effort.requested,expected,raw);assert.equal(el.value,index,raw);
    assert.equal(saves.length,expected==='normal'?0:1,raw);
  }
});

test('keyboard navigation from a fractional position targets integer supported stops',async()=>{
  for(const [raw,key,unsupported,expected,index] of [
    ['2.34','ArrowRight',{},'extra','3'],
    ['2.34','ArrowLeft',{},'high','2'],
    ['2.34','ArrowLeft',{high:'Unavailable'},'normal','1'],
    ['3.72','ArrowRight',{max:'Unavailable'},'ultra','5'],
  ]){
    const {nodes,S}=harness(),el=nodes['lvl-effort'];S.effort.unsupported=unsupported;
    await el.emit('pointerdown');el.value=raw;await el.emit('input');
    let prevented=false;await el.emit('keydown',{key,preventDefault(){prevented=true;}});await tick();
    assert.equal(prevented,true);assert.equal(S.effort.requested,expected,key);assert.equal(el.value,index,key);
    assert.equal(el._preview,null);assert.equal(nodes['lvb-effort'].classList.contains('is-dragging'),false);
  }
});

test('a model or backend change cancels the old fractional drag before a poll, input or release can save it',async()=>{
  for(const event of ['poll','input','change'])for(const target of ['model','backend']){
    const {s,nodes,S,requests}=harness(),el=nodes['lvl-effort'],wrap=nodes['lvb-effort'];
    await el.emit('pointerdown');el.value='2.61';await el.emit('input');
    if(target==='model')S.pinned='AnotherModel';else S.mode='cloud';
    Object.assign(S.effort,{requested:'low',applied:'low',model:target==='model'?'AnotherModel':'CloudModel'});
    if(event==='poll')s.syncSlider('effort');else await el.emit(event);
    assert.equal(el.value,'0',`${target}/${event}`);assert.equal(el._preview,null);
    assert.equal(wrap.classList.contains('is-dragging'),false);assert.match(nodes['lv-effort'].innerHTML,/>Quick</);
    await el.emit('change');assert.equal(requests.length,0,`${target}/${event}`);assert.equal(S.effort.requested,'low');
  }
});

test('late native input after cancellation stays confirmed until a new pointer gesture starts',async()=>{
  for(const cause of ['pointercancel','escape','model','backend']){
    const {s,nodes,S,requests}=harness(),el=nodes['lvl-effort'],wrap=nodes['lvb-effort'];
    await el.emit('pointerdown');el.value='4.37';await el.emit('input');
    if(cause==='pointercancel')await el.emit('pointercancel');
    else if(cause==='escape')await el.emit('keydown',{key:'Escape',preventDefault(){},stopPropagation(){}});
    else {
      if(cause==='model')S.pinned='AnotherModel';else S.mode='cloud';
      Object.assign(S.effort,{requested:'low',applied:'low',model:'AnotherModel'});s.syncSlider('effort');
    }
    const confirmed=el.value,label=nodes['lv-effort'].innerHTML;
    el.value='4.83';await el.emit('input');s.syncSlider('effort');
    assert.equal(el.value,confirmed,cause);assert.equal(el._preview,null,cause);
    assert.equal(nodes['lv-effort'].innerHTML,label,cause);assert.equal(wrap.dataset.energy,'',cause);
    assert.equal(wrap.classList.contains('is-dragging'),false,cause);
    await el.emit('pointerup');await el.emit('change');assert.equal(requests.length,0,cause);
    await el.emit('pointerdown');el.value='2.61';await el.emit('input');
    assert.equal(el.value,'2.61',cause);assert.equal(el._preview,3,cause);
    await el.emit('pointerup');await el.emit('change');
    assert.equal(requests.length,1,cause);assert.equal(S.effort.requested,'extra',cause);
  }
});

test('real refresh ignores stale model and same-model results after the latest reply',async()=>{
  for(const switchModel of [false,true]){
    const r=realRefreshHarness(),{h,pending}=r,old=h.s.refreshEffort();
    if(switchModel)h.S.pinned='NewModel';
    const current=h.s.refreshEffort(),model=h.S.pinned;
    pending[2].reply.resolve({...h.S.effort,requested:'low',applied:'low',model});
    pending[3].reply.resolve({...h.S.prep,mode:'off'});await current;
    pending[0].reply.resolve({...h.S.effort,requested:'max',applied:'max',model:'TestModel'});
    pending[1].reply.resolve({...h.S.prep,mode:'plan'});await old;
    assert.equal(h.S.effort.model,model);assert.equal(h.S.effort.requested,'low');
    assert.equal(h.S.prep.mode,'off');assert.equal(h.S.effortError,null);
    assert.equal(r.renders,1);assert.equal(r.syncs,1);
  }
});

test('real refresh ignores stale errors without replacing the latest successful state',async()=>{
  const r=realRefreshHarness(),{h,pending}=r,old=h.s.refreshEffort();h.S.pinned='NewModel';
  const current=h.s.refreshEffort();
  pending[2].reply.resolve({...h.S.effort,requested:'low',applied:'low',model:'NewModel'});
  pending[3].reply.resolve({...h.S.prep});await current;
  pending[0].reply.reject(new Error('Old model request failed'));pending[1].reply.resolve({...h.S.prep});await old;
  assert.equal(h.S.effort.model,'NewModel');assert.equal(h.S.effort.requested,'low');
  assert.equal(h.S.effortError,null);assert.equal(r.renders,1);assert.equal(r.syncs,1);
});

test('real refresh never applies a previous owner even when the replacement request has not started',async()=>{
  for(const change of ['model','backend'])for(const fails of [false,true]){
    const r=realRefreshHarness(),{h,pending}=r,old=h.s.refreshEffort();
    const effort=h.S.effort,prep=h.S.prep;h.S.effortError='Current status';
    if(change==='model')h.S.pinned='NewModel';else h.S.mode='cloud';
    if(fails)pending[0].reply.reject(new Error('Old owner failed'));
    else pending[0].reply.resolve({...effort,requested:'max',applied:'max'});
    pending[1].reply.resolve({...prep,mode:'plan'});await old;
    assert.equal(h.S.effort,effort,change);assert.equal(h.S.prep,prep,change);
    assert.equal(h.S.effortError,'Current status',change);assert.equal(r.renders,0);assert.equal(r.syncs,0);
  }
});

test('Workflow feedback only activates for a supported fixed choice and stops on fallback or Auto',async()=>{
  const h=harness(),wrap=h.nodes['lvb-effort'];await h.s.pickLevel('effort','ultra');
  assert.equal(wrap.dataset.workflow,'true');assert.equal(h.nodes['lp-effort'].textContent,'Multi-step projects');
  assert.equal(wrap.dataset.energy,'ultra');
  Object.assign(h.S.effort,{requested:'ultra',applied:'high',fallback:'Small window',unsupported:{ultra:'Small window'}});h.s.syncSlider('effort');
  assert.equal(wrap.dataset.workflow,'false');assert.match(h.nodes['lv-effort'].innerHTML,/Runs as Thorough/);
  assert.equal(wrap.dataset.energy,'');
  Object.assign(h.S.effort,{requested:'auto',applied:'ultra',fallback:null,unsupported:{}});h.s.syncSlider('effort');
  assert.equal(wrap.dataset.workflow,'false');assert.equal(wrap.classList.contains('is-auto'),true);
  assert.equal(wrap.dataset.energy,'');
});

test('Max energy follows the supported fixed choice or preview and leaves Cross-check independent',async()=>{
  const h=harness(),wrap=h.nodes['lvb-effort'],el=h.nodes['lvl-effort'];await h.s.pickLevel('effort','max');
  assert.equal(wrap.dataset.energy,'max');assert.equal(h.S.settings.cross_check,false);
  Object.assign(h.S.effort,{requested:'max',applied:'normal',fallback:'Small window',unsupported:{max:'Small window'}});h.s.syncSlider('effort');
  assert.equal(wrap.dataset.energy,'');
  Object.assign(h.S.effort,{requested:'auto',applied:'max',fallback:null,unsupported:{}});h.s.syncSlider('effort');
  assert.equal(wrap.dataset.energy,'');
  await el.emit('pointerdown');el.value='4';await el.emit('input');assert.equal(wrap.dataset.energy,'max');
  await el.emit('pointercancel');assert.equal(wrap.dataset.energy,'');assert.equal(h.S.effort.requested,'auto');
  assert.equal(h.S.settings.cross_check,false);
});

test('polling preserves fractional preview position and cancellation clears the drag treatment',async()=>{
  const h=harness(),wrap=h.nodes['lvb-effort'],el=h.nodes['lvl-effort'];await h.s.pickLevel('effort','high');
  await el.emit('pointerdown');el.value='4.73';await el.emit('input');h.s.syncSlider('effort');h.s.syncSlider('effort');
  assert.equal(el.value,'4.73');assert.equal(Number(wrap.style.values['--pos']),4.73/5);
  assert.equal(wrap.classList.contains('is-dragging'),true);assert.equal(wrap.dataset.workflow,'true');
  await el.emit('pointercancel');assert.equal(wrap.classList.contains('is-dragging'),false);
  assert.equal(wrap.dataset.workflow,'false');assert.equal(wrap.dataset.level,'high');assert.equal(el.value,'2');
});

test('failed highest-level saves restore the actual level and stop its effect',async()=>{
  const h=harness(),wrap=h.nodes['lvb-effort'];h.reply=async()=>{throw new Error('Disk unavailable');};
  h.nodes['lvl-effort'].value='5';await h.nodes['lvl-effort'].emit('input');
  assert.equal(wrap.dataset.workflow,'true');await h.nodes['lvl-effort'].emit('change');
  assert.equal(wrap.dataset.workflow,'false');assert.equal(wrap.dataset.level,'normal');
  assert.equal(h.S.effort.requested,'normal');assert.equal(h.S.settings.cross_check,false);
});
test('cancelled dragging restores the confirmed level and cannot save a late change',async()=>{
  const {nodes,saves}=harness(),el=nodes['lvl-effort'];await el.emit('pointerdown');el.value='4.37';await el.emit('input');
  await el.emit('pointercancel');assert.equal(el.value,'1');await el.emit('change');assert.equal(saves.length,0);
});
test('releasing on an unsupported level chooses a supported neighbor and explains it',async()=>{
  const {nodes,S}=harness(),el=nodes['lvl-effort'];S.effort.unsupported={extra:'Small context',max:'Small context',ultra:'No tools'};
  el.value='5';await el.emit('input');await el.emit('change');assert.equal(S.effort.requested,'high');assert.equal(el.value,'2');
  assert.match(nodes['ln-effort'].innerHTML,/Ultra.*isn't available.*No tools/);
});
test('direct choices preserve model-specific persistence and reject unknown or unsupported values',async()=>{
  const {S,s,saves,requests}=harness();await s.pickLevel('effort','max');
  assert.deepEqual(JSON.parse(JSON.stringify(saves[0])),{data:{effort:'max'},target:{for_mode:'local',for_model:'TestModel'}});
  S.effort.unsupported={ultra:'No tools'};await s.pickLevel('effort','ultra');await s.pickLevel('effort','invented');
  assert.equal(saves.length,1);assert.equal(requests.length,1);
});
test('project and session overrides still use session APIs; Prompt Prep stays independent',async()=>{
  const {s,S,nodes,requests,saves}=harness();S.effort.source='project';nodes['lvl-session'].checked=false;
  await s.pickLevel('effort','high');assert.equal(requests[0].url,'/api/effort/session');assert.equal(saves.length,0);
  nodes['lvl-session'].checked=true;await s.pickLevel('prompt_prep','plan');assert.equal(requests[1].url,'/api/prep/session');
  assert.equal(S.effort.requested,'high');assert.equal(S.prep.mode,'plan');
  await s.setSessionOnly(false);assert.equal(requests.at(-2).data.level,null);assert.equal(requests.at(-1).data.mode,null);
});

test('failed and unconfirmed effort/prep saves restore confirmed thumb, labels, and settings',async()=>{
  const h=harness();h.reply=async()=>{throw new Error('Disk unavailable');};
  h.nodes['lvl-effort'].value='4';await h.nodes['lvl-effort'].emit('change');
  assert.equal(h.S.effort.requested,'normal');assert.equal(h.S.settings.effort,'normal');
  assert.equal(h.nodes['lvl-effort'].value,'1');assert.match(h.toasts.at(-1),/Disk unavailable/);
  h.reply=async()=>({ok:true});await h.s.pickLevel('prompt_prep','plan');
  assert.equal(h.S.prep.mode,'clarify');assert.equal(h.S.settings.prompt_prep,'clarify');
  assert.match(h.toasts.at(-1),/not confirmed/);assert.equal(h.nodes['lvl-prompt_prep'].disabled,false);
  h.nodes['lvl-session'].checked=true;await h.s.pickLevel('effort','max');
  assert.equal(h.S.effort.requested,'normal');assert.match(h.toasts.at(-1),/not confirmed/);
});

test('the slider stays live while saving; the newest choice is sent next',async()=>{
  const h=harness(),pending=deferred();h.reply=()=>pending.promise;
  const save=h.s.pickLevel('effort','max');await h.s.pickLevel('effort','extra');await h.s.pickLevel('effort','high');
  assert.equal(h.requests.length,1);assert.equal(h.S.effort.requested,'normal');
  assert.equal(h.nodes['lvl-effort'].disabled,false);assert.equal(h.nodes['lvl-effort'].getAttribute('aria-busy'),'true');
  assert.equal(h.nodes['lvl-effort'].value,'2');
  h.reply=async (url,body)=>({settings:{...h.S.settings,effort:body.effort}});
  pending.resolve({settings:{...h.S.settings,effort:'max'}});await save;await tick();
  assert.equal(h.requests.filter(r=>r.url==='/api/settings').length,2);assert.equal(h.S.effort.requested,'high');assert.equal(h.nodes['lvl-effort'].value,'2');
});

test('a pending save holds the target visual and description without changing the active settings',async()=>{
  const h=harness(),pending=deferred(),el=h.nodes['lvl-effort'],wrap=h.nodes['lvb-effort'];h.reply=()=>pending.promise;
  const save=h.s.pickLevel('effort','max');
  assert.equal(el.value,'4');assert.equal(wrap.dataset.level,'max');assert.equal(wrap.dataset.energy,'max');
  assert.match(h.nodes['lv-effort'].innerHTML,/>Max</);assert.doesNotMatch(h.nodes['lv-effort'].innerHTML,/Saving/);
  assert.match(h.nodes['ln-effort'].innerHTML,/up to three revisions/);
  assert.equal(el.getAttribute('aria-valuetext'),'Saving Max. Active level: Balanced.');
  assert.equal(h.S.effort.requested,'normal');assert.equal(h.S.settings.effort,'normal');
  assert.equal(el.disabled,false);assert.equal(el.getAttribute('aria-busy'),'true');
  h.s.syncSlider('effort');h.s.syncSlider('effort');
  assert.equal(el.value,'4');assert.equal(Number(wrap.style.values['--pos']),4/5);
  pending.resolve({settings:{...h.S.settings,effort:'max'}});await save;
  assert.equal(h.S.effort.requested,'max');assert.equal(h.S.settings.effort,'max');assert.equal(el.value,'4');
  assert.doesNotMatch(h.nodes['lv-effort'].innerHTML,/Saving/);assert.ok(!h.S.levelPending?.effort);
});

test('a rejected pending save restores the confirmed rail, description and accessible value',async()=>{
  const h=harness(),pending=deferred(),el=h.nodes['lvl-effort'],wrap=h.nodes['lvb-effort'];h.reply=()=>pending.promise;
  const save=h.s.pickLevel('effort','max');assert.equal(el.value,'4');
  pending.reject(new Error('Disk unavailable'));await save;
  assert.equal(h.S.effort.requested,'normal');assert.equal(h.S.settings.effort,'normal');assert.equal(el.value,'1');
  assert.equal(wrap.dataset.level,'normal');assert.equal(wrap.dataset.energy,'');assert.ok(!h.S.levelPending?.effort);
  assert.match(h.nodes['ln-effort'].innerHTML,/Everyday default/);assert.doesNotMatch(el.getAttribute('aria-valuetext'),/Saving/);
  assert.doesNotMatch(h.nodes['lv-effort'].innerHTML,/Saving/);assert.equal(el.disabled,false);
  assert.match(h.toasts.at(-1),/Disk unavailable/);
});

test('pending feedback names the applied effort when the original choice has fallen back',async()=>{
  const h=harness(),pending=deferred();h.reply=()=>pending.promise;
  Object.assign(h.S.effort,{requested:'ultra',applied:'high',fallback:'Small window',unsupported:{ultra:'Small window'}});
  const save=h.s.pickLevel('effort','normal');
  assert.match(h.nodes['lvl-effort'].getAttribute('aria-valuetext'),/Saving Balanced\. Active level: Thorough/);
  assert.equal(h.S.effort.requested,'ultra');assert.equal(h.S.effort.applied,'high');
  pending.reject(new Error('Disk unavailable'));await save;
  assert.match(h.nodes['lv-effort'].innerHTML,/Runs as Thorough/);
});

test('a confirmed reply for a previous model cannot relabel the newly selected model',async()=>{
  const h=harness(),pending=deferred();h.reply=()=>pending.promise;
  const save=h.s.pickLevel('effort','max');h.S.pinned='AnotherModel';
  h.S.effort={...h.S.effort,requested:'low',applied:'low',model:'AnotherModel'};
  h.s.syncSlider('effort');assert.equal(h.nodes['lvl-effort'].value,'0');
  assert.match(h.nodes['lv-effort'].innerHTML,/>Quick</);assert.doesNotMatch(h.nodes['lv-effort'].innerHTML,/Saving/);
  assert.equal(h.nodes['lvb-effort'].dataset.energy,'');
  pending.resolve({settings:{...h.S.settings,effort:'max'}});await save;
  assert.equal(h.S.effort.requested,'low');assert.equal(h.nodes['lvl-effort'].value,'0');
  assert.equal(h.saves[0].target.for_model,'TestModel');
});

test('Escape cancels a slider preview without saving a late change event',async()=>{
  const h=harness(),el=h.nodes['lvl-effort'];await el.emit('pointerdown');el.value='4.37';await el.emit('input');
  let stopped=false;await el.emit('keydown',{key:'Escape',preventDefault(){},stopPropagation(){stopped=true;}});
  assert.equal(stopped,true);assert.equal(el.value,'1');await el.emit('change');assert.equal(h.requests.length,0);
});

test('Auto remains available when no fixed effort is supported',async()=>{
  const h=harness();h.S.effort.unsupported=Object.fromEntries(['low','normal','high','extra','max','ultra'].map(x=>[x,'Unavailable']));
  h.s.syncSlider('effort');assert.equal(h.nodes['lvl-effort'].disabled,true);
  assert.equal(h.nodes['lvb-effort'].buttons[0].disabled,false);await h.nodes['lvb-effort'].buttons[0].emit('click');await tick();
  assert.equal(h.S.effort.requested,'auto');assert.equal(h.nodes['lvb-effort'].classList.contains('is-auto'),true);
});

test('Cross-check is an independent confirmed toggle and effort changes cannot turn it on',async()=>{
  const h=harness();await h.s.pickLevel('effort','max');
  assert.equal(h.S.settings.cross_check,false);assert.equal(h.nodes['agent-cross-check'].getAttribute('aria-checked'),'false');
  await h.s.setCrossCheck(true);assert.equal(h.S.settings.cross_check,true);assert.equal(h.S.effort.requested,'max');
  assert.deepEqual(JSON.parse(JSON.stringify(h.requests.at(-1))),{url:'/api/settings',data:{cross_check:true}});
  assert.equal(h.nodes['settings-cross-check'].getAttribute('aria-checked'),'true');
  assert.match(h.nodes['agent-cross-check-desc'].textContent,/Unavailable/);
  await h.s.pickLevel('effort','low');assert.equal(h.S.settings.cross_check,true);
});

test('failed, unconfirmed and duplicate Cross-check saves preserve the prior selection',async()=>{
  const h=harness();h.reply=async()=>{throw new Error('Disk unavailable');};await h.s.setCrossCheck(true);
  assert.equal(h.S.settings.cross_check,false);assert.equal(h.nodes['agent-cross-check'].disabled,false);
  h.reply=async()=>({ok:true});await h.s.setCrossCheck(true);assert.equal(h.S.settings.cross_check,false);
  const pending=deferred();h.reply=()=>pending.promise;
  const save=h.s.setCrossCheck(true);await h.s.setCrossCheck(true);assert.equal(h.requests.length,3);
  assert.equal(h.nodes['agent-cross-check'].disabled,true);
  pending.resolve({settings:{...h.S.settings,cross_check:true}});await save;
  assert.equal(h.S.settings.cross_check,true);assert.equal(h.nodes['agent-cross-check'].disabled,false);
});

test('Cross-check explains Raw mode and stays disabled against an older backend',()=>{
  const h=harness();h.S.settings.response_mode='raw';h.s.syncCrossCheck();
  assert.match(h.nodes['settings-cross-check-desc'].textContent,/Raw replies skip review/);
  delete h.S.effort.cross_check;h.s.syncCrossCheck();assert.equal(h.nodes['agent-cross-check'].disabled,true);
});
test('failed and unconfirmed threshold saves leave the previous selection active',async()=>{
  const h=harness();h.S.context={...c};h.reply=async()=>{throw new Error('Disk unavailable');};
  await h.s.setAutoCompress('80');assert.equal(h.S.context.auto_at,95);assert.equal(h.S.settings.context_auto_compress,'95');assert.equal(h.S.contextAutoSaving,false);
  h.reply=async()=>({ok:true});await h.s.setAutoCompress('90');assert.equal(h.S.context.auto_at,95);assert.match(h.toasts.at(-1),/not confirmed/);
});
test('threshold saves wait for confirmation, reject duplicates, and accept Off',async()=>{
  const h=harness(),pending=deferred();h.S.context={...c};h.reply=()=>pending.promise;
  const save=h.s.setAutoCompress('off');await h.s.setAutoCompress('80');assert.equal(h.requests.length,1);assert.equal(h.S.context.auto_at,95);
  pending.resolve({settings:{...h.S.settings,context_auto_compress:'off'}});await save;assert.equal(h.S.context.auto_at,null);
  await h.s.setAutoCompress('invalid');assert.equal(h.requests.length,1);
});
test('polling an open context panel preserves focus on its threshold control',()=>{
  const h=harness(),focused=element(),next=element(),body=element();focused.dataset.compressAt='95';
  h.pop.classList.remove('agent-pop');h.pop.classList.add('ctx-pop');h.pop.contains=el=>el===focused;
  h.pop.querySelector=q=>q==='.ctx-body'?body:q==='[data-compress-at="95"]'?next:null;
  h.s.document.activeElement=focused;h.s.renderContextMeter(c);assert.equal(next.focused,true);assert.match(body.innerHTML,/Tokens in use/);
});
