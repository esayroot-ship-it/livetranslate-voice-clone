const $=(s,r=document)=>r.querySelector(s);const $$=(s,r=document)=>[...r.querySelectorAll(s)];
const api=async(url,options={})=>{const opt={...options,headers:{"Content-Type":"application/json",...(options.headers||{})}};const res=await fetch(url,opt);let data={};try{data=await res.json()}catch{}if(!res.ok)throw new Error(data.detail||`HTTP ${res.status}`);return data};
function toast(message,bad=false){const el=$("#toast");el.textContent=message;el.className=`toast ${bad?"bad":""}`;el.style.display="block";clearTimeout(window.__toast);window.__toast=setTimeout(()=>el.style.display="none",4200)}
async function busy(button,work){const old=button.innerHTML;button.disabled=true;button.innerHTML='<span class="loader"></span> 处理中';try{return await work()}finally{button.disabled=false;button.innerHTML=old}}
function nav(){const page=location.pathname.split('/').pop()||'index.html';$$('.nav a').forEach(a=>a.classList.toggle('active',a.getAttribute('href')===page))}
async function health(){try{const h=await api('/api/health');$('#service-dot')?.classList.add('good');if($('#service-text'))$('#service-text').textContent=h.running?'服务在线 · 翻译运行中':'服务在线 · 待机'}catch{$('#service-dot')?.classList.add('bad');if($('#service-text'))$('#service-text').textContent='服务断开'}}
function nested(obj,path,value){const parts=path.split('.');let cur=obj;parts.slice(0,-1).forEach(k=>cur=cur[k]??=( {}));cur[parts.at(-1)]=value}
function valueAt(obj,path){return path.split('.').reduce((a,k)=>a?.[k],obj)}
function fillForm(config,root=document){$$('[data-path]',root).forEach(el=>{const v=valueAt(config,el.dataset.path);if(el.type==='checkbox')el.checked=!!v;else if(v!==undefined&&v!==null)el.value=typeof v==='object'?JSON.stringify(v,null,2):v})}
function collectForm(root=document){const result={};$$('[data-path]',root).forEach(el=>{let v=el.type==='checkbox'?el.checked:el.value;if(el.dataset.type==='number')v=Number(v);if(el.dataset.type==='json'){try{v=JSON.parse(v||'{}')}catch{throw new Error(`${el.previousElementSibling?.textContent||el.dataset.path} 不是有效 JSON`)}}nested(result,el.dataset.path,v)});return result}
async function saveForm(root=document){const result=await api('/api/config',{method:'PUT',body:JSON.stringify(collectForm(root))});toast('配置已安全保存');return result}
function fmtMs(v){return v===null||v===undefined?'—':`${Number(v).toFixed(1)} ms`}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':'&quot;',"'":"&#39;"}[c]))}
document.addEventListener('DOMContentLoaded',()=>{nav();health();setInterval(health,10000)});
