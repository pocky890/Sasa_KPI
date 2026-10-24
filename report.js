'use strict';
// 報告端 (瀏覽器) 的分析 + 渲染: 邏輯與 analyze.py 的 analyze() 一致, 讓使用者在報告上直接選年度/月份/下單量定義

const MOM_THRESHOLD = 0.3;
const TOP_GROWTH = 5;
const NO_BRAND_LINES = ['硬體維護'];  // 這些產品線 / 品項不另外列占比最高廠牌 (與 codebook.py 的 NO_BRAND_LINES 一致)
const NO_BASE = 0;  // 基準年選「無」時的代號 (不做年度比較, 只看比較年的月份 / 季度)
const TOP_BRAND_GROWTH = 5;     // 廠牌 YoY/QoQ/MoM 每個第一碼列出前 N 個廠牌 (依當期金額)
const TOP_DELIVERY_BRANDS = 5;   // 廠牌達交率每個第一碼列出前 N 個廠牌 (依筆數)
const TOP_BUYER_ITEMS = 5;       // 各採購人員達交率: 每位採購人員、每個第一碼列出前 N 個品項 (依筆數)
// 採購未到貨代號為這些原因者為「資料例外」, 不計入達交率分母 (與 analyze.py 的 DELIVERY_EXCEPTIONS 一致)
const DELIVERY_EXCEPTION_REASONS = ['資料不齊-(業務)', '訂單暫停-(業務)', '客戶因素-(客戶)', '其他原因-(其他)', '倉庫未收-(倉庫)'];
// 不納入增幅排名的名稱 (與 codebook.py 的 is_unclassified 一致)
const METRIC_NAMES = { lines: '明細行數', amount: '採購未稅金額', qty: '分批採購數量' };
const isUnclassified =s => s === '(未對應碼表)' || s === '(未識別)' || s === '(未歸類)' || s.endsWith('不分類');
const LOWEST_MARGIN_N = 3;
const HL_TOP = 3;           // 一頁重點: 每個清單列出前 N 項
const MIN_RATE_LINES = 30;  // 達交率樣本數門檻: 少於這個筆數的達交率只是雜訊, 表內加 † 並淡色顯示, 一頁重點的排名也不計
const RATE_LOW = 70, RATE_GOOD = 90;  // 達交率紅字 / 綠字門檻 (%)
const DQ_WARN = 10;         // 資料品質: 未歸類品項 / 未識別廠牌占比達這個值 (%) 以紅字標示

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
      line: D.line[r[12]], brand: D.brand[r[13]], item: D.item[r[14]], exc: r[15] >= 0 ? D.exc[r[15]] : null, dropBuyer: r[16] === 1,
    };
  });
}

