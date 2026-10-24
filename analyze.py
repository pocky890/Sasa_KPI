"""採購分析 (原 n8n 兩個 Code node 的本機版)

用法:
    python analyze.py                       # 讀 data/採購明細表2025-2026.xlsx, 輸出到 output/
    python analyze.py 其他檔案.xlsx -o 輸出資料夾
輸出:
    output/analysis.json      (原 Node 1 的輸出)
    output/採購分析報告.html   (原 Node 2 的輸出)
"""
import argparse
import calendar
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import codebook

# ===== 設定 =====
YEARS = [2025, 2026]       # [基準年, 比較年]
MONTH_FROM, MONTH_TO = 1, 12  # 只分析這個月份區間 (兩個年度都套用)
DATA_END = None            # 資料中最晚的採購日期 (YYYY-MM-DD), 由 run() 設定; 用來判斷最新月份是否滿月
VOLUME_METRIC = 'lines'    # 'lines' 行數 | 'amount' 採購未稅金額 | 'qty' 分批採購數量
MOM_THRESHOLD = 0.3        # 月對月波動率異常門檻
TOP_GROWTH = 5             # 下單量增加量最高的前 N 名產品線
TOP_BRAND_GROWTH = 5       # 廠牌 YoY/QoQ/MoM 每個第一碼列出前 N 個廠牌 (依當期金額), 其餘合併為「其他已識別廠牌」
TOP_DELIVERY_BRANDS = 5    # 廠牌達交率每個第一碼列出前 N 個廠牌 (依筆數)
TOP_BUYER_ITEMS = 5        # 各採購人員達交率: 每位採購人員、每個第一碼列出前 N 個品項 (依筆數)
# 採購人員調整 (下單量與達交率的資料): BUYER_RENAMES 為「原採購人員 → 改算給誰」(併入後一起計算),
# BUYERS_EXCLUDED 為整個排除的採購人員 (不計入下單量與達交率相關分析; 毛利率分析仍使用全部明細)
BUYER_RENAMES = {'劉孟丞': '林俐廷', '張庭嘉': '郭庭毓', '吳婉瑜': '蔡沂錞'}
BUYERS_EXCLUDED = {'tiptop', '李佳玟', '簡瑋萱'}
# 採購未到貨代號為這些原因者標記為「資料例外」, 不計入達交率分母 (代號格式「原因-(責任方)」)
DELIVERY_EXCEPTIONS = {'資料不齊': '業務', '訂單暫停': '業務', '客戶因素': '客戶', '其他原因': '其他', '倉庫未收': '倉庫'}
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


def adjust_buyer(raw):
    """回傳 (調整後的採購人員, 是否排除): 先套用 BUYER_RENAMES 再判斷 BUYERS_EXCLUDED (不分大小寫)"""
    name = txt(raw, '(未知)').strip()
    name = BUYER_RENAMES.get(name, name)
    return name, name.lower() in {b.lower() for b in BUYERS_EXCLUDED}


def delivery_exception(code):
    """採購未到貨代號屬於「資料例外」原因時回傳標準寫法 (例「資料不齊-(業務)」), 否則 None"""
    m = re.match(r'\s*([^\-－(（\s]+)', code or '')
    name = m.group(1) if m else ''
    return f'{name}-({DELIVERY_EXCEPTIONS[name]})' if name in DELIVERY_EXCEPTIONS else None


