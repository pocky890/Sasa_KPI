"""採購分析 (原 n8n 兩個 Code node 的本機版)

用法:
    python analyze.py                       # 讀 data/TEST REPORT.xlsx, 輸出到 output/
    python analyze.py 其他檔案.xlsx -o 輸出資料夾
輸出:
    output/analysis.json      (原 Node 1 的輸出)
    output/採購分析報告.html   (原 Node 2 的輸出)
"""
import argparse
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# ===== 設定 =====
YEARS = [2025, 2026]       # [基準年, 比較年]
VOLUME_METRIC = 'lines'    # 'lines' 行數 | 'amount' 採購未稅金額 | 'qty' 分批採購數量
MOM_THRESHOLD = 0.3        # 月對月波動率異常門檻
TOP_SUPPLIERS = 5
TOP_GROWTH = 3
LOWEST_MARGIN_N = 3


# ===== 工具 =====
def jround(x):  # JS Math.round (四捨五入, 非銀行家捨入)
    return math.floor(x + 0.5)


def num(v):
    try:
        n = float(str(v if v is not None else '').replace(',', ''))
        return n if math.isfinite(n) else 0.0
    except ValueError:
        return 0.0


def pct(v):
    return None if v is None or not math.isfinite(v) else jround(v * 10000) / 100


def r2(v):
    return jround(v * 100) / 100


_DATE_RE = re.compile(r'(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})')


def parse_date(s):
    """回傳以『天』為單位的序號 (UTC), 無法解析回傳 None"""
    m = _DATE_RE.search(str(s if s is not None else ''))
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc).timestamp() / 86400
    except ValueError:
        return None


def ym_label(i):
    return f'{i // 12}-{i % 12 + 1:02d}'


def margin(sales, cost):
    return (sales - cost) / sales if sales > 0 else None


def mean(a):
    return sum(a) / len(a) if a else None


def group_by(arr, key_fn):
    m = defaultdict(list)
    for x in arr:
        m[key_fn(x)].append(x)
    return m


def txt(v, default):
    s = '' if v is None else str(v)
    return s if s else default


# ===== 讀取 + 篩選「已確認」=====
def load_records(path):
    if str(path).lower().endswith('.csv'):
        df = pd.read_csv(path, dtype=str, encoding='utf-8-sig').astype(object)
    else:
        df = pd.read_excel(path, dtype=str).astype(object)
    df = df.where(lambda d: d.notna(), None)  # NaN → None
    recs = []
    for r in df.to_dict('records'):
        if str(r.get('狀態') or '').strip() != '已確認':
            continue
        pd_ = parse_date(r.get('採購日期'))
        if pd_ is None:
            continue
        d = datetime.fromtimestamp(pd_ * 86400, tz=timezone.utc)
        if d.year not in YEARS:
            continue
        std, ven = parse_date(r.get('標準交貨日期')), parse_date(r.get('廠商交貨日期'))
        diff = jround(std - ven) if std is not None and ven is not None else None
        rec = {
            'year': d.year, 'month': d.month, 'idx': d.year * 12 + d.month - 1,
            'cat': (str(r.get('料件編號') or '').strip()[:1].upper() or '?'),
            'supplier': txt(r.get('採購供應商'), '(未知)'),
            'buyer': txt(r.get('採購人員'), '(未知)'),
            'name': txt(r.get('品名'), '(未知)'),
            'qty': num(r.get('分批採購數量')),
            'cost': num(r.get('採購未稅金額')),
            'sales': num(r.get('銷售金額(未稅)')),
            'diffDays': diff,
            'onTime': None if diff is None else diff >= 0,
        }
        rec['vol'] = rec['cost'] if VOLUME_METRIC == 'amount' else rec['qty'] if VOLUME_METRIC == 'qty' else 1
        recs.append(rec)
    return recs


