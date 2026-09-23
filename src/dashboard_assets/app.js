const $ = id => document.getElementById(id);
const money = n => n == null ? '—' : Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:6});
const qty = n => n == null ? '—' : Number(n).toLocaleString(undefined,{maximumFractionDigits:8});
const date = s => s ? new Date(s.includes('T') ? s : s.replace(' ','T')+'Z').toLocaleString() : '—';
const pageState=Object.fromEntries(['positions','wallets','orders','execution'].map(id=>[id,{page:1,size:10}]));
function pagination(id,records,columns){
  const host=$(id+'-pagination');if(!host)return;host.replaceChildren();
  const state=pageState[id],total=records.length,pages=Math.max(1,Math.ceil(total/state.size));
  const info=document.createElement('span');info.className='pagination-info';info.textContent=total?`Page ${state.page} of ${pages} · ${total} records`:'0 records';
  const controls=document.createElement('div');controls.className='pagination-controls';
  const move=(label,page,disabled)=>{const button=document.createElement('button');button.type='button';button.textContent=label;button.disabled=disabled;button.addEventListener('click',()=>{state.page=page;rows(id,records,columns);});return button;};
  controls.append(move('First',1,state.page===1),move('Previous',state.page-1,state.page===1));
  const select=document.createElement('select');select.setAttribute('aria-label',`${id} rows per page`);
  for(const size of [10,25,50,100]){const option=document.createElement('option');option.value=size;option.textContent=size+' / page';option.selected=size===state.size;select.append(option);}
  select.addEventListener('change',()=>{state.size=Number(select.value);state.page=1;rows(id,records,columns);});controls.append(select);
  controls.append(move('Next',state.page+1,state.page===pages),move('Last',pages,state.page===pages));host.append(info,controls);
}
function rows(id, records, columns){
  const body=$(id),state=pageState[id];body.replaceChildren();
  const pages=Math.max(1,Math.ceil(records.length/state.size));state.page=Math.min(Math.max(1,state.page),pages);
  if(!records.length){const tr=body.insertRow(),td=tr.insertCell();td.colSpan=columns.length;td.className='empty';td.textContent='Nothing to show yet';pagination(id,records,columns);return;}
  const start=(state.page-1)*state.size;
  for(const record of records.slice(start,start+state.size)){const tr=body.insertRow();for(const column of columns){const td=tr.insertCell();const value=column(record);if(value instanceof Node)td.append(value);else td.textContent=value??'—';}}
  pagination(id,records,columns);
}
function badge(text){const span=document.createElement('span');span.className='badge';span.textContent=text;return span;}
function gain(value){const span=document.createElement('span');span.className=value==null?'':value<0?'negative':'positive';span.textContent=money(value);return span;}
function stopLevel(t,enabled=true){const div=document.createElement('div');if(!enabled){div.textContent='Disabled';return div;}div.textContent=money(t.stop_loss);if(t.emergency_stop!=null){const small=document.createElement('small');small.textContent=t.stop_observations+' observations · emergency '+money(t.emergency_stop);div.append(small);}return div;}
const svg=(name,attrs={})=>{const node=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,value);return node;};
function renderChart(performance){
  const host=$('chart'),points=performance?.points||[];host.replaceChildren();
  if(!points.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='No closed-trade history yet';host.append(empty);return;}
  const W=1120,H=350,L=76,R=32,top=24,lineBottom=205,barTop=235,barBottom=310,inner=W-L-R;
  const chart=svg('svg',{viewBox:`0 0 ${W} ${H}`,'aria-hidden':'true'});host.append(chart);
  const totals=points.map(p=>p.total_value).filter(v=>v!=null),profits=points.map(p=>Number(p.daily_profit)||0);
  const minT=totals.length?Math.min(...totals):0,maxT=totals.length?Math.max(...totals):1,pad=Math.max((maxT-minT)*.15,1),lo=minT-pad,hi=maxT+pad;
  const maxP=Math.max(...profits.map(Math.abs),.01),x=i=>L+(points.length===1?inner/2:i*inner/(points.length-1));
  const yT=v=>top+(hi-v)/(hi-lo)*(lineBottom-top),zero=(barTop+barBottom)/2,yP=v=>zero-v/maxP*(barBottom-barTop)/2;
  for(const [y,label] of [[top,hi],[lineBottom,lo],[zero,0]]){const ln=svg('line',{x1:L,x2:W-R,y1:y,y2:y,class:'grid'});chart.append(ln);const tx=svg('text',{x:L-10,y:y+4,class:'axis','text-anchor':'end'});tx.textContent=money(label);chart.append(tx);}
  const barWidth=Math.max(3,Math.min(28,inner/points.length*.55));
  points.forEach((p,i)=>{const value=Number(p.daily_profit)||0,y=yP(value),rect=svg('rect',{x:x(i)-barWidth/2,y:Math.min(y,zero),width:barWidth,height:Math.max(1,Math.abs(zero-y)),class:value<0?'bar loss':'bar gain'});const title=svg('title');title.textContent=`${p.date} · Daily profit ${money(value)} USDC`;rect.append(title);chart.append(rect);});
  if(totals.length===points.length){const path=svg('path',{d:points.map((p,i)=>(i?'L':'M')+x(i)+' '+yT(p.total_value)).join(' '),class:'equity-line'});chart.append(path);points.forEach((p,i)=>{const dot=svg('circle',{cx:x(i),cy:yT(p.total_value),r:4,class:'equity-dot'}),title=svg('title');title.textContent=`${p.date} · Total value ${money(p.total_value)} USDC · Profit ${money(p.daily_profit)}`;dot.append(title);chart.append(dot);});}
  const every=Math.max(1,Math.ceil(points.length/7));points.forEach((p,i)=>{if(i%every&&i!==points.length-1)return;const tx=svg('text',{x:x(i),y:338,class:'axis','text-anchor':'middle'});tx.textContent=p.date.slice(5);chart.append(tx);});
  const lineLabel=svg('text',{x:L,y:16,class:'legend equity'});lineLabel.textContent='● Total value';chart.append(lineLabel);const profitLabel=svg('text',{x:L+120,y:16,class:'legend profit'});profitLabel.textContent='■ Daily realized profit';chart.append(profitLabel);
}
function render(data){
  const active=data.trades.filter(t=>!['CLOSED','FAILED'].includes(t.state));
  const complete=data.balances.length>0&&data.balances.every(b=>b.usdc!=null);
  $('cash').textContent=complete?money(data.balances.reduce((n,b)=>n+b.usdc,0)):'Unavailable';
  $('cost').textContent=money(active.reduce((n,t)=>n+(t.cost||0),0));
  const pnl=data.trades.reduce((n,t)=>n+(t.realized_pnl||0),0);$('pnl').textContent=money(pnl);$('pnl').className=pnl<0?'negative':'positive';
  $('active').textContent=active.length;
  const unrealized=data.valuation.unrealized_pnl;
  $('unrealized').textContent=unrealized==null?'Unavailable':money(unrealized);
  $('unrealized').className=unrealized==null?'':unrealized<0?'negative':'positive';
  $('walletcount').textContent=data.wallet_summary.total;
  const stats=data.trade_summary;
  $('wins').textContent=stats.wins;$('losses').textContent=stats.losses;
  $('takeprofits').textContent=stats.take_profit;$('stoplosses').textContent=stats.stop_loss;
  $('winrate').textContent=stats.win_rate_pct==null?'No closed trades yet':stats.win_rate_pct.toFixed(1)+'% win rate · all closed trades';
  $('closedcount').textContent=`${stats.closed} closed · ${stats.breakeven} breakeven · excludes gas`;
  $('otherexits').textContent=`${stats.other_exits} other/time exits · reason ≠ profitability`;
  renderChart(data.daily_performance);
  const perf=data.daily_performance||{};$('chartsummary').textContent=perf.current_total==null?'Daily realized profit · total unavailable':`Current total ${money(perf.current_total)} USDC`;
  $('chartnote').textContent=perf.methodology||'Historical totals are reconstructed from settled trade profit.';
  $('walletbreakdown').textContent=Object.entries(data.wallet_summary.by_status).map(([status,count])=>`${count} ${status}`).join(' · ')+' · excludes vault/deleted records';
  $('pricetime').textContent=data.valuation.fetched_at?'Backpack prices fetched '+date(data.valuation.fetched_at):'No reference prices needed or available';
  $('service').textContent=`${data.mode.toUpperCase()} · ${data.chain} · ${data.mode==='live'?'Service: '+data.service:'Simulation ledger'}`;
  $('updated').textContent='Snapshot: '+date(data.generated_at);
  $('warning').textContent=[...data.warnings,...(!data.database_available?['No database exists for this mode yet.']:[])].join(' ');
  rows('positions',active,[t=>t.symbol,t=>badge(t.state),t=>qty(t.quantity),t=>money(t.cost),t=>money(t.reference_price),t=>money(t.market_value),t=>gain(t.unrealized_pnl),t=>t.unrealized_pct==null?'—':t.unrealized_pct.toFixed(2)+'%',t=>stopLevel(t,data.stop_exits_enabled!==false),t=>money(t.take_profit),t=>t.session]);
  rows('wallets',data.balances.filter(b=>b.status!=='abandoned'),[b=>{const div=document.createElement('div');div.textContent=b.label+' · '+b.status+(b.status==='vault'?'':' · losses: '+b.loss_count);const small=document.createElement('small');small.textContent=b.address;div.append(small);return div;},b=>money(b.usdc),b=>qty(b.native)]);
  rows('orders',data.orders,[o=>date(o.created_at),o=>o.stock_ticker,o=>o.order_type.toUpperCase(),o=>money(o.amount_usdc),o=>qty(o.quantity),o=>badge(o.status),o=>money(o.profit_loss)]);
  rows('execution',data.execution||[],[o=>o.symbol+' '+o.kind,o=>o.rejection||(o.delivery_at&&o.state!=='SETTLED'?'DELIVERED / RECONCILING':o.state),o=>money(o.decision_price),o=>money(o.execution_quote),o=>date(o.submission_at),o=>date(o.settlement_at||o.delivery_at),o=>{const lag=o.recognition_delay_seconds??o.delivery_recognition_delay_seconds;return lag==null?'—':lag.toFixed(1)+'s';}]);
  $('health').replaceChildren();
  const diagnostics=data.health?{'Fresh price symbols':data.health.feed.fresh_count,...data.health.scan}:{Status:data.mode==='paper'?'Live service health is not applied to paper mode':'No health snapshot available'};
  for(const [key,value] of Object.entries(diagnostics)){const div=document.createElement('div'),label=document.createElement('span'),val=document.createElement('strong');label.textContent=key.replaceAll('_',' ');val.textContent=value??'—';div.append(label,val);$('health').append(div);}
  $('healthtime').textContent=data.health?'Log: '+data.health.logged_at+' (server local)':'—';
  $('schedule').textContent=`${data.schedule.exchange_timezone}: entry ${data.schedule.first_15m_complete}–${data.schedule.scan_end} · force exit ${data.schedule.force_exit}`;
}
let requestId=0;
async function refresh(){
  const id=++requestId,mode=$('mode').value;
  try{const response=await fetch('/api/state?mode='+mode,{signal:AbortSignal.timeout(30000)});if(!response.ok)throw Error('unavailable');const data=await response.json();if(id===requestId)render(data);}
  catch(error){if(id===requestId)$('warning').textContent='Refresh failed. Displayed data may be stale; retrying automatically.';}
}
$('mode').value=localStorage.getItem('fgv-mode')==='paper'?'paper':'live';
$('mode').addEventListener('change',()=>{localStorage.setItem('fgv-mode',$('mode').value);$('warning').textContent='Loading selected mode…';for(const id of Object.keys(pageState)){pageState[id].page=1;$(id).replaceChildren();$(id+'-pagination').replaceChildren();}for(const id of ['health','chart'])$(id).replaceChildren();for(const id of ['cash','cost','pnl','active','unrealized','walletcount','walletbreakdown','pricetime','wins','losses','takeprofits','stoplosses','winrate','closedcount','otherexits','chartsummary'])$(id).textContent='—';refresh();});
$('refresh').addEventListener('click',refresh);refresh();setInterval(refresh,10000);
