"""採購分析 (原 n8n 兩個 Code node 的本機版)

用法:
    python analyze.py                       # 讀 data/採購明細表2025-2026.xlsx, 輸出到 output/
    python analyze.py 其他檔案.xlsx -o 輸出資料夾
輸出:
    output/analysis.json      (原 Node 1 的輸出)
    output/採購分析報告.html   (原 Node 2 的輸出)
"""
import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# ===== 設定 =====
YEARS = [2025, 2026]       # [基準年, 比較年]
MONTH_FROM, MONTH_TO = 1, 12  # 只分析這個月份區間 (兩個年度都套用)
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


# ===== 讀取 (只留「已確認」, 年度/月份在 analyze 篩選) =====
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
        d = parse_date(r.get('採購日期'))
        d = datetime.fromtimestamp(d * 86400, tz=timezone.utc) if d is not None else None
        std, ven = parse_date(r.get('標準交貨日期')), parse_date(r.get('廠商交貨日期'))
        diff = jround(std - ven) if std is not None and ven is not None else None
        sd = datetime.fromtimestamp(std * 86400, tz=timezone.utc) if std is not None else None
        recs.append({
            'year': d.year if d else None, 'month': d.month if d else None,
            'idx': d.year * 12 + d.month - 1 if d else None,
            'sYear': sd.year if sd else None, 'sMonth': sd.month if sd else None,
            'sIdx': sd.year * 12 + sd.month - 1 if sd else None,
            'cat': (str(r.get('料件編號') or '').strip()[:1].upper() or '?'),
            'supplier': txt(r.get('採購供應商'), '(未知)'),
            'buyer': txt(r.get('採購人員'), '(未知)'),
            'name': txt(r.get('品名'), '(未知)'),
            'qty': num(r.get('分批採購數量')),
            'cost': num(r.get('採購未稅金額')),
            'sales': num(r.get('銷售金額(未稅)')),
            'diffDays': diff,
            'onTime': None if diff is None else diff >= 0,
        })
    return recs


# ===== Node 1: 分析 =====
def analyze(all_recs):
    # 下單量/毛利率以採購日期歸屬
    recs = [r for r in all_recs
            if r['year'] in YEARS and MONTH_FROM <= r['month'] <= MONTH_TO]
    for r in recs:
        r['vol'] = r['cost'] if VOLUME_METRIC == 'amount' else r['qty'] if VOLUME_METRIC == 'qty' else 1
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
        ca = sum(r['cost'] for r in recs if r['year'] == Y0 and r['month'] == m)
        cb = sum(r['cost'] for r in recs if r['year'] == Y1 and r['month'] == m)
        monthly_volume.append({'month': m, Y0: r2(a), Y1: r2(b), 'yoyPct': pct((b - a) / a) if a > 0 else None,
                               f'cost{Y0}': r2(ca), f'cost{Y1}': r2(cb),
                               'costYoyPct': pct((cb - ca) / ca) if ca > 0 else None})

    cat_summary = []
    for cat, arr in group_by(recs, lambda r: r['cat']).items():
        base = sum(r['vol'] for r in arr if r['year'] == Y0 and MONTH_FROM <= r['month'] <= cutoff)
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
    # 以標準交貨日期的年月歸屬 (與採購日期年度無關)
    del_recs = [r for r in all_recs if r['onTime'] is not None and r['sYear'] in YEARS
                and MONTH_FROM <= r['sMonth'] <= MONTH_TO]

    def rate(arr):
        return sum(1 for r in arr if r['onTime']) / len(arr) if arr else None

    delivery_by_buyer = []
    for buyer, arr in group_by(del_recs, lambda r: r['buyer']).items():
        months = []
        for m in range(1, 13):
            a = [r for r in arr if r['sYear'] == Y0 and r['sMonth'] == m]
            b = [r for r in arr if r['sYear'] == Y1 and r['sMonth'] == m]
            if not a and not b:
                continue
            ra, rb = rate(a), rate(b)
            months.append({'month': m, f'rate{Y0}Pct': pct(ra), f'n{Y0}': len(a),
                           f'rate{Y1}Pct': pct(rb), f'n{Y1}': len(b),
                           'deltaPP': r2((rb - ra) * 100) if ra is not None and rb is not None else None})
        delivery_by_buyer.append({'buyer': buyer, 'months': months, 'overallPct': pct(rate(arr)), 'lines': len(arr)})

    del_by_idx = group_by(del_recs, lambda r: r['sIdx'])
    delivery_overall = []
    for i in sorted(del_by_idx):
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
        'filter': {'status': '已確認', 'years': YEARS, 'volumeMetric': VOLUME_METRIC, 'sameCutoffMonth': cutoff, 'monthFrom': MONTH_FROM, 'monthTo': MONTH_TO},
        'totalRecords': len(recs),
        'deliveryRecords': len(del_recs),
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