// ===== 分析 =====
function analyze(allRecsFull, o) {
  const Y0 = o.Y0, Y1 = o.Y1, mf = o.mf, mt = o.mt, metric = o.metric;
  const noBase = Y0 === NO_BASE;  // 無基準年: 基準年數值全為 0, 排名即依比較年數值
  const YEARS = [Y0, Y1];
  // 下單量/毛利率以採購日期歸屬
  // recsM: 全部明細 (毛利率分析用); allRecs / recs: 排除採購人員 (BUYERS_EXCLUDED) 後, 供下單量與達交率分析用
  const allRecs = allRecsFull.filter(r => !r.dropBuyer);
  const recsM = allRecsFull.filter(r => r.year != null && YEARS.includes(r.year) && r.month >= mf && r.month <= mt);
  for (const r of recsM) r.vol = metric === 'amount' ? r.cost : metric === 'qty' ? r.qty : 1;
  const recs = recsM.filter(r => !r.dropBuyer);
  if (!recs.length) return { error: '沒有符合條件(狀態=已確認且採購日期在指定年度/月份)的資料' };

  const minIdx = minBy(recsM, r => r.idx), maxIdx = maxBy(recsM, r => r.idx);
  const allIdx = [];
  for (let i = minIdx; i <= maxIdx; i++) allIdx.push(i);
  const y1Months = recs.filter(r => r.year === Y1).map(r => r.month);
  const cutoff = y1Months.length ? y1Months.reduce((a, b) => Math.max(a, b), -Infinity) : 12;
  const byIdxAll = groupBy(recsM, r => r.idx);

  const monthSeries = (arr, valFn) => {
    const byIdx = groupBy(arr, r => r.idx);
    return allIdx.map(i => ({ i, ym: ymLabel(i), v: sumBy(byIdx.get(i) || [], valFn) }));
  };
  const momList = series => series.map((p, k) => {
    const prev = k > 0 ? series[k - 1].v : 0;
    return { ym: p.ym, value: r2(p.v), mom: k > 0 && prev > 0 ? (p.v - prev) / prev : null };
  });

  // --- 1. 每月下單量 ---
  const monthlyOf = arr => {
    const rows = [];
    for (let m = 1; m <= 12; m++) {
      const a = sumBy(arr.filter(r => r.year === Y0 && r.month === m), r => r.vol);
      const b = sumBy(arr.filter(r => r.year === Y1 && r.month === m), r => r.vol);
      const ca = sumBy(arr.filter(r => r.year === Y0 && r.month === m), r => r.cost);
      const cb = sumBy(arr.filter(r => r.year === Y1 && r.month === m), r => r.cost);
      rows.push({ month: m, [Y0]: r2(a), [Y1]: r2(b), yoyPct: a > 0 ? pct((b - a) / a) : null,
        [`cost${Y0}`]: r2(ca), [`cost${Y1}`]: r2(cb), costYoyPct: ca > 0 ? pct((cb - ca) / ca) : null });
    }
    return rows;
  };
  const monthlyVolume = monthlyOf(recs);
  // 依料件編號第一碼區分的逐月下單量 / 採購未稅金額 (同樣的欄位, 每個第一碼一組)
  const monthlyByCat = [...groupBy(recs, r => r.cat)].map(([cat, arr]) => ({ category: cat, months: monthlyOf(arr) }));

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
      isNewCategory: !noBase && base === 0 && cur > 0,
      avgAbsMomPct: pct(avgAbs),
      volatilityFlag: avgAbs !== null && avgAbs > MOM_THRESHOLD,
      spikeMonths: series.filter(s => s.mom !== null && Math.abs(s.mom) > MOM_THRESHOLD)
        .map(s => ({ ym: s.ym, value: s.value, momPct: pct(s.mom) })),
    });
  }
  // 每個分析項目都呈現 下單量 與 採購未稅金額 (基準年同期 / 比較年), 占比與增幅在顯示端由這四個數值算出
  const inBase = r => r.year === Y0 && r.month >= mf && r.month <= cutoff;
  const measures = arr => ({
    volBase: r2(sumBy(arr.filter(inBase), r => r.vol)), volCur: r2(sumBy(arr.filter(r => r.year === Y1), r => r.vol)),
    costBase: r2(sumBy(arr.filter(inBase), r => r.cost)), costCur: r2(sumBy(arr.filter(r => r.year === Y1), r => r.cost)),
  });
  // 產品線下單量增加量排名 (同期比較同上), 依料件編號第一碼分別排名; 每個產品線併列其中占比最高的廠牌
  // 產品線內「已識別」廠牌中, 比較年下單量最高者及其占比 (占比 = 廠牌下單量 ÷ 產品線下單量); 全部未識別則 null
  // field: 下單量 vol / 採購未稅金額 cost
  const topBrand = (arr, baseTotal, curTotal, field = 'vol') => {
    let best = null;
    for (const [b, a] of groupBy(arr, r => r.brand)) {
      if (isUnclassified(b)) continue;
      const v0 = sumBy(a.filter(r => r.year === Y0 && r.month >= mf && r.month <= cutoff), r => r[field]);
      const v1 = sumBy(a.filter(r => r.year === Y1), r => r[field]);
      if (v1 > 0 && (!best || v1 > best[2] || (v1 === best[2] && v0 > best[1]))) best = [b, v0, v1];  // 比較年沒有該廠牌的數值就不列
    }
    if (!best) return null;
    const s0 = baseTotal > 0 ? best[1] / baseTotal : null, s1 = curTotal > 0 ? best[2] / curTotal : null;
    return { name: best[0], basePeriod: r2(best[1]), currentPeriod: r2(best[2]), baseSharePct: pct(s0), currentSharePct: pct(s1) };
  };
  const topGrowth = () => {
    const groups = [];
    for (const [cat, catArr] of groupBy(recs, r => r.cat)) {
      const rows = [];
      for (const [key, arr] of groupBy(catArr, r => r.line)) {
        const base = sumBy(arr.filter(r => r.year === Y0 && r.month >= mf && r.month <= cutoff), r => r.vol);
        const cur = sumBy(arr.filter(r => r.year === Y1), r => r.vol);
        rows.push({ name: key, basePeriod: r2(base), currentPeriod: r2(cur), delta: r2(cur - base),
          growthPct: base > 0 ? pct((cur - base) / base) : null, isNew: !noBase && base === 0 && cur > 0,
          ...measures(arr), topBrand: NO_BRAND_LINES.includes(key) ? null : topBrand(arr, base, cur) });
      }
      const tot1 = sumBy(rows, x => x.currentPeriod);
      const skip = rows.filter(x => isUnclassified(x.name));
      const keep = rows.filter(x => !isUnclassified(x.name))
        .sort((a, b) => (b.delta - a.delta) || ((b.growthPct || 0) - (a.growthPct || 0)));
      groups.push({ category: cat, top: keep.slice(0, TOP_GROWTH), totals: measures(catArr), _t: tot1,
        excludedSharePct: tot1 > 0 ? pct(sumBy(skip, x => x.currentPeriod) / tot1) : null });
    }
    return groups.sort((a, b) => b._t - a._t).map(({ _t, ...rest }) => rest);
  };
  const topLines = topGrowth();

  // 前5大交易品項: 依料件編號第一碼分別; 品項依 品名關鍵字 → 子類別名稱 → 類別名稱 辨識歸類 (見 codebook.py item);
  // 以採購未稅金額加總 (與下單量定義無關), 依比較年金額排序, 基準年取同期
  const topItems = () => {
    const groups = [];
    for (const [cat, catArr] of groupBy(recs, r => r.cat)) {
      const rows = [];
      for (const [key, arr] of groupBy(catArr, r => r.item)) {
        const base = sumBy(arr.filter(r => r.year === Y0 && r.month >= mf && r.month <= cutoff), r => r.cost);
        const cur = sumBy(arr.filter(r => r.year === Y1), r => r.cost);
        rows.push({ name: key, basePeriod: r2(base), currentPeriod: r2(cur), delta: r2(cur - base),
          growthPct: base > 0 ? pct((cur - base) / base) : null, isNew: !noBase && base === 0 && cur > 0,
          ...measures(arr), topBrand: NO_BRAND_LINES.includes(key) ? null : topBrand(arr, base, cur, 'cost') });
      }
      const tot1 = sumBy(rows, x => x.currentPeriod);
      const skip = rows.filter(x => isUnclassified(x.name));
      const keep = rows.filter(x => !isUnclassified(x.name))
        .sort((a, b) => (b.currentPeriod - a.currentPeriod) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
      const top = keep.slice(0, TOP_GROWTH);
      groups.push({ category: cat, top, totals: measures(catArr), _t: tot1,
        topSharePct: tot1 > 0 ? pct(sumBy(top, x => x.currentPeriod) / tot1) : null,
        excludedSharePct: tot1 > 0 ? pct(sumBy(skip, x => x.currentPeriod) / tot1) : null });
    }
    return groups.sort((a, b) => b._t - a._t).map(({ _t, ...rest }) => rest);
  };
  const topAmountItems = topItems();

  // 廠牌成長率 YoY / QoQ / MoM: 依料件編號第一碼分別; 廠牌依 品名 → 子類別名稱 → 類別名稱 → 第7-9碼 辨識 (見 codebook.py brand);
  // 以採購未稅金額 (依採購日期歸屬) 加總。以所選區間內比較年「最後一個完整月份」為基準:
  //   YoY: 比較年 起始月~基準月  vs 去年同期 (基準年同月份; 無基準年則不算)
  //   QoQ: 最後一個完整季度 vs 上一季
  //   MoM: 基準月 vs 上個月
  // QoQ / MoM 會用到區間以外的月份, 所以用全部資料 (allRecs) 計算, 不受月份篩選限制
  // 基準取「最後一個完整月份」: 資料最新日期 (o.dataEnd) 還沒到該月月底就退一個月, 避免未滿月造成增率失真
  const [dEy, dEm, dEd] = String(o.dataEnd || '').split('-').map(Number);
  const partialIdx = (dEy && dEd < new Date(dEy, dEm, 0).getDate()) ? dEy * 12 + dEm - 1 : null;
  const bgA0 = Y1 * 12 + cutoff - 1;
  const bgA = bgA0 === partialIdx ? bgA0 - 1 : bgA0;
  const bgAm = bgA % 12 + 1;
  const bgQEnd = Math.floor(bgA / 12) * 12 + Math.floor(bgAm / 3) * 3 - 1;  // 最後一個完整季度的最後一個月
  const bgStart = Y1 * 12 + mf - 1, bgShift = (Y1 - Y0) * 12;
  const bgWin = {
    yoy: (noBase || bgA < bgStart) ? null : { cur: [bgStart, bgA], prev: [bgStart - bgShift, bgA - bgShift] },
    qoq: { cur: [bgQEnd - 2, bgQEnd], prev: [bgQEnd - 5, bgQEnd - 3] },
    mom: { cur: [bgA, bgA], prev: [bgA - 1, bgA - 1] },
  };
  const qLabel = i => `${Math.floor(i / 12)} Q${Math.floor((i % 12) / 3) + 1}`;
  const bgLabels = {
    yoy: bgWin.yoy ? `${Y1} 年 ${mf}~${bgAm} 月 vs ${Y0} 年同月份` : null,
    qoq: `${qLabel(bgQEnd)} vs ${qLabel(bgQEnd - 3)}`,
    mom: `${ymLabel(bgA)} vs ${ymLabel(bgA - 1)}`,
    sharePeriod: bgA >= bgStart ? `${ymLabel(bgStart)} ~ ${ymLabel(bgA)}` : null,  // 當期占比的計算期間
    anchorNote: bgA !== bgA0 ? `${ymLabel(bgA0)} 資料尚未滿月（資料最新至 ${o.dataEnd}），以最近完整月份 ${ymLabel(bgA)} 為基準` : '',
  };
  const volOf = r => metric === 'amount' ? r.cost : metric === 'qty' ? r.qty : 1;  // 下單量 (依下單量定義)
  const bgSum = (arr, w, f = r => r.cost) => w ? r2(sumBy(arr.filter(r => r.idx != null && r.idx >= w[0] && r.idx <= w[1]), f)) : null;
  const bgRate = (c, p0) => (c !== null && p0 !== null && p0 > 0) ? pct((c - p0) / p0) : null;
  const bgRow = (name, arr) => {
    const row = { name, rank: bgSum(arr, [Y1 * 12 + mf - 1, bgA]), volRank: bgSum(arr, [Y1 * 12 + mf - 1, bgA], volOf) };
    for (const k of ['yoy', 'qoq', 'mom']) {
      const c = bgWin[k] ? bgSum(arr, bgWin[k].cur) : null, p0 = bgWin[k] ? bgSum(arr, bgWin[k].prev) : null;
      row[`${k}Cur`] = c; row[`${k}Prev`] = p0; row[`${k}Pct`] = bgRate(c, p0);
    }
    // 下單量的 YoY (當期 vs 去年同期), 與金額的 YoY 並列
    const vc = bgWin.yoy ? bgSum(arr, bgWin.yoy.cur, volOf) : null, vp = bgWin.yoy ? bgSum(arr, bgWin.yoy.prev, volOf) : null;
    row.volYoyPrev = vp; row.volYoyPct = bgRate(vc, vp);
    return row;
  };
  const brandGrowth = [...new Set(recs.map(r => r.cat))].map(cat => {
    const arr = allRecs.filter(r => r.cat === cat && r.idx != null);
    const by = groupBy(arr, r => r.brand);
    const cmpName = (x, y) => (x < y ? -1 : x > y ? 1 : 0);
    const rows = [...by].map(([k, a]) => bgRow(k, a));
    const known = rows.filter(x => !isUnclassified(x.name)).sort((x, y) => (y.rank - x.rank) || cmpName(x.name, y.name));
    // 只列已識別廠牌中當期金額最大的前 N 個 (不再列「其他」「未識別」彙總列); 「全部」為該第一碼的總計
    const total = bgRow('全部', arr), top = known.slice(0, TOP_BRAND_GROWTH);
    // 當期占比 = 廠牌當期金額 ÷ 該第一碼同期間金額合計 (含未識別廠牌)
    for (const x of [total, ...top]) {
      x.sharePct = total.rank > 0 ? pct(x.rank / total.rank) : null;
      x.volSharePct = total.volRank > 0 ? pct(x.volRank / total.volRank) : null;  // 下單量占比 (同樣以當期、含未識別為分母)
    }
    return { category: cat, total, rows: top };
  }).sort((a, b) => b.total.rank - a.total.rank);

  // --- 2. 採購人員達交率 (月份以標準交貨日期歸屬) ---
  // 標準交貨日 − 廠商交貨日 < 0 為不達交; 「資料例外」(採購未到貨代號) 另列, 不計入分母
  const inScope = allRecs.filter(r => r.onTime !== null && r.sYear != null && YEARS.includes(r.sYear)
    && r.sMonth >= mf && r.sMonth <= mt);
  const excRecs = inScope.filter(r => r.exc !== null);
  const delRecs = inScope.filter(r => r.exc === null);
  const deliveryExceptions = {
    total: excRecs.length,
    byReason: DELIVERY_EXCEPTION_REASONS.map(k => ({ reason: k, lines: excRecs.filter(r => r.exc === k).length,
      late: excRecs.filter(r => r.exc === k && !r.onTime).length })),
  };
  const rate = arr => arr.length ? arr.filter(r => r.onTime).length / arr.length : null;

  // 達交 / 未達交 的採購未稅金額 (達交率分母明細, 已排除資料例外) 與各自的同期年增率
  const splitCost = (a, b) => {
    const f = (arr, ok) => r2(sumBy(arr.filter(r => !!r.onTime === ok), r => r.cost));
    const on0 = f(a, true), on1 = f(b, true), lt0 = f(a, false), lt1 = f(b, false);
    return { [`costOn${Y0}`]: on0, [`costOn${Y1}`]: on1, costOnYoyPct: on0 > 0 ? pct((on1 - on0) / on0) : null,
      [`costLate${Y0}`]: lt0, [`costLate${Y1}`]: lt1, costLateYoyPct: lt0 > 0 ? pct((lt1 - lt0) / lt0) : null };
  };
  // 逐月、基準年 vs 比較年 (與每月下單量同樣的呈現方式)
  const deliveryMonths = arr => {
    const months = [];
    for (let m = 1; m <= 12; m++) {
      const a = arr.filter(r => r.sYear === Y0 && r.sMonth === m);
      const b = arr.filter(r => r.sYear === Y1 && r.sMonth === m);
      if (!a.length && !b.length) continue;
      const ra = rate(a), rb = rate(b);
      // 達交率分母明細 (已排除資料例外) 的 下單量 / 採購未稅金額 與年增率
      const va = sumBy(a, volOf), vb = sumBy(b, volOf), ca = sumBy(a, r => r.cost), cb = sumBy(b, r => r.cost);
      months.push({
        month: m, [`rate${Y0}Pct`]: pct(ra), [`n${Y0}`]: a.length,
        [`rate${Y1}Pct`]: pct(rb), [`n${Y1}`]: b.length,
        deltaPP: ra !== null && rb !== null ? r2((rb - ra) * 100) : null,
        [`vol${Y0}`]: r2(va), [`vol${Y1}`]: r2(vb), volYoyPct: va > 0 ? pct((vb - va) / va) : null,
        [`cost${Y0}`]: r2(ca), [`cost${Y1}`]: r2(cb), costYoyPct: ca > 0 ? pct((cb - ca) / ca) : null,
        ...splitCost(a, b),
      });
    }
    return months;
  };
  // 各組合計 (用於占比 = 該組 ÷ 全體)
  const delTotals = arr => {
    const a = arr.filter(r => r.sYear === Y0), b = arr.filter(r => r.sYear === Y1);
    return { vol0: r2(sumBy(a, volOf)), vol1: r2(sumBy(b, volOf)), cost0: r2(sumBy(a, r => r.cost)), cost1: r2(sumBy(b, r => r.cost)) };
  };
  const deliveryBy = keyFn => [...groupBy(delRecs, keyFn)].map(([key, arr]) =>
    ({ key, months: deliveryMonths(arr), overallPct: pct(rate(arr)), lines: arr.length, totals: delTotals(arr) }));
  const deliveryByBuyer = deliveryBy(r => r.buyer).map(({ key, ...rest }) => ({ buyer: key, ...rest }));
  const deliveryByCategory = deliveryBy(r => r.cat).map(({ key, ...rest }) => ({ category: key, ...rest }));
  const deliveryOverallYoY = { months: deliveryMonths(delRecs), overallPct: pct(rate(delRecs)), lines: delRecs.length,
    totals: delTotals(delRecs) };
  // 廠牌達交率: 依料件編號第一碼分別; 廠牌依 品名 → 子類別名稱 → 類別名稱 → 第7-9碼 辨識 (見 codebook.py brand);
  // 基準年 / 比較年以標準交貨日期歸屬, 資料例外筆數另列
  const brandRow = (name, arr, excArr) => {
    const a = arr.filter(r => r.sYear === Y0), b = arr.filter(r => r.sYear === Y1);
    const ra = rate(a), rb = rate(b);
    const va = sumBy(a, volOf), vb = sumBy(b, volOf), ca = sumBy(a, r => r.cost), cb = sumBy(b, r => r.cost);
    return { name, [`n${Y0}`]: a.length, [`late${Y0}`]: a.filter(r => !r.onTime).length, [`rate${Y0}Pct`]: pct(ra),
      [`n${Y1}`]: b.length, [`late${Y1}`]: b.filter(r => !r.onTime).length, [`rate${Y1}Pct`]: pct(rb),
      deltaPP: ra !== null && rb !== null ? r2((rb - ra) * 100) : null, exceptions: excArr.length,
      [`vol${Y0}`]: r2(va), [`vol${Y1}`]: r2(vb), volYoyPct: va > 0 ? pct((vb - va) / va) : null,
      [`cost${Y0}`]: r2(ca), [`cost${Y1}`]: r2(cb), costYoyPct: ca > 0 ? pct((cb - ca) / ca) : null,
      ...splitCost(a, b) };
  };
  const deliveryByBrand = [...new Set(inScope.map(r => r.cat))].sort().map(cat => {
    const catDel = delRecs.filter(r => r.cat === cat), catExc = excRecs.filter(r => r.cat === cat);
    const excBy = groupBy(catExc, r => r.brand), by = groupBy(catDel, r => r.brand);
    const cmpName = (x, y) => (x < y ? -1 : x > y ? 1 : 0);
    const known = [...new Set([...by.keys(), ...excBy.keys()])].filter(k => !isUnclassified(k))
      .sort((x, y) => ((by.get(y) || []).length - (by.get(x) || []).length) || cmpName(x, y));
    // 只列已識別廠牌中筆數最多的前 N 個 (不再列「其他」「未識別」彙總列); 「全部」為該第一碼的總計
    const rows = known.slice(0, TOP_DELIVERY_BRANDS).map(k => brandRow(k, by.get(k) || [], excBy.get(k) || []));
    return { category: cat, total: brandRow('全部', catDel, catExc), rows };
  }).sort((a, b) => (b.total[`n${Y0}`] + b.total[`n${Y1}`]) - (a.total[`n${Y0}`] + a.total[`n${Y1}`]));

  // 各採購人員達交率: 每位採購人員依料件編號第一碼分別, 每個第一碼列出筆數最多的前 N 個品項
  // (品項依 品名關鍵字 → 子類別名稱 → 類別名稱 → 碼表第4-6碼 歸類, 見 codebook.py item; 不分類 / 未歸類不列); 「全部」為該人員該第一碼的總計
  const cmpStr = (x, y) => (x < y ? -1 : x > y ? 1 : 0);
  const deliveryByBuyerItem = [...groupBy(delRecs, r => r.buyer)].map(([buyer, arr]) => {
    const excB = excRecs.filter(r => r.buyer === buyer);
    const cats = [...new Set(arr.map(r => r.cat))].sort().map(cat => {
      const catDel = arr.filter(r => r.cat === cat), catExc = excB.filter(r => r.cat === cat);
      const by = groupBy(catDel, r => r.item), excBy = groupBy(catExc, r => r.item);
      const known = [...new Set([...by.keys(), ...excBy.keys()])].filter(k => !isUnclassified(k))
        .sort((x, y) => ((by.get(y) || []).length - (by.get(x) || []).length) || cmpStr(x, y));
      return { category: cat, total: brandRow('全部', catDel, catExc),
        rows: known.slice(0, TOP_BUYER_ITEMS).map(k => brandRow(k, by.get(k) || [], excBy.get(k) || [])) };
    }).sort((a, b) => (b.total[`n${Y0}`] + b.total[`n${Y1}`]) - (a.total[`n${Y0}`] + a.total[`n${Y1}`]));
    return { buyer, overallPct: pct(rate(arr)), lines: arr.length, totals: delTotals(arr),
      total: brandRow('全部', arr, excB), cats };
  });

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
      sales: r2(s), cost: r2(c), vol: r2(sumBy(arr, r => r.vol)), grossProfit: r2(s - c), margin: margin(s, c), arr });
  }
  const findPm = (y, m) => profitMonthly.find(p => p.year === y && p.month === m) || null;

  const marginYoy = [];
  for (let m = 1; m <= 12; m++) {
    const a = findPm(Y0, m), b = findPm(Y1, m);
    if (!a && !b) continue;
    const ma = a ? a.margin : null, mb = b ? b.margin : null;
    // 同時呈現該月的 下單量 / 採購未稅金額 (= 成本) 與年增率
    const va = a ? a.vol : 0, vb = b ? b.vol : 0, ca = a ? a.cost : 0, cb = b ? b.cost : 0;
    marginYoy.push({ month: m, [`margin${Y0}Pct`]: pct(ma), [`margin${Y1}Pct`]: pct(mb),
      [`sales${Y0}`]: a ? a.sales : 0, [`sales${Y1}`]: b ? b.sales : 0,
      deltaPP: ma !== null && mb !== null ? r2((mb - ma) * 100) : null,
      [`vol${Y0}`]: va, [`vol${Y1}`]: vb, volYoyPct: va > 0 ? pct((vb - va) / va) : null,
      [`cost${Y0}`]: ca, [`cost${Y1}`]: cb, costYoyPct: ca > 0 ? pct((cb - ca) / ca) : null });
  }

  const yearlySummary = YEARS.map(y => {
    const ms = profitMonthly.filter(p => p.year === y);
    const s = sumBy(ms, p => p.sales), c = sumBy(ms, p => p.cost);
    return { year: y, months: ms.length,
      avgMonthlyMarginPct: pct(mean(ms.filter(p => p.margin !== null).map(p => p.margin))),
      aggregateMarginPct: pct(margin(s, c)), sales: r2(s), cost: r2(c), vol: r2(sumBy(ms, p => p.vol)) };
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
  for (const [name, arr] of groupBy(recsM, r => r.name)) {
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
    categoryCostTrend: x.cats.map(c => ({ category: c, trend: trend(recsM.filter(r => r.cat === c)) })),
  }));

  // 一頁重點用 (只在 HTML 報表, 不在 analyze.py 的 analysis.json)。比較期間取到「最後一個完整月份」(hlCut):
  // 資料最新日期還沒到月底的月份 (例如只有月初幾天) 不納入, 避免未滿月把下單量 / 金額增幅拉低
  const hlCut = bgA >= bgStart ? bgA % 12 + 1 : cutoff;
  const inHl = (r, y) => r.year === y && r.month >= mf && r.month <= hlCut;
  const hlSum = (arr, y, f) => sumBy(arr.filter(r => inHl(r, y)), f);
  const mkRow = (category, name, arr) => {
    const base = hlSum(arr, Y0, r => r.vol), cur = hlSum(arr, Y1, r => r.vol);
    return { category, name, basePeriod: r2(base), currentPeriod: r2(cur), delta: r2(cur - base),
      growthPct: base > 0 ? pct((cur - base) / base) : null, isNew: !noBase && base === 0 && cur > 0 };
  };
  const hlLines = [], hlCats = [];
  for (const [cat, catArr] of groupBy(recs, r => r.cat)) {
    hlCats.push(mkRow(cat, cat, catArr));
    for (const [key, arr] of groupBy(catArr, r => r.line)) if (!isUnclassified(key)) hlLines.push(mkRow(cat, key, arr));
  }
  const lineMovers = {
    up: hlLines.filter(x => noBase ? x.currentPeriod > 0 : x.delta > 0)
      .sort((a, b) => noBase ? b.currentPeriod - a.currentPeriod : b.delta - a.delta).slice(0, HL_TOP),
    down: noBase ? [] : hlLines.filter(x => x.delta < 0).sort((a, b) => a.delta - b.delta).slice(0, HL_TOP),
  };
  const hlTotals = { cut: hlCut, vol0: r2(hlSum(recs, Y0, r => r.vol)), vol1: r2(hlSum(recs, Y1, r => r.vol)),
    cost0: r2(hlSum(recs, Y0, r => r.cost)), cost1: r2(hlSum(recs, Y1, r => r.cost)) };

  // 資料品質 (只在 HTML 報表): 目前篩選範圍 (採購日期) 內的歸類 / 辨識結果與缺漏; 硬體維護不另列廠牌, 不算「未識別廠牌」
  const dqRow = (label, arr) => {
    const brandArr = arr.filter(r => !NO_BRAND_LINES.includes(r.item));
    const unItem = arr.filter(r => isUnclassified(r.item)), unBrand = brandArr.filter(r => isUnclassified(r.brand));
    return { label, lines: arr.length, cost: r2(sumBy(arr, r => r.cost)),
      unItemLines: unItem.length, unItemCost: r2(sumBy(unItem, r => r.cost)),
      brandLines: brandArr.length, unBrandLines: unBrand.length, unBrandCost: r2(sumBy(unBrand, r => r.cost)) };
  };
  const droppedBy = [...groupBy(recsM.filter(r => r.dropBuyer), r => r.buyer)].map(([buyer, a]) => ({ buyer, lines: a.length }))
    .sort((a, b) => b.lines - a.lines);
  const dataQuality = {
    byCat: [...groupBy(recs, r => r.cat)].map(([cat, arr]) => dqRow(`類別 ${cat}`, arr)).sort((a, b) => b.lines - a.lines),
    total: dqRow('全部', recs),
    allLines: recsM.length, droppedBuyers: droppedBy,
    noDelivery: recs.filter(r => r.onTime === null).length,
    unknownBuyer: recs.filter(r => r.buyer === '(未知)').length,
    nonPositiveCost: recs.filter(r => r.cost <= 0).length,
  };

  return {
    dataQuality,
    highlights: { lineMovers, catGrowth: hlCats.filter(x => x.growthPct != null).sort((a, b) => b.growthPct - a.growthPct),
      totals: hlTotals, partialNote: bgLabels.anchorNote, dataEnd: o.dataEnd || null },
    filter: { status: '已確認', years: YEARS, noBase, volumeMetric: metric, sameCutoffMonth: cutoff, monthFrom: mf, monthTo: mt },
    totalRecords: recs.length,
    deliveryRecords: delRecs.length,
    dim1_orderVolume: {
      monthlyVolume, monthlyByCat, topProductLineGrowth: topLines, topAmountItems,
      brandGrowth: { windows: bgLabels, groups: brandGrowth }, categoryVolatility: catSummary,
      volatileCategories: catSummary.filter(c => c.volatilityFlag).map(c => c.category),
    },
    dim2_deliveryRate: { overallByMonth: deliveryOverall, overallYoY: deliveryOverallYoY,
      byBuyer: deliveryByBuyer, byCategory: deliveryByCategory,
      byBrand: deliveryByBrand, byBuyerItem: deliveryByBuyerItem, exceptions: deliveryExceptions },
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
// 未選基準年 (Y0 = NO_BASE) 時, 隱藏所有基準年與「增加量 / 增幅 / 變化 / 位移」等比較欄位
const BASE_ONLY_HEADERS = ['增加量', '增幅', '變化', '單量年增率', '採購金額年增率', '達交金額年增率', '未達交金額年增率', '佔比位移', '占比位移',
  '下單量增幅', '採購金額增幅', 'YoY 去年同期金額', 'YoY 金額增幅', 'YoY 下單量增幅'];
let hideBase = false;
// 表頭對齊: 依第一列的儲存格判斷哪些欄是數字欄 (class 含 num), 表頭一併靠右
function table(head, body, cls = '') {
  if (hideBase) {
    const keep = head.map(h => !(String(h).startsWith(`${NO_BASE} `) || BASE_ONLY_HEADERS.includes(h)));
    head = head.filter((_, i) => keep[i]);
    body = body.map(row => {
      const cs = row.match(/<td\b[\s\S]*?<\/td>/g) || [];
      return cs.length === keep.length ? `<tr>${cs.filter((_, i) => keep[i]).join('')}</tr>` : row;
    });
  }
  const cells = body.length ? (body[0].match(/<td class="[^"]*"/g) || []) : [];
  const th = head.map((h, i) => `<th${/\bnum\b/.test(cells[i] || '') ? ' class="num"' : ''}>${h}</th>`).join('');
  return `<div class="scroll"><table class="${cls}"><thead><tr>${th}</tr></thead><tbody>${body.join('')}</tbody></table></div>`;
}
const tr = cells => `<tr>${cells.join('')}</tr>`;
const td = (v, cls = '') => `<td class="${cls}">${v}</td>`;
const empty = msg => `<p class="muted">${msg}</p>`;

function renderReport(d, generatedAt) {
  const [Y0, Y1] = d.filter.years;
  const noBase = !!d.filter.noBase;
  hideBase = noBase;                                       // 見 table(): 無基準年時隱藏基準年 / 比較欄位
  const vs = noBase ? `${Y1}` : `${Y0} vs ${Y1}`;          // 標題用的年度字樣
  const V = d.dim1_orderVolume, D = d.dim2_deliveryRate, G = d.dim3_grossMargin;
  const metricName = METRIC_NAMES[d.filter.volumeMetric] || d.filter.volumeMetric;
  const cutoff = d.filter.sameCutoffMonth, mf = d.filter.monthFrom, mt = d.filter.monthTo;
  const period = (mf === 1 && mt === 12) ? '' : ` · 月份 ${mf}~${mt} 月`;

  const vol0 = sumBy(V.monthlyVolume.filter(m => m.month >= mf && m.month <= cutoff), m => m[Y0]);
  const vol1 = sumBy(V.monthlyVolume, m => m[Y1]);
  const ys = {};
  for (const y of G.yearlySummary) ys[y.year] = y;
  const kpis = [
    noBase
      ? { l: `下單量 ${Y1}`, v: n(vol1), s: '未選基準年，不做年度比較', c: '' }
      : { l: `下單量 ${Y1} vs ${Y0} 同期`, v: n(vol1),
          s: vol0 > 0 ? `${vol1 >= vol0 ? '+' : ''}${n((vol1 - vol0) / vol0 * 100)}% (${Y0}: ${n(vol0)})` : '',
          c: vol1 >= vol0 ? 'pos' : 'neg' },
    { l: `整體毛利率 ${Y1}`, v: p(ys[Y1] && ys[Y1].aggregateMarginPct),
      s: noBase ? '' : `${Y0}: ${p(ys[Y0] && ys[Y0].aggregateMarginPct)}`, c: '' },
    noBase
      ? { l: '毛利率低於去年同月', v: '—', s: '未選基準年，無法比較', c: '' }
      : { l: '毛利率低於去年同月', v: `${G.declineMonths.length} 個月`,
          s: G.declineMonths.map(x => x.ym).join('、') || '無', c: G.declineMonths.length ? 'neg' : 'pos' },
  ];

  // 1. 下單量
  // 採購未稅金額 (含銷售額 / 成本 / 變化量) 一律以千萬為單位、三位小數; 不到 0.001 千萬者顯示「≈0」避免變成 0
  const wan = v => {
    if (v == null) return '—';
    const x = Math.round(v / 1e7 * 1000) / 1000;
    return x === 0 ? (v === 0 ? '0' : '≈0') : x.toLocaleString('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  };
  const sgnW = (v, goodUp = true) => {
    if (v == null) return '<td class="num muted">—</td>';
    const cls = v === 0 ? '' : (v > 0) === goodUp ? 'pos' : 'neg';
    return `<td class="num ${cls}">${v > 0 ? '+' : ''}${wan(v)}</td>`;
  };
  // 每個分析都呈現 下單量 / 採購未稅金額 / 占比 / 增幅: 占比 = 該列 ÷ 合計, 增幅 = (當期 − 同期) ÷ 同期
  const shareOf = (x, t) => t > 0 ? pct(x / t) : null;
  const rateOf = (c, b) => b > 0 ? pct((c - b) / b) : null;
  const growthCell = v => v === null ? '<td class="num muted">—</td>' : sgn(v, '%');
  const mRows = V.monthlyVolume.filter(m => m[Y0] || m[Y1]);
  // 逐月表只呈現同期比 (單量年增率 / 採購金額年增率), 不列「占全年」的占比
  const monthlyTbl = table(['月份', `${Y0} 下單量`, `${Y1} 下單量`, '單量年增率',
    `${Y0} 採購未稅金額(千萬)`, `${Y1} 採購未稅金額(千萬)`, '採購金額年增率'],
    mRows.map(m =>
      tr([td(`${m.month} 月`), td(n(m[Y0]), 'num'),
        td(n(m[Y1]), 'num'), sgn(m.yoyPct, '%'),
        td(wan(m[`cost${Y0}`]), 'num'), td(wan(m[`cost${Y1}`]), 'num'), sgn(m.costYoyPct, '%')])),
    );

  // 逐月趨勢 (依料件編號第一碼區分): 類別 H、S、T 直列
  const MONTHLY_CATS = ['H', 'S', 'T'];
  const monthlyCols = [
    { h: `${Y0} 下單量`, base: true, f: m => td(n(m[Y0]), 'num') },
    { h: `${Y1} 下單量`, f: m => td(n(m[Y1]), 'num') },
    { h: '單量年增率', base: true, f: m => growthCell(m.yoyPct) },
    { h: `${Y0} 採購未稅金額（千萬）`, base: true, f: m => td(wan(m[`cost${Y0}`]), 'num') },
    { h: `${Y1} 採購未稅金額（千萬）`, f: m => td(wan(m[`cost${Y1}`]), 'num') },
    { h: '採購金額年增率', base: true, f: m => growthCell(m.costYoyPct) },
  ];
  // 直列表: 群組由上而下排列, 每個群組一個區塊, 群組欄為合併儲存格; cols 中 base: true 者在「無基準年」時隱藏
  // groups: [{ label, months: [{ month 或 label, ... }] }] (months 即該群組的各列; 列有 label 時顯示 label, 否則顯示「N 月」);
  // 欄位函式 col.f(m) 回傳該列的儲存格; heads: [群組欄標題, 列欄標題]
  const stackedGroupTbl = (groups, cols, heads = ['類別', '月份']) => {
    const cs = cols.filter(c => !(noBase && c.base));
    if (!groups.length) return empty('沒有資料');
    const rowLabel = m => m.label != null ? `<b>${esc(m.label)}</b>` : `${m.month} 月`;
    const body = groups.map(g => g.months.map((m, i) =>
      `<tr${i === 0 ? ' class="gt"' : ''}>${i === 0 ? `<td rowspan="${g.months.length}" class="gname"><b>${esc(g.label)}</b></td>` : ''}`
      + `<td>${rowLabel(m)}</td>${cs.map(c => c.f(m)).join('')}</tr>`).join('')).join('');
    return `<div class="scroll"><table><thead><tr><th>${heads[0]}</th><th>${heads[1]}</th>${cs.map(c => `<th class="num">${c.h}</th>`).join('')}</tr></thead>`
      + `<tbody>${body}</tbody></table></div>`;
  };
  const monthlyCatTbl = stackedGroupTbl(MONTHLY_CATS.map(k => V.monthlyByCat.find(c => c.category === k)).filter(Boolean)
    .map(c => ({ label: `類別 ${c.category}`, months: c.months.filter(m => m[Y0] || m[Y1]) })), monthlyCols);

  // 產品線 / 品項共用: 第一欄 (產品線 / 品項) 右側為「占比最高廠牌」(只列名稱), 再來是下單量 + 採購未稅金額 各自的 同期 / 當期 / 增幅;
  // 排名依據的指標 (rankBy) 另列「增加量」。不列任何占比欄 (下單量占比 / 採購金額占比 / 廠牌占比)
  const measureTbl = (g, label, rankBy) => {
    const head = ['排名', label, '占比最高廠牌',
      `${Y0} 下單量（同期）`, `${Y1} 下單量`, ...(rankBy === 'vol' ? ['增加量'] : []), '下單量增幅',
      `${Y0} 採購未稅金額（同期，千萬）`, `${Y1} 採購未稅金額（千萬）`, ...(rankBy === 'cost' ? ['增加量'] : []), '採購金額增幅'];
    return table(head, g.top.map((c, i) => {
      const b = c.topBrand;
      return tr([td(i + 1), td(`<b>${esc(c.name)}</b>` + (c.isNew ? ' <span class="tag">新增</span>' : '')),
        b ? td(`<b>${esc(b.name)}</b>`) : td('—', 'muted'),
        td(n(c.volBase), 'num'), td(n(c.volCur), 'num'), ...(rankBy === 'vol' ? [sgn(c.delta)] : []),
        growthCell(rateOf(c.volCur, c.volBase)),
        td(wan(c.costBase), 'num'), td(wan(c.costCur), 'num'), ...(rankBy === 'cost' ? [sgnW(c.delta)] : []),
        growthCell(rateOf(c.costCur, c.costBase))]);
    }));
  };
  const measureNote = (noBase ? '' : `增幅 = (${Y1} − ${Y0} 同期) ÷ ${Y0} 同期；`) + `下單量以${esc(metricName)}計、採購未稅金額為金額加總。`;

  const topItemsHtml = V.topAmountItems.map(g => `<h4>第一碼 ${esc(g.category)}</h4>`
    + (g.top.length ? measureTbl(g, '品項', 'cost') : empty('沒有可排名的品項'))
    + `<div class="note">前五名合計佔第一碼 ${esc(g.category)} ${Y1} 年採購未稅金額的 ${p(g.topSharePct)}；`
    + `「不分類 / 未歸類」佔 ${p(g.excludedSharePct)}，不納入排名。${measureNote}占比最高廠牌 = 該品項內已識別廠牌中比較年採購未稅金額最高者。</div>`).join('');

  // 廠牌 YoY / QoQ / MoM (採購未稅金額): 依料件編號第一碼分別, 每個廠牌列出各比較的當期、同期金額與增率
  const bgRowHtml = (b, bold) => tr([td(bold ? `<b>${esc(b.name)}</b>` : esc(b.name)),
    td(n(b.volRank), 'num'), td(wan(b.rank), 'num'),
    td(wan(b.yoyPrev), 'num'), sgn(b.yoyPct, '%'), sgn(b.volYoyPct, '%'),
    td(wan(b.qoqCur), 'num'), td(wan(b.qoqPrev), 'num'), sgn(b.qoqPct, '%'),
    td(wan(b.momCur), 'num'), td(wan(b.momPrev), 'num'), sgn(b.momPct, '%')]);
  const brandGrowthHtml = V.brandGrowth.groups.map(g => `<h4>第一碼 ${esc(g.category)}</h4>`
    + table(['廠牌', '當期下單量', '當期採購金額（千萬）', 'YoY 去年同期金額（千萬）', 'YoY 金額增幅', 'YoY 下單量增幅',
      'QoQ 本季金額（千萬）', 'QoQ 上一季金額（千萬）', 'QoQ 金額增幅', 'MoM 本月金額（千萬）', 'MoM 上個月金額（千萬）', 'MoM 金額增幅'],
      [bgRowHtml(g.total, true), ...g.rows.map(b => bgRowHtml(b, false))])).join('');
  const bgW = V.brandGrowth.windows;

  // 產品線與廠牌合併: 每個前五名產品線併列其中占比最高 (已識別) 的廠牌及占比
  const topLineTbl = groups => groups.map(g => `<h4>第一碼 ${esc(g.category)}</h4>`
    + (g.top.length ? measureTbl(g, '產品線', 'vol') : empty('沒有可排名的產品線'))
    + `<div class="note">${Y1} 年「不分類 / 未歸類 / 未識別」佔 ${p(g.excludedSharePct)}，不納入排名。${measureNote}占比最高廠牌 = 該產品線內已識別廠牌中比較年下單量最高者。</div>`).join('');

  // 2. 達交率
  const rateCls = r => r == null ? '' : r >= RATE_GOOD ? 'pos' : r < RATE_LOW ? 'neg' : '';
  // 達交率儲存格: 該期間筆數 (cnt) 少於 MIN_RATE_LINES 時淡色並加 †, 不套紅 / 綠字 (樣本太少, 只供參考)
  const rateTd = (r, cnt) => r == null ? td('—', 'num muted')
    : cnt < MIN_RATE_LINES ? td(`${p(r)}†`, 'num lown') : td(p(r), `num ${rateCls(r)}`);
  // 金額基準達交率 = 達交採購未稅金額 ÷ (達交 + 未達交採購未稅金額), 同樣的明細範圍 (已排除資料例外)
  const amtPct = (on, late) => on + late > 0 ? pct(on / (on + late)) : null;
  const amtRateTd = (on, late, cnt) => rateTd(amtPct(on, late), cnt);
  // 與「每月下單量」相同的呈現: 逐月, 基準年 vs 比較年, 變化 (pp)
  // 每列同時呈現 達交率、下單量、採購未稅金額（千萬）與同期年增率 (達交率分母明細, 已排除資料例外)
  // 採購未稅金額拆成「達交」與「未達交」兩部分 (達交率分母明細, 已排除資料例外), 各自附同期年增率
  const delivYoyTbl = months => table(['月份', `${Y0} 達交率`, `${Y1} 達交率`, '變化',
    `${Y0} 金額達交率`, `${Y1} 金額達交率`,
    `${Y0} 下單量`, `${Y1} 下單量`, '單量年增率',
    `${Y0} 達交採購未稅金額（千萬）`, `${Y1} 達交採購未稅金額（千萬）`, '達交金額年增率',
    `${Y0} 未達交採購未稅金額（千萬）`, `${Y1} 未達交採購未稅金額（千萬）`, '未達交金額年增率'], months.map(m =>
    tr([td(`${m.month} 月`), rateTd(m[`rate${Y0}Pct`], m[`n${Y0}`]),
      rateTd(m[`rate${Y1}Pct`], m[`n${Y1}`]), sgn(m.deltaPP, ' pp'),
      amtRateTd(m[`costOn${Y0}`], m[`costLate${Y0}`], m[`n${Y0}`]), amtRateTd(m[`costOn${Y1}`], m[`costLate${Y1}`], m[`n${Y1}`]),
      td(n(m[`vol${Y0}`]), 'num'), td(n(m[`vol${Y1}`]), 'num'), growthCell(m.volYoyPct),
      td(wan(m[`costOn${Y0}`]), 'num'), td(wan(m[`costOn${Y1}`]), 'num'), growthCell(m.costOnYoyPct),
      td(wan(m[`costLate${Y0}`]), 'num'), td(wan(m[`costLate${Y1}`]), 'num'), growthCell(m.costLateYoyPct)])));
  const delivTbl = delivYoyTbl(D.overallYoY.months);

  // 廠牌達交率: 依料件編號第一碼分別, 各廠牌 Y0 vs Y1 (標準交貨日歸屬); 資料例外不計入分母, 另列筆數
  const brandDelivRow = (b, bold) => tr([td(bold ? `<b>${esc(b.name)}</b>` : esc(b.name)),
    rateTd(b[`rate${Y0}Pct`], b[`n${Y0}`]), amtRateTd(b[`costOn${Y0}`], b[`costLate${Y0}`], b[`n${Y0}`]),
    td(n(b[`vol${Y0}`]), 'num'), td(n(b[`late${Y0}`]), 'num muted'),
    rateTd(b[`rate${Y1}Pct`], b[`n${Y1}`]), amtRateTd(b[`costOn${Y1}`], b[`costLate${Y1}`], b[`n${Y1}`]),
    td(n(b[`vol${Y1}`]), 'num'), td(n(b[`late${Y1}`]), 'num muted'),
    sgn(b.deltaPP, ' pp'), growthCell(b.volYoyPct),
    td(wan(b[`costOn${Y0}`]), 'num'), td(wan(b[`costOn${Y1}`]), 'num'), growthCell(b.costOnYoyPct),
    td(wan(b[`costLate${Y0}`]), 'num'), td(wan(b[`costLate${Y1}`]), 'num'), growthCell(b.costLateYoyPct),
    td(n(b.exceptions), 'num muted')]);
  // 廠牌 / 品項共用的達交率表 (label 為第一欄標題); 第一列「全部」為該組總計
  const delivGroupTbl = (g, label) => table([label, `${Y0} 達交率`, `${Y0} 金額達交率`, `${Y0} 下單量`, `${Y0} 不達交`,
      `${Y1} 達交率`, `${Y1} 金額達交率`, `${Y1} 下單量`, `${Y1} 不達交`, '變化', '單量年增率',
      `${Y0} 達交採購未稅金額（千萬）`, `${Y1} 達交採購未稅金額（千萬）`, '達交金額年增率',
      `${Y0} 未達交採購未稅金額（千萬）`, `${Y1} 未達交採購未稅金額（千萬）`, '未達交金額年增率', '資料例外'],
      [brandDelivRow(g.total, true), ...g.rows.map(b => brandDelivRow(b, false))]);
  const groupHead = g => `<h4>第一碼 ${esc(g.category)} <span class="muted">整體達交率 ${p(g.total[`rate${Y0}Pct`])} → ${p(g.total[`rate${Y1}Pct`])}</span></h4>`;
  const brandDelivHtml = D.byBrand.map(g => groupHead(g) + delivGroupTbl(g, '廠牌')).join('');
  // 各採購人員 (點開看): 依料件編號第一碼分別, 每個第一碼列出筆數最多的前 N 個品項
  const buyerHtml = D.byBuyerItem.slice().sort((a, b) => b.lines - a.lines).map(b =>
    `<details><summary><b>${esc(b.buyer)}</b> <span class="muted">整體達交率 ${p(b.overallPct)} · ${n(b.lines)} 筆</span></summary>`
    + b.cats.map(g => groupHead(g) + (g.rows.length ? delivGroupTbl(g, '品項') : empty('沒有可列出的品項（皆為不分類／未歸類）'))).join('')
    + '</details>').join('');
  const excHtml = `資料例外（採購未到貨代號為 ${DELIVERY_EXCEPTION_REASONS.join('、')}）共 ${n(D.exceptions.total)} 筆，標記後不計入達交率分母：`
    + D.exceptions.byReason.map(r => `${esc(r.reason)} ${n(r.lines)} 筆（其中不達交 ${n(r.late)}）`).join('、') + '。';

  // 依料件編號第一碼區分: 類別 T、S、H、Y 在同一張逐月表中由上而下直列
  const WIDE_CATS = ['T', 'S', 'H', 'Y'];
  const catCols = [   // 每個類別一組欄位; base: true 者在「無基準年」時隱藏
    { h: `${Y0} 達交率`, base: true, f: m => rateTd(m[`rate${Y0}Pct`], m[`n${Y0}`]) },
    { h: `${Y1} 達交率`, f: m => rateTd(m[`rate${Y1}Pct`], m[`n${Y1}`]) },
    { h: '變化', base: true, f: m => sgn(m.deltaPP, ' pp') },
    { h: `${Y0} 金額達交率`, base: true, f: m => amtRateTd(m[`costOn${Y0}`], m[`costLate${Y0}`], m[`n${Y0}`]) },
    { h: `${Y1} 金額達交率`, f: m => amtRateTd(m[`costOn${Y1}`], m[`costLate${Y1}`], m[`n${Y1}`]) },
    { h: `${Y1} 下單量`, f: m => td(n(m[`vol${Y1}`]), 'num') },
    { h: '單量年增率', base: true, f: m => growthCell(m.volYoyPct) },
    { h: `${Y1} 達交採購未稅金額（千萬）`, f: m => td(wan(m[`costOn${Y1}`]), 'num') },
    { h: '達交金額年增率', base: true, f: m => growthCell(m.costOnYoyPct) },
    { h: `${Y1} 未達交採購未稅金額（千萬）`, f: m => td(wan(m[`costLate${Y1}`]), 'num') },
    { h: '未達交金額年增率', base: true, f: m => growthCell(m.costLateYoyPct) },
  ].filter(c => !(noBase && c.base));
  const wideCats = WIDE_CATS.map(k => D.byCategory.find(c => c.category === k)).filter(Boolean);
  const catWideTbl = stackedGroupTbl(wideCats.map(c => ({ label: `類別 ${c.category}`, months: c.months })), catCols);
  const catHtml = catWideTbl
    + `<div class="note">${wideCats.map(c => `類別 ${esc(c.category)}：整體達交率 ${p(c.overallPct)} · ${n(c.lines)} 筆`).join('　｜　')}。`
    + `類別 ${WIDE_CATS.join('、')} 由上而下直列；達交率以標準交貨日期歸屬，已排除資料例外。</div>`;

  // 各採購人員 × 料件編號第一碼 (以類別為主): 每個第一碼一個區塊, 第一列「全部」為該類別合計, 其下由上而下列出各採購人員
  // (欄位同上, 為整個區間合計; 採購人員依該類別的筆數由多到少排列)
  const buyerCatGroups = WIDE_CATS.map(k => {
    const tot = (D.byBrand.find(g => g.category === k) || {}).total;
    const buyers = D.byBuyerItem.map(b => {
      const g = b.cats.find(x => x.category === k);
      return g ? { ...g.total, label: b.buyer } : null;
    }).filter(Boolean).sort((a, b) => (b[`n${Y0}`] + b[`n${Y1}`]) - (a[`n${Y0}`] + a[`n${Y1}`]));
    return { label: `類別 ${k}`, months: [...(tot ? [{ ...tot, label: '全部' }] : []), ...buyers], nBuyers: buyers.length };
  }).filter(g => g.nBuyers > 0);
  const buyerCatTbl = stackedGroupTbl(buyerCatGroups, catCols, ['類別', '採購人員']);
  const buyerCatHtml = buyerCatTbl
    + `<div class="note">以類別（料件編號第一碼）為主，每個類別第一列「全部」為該類別合計，其下由上而下列出各採購人員（${buyerCatGroups.map(g => `類別 ${g.label.slice(3)} ${g.nBuyers} 位`).join('、')}），採購人員依該類別的筆數由多到少排列。達交率以標準交貨日期歸屬，已排除資料例外；金額單位：千萬。</div>`;

  // 3. 毛利率
  // 逐月: 毛利率之外同時呈現 下單量 / 採購未稅金額（= 成本，千萬）與同期年增率
  const yoyTbl = table(['月份', `${Y0} 毛利率`, `${Y1} 毛利率`, '變化', `${Y0} 下單量`, `${Y1} 下單量`, '單量年增率',
    `${Y0} 採購未稅金額（千萬）`, `${Y1} 採購未稅金額（千萬）`, '採購金額年增率'], G.yoyByMonth.map(m =>
    tr([td(`${m.month} 月`), td(p(m[`margin${Y0}Pct`]), 'num'), td(p(m[`margin${Y1}Pct`]), 'num'), sgn(m.deltaPP, ' pp'),
      td(n(m[`vol${Y0}`]), 'num'), td(n(m[`vol${Y1}`]), 'num'), growthCell(m.volYoyPct),
      td(wan(m[`cost${Y0}`]), 'num'), td(wan(m[`cost${Y1}`]), 'num'), growthCell(m.costYoyPct)])));
  // 年度比較: 各年度為所選區間內有資料的全部月份; 另加「同期增幅」列, 只比較兩年度都有資料的月份 (起始月~比較年最新月),
  // 毛利率為百分點差, 其餘為 %
  const sumRows = G.yearlySummary.filter(y => !(noBase && y.year === Y0)).map(y =>
    tr([td(`<b>${y.year}</b>`), td(y.months, 'num'), td(p(y.avgMonthlyMarginPct), 'num'),
      td(p(y.aggregateMarginPct), 'num'), td(n(y.vol), 'num'), td(wan(y.sales), 'num'), td(wan(y.cost), 'num')]));
  if (!noBase) {
    const same = G.yoyByMonth.filter(m => m.month >= mf && m.month <= cutoff);
    const agg = y => {
      const vol = sumBy(same, m => m[`vol${y}`]), sales = sumBy(same, m => m[`sales${y}`]), cost = sumBy(same, m => m[`cost${y}`]);
      const ms = same.map(m => m[`margin${y}Pct`]).filter(v => v != null);
      return { vol, sales, cost, avgM: ms.length ? mean(ms) : null, aggM: sales > 0 ? (sales - cost) / sales * 100 : null };
    };
    const a0 = agg(Y0), a1 = agg(Y1);
    const ppd = (x, y) => (x == null || y == null) ? null : r2(y - x);
    sumRows.push(tr([td(`<b>同期增幅（${mf}~${cutoff} 月）</b>`), td(same.length, 'num'),
      sgn(ppd(a0.avgM, a1.avgM), ' pp'), sgn(ppd(a0.aggM, a1.aggM), ' pp'),
      growthCell(rateOf(a1.vol, a0.vol)), growthCell(rateOf(a1.sales, a0.sales)), growthCell(rateOf(a1.cost, a0.cost))]));
  }
  const sumTbl = table(['年度', '月數', '月平均毛利率', '整體毛利率', '下單量', '銷售額(未稅，千萬)', '採購未稅金額（千萬，即成本）'], sumRows);

  const declineCard = m => {
    const items = m.topNegativeItems.length
      ? table(['品名', '銷售額變化（千萬）', '採購未稅金額（成本）變化（千萬）', '毛利變化（千萬）', '主因'], m.topNegativeItems.map(i =>
        tr([td(esc(i.name), 'wrap'), sgnW(i.dSales), sgnW(i.dCost, false), sgnW(i.dGrossProfit),
          td(`<span class="tag">${esc(i.driver)}</span>`)])))
      : empty('沒有毛利為負貢獻的品名');
    return `<div class="card"><h4>${esc(m.ym)} <span class="muted">vs ${esc(m.comparedWith)}</span></h4>`
      + `<p>毛利率 <b>${p(m.marginPct)}</b>（去年同月 ${p(m.prevYearMarginPct)}，`
      + `<span class="neg">${n(m.dropPP)} pp</span>）· 銷售額變化 ${wan(m.totalSalesChange)} 千萬 · `
      + `採購未稅金額（成本）變化 ${wan(m.totalCostChange)} 千萬</p>${items}</div>`;
  };
  const declineHtml = noBase ? empty('未選基準年，無法與去年同月比較毛利率。')
    : G.declineMonths.map(declineCard).join('') || empty('沒有毛利率低於去年同月的月份');

  const trendTbl = rows => table(['月份', '採購未稅金額（千萬，成本）', '數量', '平均單位成本', 'MoM'], rows.map(t =>
    tr([td(esc(t.ym)), td(wan(t.cost), 'num'), td(n(t.qty), 'num'), td(n(t.avgUnitCost), 'num'),
      sgn(t.unitCostMomPct, '%', false)])));
  const linkHtml = G.lowestMarginLinkage.map((x, i) =>
    `<div class="card"><h4>#${i + 1} ${esc(x.name)}</h4>`
    + `<p>毛利率 <b class="${x.marginPct < 0 ? 'neg' : ''}">${p(x.marginPct)}</b> · 銷售額 ${wan(x.sales)} 千萬 · `
    + `採購未稅金額（成本）${wan(x.cost)} 千萬 · 類別 ${x.categories.map(esc).join('、')}</p>`
    + `<details><summary>此品名每月採購成本</summary>${trendTbl(x.itemCostTrend)}</details>`
    + x.categoryCostTrend.map(c => `<details><summary>類別 ${esc(c.category)} 採購成本趨勢</summary>${trendTbl(c.trend)}</details>`).join('')
    + '</div>').join('');

  // ===== 一頁重點: 依目前篩選條件自動產生 (KPI 卡片 + 下單量 / 達交率 / 毛利率 三張重點卡) =====
  const sumM = (rows, f) => sumBy(rows, f);
  const HL = d.highlights;
  // 一頁重點的比較期間: mf ~ 最後一個完整月份 (hlCut), 兩個年度同期; 資料只到月初的月份不納入
  const { cut: hlCut, vol0: hv0, vol1: hv1, cost0, cost1 } = HL.totals;
  const dCut = hlCut;
  const dSel = D.overallYoY.months.filter(m => m.month >= mf && m.month <= dCut);
  const dRate = y => {
    const cnt = sumM(dSel, m => m[`n${y}`]);
    return cnt > 0 ? sumM(dSel, m => (m[`rate${y}Pct`] || 0) * m[`n${y}`]) / cnt : null;
  };
  const rate0 = noBase ? null : dRate(Y0), rate1 = dRate(Y1);
  const late0 = sumM(dSel, m => m[`costLate${Y0}`]), late1 = sumM(dSel, m => m[`costLate${Y1}`]);
  const on0 = sumM(dSel, m => m[`costOn${Y0}`]), on1 = sumM(dSel, m => m[`costOn${Y1}`]);
  const amtRate0 = noBase ? null : amtPct(on0, late0), amtRate1 = amtPct(on1, late1);
  const pctChg = (c, b) => b > 0 ? `${c >= b ? '+' : ''}${n((c - b) / b * 100)}%` : '';
  const hlKpis = [
    { l: `採購未稅金額 ${Y1}（千萬）`, v: wan(cost1),
      s: noBase ? '未選基準年，不做年度比較' : `${pctChg(cost1, cost0)}（${Y0} 同期：${wan(cost0)}）`,
      c: noBase || cost0 <= 0 ? '' : cost1 >= cost0 ? 'pos' : 'neg' },
    { l: noBase ? `下單量 ${Y1}` : `下單量 ${Y1} vs ${Y0} 同期`, v: n(hv1),
      s: noBase ? '未選基準年，不做年度比較' : hv0 > 0 ? `${pctChg(hv1, hv0)}（${Y0} 同期：${n(hv0)}）` : '',
      c: noBase || hv0 <= 0 ? '' : hv1 >= hv0 ? 'pos' : 'neg' },
    { l: `整體達交率 ${Y1}`, v: p(rate1 == null ? null : Math.round(rate1 * 100) / 100),
      s: noBase || rate0 == null || rate1 == null ? '' : `${Y0} 同期：${p(Math.round(rate0 * 100) / 100)}（${rate1 >= rate0 ? '+' : ''}${n(rate1 - rate0)} 個百分點）`,
      c: rate1 == null ? '' : rateCls(rate1) },
    { l: `未達交採購未稅金額 ${Y1}（千萬）`, v: wan(late1),
      s: noBase ? '' : `${pctChg(late1, late0)}（${Y0} 同期：${wan(late0)}）`,
      c: noBase || late0 <= 0 ? '' : late1 > late0 ? 'neg' : 'pos' },
    kpis[1], kpis[2],
  ];
  const growthTxt = x => x.isNew ? '新增' : x.growthPct == null ? '' : `${x.growthPct > 0 ? '+' : ''}${n(x.growthPct)}%`;
  const moverLi = (x, signed) => `<li>類別 ${esc(x.category)}　${esc(x.name)}　`
    + (noBase ? `${n(x.currentPeriod)}` : `<span class="${x.delta >= 0 ? 'pos' : 'neg'}">${x.delta > 0 ? '+' : ''}${n(x.delta)}${growthTxt(x) ? `（${growthTxt(x)}）` : ''}</span>`) + '</li>';
  const catLine = HL.catGrowth
    .map(c => `類別 ${esc(c.category)} <span class="${c.growthPct >= 0 ? 'pos' : 'neg'}">${c.growthPct > 0 ? '+' : ''}${n(c.growthPct)}%</span>`).join('、');
  const volCard = `<div class="card"><h4>下單量（以${esc(metricName)}計）</h4><ul>`
    + (HL.lineMovers.up.length ? `<li><b>${noBase ? '下單量最高的產品線' : '增加量最大的產品線'}</b><ul>${HL.lineMovers.up.map(x => moverLi(x)).join('')}</ul></li>` : '')
    + (HL.lineMovers.down.length ? `<li><b>下滑最多的產品線</b><ul>${HL.lineMovers.down.map(x => moverLi(x)).join('')}</ul></li>` : '')
    + (!noBase && catLine ? `<li><b>各類別增幅</b>：${catLine}</li>` : '')
    + '</ul></div>';
  // 達交率最低者: 只計比較年筆數 >= MIN_RATE_LINES 的 (樣本太少的達交率是雜訊)
  const lowRates = rows => rows.filter(x => x[`n${Y1}`] >= MIN_RATE_LINES && x[`rate${Y1}Pct`] != null)
    .sort((a, b) => a[`rate${Y1}Pct`] - b[`rate${Y1}Pct`]);
  const rateLi = (label, x) => `<li>${label}　<span class="${rateCls(x[`rate${Y1}Pct`])}">${p(x[`rate${Y1}Pct`])}</span>`
    + `（未達交 ${n(x[`late${Y1}`])} / ${n(x[`n${Y1}`])} 筆；金額達交率 ${p(amtPct(x[`costOn${Y1}`], x[`costLate${Y1}`]))}；未達交金額 ${wan(x[`costLate${Y1}`])} 千萬）</li>`;
  const buyerLow = lowRates(D.byBuyerItem.map(b => ({ ...b.total, name: b.buyer })));
  const brandLow = lowRates(D.byBrand.flatMap(g => g.rows.map(x => ({ ...x, cat: g.category }))));
  const belowBuyers = buyerLow.filter(x => x[`rate${Y1}Pct`] < RATE_LOW).length;
  const belowBrands = brandLow.filter(x => x[`rate${Y1}Pct`] < RATE_LOW).length;
  const delCard = `<div class="card"><h4>達交率（${Y1}，標準交貨日期歸屬）</h4><ul>`
    + `<li>金額達交率 ${Y1}：<b>${p(amtRate1)}</b>${noBase ? '' : `（${Y0} 同期：${p(amtRate0)}）`}　<span class="muted">（依採購未稅金額計，達交金額 ÷ 達交與未達交金額合計；件數基準為 ${p(rate1 == null ? null : Math.round(rate1 * 100) / 100)}）</span></li>`
    + `<li>達交率低於 ${RATE_LOW}% 的採購人員 ${belowBuyers} 位、廠牌 ${belowBrands} 個（只計 ${Y1} 筆數 ≥ ${MIN_RATE_LINES}；廠牌為各第一碼筆數前 ${TOP_DELIVERY_BRANDS} 名）</li>`
    + (buyerLow.length ? `<li><b>達交率最低的採購人員</b><ul>${buyerLow.slice(0, HL_TOP).map(x => rateLi(esc(x.name), x)).join('')}</ul></li>` : '')
    + (brandLow.length ? `<li><b>達交率最低的廠牌</b><ul>${brandLow.slice(0, HL_TOP).map(x => rateLi(`類別 ${esc(x.cat)}　${esc(x.name)}`, x)).join('')}</ul></li>` : '')
    + `<li>資料例外 ${n(D.exceptions.total)} 筆，不計入達交率</li></ul></div>`;
  const marginCard = `<div class="card"><h4>毛利率（全部明細）</h4><ul>`
    + `<li>整體毛利率 ${Y1}：<b>${p(ys[Y1] && ys[Y1].aggregateMarginPct)}</b>${noBase ? '' : `（${Y0}：${p(ys[Y0] && ys[Y0].aggregateMarginPct)}）`}</li>`
    + (noBase ? '' : `<li>毛利率低於去年同月：${G.declineMonths.length ? `<span class="neg">${esc(G.declineMonths.map(x => x.ym).join('、'))}</span>` : '無'}</li>`)
    + '</ul></div>';
  const hlHtml = `<h2>一頁重點</h2>
<div class="kpis">${hlKpis.map(k => `<div class="kpi"><div class="l">${esc(k.l)}</div><div class="v ${k.c}">${esc(k.v)}</div><div class="s">${esc(k.s)}</div></div>`).join('')}</div>
<div class="hlgrid">${volCard}${delCard}${marginCard}</div>
<div class="note">依目前篩選條件自動產生${HL.dataEnd ? `；資料最新至 ${esc(HL.dataEnd)}` : ''}。${noBase ? `只看 ${Y1} 年 ${mf}~${hlCut} 月。` : `金額、下單量、達交率與產品線排名都比較 ${Y0} 與 ${Y1} 的同期（${mf}~${hlCut} 月）；`}${HL.partialNote ? esc(HL.partialNote) + '，所以不含該月。' : ''}達交率清單為 ${Y1} 全期間，只計筆數 ≥ ${MIN_RATE_LINES} 者。下單量與達交率已排除設定中不計入的採購人員；毛利率使用全部明細。分類與廠牌辨識的完整度見第 4 節「資料品質」。</div>`;

  // 4. 資料品質: 讓讀者自己判斷排名與廠牌分析的可信度 (未歸類品項 / 未識別廠牌愈多, 該類別的排名愈不可靠)
  const DQ = d.dataQuality;
  const dqShare = (x, t) => t > 0 ? pct(x / t) : null;
  const dqRowHtml = (r, bold) => {
    const sItem = dqShare(r.unItemLines, r.lines), sBrand = dqShare(r.unBrandLines, r.brandLines);
    const warn = s => s != null && s >= DQ_WARN ? 'neg' : '';
    return tr([td(bold ? `<b>${esc(r.label)}</b>` : esc(r.label)), td(n(r.lines), 'num'), td(wan(r.cost), 'num'),
      td(n(r.unItemLines), 'num'), td(p(sItem), `num ${warn(sItem)}`), td(wan(r.unItemCost), 'num'),
      td(n(r.unBrandLines), 'num'), td(p(sBrand), `num ${warn(sBrand)}`), td(wan(r.unBrandCost), 'num')]);
  };
  const dqTbl = table(['類別', '採購明細筆數', '採購未稅金額（千萬）', '未歸類品項 筆數', '佔該類別筆數', '未歸類品項 金額（千萬）',
    '未識別廠牌 筆數', '佔該類別筆數', '未識別廠牌 金額（千萬）'], [dqRowHtml(DQ.total, true), ...DQ.byCat.map(r => dqRowHtml(r, false))]);
  const dqHtml = `<h2>4. 資料品質</h2>
<div class="note">範圍同上方篩選（採購日期 ${noBase ? Y1 : `${Y0}、${Y1}`} 年 ${mf}~${mt} 月）${HL.dataEnd ? `；資料最新至 ${esc(HL.dataEnd)}` : ''}。
  分類與廠牌是依品名關鍵字、子類別、類別、料件編號碼表依序辨識出來的，辨識不出的只能放進「未歸類 / 未識別」，不納入各排名；某個類別的占比愈高，該類別的排名愈需要保留看待（占比 ≥ ${DQ_WARN}% 以紅字標示）。</div>
<h3>歸類與廠牌辨識</h3>${dqTbl}
<div class="note">未識別廠牌的占比分母不含「硬體維護」（該產品線依規則不另列廠牌）。未歸類品項包含碼表中的「不分類」。</div>
<h3>其他資料檢查</h3>
<ul class="dq">
  <li>採購明細 ${n(DQ.allLines)} 筆（含被排除的採購人員）；下單量與達交率分析使用其中 ${n(d.totalRecords)} 筆。
    ${DQ.droppedBuyers.length ? `不計入的採購人員：${DQ.droppedBuyers.map(b => `${esc(b.buyer)} ${n(b.lines)} 筆`).join('、')}（毛利率仍使用全部明細）。` : '沒有被排除的採購人員。'}</li>
  <li>無法判定達交（缺標準交貨日或廠商交貨日）：${n(DQ.noDelivery)} 筆，不計入達交率。</li>
  <li>達交率的資料例外：${n(D.exceptions.total)} 筆（以標準交貨日期歸屬，明細見第 2 節）。</li>
  <li>採購人員空白（顯示為「(未知)」）：${n(DQ.unknownBuyer)} 筆；採購未稅金額 ≤ 0：${n(DQ.nonPositiveCost)} 筆。</li>
</ul>`;

  return `<div class="sub">${vs}${period} · 只計「${esc(d.filter.status)}」· 採購明細 ${n(d.totalRecords)} 筆 · 達交率明細 ${n(d.deliveryRecords)} 筆 · 下單量以${esc(metricName)}計算 · 資料產生時間 ${esc(generatedAt)}</div>
${hlHtml}

<h2>1. 每月下單量</h2>
<div class="note">${noBase ? `只看 ${Y1} 年 ${mf}~${mt} 月（未選基準年，不做年度比較，排名依 ${Y1} 年數值）。`
  : `增幅採同期比較：${Y0} 年 ${mf}~${cutoff} 月 vs ${Y1} 年 ${mf}~${cutoff} 月。`}</div>
<h3>逐月趨勢</h3>${monthlyTbl}
<h3>逐月趨勢：依料件編號第一碼區分（類別 ${MONTHLY_CATS.join('、')}）</h3>${monthlyCatTbl}
<div class="note">類別 ${MONTHLY_CATS.join('、')} 由上而下直列（類別 Y 未列）；每個類別各自呈現下單量、採購未稅金額（千萬）與同期年增率。上方合計表含所有類別（包括 Y），所以 ${MONTHLY_CATS.join('、')} 加總會略少於合計。</div>
<div class="note">採購未稅金額為該月（依採購日期歸屬）明細的採購未稅金額加總，單位：千萬。下單量以${esc(metricName)}計；${noBase ? '' : `單量年增率 = (${Y1} 同月下單量 − ${Y0} 同月下單量) ÷ ${Y0} 同月下單量，採購金額年增率同理。`}</div>
<h3>${noBase ? '下單量最高' : '增加量最高'}前五名產品線與廠牌（依料件編號第一碼分別排名；依序用品名關鍵字、子類別名稱、類別名稱辨識歸類，都找不到才退回料件編號第4-6碼（產品線）／第7-9碼（廠牌）對照碼表；採購供應商：鼎華智能系統、FOUR J、SAP Ireland Limited 各自為一條產品線；下單量以${esc(metricName)}計）</h3>${topLineTbl(V.topProductLineGrowth)}
<div class="note">每個產品線只列一個廠牌：該產品線內已識別廠牌中下單量最高者（占比最高者）；未識別的下單量不參與比較。
  特別處理：① 採購供應商「鼎華智能系統」的品項，廠牌辨識不出時直接以「鼎華」取代空值；② 第一碼 T 的品項，採購供應商為群環科技、精技台北、精技台南、精技台中、精技新竹、台灣聯想環、漢碩科技且子類別名稱空白者，一律歸為「硬體維護」，該產品線不另外列廠牌。</div>
<h3>前五大交易品項（依料件編號第一碼分別排名；依序用品名關鍵字、子類別名稱、類別名稱辨識歸類；採購供應商：鼎華智能系統、FOUR J、SAP Ireland Limited 各自為一條產品線；以採購未稅金額加總，依 ${Y1} 年金額排序${noBase ? '' : `，${Y0} 年取同期比較`}）</h3>${topItemsHtml}
<h3>廠牌成長率 YoY / QoQ / MoM（依料件編號第一碼分別；廠牌依序用品名關鍵字、子類別名稱、類別名稱辨識歸類，都找不到才退回第7-9碼；以採購未稅金額加總，取同期比較；每個第一碼列出當期金額最大的前 ${TOP_BRAND_GROWTH} 個廠牌）</h3>
<div class="note">YoY（年增率）與去年同期比較：${bgW.yoy ? esc(bgW.yoy) : '未選基準年，不計算'}；QoQ（季增率）與上一季比較：${esc(bgW.qoq)}（最後一個完整季度）；MoM（月增率）與上個月比較：${esc(bgW.mom)}。
  ${bgW.anchorNote ? `${esc(bgW.anchorNote)}。` : ''}QoQ / MoM 不受上方月份篩選限制（會用到區間以外的月份）。增率 = (當期 − 比較期) ÷ 比較期，比較期為 0 時不計算。
  當期 = ${bgW.sharePeriod ? esc(bgW.sharePeriod) + '（起始月～基準月）' : '起始月～基準月'}；「全部」為該第一碼所有廠牌（含未識別）的合計，其餘僅列已識別廠牌中當期金額最大的前 ${TOP_BRAND_GROWTH} 個。下單量以${esc(metricName)}計；YoY 下單量增幅 = (當期下單量 − 去年同期下單量) ÷ 去年同期下單量。</div>${brandGrowthHtml}

<h2>2. 達交率（廠牌 / 料件編號第一碼 / 採購人員）</h2>
<div class="note">月份以標準交貨日期歸屬；標準交貨日 − 廠商交貨日 ≥ 0 為達交，&lt; 0 為不達交。紅字 &lt;70%，綠字 ≥90%。</div>
<div class="note">${excHtml}</div>
<div class="note">金額達交率 = 達交採購未稅金額 ÷（達交 + 未達交採購未稅金額），與件數基準的達交率並列：一筆大金額延遲和一筆小金額延遲在件數上相同，但對採購的影響差很多，兩個數字差距大時要特別留意。<b>†</b> 表示該期間筆數少於 ${MIN_RATE_LINES} 筆，達交率只供參考（淡色顯示、不套紅綠字）。</div>
<div class="note">達交採購未稅金額 = 達交明細的採購未稅金額加總；未達交採購未稅金額 = 不達交明細的採購未稅金額加總（兩者合計即達交率分母明細的採購未稅金額，已排除資料例外）；金額單位：千萬。</div>
<h3>廠牌達交率（依料件編號第一碼分別；廠牌依序用品名關鍵字、子類別名稱、類別名稱辨識歸類，都找不到才退回第7-9碼；每個第一碼列出筆數最多的前 ${TOP_DELIVERY_BRANDS} 個廠牌）</h3>${brandDelivHtml}
<h3>全體逐月 ${vs} <span class="muted">整體達交率 ${p(D.overallYoY.overallPct)} · ${n(D.overallYoY.lines)} 筆</span></h3>${delivTbl}
<h3>依料件編號第一碼區分（逐月 ${vs}）</h3>${catHtml}
<h3>各採購人員達交率：依料件編號第一碼區分（以類別為主，採購人員直列）</h3>${buyerCatHtml}
<h3>各採購人員達交率（點開看各採購人員；依料件編號第一碼分別；品項依序用品名關鍵字、子類別名稱、類別名稱辨識歸類，都找不到才退回料件編號第4-6碼對照碼表；採購供應商：鼎華智能系統、FOUR J、SAP Ireland Limited 各自為一條；每個第一碼列出筆數最多的前 ${TOP_BUYER_ITEMS} 個品項）</h3>${buyerHtml}

<h2>3. 毛利率</h2>
<div class="note">毛利率 = (銷售金額(未稅) − 採購未稅金額) ÷ 銷售金額(未稅)。金額一律以千萬為單位（三位小數；≈0 表示不到 0.001 千萬）；逐月與年度表同樣呈現下單量與採購未稅金額${noBase ? '' : `及同期增幅（年度表最後一列只比較兩年度都有資料的 ${mf}~${cutoff} 月；增幅 = (${Y1} − ${Y0}) ÷ ${Y0}，毛利率為百分點差）`}；逐月表依需求不列「占全年」的占比。</div>
<h3>年度比較</h3>${sumTbl}
<h3>每月毛利率 ${vs}</h3>${yoyTbl}
<h3>毛利率低於去年同月的月份：品名拆解</h3>${declineHtml}
<h3>毛利率最低品名與採購端成本趨勢</h3>${linkHtml}

${dqHtml}`;
}

// ===== 頁面互動 =====
function initPage() {
  const DATA = JSON.parse(document.getElementById('data').textContent);
  const recs = buildRecs(DATA);
  const $ = id => document.getElementById(id);
  const opts = (vals, sel, fmt) => vals.map(v => `<option value="${v}"${v === sel ? ' selected' : ''}>${fmt ? fmt(v) : v}</option>`).join('');
  const months = Array.from({ length: 12 }, (_, i) => i + 1);
  const def = DATA.defaults;

  // 季度快速選取 (對應月份區間); 月份改動後若不符合任何季度則顯示「自訂」
  const QUARTERS = { all: [1, 12, '全年'], Q1: [1, 3, 'Q1 (1~3月)'], Q2: [4, 6, 'Q2 (4~6月)'],
    Q3: [7, 9, 'Q3 (7~9月)'], Q4: [10, 12, 'Q4 (10~12月)'] };
  const quarterOf = (a, b) => Object.keys(QUARTERS).find(k => QUARTERS[k][0] === a && QUARTERS[k][1] === b) || 'custom';

  $('controls').innerHTML =
    `<label>基準年 <select id="y0">${opts([NO_BASE, ...DATA.years], def.y0, v => v === NO_BASE ? '無（不比較）' : v)}</select></label>`
    + `<label>比較年 <select id="y1">${opts(DATA.years, def.y1)}</select></label>`
    + `<label>季度 <select id="q">${opts([...Object.keys(QUARTERS), 'custom'], quarterOf(def.mf, def.mt), k => k === 'custom' ? '自訂' : QUARTERS[k][2])}</select></label>`
    + `<label>月份 <select id="mf">${opts(months, def.mf)}</select> ~ <select id="mt">${opts(months, def.mt)}</select></label>`
    + `<label>下單量 <select id="metric">${opts(Object.keys(METRIC_NAMES), def.metric, k => METRIC_NAMES[k])}</select></label>`;

  function update() {
    const o = { Y0: +$('y0').value, Y1: +$('y1').value, mf: +$('mf').value, mt: +$('mt').value, metric: $('metric').value,
      dataEnd: def.dataEnd };
    const out = $('report');
    document.title = o.Y0 === NO_BASE ? `採購分析報告 ${o.Y1}` : `採購分析報告 ${o.Y0}-${o.Y1}`;
    if (o.Y0 === o.Y1) { out.innerHTML = empty('基準年與比較年不能相同（若只想看單一年度，基準年請選「無」）'); return; }
    if (o.mf > o.mt) { out.innerHTML = empty('起始月不能大於結束月'); return; }
    const d = analyze(recs, o);
    out.innerHTML = d.error ? empty(esc(d.error)) : renderReport(d, DATA.generatedAt);
  }
  for (const id of ['y0', 'y1', 'mf', 'mt', 'metric']) $(id).addEventListener('change', update);
  $('q').addEventListener('change', () => {
    const q = QUARTERS[$('q').value];
    if (q) { $('mf').value = String(q[0]); $('mt').value = String(q[1]); }
    update();
  });
  for (const id of ['mf', 'mt']) $(id).addEventListener('change', () => { $('q').value = quarterOf(+$('mf').value, +$('mt').value); });
  update();
}

if (typeof document !== 'undefined' && document.getElementById('data')) initPage();
if (typeof module !== 'undefined') module.exports = { buildRecs, analyze, renderReport };
