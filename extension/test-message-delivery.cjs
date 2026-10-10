"use strict";
const assert = require('node:assert/strict');
const {sendTurn} = require('./message-delivery.js');

async function scenario({unknown = false, disable = false} = {}) {
  const calls = [];
  const turn = {protocol_version:2,turn_id:'turn',room_id:'room',messages:[
    {message_id:'m1',content:'Tin một',content_hash:'h1',state:'sent'},
    {message_id:'m2',content:'Tin hai',content_hash:'h2',state:unknown?'unknown':'ready'},
    {message_id:'m3',content:'Tin ba',content_hash:'h3',state:'ready'}]};
  let cooling = true;
  const adapter = {
    active:async()=>!disable,
    wait:async()=>calls.push('wait'),
    control:async(operation,identity)=>{
      calls.push([operation,identity.message_id]);
      const message = turn.messages.find(m=>m.message_id===identity.message_id);
      if (operation==='status') return {ok:true,turn};
      if (operation==='claim') {
        if(cooling){cooling=false;return {ok:false,error:'delivery_cooldown',retry_after_s:.1};}
        return {ok:true,content:message.content,content_hash:message.content_hash,claim_token:'lease'};
      }
      return {ok:true};
    },
    send:async(message)=>{calls.push(['send',message.message_id]);return {ok:true,response_code:0,sent_at_ms:1000};},
    ack:async(identity)=>{turn.messages.find(m=>m.message_id===identity.message_id).state='sent';return {ok:true};},
    unknown:async(identity)=>calls.push(['unknown',identity.message_id])
  };
  if(unknown||disable) await assert.rejects(sendTurn(turn,adapter));
  else assert.deepEqual(await sendTurn(turn,adapter),{ok:true,sent:3});
  assert.deepEqual(calls.filter(c=>Array.isArray(c)&&c[0]==='send'),
    unknown||disable?[]:[['send','m2'],['send','m3']]);
  return {turn,adapter,calls};
}
(async()=>{
  await scenario();await scenario({unknown:true});await scenario({disable:true});
  const {turn,adapter,calls}=await scenario();
  turn.messages[1].state='ready';
  adapter.send=async()=>{throw new Error('timeout after dispatch');};
  await assert.rejects(sendTurn(turn,adapter),/timeout/);
  assert.ok(calls.some(c=>c[0]==='unknown'&&c[1]==='m2'));
  const failedAck = await scenario();
  failedAck.turn.messages[1].state='ready';
  failedAck.adapter.ack=async()=>({ok:false});
  await assert.rejects(sendTurn(failedAck.turn,failedAck.adapter),/receipt/);
  assert.equal(failedAck.calls.some(c=>Array.isArray(c)&&c[0]==='unknown'),false,
    'a successful platform receipt awaiting persistence must not be replaced with unknown');
  for (const verdict of ['revision_changed','revision_stale']) {
    const stopped = await scenario();
    stopped.turn.messages[1].state='ready'; stopped.turn.messages[1].content='Tin hai';
    stopped.calls.length=0;
    stopped.adapter.control=async(operation,identity)=>{
      stopped.calls.push([operation,identity.message_id]);
      if(operation==='status') return {ok:true,turn:stopped.turn};
      if(operation==='claim') return {ok:false,error:verdict,recompose:verdict==='revision_changed'};
      throw new Error('unexpected '+operation);
    };
    assert.deepEqual(await sendTurn(stopped.turn,stopped.adapter),{ok:false,stopped:verdict,sent:1});
    assert.deepEqual(stopped.calls.map(c=>c[0]),['status','status','claim'],'no dispatch, send, unknown or ack');
  }
  const fs=require('node:fs');
  assert.equal(fs.readFileSync(__dirname+'/message-delivery.js','utf8'),
    fs.readFileSync(__dirname+'/../extension-shopee/message-delivery.js','utf8'));
  console.log('MESSAGE_DELIVERY_FIFO_RECEIPT_TEST_OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
