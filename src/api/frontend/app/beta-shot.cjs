
const http = require("http");
function get(path){return new Promise((res,rej)=>{http.get({host:"127.0.0.1",port:9222,path},r=>{let d="";r.on("data",c=>d+=c);r.on("end",()=>res(JSON.parse(d)))}).on("error",rej)})}
(async()=>{
  const tabs = await get("/json");
  let tab = tabs.find(t=>t.type==="page" && !t.url.startsWith("devtools"));
  if(!tab){ console.log("no page tab"); return }
  const WebSocket = (await import("ws")).default;
  const ws = new WebSocket(tab.webSocketDebuggerUrl,{maxPayload:512*1024*1024});
  let id=0; const pending={};
  function send(method,params={}){return new Promise(res=>{const i=++id;pending[i]=res;ws.send(JSON.stringify({id:i,method,params}))})}
  ws.on("message",m=>{const msg=JSON.parse(m);if(pending[msg.id])pending[msg.id](msg.result)});
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  ws.on("open",async()=>{
    await send("Page.navigate",{url:"http://localhost:3000/static-vue/beta-apply?platform=github&username=neko-test"});
    await sleep(2500);
    const r1=await send("Runtime.evaluate",{expression:`JSON.stringify({
      title: document.querySelector('.beta__logo h1')?.textContent,
      platform: document.getElementById('fAccountType') ? 'has-select' : document.querySelector('select')?.value,
      username: [...document.querySelectorAll('input')].map(i=>i.value).find(v=>v==='neko-test') || 'MISSING',
      cards: document.querySelectorAll('.beta__id-card').length,
      fields: document.querySelectorAll('.beta__field label').length,
      err: document.body.innerText.includes('__') || document.querySelector('#app')?.childElementCount
    })`});
    console.log("STATE:", r1.result.value);
    // click identity student -> branch appears
    await send("Runtime.evaluate",{expression:"document.querySelector('.beta__id-card[data-x]') ? 0 : document.querySelectorAll('.beta__id-card')[0].click()"});
    await sleep(300);
    const r2=await send("Runtime.evaluate",{expression:`JSON.stringify({
      studentBranch: !!document.querySelector('.beta__branch'),
      radios: document.querySelectorAll('.beta__radio-item').length,
      sessionPersist: sessionStorage.getItem('beta_apply_form') ? 'saved' : 'NOT-SAVED'
    })`});
    console.log("INTERACT:", r2.result.value);
    await send("Page.captureScreenshot",{format:"png"}).then(r=>require("fs").writeFileSync("/tmp/beta-light.png",Buffer.from(r.data,"base64")));
    await send("Runtime.evaluate",{expression:"document.documentElement.dataset.theme='dark'"});
    await sleep(400);
    await send("Page.captureScreenshot",{format:"png"}).then(r=>require("fs").writeFileSync("/tmp/beta-dark.png",Buffer.from(r.data,"base64")));
    console.log("theme:", (await send("Runtime.evaluate",{expression:"document.documentElement.dataset.theme"})).result.value);
    ws.close(); process.exit(0);
  });
})().catch(e=>{console.error(e.message);process.exit(1)})
