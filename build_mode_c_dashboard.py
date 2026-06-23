from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


THEME_RULES: list[dict[str, Any]] = [
    {
        "id": "ai_chip_second_order",
        "name": "AI 晶片二階受益鏈",
        "thesis": "AI 晶片需求若持續，受益者不只 GPU；先進製程、設備、記憶體、電源管理、散熱、連接器、被動元件與測試量測都要一起檢查。",
        "segments": ["晶片設計", "晶圓代工", "半導體設備", "HBM/記憶體", "電源/類比", "散熱/電力", "連接器/被動元件", "測試量測"],
        "tickers": ["NVDA", "AMD", "AVGO", "MRVL", "TSM", "ASML", "AMAT", "LRCX", "KLAC", "MU", "WDC", "STX", "ADI", "TXN", "MPWR", "MCHP", "ON", "APH", "TEL", "VSH", "GLW", "KEYS", "TER", "VRT", "ETN", "ANET"],
        "keywords": ["semiconductor", "semiconductor equipment", "memory", "electronic components", "communication equipment", "computer hardware", "electrical equipment", "specialty industrial machinery", "connectors", "passive", "analog", "power", "thermal", "testing", "measurement"],
        "questions": ["營收是否真的跟 AI/資料中心 capex 連動？", "供給是否吃緊到能維持毛利？", "這是必需品、規格升級，還是一次性拉貨？"],
    },
    {
        "id": "data_center_power_cooling",
        "name": "資料中心電力與散熱",
        "thesis": "AI cluster 功耗提高後，瓶頸可能從晶片轉到電力、UPS、配電、液冷、空調與機房工程。",
        "segments": ["電力設備", "UPS/配電", "液冷/散熱", "機房工程", "工業自動化"],
        "tickers": ["VRT", "ETN", "TT", "CARR", "JCI", "PWR", "HUBB", "EMR", "PH", "ROK"],
        "keywords": ["electrical equipment", "building products", "specialty industrial machinery", "engineering", "construction", "hvac", "thermal", "cooling", "automation"],
        "questions": ["訂單是否來自資料中心而非一般景氣循環？", "毛利是否因競爭加劇而回落？", "capex 週期反轉時營收會掉多深？"],
    },
    {
        "id": "electrification_grid",
        "name": "電氣化與電網升級",
        "thesis": "AI、EV、再工業化與電力需求成長可能推動電網、變壓器、配電與工業電氣設備。",
        "segments": ["電網設備", "配電", "工業電氣", "工程服務"],
        "tickers": ["ETN", "HUBB", "PWR", "EMR", "PH", "ROK", "TT", "VRT", "ABBNY", "SIEGY"],
        "keywords": ["electrical equipment", "utilities regulated electric", "engineering", "industrial", "automation", "specialty industrial machinery"],
        "questions": ["需求是長週期基建還是短期補庫存？", "公司是否有定價權與 backlog？", "估值是否已把多年成長一次反映？"],
    },
    {
        "id": "cybersecurity_ai_software",
        "name": "AI 軟體與資安防線",
        "thesis": "企業導入 AI 與雲端後，資安、資料治理、雲端平台與自動化軟體可能成為伴隨支出。",
        "segments": ["資安", "雲端平台", "資料治理", "企業自動化"],
        "tickers": ["PANW", "FTNT", "CRWD", "ZS", "NET", "DDOG", "SNOW", "NOW", "MSFT", "ADBE", "CRM"],
        "keywords": ["software - infrastructure", "software - application", "cybersecurity", "cloud", "data", "security"],
        "questions": ["ARR/留存率是否支持估值？", "SBC 是否吃掉 FCF？", "AI 是否提升護城河，還是壓低價格？"],
    },
    {
        "id": "healthcare_quality_defensive",
        "name": "高品質醫療與防守成長",
        "thesis": "當市場太集中在科技時，醫療器材、診斷、製藥與生命科學工具可作為品質型分散研究池。",
        "segments": ["醫療器材", "生命科學工具", "製藥", "診斷"],
        "tickers": ["LLY", "NVO", "ISRG", "TMO", "DHR", "SYK", "VRTX", "ABT", "MDT", "MRK", "JNJ"],
        "keywords": ["healthcare", "medical", "diagnostics", "drug manufacturers", "biotechnology", "life sciences"],
        "questions": ["專利/產品週期風險是否集中？", "估值是否已反映管線成功？", "Real FCF 與研發投入是否健康？"],
    },
]


