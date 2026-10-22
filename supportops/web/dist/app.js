"use strict";
// 文档、回答和错误始终通过 textContent 输出；不解析 HTML，不插入不可信 URL。
const $ = (id) => document.getElementById(id);
const el = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); if (className) node.className = className; return node; };
const state = {token: new URLSearchParams(location.hash.slice(1)).get("access"), workspace: null, chat: null, workspaces: [], update: null, busy: false, pending: null, epoch: 0, session: null};
history.replaceState(null, "", "/");
// 从锁定页面重新粘贴完整启动链接时，浏览器可能只执行 fragment 导航；重新加载以消费新凭据。
window.addEventListener("hashchange",()=>{if(new URLSearchParams(location.hash.slice(1)).has("access"))location.reload();});
const path = (suffix) => `/api/workspaces/${encodeURIComponent(state.workspace)}${suffix}`;
const uid = () => crypto.randomUUID();
const labels = {pending:"等待处理", running:"执行中", succeeded:"已完成", failed:"失败", cancelled:"已取消", ready:"已就绪", deleted:"已删除", current:"当前版本", historical:"历史版本"};
function button(text, action, className="secondary") { const node = el("button", text, className); node.type = "button"; node.addEventListener("click", () => task(action)); return node; }
async function api(url, body, method = body === undefined ? "GET" : "POST") {
  const response = await fetch(url, {method, headers:{Authorization:`Bearer ${state.token}`, ...(body === undefined ? {} : {"Content-Type":"application/json"})}, ...(body === undefined ? {} : {body:JSON.stringify(body)}), cache:"no-store", credentials:"omit"});
  const payload = await response.json();
  if (!response.ok) { const error = new Error(`${payload.message || "请求失败"} [${payload.code || response.status}]${payload.request_id ? ` · ${payload.request_id}` : ""}`); Object.assign(error,{code:payload.code,run:payload.run_id,received:true}); throw error; }
  return payload.data;
}
function notice(text, error=false) { $("notice").replaceChildren(el("span",text)); $("notice").className = `notice${error ? " error" : ""}`; $("notice").hidden = !text; }
function renderModel(session) {
  state.session=session;
  const live=session.model_mode==="local_deepseek", guard=session.financial_guard;
  $("model-banner").textContent=live ? "本机真实 DeepSeek · 问题、相关对话和公开证据将发送给供应商并产生费用 · 禁止敏感内容、代理或公网部署" : "本地演示身份，非企业认证 · Fake 原文选择器，未调用真实模型，不具备语义判断 · 请勿上传敏感资料";
  $("model-budget").textContent=live ? `Chat 预留次数 ${guard.request_attempts}/${guard.max_requests} · 本次启动会话预留 ¥${guard.session_reserved_cny}/¥${guard.session_budget_cny} · 单次预留上限 ¥${guard.request_budget_cny} · 累计剩余可预留 ¥${guard.cumulative_available_cny}${guard.stopped ? " · 已停止，需先核对记录" : ""}（均非账单）` : "Fake 离线模式 · 无付费模型调用";
  $("ticket-model-budget").textContent=$("model-budget").textContent;
  $("model-consent").textContent=live ? "发送前需确认公开内容；仅检索公开种子资料，不自动重试，不调用在线 Embedding。" : "证据只进入本轮临时上下文；不执行文档中的指令。";
  if($("model-intro"))$("model-intro").textContent=live ? "真实模型从公开资料中摘取回答并返回引用；请核对原文，不保证结论正确。" : "先新建会话，再输入问题。Fake 选择原文，不判断是否足以回答。";
}
async function refreshModel() { renderModel(await api("/api/session")); }
async function confirmModelRequest() {
  if(state.session?.model_mode!=="local_deepseek")return true;
  await refreshModel();
  if(state.session.financial_guard.stopped)throw new Error("真实会话已停止，请先核对运行与费用记录；不自动重试。");
  return confirm("本次将把问题、相关历史对话和公开资料证据发送给 DeepSeek，并产生费用。确认不含任何敏感或个人信息并发送？");
}
function showError(error) {
  notice(error.message || "连接失败，请检查本机服务。",true);
  if (error.run) $("notice").append(button("查看失败运行",()=>showRun(error.run)));
  if (error.code === "UNAUTHENTICATED") lock();
}
function lock() { state.token = null; state.epoch++; state.chat = null; state.pending=null;state.workspace=null;state.workspaces=[];window.SupportOpsTickets?.reset();resetUpload();$("question").value="";$("app").hidden = true; $("locked").hidden = false; for(const id of ["chat-list","messages","citation-panel","run-panel","document-list","job-list","workspace"])$(id).replaceChildren(); $("identity").textContent="未认证"; $("workspace").disabled = true; }
async function task(action) {
  if (state.busy) return;
  state.busy = true;
  const controls = [...document.querySelectorAll("button,input,select,textarea")].filter(x=>!x.disabled);
  controls.forEach(x=>x.disabled=true); document.querySelector(".shell").inert=true; $("app").setAttribute("aria-busy","true");
  try { await action(); } catch(error) { showError(error); }
  finally { state.busy=false; controls.forEach(x=>{if(x.isConnected)x.disabled=false;}); document.querySelector(".shell").inert=false; $("app").removeAttribute("aria-busy"); if (!state.token) $("workspace").disabled=true; }
}
function tab(name) {
  for(const item of ["chat","documents","tickets"])$(item+"-page").hidden=item!==name;
  $("conversation-nav").hidden = name !== "chat"; $("page-title").textContent = {chat:"知识问答",documents:"文档与任务",tickets:"工单助手"}[name];
  for (const item of ["chat","documents","tickets"]) { $("nav-"+item).classList.toggle("active",item===name); if(item===name) $("nav-"+item).setAttribute("aria-current","page"); else $("nav-"+item).removeAttribute("aria-current"); }
}
function emptyChat() { $("messages").replaceChildren(el("div","新建或选择一个私有会话，即可开始提问。","empty muted")); $("conversation-title").textContent="从可追溯的证据开始"; $("citation-panel").textContent="点击引用查看原文。"; $("run-panel").textContent="点击“查看运行”查看诊断。"; }
async function refresh() {
  window.SupportOpsTickets?.invalidate();
  if (!state.workspace) { window.SupportOpsTickets?.reset();$("messages").replaceChildren(el("p","当前身份没有可访问空间。")); return; }
  const epoch=state.epoch;
  const [docs,jobs,chats] = await Promise.all([api(path("/documents")),api(path("/jobs")),api(path("/conversations")),refreshModel()]);
  if(epoch !== state.epoch) return;
  const role = state.workspaces.find(x=>x.id===state.workspace)?.role;
  $("role").textContent=`${role || "—"} · 本机演示`;
  document.querySelectorAll(".writer").forEach(x=>x.hidden=!(["admin","editor"].includes(role)));
  renderDocuments(docs,role); renderJobs(jobs,role); renderChats(chats);
  await window.SupportOpsTickets?.refresh();
  if (state.chat) await loadChat(state.chat);
}
function renderChats(chats) {
  $("chat-list").replaceChildren(...chats.map(chat=>{const node=button(chat.title,()=>loadChat(chat.id),chat.id===state.chat?"selected":""); node.dataset.chat=chat.id; return node;}));
}
async function loadChat(id) {
  $("citation-panel").textContent="点击引用重新鉴权并读取原文；页面不复用上一轮的证据快照。";$("run-panel").textContent="点击“查看运行”读取本人的诊断记录。";
  const epoch=state.epoch, data=await api(path(`/conversations/${id}`));
  const answers=await Promise.all(data.messages.map(message=>message.role==="assistant" ? api(path(`/answers/${message.answer_id}`)) : null));
  if(epoch!==state.epoch)return;
  state.chat=id; $("conversation-title").textContent=data.title; $("messages").replaceChildren();
  document.querySelectorAll("[data-chat]").forEach(x=>x.classList.toggle("selected",x.dataset.chat===id));
  for (let i=0;i<data.messages.length;i++) {
    const message=data.messages[i], node=el("article",undefined,`message ${message.role}`);
    node.append(el("div",message.role==="user"?"YOU / 我的提问":"SUPPORTOPS / 原文选择","speaker"),el("div",answers[i]?.content || message.content,"body"));
    if(answers[i]) await appendAnswer(node,answers[i]);
    if(epoch!==state.epoch)return;
    $("messages").append(node);
  }
  if(!data.messages.length) $("messages").append(el("div","会话已建立。输入你的第一个问题。","empty muted"));
  $("messages").scrollTop=$("messages").scrollHeight;
}
async function appendAnswer(node,answer) {
  node.dataset.answer=answer.id;
  if(answer.source_state!=="current")node.append(el("div",answer.source_state==="deleted"?"来源已删除，回答正文已隐藏。":"历史资料版本：引用不会自动替换为最新版。","source-warning"));
  if(answer.status==="insufficient_evidence")node.append(el("div","资料不足，不等于服务故障。","source-warning"));
  const actions=el("div",undefined,"message-actions");
  for(const cite of answer.citations) {const b=button(`[${cite.label}] ${labels[cite.state] || cite.state}`,()=>showCitation(answer.id,cite.label)); b.dataset.citation=cite.label; actions.append(b);}
  const run=button("查看运行",()=>showRun(answer.run_id));run.dataset.run=answer.run_id;actions.append(run);node.append(actions);
  const feedback=el("form",undefined,"feedback"),rating=el("select"),reason=el("select"),comment=el("input"),submit=el("button","保存反馈"),saved=el("small","");
  rating.setAttribute("aria-label","反馈评价");reason.setAttribute("aria-label","反馈原因");comment.setAttribute("aria-label","反馈补充");comment.placeholder="补充说明（可选，最多 500 字）";comment.maxLength=500;
  for(const [value,label] of [["useful","有用"],["not_useful","无用"]]){const option=el("option",label);option.value=value;rating.append(option);}
  for(const [value,label] of [["","选择原因（可选）"],["irrelevant","引用不相关"],["incorrect","结论错误"],["outdated","资料过时"],["other","其他"]]){const option=el("option",label);option.value=value;reason.append(option);}
  const old=await api(path(`/answers/${answer.id}/feedback`));if(old){rating.value=old.rating;reason.value=old.reason || "";comment.value=old.comment || "";saved.textContent="已保存";}
  feedback.append(rating,reason,submit,comment,saved);
  feedback.addEventListener("submit",event=>{event.preventDefault();task(async()=>{await api(path(`/answers/${answer.id}/feedback`),{rating:rating.value,reason:reason.value || null,comment:comment.value},"PUT");saved.textContent="已保存";notice("反馈已保存，仅关联你自己的回答。");});});
  node.append(feedback);
}
function details(panel,fields) { const dl=el("dl");for(const [key,value] of fields){dl.append(el("dt",key),el("dd",value===null?"未提供":value));}panel.replaceChildren(dl); }
async function showCitation(answer,label,panelId="citation-panel") {
  const panel=$(panelId);panel.textContent="正在重新鉴权与读取来源…";
  try {const data=await api(path(`/answers/${answer}/citations/${label}`)),chunk=data.chunk;
    details(panel,[["引用",`[${label}] ${data.is_current?"当前版本":"历史版本"}`],["标题",chunk.title || chunk.document_id],["版本",data.version_id],["章节",chunk.section || "—"],["字符区间",`${chunk.start_char} – ${chunk.end_char}`]]);
    panel.append(el("div",chunk.content,"quote"));
  } catch(error) { panel.textContent="来源不可访问或已失效；不会用新版本替代旧引用。";throw error; }
}
async function showRun(id,panelId="run-panel") {
  const run=await api(path(`/runs/${id}`));
  const usage=run.actual_usage, live=run.model_mode==="local_deepseek";
  details($(panelId),[["运行 ID",run.id],["Request ID",run.request_id],["状态 / 错误",`${run.state} / ${run.error_code || "无"}`],["客户端模式",run.model_mode],["invoke 尝试（不代表供应商调用）",run.model_attempts],["输入 Token（本地估算）",run.estimated_input_tokens],["输出 Token（本地估算）",run.estimated_output_tokens],["检索 / 生成 / 总耗时（ms）",[run.retrieval_ms,run.generation_ms,run.total_ms].map(x=>Number(x).toFixed(1)).join(" / ")],["usage 来源",run.usage_source],["供应商 Token 回执（输入 / 输出 / 总计）",usage ? `${usage.input_tokens} / ${usage.output_tokens} / ${usage.total_tokens}` : "未提供；不能按零计算"],["本次费用预留（非账单）",run.reserved_cny===undefined ? "未提供" : `¥${run.reserved_cny}`],["回执按高峰价保守估算（非账单）",run.receipt_priced_upper_estimate_cny===undefined || run.receipt_priced_upper_estimate_cny===null ? "未提供" : `¥${run.receipt_priced_upper_estimate_cny}`],["供应商实际费用","未提供"],["外部请求预留尝试",live ? `${run.external_model_attempts ?? "未知"}（不等于供应商收到）` : "0（本机 Fake 模式）"]]);
}
function resetUpload() { state.update=null;state.uploadPending=null;$("upload-heading").textContent="接入知识文档";$("upload-submit").textContent="提交索引任务";$("upload-state").textContent="新文档";$("cancel-update").hidden=true;$("upload").reset(); }
function renderDocuments(docs,role) {
  $("document-count").textContent=docs.length;$("ready-count").textContent=docs.filter(x=>x.current_version_id).length;
  $("document-list").replaceChildren(...docs.map(doc=>{
    const tr=el("tr"),title=el("td",doc.title),version=el("td",`v${doc.latest_version_number} · ${labels[doc.latest_state] || doc.latest_state}`),current=el("td",doc.current_version_id || "尚未发布"),actions=el("td");title.append(el("small",doc.id));
    if(["editor","admin"].includes(role)) {
      const update=button("更新",()=>{state.update=doc.id;$("upload-title").value=doc.title;$("upload-content").value="";$("upload-state").textContent=`更新 ${doc.id}（成功后才切换版本）`;$("upload-heading").textContent="提交新版本";$("upload-submit").textContent="提交新版本任务";$("cancel-update").hidden=false;$("upload-card").scrollIntoView({behavior:"smooth"});}); update.dataset.update=doc.id;
      const remove=button("删除",async()=>{if(!confirm(`确认删除「${doc.title}」的原件与全部索引？依赖该资料的旧回答将隐藏。此操作没有界面撤销。`))return;await api(path(`/documents/${doc.id}`),{confirm:true},"DELETE");resetUpload();notice("资料已删除，原件和索引已清理；历史元数据仍按保留策略保存。");await refresh();},"danger");remove.dataset.delete=doc.id;actions.append(update,remove);
    } else actions.textContent="只读";
    tr.append(title,version,current,actions);return tr;
  }));
  if(!docs.length){const tr=el("tr"),td=el("td","尚未接入文档。管理员可以上传公开项目资料。");td.colSpan=4;tr.append(td);$("document-list").append(tr);}
}
function renderJobs(jobs,role) {
  $("pending-count").textContent=jobs.filter(x=>["pending","running"].includes(x.state)).length;
  $("job-list").replaceChildren(...jobs.map(job=>{const tr=el("tr"),id=el("td",job.id),status=el("td"),attempts=el("td",job.attempts),action=el("td",job.error_code || "—");id.append(el("small",job.document_id));status.append(el("span",labels[job.state] || job.state,"tag"));if(job.state==="failed" && ["editor","admin"].includes(role))action.append(button("重试",async()=>{await api(path(`/jobs/${job.id}/retry`),{});notice("已重新排队，请显式处理任务。");await refresh();}));tr.append(id,status,attempts,action);return tr;}));
}
$("nav-chat").addEventListener("click",()=>tab("chat"));$("nav-documents").addEventListener("click",()=>tab("documents"));
$("workspace").addEventListener("change",()=>task(async()=>{state.epoch++;state.workspace=$("workspace").value;state.chat=null;state.pending=null;resetUpload();emptyChat();await refresh();}));
$("refresh").addEventListener("click",()=>task(async()=>{state.workspaces=await api("/api/workspaces");const current=state.workspaces.find(x=>x.id===state.workspace);if(!current){state.epoch++;state.workspace=state.workspaces[0]?.id;state.chat=null;emptyChat();}$("workspace").replaceChildren(...state.workspaces.map(x=>{const o=el("option",x.name);o.value=x.id;return o;}));$("workspace").value=state.workspace || "";await refresh();}));
$("disconnect").addEventListener("click",()=>{lock();notice("当前页面的访问凭据已丢弃。关闭服务进程才能使启动链接失效。");});
$("example-question").addEventListener("click",()=>{$("question").value="扫描版 PDF 为什么无法检索？";$("question").focus();});
$("new-chat").addEventListener("submit",event=>{event.preventDefault();task(async()=>{if(!state.workspace)throw new Error("请先选择有权访问的空间。");const chat=await api(path("/conversations"),{title:$("chat-title").value});state.chat=chat.id;await refresh();tab("chat");notice("已建立本人私有会话。管理员不能越权读取。");});});
$("ask").addEventListener("submit",event=>{event.preventDefault();task(async()=>{
  if(!state.chat)throw new Error("请先新建或选择会话。");const question=$("question").value.trim();if(!question)throw new Error("请输入问题。");
  if(!await confirmModelRequest())return;
  if(!state.pending || state.pending.chat!==state.chat || state.pending.question!==question)state.pending={chat:state.chat,question,key:uid()};
  notice(state.session?.model_mode==="local_deepseek" ? "正在请求 DeepSeek，请勿重复发送；失败不自动重试…" : "正在选择原文…");
  try {await api(path(`/conversations/${state.chat}/messages`),{question,idempotency_key:state.pending.key});state.pending=null;$("question").value="";await refresh();notice("已保存完整问答轮次。请核对原文，不将摘录当作经过语义验证的结论。");}
  catch(error){if(error.received)state.pending=null;try{await refreshModel();}catch{}throw error;}
});});
$("upload-file").addEventListener("change",()=>task(async()=>{const file=$("upload-file").files[0];if(!file)return;if(file.size>256000 || !/\.(txt|md|markdown)$/i.test(file.name))throw new Error("只支持最大 256000 字节的 TXT / Markdown。");const text=new TextDecoder("utf-8",{fatal:true}).decode(await file.arrayBuffer());$("upload-filename").value=file.name;$("upload-title").value=file.name.replace(/\.[^.]+$/,"");$("upload-content").value=text;}));
$("upload").addEventListener("submit",event=>{event.preventDefault();task(async()=>{const text=$("upload-content").value;if(new TextEncoder().encode(text).length>256000)throw new Error("正文超过 256000 字节。");const suffix=state.update?`/documents/${state.update}/versions`:"/documents",payload={title:$("upload-title").value,filename:$("upload-filename").value,content_text:text},fingerprint=JSON.stringify([state.workspace,suffix,payload]);if(state.uploadPending?.fingerprint!==fingerprint)state.uploadPending={fingerprint,key:uid()};await api(path(suffix),{...payload,idempotency_key:state.uploadPending.key});resetUpload();await refresh();notice("索引任务已创建。点击“处理下一个任务”后发布；旧版本在成功发布前继续可用。");});});
$("cancel-update").addEventListener("click",resetUpload);
$("process-job").addEventListener("click",()=>task(async()=>{const result=await api(path("/jobs/process-next"),{});await refresh();notice(result.job?`任务 ${result.job.id}：${labels[result.job.state] || result.job.state}${result.job.error_code?` / ${result.job.error_code}`:""}`:"没有可领取任务；执行中的任务须等待租约过期后才能重领。");}));
task(async()=>{if(!state.token){lock();return;}const session=await api("/api/session");renderModel(session);$("identity").textContent=session.identity.subject;state.workspaces=await api("/api/workspaces");$("workspace").replaceChildren(...state.workspaces.map(x=>{const option=el("option",x.name);option.value=x.id;return option;}));state.workspace=state.workspaces[0]?.id || null;$("workspace").disabled=false;$("app").hidden=false;$("locked").hidden=true;await refresh();});
