"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const core = require('./core.js');
const pause = () => new Promise(resolve => setTimeout(resolve, 15));

async function invalidation(file, stage) {
  let valid = true, tick, cleared = 0, writes = 0, sends = 0;
  const invalid = () => { valid = false; throw Error('Extension context invalidated.'); };
  const runtime = {get id() { if (!valid || stage === 'getter') return invalid(); return 'test'; },
    sendMessage() { sends++; if(stage==='set') return Promise.resolve({ok:true,comments:[],controller:true,config:{},position:{},timelines:{},videos:{},products:[]}); return invalid(); }};
  const element = () => ({style:{},querySelector(){return this;},addEventListener(){},classList:{toggle(){}},setAttribute(){},remove(){},appendChild(x){this.firstChild=x;},attachShadow(){return this.shadowRoot=element();}});
  const context = {chrome: {runtime, storage: {local: {
    get() { if (stage === 'get') return invalid(); return Promise.resolve({shopeeConfig: {aiEnabled:true,sessionId:'123'}}); },
    set() { writes++; return invalid(); }
  }}}, document:{documentElement:element(),createElement:element,querySelectorAll:()=>[]}, location: {href:'https://live.shopee.vn/pc/live?session=123'},
    KidTEShopeeCore:core, setInterval(fn) {tick=fn;return 1;}, clearInterval() {cleared++;}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/live-widget.js','utf8'), context);
  vm.runInNewContext(fs.readFileSync(__dirname+'/'+file,'utf8'), context);
  await pause();
  await tick();
  assert.ok(cleared, file+' must stop after context invalidation');
  const before = sends;
  await tick();
  assert.equal(sends,before,'Stopped worker must not send more requests');
  assert.equal(writes,stage==='set'?1:0,'Cannot persist an error through the invalidated context');
}

async function stoppedDuringInference() {
  let tick, finish, delivered = 0;
  const config={aiEnabled:true,sessionId:'123',videoId:'v'};
  const context={chrome:{runtime:{id:'test', async sendMessage(m) {
    if(m.request?.action==='chat_poll') return {ok:true,comments:[{comment_id:'1',user_id:'u',nickname:'A',comment:'TC01?'}]};
    if(m.request?.action==='state') return {ok:true,available:true,shop_id:'s',product_id:'p'};
    if(m.request?.action==='catalog') return {ok:true,products:[{shop_id:'s',product_id:'p'}]};
    if(m.resource==='reply') return await new Promise(resolve=>{finish=resolve;});
    if(m.resource==='mapping') return {ok:true,videos:{v:{}}};
    if(m.resource==='position') return {ok:true,available:true,video_id:'v'};
    if(m.resource==='timelines') return {ok:true,timelines:{}};
    if(m.request?.action==='chat_send'||m.resource==='delivery') delivered++;
    return {ok:true};
  }},storage:{local:{async get(){return {shopeeConfig:config};},async set(){}}}},
    location:{href:'https://live.shopee.vn/pc/live?session=123'},
    KidTEShopeeCore:{liveSession:core.liveSession,desired:()=>({ok:true,action:'show',shop_id:'s',product_id:'p'})},
    setInterval(fn){tick=fn;return 1;},clearInterval(){}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/ai-controller.js','utf8'),context);
  await pause(); assert.ok(finish);
  context.KidTEShopeeAiStop();
  let replacementStopped=false;
  context.KidTEShopeeAiStop=()=>{replacementStopped=true;};
  finish({ok:true,should_send:true,turn_id:'t',reply:'reply'});
  await pause(); await tick();
  assert.equal(delivered,0,'Old inference must not deliver after stop');
  assert.equal(replacementStopped,false,'Old worker must not stop its replacement');
}

(async()=>{
  for(const file of ['ai-controller.js','controller.js'])
    for(const stage of ['getter','get','send','set']) await invalidation(file,stage);
  await stoppedDuringInference();
  console.log('Shopee invalidated context, synchronous API errors and stopped inference passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
