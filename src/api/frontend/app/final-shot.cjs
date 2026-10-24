
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
    // ensure dark OS + reload with ls already seeded (persisted from previous runs)
    await send("Emulation.setEmulatedMedia",{features:[{name:"prefers-color-scheme", value:"dark"}]});
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html#my-feedback"});
    await sleep(5000);
    const r=await send("Runtime.evaluate",{expression:`JSON.stringify({
      theme: document.documentElement.getAttribute('data-theme'),
      mode: document.documentElement.getAttribute('data-theme-mode'),
      bg: getComputedStyle(document.body).backgroundColor,
      vuePage: !!document.querySelector('#pageContent .myfb'),
      empty: !!document.querySelector('#pageContent .myfb__empty')
    })`});
    console.log("STATE:", r.result.value);
    await send("Page.captureScreenshot",{format:"png"}).then(r=>require("fs").writeFileSync("/tmp/myfb-dark-final.png",Buffer.from(r.data,"base64")));
    // light version
    await send("Emulation.setEmulatedMedia",{features:[{name:"prefers-color-scheme", value:"light"}]});
    await sleep(600);
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html#my-feedback"});
    await sleep(4000);
    await send("Page.captureScreenshot",{format:"png"}).then(r=>require("fs").writeFileSync("/tmp/myfb-light-final.png",Buffer.from(r.data,"base64")));
    const r2=await send("Runtime.evaluate",{expression:`getComputedStyle(document.body).backgroundColor`});
    console.log("LIGHT bg:", r2.result.value);
    ws.close();process.exit(0);
  });
})().catch(e=>{console.error(e.message);process.exit(1)})
