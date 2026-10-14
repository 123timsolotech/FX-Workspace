// Runs the real page's voice controller with controlled audio/network completion.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../../index.html'), 'utf8');
const voiceSource = html.slice(html.indexOf('const Voice = (() => {'), html.indexOf('// Settings > Appearance: show or hide one panel'));
const queueSource = html.slice(html.indexOf('function queueMessage('), html.indexOf('/* ---- Research, Critique and Compare replies'));
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise,resolve,reject}; };
const tick = () => new Promise(resolve => setImmediate(resolve));
test('app and operating-system motion settings resolve for full, reduced and off',()=>{
  const source=html.match(/^const motionStill = .*\r?\nconst motionOff = .*$/m)[0];
  const check=(mode,os)=>{
    const root={dataset:{motion:mode}},context={document:{documentElement:root},reducedMotion:{matches:os}};
    vm.runInNewContext(source+'\nglobalThis.result=[motionStill(),motionOff()];',context);
    return Array.from(context.result);
  };
  assert.deepEqual(check('full',false),[false,false]);
  assert.deepEqual(check('reduced',false),[true,false]);
  assert.deepEqual(check('off',false),[true,true]);
  assert.deepEqual(check('full',true),[true,false]);
});
function element() {
  const nodes = new Map(), classes = new Set();
  return {hidden:false, dataset:{}, style:{}, innerHTML:'', textContent:'', value:'',
    classList:{add:(...xs)=>xs.forEach(x=>classes.add(x)),remove:x=>classes.delete(x),toggle:(x,on)=>on?classes.add(x):classes.delete(x),contains:x=>classes.has(x)},
    setAttribute(){}, addEventListener(){}, focus(){}, contains(){return false;},
    querySelector(q){ if(!nodes.has(q)) nodes.set(q,element()); return nodes.get(q); }, querySelectorAll(){return [];}};
}
function harness() {
  const stage = element(), timers = new Map(), sent=[], requests=[], persisted=[]; let timer=0;
  const graph = () => ({connect(){},disconnect(){},gain:{value:1}});
  const ctx = {currentTime:2,sampleRate:16000,state:'running',destination:{},createAnalyser:graph,createGain:graph,createMediaStreamSource:graph,createScriptProcessor:graph,
    decodeAudioData:async()=>({duration:1}),createBufferSource(){return {connect(){},start(at){this.started=at;},stop(){this.stopped=true;}};}};
  const status = {hearing:true,speaking:true,speaker:'natural',packs:{kokoro:true},voices:{},langs:{},catalog:{natural_voices:{af_heart:{label:'Heart'},am_michael:{label:'Michael'}},voices:{},langs:{}}};
  const h = {stage,ctx,status,sent,requests,persisted,timers};
  const s = {S:{settings:{voice_natural:'af_heart',voice_engine:'natural',voice_conversation:false,voice_sounds:false},tools:[],busy:false},
    document:{documentElement:element(),addEventListener(){},activeElement:null,querySelector(){return null;}}, window:{},
    navigator:{mediaDevices:{getUserMedia:()=>Promise.reject(new Error('test microphone'))}},
    AudioContext:function(){return ctx;}, Uint8Array,Int16Array,Float32Array,ArrayBuffer,DataView,AbortController,Date,Math,performance:{now:()=>Date.now()},
    addEventListener(){},requestAnimationFrame(){return 0;},cancelAnimationFrame(){},reducedMotion:{matches:true},motionStill:()=>true,motionOff:()=>false,
    setTimeout(fn,ms){timers.set(++timer,{fn,ms});return timer;},clearTimeout(id){timers.delete(id);},
    promptEl:element(),autoGrow(){},updateSend(){},Drafts:{clear(){}},renderToolChips(){},toast(){},icon(){return '';},esc:x=>String(x),
    applyAppearance(){},store:{set:(key,value)=>persisted.push({key,value})},closePopover(){},isTemp(){return false;},
    fetch:async(url,options)=>{requests.push({url,options});return {ok:true,arrayBuffer:async()=>new ArrayBuffer(1)};},
    api:async(url,data)=>url==='/api/voice'?status:{settings:{...s.S.settings,...data}},
    sendPrompt:(text,opts)=>{sent.push({text,opts});s.Voice.beginTurn(text,opts);}, TOOL_NAMES:{}};
  const injection = `_review: {startMic, stopMic, endRecording, submit, settleReview, pauseBufferedSpeech, changeControl, flushControls, pump, caption,
    seed(v) { if ('stage' in v) stage=v.stage; if ('ctx' in v) {ctx=v.ctx;vol=ctx.createGain();outA=ctx.createAnalyser();} if ('st' in v) st=v.st; if ('dry' in v) dry=v.dry;
      if ('rec' in v) rec=v.rec; if ('queue' in v) queue=v.queue; if ('scheduled' in v) {scheduled=v.scheduled;playing=scheduled[0]||null;}
      if ('state' in v) state=v.state; if ('turnDone' in v) turnDone=v.turnDone; },
    snapshot: () => ({stream, micPending, queue, scheduled, fetching, turnLang, turnDone, speechPaused, state})},`;
  vm.createContext(s);
  vm.runInContext(voiceSource.replace('return { open, close, feed, finish, beginTurn, waiting, approval, setup:', `return { ${injection} open, close, feed, finish, beginTurn, waiting, approval, setup:`)+ '\nglobalThis.Voice=Voice;\n'+queueSource,s);
  h.s=s; h.V=s.Voice; h.v=s.Voice._review;
  h.v.seed({stage,ctx,st:status,dry:true,state:'idle'});
  return h;
}
test('closing while microphone permission is pending releases the late stream', async()=>{
  const h=harness(),d=deferred();let stops=0,calls=0;
  h.s.navigator.mediaDevices.getUserMedia=()=>{calls++;return d.promise;};
  const one=h.v.startMic(),two=h.v.startMic();
  await tick();
  assert.equal(calls,1); h.V.close();
  d.resolve({getTracks:()=>[{stop(){stops++;}}]});await Promise.all([one,two]);
  assert.equal(stops,1);assert.equal(h.v.snapshot().stream,null);assert.equal(h.V.state,'off');
});
test('microphone API failure clears the pending request so it can be retried',async()=>{
  const h=harness();h.s.navigator.mediaDevices=undefined;await h.v.startMic();
  assert.equal(h.v.snapshot().micPending,null);assert.equal(h.V.state,'error');
});
test('closing during AudioContext resume also releases the acquired stream',async()=>{
  const h=harness(),d=deferred();let stops=0;
  h.ctx.state='suspended';h.ctx.resume=()=>d.promise;
  h.s.navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[{stop(){stops++;}}]});
  const pending=h.v.startMic();await tick();h.V.close();d.resolve();await pending;
  assert.equal(stops,1);assert.equal(h.v.snapshot().stream,null);
});
test('late transcription cannot submit after close and reopen',async()=>{
  const h=harness(),d=deferred();
  h.v.seed({rec:{chunks:[new Float32Array(16000)]}});
  h.s.fetch=()=>d.promise;const pending=h.v.endRecording();h.V.close();h.stage.hidden=false;
  h.v.seed({state:'idle'});d.resolve({json:async()=>({ok:true,text:'Old words',lang:'en'})});await pending;
  assert.equal(h.sent.length,0);
});
test('a stale transcription cannot overwrite the next recording timings',async()=>{
  const h=harness(),old=deferred(),fresh=deferred();let calls=0;
  h.s.fetch=()=>++calls===1?old.promise:fresh.promise;
  h.v.seed({rec:{chunks:[new Float32Array(16000)]}});
  const first=h.v.endRecording();h.V.close();h.stage.hidden=false;
  h.v.seed({state:'idle',rec:{chunks:[new Float32Array(16000)]}});
  const second=h.v.endRecording();
  fresh.resolve({json:async()=>({ok:true,text:'New words',lang:'en',ms:222,review:{required:true,reasons:['review']}})});
  await second;
  old.resolve({json:async()=>({ok:true,text:'Old words',lang:'en',ms:999})});await first;
  assert.equal(h.V._test.timings.asrMs,222);
  assert.equal(h.V.state,'review');assert.equal(h.sent.length,0);
});

