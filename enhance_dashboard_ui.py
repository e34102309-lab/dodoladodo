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
        && (n(x.Long_Term_Score) ?? -1) >= minScore
        && (!selectedTheme || themeIds(x).includes(selectedTheme))
        && candidateMatches2(x, activeCandidate2)
        && (view !== 'shortlist' || yes(x.IsShortlist))
        && (view !== 'eligible' || yes(x.Long_Term_Eligible))
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
    $('#rows').innerHTML = out.map(x => `<tr data-t="${e(x.Ticker)}"><td>${x.Rank || '-'}</td><td><b>${e(x.Ticker)}</b> ${yes(x.IsShortlist) ? '<span class="badge good">Shortlist</span>' : ''}</td><td>${f(x.Long_Term_Score)}</td><td>${e(x.Verdict || x.Status || '待查')}</td><td class="optional">${e(x.Sector || x.Industry || 'N/A')}</td><td class="optional">${tagBadges(x)}</td><td class="optional">${f(x.Real_FCF_Yield_pct, 2, '%')}</td><td><button data-w="${e(x.Ticker)}">${watch.has(x.Ticker) ? '移除' : '加入'}</button></td></tr>`).join('');
    $('#empty').hidden = out.length > 0;
    document.querySelectorAll('tr[data-t]').forEach(row => row.onclick = event => {
      if (!event.target.dataset.w) openDetail(row.dataset.t);
    });
    document.querySelectorAll('[data-w]').forEach(button => button.onclick = event => {
      event.stopPropagation();
      toggle(button.dataset.w);
    });
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


def enhance_dashboard(index_path: Path) -> bool:
    html = index_path.read_text(encoding="utf-8")
    if "emergingThemeUiEnhancer" in html:
        return False
    if "</body>" not in html:
        raise ValueError(f"{index_path} does not look like an HTML dashboard")
    index_path.write_text(html.replace("</body>", ENHANCEMENT + "</body>", 1), encoding="utf-8")
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
