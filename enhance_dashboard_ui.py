from __future__ import annotations

import argparse
from pathlib import Path


ENHANCEMENT = r'''
<script id="emergingThemeUiEnhancer">
(function(){
  if (window.__emergingThemeUiEnhanced) return;
  window.__emergingThemeUiEnhanced = true;

  const style = document.createElement('style');
  style.textContent = `
    .guidegrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0}
    .guide{padding:12px;border:1px solid var(--line);border-radius:14px;background:#091827}
    .guide b{display:block;margin-bottom:6px}
    .guide small{color:var(--muted);line-height:1.5}
    .emerging-card{cursor:pointer}
    .emerging-card:hover,.emerging-card.active{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue) inset}
    .mini-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:10px 0}
    .mini-metrics span{padding:7px;border:1px solid var(--line);border-radius:10px;background:#061422}
    .mini-metrics small{display:block;font-size:11px}
    .candidate-action{margin-top:9px;color:var(--blue);font-size:12px}
    .filterbar{display:none;align-items:center;gap:8px;padding:12px;margin-top:12px}
    .filterbar.active{display:flex}
    .filterbar strong{color:var(--green)}
    @media(max-width:980px){.guidegrid{grid-template-columns:1fr}.mini-metrics{grid-template-columns:repeat(2,1fr)}}
    @media(max-width:520px){.filterbar{align-items:stretch;flex-direction:column}}
  `;
  document.head.appendChild(style);

  const baseline = document.getElementById('baseline');
  const cards = document.getElementById('emergingCards');
  if (baseline && cards && !document.getElementById('emergingGuide')) {
    const guide = document.createElement('div');
    guide.id = 'emergingGuide';
    guide.className = 'guidegrid';
    guide.innerHTML = `
      <div class="guide"><b>它會自動偵測什麼？</b><small>產業、行業、既有主題層級是否同時出現分數提高、合格公司變多、shortlist 增加、營收/毛利/Real FCF 改善。</small></div>
      <div class="guide"><b>它不會自動做什麼？</b><small>不會自動買入、不會自動定案新主題，也不會把短期股價熱度直接升格成風口。</small></div>
      <div class="guide"><b>你要怎麼用？</b><small>點一張候選卡，下面股票表會只顯示相關公司；若財報與供應鏈邏輯都合理，再手動加入正式主題庫。</small></div>`;
    baseline.insertAdjacentElement('afterend', guide);

    const filter = document.createElement('div');
    filter.id = 'activeCandidate';
    filter.className = 'filterbar';
    guide.insertAdjacentElement('afterend', filter);
  }

  const candidateMap2 = new Map((emerging || []).map(x => [x.key, x]));
  let activeCandidate2 = null;

  function candidateMatches2(x, c) {
    if (!c) return true;
    const key = String(c.key || '');
    const parts = key.split(':');
    const kind = parts[0];
    const value = parts.slice(1).join(':');
    if (kind === 'sector') return String(x.Sector || '') === value;
    if (kind === 'industry') return String(x.Industry || '') === value;
    if (kind === 'theme_layer') {
      const themeId = parts[1];
      const layer = parts.slice(2).join(':');
      return layerMap(x)[themeId] === layer;
    }
    return (c.top || []).includes(x.Ticker);
  }

  function metricText2(c) {
    const m = c.metrics || {};
    return `<div class="mini-metrics">
      <span><small>樣本</small>${e(m.count ?? 'N/A')}</span>
      <span><small>合格</small>${e(m.eligible ?? 'N/A')}</span>
      <span><small>Shortlist</small>${e(m.shortlist ?? 'N/A')}</span>
      <span><small>均分</small>${f(m.avg_score, 1)}</span>
    </div>`;
  }

  function renderActiveCandidate2() {
    const box = document.getElementById('activeCandidate');
    if (!box) return;
    if (!activeCandidate2) {
      box.classList.remove('active');
      box.innerHTML = '';
      return;
    }
    box.classList.add('active');
    box.innerHTML = `目前下方股票表正在看：<strong>${e(activeCandidate2.name)}</strong><span class="muted">這只是候選風口篩選，不是正式主題。</span><button id="clearCandidate">清除候選篩選</button>`;
    document.getElementById('clearCandidate').onclick = () => {
      activeCandidate2 = null;
      render();
    };
  }

  visible = function() {
    const query = $('#search').value.toLowerCase();
    const view = $('#view').value;
    const minScore = Number($('#min').value);
    const selectedTheme = $('#theme').value;
    const out = stocks.filter(x => {
      const haystack = [x.Ticker, x.Sector, x.Industry, x.Status, x.Verdict, ...tags(x), ...layerTags(x)].join(' ').toLowerCase();
      return (!query || haystack.includes(query))
        && (minScore <= 0 || (n(x.Long_Term_Score) ?? -1) >= minScore)
        && (!selectedTheme || themeIds(x).includes(selectedTheme))
        && candidateMatches2(x, activeCandidate2)
        && (view !== 'complete' || yes(x.Data_Integrity_Complete))
        && (view !== 'estimated' || integrityCount(x, 'ESTIMATED') > 0)
        && (view !== 'missing' || integrityCount(x, 'MISSING') > 0)
        && (view !== 'specialized' || (x.Industry_Model_Key && x.Industry_Model_Key !== 'GENERAL_CORPORATE'))
        && (view !== 'general' || !x.Industry_Model_Key || x.Industry_Model_Key === 'GENERAL_CORPORATE')
        && (view !== 'shortlist' || yes(x.IsShortlist))
        && (view !== 'eligible' || yes(x.Long_Term_Eligible))
        && (view !== 'abstain' || x.Decision_State === 'ABSTAIN')
        && (view !== 'watch' || watch.has(x.Ticker));
    });
    if (view === 'watch' && !activeCandidate2) {
      for (const ticker of watch) {
        if (!map.has(ticker) && (!query || ticker.toLowerCase().includes(query))) {
          out.push({Ticker:ticker,Status:'尚未在本次篩選資料',Theme_Tags:[],Theme_Ids:[],Theme_Layer_Map:{},Theme_Layer_Tags:[]});
        }
      }
    }
    return out;
  };

  renderEmerging = function() {
    const base = data.trend_baseline || {};
    $('#baseline').textContent = `狀態：${base.status || '建立基準中'}${base.previous_generated_at ? '；上次資料：' + new Date(base.previous_generated_at).toLocaleString('zh-TW') : ''}。候選只代表待查線索，不是買入訊號。點候選卡可把下方股票表切成該產業 / 行業 / 主題層級。`;
    $('#emergingCards').innerHTML = emerging.length ? emerging.map(c => `
      <div class="emerging-card" data-candidate="${e(c.key)}">
        <b>${e(c.name)}</b>
        <span class="badge warn">${e(c.status)}</span>
        <span class="badge ${c.confidence === '高' ? 'good' : 'warn'}">信心 ${e(c.confidence)}</span>
        <span class="badge">${e(c.kind)}</span>
        <div class="nums">雷達分數 ${f(c.signal_score, 1)}</div>
        ${metricText2(c)}
        <small>Top: ${(c.top || []).map(e).join(', ') || '待資料'}</small>
        <div class="reasons"><b>出現原因</b><br>${(c.reasons || []).map(r => '• ' + e(r)).join('<br>')}</div>
        <div class="candidate-action">點我 → 下面只看這群股票</div>
      </div>`).join('') : '<div class="emerging-card"><b>尚無明確候選風口</b><small>如果是第一次跑，系統正在建立基準；下一次開始會比較產業、行業與主題層級是否變強。</small></div>';
    document.querySelectorAll('[data-candidate]').forEach(card => card.onclick = () => {
      activeCandidate2 = candidateMap2.get(card.dataset.candidate) || null;
      $('#theme').value = '';
      render();
      document.querySelector('.table')?.scrollIntoView({behavior:'smooth',block:'start'});
    });
  };

  renderThemes = function() {
    const selector = $('#theme');
    selector.innerHTML = '<option value="">不限主題</option>' + themes.map(t => `<option value="${e(t.id)}">${e(t.name)}</option>`).join('');
    $('#themeCards').innerHTML = themes.map(t => `<div class="theme-card" data-theme="${e(t.id)}"><b>${e(t.name)}</b><small>${e(t.thesis)}</small><div class="nums">${t.count} 檔 · eligible ${t.eligible} · shortlist ${t.shortlist}</div><small>Top: ${(t.top || []).map(e).join(', ') || '待資料'}</small><div class="layers">${(t.layers || []).map(l => `${e(l.name)}：${(l.top || []).slice(0, 5).map(e).join(', ') || '待資料'}`).join('<br>')}</div></div>`).join('');
    document.querySelectorAll('[data-theme]').forEach(card => card.onclick = () => {
      activeCandidate2 = null;
      $('#theme').value = card.dataset.theme;
      render();
    });
  };

  render = function() {
    const out = visible();
    const activeTheme = $('#theme').value;
    renderActiveCandidate2();
    document.querySelectorAll('[data-theme]').forEach(card => card.classList.toggle('active', card.dataset.theme === activeTheme));
    document.querySelectorAll('[data-candidate]').forEach(card => card.classList.toggle('active', activeCandidate2 && card.dataset.candidate === activeCandidate2.key));
    $('#rows').innerHTML = out.map(x => `<tr data-t="${e(x.Ticker)}"><td>${x.Rank || '-'}</td><td><b>${e(x.Ticker)}</b> ${yes(x.IsShortlist) ? '<span class="badge good">Shortlist</span>' : ''}</td><td>${metricHtml(x, 'Long_Term_Score')}</td><td>${e(x.Verdict || x.Status || '待查')}</td><td class="optional">${e(x.Sector || x.Industry || 'N/A')}</td><td class="optional">${tagBadges(x)}</td><td class="optional">${metricHtml(x, 'Real_FCF_Yield_pct', 2, '%')}</td><td><button data-w="${e(x.Ticker)}">${watch.has(x.Ticker) ? '移除' : '加入'}</button></td></tr>`).join('');
    $('#empty').hidden = out.length > 0;
    document.querySelectorAll('tr[data-t]').forEach(row => row.onclick = event => {
      if (!event.target.dataset.w) openDetail(row.dataset.t);
    });
    document.querySelectorAll('[data-w]').forEach(button => button.onclick = event => {
      event.stopPropagation();
      toggle(button.dataset.w);
    });
  };

  const fieldLabel2 = (key, label) => {
    const item = fields.find(field => field[1] === key);
    if (item) item[0] = label;
  };
  fieldLabel2('Real_FCF_Yield_pct', '維護 FCF / 市值');
  fieldLabel2('Conservative_Real_FCF_Yield_pct', '保守 FCF / 市值');
  fields.push(
    ['維護 Real FCF', 'Maintenance_Real_FCF_B', 'B'],
    ['保守 Real FCF', 'Conservative_Real_FCF_B', 'B'],
    ['維護 FCF / EV', 'Maintenance_Real_FCF_to_EV_Yield_pct', '%'],
    ['保守 FCF / EV', 'Conservative_Real_FCF_to_EV_Yield_pct', '%'],
    ['Maintenance CapEx 低情境', 'Maintenance_CapEx_Low_B', 'B'],
    ['Maintenance CapEx 高情境', 'Maintenance_CapEx_High_B', 'B'],
    ['歷史估值覆蓋率', 'Historical_Valuation_Coverage']
  );

  const baseOpenDetail2 = openDetail;
  openDetail = function(ticker) {
    baseOpenDetail2(ticker);
    const stock = map.get(ticker);
    if (!stock) return;
    const metricSections = [...document.querySelectorAll('#detailBody .section')].filter(section => section.querySelector('.grid'));
    const financialSection = metricSections.at(-1);
    const specialized = stock.Industry_Model_Key && stock.Industry_Model_Key !== 'GENERAL_CORPORATE';
    (financialSection ? [...financialSection.querySelectorAll('.metric')] : []).forEach((node, index) => {
      const field = fields[index];
      if (!field) return;
      const meta = (stock.Metric_Metadata || {})[field[1]] || {};
      if (specialized && meta.status === 'NOT_APPLICABLE') {
        node.remove();
        return;
      }
      node.querySelector('b').innerHTML = metricHtml(stock, field[1], 2, field[2] || '');
    });
    if (metricSections.length > 1) {
      const componentNames = Object.keys(obj(stock.Industry_Model_Components_JSON));
      const metricNames = Object.keys(obj(stock.Industry_Model_Metrics_JSON));
      const specializedKeys = [
        ...componentNames.map(name => `industry_component.${name}`),
        ...metricNames.map(name => `industry.${name}`),
      ];
      metricSections[0].querySelectorAll('.metric').forEach((node, index) => {
        const key = specializedKeys[index];
        if (key) node.querySelector('b').innerHTML = metricHtml(stock, key);
      });
    }
    const qualityBox = document.querySelector('#detailBody .section:last-child .box');
    const counts = stock.Data_Integrity_Summary || {};
    if (qualityBox) qualityBox.innerHTML += `<br><br><b>狀態契約稽核</b><br>模型路由：${e(stock.Industry_Model_Key || 'GENERAL_CORPORATE')}<br>路由原因：${e(stock.Model_Route_Reason || '待查')}<br>適用欄位：${e(stock.Applicable_Metrics || '待查')}<br>不適用欄位：${e(stock.Not_Applicable_Metrics || '無')}<br>必要缺值：${e(stock.Required_Missing_Metrics || '無')}<br>選用缺值：${e(stock.Optional_Missing_Metrics || '無')}<br>指標證據覆蓋：${f(stock.Metric_Evidence_Coverage, 2)}<br>最新資料日期：${e(stock.Latest_Metric_AsOf || '待查')}<br>Yahoo fallback：${yes(stock.Uses_Yahoo_Fallback) ? '是' : '否'}<br>年度 fallback：${yes(stock.Uses_Annual_Fallback) ? '是' : '否'}<br>估計 Maintenance CapEx：${yes(stock.Uses_Estimated_Maintenance_CapEx) ? '是' : '否'}<br>外幣換算：${yes(stock.Uses_FX_Conversion) ? '是' : '否'}<br>有效 ${e(counts.VALID || 0)} / 估計 ${e(counts.ESTIMATED || 0)} / 缺資料 ${e(counts.MISSING || 0)} / 不適用 ${e(counts.NOT_APPLICABLE || 0)} / 暫不判斷 ${e(counts.ABSTAIN || 0)} / 過舊 ${e(counts.STALE || 0)}<br>Maintenance CapEx 信心：${e(stock.Maintenance_CapEx_Confidence || '不適用')}<br>產業壓力延伸：${e(stock.Industry_Stress_Extension_Status || '待查')} - ${e(stock.Industry_Stress_Extension_Reason || '')}`;
  };

  prompt = function(x) {
    const special = x.Industry_Model_Key && x.Industry_Model_Key !== 'GENERAL_CORPORATE';
    return [
      `請以中長期價值投資角度研究 ${x.Ticker}，不要直接下買賣指令。`,
      `模型：${x.Industry_Model_Key || 'GENERAL_CORPORATE'}；決策：${x.Decision_State || '待查'}；資料信心：${metricText(x, 'Data_Confidence_Score')}。`,
      special
        ? `專用指標：${x.Industry_Model_Metrics_JSON || '待查'}。`
        : `維護 FCF / 市值：${metricText(x, 'Real_FCF_Yield_pct', 2, '%')}；ICR：${metricText(x, 'ICR', 2, 'x')}；ROIC：${metricText(x, 'ROIC_pct', 2, '%')}。`,
      `必要缺值：${x.Required_Missing_Metrics || '無'}；不適用欄位：${x.Not_Applicable_Metrics || '無'}。`,
      '請以最新官方財報核對產業 KPI、現金流、資產負債表、稀釋、估值、壓力情境與 thesis 失效條件。',
    ].join('\n');
  };

  ['#search', '#view', '#min'].forEach(selector => $(selector).oninput = render);
  $('#theme').oninput = () => {
    activeCandidate2 = null;
    render();
  };
  $('#refresh').onclick = () => {
    activeCandidate2 = null;
    $('#theme').value = '';
    render();
  };
  renderEmerging();
  renderThemes();
  save();
  render();
})();
</script>
'''