# ===== Node 1: 分析 =====
def analyze(recs):
    if not recs:
        return {'error': '沒有符合條件(狀態=已確認且採購日期在指定年度)的資料'}

    Y0, Y1 = YEARS
    min_idx, max_idx = min(r['idx'] for r in recs), max(r['idx'] for r in recs)
    all_idx = list(range(min_idx, max_idx + 1))
    # 同期比較: 比較年只有到 cutoff 月, 基準年也只取到同一個月
    y1_months = [r['month'] for r in recs if r['year'] == Y1]
    cutoff = max(y1_months) if y1_months else 12
    by_idx_all = group_by(recs, lambda r: r['idx'])

    def month_series(arr, val_fn):
        by_idx = group_by(arr, lambda r: r['idx'])
        return [{'i': i, 'ym': ym_label(i), 'v': sum(val_fn(r) for r in by_idx.get(i, []))} for i in all_idx]

    def mom_list(series):
        out = []
        for k, p in enumerate(series):
            prev = series[k - 1]['v'] if k > 0 else 0
            out.append({'ym': p['ym'], 'value': r2(p['v']),
                        'mom': (p['v'] - prev) / prev if k > 0 and prev > 0 else None})
        return out

    # --- 1. 每月下單量 ---
    monthly_volume = []
    for m in range(1, 13):
        a = sum(r['vol'] for r in recs if r['year'] == Y0 and r['month'] == m)
        b = sum(r['vol'] for r in recs if r['year'] == Y1 and r['month'] == m)
        monthly_volume.append({'month': m, Y0: r2(a), Y1: r2(b), 'yoyPct': pct((b - a) / a) if a > 0 else None})

    cat_summary = []
    for cat, arr in group_by(recs, lambda r: r['cat']).items():
        base = sum(r['vol'] for r in arr if r['year'] == Y0 and r['month'] <= cutoff)
        cur = sum(r['vol'] for r in arr if r['year'] == Y1)
        series = mom_list(month_series(arr, lambda r: r['vol']))
        moms = [s['mom'] for s in series if s['mom'] is not None]
        avg_abs = mean([abs(x) for x in moms]) if moms else None
        cat_summary.append({
            'category': cat,
            'basePeriod': r2(base), 'currentPeriod': r2(cur), 'delta': r2(cur - base),
            'growthPct': pct((cur - base) / base) if base > 0 else None,
            'isNewCategory': base == 0 and cur > 0,
            'avgAbsMomPct': pct(avg_abs),
            'volatilityFlag': avg_abs is not None and avg_abs > MOM_THRESHOLD,
            'spikeMonths': [{'ym': s['ym'], 'value': s['value'], 'momPct': pct(s['mom'])}
                            for s in series if s['mom'] is not None and abs(s['mom']) > MOM_THRESHOLD],
        })

    def growth_key(c):
        if c['growthPct'] is not None:
            return c['growthPct']
        return 1e9 if c['isNewCategory'] else -1e9

    top3 = [{k: v for k, v in c.items() if k != 'spikeMonths'}
            for c in sorted(cat_summary, key=lambda c: (-growth_key(c), -c['delta']))[:TOP_GROWTH]]

    # 供應商佔比位移
    tot0 = sum(r['vol'] for r in recs if r['year'] == Y0)
    tot1 = sum(r['vol'] for r in recs if r['year'] == Y1)
    sup_rows = []
    for s, arr in group_by(recs, lambda r: r['supplier']).items():
        v0 = sum(r['vol'] for r in arr if r['year'] == Y0)
        v1 = sum(r['vol'] for r in arr if r['year'] == Y1)
        s0, s1 = (v0 / tot0 if tot0 > 0 else 0), (v1 / tot1 if tot1 > 0 else 0)
        sup_rows.append({'supplier': s, f'vol{Y0}': r2(v0), f'vol{Y1}': r2(v1),
                         f'share{Y0}Pct': pct(s0), f'share{Y1}Pct': pct(s1),
                         'shiftPP': r2((s1 - s0) * 100), '_t': v0 + v1})
    sup_rows.sort(key=lambda x: -x['_t'])
    top_suppliers = [{k: v for k, v in x.items() if k != '_t'} for x in sup_rows[:TOP_SUPPLIERS]]

    # --- 2. 採購人員達交率 ---
    del_recs = [r for r in recs if r['onTime'] is not None]

    def rate(arr):
        return sum(1 for r in arr if r['onTime']) / len(arr) if arr else None

    delivery_by_buyer = []
    for buyer, arr in group_by(del_recs, lambda r: r['buyer']).items():
        months = []
        for m in range(1, 13):
            a = [r for r in arr if r['year'] == Y0 and r['month'] == m]
            b = [r for r in arr if r['year'] == Y1 and r['month'] == m]
            if not a and not b:
                continue
            ra, rb = rate(a), rate(b)
            months.append({'month': m, f'rate{Y0}Pct': pct(ra), f'n{Y0}': len(a),
                           f'rate{Y1}Pct': pct(rb), f'n{Y1}': len(b),
                           'deltaPP': r2((rb - ra) * 100) if ra is not None and rb is not None else None})
        delivery_by_buyer.append({'buyer': buyer, 'months': months, 'overallPct': pct(rate(arr)), 'lines': len(arr)})

    del_by_idx = group_by(del_recs, lambda r: r['idx'])
    delivery_overall = []
    for i in all_idx:
        arr = del_by_idx.get(i, [])
        if arr:
            delivery_overall.append({'ym': ym_label(i), 'ratePct': pct(rate(arr)), 'lines': len(arr),
                                     'late': sum(1 for r in arr if not r['onTime'])})

    # --- 3. 每月毛利率 ---
    profit_monthly = []
    for i in all_idx:
        arr = by_idx_all.get(i, [])
        if not arr:
            continue
        s, c = sum(r['sales'] for r in arr), sum(r['cost'] for r in arr)
        profit_monthly.append({'i': i, 'ym': ym_label(i), 'year': i // 12, 'month': i % 12 + 1,
                               'sales': r2(s), 'cost': r2(c), 'grossProfit': r2(s - c),
                               'margin': margin(s, c), 'arr': arr})

    def find_pm(y, m):
        return next((p for p in profit_monthly if p['year'] == y and p['month'] == m), None)

    margin_yoy = []
    for m in range(1, 13):
        a, b = find_pm(Y0, m), find_pm(Y1, m)
        if not a and not b:
            continue
        ma, mb = a['margin'] if a else None, b['margin'] if b else None
        margin_yoy.append({'month': m, f'margin{Y0}Pct': pct(ma), f'margin{Y1}Pct': pct(mb),
                           'deltaPP': r2((mb - ma) * 100) if ma is not None and mb is not None else None})

    yearly_summary = []
    for y in YEARS:
        ms = [p for p in profit_monthly if p['year'] == y]
        s, c = sum(p['sales'] for p in ms), sum(p['cost'] for p in ms)
        yearly_summary.append({'year': y, 'months': len(ms),
                               'avgMonthlyMarginPct': pct(mean([p['margin'] for p in ms if p['margin'] is not None])),
                               'aggregateMarginPct': pct(margin(s, c)), 'sales': r2(s), 'cost': r2(c)})

    def agg_name(arr):
        m = {}
        for r in arr:
            o = m.setdefault(r['name'], {'sales': 0.0, 'cost': 0.0})
            o['sales'] += r['sales']
            o['cost'] += r['cost']
        return m

    decline_months = []
    for cur in [p for p in profit_monthly if p['year'] == Y1]:
        prev = find_pm(Y0, cur['month'])
        if not prev or cur['margin'] is None or prev['margin'] is None or cur['margin'] >= prev['margin']:
            continue
        A, B = agg_name(prev['arr']), agg_name(cur['arr'])
        items = []
        for name in dict.fromkeys([*A, *B]):
            a, b = A.get(name, {'sales': 0, 'cost': 0}), B.get(name, {'sales': 0, 'cost': 0})
            d_sales, d_cost = b['sales'] - a['sales'], b['cost'] - a['cost']
            d_gp = (b['sales'] - b['cost']) - (a['sales'] - a['cost'])
            if name not in A:
                driver = '去年同月無下單(今年新增品項)'
            elif name not in B:
                driver = '今年同月無下單'
            elif d_cost > 0 and d_sales < 0:
                driver = '成本增加+銷售額下降'
            elif d_cost > 0 and d_sales >= 0:
                driver = '成本增幅大於銷售增幅' if d_sales > 0 and d_cost > d_sales else '成本增加'
            elif d_sales < 0:
                driver = '銷售降幅大於成本降幅' if d_cost < 0 and -d_sales > -d_cost else '銷售額下降'
            else:
                driver = '其他'
            items.append({'name': name, 'dSales': r2(d_sales), 'dCost': r2(d_cost),
                          'dGrossProfit': r2(d_gp), 'driver': driver})
        items = sorted([x for x in items if x['dGrossProfit'] < 0], key=lambda x: x['dGrossProfit'])[:5]
        decline_months.append({
            'ym': cur['ym'], 'comparedWith': prev['ym'],
            'marginPct': pct(cur['margin']), 'prevYearMarginPct': pct(prev['margin']),
            'dropPP': r2((cur['margin'] - prev['margin']) * 100),
            'totalSalesChange': r2(cur['sales'] - prev['sales']),
            'totalCostChange': r2(cur['cost'] - prev['cost']),
            'topNegativeItems': items,
        })

    # 3.2 最低毛利率品名 → 類別採購成本變動
    name_agg = []
    for name, arr in group_by(recs, lambda r: r['name']).items():
        s, c = sum(r['sales'] for r in arr), sum(r['cost'] for r in arr)
        mg = margin(s, c)
        if mg is not None:
            name_agg.append({'name': name, 'sales': s, 'cost': c, 'margin': mg,
                             'cats': list(dict.fromkeys(r['cat'] for r in arr)), 'arr': arr})
    name_agg = sorted(name_agg, key=lambda x: x['margin'])[:LOWEST_MARGIN_N]

    def trend(arr):
        by_idx = group_by(arr, lambda r: r['idx'])
        ser = []
        for i in all_idx:
            a = by_idx.get(i, [])
            if a:
                q, c = sum(r['qty'] for r in a), sum(r['cost'] for r in a)
                ser.append({'ym': ym_label(i), 'cost': r2(c), 'qty': q,
                            'avgUnitCost': r2(c / q) if q > 0 else None})
        out = []
        for k, p in enumerate(ser):
            prev = ser[k - 1]['avgUnitCost'] if k > 0 else None
            mom = (pct((p['avgUnitCost'] - prev) / prev)
                   if k > 0 and p['avgUnitCost'] is not None and prev else None)
            out.append({**p, 'unitCostMomPct': mom})
        return out

    lowest = [{'name': x['name'], 'marginPct': pct(x['margin']), 'sales': r2(x['sales']), 'cost': r2(x['cost']),
               'categories': x['cats'], 'itemCostTrend': trend(x['arr']),
               'categoryCostTrend': [{'category': c, 'trend': trend([r for r in recs if r['cat'] == c])}
                                     for c in x['cats']]} for x in name_agg]

    return {
        'generatedAt': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
        'filter': {'status': '已確認', 'years': YEARS, 'volumeMetric': VOLUME_METRIC, 'sameCutoffMonth': cutoff},
        'totalRecords': len(recs),
        'dim1_orderVolume': {
            'monthlyVolume': monthly_volume,
            'top3CategoryGrowth': top3,
            'categoryVolatility': cat_summary,
            'volatileCategories': [c['category'] for c in cat_summary if c['volatilityFlag']],
            'topSuppliers': top_suppliers,
        },
        'dim2_deliveryRate': {'overallByMonth': delivery_overall, 'byBuyer': delivery_by_buyer},
        'dim3_grossMargin': {
            'monthly': [{**{k: v for k, v in p.items() if k not in ('arr', 'i', 'margin')}, 'marginPct': pct(p['margin'])}
                        for p in profit_monthly],
            'yoyByMonth': margin_yoy,
            'yearlySummary': yearly_summary,
            'declineMonths': decline_months,
            'lowestMarginLinkage': lowest,
        },
    }


