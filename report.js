'use strict';
// 報告端 (瀏覽器) 的分析 + 渲染: 邏輯與 analyze.py 的 analyze() 一致, 讓使用者在報告上直接選年度/月份/下單量定義

const MOM_THRESHOLD = 0.3;
const TOP_SUPPLIERS = 5;
const TOP_GROWTH = 3;
const LOWEST_MARGIN_N = 3;

// ===== 工具 =====
const jround = x => Math.floor(x + 0.5);
const pct = v => (v == null || !isFinite(v)) ? null : jround(v * 10000) / 100;
const r2 = v => jround(v * 100) / 100;
const ymLabel = i => `${Math.floor(i / 12)}-${String(i % 12 + 1).padStart(2, '0')}`;
const margin = (s, c) => s > 0 ? (s - c) / s : null;
const mean = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
const sumBy = (arr, f) => arr.reduce((s, x) => s + f(x), 0);
const minBy = (arr, f) => arr.reduce((m, x) => Math.min(m, f(x)), Infinity);
const maxBy = (arr, f) => arr.reduce((m, x) => Math.max(m, f(x)), -Infinity);
function groupBy(arr, f) {
  const m = new Map();
  for (const x of arr) {
    const k = f(x);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(x);
  }
  return m;
}

// ===== 還原紀錄 =====
function buildRecs(D) {
  return D.rows.map(r => {
    const y = r[0] || null, m = r[1] || null, sy = r[2] || null, sm = r[3] || null;
    return {
      year: y, month: m, idx: y ? y * 12 + m - 1 : null,
      sYear: sy, sMonth: sm, sIdx: sy ? sy * 12 + sm - 1 : null,
      cat: D.cat[r[4]], supplier: D.sup[r[5]], buyer: D.buy[r[6]], name: D.name[r[7]],
      qty: r[8], cost: r[9], sales: r[10], onTime: r[11] < 0 ? null : r[11] === 1,
    };
  });
}