MODERN_ENHANCEMENT = r'''
<script id="researchClusterUiEnhancer">
(function(){
  if (window.__researchClusterUiEnhanced) return;
  window.__researchClusterUiEnhanced = true;
  const baseline = document.getElementById('baseline');
  if (baseline && !document.getElementById('researchClusterGuide')) {
    const guide = document.createElement('div');
    guide.id = 'researchClusterGuide';
    guide.className = 'guidegrid';
    guide.innerHTML = `
      <div class="guide"><b>這裡顯示什麼</b><small>依模型內收縮百分位、資料信心、核心 KPI 覆蓋與營運趨勢找出研究群聚。</small></div>
      <div class="guide"><b>這裡不代表什麼</b><small>群聚不是產業輪動預測，也不是跨產業報酬排名；點選後只會縮小研究範圍。</small></div>
      <div class="guide"><b>版本處理</b><small>排序或指標版本改變時，舊趨勢基準會失效並重新建立。</small></div>`;
    baseline.insertAdjacentElement('afterend', guide);
    const filter = document.createElement('div');
    filter.id = 'activeCandidate';
    filter.className = 'filterbar';
    guide.insertAdjacentElement('afterend', filter);
  }

  const style = document.createElement('style');
  style.textContent = `.guidegrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0}.guide{padding:12px;border:1px solid var(--line);background:#091827}.guide b{display:block;margin-bottom:6px}.guide small{color:var(--muted);line-height:1.5}.emerging-card{cursor:pointer}.emerging-card:hover,.emerging-card.active{border-color:var(--blue)}.filterbar{display:none;align-items:center;gap:8px;padding:12px;margin-top:12px}.filterbar.active{display:flex}@media(max-width:980px){.guidegrid{grid-template-columns:1fr}}`;
  document.head.appendChild(style);

  const candidateMap = new Map((emerging || []).map(candidate => [candidate.key, candidate]));
  let activeCandidate = null;
  const matchesCandidate = (stock, candidate) => {
    if (!candidate) return true;
    const parts = String(candidate.key || '').split(':');
    const kind = parts[0], value = parts.slice(1).join(':');
    if (kind === 'sector') return String(stock.Sector || '') === value;
    if (kind === 'industry') return String(stock.Industry || '') === value;
    if (kind === 'theme_layer') return layerMap(stock)[parts[1]] === parts.slice(2).join(':');
    return (candidate.top || []).includes(stock.Ticker);
  };
  const baseVisible = visible;
  visible = function(){ return baseVisible().filter(stock => matchesCandidate(stock, activeCandidate)); };

  const renderFilter = () => {
    const box = document.getElementById('activeCandidate');
    if (!box) return;
    if (!activeCandidate) { box.classList.remove('active'); box.innerHTML = ''; return; }
    box.classList.add('active');
    box.innerHTML = `目前研究群聚：<strong>${e(activeCandidate.name)}</strong><button id="clearCandidate">清除群聚篩選</button>`;
    document.getElementById('clearCandidate').onclick = () => { activeCandidate = null; render(); };
  };
  const attachCandidateEvents = () => document.querySelectorAll('[data-candidate]').forEach(card => {
    card.classList.toggle('active', activeCandidate && card.dataset.candidate === activeCandidate.key);
    card.onclick = () => { activeCandidate = candidateMap.get(card.dataset.candidate) || null; $('#theme').value = ''; render(); document.querySelector('.table')?.scrollIntoView({behavior:'smooth',block:'start'}); };
  });
  const baseRender = render;
  render = function(){ baseRender(); renderFilter(); attachCandidateEvents(); };
  const baseRenderEmerging = renderEmerging;
  renderEmerging = function(){ baseRenderEmerging(); attachCandidateEvents(); };
  $('#refresh').onclick = () => { activeCandidate = null; $('#theme').value = ''; render(); };
  renderEmerging();
  render();
})();
</script>
'''


def enhance_dashboard(index_path: Path) -> bool:
    html = index_path.read_text(encoding="utf-8")
    if "researchClusterUiEnhancer" in html:
        return False
    if "</body>" not in html:
        raise ValueError(f"{index_path} does not look like an HTML dashboard")
    index_path.write_text(
        html.replace("</body>", MODERN_ENHANCEMENT + "</body>", 1),
        encoding="utf-8",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Enhance the static dashboard UI after generation.")
    parser.add_argument("index", nargs="?", default="public/index.html")
    args = parser.parse_args()
    changed = enhance_dashboard(Path(args.index))
    print("Dashboard UI enhanced." if changed else "Dashboard UI already enhanced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
