"""由 data/料號編碼.xlsx 產生 codebook.json (料件編號 第一碼 / 第4-6碼 / 第7-9碼 對照表)

用法: python make_codebook.py [料號編碼.xlsx]
"""
import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent


def build(xlsx):
    df = pd.read_excel(xlsx, dtype=str, header=None, skiprows=1).fillna('')
    lines, brands = {}, {}
    for c1, c46, d46, c79, d79 in df.iloc[:, :5].itertuples(index=False):
        c1, c46, c79 = c1.strip().upper(), c46.strip(), c79.strip()
        if c1 and c46:
            lines[f'{c1}{c46}'] = d46.strip()
        if c79 and d79.strip():
            brands[c79] = d79.strip()
    return {'lines': lines, 'brands': brands}


if __name__ == '__main__':
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else next(HERE.glob('data/料號編碼.xlsx'))
    book = build(src)
    (HERE / 'codebook.json').write_text(json.dumps(book, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'codebook.json: {len(book["lines"])} 產品線碼, {len(book["brands"])} 廠牌碼')
