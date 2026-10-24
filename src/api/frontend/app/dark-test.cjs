
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
    // emulate OS dark
    await send("Emulation.setEmulatedMedia",{features:[{name:"prefers-color-scheme", value:"dark"}]});
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html"});
    await sleep(1200);
    await send("Runtime.evaluate",{expression:`localStorage.setItem('token', ${JSON.stringify(TOKEN)}); localStorage.setItem('user', ${USER}); localStorage.setItem('grapeTheme','apple'); localStorage.setItem('grapeThemeMode','auto'); 'ok'`});
    await send("Page.navigate",{url:"http://localhost:3000/static/index.html#my-feedback"});
    await sleep(3000);
    const r=await send("Runtime.evaluate",{expression:`JSON.stringify({
      theme: document.documentElement.getAttribute('data-theme'),
      mode: document.documentElement.getAttribute('data-theme-mode'),
      bodyBg: getComputedStyle(document.body).backgroundColor,
      darkVars: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),
      empty: !!document.querySelector('#pageContent .myfb__empty')
    })`});
    console.log("DARK-AUTO:", r.result.value);
    await send("Page.captureScreenshot",{format:"png"}).then(r=>require("fs").writeFileSync("/tmp/myfb-list-dark2.png",Buffer.from(r.data,"base64")));
    ws.close();process.exit(0);
  });
})().catch(e=>{console.error(e.message);process.exit(1)})
