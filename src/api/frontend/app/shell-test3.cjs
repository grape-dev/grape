
const http = require("http");
function get(path){return new Promise((res,rej)=>{http.get({host:"127.0.0.1",port:9222,path},r=>{let d="";r.on("data",c=>d+=c);r.on("end",()=>res(JSON.parse(d)))}).on("error",rej)})}
(async()=>{
  const tabs=await get("/json");
  const tab=tabs.find(t=>t.type==="page"&&!t.url.startsWith("devtools"));
  const WebSocket=(await import("ws")).default;
  const ws=new WebSocket(tab.webSocketDebuggerUrl,{maxPayload:512*1024*1024});
  let id=0;const pending={};
  function send(method,params={}){return new Promise(res=>{const i=++id;pending[i]=res;ws.send(JSON.stringify({id:i,method,params}))})}
  ws.on("message",m=>{const msg=JSON.parse(m);if(pending[msg.id])pending[msg.id](msg.result)});
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  ws.on("open",async()=>{
    const TOKEN=process.env.TOK, USER=process.env.USERJ;
    await send("Runtime.enable");
    const errs=[];
    ws.on("event",(ev)=>{ if(ev.method==="Runtime.exceptionThrown") errs.push(ev.params.exceptionDetails.text||"exc"); });
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html"});
    await sleep(1500);
    await send("Runtime.evaluate",{expression:`localStorage.setItem('token', ${JSON.stringify(TOKEN)}); localStorage.setItem('user', ${USER}); 'ok'`});
    // list view
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html#my-feedback"});
    await sleep(3000);
    const r1=await send("Runtime.evaluate",{expression:`JSON.stringify({
      empty: !!document.querySelector('#pageContent .myfb__empty'),
      emptyText: document.querySelector('#pageContent .myfb__empty-text')?.textContent,
      loading: document.querySelector('#pageContent .myfb__hint')?.textContent,
      glass: getComputedStyle(document.querySelector('#pageContent .myfb__list')||document.querySelector('#pageContent .myfb__empty')).backdropFilter,
      errCount: ${"0"}
    })`});
    console.log("LIST:", r1.result.value, "| console errs:", JSON.stringify(errs));
    await send("Page.captureScreenshot",{format:"png"}).then(r=>require("fs").writeFileSync("/tmp/myfb-list-light.png",Buffer.from(r.data,"base64")));
    // detail 404 path
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html#my-feedback-detail/99999"});
    await sleep(3000);
    const r2=await send("Runtime.evaluate",{expression:`document.querySelector('#pageContent .mfd__hint')?.textContent || document.getElementById('pageContent').innerText.slice(0,120)`});
    console.log("DETAIL-404:", JSON.stringify(r2.result.value));
    // dark screenshot (list)
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html#my-feedback"});
    await sleep(2500);
    await send("Runtime.evaluate",{expression:`document.documentElement.setAttribute('data-theme','dark')`});
    await sleep(500);
    await send("Page.captureScreenshot",{format:"png"}).then(r=>require("fs").writeFileSync("/tmp/myfb-list-dark.png",Buffer.from(r.data,"base64")));
    console.log("final errs:", JSON.stringify(errs));
    ws.close();process.exit(0);
  });
})().catch(e=>{console.error(e.message);process.exit(1)})
