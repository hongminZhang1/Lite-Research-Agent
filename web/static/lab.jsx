const {useState, useEffect, useRef} = React;
const policyLabel = {adaptive:"按需升级", strong:"每步强模型", compare:"对照运行"};
const statusLabel = {complete:"研究完成", budget:"预算已用尽", no_evidence:"证据不足", model_error:"模型请求失败", error:"执行失败"};
const blankEndpoint = {base:"", model:"", key:"", input_price:"", output_price:"", cache_price:"", rate_source:"", billing:"auto"};
const deepseekBase = "https://api.deepseek.com";
const blankLimits = {rounds:3, seconds:"", tokens:"", usd:""};
const budgetFields = [
  ["rounds","轮数",1],
  ["seconds","秒",5],
  ["tokens","Token",64],
  ["usd","美元",0],
];
const fee = value => value == null ? "未知" : `US$ ${Number(value).toFixed(6)}`;
const tokens = value => Number(value ?? 0).toLocaleString("zh-CN");
const safeLink = url => /^https?:\/\//i.test(url ?? "") ? url : undefined;

function payload(config) {
  const optionalNumber = value => value === "" || value == null ? null : Number(value);
  const clean = item => {
    const base = item.base.trim();
    const model = item.model.trim();
    const key = item.key.trim();
    if (!base && !model && !key) return null;
    return {base, model, key, input_price:optionalNumber(item.input_price), output_price:optionalNumber(item.output_price),
      cache_price:optionalNumber(item.cache_price), rate_source:item.rate_source, billing:item.billing};
  };
  return {fast:clean(config.fast), strong:clean(config.strong), search_key:config.search_key,
    search_credit_price:optionalNumber(config.search_credit_price)};
}

function budgetError(limits) {
  const messages = {
    rounds:"轮数必须是 1 或以上的整数。",
    seconds:"时间预算必须是 5 或以上的整数秒，也可以留空表示不限制。",
    tokens:"Token 预算必须是 64 或以上的整数，也可以留空表示不限制。",
    usd:"费用预算必须是 0 或以上的数字，也可以留空。",
  };
  for (const [field,,min] of budgetFields) {
    const empty = limits[field] === "";
    if (field !== "rounds" && empty) continue;
    const value = Number(limits[field]);
    if (empty || !Number.isFinite(value) || value < min || (field !== "usd" && !Number.isInteger(value))) {
      return messages[field];
    }
  }
  return "";
}

