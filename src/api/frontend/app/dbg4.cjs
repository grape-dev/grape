
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
  ws.on("open",async()=>{
    const r=await send("Runtime.evaluate",{expression:`document.documentElement.outerHTML.slice(0,300)`});
    console.log(r.result.value);
    ws.close();process.exit(0);
  });
})().catch(e=>{console.error(e.message);process.exit(1)})
