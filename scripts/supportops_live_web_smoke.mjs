// 一条聚焦的浏览器流程，全程模拟供应商；不启动真正的 serve-live。
import {spawn} from "node:child_process";
import {mkdtemp, mkdir, readFile, writeFile} from "node:fs/promises";
import {existsSync} from "node:fs";
import {resolve, join} from "node:path";
import {createInterface} from "node:readline";
import assert from "node:assert/strict";

const root=resolve(import.meta.dirname,".."), output=join(root,"outputs/supportops/browser");
await mkdir(output,{recursive:true});
const profile=await mkdtemp(join(output,"live-chrome-"));
const chrome=["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe","C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find(existsSync);
if(!chrome)throw new Error("没有可用的本机浏览器，未完成页面验证");
const fixture=spawn("D:\\conda-envs\\helloagent\\python.exe",["-u","-m","scripts.supportops_live_web_fixture"],{cwd:root,windowsHide:true,
  env:{PYTHON_DOTENV_DISABLED:"1",HELLO_AGENTS_RUN_REAL_STORES:"0",SystemRoot:process.env.SystemRoot,TEMP:process.env.TEMP,TMP:process.env.TMP},stdio:["pipe","pipe","pipe"]});
let launch,browser,socket,cdp,dialogs=0;
const ticketMode=process.argv.includes("--tickets");
createInterface({input:fixture.stdout}).on("line",line=>{launch=JSON.parse(line).ready;});
fixture.stderr.on("data",()=>{});
async function until(check){const end=Date.now()+20000;while(Date.now()<end){if(await check())return;await new Promise(r=>setTimeout(r,120));}throw new Error("页面定点检查超时");}
class CDP {
  constructor(ws){this.ws=ws;this.id=0;this.pending=new Map();ws.addEventListener("message",event=>{
    const msg=JSON.parse(event.data);if(msg.id){const p=this.pending.get(msg.id);this.pending.delete(msg.id);if(msg.error)p.reject(new Error("CDP error"));else p.resolve(msg.result);}
    else if(msg.method==="Page.javascriptDialogOpening"){dialogs++;this.call("Page.handleJavaScriptDialog",{accept:true}).catch(()=>{});}
  });}
  call(method,params={}){return new Promise((resolve,reject)=>{const id=++this.id;const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error(`CDP timeout: ${method}`));},15000);this.pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});this.ws.send(JSON.stringify({id,method,params}));});}
  async eval(expression){const result=await this.call("Runtime.evaluate",{expression,awaitPromise:true,returnByValue:true});if(result.exceptionDetails)throw new Error("页面执行异常");return result.result.value;}
}
async function idle(){await until(()=>cdp.eval("document.readyState==='complete' && !document.querySelector('.shell')?.inert"));}
async function evaluate(expression){const value=await cdp.eval(expression);await idle();return value;}
try {
  await until(()=>launch || fixture.exitCode!==null);assert(launch,"临时模拟服务未启动");
  console.log("mock server ready");
  browser=spawn(chrome,["--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check","--disable-background-networking","--disable-component-update","--remote-debugging-address=127.0.0.1","--remote-debugging-port=0",`--user-data-dir=${profile}`,"about:blank"],{windowsHide:true,stdio:"ignore"});
  await until(()=>existsSync(join(profile,"DevToolsActivePort")));
  console.log("browser ready");
  const [port]=String(await readFile(join(profile,"DevToolsActivePort"))).trim().split(/\r?\n/);
  const tabs=await (await fetch(`http://127.0.0.1:${port}/json/list`,{signal:AbortSignal.timeout(5000)})).json();
  socket=new WebSocket(tabs.find(x=>x.type==="page").webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error("CDP connection timeout")),5000);socket.addEventListener("open",()=>{clearTimeout(timer);resolve();},{once:true});socket.addEventListener("error",()=>{clearTimeout(timer);reject(new Error("CDP connection failed"));},{once:true});});cdp=new CDP(socket);
  await cdp.call("Page.enable");await cdp.call("Runtime.enable");
  await cdp.call("Emulation.setDeviceMetricsOverride",{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  await cdp.call("Page.navigate",{url:launch});
  await until(()=>cdp.eval("!document.getElementById('app')?.hidden && document.getElementById('model-budget')?.textContent.includes('0/1')"));await idle();
  console.log("page ready");
  assert(await cdp.eval("document.getElementById('model-banner').textContent.includes('真实 DeepSeek')"));
  assert(await cdp.eval("location.hash===''"));
  if(ticketMode){
    await evaluate("document.getElementById('nav-tickets').click()");
    await evaluate("document.getElementById('ticket-title').value='扫描版 PDF 无法检索';document.getElementById('ticket-description').value='上传扫描版 PDF 后无法检索文字，应如何处理？';document.getElementById('ticket-category').value='documents';document.getElementById('ticket-create').requestSubmit()");
    assert(await cdp.eval("document.getElementById('ticket-detail').textContent.includes('待处理')"));
    assert(await cdp.eval("document.getElementById('model-budget').textContent.includes('0/1')"));
    await evaluate("document.getElementById('ticket-suggest').click()");
    assert.equal(dialogs,1);
    assert(await cdp.eval("document.getElementById('ticket-detail').textContent.includes('待人工核对')"));
    await evaluate("document.querySelector('[data-ticket-citation]').click()");
    assert(await cdp.eval("Boolean(document.querySelector('#ticket-evidence .quote'))"));
    await evaluate("document.querySelector('[data-ticket-run]').click()");
    assert(await cdp.eval("document.getElementById('ticket-run').textContent.includes('10 / 20 / 30')"));
    await evaluate("document.querySelector('[data-ticket-accept]').click()");
    assert.equal(dialogs,2);
    assert(await cdp.eval("document.getElementById('ticket-detail').textContent.includes('处理中')"));
    await evaluate("document.getElementById('ticket-note').value='人工确认扫描件无文字层，已另行执行OCR并核对提取文字。';document.getElementById('ticket-resolve').click()");
    assert.equal(dialogs,3);
    assert(await cdp.eval("document.getElementById('ticket-detail').textContent.includes('已解决')"));
    assert(await cdp.eval("document.getElementById('model-budget').textContent.includes('1/1')"));
    await cdp.call("Emulation.setDeviceMetricsOverride",{width:1440,height:1500,deviceScaleFactor:1,mobile:false});
    const capture=await cdp.call("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});
    const screenshot=join(output,`tickets-mock-${Date.now()}.png`);await writeFile(screenshot,Buffer.from(capture.data,"base64"));
    await evaluate("document.getElementById('disconnect').click()");
    assert(await cdp.eval("document.getElementById('locked').hidden===false && !document.getElementById('ticket-detail').textContent.includes('人工确认扫描件')"));
    console.log(JSON.stringify({passed:true,scope:"one_ticket_mock_browser_flow",real_model_calls:0,checks:["create_without_model","suggest_once","citation","usage","human_accept","human_resolve","disconnect_clears_private_data"],screenshot}));
  }else{
  await evaluate("document.getElementById('new-chat').requestSubmit()");
  await evaluate("document.getElementById('question').value='扫描版 PDF 为什么无法检索？';document.getElementById('ask').requestSubmit()");
  assert.equal(dialogs,1);
  assert(await cdp.eval("Boolean(document.querySelector('[data-citation]'))"));
  await evaluate("document.querySelector('[data-citation]').click()");
  assert(await cdp.eval("Boolean(document.querySelector('#citation-panel .quote'))"));
  await evaluate("document.querySelector('[data-run]').click()");
  assert(await cdp.eval("document.getElementById('run-panel').textContent.includes('10 / 20 / 30')"));
  assert(await cdp.eval("document.getElementById('model-budget').textContent.includes('1/1')"));
  assert(await cdp.eval("document.getElementById('upload-card').hidden"));
  const capture=await cdp.call("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});
  const screenshot=join(output,`live-web-mock-${Date.now()}.png`);await writeFile(screenshot,Buffer.from(capture.data,"base64"));
  await evaluate("document.getElementById('question').value='再次提问';document.getElementById('ask').requestSubmit()");
  assert(await cdp.eval("document.getElementById('notice').textContent.includes('WEB_BUDGET_EXCEEDED')"));
  console.log(JSON.stringify({passed:true,scope:"one_mock_browser_flow",real_model_calls:0,checks:["mode","consent","answer","citation","usage","read_only","budget_rejection"],screenshot}));
  }
} finally {
  socket?.close();browser?.kill();fixture.stdin.write("stop\n");fixture.stdin.end();
  await new Promise(resolve=>{if(fixture.exitCode!==null)return resolve();fixture.once("exit",resolve);setTimeout(()=>{fixture.kill();resolve();},5000).unref();});
}