test('uncertain critical words wait for an explicit edited transcript',async()=>{
  const h=harness();h.v.seed({rec:{chunks:[new Float32Array(16000)],heard:'Track FL123 near Haifa'}});
  h.s.fetch=async()=>({json:async()=>({ok:true,text:'Track FL128 near Haifa',lang:'en',ms:320,
    review:{required:true,reasons:['number or identifier']}})});
  await h.v.endRecording();
  assert.equal(h.sent.length,0);assert.equal(h.V.state,'review');
  h.stage.querySelector('.vs-review textarea').value='Track FL123 near Haifa';
  h.v.settleReview('confirm');
  assert.equal(h.sent.length,1);assert.equal(h.sent[0].text,'Track FL123 near Haifa');
});
test('clean transcription sends immediately without a review step',async()=>{
  const h=harness();h.v.seed({rec:{chunks:[new Float32Array(16000)],heard:''}});
  h.s.fetch=async()=>({json:async()=>({ok:true,text:'Tell me a story',lang:'en',ms:220,
    review:{required:false,reasons:[]}})});
  await h.v.endRecording();
  assert.equal(h.sent[0].text,'Tell me a story');
});
test('queued voice retains its language and starts a fresh speaking turn when dispatched',()=>{
  const h=harness(); h.s.S.settings.voice_lang='auto';h.s.S.settings.voice_auto_langs=['he'];
  h.V.beginTurn('First',{voice:true,voiceLang:'en'});h.s.S.busy=true;h.v.submit('שלום','he');
  assert.equal(h.s.S.queued.voice,true);assert.equal(h.s.S.queued.voiceLang,'he');assert.equal(h.v.snapshot().turnLang,'en');
  h.V.finish({status:'success',message:'First answer'});h.s.S.busy=false;h.s.sendQueued('success');
  [...h.timers.values()].find(t=>t.ms===350).fn();
  assert.equal(h.sent[0].opts.voiceLang,'he');assert.equal(h.v.snapshot().turnDone,false);
  h.V.finish({status:'success',message:'תשובה'});
  assert.equal(h.v.snapshot().queue[0].lang,'he');assert.equal(h.v.snapshot().queue[0].text,'תשובה');
});
test('voice changes keep the active phrase and regenerate only unplayed speech',()=>{
  const h=harness(),active={_voiceAt:1,_voiceEnd:3,_voiceText:'First',_voiceLang:'en',stop(){this.stopped=true;}},future={_voiceAt:3,_voiceEnd:4,_voiceText:'Second',_voiceLang:'en',stop(){this.stopped=true;}};
  h.v.seed({scheduled:[active,future],queue:[{text:'Third',lang:'en',audio:{duration:1}}]});h.v.pauseBufferedSpeech();
  assert.equal(active.stopped,undefined);assert.equal(future.stopped,true);
  assert.equal(h.v.snapshot().scheduled.length,1);
  assert.deepEqual(Array.from(h.v.snapshot().queue,q=>[q.text,q.lang,!!q.audio]),[['Second','en',false],['Third','en',false]]);
});
test('an old decoded voice cannot be scheduled after a voice change',async()=>{
  const h=harness(),d=deferred();h.ctx.decodeAudioData=()=>d.promise;
  h.v.seed({dry:false,queue:[{text:'Hello',lang:'en'}]});const pending=h.v.pump();await tick();
  h.v.pauseBufferedSpeech();d.resolve({duration:1});await pending;
  assert.equal(h.v.snapshot().scheduled.length,0);assert.equal(h.v.snapshot().queue[0].audio,undefined);
});
test('rapid settings edits serialize saves and keep speech paused until all are saved',async()=>{
  const h=harness(),first=deferred(),writes=[];
  h.s.api=async(url,data)=>{if(url==='/api/voice')return h.status;writes.push(data);if(writes.length===1)await first.promise;return {settings:{...h.s.S.settings,...data}};};
  h.v.changeControl('voice_choice','natural:am_michael');const pending=h.v.flushControls();
  h.v.changeControl('voice_rate',1.2);h.v.flushControls();assert.equal(writes.length,1);assert.equal(h.v.snapshot().speechPaused,true);
  first.resolve();await pending;
  assert.equal(writes.length,2);assert.equal(h.s.S.settings.voice_natural,'am_michael');assert.equal(h.s.S.settings.voice_rate,1.2);
  assert.equal(h.v.snapshot().speechPaused,false);assert.ok(h.persisted.every(x=>x.key==='settings'));
});
test('failed voice setting saves resume existing speech and keep saved settings',async()=>{
  const h=harness();h.s.api=async()=>{throw Error('offline');};h.v.changeControl('voice_rate',1.4);await h.v.flushControls();
  assert.equal(h.s.S.settings.voice_rate,undefined);assert.equal(h.v.snapshot().speechPaused,false);
});
test('uninstalled voices cannot be chosen and transcript text is not interpreted as HTML',()=>{
  const h=harness();h.v.changeControl('voice_choice','fast:missing');assert.equal(h.timers.size,0);
  h.v.caption('you','<img src=x onerror=alert(1)>',true);
  const cap=h.stage.querySelector('.vs-cap.you');assert.equal(cap.innerHTML,'');assert.equal(cap.textContent,'<img src=x onerror=alert(1)>…');
  assert.equal(cap.dataset.partial,'true');
});

test('Russian and Hebrew approval words must be the entire utterance',()=>{
  const h=harness();
  for(const text of ['Подтверждаю','Да, разрешаю','מאשר']) assert.equal(h.V._test.command(text),'yes');
  for(const text of ['Нет','Отмена','בטל']) assert.equal(h.V._test.command(text),'no');
  for(const text of ['Я не подтверждаю','не разрешаю','אני לא מאשר','Да']) assert.equal(h.V._test.command(text),'');
});
test('the preview policy is in a trusted head before commented or malformed generated heads',()=>{
  const block=html.slice(html.indexOf('  const CSP =',html.indexOf('const DesignPanel')),html.indexOf('  function render() {',html.indexOf('const DesignPanel')));
  for(const malicious of ['<!-- <head> --> <img src="https://example.invalid/leak">','<script>/* <head> */</script>','<template><head></template>']) {
    const s={code:malicious,kind:'html',page:''};vm.createContext(s);vm.runInContext(block+'\nthis.result=documentFor();',s);
    assert.ok(s.result.startsWith('<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy"'));
    assert.ok(s.result.indexOf("default-src 'none'") < s.result.indexOf(malicious));
  }
});
