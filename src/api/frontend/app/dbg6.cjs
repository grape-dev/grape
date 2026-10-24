
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
    await send("Page.enable");
    const mutations=[];
    ws.on("event",(ev)=>{
      if(ev.method==="Runtime.consoleAPICalled" && ev.params.type==="log") mutations.push(ev.params.args.map(a=>a.value||"").join(" "));
    });
    await send("Page.addScriptToEvaluateOnNewDocument",{source:`
      (function(){
        var obs=new MutationObserver(function(muts){
          muts.forEach(function(mu){
            if(mu.attributeName && mu.attributeName.indexOf('data-theme')===0){
              console.log('ATTR-CHANGE', mu.attributeName, document.documentElement.getAttribute(mu.attributeName), (new Error().stack||'').split('\\n')[2]||'');
            }
          });
        });
        obs.observe(document.documentElement,{attributes:true});
      })();
    `});
    await send("Emulation.setEmulatedMedia",{features:[{name:"prefers-color-scheme", value:"dark"}]});
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html#my-feedback"});
    await sleep(6000);
    console.log(mutations.slice(0,20).join("\n") || "NO ATTR MUTATIONS");
    const r=await send("Runtime.evaluate",{expression:`document.documentElement.getAttribute('data-theme')+'|'+document.documentElement.getAttribute('data-theme-mode')`});
    console.log("FINAL:", r.result.value);
    ws.close();process.exit(0);
  });
})().catch(e=>{console.error(e.message);process.exit(1)})
