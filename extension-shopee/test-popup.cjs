const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
(async () => {
  const elements = new Map();
  const document = {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {textContent:'',className:'',addEventListener(){}});
    return elements.get(id);
  }};
  let stored={};
  const context = {document, setInterval(){}, chrome:{storage:{local:{get:async()=>stored},onChanged:{addListener(){}}}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'popup.js'),'utf8'), context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(elements.get('ai-status').textContent,/Đang tắt/);
  assert.equal(document.getElementById('status').className,'');
  assert.match(elements.get('runtime').textContent,/đang tắt/);
  stored={shopeeConfig:{aiEnabled:true,sessionId:'new'},shopeeAiStatus:{ok:true,sessionId:'new',at:Date.now(),label:'Đang nghe bình luận thật'},
    shopeeAiLastActivity:{sessionId:'new',at:Date.now(),ok:true,label:'Đã gửi #receipt1'}};
  await context.refreshStatus();
  assert.match(elements.get('ai-status').textContent,/Đang nghe bình luận thật/);
  assert.match(elements.get('ai-status').textContent,/Đã gửi #receipt1/);
  stored.shopeeAiLastActivity.sessionId='old';
  await context.refreshStatus();
  assert.doesNotMatch(elements.get('ai-status').textContent,/receipt1/);
  console.log('Shopee fresh popup passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
