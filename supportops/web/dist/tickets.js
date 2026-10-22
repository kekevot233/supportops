"use strict";
// 工单数据只经 textContent 展示；建议是已有回答的引用，不执行其中的动作。
(() => {
  const ticketState={workspace:null,selected:null,data:null,pending:null,createPending:null};
  const ticketLabels={open:"待处理",in_progress:"处理中",resolved:"已解决",generating:"生成中",ready:"待人工核对",failed:"生成失败",general:"一般支持",installation:"安装部署",documents:"文档检索",memory:"记忆存储",tools:"工具调用",normal:"普通",low:"低",high:"高"};
  const eventLabels={created:"提交工单",suggestion_started:"请求建议",suggestion_ready:"建议就绪",suggestion_failed:"建议失败",suggestion_interrupted:"运行中断",accept:"人工确认",start:"人工接管",note:"处理记录",resolve:"人工标记解决"};
  function invalidate(){ticketState.data=null;$("ticket-detail-title").textContent="工单详情";$("ticket-count").textContent="—";$("ticket-list").replaceChildren();$("ticket-detail").replaceChildren(el("p","正在重新鉴权并读取工单…","muted"));$("ticket-evidence").textContent="点击建议引用重新读取原文。";$("ticket-run").textContent="查看建议对应的运行与用量。";}
  function reset(){ticketState.workspace=null;ticketState.selected=null;ticketState.data=null;ticketState.pending=null;ticketState.createPending=null;$("ticket-create").reset();$("ticket-list").replaceChildren();$("ticket-count").textContent="0";$("ticket-detail-title").textContent="从问题到可追溯的处理记录";$("ticket-detail").replaceChildren(el("p","创建或选择本人提交的工单。","muted"));$("ticket-evidence").textContent="点击建议引用重新读取原文。";$("ticket-run").textContent="查看建议对应的运行与用量。";}
  async function refresh(){
    if(ticketState.workspace!==state.workspace){reset();ticketState.workspace=state.workspace;}
    if(!state.workspace)return;
    invalidate();const epoch=state.epoch, rows=await api(path("/tickets"));if(epoch!==state.epoch)return;
    $("ticket-count").textContent=rows.length;
    $("ticket-list").replaceChildren(...rows.map(row=>{const node=button(row.title,()=>open(row.id),"ticket-row"+(row.id===ticketState.selected?" selected":""));node.dataset.ticket=row.id;node.append(el("small",`${ticketLabels[row.state]} · ${ticketLabels[row.priority]} · ${ticketLabels[row.category]}`));return node;}));
    if(!rows.length)$("ticket-list").append(el("p","还没有本人提交的工单。","muted"));
    if(ticketState.selected)await open(ticketState.selected);
  }
  async function open(id){
    ticketState.data=null;$("ticket-detail-title").textContent="工单详情";$("ticket-detail").replaceChildren(el("p","正在读取工单…","muted"));const epoch=state.epoch, data=await api(path(`/tickets/${id}`));if(epoch!==state.epoch)return;
    ticketState.selected=id;ticketState.data=data;
    $("ticket-evidence").textContent="点击建议引用重新读取原文。";$("ticket-run").textContent="查看建议对应的运行与用量。";
    render(data);document.querySelectorAll("[data-ticket]").forEach(node=>node.classList.toggle("selected",node.dataset.ticket===id));
  }
  async function operation(action,payload={}){
    const data=ticketState.data;
    if(!data)throw new Error("请先选择工单。");
    const base={expected_revision:data.revision,...payload}, fingerprint=JSON.stringify([state.workspace,data.id,action,base]);
    if(ticketState.pending?.fingerprint!==fingerprint)ticketState.pending={fingerprint,key:uid()};
    try {
      const result=await api(path(`/tickets/${data.id}/${action}`),{...base,idempotency_key:ticketState.pending.key});
      ticketState.pending=null;await refreshModel();await refresh();return result;
    } catch(error) {try{await refreshModel();await refresh();}catch{}throw error;}
  }
  function render(data){
    $("ticket-detail-title").textContent=data.title;
    const panel=$("ticket-detail");panel.replaceChildren();
    const meta=el("div",undefined,"ticket-meta");meta.append(el("span",ticketLabels[data.state],"tag"),el("span",`${ticketLabels[data.category]} / ${ticketLabels[data.priority]} · 人工选择`,"muted"),el("small",`修订 ${data.revision} · ${data.id}`,"muted"));panel.append(meta,el("p",data.description,"ticket-description"));
    const heading=el("div",undefined,"form-actions");heading.append(el("h3","处理建议（仅引用资料，不代表已执行）"));
    if(data.state==="open"&&!data.suggestions.some(x=>x.state==="generating")){
      const generate=button(data.suggestions.length?"生成新建议":"生成引用建议",async()=>{if(!await confirmModelRequest())return;notice("正在检索工单问题并生成引用建议；不自动重试…");await operation("suggestions");notice("建议已保存，请核对原文后人工确认；尚未执行任何修复。");},"");generate.id="ticket-suggest";heading.append(generate);
    }
    panel.append(heading);
    if(data.state==="open"&&!data.suggestions.some(x=>x.state==="generating")){
      const takeover=button("人工接管（不调用模型）",async()=>{const reason=prompt("填写人工接管原因。此操作不采纳模型建议，也不执行修复：");if(reason===null)return;if(!reason.trim())throw new Error("请填写人工接管原因。");await operation("start",{content:reason,confirm:true});notice("已记录人工接管；后续请填写实际处理结果。");});takeover.id="ticket-start";panel.append(takeover);
    }
    if(!data.suggestions.length)panel.append(el("p","尚未生成建议。提交工单不会请求模型。","muted"));
    for(const suggestion of data.suggestions){
      const card=el("article",undefined,"ticket-suggestion");card.dataset.suggestion=suggestion.id;
      card.append(el("strong",data.accepted_suggestion_id===suggestion.id ? "已由人工采纳" : ticketLabels[suggestion.state]));
      if(suggestion.answer){
        const answer=suggestion.answer;card.append(el("p",answer.content,"ticket-answer"));
        if(answer.status==="insufficient_evidence")card.append(el("p","证据不足，不能采纳为有依据的处理建议。请补充资料或人工排查。","source-warning"));
        if(answer.source_state!=="current")card.append(el("p","原建议来源已更新或删除，不能作为当前有效建议再次采纳。","source-warning"));
        const actions=el("div",undefined,"form-actions");
        for(const cite of answer.citations){const node=button(`[${cite.label}] ${labels[cite.state]||cite.state}`,()=>showCitation(answer.id,cite.label,"ticket-evidence"));node.dataset.ticketCitation=cite.label;actions.append(node);}
        const run=button("查看建议运行",()=>showRun(answer.run_id,"ticket-run"));run.dataset.ticketRun=answer.run_id;actions.append(run);
        if(data.state==="open"&&answer.status==="answered"&&answer.source_state==="current"&&!data.suggestions.some(x=>x.state==="generating")){
          const accept=button("人工确认并开始处理",async()=>{if(!confirm("确认已核对引用，采纳这条建议并将工单标记为处理中？此操作不执行任何系统修复。"))return;await operation("accept",{suggestion_id:suggestion.id,confirm:true});notice("已记录人工确认。请实际处理问题，并填写处理结果。");},"");accept.dataset.ticketAccept=suggestion.id;actions.append(accept);
        }
        card.append(actions);
      }
      if(suggestion.error_code)card.append(el("p",`${suggestion.error_code} · 不自动重试；请先核对失败运行。`,"source-warning"));
      if(!suggestion.answer){
        const actions=el("div",undefined,"form-actions");
        if(suggestion.run_id)actions.append(button("查看失败运行",()=>showRun(suggestion.run_id,"ticket-run")));
        actions.append(button("核对中断状态（不调用模型）",async()=>{await api(path(`/tickets/${data.id}/suggestions/${suggestion.id}/reconcile`),{});await refresh();notice("已核对已有运行；没有重新请求模型。");}));card.append(actions);
      }
      panel.append(card);
    }
    panel.append(el("h3","人工处理记录"));
    const timeline=el("ol",undefined,"ticket-timeline");for(const event of data.events){const item=el("li");item.append(el("strong",eventLabels[event.kind]||event.kind),el("small",event.created_at),el("p",event.content));timeline.append(item);}panel.append(timeline);
    if(data.state!=="resolved"){
      const form=el("form",undefined,"ticket-form"),label=el("label","处理过程 / 结果"),text=el("textarea"),actions=el("div",undefined,"form-actions"),save=el("button","追加处理记录");text.id="ticket-note";text.maxLength=2000;text.rows=3;text.required=true;text.placeholder="记录实际完成的操作与观察，不要把助手建议当作已执行结果。";label.append(text);save.type="submit";actions.append(save);
      if(data.state==="in_progress"){
        const resolve=button("记录结果并标记解决",async()=>{if(!text.reportValidity())return;if(!confirm("确认问题已由你实际处理，并将填写内容作为解决记录保存？"))return;await operation("resolve",{content:text.value,confirm:true});notice("已保存人工解决记录；没有自动关闭外部工单或写入知识库。");},"");resolve.id="ticket-resolve";actions.append(resolve);
      }
      form.append(label,actions);form.addEventListener("submit",event=>{event.preventDefault();task(async()=>{await operation("note",{content:text.value});notice("处理记录已保存。");});});panel.append(form);
    }else panel.append(el("p","此工单已由本人标记解决；处理记录保留，不自动沉淀到知识库。","source-warning"));
  }
  $("nav-tickets").addEventListener("click",()=>task(async()=>{tab("tickets");await refresh();}));
  $("ticket-create").addEventListener("submit",event=>{event.preventDefault();task(async()=>{
    if(!state.workspace)throw new Error("请选择有权访问的空间。");
    const payload={title:$("ticket-title").value,description:$("ticket-description").value,category:$("ticket-category").value,priority:$("ticket-priority").value},fingerprint=JSON.stringify([state.workspace,payload]);
    if(ticketState.createPending?.fingerprint!==fingerprint)ticketState.createPending={fingerprint,key:uid()};
    const result=await api(path("/tickets"),{...payload,idempotency_key:ticketState.createPending.key});ticketState.createPending=null;ticketState.selected=result.id;$("ticket-create").reset();await refresh();notice("工单已提交；尚未调用模型，分类与优先级由你选择。");
  });});
  window.SupportOpsTickets={refresh,reset,invalidate};
})();
