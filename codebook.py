"""產品線 / 交易品項 / 廠牌 歸類 (料件編號、品名、類別名稱、子類別名稱)

產品線 / 品項: 依序用「品名」產品關鍵字、「子類別名稱」、「類別名稱」辨識歸類;
        三者都沒有才用料件編號 第一碼 + 第4-6碼 對照 codebook.json (料號編碼.xlsx)。
        少數產品線碼為 4 碼 (例 H1107, 位於第3-6碼), 先試 4 碼再試 3 碼。
廠牌:  依序在「品名」、「子類別名稱」、「類別名稱」中找廠牌關鍵字
        (關鍵字取自碼表第7-9碼的廠牌名稱, 另加少數產品別名)。
        品名中括號內文字、「for ...」之後的文字不納入判斷 (避免規格/客戶名稱干擾)。
        三者都找不到才退回用料件編號第7-9碼對照碼表。
"""
import json
import re
import sys
from pathlib import Path

UNKNOWN_LINE = '(未對應碼表)'
UNKNOWN_BRAND = '(未識別)'
UNKNOWN_ITEM = '(未歸類)'

# 品名中出現這些產品名稱時視為對應廠牌 (key 為品名關鍵字, value 為碼表第7-9碼)
ALIASES = {
    'WINDOWS': '056', 'SQL SERVER': '056', 'M365': '056', 'OFFICE 365': '056',
    'THINKSYSTEM': '051', 'THINKPAD': '051', 'THINKCENTRE': '051', 'THINKSTATION': '051',
    'POWEREDGE': '027', 'PROLIANT': '041', 'SIMPLIVITY': '041',
    'VSPHERE': '094', 'VCENTER': '094', 'ESXI': '094',
    'FORTIGATE': '035', 'FORTIWIFI': '035', 'FORTIANALYZER': '035',
    'GENERO': '002', 'CRYSTAL': '016', 'BOE': '016', 'BUSINESSOBJECTS': '016', 'XCELSIUS': '016',
}

# 例外處理: 料件編號對不到碼表時 (自家智能製造軟體、Y 開頭編號等), 改由品名中的「產品」關鍵字判定產品線。
# 只認產品名稱; 維護 / 安裝 / 顧問 / 客製 / 授權 等服務或規格描述不參與判斷 (「sMES…軟體安裝」仍屬 MES)。
NAME_LINES = [
    ('MES 製造執行系統', ['MES', 'SMES', 'EMES', 'IMES', '車間執行', '現場執行', '智造執行', 'WIP', 'EMS', 'EPM', 'IEW', '預警管理', '設備稼動', '模治具']),
    ('SFT 廠區生產追蹤系統', ['SFT', '廠區生產', '智能現場管理', 'TRC', '產品回溯']),
    ('QMS 品質管理系統', ['QMS', 'SQMS', '品質管理', '質量管理', 'SPC']),
    ('APS 先進規劃排程系統', ['APS', 'SAPS', '先進規劃', '生產排程規劃']),
    ('e-Kanban 電子看板', ['KANBAN', 'EKB', '電子看板', '电子看板', '看板']),
    ('KMI / ESB / 設備雲 整合平台', ['KMI', 'ESB', '機台整合', '機臺整合', '機聯', '設備雲', '整合平台']),
    ('PLM 產品生命週期管理', ['OPENPLM', 'PLM']),
    ('eSOP 電子作業指導書', ['ESOP']),
    ('智工廠戰情室', ['戰情室']),
    ('Genero 開發工具', ['GENERO', '4J']),
    ('BusinessObjects / Crystal Reports', ['BUSINESSOBJECTS', 'CRYSTAL', 'BOE', 'BO', 'XCELSIUS']),
    ('V-Point', ['V-POINT']),
    ('TOP GP 整合介面', ['TOP GP']),
]
NAME_LINE_SUFFIX = '(品名辨識)'

# 歸類後視為同一品項 / 產品線的合併對照 (key 為歸類出來的名稱, value 為合併後名稱)
ITEM_ALIASES = {
    **{name: '伺服器' for name in (
        '機架式伺服器(單插槽)', '機架式伺服器(雙插槽)', '機架式伺服器(四插槽)',
        '直立式伺服器(單插槽)', '直立式伺服器(雙插槽)', '直立式伺服器(四插槽)',
        '刀鋒型伺服器', '郵件伺服器', '超融合', '伺服器元件', '印表機伺服器',
        '伺服器(類別辨識)',  # 子類別為「不分類」、只靠類別名稱「伺服器」歸出來的品項
        '伺服器元件(類別辨識)',  # 同上, 只靠類別名稱「伺服器元件」歸出來的品項
    )},
}

