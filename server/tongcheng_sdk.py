"""Bridge to the already authenticated page SDK; never extracts login tokens.

No second login, heartbeat, socket, or retry loop is created by this bridge.
The SDK is a private platform interface; DOM targeting and echo checks remain mandatory.
"""
ATTACH = r"""async () => {
 const urls=performance.getEntriesByType('resource').map(r=>r.name);
 const url=urls.find(u=>/^https:\/\/file\.40017\.cn\/vacation_feresource\/im\/pc\/assets\/im-[\w-]+\.js$/.test(u));
 if(!url)return {supported:false,reason:'sdk_resource_not_observed'};
 const mod=await import(url),sdk=mod.n;
 if(!sdk || typeof sdk.isOnline!=='function' || typeof sdk.sendMsg!=='function' || typeof sdk.on!=='function' || typeof mod.t!=='function')
   return {supported:false,reason:'sdk_contract_changed'};
 const store=mod.t();
 if(!window.__tcWorkbenchWire || window.__tcWorkbenchWire.sdk!==sdk){
  const bridge={sdk,store,events:[],overflow:0};
  sdk.on('received-message',packet=>{
   // Keep only chat protocol fields; never copy SDK user/login/configuration objects.
   if(packet.typeu!==3 || typeof packet.fp!=='string')return;
   let body;try{body=JSON.parse(packet.dataContent)}catch{return}
   if(!packet.from || !packet.to)return;
   if(bridge.events.length>=256){bridge.events.shift();bridge.overflow++}
   bridge.events.push({message_id:packet.fp,from:String(packet.from),to:String(packet.to),
     kind:body.ty===0?'text':'unsupported',text:body.ty===0&&typeof body.m==='string'?body.m:'',received_at:new Date().toISOString()});
  });
  window.__tcWorkbenchWire=bridge;
 }
 const anchors=new Set(Array.from(document.querySelectorAll('#imDialog .msgBox[data-index]'),e=>e.getAttribute('data-index')));
 const bound=Array.isArray(store.currentMessages)&&store.currentMessages.some(m=>anchors.has(m.fingerPrint));
 return {supported:true,online:!!sdk.isOnline(),current_customer_id:String(store.currentChatUserId||''),customer_bound:bound,
   queued:window.__tcWorkbenchWire.events.length,overflow:window.__tcWorkbenchWire.overflow,
   transport:'page_sdk_websocket',independent_socket:false};
}"""

DRAIN = """() => {
 const b=window.__tcWorkbenchWire;
 return b?{events:b.events.splice(0),overflow:b.overflow}:{events:[],overflow:0};
}"""

SEND = r"""({customer,text,fingerprint,tail}) => {
 const b=window.__tcWorkbenchWire;
 if(!b || !b.sdk.isOnline())throw Error('sdk_not_online');
 if(String(b.store.currentChatUserId)!==customer)throw Error('sdk_customer_changed');
 const anchors=new Set(Array.from(document.querySelectorAll('#imDialog .msgBox[data-index]'),e=>e.getAttribute('data-index')));
 if(!b.store.currentMessages.some(m=>anchors.has(m.fingerPrint)))throw Error('sdk_customer_not_bound');
 const rows=document.querySelectorAll('#imDialog .msgBox[data-index]');
 if(!rows.length||rows[rows.length-1].getAttribute('data-index')!==tail)throw Error('latest_message_changed');
 const title=document.querySelector('#im-dialog-header .nickname'),active=document.querySelector('#imDialog .user-item.active h3');
 if(!title||!active||title.innerText!==active.innerText)throw Error('customer_switch_in_progress');
 const editor=document.querySelector('#imDialog textarea');
 if(!editor || editor.value)throw Error('human_input_present');
 // Submit once. SDK acceptance is not a delivery receipt; caller verifies page echo.
 b.sdk.sendMsg({chatType:0,msgType:0,toId:customer,msgContent:text,fingerPrint:fingerprint,quoteMsgObj:{}});
 return {submitted:true,fingerprint};
}"""