function Settings({dialog,config,change}) {
  const [message,setMessage]=useState("");
  const [checking,setChecking]=useState("");
  async function test(kind) {
    setChecking(kind); setMessage("");
    try {
      const response=await fetch(`/api/lab/check/${kind}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload(config))});
      const result=await response.json();
      setMessage((result.ok?"通过 · ":"未通过 · ")+(result.message||"服务未返回结果。")+(result.sources?.length?` ${result.sources.join("；")}`:""));
    } catch {setMessage("无法连接到本地服务。");}
    finally {setChecking("");}
  }
  return <dialog ref={dialog} className="settings"><div className="headrow"><h2>模型与搜索设置</h2><button className="quiet" onClick={()=>dialog.current.close()}>完成</button></div>
  <small>手动输入的密钥仅保存在当前页面。模型连接测试会发出一次 API 调用。费率单位为美元 / 百万 Token。</small>
    {[["fast","普通模型"],["strong","强模型（可选）"]].map(([kind,title])=><section key={kind}>
      <div className="headrow"><h3>{title}</h3><div className="actions">
        <button className="quiet" disabled={!!checking} onClick={()=>test(kind)}>{checking===kind?"正在测试…":"测试模型连接"}</button>
      </div></div>
      <div className="fields">{[["base","API Base URL","https://服务商地址/v1"],["model","模型名","服务商提供的模型 ID"],["key","API Key","输入服务商密钥"],["input_price","输入费率","可选"],["output_price","输出费率","可选"],["cache_price","缓存输入费率","可选"],["rate_source","费率来源","供应商价格页或协议名称"]].map(([field,label,placeholder])=><label className={`field ${field==="base"?"full":""}`} key={field}>{label}
        <input type={field==="key"?"password":field.endsWith("price")?"number":"text"} min="0" step="any"
          placeholder={field==="key"&&config.deepseek_configured&&[deepseekBase,deepseekBase+"/v1"].includes(config[kind].base.trim().replace(/\/+$/, ""))?"留空使用本机已配置密钥":placeholder}
          autoComplete="off" value={config[kind][field]} onChange={e=>change({...config,[kind]:{...config[kind],[field]:e.target.value}})}/></label>)}
        <label className="field">计费口径<select value={config[kind].billing} onChange={e=>change({...config,[kind]:{...config[kind],billing:e.target.value}})}><option value="auto">自动识别</option><option value="openai">OpenAI 缓存字段</option><option value="deepseek">DeepSeek 缓存字段</option><option value="flat">供应商统一输入费率</option></select></label></div>
    </section>)}
    <section><div className="headrow"><h3>联网搜索</h3><button className="quiet" disabled={!!checking} onClick={()=>test("search")}>{checking==="search"?"正在测试…":"测试搜索"}</button></div>
      <label className="field">Tavily API Key（可选）<input type="password" value={config.search_key} autoComplete="off" placeholder="留空使用公开检索" onChange={e=>change({...config,search_key:e.target.value})}/></label>
      <label className="field">Tavily 费率（美元 / 积分）<input type="number" min="0" step="any" value={config.search_credit_price} onChange={e=>change({...config,search_credit_price:e.target.value})}/></label>
      <small>公开检索按查询使用 arXiv、OpenAlex、Bing RSS 或维基百科。费用预算只计算模型调用。</small></section>
    {message&&<p role="status" className="status-line">{message}</p>}
  </dialog>;
}

function Metrics({usage, live}) {
  if(!usage) return null;
  return <div className="metrics"><span>总耗时 {usage.seconds} 秒</span><span>输入 / 输出 {tokens(usage.input)} / {tokens(usage.output)} Token</span><span>{usage.unpriced_calls?`模型已知小计 ${fee(usage.known_usd)} · ${usage.unpriced_calls} 次无法估算`:`模型费用 ${fee(usage.usd)}`}</span>
    {usage.search_calls?.length>0&&<span>搜索费用 {fee(usage.search_known_usd)}{usage.search_unpriced_calls>0?` · ${usage.search_unpriced_calls} 次无法估算`:""}</span>}
    {live?<><span>{usage.remaining_seconds!=null?`剩余 ${usage.remaining_seconds} 秒`:"时间不限"}</span><span>{usage.remaining_tokens!=null?`剩余 ${tokens(usage.remaining_tokens)} Token`:"Token 不限"}</span>{usage.remaining_usd!=null&&<span>剩余 {fee(usage.remaining_usd)}</span>}</>:<><span>检索 {usage.search_seconds} 秒</span><span>实验 {usage.experiment_seconds} 秒</span></>}
  </div>;
}

function ExperimentTable({experiments}) {
  if(!experiments?.length) return null;
  return <div className="table-wrap"><table><thead><tr>{["实验 / 候选","数据","正确性","基准 ms","候选 ms","加速比"].map(x=><th key={x}>{x}</th>)}</tr></thead><tbody>
    {experiments.flatMap(item=>item.datasets.map(row=><React.Fragment key={item.id+row.dataset}><tr><td>{item.id} · {item.candidate}</td><td>{row.dataset}</td><td>{row.passed?"通过":"未通过"}</td><td>{row.baseline_ms??"—"}</td><td>{row.candidate_ms??"—"}</td><td>{row.speedup==null?"—":`${row.speedup}×`}</td></tr>
      {row.reason&&<tr><td colSpan="6" className="empty-row">{row.reason}</td></tr>}</React.Fragment>))}
  </tbody></table></div>;
}

function RoundReviews({rounds, live}) {
  if (!rounds?.length) return null;
  const refs = ids => ids?.map(id => `[${id}]`).join(" ");
  return <section aria-label="逐轮核查"><h4>逐轮核查（{rounds.length} 轮）</h4>{rounds.map(row=><details key={row.round} open={live||undefined}>
    <summary>第 {row.round} 轮 · {row.goal}</summary><div className="detail">
    {row.attempts?.map((attempt,i)=><div key={i}><strong>{attempt.channel} · {attempt.query}</strong>
      {attempt.lookup&&<p>{attempt.lookup}</p>}
      {attempt.sources?.length>0&&<p>本次来源：{refs(attempt.sources)}</p>}
      {attempt.issues?.map((issue,j)=><p className="notice" key={j}>{safeLink(issue.url)?<a href={safeLink(issue.url)} target="_blank" rel="noreferrer">{issue.message}</a>:issue.message}</p>)}</div>)}
    {row.experiment&&<p>实测依据：{row.experiment}</p>}
    {row.supported?.map((item,i)=><p key={i}>已有支持：{item.claim} {refs(item.refs)}</p>)}
    {row.gaps?.map((gap,i)=><p key={i}>待解决：{gap}</p>)}
    {row.conflicts?.map((conflict,i)=><p key={i}>证据冲突：{conflict}</p>)}
    {row.next&&<p><strong>下一步：</strong>{row.next}</p>}
    </div></details>)}</section>;
}

function RunCard({run, live=false}) {
  const [pdfStatus,setPdfStatus]=useState("");
  async function downloadPdf(e) {
    e.preventDefault();
    if(pdfStatus==="正在编译 PDF…")return;
    setPdfStatus("正在编译 PDF…");
    try {
      const response=await fetch(e.currentTarget.href);
      if(!response.ok){const error=await response.json();throw Error(error.detail||"PDF 编译失败。");}
      const url=URL.createObjectURL(await response.blob());
      const a=document.createElement("a");a.href=url;a.download=`research-${run.id}.pdf`;a.click();setTimeout(()=>URL.revokeObjectURL(url),5000);
      setPdfStatus("");
    }catch(error){setPdfStatus(`${error.message} 可先下载 LaTeX 源码。`);}
  }
  return <article className="card"><div className="headrow"><h3>{policyLabel[run.policy]||"研究"}</h3><span className="phase" role="status">{live?(run.phase||"正在连接…"):(statusLabel[run.status]||"研究完成")}</span></div>
    <Metrics usage={run.usage} live={live}/>
    {run.abstract&&<p className="copy"><strong>摘要</strong> {run.abstract}</p>}
    {run.detail&&run.detail!==run.end_reason&&<p className="notice">{run.detail}</p>}
    {run.notices?.filter(x=>!run.rounds?.some(row=>row.attempts?.some(attempt=>attempt.issues?.some(issue=>issue.message===x)))).map((x,i)=><p key={i} className="notice">{x}</p>)}
    {run.plan?.criterion&&<small>验收标准 · {run.plan.criterion}</small>}
    {live&&run.query&&<small>检索 · {run.query}</small>}
    <RoundReviews rounds={run.rounds} live={live}/>
    {run.end_reason&&<p className={run.end_reason===run.detail?"notice":"copy"}><strong>结束原因：</strong>{run.end_reason}</p>}
    {run.unresolved?.length>0&&<p className="copy"><strong>仍待解决：</strong>{run.unresolved.join("；")}</p>}
    {(run.reused_sources>0||run.reused_experiments>0)&&<small>本次复用 {run.reused_sources} 条证据，参考 {run.reused_experiments} 条既有实验记录。</small>}
    {run.hypotheses?.length>0&&<details><summary>假设与判定标准（{run.hypotheses.length}）</summary><div className="detail">{run.hypotheses.map(h=><div key={h.round}><strong>第 {h.round} 轮</strong> {h.text} {h.refs.map(r=>`[${r}]`).join(" ")}<p>预期：{h.expected}。判定：{h.criterion}</p></div>)}</div></details>}
    <ExperimentTable experiments={run.experiments}/>
    {run.revisions?.map(x=><p key={x.round} className="copy"><strong>第 {x.round} 轮 · 修正</strong> {x.reason}{x.based_on?`（依据 ${x.based_on}）`:""}</p>)}
    {run.best&&<p>两组数据均验证通过的最佳候选 · <strong>{run.best}</strong></p>}
    {run.summary&&<p className="copy">{run.summary}</p>}
    {run.design&&<details><summary>后续实验设计（待验证）</summary><div className="detail">{Object.entries({hypothesis:"假设",control:"对照组",variables:"数据与变量",metric:"评价指标",criterion:"判定标准"}).map(([key,label])=><p key={key}>{label} · {run.design[key]}</p>)}</div></details>}
    {run.sources?.length>0&&<details open={live||undefined}><summary>证据来源（{run.sources.length}）</summary><ol className="sources">{run.sources.map((x,i)=><li key={x.url+i}><a href={safeLink(x.url)} target="_blank" rel="noreferrer">{x.title}</a> <small>{x.basis}</small></li>)}</ol></details>}
    {run.usage?.calls?.length>0&&<details><summary>分阶段用量与模型调用（{run.usage.calls.length}）</summary><div className="detail">
      {Object.entries(run.usage.stages||{}).map(([stage,x])=><small key={stage}>{stage} · {x.calls} 次 · {tokens(x.input)}/{tokens(x.output)} Token · {Number(x.seconds).toFixed(2)} 秒</small>)}
      {run.usage.calls.map((x,i)=><small key={i}>{x.stage} · {x.provider&&`${x.provider} / `}{x.model} · {x.tier==="strong"?"强模型":"普通模型"} · {tokens(x.input)}/{tokens(x.output)} Token{x.cached_input!=null?`（缓存 ${tokens(x.cached_input)}）`:""}{x.usage_estimated?"（预留估算）":""} · {x.seconds} 秒 · {fee(x.usd)}{x.cost_reason?` · ${x.cost_reason}`:""}{x.usage_reason?` · ${x.usage_reason}`:""}{x.rate_source?` · 费率来源 ${x.rate_source}`:""}{x.reason?` · ${x.reason}`:""}</small>)}</div></details>}
    {run.id&&<div className="actions"><a className="quiet link-btn" href={`/api/lab/reports/${run.id}.pdf`} onClick={downloadPdf}>下载 PDF</a><a className="link-btn" href={`/api/lab/reports/${run.id}.tex`}>下载 LaTeX 源码</a><a className="link-btn" href={`/api/lab/runs/${run.id}.json`}>下载研究记录</a></div>}
    {pdfStatus&&<p role="status" className="status-line">{pdfStatus}</p>}
  </article>;
}

function Comparison({runs}) {
  const a=runs.find(x=>x.policy==="adaptive"), b=runs.find(x=>x.policy==="strong");
  if(!a||!b) return null;
  return <article className="card"><h3>调用策略对照</h3><div className="table-wrap"><table><thead><tr><th>策略</th><th>状态</th><th>Token</th><th>模型费用</th><th>总耗时</th><th>两组中较低加速比</th></tr></thead><tbody>
    {[a,b].map(r=>{const best=r.experiments.find(x=>x.id===r.best);return <tr key={r.id}><td>{policyLabel[r.policy]}</td><td>{statusLabel[r.status]}</td><td>{tokens(r.usage.tokens)}</td><td>{fee(r.usage.usd)}</td><td>{r.usage.seconds} 秒</td><td>{best?`${Math.min(...best.datasets.map(x=>x.speedup))}×`:`${r.sources.length} 个来源`}</td></tr>;})}
    </tbody></table></div></article>;
}

function App() {
  const [config,setConfig]=useState({fast:{...blankEndpoint},strong:{...blankEndpoint},search_key:"",search_credit_price:"",deepseek_configured:false});
  const [loadingConfig,setLoadingConfig]=useState(true);
  const [limits,setLimits]=useState(blankLimits);
  const [scenario,setScenario]=useState("general"), [policy,setPolicy]=useState("adaptive");
  const [sessions,setSessions]=useState([]), [selected,setSelected]=useState(null);
  const [runs,setRuns]=useState([]), [legacy,setLegacy]=useState([]), [pending,setPending]=useState(null);
  const [input,setInput]=useState(""), [error,setError]=useState(""), [sidebar,setSidebar]=useState(false);
  const [deleteTarget,setDeleteTarget]=useState(null), [deleteError,setDeleteError]=useState(""), [deleting,setDeleting]=useState(false);
  const dialog=useRef(null), deleteDialog=useRef(null), scroll=useRef(null), socket=useRef(null), nav=useRef(0), configChanged=useRef(false);
  const listRequest=useRef(0), deleteRequest=useRef(false);
  const busy=!!pending;
  async function refresh(){
    const sequence=++listRequest.current;
    try {
      const response=await fetch("/api/lab/sessions", {cache:"no-store"});
      if (!response.ok) return;
      const rows=await response.json();
      if (sequence===listRequest.current) setSessions(rows);
    } catch {}
  }
  useEffect(()=>{
    refresh();
    fetch("/api/lab/defaults",{cache:"no-store"}).then(response=>{
      if(!response.ok) throw Error();
      return response.json();
    }).then(defaults=>{
      if(!configChanged.current) setConfig(current=>({...current,fast:{...blankEndpoint,...defaults.fast},
        strong:{...blankEndpoint,...defaults.strong},deepseek_configured:!!defaults.deepseek_configured}));
    }).catch(()=>setError("无法读取默认模型设置，请重新加载页面或手动配置模型。"))
      .finally(()=>setLoadingConfig(false));
    return ()=>socket.current?.close();
  },[]);
  useEffect(()=>{scroll.current?.scrollIntoView({block:"end"});},[runs,pending,legacy]);
  function resetSession(id) {
    const seq=++nav.current;
    setSidebar(false);setError("");setSelected(id);setRuns([]);setLegacy([]);setInput("");
    return seq;
  }
  async function load(id){
    if(busy)return;
    const seq=resetSession(id);
    if(!id){setScenario("general");return;}
    try{const r=await fetch(`/api/lab/sessions/${id}`, {cache:"no-store"});if(!r.ok)throw Error();const data=await r.json();
      if(seq===nav.current){setRuns(data.runs);setLegacy(data.turns);setScenario(data.runs.at(-1)?.scenario||"general");}}
    catch{if(seq===nav.current)setError("无法读取会话。");}
  }
  function confirmDelete(item) {
    setDeleteTarget(item);
    setDeleteError("");
    deleteDialog.current.showModal();
  }
  async function removeSession() {
    if (!deleteTarget || deleteRequest.current) return;
    deleteRequest.current=true;
    setDeleting(true);
    setDeleteError("");
    try {
      const response=await fetch(`/api/lab/sessions/${deleteTarget.id}`, {method:"DELETE"});
      if (!response.ok) {
        const problem=await response.json().catch(()=>null);
        setDeleteError(typeof problem?.detail==="string" ? problem.detail : "删除失败，请稍后重试。");
        return;
      }
      ++listRequest.current;
      setSessions(old=>old.filter(item=>item.id!==deleteTarget.id));
      if (selected===deleteTarget.id) {
        resetSession(null);
        setScenario("general");
      }
      deleteDialog.current.close();
    } catch {
      setDeleteError("无法连接本地服务，请稍后重试。");
    } finally {
      deleteRequest.current=false;
      setDeleting(false);
    }
  }
  function send(e){
    e.preventDefault();const question=input.trim();if(!question||socket.current)return;
    if(loadingConfig)return;
    const invalidBudget=budgetError(limits);
    if(invalidBudget){setError(invalidBudget);return;}
    if(!config.fast.model&&!config.strong.model){dialog.current.showModal();return;}
    setInput("");setError("");++nav.current;
    setPending({question,cards:{}});
    const ws=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}/ws/research`);
    const group=Date.now();
    socket.current=ws;
    let terminal=false;
    ws.onopen=()=>ws.send(JSON.stringify({question,scenario,policy,session_id:selected,settings:payload(config),
      limits:{rounds:Number(limits.rounds),seconds:limits.seconds===""?null:Number(limits.seconds),tokens:limits.tokens===""?null:Number(limits.tokens),usd:limits.usd===""?null:Number(limits.usd)}}));
    ws.onmessage=e=>{
      const {type,data,policy:which,message,session_id,code}=JSON.parse(e.data);
      if(type==="complete"||type==="error"){
        terminal=true;
        if(type==="complete"){setSelected(session_id);refresh();}
        else {setError(message);if(code==="validation")setInput(question);}
        setPending(null);return;
      }
      if(type==="result"){
        setRuns(old=>[...old,{...data,group:data.group||group}]);
        setPending(old=>{if(!old)return old;const cards={...old.cards};delete cards[which];return {...old,cards};});return;
      }
      setPending(old=>{
        if(!old)return old;
        const item={policy:which,...old.cards[which]};
        if(type==="phase")item.phase=data.label;
        if(type==="usage")item.usage=data;
        if(type==="plan")item.plan=data;
        if(type==="search")item.query=data.query;
        if(type==="finding")item.sources=[...(item.sources||[]).filter(x=>x.url!==data.url),data];
        if(type==="finding_removed")item.sources=(item.sources||[]).filter(x=>x.url!==data.url);
        if(type==="notice")item.notices=[...(item.notices||[]),data.message];
        if(type==="round_review")item.rounds=[...(item.rounds||[]).filter(row=>row.round!==data.round),data].sort((a,b)=>a.round-b.round);
        if(type==="experiment")item.experiments=[...(item.experiments||[]),data];
        if(type==="hypothesis")item.hypotheses=[...(item.hypotheses||[]),data];
        if(type==="revision")item.revisions=[...(item.revisions||[]),data];
        return {...old,cards:{...old.cards,[which]:item}};
      });
    };
    ws.onclose=()=>{socket.current=null; if(!terminal){setError("连接已关闭。可从左侧重新打开会话查看已保存的结果。");setPending(null);refresh();}};
  }
  const groupRuns=runs.reduce((groups,run)=>{const key=run.group||run.id;const last=groups.at(-1);if(last?.key===key)last.items.push(run);else groups.push({key,items:[run]});return groups;},[]);
  return <div className="layout"><aside className={sidebar?"open":""}><div className="logo"><span className="logo-mark" aria-hidden="true">◆</span><div>Research Agent</div></div>
    <button className="primary" disabled={busy} onClick={()=>load(null)}>＋ 新研究</button><small>研究会话</small>
    <nav className="history" aria-label="研究会话">{sessions.map(item=>{
      const running=busy&&selected===item.id;
      return <div className="session-row" key={item.id}>
        <button className={selected===item.id?"active":""} disabled={busy} title={item.title} onClick={()=>load(item.id)}>{item.title}</button>
        {running?<small className="session-running" title="研究执行期间无法删除此会话">研究中</small>:
          <button className="session-delete" aria-label={`删除会话 ${item.title}`} title="删除会话" onClick={()=>confirmDelete(item)}>×</button>}
      </div>;
    })}</nav>
    <button className="quiet mobile" onClick={()=>setSidebar(false)}>关闭会话列表</button>
    <button className="quiet" onClick={()=>{setSidebar(false);dialog.current.showModal();}}>模型与搜索设置</button>
  </aside><main><header><button className="quiet mobile" aria-label="打开会话列表" onClick={()=>setSidebar(!sidebar)}>☰</button><div><strong>深度研究助手</strong><small>{selected?"在当前会话继续研究":"从研究问题到可复现报告"}</small></div></header>
    <div className="scroll"><div className="column">
      {!runs.length&&!legacy.length&&!pending&&<div className="welcome"><h1>你想研究什么？</h1><p>提交一个课题，查看证据、验证过程和最终报告。</p><div className="examples"><button className="quiet" onClick={()=>{setScenario("market");setInput("如何加速行情事件分组聚合，同时保证成交量和事件数一致？");}}>试跑行情聚合实验</button><button className="quiet" onClick={()=>{setScenario("general");setInput("VGGT4D 如何分离动态物体与静态背景？请给出原论文来源。");}}>研究 VGGT4D</button></div></div>}
      {legacy.map((t,i)=><div key={i} className={t.role==="user"?"question":"card copy"}>{t.content}</div>)}
      {groupRuns.map(g=><div className="run" key={g.key}><div className="question">{g.items[0].question}</div>{g.items.map(r=><RunCard run={r} key={r.id}/>)}<Comparison runs={g.items}/></div>)}
      {pending&&<div className="run">{!groupRuns.some(g=>g.items[0].question===pending.question)&&<div className="question">{pending.question}</div>}{Object.values(pending.cards).map(r=><RunCard key={r.policy} run={r} live/>)}{!Object.keys(pending.cards).length&&<small>正在处理研究请求…</small>}</div>}
      {error&&<p role="alert" className="notice">{error}</p>}<div ref={scroll}/>
    </div></div>
    <form className="composer" onSubmit={send} noValidate><div className="column composer-card"><div className="compose-main"><textarea aria-label="研究问题" placeholder={selected?"针对已有报告追问或补充角度…":"输入一个研究问题…"} required maxLength="2500" value={input} disabled={busy} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();e.currentTarget.form.requestSubmit();}}}/><button className="primary" disabled={loadingConfig||busy||!input.trim()}>{busy?"研究中…":"开始研究"}</button></div>
      <div className="options"><label>场景<select disabled={busy} value={scenario} onChange={e=>setScenario(e.target.value)}><option value="general">开放调研</option><option value="market">行情聚合实验</option></select></label><label>策略<select disabled={busy} value={policy} onChange={e=>setPolicy(e.target.value)}><option value="adaptive">按需升级</option><option value="strong">每步强模型</option><option value="compare">对照运行</option></select></label>
        {budgetFields.map(([field,label,min])=><label key={field}>{label}<input type="number" min={min} step={field==="usd"?"any":1} required={field==="rounds"} placeholder={field==="rounds"?undefined:"可选，留空不限制"} disabled={busy} value={limits[field]} onChange={e=>setLimits({...limits,[field]:e.target.value})}/></label>)}</div>
      {policy==="compare"&&<small>两个策略依次运行，各自使用同一套预算和验收标准。</small>}
    </div></form>
  </main><Settings dialog={dialog} config={config} change={value=>{configChanged.current=true;setConfig(value);}}/>
  <dialog ref={deleteDialog} className="settings delete-dialog" aria-labelledby="delete-title" aria-describedby="delete-description"
    onCancel={event=>{if(deleteRequest.current)event.preventDefault();}}>
    <h2 id="delete-title">删除会话</h2>
    <p id="delete-description">删除「{deleteTarget?.title}」及其中的追问、研究记录和报告。此操作不可撤销。</p>
    {deleteError&&<p role="alert" className="notice">{deleteError}</p>}
    <div className="actions">
      <button type="button" className="quiet" autoFocus disabled={deleting} onClick={()=>deleteDialog.current.close()}>取消</button>
      <button type="button" className="danger" disabled={deleting} onClick={removeSession}>{deleting?"正在删除…":"确认删除"}</button>
    </div>
  </dialog></div>;
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