def clean(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (bool, int, float)) else str(value)


def records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        {str(key): clean(value) for key, value in row.items()}
        for row in pd.read_csv(path).to_dict(orient="records")
    ]


def ticker(row: dict[str, Any]) -> str:
    return str(row.get("Ticker") or "").strip().upper()


def score(row: dict[str, Any]) -> float:
    try:
        return float(row.get("Long_Term_Score"))
    except (TypeError, ValueError):
        return -1.0


def row_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(field) or "")
        for field in ("Ticker", "Name", "Sector", "Industry", "Status", "Verdict", "Research_Action")
    ).lower()


def match_theme(row: dict[str, Any], theme: dict[str, Any]) -> bool:
    symbol = ticker(row)
    if symbol and symbol in set(theme.get("tickers", [])):
        return True
    text = row_text(row)
    return any(str(keyword).lower() in text for keyword in theme.get("keywords", []))


def attach_theme_tags(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in stocks:
        ids: list[str] = []
        names: list[str] = []
        for theme in THEME_RULES:
            if match_theme(row, theme):
                ids.append(str(theme["id"]))
                names.append(str(theme["name"]))
        row["Theme_Ids"] = ids
        row["Theme_Tags"] = names
    return stocks


def build_theme_summary(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for theme in THEME_RULES:
        theme_id = str(theme["id"])
        matched = [row for row in stocks if theme_id in row.get("Theme_Ids", [])]
        matched.sort(key=lambda row: (-score(row), ticker(row)))
        summary.append(
            {
                "id": theme_id,
                "name": theme["name"],
                "thesis": theme["thesis"],
                "segments": theme["segments"],
                "questions": theme["questions"],
                "count": len(matched),
                "eligible": sum(bool(row.get("Long_Term_Eligible")) for row in matched),
                "shortlist": sum(bool(row.get("IsShortlist")) for row in matched),
                "top": [ticker(row) for row in matched[:8]],
            }
        )
    return summary


def build_payload(screen: Path, shortlist: Path, universe: Path) -> dict[str, Any]:
    shortlist_set = {ticker(row) for row in records(shortlist) if ticker(row)}
    cik_map = {
        ticker(row): str(row.get("CIK") or "").replace(".0", "").zfill(10)
        for row in records(universe)
        if ticker(row)
    }
    stocks = []
    for row in records(screen):
        symbol = ticker(row)
        if symbol:
            stocks.append({**row, "Ticker": symbol, "CIK": cik_map.get(symbol, ""), "IsShortlist": symbol in shortlist_set})

    stocks.sort(key=lambda row: (-score(row), ticker(row)))
    for rank, row in enumerate(stocks, 1):
        row["Rank"] = rank
    attach_theme_tags(stocks)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "total": len(stocks),
            "eligible": sum(bool(row.get("Long_Term_Eligible")) for row in stocks),
            "shortlist": len(shortlist_set),
        },
        "themes": build_theme_summary(stocks),
        "stocks": stocks,
    }


