
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
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html"});
    await sleep(1500);
    await send("Runtime.evaluate",{expression:`localStorage.setItem('token', ${JSON.stringify(TOKEN)}); localStorage.setItem('user', ${USER}); 'ok'`});
    const r=await send("Runtime.evaluate",{expression:`(async function(){
      try {
        var out = await window.API.get('/feedback/mine?page=0&size=10');
        return 'RESOLVED: ' + JSON.stringify(out).slice(0,200);
      } catch(e) {
        return 'REJECTED: ' + String(e && e.message || e);
      }
    })()`, awaitPromise:true, returnByValue:true});
    console.log("API.get:", r.result.value);
    ws.close();process.exit(0);
  });
})().catch(e=>{console.error(e.message);process.exit(1)})