// ===== 分析 =====
function analyze(allRecs, o) {
  const Y0 = o.Y0, Y1 = o.Y1, mf = o.mf, mt = o.mt, metric = o.metric;
  const YEARS = [Y0, Y1];
  // 下單量/毛利率以採購日期歸屬
  const recs = allRecs.filter(r => r.year != null && YEARS.includes(r.year) && r.month >= mf && r.month <= mt);
  for (const r of recs) r.vol = metric === 'amount' ? r.cost : metric === 'qty' ? r.qty : 1;
  if (!recs.length) return { error: '沒有符合條件(狀態=已確認且採購日期在指定年度/月份)的資料' };

  const minIdx = minBy(recs, r => r.idx), maxIdx = maxBy(recs, r => r.idx);
  const allIdx = [];
  for (let i = minIdx; i <= maxIdx; i++) allIdx.push(i);
  const y1Months = recs.filter(r => r.year === Y1).map(r => r.month);
  const cutoff = y1Months.length ? y1Months.reduce((a, b) => Math.max(a, b), -Infinity) : 12;
  const byIdxAll = groupBy(recs, r => r.idx);

  const monthSeries = (arr, valFn) => {
    const byIdx = groupBy(arr, r => r.idx);
    return allIdx.map(i => ({ i, ym: ymLabel(i), v: sumBy(byIdx.get(i) || [], valFn) }));
  };
  const momList = series => series.map((p, k) => {
    const prev = k > 0 ? series[k - 1].v : 0;
    return { ym: p.ym, value: r2(p.v), mom: k > 0 && prev > 0 ? (p.v - prev) / prev : null };
  });

  // --- 1. 每月下單量 ---
  const monthlyVolume = [];
  for (let m = 1; m <= 12; m++) {
    const a = sumBy(recs.filter(r => r.year === Y0 && r.month === m), r => r.vol);
    const b = sumBy(recs.filter(r => r.year === Y1 && r.month === m), r => r.vol);
    const ca = sumBy(recs.filter(r => r.year === Y0 && r.month === m), r => r.cost);
    const cb = sumBy(recs.filter(r => r.year === Y1 && r.month === m), r => r.cost);
    monthlyVolume.push({ month: m, [Y0]: r2(a), [Y1]: r2(b), yoyPct: a > 0 ? pct((b - a) / a) : null,
      [`cost${Y0}`]: r2(ca), [`cost${Y1}`]: r2(cb), costYoyPct: ca > 0 ? pct((cb - ca) / ca) : null });
  }

  const catSummary = [];
  for (const [cat, arr] of groupBy(recs, r => r.cat)) {
    const base = sumBy(arr.filter(r => r.year === Y0 && r.month >= mf && r.month <= cutoff), r => r.vol);
    const cur = sumBy(arr.filter(r => r.year === Y1), r => r.vol);
    const series = momList(monthSeries(arr, r => r.vol));
    const moms = series.map(s => s.mom).filter(x => x !== null);
    const avgAbs = moms.length ? mean(moms.map(Math.abs)) : null;
    catSummary.push({
      category: cat,
      basePeriod: r2(base), currentPeriod: r2(cur), delta: r2(cur - base),
      growthPct: base > 0 ? pct((cur - base) / base) : null,
      isNewCategory: base === 0 && cur > 0,
      avgAbsMomPct: pct(avgAbs),
      volatilityFlag: avgAbs !== null && avgAbs > MOM_THRESHOLD,
      spikeMonths: series.filter(s => s.mom !== null && Math.abs(s.mom) > MOM_THRESHOLD)
        .map(s => ({ ym: s.ym, value: s.value, momPct: pct(s.mom) })),
    });
  }
  const growthKey = c => c.growthPct !== null ? c.growthPct : (c.isNewCategory ? 1e9 : -1e9);
  const top3 = catSummary.slice().sort((a, b) => (growthKey(b) - growthKey(a)) || (b.delta - a.delta))
    .slice(0, TOP_GROWTH).map(c => { const { spikeMonths, ...rest } = c; return rest; });

  // 供應商佔比位移
  const tot0 = sumBy(recs.filter(r => r.year === Y0), r => r.vol);
  const tot1 = sumBy(recs.filter(r => r.year === Y1), r => r.vol);
  const supRows = [];
  for (const [s, arr] of groupBy(recs, r => r.supplier)) {
    const v0 = sumBy(arr.filter(r => r.year === Y0), r => r.vol);
    const v1 = sumBy(arr.filter(r => r.year === Y1), r => r.vol);
    const s0 = tot0 > 0 ? v0 / tot0 : 0, s1 = tot1 > 0 ? v1 / tot1 : 0;
    supRows.push({
      supplier: s, [`vol${Y0}`]: r2(v0), [`vol${Y1}`]: r2(v1),
      [`share${Y0}Pct`]: pct(s0), [`share${Y1}Pct`]: pct(s1), shiftPP: r2((s1 - s0) * 100), _t: v0 + v1,
    });
  }
  supRows.sort((a, b) => b._t - a._t);
  const topSuppliers = supRows.slice(0, TOP_SUPPLIERS).map(x => { const { _t, ...rest } = x; return rest; });

  // --- 2. 採購人員達交率 (月份以標準交貨日期歸屬) ---
  const delRecs = allRecs.filter(r => r.onTime !== null && r.sYear != null && YEARS.includes(r.sYear)
    && r.sMonth >= mf && r.sMonth <= mt);
  const rate = arr => arr.length ? arr.filter(r => r.onTime).length / arr.length : null;

  const deliveryByBuyer = [];
  for (const [buyer, arr] of groupBy(delRecs, r => r.buyer)) {
    const months = [];
    for (let m = 1; m <= 12; m++) {
      const a = arr.filter(r => r.sYear === Y0 && r.sMonth === m);
      const b = arr.filter(r => r.sYear === Y1 && r.sMonth === m);
      if (!a.length && !b.length) continue;
      const ra = rate(a), rb = rate(b);
      months.push({
        month: m, [`rate${Y0}Pct`]: pct(ra), [`n${Y0}`]: a.length,
        [`rate${Y1}Pct`]: pct(rb), [`n${Y1}`]: b.length,
        deltaPP: ra !== null && rb !== null ? r2((rb - ra) * 100) : null,
      });
    }
    deliveryByBuyer.push({ buyer, months, overallPct: pct(rate(arr)), lines: arr.length });
  }
  const delByIdx = groupBy(delRecs, r => r.sIdx);
  const deliveryOverall = [];
  for (const i of [...delByIdx.keys()].sort((a, b) => a - b)) {
    const arr = delByIdx.get(i);
    deliveryOverall.push({ ym: ymLabel(i), ratePct: pct(rate(arr)), lines: arr.length, late: arr.filter(r => !r.onTime).length });
  }

  // --- 3. 每月毛利率 ---
  const profitMonthly = [];
  for (const i of allIdx) {
    const arr = byIdxAll.get(i) || [];
    if (!arr.length) continue;
    const s = sumBy(arr, r => r.sales), c = sumBy(arr, r => r.cost);
    profitMonthly.push({ i, ym: ymLabel(i), year: Math.floor(i / 12), month: i % 12 + 1,
      sales: r2(s), cost: r2(c), grossProfit: r2(s - c), margin: margin(s, c), arr });
  }
  const findPm = (y, m) => profitMonthly.find(p => p.year === y && p.month === m) || null;

  const marginYoy = [];
  for (let m = 1; m <= 12; m++) {
    const a = findPm(Y0, m), b = findPm(Y1, m);
    if (!a && !b) continue;
    const ma = a ? a.margin : null, mb = b ? b.margin : null;
    marginYoy.push({ month: m, [`margin${Y0}Pct`]: pct(ma), [`margin${Y1}Pct`]: pct(mb),
      deltaPP: ma !== null && mb !== null ? r2((mb - ma) * 100) : null });
  }

  const yearlySummary = YEARS.map(y => {
    const ms = profitMonthly.filter(p => p.year === y);
    const s = sumBy(ms, p => p.sales), c = sumBy(ms, p => p.cost);
    return { year: y, months: ms.length,
      avgMonthlyMarginPct: pct(mean(ms.filter(p => p.margin !== null).map(p => p.margin))),
      aggregateMarginPct: pct(margin(s, c)), sales: r2(s), cost: r2(c) };
  });

  const aggName = arr => {
    const m = new Map();
    for (const r of arr) {
      if (!m.has(r.name)) m.set(r.name, { sales: 0, cost: 0 });
      const o2 = m.get(r.name);
      o2.sales += r.sales;
      o2.cost += r.cost;
    }
    return m;
  };

  const declineMonths = [];
  for (const cur of profitMonthly.filter(p => p.year === Y1)) {
    const prev = findPm(Y0, cur.month);
    if (!prev || cur.margin === null || prev.margin === null || cur.margin >= prev.margin) continue;
    const A = aggName(prev.arr), B = aggName(cur.arr);
    let items = [];
    for (const name of new Set([...A.keys(), ...B.keys()])) {
      const a = A.get(name) || { sales: 0, cost: 0 }, b = B.get(name) || { sales: 0, cost: 0 };
      const dSales = b.sales - a.sales, dCost = b.cost - a.cost;
      const dGp = (b.sales - b.cost) - (a.sales - a.cost);
      let driver;
      if (!A.has(name)) driver = '去年同月無下單(今年新增品項)';
      else if (!B.has(name)) driver = '今年同月無下單';
      else if (dCost > 0 && dSales < 0) driver = '成本增加+銷售額下降';
      else if (dCost > 0 && dSales >= 0) driver = (dSales > 0 && dCost > dSales) ? '成本增幅大於銷售增幅' : '成本增加';
      else if (dSales < 0) driver = (dCost < 0 && -dSales > -dCost) ? '銷售降幅大於成本降幅' : '銷售額下降';
      else driver = '其他';
      items.push({ name, dSales: r2(dSales), dCost: r2(dCost), dGrossProfit: r2(dGp), driver });
    }
    items = items.filter(x => x.dGrossProfit < 0).sort((a, b) => a.dGrossProfit - b.dGrossProfit).slice(0, 5);
    declineMonths.push({
      ym: cur.ym, comparedWith: prev.ym,
      marginPct: pct(cur.margin), prevYearMarginPct: pct(prev.margin),
      dropPP: r2((cur.margin - prev.margin) * 100),
      totalSalesChange: r2(cur.sales - prev.sales),
      totalCostChange: r2(cur.cost - prev.cost),
      topNegativeItems: items,
    });
  }

  // 3.2 最低毛利率品名 → 類別採購成本變動
  let nameAgg = [];
  for (const [name, arr] of groupBy(recs, r => r.name)) {
    const s = sumBy(arr, r => r.sales), c = sumBy(arr, r => r.cost);
    const mg = margin(s, c);
    if (mg !== null) nameAgg.push({ name, sales: s, cost: c, margin: mg, cats: [...new Set(arr.map(r => r.cat))], arr });
  }
  nameAgg = nameAgg.sort((a, b) => a.margin - b.margin).slice(0, LOWEST_MARGIN_N);

  const trend = arr => {
    const byIdx = groupBy(arr, r => r.idx);
    const ser = [];
    for (const i of allIdx) {
      const a = byIdx.get(i) || [];
      if (a.length) {
        const q = sumBy(a, r => r.qty), c = sumBy(a, r => r.cost);
        ser.push({ ym: ymLabel(i), cost: r2(c), qty: q, avgUnitCost: q > 0 ? r2(c / q) : null });
      }
    }
    return ser.map((p, k) => {
      const prev = k > 0 ? ser[k - 1].avgUnitCost : null;
      const mom = (k > 0 && p.avgUnitCost !== null && prev) ? pct((p.avgUnitCost - prev) / prev) : null;
      return { ...p, unitCostMomPct: mom };
    });
  };
  const lowest = nameAgg.map(x => ({
    name: x.name, marginPct: pct(x.margin), sales: r2(x.sales), cost: r2(x.cost),
    categories: x.cats, itemCostTrend: trend(x.arr),
    categoryCostTrend: x.cats.map(c => ({ category: c, trend: trend(recs.filter(r => r.cat === c)) })),
  }));

  return {
    filter: { status: '已確認', years: YEARS, volumeMetric: metric, sameCutoffMonth: cutoff, monthFrom: mf, monthTo: mt },
    totalRecords: recs.length,
    deliveryRecords: delRecs.length,
    dim1_orderVolume: {
      monthlyVolume, top3CategoryGrowth: top3, categoryVolatility: catSummary,
      volatileCategories: catSummary.filter(c => c.volatilityFlag).map(c => c.category),
      topSuppliers,
    },
    dim2_deliveryRate: { overallByMonth: deliveryOverall, byBuyer: deliveryByBuyer },
    dim3_grossMargin: {
      monthly: profitMonthly.map(p => ({ ym: p.ym, year: p.year, month: p.month, sales: p.sales, cost: p.cost,
        grossProfit: p.grossProfit, marginPct: pct(p.margin) })),
      yoyByMonth: marginYoy, yearlySummary, declineMonths, lowestMarginLinkage: lowest,
    },
  };
}

