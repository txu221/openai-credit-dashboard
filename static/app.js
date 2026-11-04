'use strict';
const $ = id => document.getElementById(id);
let lang = localStorage.getItem('credit-language') || 'zh-CN';
let dark = localStorage.getItem('credit-theme') === 'dark';
let summary;
const words = {
 'zh-CN': {private:'● 本地优先',title:'让每一分额度，都用在创造上。',subtitle:'了解花费，掌握节奏，在额度到期前从容规划。',refresh:'↻ 刷新数据',balance:'可用额度',ofGrant:'初始赠金',spent:'已使用',usage:'额度使用进度',yourCredit:'一点投入，无限可能。',runway:'额度倒计时',days:'天',expires:'到期日期',forecastNote:'按最近 7 个完整日的平均花费预测',burn:'日均花费 · 近 7 日',burnHint:'包含无花费的日期',projected:'到期时预计总花费',projectedHint:'已花费 + 预计未来花费',tokens:'累计 Token',tokenHint:'输入与输出，缓存包含在输入中',projects:'活跃项目',projectHint:'当前赠金周期内',daily:'每日花费',dailyHint:'看见每一次探索的投入',last30:'最近 30 天 · USD',models:'模型分布',modelHint:'按 Token 价格估算 · USD',byProject:'项目花费',projectSub:'每个想法，都有迹可循',topDays:'高花费日期',topHint:'投入最多的 10 天',date:'日期',cost:'花费 / USD',share:'占总花费',about:'关于这些数字',footer:'本地运行 · 开源 · 属于你的数据',timezone:'统计时区 UTC · USD',empty:'暂无花费记录',local:'● 本地日志 · 估算',official:'● 官方账单',estimate:'估算 · USD',updated:'更新于 ',loading:'正在读取数据…',error:'无法加载数据：',fallback:'使用默认价格的模型：',localNote:'本地花费为公开 API 价格估算，不是实际账单。Codex 订阅会话可能并不消耗 API 赠金。仅统计已保存的日志，不含工具费用、长上下文或服务层级等价格调整。',officialNote:'总花费来自组织 Costs API，可能延迟；它不是实际赠金余额。模型花费按 completions Token 估算，可能与账单总额不一致。',badLog:'部分日志不存在、无法读取或格式无效，请检查配置。',fallbackNote:'部分模型使用默认价格，请编辑 pricing.json。',expired:'额度已到期',exhausted:'额度已用完'},
 en:{private:'● LOCAL FIRST',title:'Make every credit count.',subtitle:'Understand your spend. Find your pace. Build before your credits expire.',refresh:'↻ Refresh data',balance:'Available credit',ofGrant:'Original grant',spent:'Spent',usage:'Grant used',yourCredit:'A little credit. A world of possibilities.',runway:'Time remaining',days:'days',expires:'Expires on',forecastNote:'Based on the last 7 completed days',burn:'Daily burn · last 7 days',burnHint:'Includes zero-spend days',projected:'Projected spend at expiry',projectedHint:'Spent so far + forecast future spend',tokens:'Total tokens',tokenHint:'Input + output; cache is included in input',projects:'Active projects',projectHint:'Within this grant period',daily:'Daily spend',dailyHint:'The cost of a little curiosity',last30:'LAST 30 DAYS · USD',models:'By model',modelHint:'Token-price estimates · USD',byProject:'By project',projectSub:'Follow the ideas you invest in',topDays:'Top spending days',topHint:'Your 10 highest-spend days',date:'Date',cost:'Spend / USD',share:'Share of spend',about:'About these numbers',footer:'Local · Open source · Your data stays yours',timezone:'UTC reporting · USD',empty:'No spending records yet',local:'● LOCAL LOGS · ESTIMATE',official:'● OFFICIAL COSTS',estimate:'ESTIMATE · USD',updated:'Updated ',loading:'Reading your data…',error:'Unable to load data: ',fallback:'Default pricing used for: ',localNote:'Local spend uses public API-price estimates, not actual billing. Codex subscription sessions may not consume API credit. Saved logs only; tool fees, long-context and service-tier adjustments are excluded.',officialNote:'Totals come from organization Costs API and may lag; this is not a live gift-credit balance. Model costs are estimates from completions tokens and may not match billed totals.',badLog:'Some logs are missing, unreadable or invalid. Check your configuration.',fallbackNote:'Some models use default rates; edit pricing.json.',expired:'Credit has expired',exhausted:'Credit used up'}
};
const t = key => words[lang][key];
const money = value => new Intl.NumberFormat(lang,{style:'currency',currency:'USD',currencyDisplay:'narrowSymbol',maximumFractionDigits:2}).format(value);
function element(tag, text, className){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;}
function translations(){
 document.documentElement.lang=lang;
 document.querySelectorAll('[data-i18n]').forEach(e=>e.textContent=t(e.dataset.i18n));
 $('language').textContent=lang==='en'?'中文':'EN';
 $('theme').setAttribute('aria-label',lang==='en'?'Toggle light/dark theme':'切换明暗主题');
 document.body.classList.toggle('dark',dark);
 if(summary)render(summary);
}
function forecast(s){
 if(s.status==='expired')return lang==='en'?`~${money(s.remaining)} expired unused`:`约 ${money(s.remaining)} 未使用，已到期`;
 if(s.status==='exhausted')return t('exhausted');
 if(s.depletion_date)return lang==='en'?`Will use it up by ${s.depletion_date}`:`预计于 ${s.depletion_date} 用完`;
 return lang==='en'?`~${money(s.unused_at_expiry)} will expire unused`:`约 ${money(s.unused_at_expiry)} 将到期未使用`;
}
function breakdown(id, rows){
 const root=$(id);root.replaceChildren();
 if(!rows.length){root.append(element('div',t('empty'),'empty'));return;}
 const max=Math.max(...rows.map(r=>r.cost),0.000001);
 rows.forEach((row,i)=>{
  const entry=element('div',undefined,'break-row');
  const info=element('div',undefined,'break-info');
  info.append(element('span',row.name,'break-name'),element('span',money(row.cost),'break-value'));
  const track=element('div',undefined,'break-track'),fill=element('div',undefined,`break-fill color-${i%5}`);
  fill.style.width=`${Math.max(0,row.cost)/max*100}%`;track.append(fill);entry.append(info,track);root.append(entry);
 });
}
function chart(rows){
 const root=$('chart');root.replaceChildren();rows=rows.slice(-30);
 if(!rows.some(r=>r.cost>0)){root.append(element('div',t('empty'),'empty'));return;}
 const NS='http://www.w3.org/2000/svg';
 const make=(tag,attrs,text)=>{const e=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v));if(text!==undefined)e.textContent=text;return e;};
 const plotWidth=Math.max(280,root.clientWidth);
 const svg=make('svg',{viewBox:`0 0 ${plotWidth} 218`,role:'img','aria-label':t('daily')});
 svg.append(make('title',{},t('daily')));
 const max=Math.max(...rows.map(r=>r.cost))*1.15, left=40, width=plotWidth-52, height=158;
 for(let i=0;i<=4;i++){
  const y=12+height-i*height/4;
  svg.append(make('line',{x1:left,x2:plotWidth-8,y1:y,y2:y,class:'grid'}));
  svg.append(make('text',{x:32,y:y+4,'text-anchor':'end'},(max*i/4).toFixed(max<1?2:1)));
 }
 const step=width/rows.length;
 const tickCount=plotWidth<400?4:5;
 const labels=new Set(Array.from({length:tickCount},(_,i)=>Math.round(i*(rows.length-1)/(tickCount-1))));
 rows.forEach((row,i)=>{
  const h=Math.max(0,row.cost)/max*height;
  const rect=make('rect',{x:left+i*step+step*.2,y:12+height-h,width:step*.6,height:h,rx:2,class:i===rows.length-1?'bar latest':'bar',tabindex:0});
  rect.append(make('title',{},`${row.date}: ${money(row.cost)}`));svg.append(rect);
  if(labels.has(i))svg.append(make('text',{x:left+(i+.5)*step,y:194,'text-anchor':'middle'},row.date.slice(5).replace('-','/')));
 });root.append(svg);
}
function render(s){
 $('source').textContent=t(s.mode==='local'?'local':'official');
 $('range').textContent=`${s.grant_start} → ${s.expiry} · UTC`;
 $('updated').textContent=t('updated')+new Date(s.updated_at).toLocaleTimeString(lang,{hour:'2-digit',minute:'2-digit'});
 $('estimate').textContent=s.estimated?t('estimate'):'USD';
 for(const [id,value] of Object.entries({remaining:s.remaining,grant:s.grant_total,spent:s.spent,burn:s.avg_daily_burn,projected:s.projected_spend}))$(id).textContent=money(value);
 $('percent').textContent=s.percent_used.toFixed(1)+'%';$('progress').value=Math.min(100,Math.max(0,s.percent_used));
 $('progress').setAttribute('aria-label',`${t('usage')} ${s.percent_used}%`);
 $('days').textContent=s.days_left;$('expiry').textContent=s.expiry;$('forecast').textContent=forecast(s);
 $('tokens').textContent=new Intl.NumberFormat(lang,{notation:'compact',maximumFractionDigits:1}).format(s.tokens.input_tokens+s.tokens.output_tokens);
 $('projectCount').textContent=s.by_project.length;
 breakdown('models',s.by_model);breakdown('projects',s.by_project);chart(s.daily);
 const tbody=$('topDays');tbody.replaceChildren();
 s.top_days.filter(r=>r.cost!==0).forEach(row=>{const tr=element('tr');tr.append(element('td',row.date),element('td',money(row.cost)),element('td',s.spent>0?(row.cost/s.spent*100).toFixed(1)+'%':'—'));tbody.append(tr);});
 if(!tbody.children.length){const tr=element('tr'),td=element('td',t('empty'),'empty');td.colSpan=3;tr.append(td);tbody.append(tr);}
 let note=t(s.mode==='local'?'localNote':'officialNote');
 if(s.warnings.some(w=>/skipped|could not|not found/.test(w)))note+=' '+t('badLog');
 if(s.fallback_models.length)note+=' '+t('fallbackNote');
 $('notes').textContent=note;
 $('fallback').textContent=s.fallback_models.length?t('fallback')+s.fallback_models.join(', '):'';
 $('dashboard').setAttribute('aria-busy','false');
}
async function load(){
 $('refresh').disabled=true;
 try{
  const res=await fetch('/api/summary',{cache:'no-store'}),data=await res.json();
  if(!res.ok)throw new Error(data.error||`HTTP ${res.status}`);
  summary=data;$('error').hidden=true;render(summary);
 }catch(err){$('error').textContent=t('error')+err.message;$('error').hidden=false;$('dashboard').setAttribute('aria-busy','false');}
 finally{$('refresh').disabled=false;}
}
$('language').addEventListener('click',()=>{lang=lang==='en'?'zh-CN':'en';localStorage.setItem('credit-language',lang);translations();});
$('theme').addEventListener('click',()=>{dark=!dark;localStorage.setItem('credit-theme',dark?'dark':'light');translations();});
$('refresh').addEventListener('click',load);
translations();load();setInterval(load,60000);

window.addEventListener('resize',()=>{if(summary)chart(summary.daily);});