# ===== 讀取 (只留「已確認」, 年度/月份在 analyze 篩選) =====
def load_records(path):
    if str(path).lower().endswith('.csv'):
        df = pd.read_csv(path, dtype=str, encoding='utf-8-sig').astype(object)
    else:
        df = pd.read_excel(path, dtype=str).astype(object)
    df = df.where(lambda d: d.notna(), None)  # NaN → None
    cls = codebook.Classifier()
    recs = []
    for r in df.to_dict('records'):
        if str(r.get('狀態') or '').strip() != '已確認':
            continue
        pn, pname = str(r.get('料件編號') or '').strip(), txt(r.get('品名'), '(未知)')
        supplier = txt(r.get('採購供應商'), '(未知)')
        cat_name, sub_name = txt(r.get('類別名稱'), ''), txt(r.get('子類別名稱'), '')
        # 指定供應商 (SUPPLIER_LINES) 依供應商各歸成一個品項 / 產品線;
        # 第一碼 T、採購供應商屬 HW_MAINT_SUPPLIERS 且子類別名稱空白者歸為「硬體維護」;
        # 其餘依 品名 → 子類別名稱 → 類別名稱 辨識
        if codebook.supplier_line(supplier):
            item = codebook.supplier_line(supplier)
        elif pn[:1].upper() == 'T' and not sub_name and codebook.is_hw_maint_supplier(supplier):
            item = codebook.HW_MAINT_LABEL
        else:
            item = cls.item(pn, pname, cat_name, sub_name)
        buyer, drop_buyer = adjust_buyer(r.get('採購人員'))
        brand = cls.brand(pn, pname, cat_name, sub_name)
        if brand == codebook.UNKNOWN_BRAND:  # 廠牌辨識不出時, 指定供應商 (SUPPLIER_BRANDS) 直接以其廠牌名稱取代空值
            brand = codebook.supplier_brand(supplier) or brand
        d = parse_date(r.get('採購日期'))
        d = datetime.fromtimestamp(d * 86400, tz=timezone.utc) if d is not None else None
        std, ven = parse_date(r.get('標準交貨日期')), parse_date(r.get('廠商交貨日期'))
        diff = jround(std - ven) if std is not None and ven is not None else None
        sd = datetime.fromtimestamp(std * 86400, tz=timezone.utc) if std is not None else None
        recs.append({
            'year': d.year if d else None, 'month': d.month if d else None,
            'idx': d.year * 12 + d.month - 1 if d else None,
            'ymd': d.strftime('%Y-%m-%d') if d else None,
            'sYear': sd.year if sd else None, 'sMonth': sd.month if sd else None,
            'sIdx': sd.year * 12 + sd.month - 1 if sd else None,
            'item': item,
            'cat': (pn[:1].upper() or '?'),
            'line': item,  # 產品線與品項用同一套歸類 (含指定供應商規則)
            'brand': brand,
            'supplier': supplier,
            'buyer': buyer,
            'dropBuyer': drop_buyer,
            'name': pname,
            'qty': num(r.get('分批採購數量')),
            'cost': num(r.get('採購未稅金額')),
            'sales': num(r.get('銷售金額(未稅)')),
            'diffDays': diff,
            'onTime': None if diff is None else diff >= 0,
            'exc': delivery_exception(txt(r.get('採購未到貨代號'), '')),
        })
    return recs