PAGE = r'''<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alpha Engine 長期價值研究台</title>
<style>
:root{--bg:#07111f;--panel:#0d1b2d;--line:#29425f;--text:#eef5ff;--muted:#9fb2c9;--blue:#62adff;--green:#58d5a0;--yellow:#f3cb67;--red:#ff7b86}*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#07111f,#0a1b2e);color:var(--text);font-family:system-ui,"Noto Sans TC",sans-serif}.shell{width:min(1320px,calc(100% - 24px));margin:auto;padding:24px 0 56px}.panel{background:rgba(13,27,45,.96);border:1px solid var(--line);border-radius:18px;box-shadow:0 16px 44px #0005}.hero{padding:24px;display:flex;justify-content:space-between;gap:20px;align-items:end}.hero h1{margin:4px 0 8px;font-size:clamp(28px,4vw,46px)}.hero p,.muted{color:var(--muted)}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}.stat{padding:16px}.stat small{display:block;color:var(--muted)}.stat strong{font-size:27px}.theme{padding:16px;margin:14px 0}.theme h2{margin:0 0 4px}.themegrid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-top:12px}.theme-card{padding:12px;border:1px solid var(--line);border-radius:14px;background:#091827;cursor:pointer;min-height:150px}.theme-card:hover,.theme-card.active{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue) inset}.theme-card b{display:block;margin-bottom:7px}.theme-card small{display:block;color:var(--muted);line-height:1.45}.theme-card .nums{margin-top:9px;color:var(--green)}.tools{display:grid;grid-template-columns:2fr 1fr 1.3fr 1fr auto;gap:9px;padding:12px}input,select,button{font:inherit;border:1px solid var(--line);border-radius:11px;padding:10px;background:#091827;color:var(--text)}button{cursor:pointer}button:hover{border-color:var(--blue)}.watch{display:flex;gap:9px;align-items:center;padding:12px;margin-top:12px}.watch input{max-width:230px}.table{margin-top:12px;overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--line);text-align:left}th{font-size:12px;color:var(--muted)}tbody tr{cursor:pointer}tbody tr:hover{background:#17314b88}.badge{display:inline-block;padding:3px 7px;border:1px solid var(--line);border-radius:999px;font-size:12px;margin:1px}.good{color:var(--green)}.warn{color:var(--yellow)}.danger{color:var(--red)}dialog{width:min(1000px,calc(100% - 20px));max-height:90vh;padding:0;background:#0a1728;color:var(--text);border:1px solid var(--line);border-radius:18px}dialog::backdrop{background:#0010}.head{position:sticky;top:0;background:#0a1728ee;padding:15px;display:flex;justify-content:space-between;border-bottom:1px solid var(--line)}.body{padding:18px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.metric,.box{padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}.metric small{display:block;color:var(--muted);margin-bottom:5px}.section{margin-top:18px}.actions{display:flex;gap:8px;flex-wrap:wrap}a{color:var(--blue)}.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:18px}@media(max-width:980px){.themegrid{grid-template-columns:repeat(2,1fr)}.tools{grid-template-columns:1fr 1fr}}@media(max-width:760px){.hero{display:block}.stats,.grid{grid-template-columns:repeat(2,1fr)}.watch{align-items:stretch;flex-direction:column}.watch input{max-width:none}.optional{display:none}}@media(max-width:520px){.stats,.grid,.tools,.themegrid{grid-template-columns:1fr}}
</style></head><body><main class="shell">
<section class="hero panel"><div><span class="badge good">不依賴外部 AI</span><h1>Alpha Engine 長期價值研究台</h1><p>Mode C 整理數據與風險；供應鏈雷達幫你從風口找到二階研究線索。</p></div><div class="muted">QQQ 40% · VOO 30% · 主動個股 0–30%<br>單股上限 3% · 單一主動產業上限 9%</div></section>
<section class="stats"><div class="stat panel"><small>本次分析</small><strong id="total">0</strong></div><div class="stat panel"><small>研究合格</small><strong id="eligible">0</strong></div><div class="stat panel"><small>分散 shortlist</small><strong id="shortlist">0</strong></div><div class="stat panel"><small>我的追蹤</small><strong id="watchCount">0</strong></div></section>
<section class="theme panel"><h2>供應鏈二階雷達</h2><p class="muted">用主題找研究方向，不用主題替你下買賣決定。點卡片可篩出相關公司。</p><div id="themeCards" class="themegrid"></div></section>
<section class="tools panel"><input id="search" placeholder="搜尋 ticker、產業、結論或主題"><select id="view"><option value="all">全部結果</option><option value="shortlist">Shortlist</option><option value="eligible">研究合格</option><option value="watch">我的追蹤</option></select><select id="theme"><option value="">不限主題</option></select><select id="min"><option value="0">不限分數</option><option>60</option><option>70</option><option>75</option><option>80</option></select><button id="refresh">重新整理</button></section>
<section class="watch panel"><input id="addTicker" maxlength="10" placeholder="輸入想追蹤的 ticker"><button id="add">加入追蹤</button><button id="export">匯出追蹤名單</button><span class="muted">名單只存於你的瀏覽器，不會上傳。</span></section>
<section class="table panel"><table><thead><tr><th>排名</th><th>Ticker</th><th>分數</th><th>結論</th><th class="optional">產業</th><th class="optional">主題</th><th class="optional">Real FCF Yield</th><th>追蹤</th></tr></thead><tbody id="rows"></tbody></table><p id="empty" class="muted" style="padding:20px" hidden>沒有符合條件的股票。</p></section><div id="updated" class="footer"></div></main>
<dialog id="detail"><div class="head"><strong id="detailTitle"></strong><button id="close">關閉</button></div><div class="body" id="detailBody"></div></dialog>
<script id="payload" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.querySelector('#payload').textContent),stocks=data.stocks||[],themes=data.themes||[],map=new Map(stocks.map(x=>[x.Ticker,x])),themeMap=new Map(themes.map(x=>[x.id,x])),key='alphaEngineWatchlistV1';let watch=load();
const $=s=>document.querySelector(s),e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),n=v=>{v=Number(v);return Number.isFinite(v)?v:null},f=(v,d=2,s='')=>n(v)===null?'N/A':n(v).toFixed(d)+s,yes=v=>v===true||String(v).toLowerCase()==='true',norm=v=>String(v||'').toUpperCase().replace(/[^A-Z0-9.-]/g,'').slice(0,10),tags=x=>(x.Theme_Tags||[]),themeIds=x=>(x.Theme_Ids||[]);
function load(){try{return new Set((JSON.parse(localStorage.getItem(key)||'[]')).map(norm).filter(Boolean))}catch{return new Set()}}function save(){localStorage.setItem(key,JSON.stringify([...watch].sort()));$('#watchCount').textContent=watch.size}
function toggle(t){t=norm(t);if(!t)return;watch.has(t)?watch.delete(t):watch.add(t);save();render()}
function tagBadges(x){let t=tags(x);return t.length?t.slice(0,2).map(a=>`<span class="badge warn">${e(a)}</span>`).join(''):'<span class="muted">無</span>'}
function visible(){let q=$('#search').value.toLowerCase(),v=$('#view').value,m=Number($('#min').value),theme=$('#theme').value;let out=stocks.filter(x=>{let hay=[x.Ticker,x.Sector,x.Industry,x.Status,x.Verdict,...tags(x)].join(' ').toLowerCase();return(!q||hay.includes(q))&&(n(x.Long_Term_Score)??-1)>=m&&(!theme||themeIds(x).includes(theme))&&(v!=='shortlist'||yes(x.IsShortlist))&&(v!=='eligible'||yes(x.Long_Term_Eligible))&&(v!=='watch'||watch.has(x.Ticker))});if(v==='watch')for(let t of watch)if(!map.has(t)&&(!q||t.toLowerCase().includes(q)))out.push({Ticker:t,Status:'尚未在本次篩選資料',Theme_Tags:[],Theme_Ids:[]});return out}
function renderThemes(){let sel=$('#theme');sel.innerHTML='<option value="">不限主題</option>'+themes.map(t=>`<option value="${e(t.id)}">${e(t.name)}</option>`).join('');$('#themeCards').innerHTML=themes.map(t=>`<div class="theme-card" data-theme="${e(t.id)}"><b>${e(t.name)}</b><small>${e(t.thesis)}</small><div class="nums">${t.count} 檔 · eligible ${t.eligible} · shortlist ${t.shortlist}</div><small>Top: ${(t.top||[]).map(e).join(', ')||'待資料'}</small></div>`).join('');document.querySelectorAll('[data-theme]').forEach(card=>card.onclick=()=>{$('#theme').value=card.dataset.theme;render()})}
function render(){let out=visible(),active=$('#theme').value;document.querySelectorAll('[data-theme]').forEach(card=>card.classList.toggle('active',card.dataset.theme===active));$('#rows').innerHTML=out.map(x=>`<tr data-t="${e(x.Ticker)}"><td>${x.Rank||'-'}</td><td><b>${e(x.Ticker)}</b> ${yes(x.IsShortlist)?'<span class="badge good">Shortlist</span>':''}</td><td>${f(x.Long_Term_Score)}</td><td>${e(x.Verdict||x.Status||'待查')}</td><td class="optional">${e(x.Sector||x.Industry||'N/A')}</td><td class="optional">${tagBadges(x)}</td><td class="optional">${f(x.Real_FCF_Yield_pct,2,'%')}</td><td><button data-w="${e(x.Ticker)}">${watch.has(x.Ticker)?'移除':'加入'}</button></td></tr>`).join('');$('#empty').hidden=out.length>0;document.querySelectorAll('tr[data-t]').forEach(r=>r.onclick=a=>{if(!a.target.dataset.w)openDetail(r.dataset.t)});document.querySelectorAll('[data-w]').forEach(b=>b.onclick=a=>{a.stopPropagation();toggle(b.dataset.w)})}
const fields=[['長期綜合分數','Long_Term_Score'],['品質分數','Quality_Score'],['價值分數','Value_Score'],['市場預期分數','Expectations_Score'],['資本配置分數','Capital_Allocation_Score'],['風險扣分','Risk_Penalty'],['TTM OCF','TTM_OCF_B','B'],['全部 CapEx','Dynamic_CapEx_B','B'],['TTM SBC','TTM_SBC_B','B'],['Real FCF Yield','Real_FCF_Yield_pct','%'],['ICR','ICR','x'],['ROIC','ROIC_pct','%'],['ROCE','ROCE_pct','%'],['5Y Real FCF 正值年數','Real_FCF_Positive_Years_5Y'],['5Y OCF / 淨利','OCF_to_NetIncome_5Y','x'],['EV / EBITDA','EV_EBITDA_x','x'],['P / E','PE_x','x'],['最新毛利率','GM_Latest_pct','%'],['三季毛利變化','GM_3Q_Change_pp','pp'],['三季營收變化','Rev_3Q_Change_pct','%'],['一年股數變化','Share_Count_Change_pct','%'],['三年股數變化','Share_Count_Change_3Y_pct','%'],['隱含 EBITDA CAGR','Implied_EBITDA_CAGR_3Y_pct','%'],['EBITDA -30% 下檔','EBITDA_Drawdown_30_pct','%']];
function sec(x){let c=String(x.CIK||'').replace(/\D/g,'');return c?`https://www.sec.gov/edgar/browse/?CIK=${encodeURIComponent(c)}&owner=exclude&action=getcompany`:''}function yahoo(t,p=''){return`https://finance.yahoo.com/quote/${encodeURIComponent(t)}/${p}`}
function prompt(x){let themeText=tags(x).length?tags(x).join('、'):'無明確主題標籤';return[`請以中長期價值投資角度研究 ${x.Ticker}，不要直接下買賣指令。`,`Quant 分數：${f(x.Long_Term_Score)}；品質：${f(x.Quality_Score)}；價值：${f(x.Value_Score)}；資本配置：${f(x.Capital_Allocation_Score)}。`,`主題標籤：${themeText}。請判斷它是一階、二階或三階受益者，還是只是被題材蹭到。`,`Real FCF Yield：${f(x.Real_FCF_Yield_pct,2,'%')}；ICR：${f(x.ICR,2,'x')}；ROIC：${f(x.ROIC_pct,2,'%')}。`,'請用最新官方財報回答：','1. 三句話投資論點。','2. 最強反方論點。','3. thesis 失效條件。','4. 悲觀、基準、樂觀情境。','5. 營收、毛利、Real FCF 與資產負債表警訊。','6. 股數稀釋與管理層資本配置。','7. 與 QQQ/VOO 的重疊，以及額外持有理由。','8. 主題供應鏈位置、訂單能見度、瓶頸與是否已反映在估值。','9. 尚無法確認的資料。'].join('\n')}
async function copy(t){try{await navigator.clipboard.writeText(t)}catch{let a=document.createElement('textarea');a.value=t;document.body.append(a);a.select();document.execCommand('copy');a.remove()}alert('已複製 AI 研究提示。')}
function openDetail(t){let x=map.get(t)||{Ticker:t,Status:'尚未在本次篩選資料',Theme_Tags:[],Theme_Ids:[]},s=sec(x),related=themeIds(x).map(id=>themeMap.get(id)).filter(Boolean);$('#detailTitle').textContent=x.Ticker;$('#detailBody').innerHTML=`<div class="actions"><button id="dw">${watch.has(t)?'移除追蹤':'加入追蹤'}</button><button id="cp">複製 AI 研究提示</button>${s?`<a target="_blank" rel="noopener" href="${s}">SEC 官方財報</a>`:''}<a target="_blank" rel="noopener" href="${yahoo(t,'financials')}">財務報表頁</a><a target="_blank" rel="noopener" href="${yahoo(t)}">市場資料頁</a></div><div class="section"><h3>主題與供應鏈位置</h3><div class="box">${tagBadges(x)}<br><br>${related.map(r=>`<b>${e(r.name)}</b><br>${e(r.thesis)}<br>要問：${(r.questions||[]).map(e).join('；')}`).join('<br><br>')||'尚無主題標籤，請從基本面而非題材開始。'}</div></div><div class="section"><h3>模型結論</h3><div class="box">${e(x.Research_Action||x.Verdict||x.Status||'待查')}</div></div><div class="section"><h3>財務與風險指標</h3><div class="grid">${fields.map(a=>`<div class="metric"><small>${a[0]}</small><b>${f(x[a[1]],2,a[2]||'')}</b></div>`).join('')}</div></div><div class="section"><h3>資料品質與待查事項</h3><div class="box">${e(x.GM_Diagnosis||'')}<br>${e(x.Data_Quality_Flags||'無資料品質警示')}<br>${e(x.Agent_Tasks||'請從 SEC 官方財報開始查核。')}</div></div>`;$('#dw').onclick=()=>toggle(t);$('#cp').onclick=()=>copy(prompt(x));if(!$('#detail').open)$('#detail').showModal()}
$('#total').textContent=data.stats.total;$('#eligible').textContent=data.stats.eligible;$('#shortlist').textContent=data.stats.shortlist;$('#updated').textContent='資料更新：'+new Date(data.generated_at).toLocaleString('zh-TW')+' · 本網站僅供研究，不是投資建議。';renderThemes();['#search','#view','#theme','#min'].forEach(s=>$(s).oninput=render);$('#refresh').onclick=()=>{$('#theme').value='';render()};$('#add').onclick=()=>{let t=norm($('#addTicker').value);if(t){watch.add(t);$('#addTicker').value='';save();render();openDetail(t)}};$('#addTicker').onkeydown=a=>{if(a.key==='Enter')$('#add').click()};$('#export').onclick=()=>{let text=[...watch].sort().join('\n'),a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text+(text?'\n':'')],{type:'text/plain;charset=utf-8'}));a.download='alpha-engine-watchlist.txt';a.click();URL.revokeObjectURL(a.href)};$('#close').onclick=()=>$('#detail').close();save();render();
</script></body></html>'''


def build_dashboard(screen: Path, shortlist: Path, universe: Path, output: Path) -> Path:
    payload = build_payload(screen, shortlist, universe)
    output.mkdir(parents=True, exist_ok=True)
    embedded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    index = output / "index.html"
    index.write_text(PAGE.replace("__DATA__", embedded), encoding="utf-8")
    (output / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the static Mode C research dashboard.")
    parser.add_argument("--screen", default="mode_c_screen.csv")
    parser.add_argument("--shortlist", default="mode_c_shortlist.csv")
    parser.add_argument("--universe", default="qualified_universe.csv")
    parser.add_argument("--output", default="public")
    args = parser.parse_args()
    index = build_dashboard(Path(args.screen), Path(args.shortlist), Path(args.universe), Path(args.output))
    print(f"Dashboard written to {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