# 這幾家採購供應商「依供應商」各視為一個產品線, 不再細分第4-6碼 (仍依料件編號第一碼分開排名)
SUPPLIER_LINES = ['鼎華智能系統', 'FOUR J', 'SAP Ireland Limited']
SUPPLIER_LINE_SUFFIX = '(依供應商)'

# 第一碼 T 的硬體維護: 這些採購供應商的 T 類品項, 子類別名稱空白時一律歸為「硬體維護」 (這些供應商是通路商,
# 第一碼 H / S 的硬體、軟體採購不套用); 「硬體維護」品項不另外列占比最高廠牌
HW_MAINT_SUPPLIERS = ['群環科技', '精技台北', '精技台南', '精技台中', '精技新竹', '台灣聯想環', '漢碩科技']
HW_MAINT_LABEL = '硬體維護'
NO_BRAND_LINES = {HW_MAINT_LABEL}
# 廠牌辨識不出時 (未識別), 這些採購供應商的品項直接以指定的廠牌名稱取代空值
SUPPLIER_BRANDS = {'鼎華智能系統': '鼎華'}
CAT_LINE_SUFFIX = '(類別辨識)'

_ASCII = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &.'\-]*[A-Za-z0-9]|[A-Za-z0-9]")
_CJK = re.compile(r'[一-鿿]{2,}')
_BRACKETS = re.compile(r'\([^)]*\)|（[^）]*）|\[[^\]]*\]|【[^】]*】|<[^>]*>|＜[^＞]*＞')
_FOR = re.compile(r'(?<![A-Za-z])for(?![A-Za-z])', re.I)
_LINE_SUFFIX = re.compile(r'\s*-\s*(原方案|新方案)\s*[,，].*$')


def _norm_supplier(s):
    return re.sub(r'\s+', ' ', s or '').strip().upper()


_SUPPLIER_LINES = {_norm_supplier(s): s for s in SUPPLIER_LINES}


def supplier_line(supplier):
    """採購供應商在 SUPPLIER_LINES 清單內時, 回傳以該供應商命名的產品線; 否則 None"""
    name = _SUPPLIER_LINES.get(_norm_supplier(supplier))
    return name + SUPPLIER_LINE_SUFFIX if name else None


_HW_MAINT = {_norm_supplier(s) for s in HW_MAINT_SUPPLIERS}
_SUPPLIER_BRANDS = {_norm_supplier(k): v for k, v in SUPPLIER_BRANDS.items()}


def is_hw_maint_supplier(supplier):
    return _norm_supplier(supplier) in _HW_MAINT


def supplier_brand(supplier):
    """廠牌未識別時, 依採購供應商指定的廠牌名稱 (沒有則 None)"""
    return _SUPPLIER_BRANDS.get(_norm_supplier(supplier))


def is_unclassified(label):
    """不納入增幅排名的名稱: 對不到碼表 / 辨識不出 / 碼表中的「不分類」"""
    return label in (UNKNOWN_LINE, UNKNOWN_BRAND, UNKNOWN_ITEM) or label.endswith('不分類')


def _book():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    return json.loads((base / 'codebook.json').read_text(encoding='utf-8'))


def _clean(name):
    """去掉括號內文字與「for ...」之後的文字 (規格 / 客戶名稱不參與判斷)"""
    text = _BRACKETS.sub(' ', name or '')
    m = _FOR.search(text)
    return text[:m.start()] if m else text


def _kw_pattern(tok):
    tok = tok.upper()
    if re.fullmatch(r"[A-Z0-9 &.'\-]+", tok):
        return re.compile(r'(?<![A-Za-z0-9])' + re.escape(tok) + r'(?![A-Za-z0-9])', re.I)
    return re.compile(re.escape(tok))


def _name_line_keywords():
    return [(label, _kw_pattern(tok)) for label, toks in NAME_LINES for tok in toks]