# ===== Node 1: 分析 =====
def analyze(all_recs_full):
    # 下單量/毛利率以採購日期歸屬
    # recs_m: 全部明細 (毛利率分析用); all_recs / recs: 排除 BUYERS_EXCLUDED 後 (下單量與達交率分析用)
    all_recs = [r for r in all_recs_full if not r['dropBuyer']]
    recs_m = [r for r in all_recs_full
              if r['year'] in YEARS and MONTH_FROM <= r['month'] <= MONTH_TO]
    for r in recs_m:
        r['vol'] = r['cost'] if VOLUME_METRIC == 'amount' else r['qty'] if VOLUME_METRIC == 'qty' else 1
    recs = [r for r in recs_m if not r['dropBuyer']]
    if not recs:
        return {'error': '沒有符合條件(狀態=已確認且採購日期在指定年度)的資料'}

    Y0, Y1 = YEARS
    min_idx, max_idx = min(r['idx'] for r in recs_m), max(r['idx'] for r in recs_m)
    all_idx = list(range(min_idx, max_idx + 1))
    # 同期比較: 比較年只有到 cutoff 月, 基準年也只取到同一個月
    y1_months = [r['month'] for r in recs if r['year'] == Y1]
    cutoff = max(y1_months) if y1_months else 12
    by_idx_all = group_by(recs_m, lambda r: r['idx'])

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
    def monthly_of(arr):
        rows = []
        for m in range(1, 13):
            a = sum(r['vol'] for r in arr if r['year'] == Y0 and r['month'] == m)
            b = sum(r['vol'] for r in arr if r['year'] == Y1 and r['month'] == m)
            ca = sum(r['cost'] for r in arr if r['year'] == Y0 and r['month'] == m)
            cb = sum(r['cost'] for r in arr if r['year'] == Y1 and r['month'] == m)
            rows.append({'month': m, Y0: r2(a), Y1: r2(b), 'yoyPct': pct((b - a) / a) if a > 0 else None,
                         f'cost{Y0}': r2(ca), f'cost{Y1}': r2(cb),
                         'costYoyPct': pct((cb - ca) / ca) if ca > 0 else None})
        return rows

    monthly_volume = monthly_of(recs)
    # 依料件編號第一碼區分的逐月下單量 / 採購未稅金額 (同樣的欄位, 每個第一碼一組)
    monthly_by_cat = [{'category': cat, 'months': monthly_of(arr)}
                      for cat, arr in group_by(recs, lambda r: r['cat']).items()]

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

    # 每個分析項目都呈現 下單量 與 採購未稅金額 (基準年同期 / 比較年), 占比與增幅由這四個數值算出
    def measures(arr):
        in_base = [r for r in arr if r['year'] == Y0 and MONTH_FROM <= r['month'] <= cutoff]
        cur_arr = [r for r in arr if r['year'] == Y1]
        return {'volBase': r2(sum(r['vol'] for r in in_base)), 'volCur': r2(sum(r['vol'] for r in cur_arr)),
                'costBase': r2(sum(r['cost'] for r in in_base)), 'costCur': r2(sum(r['cost'] for r in cur_arr))}

    # 產品線下單量增加量排名 (同期比較同上), 依料件編號第一碼分別排名; 每個產品線併列其中占比最高的廠牌
    def top_brand(arr, base_total, cur_total, field='vol'):
        """群組內「已識別」廠牌中, 比較年數值 (field: 下單量 vol / 採購未稅金額 cost) 最高者及其占比
        (占比 = 廠牌數值 ÷ 群組數值); 全部未識別則 None"""
        best = None
        for b, a in group_by(arr, lambda r: r['brand']).items():
            if codebook.is_unclassified(b):
                continue
            v0 = sum(r[field] for r in a if r['year'] == Y0 and MONTH_FROM <= r['month'] <= cutoff)
            v1 = sum(r[field] for r in a if r['year'] == Y1)
            if v1 > 0 and (best is None or (v1, v0) > (best[2], best[1])):  # 比較年沒有該廠牌的數值就不列
                best = [b, v0, v1]
        if best is None:
            return None
        s0 = best[1] / base_total if base_total > 0 else None
        s1 = best[2] / cur_total if cur_total > 0 else None
        return {'name': best[0], 'basePeriod': r2(best[1]), 'currentPeriod': r2(best[2]),
                'baseSharePct': pct(s0), 'currentSharePct': pct(s1)}

    def top_growth():
        groups = []
        for cat, cat_arr in group_by(recs, lambda r: r['cat']).items():
            rows = []
            for key, arr in group_by(cat_arr, lambda r: r['line']).items():
                base = sum(r['vol'] for r in arr if r['year'] == Y0 and MONTH_FROM <= r['month'] <= cutoff)
                cur = sum(r['vol'] for r in arr if r['year'] == Y1)
                rows.append({'name': key, 'basePeriod': r2(base), 'currentPeriod': r2(cur), 'delta': r2(cur - base),
                             'growthPct': pct((cur - base) / base) if base > 0 else None,
                             'isNew': base == 0 and cur > 0, **measures(arr),
                             'topBrand': None if key in codebook.NO_BRAND_LINES else top_brand(arr, base, cur)})
            tot1 = sum(x['currentPeriod'] for x in rows)
            skip = [x for x in rows if codebook.is_unclassified(x['name'])]
            keep = sorted((x for x in rows if x not in skip), key=lambda x: (-x['delta'], -(x['growthPct'] or 0)))
            groups.append({'category': cat, 'top': keep[:TOP_GROWTH], 'totals': measures(cat_arr), '_t': tot1,
                           'excludedSharePct': pct(sum(x['currentPeriod'] for x in skip) / tot1) if tot1 > 0 else None})
        groups.sort(key=lambda g: -g['_t'])
        return [{k: v for k, v in g.items() if k != '_t'} for g in groups]

    top_lines = top_growth()

    # 前5大交易品項: 依料件編號第一碼分別; 品項依 品名關鍵字 → 子類別名稱 → 類別名稱 辨識歸類 (見 codebook.item);
    # 以採購未稅金額加總 (與下單量定義無關), 依比較年金額排序, 基準年取同期
    def top_items():
        groups = []
        for cat, cat_arr in group_by(recs, lambda r: r['cat']).items():
            rows = []
            for key, arr in group_by(cat_arr, lambda r: r['item']).items():
                base = sum(r['cost'] for r in arr if r['year'] == Y0 and MONTH_FROM <= r['month'] <= cutoff)
                cur = sum(r['cost'] for r in arr if r['year'] == Y1)
                rows.append({'name': key,
                             'basePeriod': r2(base), 'currentPeriod': r2(cur), 'delta': r2(cur - base),
                             'growthPct': pct((cur - base) / base) if base > 0 else None,
                             'isNew': base == 0 and cur > 0, **measures(arr),
                             'topBrand': None if key in codebook.NO_BRAND_LINES else top_brand(arr, base, cur, 'cost')})
            tot1 = sum(x['currentPeriod'] for x in rows)
            skip = [x for x in rows if codebook.is_unclassified(x['name'])]
            keep = sorted((x for x in rows if x not in skip), key=lambda x: (-x['currentPeriod'], x['name']))
            top = keep[:TOP_GROWTH]
            groups.append({'category': cat, 'top': top, 'totals': measures(cat_arr), '_t': tot1,
                           'topSharePct': pct(sum(x['currentPeriod'] for x in top) / tot1) if tot1 > 0 else None,
                           'excludedSharePct': pct(sum(x['currentPeriod'] for x in skip) / tot1) if tot1 > 0 else None})
        groups.sort(key=lambda g: -g['_t'])
        return [{k: v for k, v in g.items() if k != '_t'} for g in groups]

    top_amount_items = top_items()

    # 廠牌成長率 YoY / QoQ / MoM: 依料件編號第一碼分別; 廠牌依 品名 → 子類別名稱 → 類別名稱 → 第7-9碼 辨識 (見 codebook.brand);
    # 以採購未稅金額 (依採購日期歸屬) 加總。以所選區間內比較年「最後一個完整月份」為基準:
    #   YoY: 比較年 起始月~基準月  vs 去年同期 (基準年同月份)
    #   QoQ: 最後一個完整季度 vs 上一季
    #   MoM: 基準月 vs 上個月
    # QoQ / MoM 會用到區間以外的月份, 所以用全部資料 (all_recs) 計算, 不受月份篩選限制
    # 基準取「最後一個完整月份」: 資料最新日期 (DATA_END) 還沒到該月月底就退一個月, 避免未滿月造成增率失真
    partial_idx = None
    if DATA_END:
        ey, em, ed = (int(x) for x in DATA_END.split('-'))
        if ed < calendar.monthrange(ey, em)[1]:
            partial_idx = ey * 12 + em - 1
    bg_a0 = Y1 * 12 + cutoff - 1
    bg_a = bg_a0 - 1 if bg_a0 == partial_idx else bg_a0
    bg_am = bg_a % 12 + 1
    bg_qend = bg_a // 12 * 12 + bg_am // 3 * 3 - 1  # 最後一個完整季度的最後一個月
    bg_start, bg_shift = Y1 * 12 + MONTH_FROM - 1, (Y1 - Y0) * 12
    bg_win = {'qoq': ((bg_qend - 2, bg_qend), (bg_qend - 5, bg_qend - 3)),
              'mom': ((bg_a, bg_a), (bg_a - 1, bg_a - 1))}
    if bg_a >= bg_start:
        bg_win = {'yoy': ((bg_start, bg_a), (bg_start - bg_shift, bg_a - bg_shift)), **bg_win}

    def q_label(i):
        return f'{i // 12} Q{i % 12 // 3 + 1}'

    bg_labels = {
        'yoy': f'{Y1} 年 {MONTH_FROM}~{bg_am} 月 vs {Y0} 年同月份' if 'yoy' in bg_win else None,
        'qoq': f'{q_label(bg_qend)} vs {q_label(bg_qend - 3)}',
        'mom': f'{ym_label(bg_a)} vs {ym_label(bg_a - 1)}',
        'sharePeriod': f'{ym_label(bg_start)} ~ {ym_label(bg_a)}' if bg_a >= bg_start else None,
        'anchorNote': (f'{ym_label(bg_a0)} 資料尚未滿月（資料最新至 {DATA_END}），以最近完整月份 {ym_label(bg_a)} 為基準'
                       if bg_a != bg_a0 else '')}

    def vol_of(r):  # 下單量 (依下單量定義)
        return r['cost'] if VOLUME_METRIC == 'amount' else r['qty'] if VOLUME_METRIC == 'qty' else 1

    def bg_sum(arr, w, f=lambda r: r['cost']):
        return r2(sum(f(r) for r in arr if r['idx'] is not None and w[0] <= r['idx'] <= w[1]))

    def bg_row(name, arr):
        row = {'name': name, 'rank': bg_sum(arr, (Y1 * 12 + MONTH_FROM - 1, bg_a)),
               'volRank': bg_sum(arr, (Y1 * 12 + MONTH_FROM - 1, bg_a), vol_of)}
        for k in ('yoy', 'qoq', 'mom'):
            if k in bg_win:
                c, p0 = bg_sum(arr, bg_win[k][0]), bg_sum(arr, bg_win[k][1])
                row.update({f'{k}Cur': c, f'{k}Prev': p0, f'{k}Pct': pct((c - p0) / p0) if p0 > 0 else None})
            else:
                row.update({f'{k}Cur': None, f'{k}Prev': None, f'{k}Pct': None})
        # 下單量的 YoY (當期 vs 去年同期), 與金額的 YoY 並列
        if 'yoy' in bg_win:
            vc, vp = bg_sum(arr, bg_win['yoy'][0], vol_of), bg_sum(arr, bg_win['yoy'][1], vol_of)
            row.update({'volYoyPrev': vp, 'volYoyPct': pct((vc - vp) / vp) if vp > 0 else None})
        else:
            row.update({'volYoyPrev': None, 'volYoyPct': None})
        return row

    brand_growth = []
    for cat in {r['cat'] for r in recs}:
        arr = [r for r in all_recs if r['cat'] == cat and r['idx'] is not None]
        by = group_by(arr, lambda r: r['brand'])
        rows = [bg_row(k, a) for k, a in by.items()]
        known = sorted((x for x in rows if not codebook.is_unclassified(x['name'])), key=lambda x: (-x['rank'], x['name']))
        # 只列已識別廠牌中當期金額最大的前 N 個 (不再列「其他」「未識別」彙總列); 「全部」為該第一碼的總計
        total, top = bg_row('全部', arr), known[:TOP_BRAND_GROWTH]
        # 當期占比 = 廠牌當期金額 ÷ 該第一碼同期間金額合計 (含未識別廠牌)
        for x in [total, *top]:
            x['sharePct'] = pct(x['rank'] / total['rank']) if total['rank'] > 0 else None
            x['volSharePct'] = pct(x['volRank'] / total['volRank']) if total['volRank'] > 0 else None  # 下單量占比
        brand_growth.append({'category': cat, 'total': total, 'rows': top})
    brand_growth.sort(key=lambda g: -g['total']['rank'])

    # --- 2. 採購人員達交率 ---
    # 以標準交貨日期的年月歸屬 (與採購日期年度無關)
    # 標準交貨日 − 廠商交貨日 < 0 為不達交; 「資料例外」(採購未到貨代號) 另列, 不計入分母
    in_scope = [r for r in all_recs if r['onTime'] is not None and r['sYear'] in YEARS
                and MONTH_FROM <= r['sMonth'] <= MONTH_TO]
    exc_recs = [r for r in in_scope if r['exc']]
    del_recs = [r for r in in_scope if not r['exc']]
    delivery_exceptions = {
        'total': len(exc_recs),
        'byReason': [{'reason': k, 'lines': sum(1 for r in exc_recs if r['exc'] == k),
                      'late': sum(1 for r in exc_recs if r['exc'] == k and not r['onTime'])}
                     for k in (f'{n}-({d})' for n, d in DELIVERY_EXCEPTIONS.items())],
    }

    def rate(arr):
        return sum(1 for r in arr if r['onTime']) / len(arr) if arr else None

    def split_cost(a, b):
        """達交 / 未達交 的採購未稅金額 (達交率分母明細, 已排除資料例外) 與各自的同期年增率"""
        def f(arr, ok):
            return r2(sum(r['cost'] for r in arr if bool(r['onTime']) == ok))
        on0, on1, lt0, lt1 = f(a, True), f(b, True), f(a, False), f(b, False)
        return {f'costOn{Y0}': on0, f'costOn{Y1}': on1, 'costOnYoyPct': pct((on1 - on0) / on0) if on0 > 0 else None,
                f'costLate{Y0}': lt0, f'costLate{Y1}': lt1, 'costLateYoyPct': pct((lt1 - lt0) / lt0) if lt0 > 0 else None}

    # 逐月、基準年 vs 比較年 (與每月下單量同樣的呈現方式)
    def delivery_months(arr):
        months = []
        for m in range(1, 13):
            a = [r for r in arr if r['sYear'] == Y0 and r['sMonth'] == m]
            b = [r for r in arr if r['sYear'] == Y1 and r['sMonth'] == m]
            if not a and not b:
                continue
            ra, rb = rate(a), rate(b)
            # 達交率分母明細 (已排除資料例外) 的 下單量 / 採購未稅金額 與年增率
            va, vb = sum(vol_of(r) for r in a), sum(vol_of(r) for r in b)
            ca, cb = sum(r['cost'] for r in a), sum(r['cost'] for r in b)
            months.append({'month': m, f'rate{Y0}Pct': pct(ra), f'n{Y0}': len(a),
                           f'rate{Y1}Pct': pct(rb), f'n{Y1}': len(b),
                           'deltaPP': r2((rb - ra) * 100) if ra is not None and rb is not None else None,
                           f'vol{Y0}': r2(va), f'vol{Y1}': r2(vb), 'volYoyPct': pct((vb - va) / va) if va > 0 else None,
                           f'cost{Y0}': r2(ca), f'cost{Y1}': r2(cb), 'costYoyPct': pct((cb - ca) / ca) if ca > 0 else None,
                           **split_cost(a, b)})
        return months

    def del_totals(arr):  # 各組合計 (用於占比 = 該組 ÷ 全體)
        a, b = [r for r in arr if r['sYear'] == Y0], [r for r in arr if r['sYear'] == Y1]
        return {'vol0': r2(sum(vol_of(r) for r in a)), 'vol1': r2(sum(vol_of(r) for r in b)),
                'cost0': r2(sum(r['cost'] for r in a)), 'cost1': r2(sum(r['cost'] for r in b))}

    def delivery_by(field, label):
        return [{label: key, 'months': delivery_months(arr), 'overallPct': pct(rate(arr)), 'lines': len(arr),
                 'totals': del_totals(arr)}
                for key, arr in group_by(del_recs, lambda r: r[field]).items()]

    delivery_by_buyer = delivery_by('buyer', 'buyer')
    delivery_by_category = delivery_by('cat', 'category')  # 料件編號第一碼
    delivery_overall_yoy = {'months': delivery_months(del_recs), 'overallPct': pct(rate(del_recs)),
                            'lines': len(del_recs), 'totals': del_totals(del_recs)}

    # 廠牌達交率: 依料件編號第一碼分別; 廠牌依 品名 → 子類別名稱 → 類別名稱 → 第7-9碼 辨識 (見 codebook.brand);
    # 基準年 / 比較年以標準交貨日期歸屬, 資料例外筆數另列
    def brand_row(name, arr, exc_arr):
        a = [r for r in arr if r['sYear'] == Y0]
        b = [r for r in arr if r['sYear'] == Y1]
        ra, rb = rate(a), rate(b)
        va, vb = sum(vol_of(r) for r in a), sum(vol_of(r) for r in b)
        ca, cb = sum(r['cost'] for r in a), sum(r['cost'] for r in b)
        return {'name': name, f'n{Y0}': len(a), f'late{Y0}': sum(1 for r in a if not r['onTime']), f'rate{Y0}Pct': pct(ra),
                f'n{Y1}': len(b), f'late{Y1}': sum(1 for r in b if not r['onTime']), f'rate{Y1}Pct': pct(rb),
                'deltaPP': r2((rb - ra) * 100) if ra is not None and rb is not None else None,
                'exceptions': len(exc_arr),
                f'vol{Y0}': r2(va), f'vol{Y1}': r2(vb), 'volYoyPct': pct((vb - va) / va) if va > 0 else None,
                f'cost{Y0}': r2(ca), f'cost{Y1}': r2(cb), 'costYoyPct': pct((cb - ca) / ca) if ca > 0 else None,
                **split_cost(a, b)}

    delivery_by_brand = []
    for cat in sorted({r['cat'] for r in in_scope}):
        cat_del = [r for r in del_recs if r['cat'] == cat]
        cat_exc = [r for r in exc_recs if r['cat'] == cat]
        exc_by_brand = group_by(cat_exc, lambda r: r['brand'])
        by_brand = group_by(cat_del, lambda r: r['brand'])
        known = sorted((k for k in set(by_brand) | set(exc_by_brand) if not codebook.is_unclassified(k)),
                       key=lambda k: (-len(by_brand.get(k, [])), k))
        # 只列已識別廠牌中筆數最多的前 N 個 (不再列「其他」「未識別」彙總列); 「全部」為該第一碼的總計
        rows = [brand_row(k, by_brand.get(k, []), exc_by_brand.get(k, [])) for k in known[:TOP_DELIVERY_BRANDS]]
        delivery_by_brand.append({'category': cat, 'total': brand_row('全部', cat_del, cat_exc), 'rows': rows})
    delivery_by_brand.sort(key=lambda g: -(g['total'][f'n{Y0}'] + g['total'][f'n{Y1}']))

    # 各採購人員達交率: 每位採購人員依料件編號第一碼分別, 每個第一碼列出筆數最多的前 N 個品項
    # (品項依 品名關鍵字 → 子類別名稱 → 類別名稱 → 碼表第4-6碼 歸類, 見 codebook.item; 不分類 / 未歸類不列)
    delivery_by_buyer_item = []
    for buyer, arr in group_by(del_recs, lambda r: r['buyer']).items():
        exc_b = [r for r in exc_recs if r['buyer'] == buyer]
        cats = []
        for cat in sorted({r['cat'] for r in arr}):
            cat_del = [r for r in arr if r['cat'] == cat]
            cat_exc = [r for r in exc_b if r['cat'] == cat]
            by_item, exc_by_item = group_by(cat_del, lambda r: r['item']), group_by(cat_exc, lambda r: r['item'])
            known = sorted((k for k in set(by_item) | set(exc_by_item) if not codebook.is_unclassified(k)),
                           key=lambda k: (-len(by_item.get(k, [])), k))
            cats.append({'category': cat, 'total': brand_row('全部', cat_del, cat_exc),
                         'rows': [brand_row(k, by_item.get(k, []), exc_by_item.get(k, []))
                                  for k in known[:TOP_BUYER_ITEMS]]})
        cats.sort(key=lambda g: -(g['total'][f'n{Y0}'] + g['total'][f'n{Y1}']))
        delivery_by_buyer_item.append({'buyer': buyer, 'overallPct': pct(rate(arr)), 'lines': len(arr),
                                       'totals': del_totals(arr), 'total': brand_row('全部', arr, exc_b),
                                       'cats': cats})

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
                               'sales': r2(s), 'cost': r2(c), 'vol': r2(sum(r['vol'] for r in arr)),
                               'grossProfit': r2(s - c),
                               'margin': margin(s, c), 'arr': arr})

    def find_pm(y, m):
        return next((p for p in profit_monthly if p['year'] == y and p['month'] == m), None)

    margin_yoy = []
    for m in range(1, 13):
        a, b = find_pm(Y0, m), find_pm(Y1, m)
        if not a and not b:
            continue
        ma, mb = a['margin'] if a else None, b['margin'] if b else None
        # 同時呈現該月的 下單量 / 採購未稅金額 (= 成本) 與年增率
        va, vb = a['vol'] if a else 0, b['vol'] if b else 0
        ca, cb = a['cost'] if a else 0, b['cost'] if b else 0
        margin_yoy.append({'month': m, f'margin{Y0}Pct': pct(ma), f'margin{Y1}Pct': pct(mb),
                           f'sales{Y0}': a['sales'] if a else 0, f'sales{Y1}': b['sales'] if b else 0,
                           'deltaPP': r2((mb - ma) * 100) if ma is not None and mb is not None else None,
                           f'vol{Y0}': va, f'vol{Y1}': vb, 'volYoyPct': pct((vb - va) / va) if va > 0 else None,
                           f'cost{Y0}': ca, f'cost{Y1}': cb, 'costYoyPct': pct((cb - ca) / ca) if ca > 0 else None})

    yearly_summary = []
    for y in YEARS:
        ms = [p for p in profit_monthly if p['year'] == y]
        s, c = sum(p['sales'] for p in ms), sum(p['cost'] for p in ms)
        yearly_summary.append({'year': y, 'months': len(ms),
                               'avgMonthlyMarginPct': pct(mean([p['margin'] for p in ms if p['margin'] is not None])),
                               'aggregateMarginPct': pct(margin(s, c)), 'sales': r2(s), 'cost': r2(c),
                               'vol': r2(sum(p['vol'] for p in ms))})

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
    for name, arr in group_by(recs_m, lambda r: r['name']).items():
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
               'categoryCostTrend': [{'category': c, 'trend': trend([r for r in recs_m if r['cat'] == c])}
                                     for c in x['cats']]} for x in name_agg]

    return {
        'generatedAt': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
        'filter': {'status': '已確認', 'years': YEARS, 'volumeMetric': VOLUME_METRIC, 'sameCutoffMonth': cutoff, 'monthFrom': MONTH_FROM, 'monthTo': MONTH_TO},
        'totalRecords': len(recs),
        'deliveryRecords': len(del_recs),
        'dim1_orderVolume': {
            'monthlyVolume': monthly_volume,
            'monthlyByCat': monthly_by_cat,
            'topProductLineGrowth': top_lines,
            'topAmountItems': top_amount_items,
            'brandGrowth': {'windows': bg_labels, 'groups': brand_growth},
            'categoryVolatility': cat_summary,
            'volatileCategories': [c['category'] for c in cat_summary if c['volatilityFlag']],
        },
        'dim2_deliveryRate': {'overallByMonth': delivery_overall, 'overallYoY': delivery_overall_yoy,
                              'byBuyer': delivery_by_buyer, 'byCategory': delivery_by_category,
                              'byBrand': delivery_by_brand, 'byBuyerItem': delivery_by_buyer_item,
                              'exceptions': delivery_exceptions},
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
td.lown{color:var(--muted);font-style:italic}
.hlgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;margin:12px 0}
.hlgrid .card{margin:0}.card ul{margin:4px 0 0;padding-left:18px}.card li{margin:3px 0}.card li ul{margin:2px 0 4px}
"""



CSS += """
th.num{text-align:right}
th.grp{text-align:center}
td.gs,th.gs{border-left:2px solid var(--line)}
tr.gt td{border-top:2px solid var(--line)}td.gname{vertical-align:top;border-right:1px solid var(--line)}
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
    tabs = {k: {} for k in ('cat', 'sup', 'buy', 'name', 'line', 'brand', 'item', 'exc')}

    def ix(kind, v):
        return tabs[kind].setdefault(v, len(tabs[kind]))

    def cn(x):
        return int(x) if float(x).is_integer() else x

    rows = [[r['year'] or 0, r['month'] or 0, r['sYear'] or 0, r['sMonth'] or 0,
             ix('cat', r['cat']), ix('sup', r['supplier']), ix('buy', r['buyer']), ix('name', r['name']),
             cn(r['qty']), cn(r['cost']), cn(r['sales']), -1 if r['onTime'] is None else int(r['onTime']),
             ix('line', r['line']), ix('brand', r['brand']), ix('item', r['item']),
             ix('exc', r['exc']) if r['exc'] else -1, 1 if r['dropBuyer'] else 0]
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
    global YEARS, VOLUME_METRIC, MONTH_FROM, MONTH_TO, DATA_END
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
    DATA_END = max((r['ymd'] for r in recs if r['ymd']), default=None)  # 資料中最晚的採購日期
    log(f'已確認 {len(recs)} 筆, 分析中 ...')
    result = analyze(recs)
    if 'error' in result:
        raise ValueError(result['error'])
    (out / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    html_path = out / '採購分析報告.html'
    html_path.write_text(render_html(recs, {'y0': YEARS[0], 'y1': YEARS[1], 'mf': MONTH_FROM, 'mt': MONTH_TO,
                                                   'metric': VOLUME_METRIC, 'dataEnd': DATA_END}, result['generatedAt']),
                         encoding='utf-8')
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
