"""採購分析報告產生器 (視窗版): 選 Excel → 產生 HTML"""
import os
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import analyze

METRICS = {'明細行數': 'lines', '採購未稅金額': 'amount', '分批採購數量': 'qty'}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('採購分析報告產生器')
        self.resizable(False, False)
        pad = {'padx': 8, 'pady': 4}

        self.src = tk.StringVar()
        self.out = tk.StringVar()
        self.y0 = tk.IntVar(value=2025)
        self.y1 = tk.IntVar(value=2026)
        self.metric = tk.StringVar(value='明細行數')
        self.open_after = tk.BooleanVar(value=True)

        ttk.Label(self, text='資料檔 (Excel/CSV)').grid(row=0, column=0, sticky='e', **pad)
        ttk.Entry(self, textvariable=self.src, width=50).grid(row=0, column=1, **pad)
        ttk.Button(self, text='選擇…', command=self.pick_src).grid(row=0, column=2, **pad)

        ttk.Label(self, text='輸出資料夾').grid(row=1, column=0, sticky='e', **pad)
        ttk.Entry(self, textvariable=self.out, width=50).grid(row=1, column=1, **pad)
        ttk.Button(self, text='選擇…', command=self.pick_out).grid(row=1, column=2, **pad)

        yrs = ttk.Frame(self)
        yrs.grid(row=2, column=1, sticky='w', **pad)
        ttk.Label(self, text='比較年度').grid(row=2, column=0, sticky='e', **pad)
        ttk.Spinbox(yrs, from_=2000, to=2100, textvariable=self.y0, width=6).pack(side='left')
        ttk.Label(yrs, text=' (基準) vs ').pack(side='left')
        ttk.Spinbox(yrs, from_=2000, to=2100, textvariable=self.y1, width=6).pack(side='left')
        ttk.Label(yrs, text=' (比較)').pack(side='left')

        ttk.Label(self, text='下單量定義').grid(row=3, column=0, sticky='e', **pad)
        ttk.Combobox(self, textvariable=self.metric, values=list(METRICS), state='readonly',
                     width=16).grid(row=3, column=1, sticky='w', **pad)

        ttk.Checkbutton(self, text='完成後自動開啟報告', variable=self.open_after).grid(
            row=4, column=1, sticky='w', **pad)

        self.btn = ttk.Button(self, text='產生報告', command=self.start)
        self.btn.grid(row=5, column=1, **pad)
        self.status = tk.StringVar(value='請選擇資料檔')
        ttk.Label(self, textvariable=self.status, foreground='#555').grid(
            row=6, column=0, columnspan=3, sticky='w', **pad)

    def pick_src(self):
        f = filedialog.askopenfilename(filetypes=[('Excel / CSV', '*.xlsx *.xlsm *.xls *.csv'), ('所有檔案', '*.*')])
        if f:
            self.src.set(f)
            if not self.out.get():
                self.out.set(str(Path(f).parent / 'output'))

    def pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.out.set(d)

    def start(self):
        if not self.src.get() or not os.path.isfile(self.src.get()):
            return messagebox.showwarning('提醒', '請先選擇存在的資料檔')
        try:
            years = [self.y0.get(), self.y1.get()]
        except tk.TclError:
            return messagebox.showwarning('提醒', '年度必須是數字')
        if years[0] == years[1]:
            return messagebox.showwarning('提醒', '基準年與比較年不能相同')
        self.btn.state(['disabled'])
        self.status.set('處理中，資料量大時需要一點時間…')
        threading.Thread(target=self.work, args=(years,), daemon=True).start()

    def work(self, years):
        try:
            path = analyze.run(self.src.get(), self.out.get() or 'output', years,
                               METRICS[self.metric.get()], log=lambda m: self.after(0, self.status.set, m))
            self.after(0, self.done, path, None)
        except Exception as e:  # 顯示給使用者, 不吞掉
            self.after(0, self.done, None, e)

    def done(self, path, err):
        self.btn.state(['!disabled'])
        if err:
            self.status.set('失敗')
            messagebox.showerror('失敗', f'{type(err).__name__}: {err}')
        else:
            self.status.set(f'完成：{path}')
            if self.open_after.get():
                webbrowser.open(Path(path).as_uri())


if __name__ == '__main__':
    App().mainloop()