def _brand_keywords(brands):
    """[(正規表示式, 第7-9碼)], 較長的關鍵字優先"""
    kws = []
    for code, desc in brands.items():
        if desc == '不分類':
            continue
        for tok in _ASCII.findall(desc) + _CJK.findall(desc):
            kws.append((tok.upper(), code))
    for tok, code in ALIASES.items():
        if code in brands:
            kws.append((tok, code))
    kws.sort(key=lambda x: -len(x[0]))
    out = []
    for tok, code in kws:
        if re.fullmatch(r"[A-Z0-9 &.'\-]+", tok):
            pat = re.compile(r'(?<![A-Za-z0-9])' + re.escape(tok) + r'(?![A-Za-z0-9])', re.I)
        else:
            pat = re.compile(re.escape(tok))
        out.append((pat, code))
    return out


class Classifier:
    def __init__(self):
        book = _book()
        self.lines = book['lines']
        self.brands = book['brands']
        self._kw = _brand_keywords(self.brands)
        self._name_kw = _name_line_keywords()
        self._cache = {}

    def _name_match(self, name):
        """品名中的產品關鍵字 → 產品線名稱 (無則 None); 品名中最前面出現者優先"""
        best = None  # (位置, -長度, 產品線)
        # 先看「for」之前; 都沒有才看完整品名 (例「客製程式版本升級 for SFT」, 產品在 for 之後)
        for text in (_clean(name), _BRACKETS.sub(' ', name or '')):
            for label, pat in self._name_kw:
                mm = pat.search(text)
                if mm:
                    cand = (mm.start(), -(mm.end() - mm.start()), label)
                    if best is None or cand < best:
                        best = cand
            if best:
                break
        return best[2] if best else None

    def item(self, pn, name='', category='', subcategory=''):
        """產品線 / 交易品項歸類: 依序用「品名」關鍵字、「子類別名稱」、「類別名稱」辨識;
        三者都沒有才退回料件編號 (第一碼+第4-6碼) 對照碼表, 仍不行回傳 UNKNOWN_ITEM;
        最後套用 ITEM_ALIASES 把指定的品項合併成同一項"""
        label = self._item_raw(pn, name, category, subcategory)
        return ITEM_ALIASES.get(label, label)

    def _item_raw(self, pn, name, category, subcategory):
        label = self._name_match(name)
        if label:
            return label + NAME_LINE_SUFFIX
        sub, cat = (subcategory or '').strip(), (category or '').strip()
        if sub and not sub.startswith('不分類'):
            return _LINE_SUFFIX.sub('', sub)
        if cat and cat != '不分類':
            return cat + CAT_LINE_SUFFIX
        # 碼表說明與 ERP 子類別名稱同名同寫法, 所以會與上面的「子類別」合併成同一項
        key = self._code_key(pn)
        return _LINE_SUFFIX.sub('', self.lines[key]).strip() if key else UNKNOWN_ITEM

    def _code_key(self, pn):
        """料件編號 → 碼表的產品線代碼 (第一碼+第4-6碼; 少數 4 碼者位於第3-6碼, 先試 4 碼)"""
        pn = pn.strip().upper()
        c1 = pn[:1]
        for key in ((c1 + pn[2:6]) if len(pn) >= 6 and pn[2] != '0' else None, c1 + pn[3:6]):
            if key and key in self.lines:
                return key
        return None

    def brand(self, pn, name, category='', subcategory=''):
        """回傳廠牌顯示名稱 (碼表第7-9碼的廠牌說明); 辨識不出回傳 UNKNOWN_BRAND。

        依序在「品名」(不含括號內文字與 for 之後)、「子類別名稱」、「類別名稱」中找廠牌關鍵字,
        每個欄位內取最前面出現者; 三者都找不到才退回料件編號第7-9碼對照碼表。
        """
        key = (pn, name, category, subcategory)
        if key in self._cache:
            return self._cache[key]
        code = None
        for text in (_clean(name), subcategory or '', category or ''):
            best = None  # (位置, -長度, code)
            for pat, c in self._kw:
                mm = pat.search(text)
                if mm:
                    cand = (mm.start(), -(mm.end() - mm.start()), c)
                    if best is None or cand < best:
                        best = cand
            if best and self.brands.get(best[2]) not in (None, '不分類'):
                code = best[2]
                break
        if code is None:
            p = pn.strip().upper()
            # 退回料件編號第7-9碼 (僅限格式正常的編號; VD/Y 類編號位置不同, 不可靠)
            if len(p) >= 9 and p[0] in 'SHT' and p[1:3].isdigit() and p[3:9].isdigit():
                code = p[6:9]
        desc = self.brands.get(code) if code else None
        out = desc if desc and desc != '不分類' else UNKNOWN_BRAND
        self._cache[key] = out
        return out