# ===== Node 2: HTML 報告 (資料內嵌, 由 report.js 在瀏覽器端依選項即時重算) =====
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



CSS += """
th.num{text-align:right}
.controls{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px;margin:14px 0;position:sticky;top:0;z-index:5}
.controls select{font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text)}
"""


def read_js():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    return (base / 'report.js').read_text(encoding='utf-8')


def default_years(recs):
    ys = sorted({r['year'] for r in recs if r['year']})
    if len(ys) >= 2:
        return ys[-2:]
    return [ys[0] - 1, ys[0]] if ys else list(YEARS)


def render_html(recs, defaults, generated_at):
    tabs = {k: {} for k in ('cat', 'sup', 'buy', 'name')}

    def ix(kind, v):
        return tabs[kind].setdefault(v, len(tabs[kind]))

    def cn(x):
        return int(x) if float(x).is_integer() else x

    rows = [[r['year'] or 0, r['month'] or 0, r['sYear'] or 0, r['sMonth'] or 0,
             ix('cat', r['cat']), ix('sup', r['supplier']), ix('buy', r['buyer']), ix('name', r['name']),
             cn(r['qty']), cn(r['cost']), cn(r['sales']), -1 if r['onTime'] is None else int(r['onTime'])]
            for r in recs]
    years = sorted({r['year'] for r in recs if r['year']})
    data = {'generatedAt': generated_at, 'years': years, 'defaults': defaults, 'rows': rows,
            **{k: list(v) for k, v in tabs.items()}}
    blob = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>採購分析報告</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>採購分析報告</h1>
<div class="controls" id="controls"></div>
<div id="report"></div>
</div>
<script id="data" type="application/json">{blob}</script>
<script>{read_js()}</script></body></html>"""


def run(input_path, out_dir, years=None, metric=None, months=None, log=print):
    """讀 Excel/CSV → 分析 → 寫出 html + json, 回傳 html 路徑"""
    global YEARS, VOLUME_METRIC, MONTH_FROM, MONTH_TO
    if metric:
        VOLUME_METRIC = metric
    if months:
        MONTH_FROM, MONTH_TO = months
        if not (1 <= MONTH_FROM <= MONTH_TO <= 12):
            raise ValueError('月份區間必須在 1~12, 且起始月不能大於結束月')
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log(f'讀取 {input_path} ...')
    recs = load_records(input_path)
    YEARS = list(years) if years else default_years(recs)  # 預設: 資料中最近兩年
    log(f'已確認 {len(recs)} 筆, 分析中 ...')
    result = analyze(recs)
    if 'error' in result:
        raise ValueError(result['error'])
    (out / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    html_path = out / '採購分析報告.html'
    html_path.write_text(render_html(recs, {'y0': YEARS[0], 'y1': YEARS[1], 'mf': MONTH_FROM, 'mt': MONTH_TO,
                                                   'metric': VOLUME_METRIC}, result['generatedAt']), encoding='utf-8')
    log(f'完成 → {html_path}')
    return html_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input', nargs='?', default=str(Path(__file__).parent / 'data' / '採購明細表2025-2026.xlsx'))
    ap.add_argument('-o', '--out', default=str(Path(__file__).parent / 'output'))
    ap.add_argument('--months', nargs=2, type=int, metavar=('起', '迄'), default=None,
                    help='只分析指定月份區間, 例如 --months 9 10')
    args = ap.parse_args()

    try:
        run(args.input, args.out, months=args.months)
    except ValueError as e:
        raise SystemExit(str(e))


if __name__ == '__main__':
    main()