# ===== Node 2: HTML 報告 =====
def esc(s):
    s = '' if s is None else str(s)
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


def n(v):
    if v is None:
        return '—'
    s = f'{float(v):,.2f}'.rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def p(v):
    return '—' if v is None else f'{n(v)}%'


def sgn(v, suffix='', good_up=True):
    if v is None:
        return '<td class="num muted">—</td>'
    good, bad = (v > 0, v < 0) if good_up else (v < 0, v > 0)
    cls = 'pos' if good else 'neg' if bad else ''
    return f'<td class="num {cls}">{"+" if v > 0 else ""}{n(v)}{suffix}</td>'


def bar(v, mx, cls=''):
    w = max(2, min(100, v / mx * 100)) if mx > 0 and v else 0
    return f'<div class="bar {cls}"><i style="width:{round(w, 2)}%"></i></div>'


def table(head, body, cls=''):
    th = ''.join(f'<th>{h}</th>' for h in head)
    return f'<div class="scroll"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def tr(cells):
    return f'<tr>{"".join(cells)}</tr>'


def td(v, cls=''):
    return f'<td class="{cls}">{v}</td>'


def empty(msg):
    return f'<p class="muted">{msg}</p>'


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#6b7385;--line:#e4e7ee;--accent:#2f5bea;--pos:#0f8a4f;--neg:#d23b3b;--b0:#b8c2dc;--b1:#2f5bea}
@media(prefers-color-scheme:dark){:root{--bg:#12151c;--card:#1b2030;--text:#e8ebf3;--muted:#98a1b8;--line:#2b3247;--accent:#7b9bff;--pos:#4fd08c;--neg:#ff7a7a;--b0:#46506e;--b1:#7b9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,"Microsoft JhengHei",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px 56px}
h1{margin:0 0 4px;font-size:24px}h2{margin:40px 0 4px;font-size:19px;border-left:4px solid var(--accent);padding-left:10px}h3{margin:24px 0 8px;font-size:15px}h4{margin:0 0 6px;font-size:15px}
.sub{color:var(--muted)}.muted{color:var(--muted)}.small{font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:18px 0}
.kpi,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card{margin:12px 0}.kpi .l{color:var(--muted);font-size:12px}.kpi .v{font-size:26px;font-weight:700;margin:2px 0}.kpi .s{font-size:12px;color:var(--muted)}
.scroll{overflow-x:auto;margin:6px 0}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}td.wrap{white-space:normal;min-width:160px}
th{font-size:12px;color:var(--muted);font-weight:600;background:color-mix(in srgb,var(--card) 70%,var(--bg))}
td.num{text-align:right;font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:0}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:var(--line);color:var(--text)}.tag.bad{background:var(--neg);color:#fff}.tag.ok{background:color-mix(in srgb,var(--pos) 20%,transparent);color:var(--pos)}
.bar{width:140px;height:8px;border-radius:4px;background:var(--line);overflow:hidden}.bar i{display:block;height:100%;background:var(--b1)}.bar.b0 i{background:var(--b0)}.bar.bneg i{background:var(--neg)}
details{margin:6px 0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 12px}summary{cursor:pointer;padding:4px 0}
.note{font-size:12px;color:var(--muted);margin:4px 0 8px}
"""


def render_html(d):
    Y0, Y1 = d['filter']['years']
    V, D, G = d['dim1_orderVolume'], d['dim2_deliveryRate'], d['dim3_grossMargin']
    metric_name = {'lines': '明細行數', 'amount': '採購未稅金額', 'qty': '分批採購數量'}.get(
        d['filter']['volumeMetric'], d['filter']['volumeMetric'])
    cutoff = d['filter']['sameCutoffMonth']

    # KPI
    vol0 = sum(m[Y0] for m in V['monthlyVolume'] if m['month'] <= cutoff)
    vol1 = sum(m[Y1] for m in V['monthlyVolume'])
    ys = {y['year']: y for y in G['yearlySummary']}
    kpis = [
        {'l': f'下單量 {Y1} vs {Y0} 同期', 'v': n(vol1),
         's': f'{"+" if vol1 >= vol0 else ""}{n((vol1 - vol0) / vol0 * 100)}% ({Y0}: {n(vol0)})' if vol0 > 0 else '',
         'c': 'pos' if vol1 >= vol0 else 'neg'},
        {'l': f'整體毛利率 {Y1}', 'v': p(ys.get(Y1, {}).get('aggregateMarginPct')),
         's': f'{Y0}: {p(ys.get(Y0, {}).get("aggregateMarginPct"))}', 'c': ''},
        {'l': '波動異常類別 (MoM>30%)', 'v': len(V['volatileCategories']),
         's': '、'.join(V['volatileCategories']) or '無', 'c': 'neg' if V['volatileCategories'] else 'pos'},
        {'l': '毛利率低於去年同月', 'v': f'{len(G["declineMonths"])} 個月',
         's': '、'.join(x['ym'] for x in G['declineMonths']) or '無', 'c': 'neg' if G['declineMonths'] else 'pos'},
    ]

    # 1. 下單量
    max_vol = max([1] + [m[y] for m in V['monthlyVolume'] for y in (Y0, Y1)])
    monthly_tbl = table(['月份', Y0, '', Y1, '', '年增率'], [
        tr([td(f'{m["month"]} 月'), td(n(m[Y0]), 'num'), td(bar(m[Y0], max_vol, 'b0')),
            td(n(m[Y1]), 'num'), td(bar(m[Y1], max_vol, 'b1')), sgn(m['yoyPct'], '%')])
        for m in V['monthlyVolume'] if m[Y0] or m[Y1]])

    top3_tbl = table(['排名', '類別', f'{Y0} 同期', f'{Y1}', '增加量', '增幅'], [
        tr([td(i + 1),
            td(f'<b>{esc(c["category"])}</b>' + (' <span class="tag">新類別</span>' if c['isNewCategory'] else '')),
            td(n(c['basePeriod']), 'num'), td(n(c['currentPeriod']), 'num'), sgn(c['delta']),
            '<td class="num muted">—</td>' if c['growthPct'] is None else sgn(c['growthPct'], '%')])
        for i, c in enumerate(V['top3CategoryGrowth'])])

    vol_rows = sorted(V['categoryVolatility'], key=lambda c: -(c['avgAbsMomPct'] if c['avgAbsMomPct'] is not None else -1))
    vol_tbl = table(['類別', '平均 |MoM|', '狀態', '超標月份 (MoM)'], [
        tr([td(f'<b>{esc(c["category"])}</b>'), td(p(c['avgAbsMomPct']), 'num'),
            td('<span class="tag bad">異常</span>' if c['volatilityFlag'] else '<span class="tag ok">正常</span>'),
            td('、'.join(f'{esc(s["ym"])} ({"+" if s["momPct"] > 0 else ""}{n(s["momPct"])}%)' for s in c['spikeMonths'])
               if c['spikeMonths'] else '—', 'small')])
        for c in vol_rows])

    sup_tbl = table(['供應商', f'{Y0} 下單量', f'{Y0} 佔比', f'{Y1} 下單量', f'{Y1} 佔比', '佔比位移'], [
        tr([td(f'<b>{esc(s["supplier"])}</b>'), td(n(s[f'vol{Y0}']), 'num'), td(p(s[f'share{Y0}Pct']), 'num'),
            td(n(s[f'vol{Y1}']), 'num'), td(p(s[f'share{Y1}Pct']), 'num'), sgn(s['shiftPP'], ' pp')])
        for s in V['topSuppliers']])

    # 2. 達交率
    def rate_cls(r):
        return '' if r is None else 'pos' if r >= 90 else 'neg' if r < 70 else ''

    deliv_tbl = table(['月份', '達交率', '', '明細數', '不達交'], [
        tr([td(esc(m['ym'])), td(p(m['ratePct']), f'num {rate_cls(m["ratePct"])}'),
            td(bar(m['ratePct'], 100, 'bneg' if m['ratePct'] < 70 else 'b1')),
            td(n(m['lines']), 'num'), td(n(m['late']), 'num')])
        for m in D['overallByMonth']])
    buyer_html = ''.join(
        f'<details><summary><b>{esc(b["buyer"])}</b> <span class="muted">整體達交率 {p(b["overallPct"])} · {n(b["lines"])} 筆</span></summary>'
        + table(['月份', f'{Y0} 達交率', f'{Y0} 筆數', f'{Y1} 達交率', f'{Y1} 筆數', '變化'], [
            tr([td(f'{m["month"]} 月'), td(p(m[f'rate{Y0}Pct']), f'num {rate_cls(m[f"rate{Y0}Pct"])}'),
                td(n(m[f'n{Y0}']), 'num muted'),
                td(p(m[f'rate{Y1}Pct']), f'num {rate_cls(m[f"rate{Y1}Pct"])}'),
                td(n(m[f'n{Y1}']), 'num muted'), sgn(m['deltaPP'], ' pp')])
            for m in b['months']]) + '</details>'
        for b in sorted(D['byBuyer'], key=lambda b: -b['lines']))

    # 3. 毛利率
    yoy_tbl = table(['月份', f'{Y0} 毛利率', f'{Y1} 毛利率', '變化'], [
        tr([td(f'{m["month"]} 月'), td(p(m[f'margin{Y0}Pct']), 'num'), td(p(m[f'margin{Y1}Pct']), 'num'),
            sgn(m['deltaPP'], ' pp')]) for m in G['yoyByMonth']])
    sum_tbl = table(['年度', '月數', '月平均毛利率', '整體毛利率', '銷售額(未稅)', '成本'], [
        tr([td(f'<b>{y["year"]}</b>'), td(y['months'], 'num'), td(p(y['avgMonthlyMarginPct']), 'num'),
            td(p(y['aggregateMarginPct']), 'num'), td(n(y['sales']), 'num'), td(n(y['cost']), 'num')])
        for y in G['yearlySummary']])

    def decline_card(m):
        items = (table(['品名', '銷售額變化', '成本變化', '毛利變化', '主因'], [
            tr([td(esc(i['name']), 'wrap'), sgn(i['dSales']), sgn(i['dCost'], '', False), sgn(i['dGrossProfit']),
                td(f'<span class="tag">{esc(i["driver"])}</span>')]) for i in m['topNegativeItems']])
                 if m['topNegativeItems'] else empty('沒有毛利為負貢獻的品名'))
        return (f'<div class="card"><h4>{esc(m["ym"])} <span class="muted">vs {esc(m["comparedWith"])}</span></h4>'
                f'<p>毛利率 <b>{p(m["marginPct"])}</b>（去年同月 {p(m["prevYearMarginPct"])}，'
                f'<span class="neg">{n(m["dropPP"])} pp</span>）· 銷售額變化 {n(m["totalSalesChange"])} · '
                f'成本變化 {n(m["totalCostChange"])}</p>{items}</div>')

    decline_html = ''.join(decline_card(m) for m in G['declineMonths']) or empty('沒有毛利率低於去年同月的月份')

    def trend_tbl(rows):
        return table(['月份', '成本', '數量', '平均單位成本', 'MoM'], [
            tr([td(esc(t['ym'])), td(n(t['cost']), 'num'), td(n(t['qty']), 'num'), td(n(t['avgUnitCost']), 'num'),
                sgn(t['unitCostMomPct'], '%', False)]) for t in rows])

    link_html = ''.join(
        f'<div class="card"><h4>#{i + 1} {esc(x["name"])}</h4>'
        f'<p>毛利率 <b class="{"neg" if x["marginPct"] < 0 else ""}">{p(x["marginPct"])}</b> · 銷售額 {n(x["sales"])} · '
        f'成本 {n(x["cost"])} · 類別 {"、".join(esc(c) for c in x["categories"])}</p>'
        f'<details><summary>此品名每月採購成本</summary>{trend_tbl(x["itemCostTrend"])}</details>'
        + ''.join(f'<details><summary>類別 {esc(c["category"])} 採購成本趨勢</summary>{trend_tbl(c["trend"])}</details>'
                  for c in x['categoryCostTrend'])
        + '</div>'
        for i, x in enumerate(G['lowestMarginLinkage']))

    kpi_html = ''.join(
        f'<div class="kpi"><div class="l">{esc(k["l"])}</div><div class="v {k["c"]}">{esc(k["v"])}</div>'
        f'<div class="s">{esc(k["s"])}</div></div>' for k in kpis)

    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>採購分析報告 {Y0}-{Y1}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>採購分析報告</h1>
<div class="sub">{Y0} vs {Y1} · 只計「{esc(d['filter']['status'])}」· {n(d['totalRecords'])} 筆明細 · 下單量以{esc(metric_name)}計算 · 產生時間 {esc(d['generatedAt'])}</div>
<div class="kpis">{kpi_html}</div>

<h2>1. 每月下單量</h2>
<div class="note">增幅採同期比較：{Y0} 年 1~{cutoff} 月 vs {Y1} 年 1~{cutoff} 月。</div>
<h3>逐月趨勢</h3>{monthly_tbl}
<h3>增幅最高前三名類別（料件編號第一碼）</h3>{top3_tbl}
<h3>單量穩定度（月對月波動，平均 |MoM| &gt; 30% 為異常）</h3>{vol_tbl}
<h3>前五大供應商佔比位移（pp = 百分點）</h3>{sup_tbl}

<h2>2. 採購人員達交率</h2>
<div class="note">標準交貨日 − 廠商交貨日 ≥ 0 為達交，&lt; 0 為不達交。紅字 &lt;70%，綠字 ≥90%。</div>
<h3>全體逐月</h3>{deliv_tbl}
<h3>各採購人員（點開看逐月 {Y0} vs {Y1}）</h3>{buyer_html}

<h2>3. 毛利率</h2>
<div class="note">毛利率 = (銷售金額(未稅) − 採購未稅金額) ÷ 銷售金額(未稅)。</div>
<h3>年度比較</h3>{sum_tbl}
<h3>每月毛利率 {Y0} vs {Y1}</h3>{yoy_tbl}
<h3>毛利率低於去年同月的月份：品名拆解</h3>{decline_html}
<h3>毛利率最低品名與採購端成本趨勢</h3>{link_html}
</div></body></html>"""


def run(input_path, out_dir, years=None, metric=None, log=print):
    """讀 Excel/CSV → 分析 → 寫出 html + json, 回傳 html 路徑"""
    global YEARS, VOLUME_METRIC
    if years:
        YEARS = list(years)
    if metric:
        VOLUME_METRIC = metric
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log(f'讀取 {input_path} ...')
    recs = load_records(input_path)
    log(f'符合條件 {len(recs)} 筆, 分析中 ...')
    result = analyze(recs)
    if 'error' in result:
        raise ValueError(result['error'])
    (out / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    html_path = out / '採購分析報告.html'
    html_path.write_text(render_html(result), encoding='utf-8')
    log(f'完成 → {html_path}')
    return html_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input', nargs='?', default=str(Path(__file__).parent / 'data' / 'TEST REPORT.xlsx'))
    ap.add_argument('-o', '--out', default=str(Path(__file__).parent / 'output'))
    args = ap.parse_args()

    try:
        run(args.input, args.out)
    except ValueError as e:
        raise SystemExit(str(e))


if __name__ == '__main__':
    main()