// ===== 渲染 =====
const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
function n(v) {
  if (v == null) return '—';
  const s = Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    .replace(/0+$/, '').replace(/\.$/, '');
  return (s === '-0' || s === '') ? '0' : s;
}
const p = v => v == null ? '—' : `${n(v)}%`;
function sgn(v, suffix = '', goodUp = true) {
  if (v == null) return '<td class="num muted">—</td>';
  const good = goodUp ? v > 0 : v < 0, bad = goodUp ? v < 0 : v > 0;
  const cls = good ? 'pos' : bad ? 'neg' : '';
  return `<td class="num ${cls}">${v > 0 ? '+' : ''}${n(v)}${suffix}</td>`;
}
function bar(v, mx, cls = '') {
  const w = (mx > 0 && v) ? Math.max(2, Math.min(100, v / mx * 100)) : 0;
  return `<div class="bar ${cls}"><i style="width:${Math.round(w * 100) / 100}%"></i></div>`;
}
// 表頭對齊: 依第一列的儲存格判斷哪些欄是數字欄 (class 含 num), 表頭一併靠右
function table(head, body, cls = '') {
  const cells = body.length ? (body[0].match(/<td class="[^"]*"/g) || []) : [];
  const th = head.map((h, i) => `<th${/\bnum\b/.test(cells[i] || '') ? ' class="num"' : ''}>${h}</th>`).join('');
  return `<div class="scroll"><table class="${cls}"><thead><tr>${th}</tr></thead><tbody>${body.join('')}</tbody></table></div>`;
}
const tr = cells => `<tr>${cells.join('')}</tr>`;
const td = (v, cls = '') => `<td class="${cls}">${v}</td>`;
const empty = msg => `<p class="muted">${msg}</p>`;

function renderReport(d, generatedAt) {
  const [Y0, Y1] = d.filter.years;
  const V = d.dim1_orderVolume, D = d.dim2_deliveryRate, G = d.dim3_grossMargin;
  const metricName = { lines: '明細行數', amount: '採購未稅金額', qty: '分批採購數量' }[d.filter.volumeMetric] || d.filter.volumeMetric;
  const cutoff = d.filter.sameCutoffMonth, mf = d.filter.monthFrom, mt = d.filter.monthTo;
  const period = (mf === 1 && mt === 12) ? '' : ` · 月份 ${mf}~${mt} 月`;

  const vol0 = sumBy(V.monthlyVolume.filter(m => m.month >= mf && m.month <= cutoff), m => m[Y0]);
  const vol1 = sumBy(V.monthlyVolume, m => m[Y1]);
  const ys = {};
  for (const y of G.yearlySummary) ys[y.year] = y;
  const kpis = [
    { l: `下單量 ${Y1} vs ${Y0} 同期`, v: n(vol1),
      s: vol0 > 0 ? `${vol1 >= vol0 ? '+' : ''}${n((vol1 - vol0) / vol0 * 100)}% (${Y0}: ${n(vol0)})` : '',
      c: vol1 >= vol0 ? 'pos' : 'neg' },
    { l: `整體毛利率 ${Y1}`, v: p(ys[Y1] && ys[Y1].aggregateMarginPct),
      s: `${Y0}: ${p(ys[Y0] && ys[Y0].aggregateMarginPct)}`, c: '' },
    { l: '波動異常類別 (MoM>30%)', v: V.volatileCategories.length,
      s: V.volatileCategories.join('、') || '無', c: V.volatileCategories.length ? 'neg' : 'pos' },
    { l: '毛利率低於去年同月', v: `${G.declineMonths.length} 個月`,
      s: G.declineMonths.map(x => x.ym).join('、') || '無', c: G.declineMonths.length ? 'neg' : 'pos' },
  ];

  // 1. 下單量
  const wan = v => n(Math.round(v / 1e7 * 100) / 100);  // 千萬為單位
  const monthlyTbl = table(['月份', `${Y0} 下單量`, `${Y0} 採購未稅金額(千萬)`, `${Y1} 下單量`, `${Y1} 採購未稅金額(千萬)`, '下單量年增率', '採購金額年增率'],
    V.monthlyVolume.filter(m => m[Y0] || m[Y1]).map(m =>
      tr([td(`${m.month} 月`), td(n(m[Y0]), 'num'), td(wan(m[`cost${Y0}`]), 'num'),
        td(n(m[Y1]), 'num'), td(wan(m[`cost${Y1}`]), 'num'), sgn(m.yoyPct, '%'), sgn(m.costYoyPct, '%')])),
    );

  const top3Tbl = table(['排名', '類別', `${Y0} 同期`, `${Y1}`, '增加量', '增幅'], V.top3CategoryGrowth.map((c, i) =>
    tr([td(i + 1),
      td(`<b>${esc(c.category)}</b>` + (c.isNewCategory ? ' <span class="tag">新類別</span>' : '')),
      td(n(c.basePeriod), 'num'), td(n(c.currentPeriod), 'num'), sgn(c.delta),
      c.growthPct === null ? '<td class="num muted">—</td>' : sgn(c.growthPct, '%')])));

  const kv = c => c.avgAbsMomPct !== null ? c.avgAbsMomPct : -1;
  const volRows = V.categoryVolatility.slice().sort((a, b) => kv(b) - kv(a));
  const volTbl = table(['類別', '平均 |MoM|', '狀態', '超標月份 (MoM)'], volRows.map(c =>
    tr([td(`<b>${esc(c.category)}</b>`), td(p(c.avgAbsMomPct), 'num'),
      td(c.volatilityFlag ? '<span class="tag bad">異常</span>' : '<span class="tag ok">正常</span>'),
      td(c.spikeMonths.length ? c.spikeMonths.map(s => `${esc(s.ym)} (${s.momPct > 0 ? '+' : ''}${n(s.momPct)}%)`).join('、') : '—', 'small')])));

  const supTbl = table(['供應商', `${Y0} 下單量`, `${Y0} 佔比`, `${Y1} 下單量`, `${Y1} 佔比`, '佔比位移'], V.topSuppliers.map(s =>
    tr([td(`<b>${esc(s.supplier)}</b>`), td(n(s[`vol${Y0}`]), 'num'), td(p(s[`share${Y0}Pct`]), 'num'),
      td(n(s[`vol${Y1}`]), 'num'), td(p(s[`share${Y1}Pct`]), 'num'), sgn(s.shiftPP, ' pp')])));

  // 2. 達交率
  const rateCls = r => r == null ? '' : r >= 90 ? 'pos' : r < 70 ? 'neg' : '';
  const delivTbl = table(['月份', '達交率', '', '明細數', '不達交'], D.overallByMonth.map(m =>
    tr([td(esc(m.ym)), td(p(m.ratePct), `num ${rateCls(m.ratePct)}`),
      td(bar(m.ratePct, 100, m.ratePct < 70 ? 'bneg' : 'b1')),
      td(n(m.lines), 'num'), td(n(m.late), 'num')])));
  const buyerHtml = D.byBuyer.slice().sort((a, b) => b.lines - a.lines).map(b =>
    `<details><summary><b>${esc(b.buyer)}</b> <span class="muted">整體達交率 ${p(b.overallPct)} · ${n(b.lines)} 筆</span></summary>`
    + table(['月份', `${Y0} 達交率`, `${Y0} 筆數`, `${Y1} 達交率`, `${Y1} 筆數`, '變化'], b.months.map(m =>
      tr([td(`${m.month} 月`), td(p(m[`rate${Y0}Pct`]), `num ${rateCls(m[`rate${Y0}Pct`])}`),
        td(n(m[`n${Y0}`]), 'num muted'),
        td(p(m[`rate${Y1}Pct`]), `num ${rateCls(m[`rate${Y1}Pct`])}`),
        td(n(m[`n${Y1}`]), 'num muted'), sgn(m.deltaPP, ' pp')]))) + '</details>').join('');

  // 3. 毛利率
  const yoyTbl = table(['月份', `${Y0} 毛利率`, `${Y1} 毛利率`, '變化'], G.yoyByMonth.map(m =>
    tr([td(`${m.month} 月`), td(p(m[`margin${Y0}Pct`]), 'num'), td(p(m[`margin${Y1}Pct`]), 'num'), sgn(m.deltaPP, ' pp')])));
  const sumTbl = table(['年度', '月數', '月平均毛利率', '整體毛利率', '銷售額(未稅)', '成本'], G.yearlySummary.map(y =>
    tr([td(`<b>${y.year}</b>`), td(y.months, 'num'), td(p(y.avgMonthlyMarginPct), 'num'),
      td(p(y.aggregateMarginPct), 'num'), td(n(y.sales), 'num'), td(n(y.cost), 'num')])));

  const declineCard = m => {
    const items = m.topNegativeItems.length
      ? table(['品名', '銷售額變化', '成本變化', '毛利變化', '主因'], m.topNegativeItems.map(i =>
        tr([td(esc(i.name), 'wrap'), sgn(i.dSales), sgn(i.dCost, '', false), sgn(i.dGrossProfit),
          td(`<span class="tag">${esc(i.driver)}</span>`)])))
      : empty('沒有毛利為負貢獻的品名');
    return `<div class="card"><h4>${esc(m.ym)} <span class="muted">vs ${esc(m.comparedWith)}</span></h4>`
      + `<p>毛利率 <b>${p(m.marginPct)}</b>（去年同月 ${p(m.prevYearMarginPct)}，`
      + `<span class="neg">${n(m.dropPP)} pp</span>）· 銷售額變化 ${n(m.totalSalesChange)} · `
      + `成本變化 ${n(m.totalCostChange)}</p>${items}</div>`;
  };
  const declineHtml = G.declineMonths.map(declineCard).join('') || empty('沒有毛利率低於去年同月的月份');

  const trendTbl = rows => table(['月份', '成本', '數量', '平均單位成本', 'MoM'], rows.map(t =>
    tr([td(esc(t.ym)), td(n(t.cost), 'num'), td(n(t.qty), 'num'), td(n(t.avgUnitCost), 'num'),
      sgn(t.unitCostMomPct, '%', false)])));
  const linkHtml = G.lowestMarginLinkage.map((x, i) =>
    `<div class="card"><h4>#${i + 1} ${esc(x.name)}</h4>`
    + `<p>毛利率 <b class="${x.marginPct < 0 ? 'neg' : ''}">${p(x.marginPct)}</b> · 銷售額 ${n(x.sales)} · `
    + `成本 ${n(x.cost)} · 類別 ${x.categories.map(esc).join('、')}</p>`
    + `<details><summary>此品名每月採購成本</summary>${trendTbl(x.itemCostTrend)}</details>`
    + x.categoryCostTrend.map(c => `<details><summary>類別 ${esc(c.category)} 採購成本趨勢</summary>${trendTbl(c.trend)}</details>`).join('')
    + '</div>').join('');

  const kpiHtml = kpis.map(k =>
    `<div class="kpi"><div class="l">${esc(k.l)}</div><div class="v ${k.c}">${esc(k.v)}</div><div class="s">${esc(k.s)}</div></div>`).join('');

  return `<div class="sub">${Y0} vs ${Y1}${period} · 只計「${esc(d.filter.status)}」· 採購明細 ${n(d.totalRecords)} 筆 · 達交率明細 ${n(d.deliveryRecords)} 筆 · 下單量以${esc(metricName)}計算 · 資料產生時間 ${esc(generatedAt)}</div>
<div class="kpis">${kpiHtml}</div>

<h2>1. 每月下單量</h2>
<div class="note">增幅採同期比較：${Y0} 年 ${mf}~${cutoff} 月 vs ${Y1} 年 ${mf}~${cutoff} 月。</div>
<h3>逐月趨勢</h3>${monthlyTbl}
<div class="note">採購未稅金額為該月（依採購日期歸屬）明細的採購未稅金額加總，單位：千萬。下單量以明細行數計算。</div>
<h3>增幅最高前三名類別（料件編號第一碼）</h3>${top3Tbl}
<h3>單量穩定度（月對月波動，平均 |MoM| &gt; 30% 為異常）</h3>${volTbl}
<h3>前五大供應商佔比位移（pp = 百分點）</h3>${supTbl}

<h2>2. 採購人員達交率</h2>
<div class="note">月份以標準交貨日期歸屬；標準交貨日 − 廠商交貨日 ≥ 0 為達交，&lt; 0 為不達交。紅字 &lt;70%，綠字 ≥90%。</div>
<h3>全體逐月</h3>${delivTbl}
<h3>各採購人員（點開看逐月 ${Y0} vs ${Y1}）</h3>${buyerHtml}

<h2>3. 毛利率</h2>
<div class="note">毛利率 = (銷售金額(未稅) − 採購未稅金額) ÷ 銷售金額(未稅)。</div>
<h3>年度比較</h3>${sumTbl}
<h3>每月毛利率 ${Y0} vs ${Y1}</h3>${yoyTbl}
<h3>毛利率低於去年同月的月份：品名拆解</h3>${declineHtml}
<h3>毛利率最低品名與採購端成本趨勢</h3>${linkHtml}`;
}

// ===== 頁面互動 =====
function initPage() {
  const DATA = JSON.parse(document.getElementById('data').textContent);
  const recs = buildRecs(DATA);
  const $ = id => document.getElementById(id);
  const opts = (vals, sel, fmt) => vals.map(v => `<option value="${v}"${v === sel ? ' selected' : ''}>${fmt ? fmt(v) : v}</option>`).join('');
  const months = Array.from({ length: 12 }, (_, i) => i + 1);
  const def = DATA.defaults;

  $('controls').innerHTML =
    `<label>基準年 <select id="y0">${opts(DATA.years, def.y0)}</select></label>`
    + `<label>比較年 <select id="y1">${opts(DATA.years, def.y1)}</select></label>`
    + `<label>月份 <select id="mf">${opts(months, def.mf)}</select> ~ <select id="mt">${opts(months, def.mt)}</select></label>`;

  function update() {
    const o = { Y0: +$('y0').value, Y1: +$('y1').value, mf: +$('mf').value, mt: +$('mt').value, metric: def.metric };
    const out = $('report');
    document.title = `採購分析報告 ${o.Y0}-${o.Y1}`;
    if (o.Y0 === o.Y1) { out.innerHTML = empty('基準年與比較年不能相同'); return; }
    if (o.mf > o.mt) { out.innerHTML = empty('起始月不能大於結束月'); return; }
    const d = analyze(recs, o);
    out.innerHTML = d.error ? empty(esc(d.error)) : renderReport(d, DATA.generatedAt);
  }
  for (const id of ['y0', 'y1', 'mf', 'mt']) $(id).addEventListener('change', update);
  update();
}

if (typeof document !== 'undefined' && document.getElementById('data')) initPage();
if (typeof module !== 'undefined') module.exports = { buildRecs, analyze, renderReport };
