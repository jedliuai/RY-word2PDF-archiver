"""Native Windows UI. Document work stays off Tk's main thread."""
from __future__ import annotations

import os
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinterdnd2 import COPY, DND_FILES, REFUSE_DROP, TkinterDnD
from archive_input import ArchiveImporter, ArchivePending
from pdf_workflow import Workflow

PHASES = {
    'new': '拖入文件夹、ZIP 或 RAR，然后点击开始', 'scanned': '扫描完成，可以开始',
    'extracting': '正在解压，原压缩包会保留', 'waiting_input': '压缩包等待绿盾解密批准',
    'converting': '正在调用 WPS 转换', 'decrypting': '正在检查绿盾加密状态',
    'waiting': '等待绿盾解密批准', 'verifying': '正在校验全部 PDF',
    'archiving': '正在打包并校验 RAR', 'review': '交付物就绪，等待你确认清理',
    'cleaning': '正在移入回收站', 'complete': '全部完成',
    'stopped': '已停止，请检查日志', 'error': '遇到问题，已阻断后续操作',
}


class WorkflowApp:
    def __init__(self, root):
        self.root = root
        root.title('WPS 转 PDF · 解密归档助手')
        root.geometry('1100x930')
        root.minsize(920, 860)
        root.configure(bg='#eef2f7')
        self.events = queue.Queue()
        self.workflow = None
        self.busy = False
        self.importer = ArchiveImporter()
        self.prepared_input = None
        self.input_cancel = threading.Event()
        self.input_phase = 'new'
        self.pending_scan_only = False
        self.selected = set()
        self.rows = {}
        self.folder = tk.StringVar()
        self.overwrite = tk.BooleanVar(value=False)
        self.auto = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value=PHASES['new'])
        self.summary = tk.StringVar(value='尚未扫描')
        self.archive_text = tk.StringVar(value='RAR 将生成在源文件夹旁边，只包含 PDF，保留子目录。')
        self._build()
        self._bind_drop()
        self.root.after(100, self._poll)
        self.root.protocol('WM_DELETE_WINDOW', self._close)

    def _build(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('TFrame', background='#eef2f7')
        style.configure('TLabel', background='#eef2f7', foreground='#25354a')
        style.configure('TCheckbutton', background='#eef2f7')
        style.configure('TButton', padding=(12, 8))
        style.configure('Primary.TButton', background='#2463db', foreground='white')
        style.map('Primary.TButton', background=[('active', '#164cb6'), ('disabled', '#a6b9da')])
        style.configure('Treeview', rowheight=31, background='white', fieldbackground='white')
        page = ttk.Frame(self.root, padding=24)
        page.pack(fill='both', expand=True)
        ttk.Label(page, text='Word → PDF → RAR', font=('Microsoft YaHei UI', 23, 'bold')).pack(anchor='w')
        ttk.Label(page, text='WPS 批量转换 · 绿盾官方解密 · 验证后清理', foreground='#627187').pack(anchor='w', pady=(4, 16))
        self.drop_area = tk.Label(page, text='把 ZIP / RAR 压缩包或文件夹拖到这里，再点击开始',
                                  bg='#e2ebfb', fg='#245699', font=('Microsoft YaHei UI', 11), pady=10)
        self.drop_area.pack(fill='x', pady=(0, 10))
        folderbar = ttk.Frame(page)
        folderbar.pack(fill='x')
        self.entry = ttk.Entry(folderbar, textvariable=self.folder)
        self.entry.pack(side='left', fill='x', expand=True, ipady=7)
        self.choose_btn = ttk.Button(folderbar, text='选择文件夹', command=self.choose)
        self.choose_btn.pack(side='left', padx=(10, 0))
        self.archive_btn = ttk.Button(folderbar, text='选择压缩包', command=self.choose_archive)
        self.archive_btn.pack(side='left', padx=(8, 0))
        self.scan_btn = ttk.Button(folderbar, text='扫描清单', command=self.scan)
        self.scan_btn.pack(side='left', padx=(8, 0))
        options = ttk.Frame(page)
        options.pack(fill='x', pady=12)
        self.overwrite_btn = ttk.Checkbutton(options, text='备份旧 PDF 后重新转换', variable=self.overwrite)
        self.overwrite_btn.pack(side='left')
        self.auto_btn = ttk.Checkbutton(options, text='全自动：RAR 验证通过后，把全部 Word 移入回收站', variable=self.auto)
        self.auto_btn.pack(side='left', padx=(18, 0))
        ttk.Label(page, text='① 解压 / 扫描  →  ② WPS 转换  →  ③ 解密与校验  →  ④ RAR 打包校验  →  ⑤ 清理 Word',
                  font=('Microsoft YaHei UI', 10, 'bold')).pack(fill='x', pady=(0, 12))
        statusbar = ttk.Frame(page)
        statusbar.pack(fill='x')
        ttk.Label(statusbar, textvariable=self.status, font=('Microsoft YaHei UI', 13, 'bold')).pack(side='left')
        ttk.Label(statusbar, textvariable=self.summary).pack(side='right')
        self.progress = ttk.Progressbar(page, mode='determinate')
        self.progress.pack(fill='x', pady=(8, 10))
        actions = ttk.Frame(page)
        actions.pack(fill='x', pady=(0, 10))
        self.start_btn = ttk.Button(actions, text='开始转换、解密并打包', style='Primary.TButton', command=self.start)
        self.start_btn.pack(side='left')
        self.resume_btn = ttk.Button(actions, text='继续等待解密', command=self.resume, state='disabled')
        self.resume_btn.pack(side='left', padx=8)
        self.stop_btn = ttk.Button(actions, text='停止', command=self.stop, state='disabled')
        self.stop_btn.pack(side='left')
        self.output_btn = ttk.Button(actions, text='打开交付目录', command=self.open_output, state='disabled')
        self.output_btn.pack(side='right')
        ttk.Label(page, textvariable=self.archive_text, wraplength=1000, foreground='#52647b').pack(anchor='w', pady=(0, 10))
        listbar = ttk.Frame(page)
        listbar.pack(fill='x')
        ttk.Label(listbar, text='Word 清理清单', font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
        self.select_btn = ttk.Button(listbar, text='全选可清理项', command=lambda: self.select_all(True), state='disabled')
        self.select_btn.pack(side='left', padx=(12, 6))
        self.clear_btn = ttk.Button(listbar, text='取消全选', command=lambda: self.select_all(False), state='disabled')
        self.clear_btn.pack(side='left')
        self.clean_btn = ttk.Button(listbar, text='确认清理选中 Word', command=self.cleanup, state='disabled')
        self.clean_btn.pack(side='right')
        frame = ttk.Frame(page)
        frame.pack(fill='both', expand=True, pady=(8, 12))
        self.tree = ttk.Treeview(frame, columns=('check', 'name', 'status'), show='headings', selectmode='browse', height=4)
        for column, title in [('check', '清理'), ('name', '源文档（包含子目录）'), ('status', '检查结果')]:
            self.tree.heading(column, text=title)
        self.tree.column('check', width=55, stretch=False, anchor='center')
        self.tree.column('name', width=610)
        self.tree.column('status', width=230)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.tree.bind('<Button-1>', self._toggle)
        ttk.Label(page, text='运行日志', font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w')
        self.log_box = tk.Text(page, height=5, wrap='word', relief='flat', background='#192538',
                               foreground='#dce7f7', padx=12, pady=8, font=('Microsoft YaHei UI', 9), state='disabled')
        self.log_box.pack(fill='x', pady=(6, 8))
        ttk.Label(page, text='完整任务完成后自动清理中间文件，保留原始输入和最终 RAR；Word 仍按选项确认清理。',
                  foreground='#6b7585').pack(anchor='w')

    def choose(self):
        selected = filedialog.askdirectory(title='选择包含 Word / PDF 的业务文件夹')
        if selected:
            self.accept_input(selected)

    def choose_archive(self):
        selected = filedialog.askopenfilename(title='选择 ZIP / RAR 压缩包',
                    filetypes=[('ZIP / RAR 压缩包', '*.zip *.rar'), ('所有文件', '*.*')])
        if selected:
            self.accept_input(selected)

    def _bind_drop(self):
        # splitlist preserves Windows paths containing spaces, braces and Chinese.
        for widget in (self.root, self.drop_area, self.entry, self.tree):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind('<<Drop>>', self.on_drop)

    def on_drop(self, event):
        if self.busy:
            return REFUSE_DROP
        try:
            paths = self.root.tk.splitlist(event.data)
            if len(paths) != 1:
                raise ValueError('每次请拖入一个文件夹或压缩包。')
            self.accept_input(paths[0])
            return COPY
        except Exception as exc:
            messagebox.showwarning('无法接收拖入内容', str(exc))
            return REFUSE_DROP

    def accept_input(self, value):
        if self.busy:
            return
        path = Path(value)
        if not path.exists() or (not path.is_dir() and path.suffix.lower() not in ('.zip', '.rar')):
            raise ValueError('请拖入存在的文件夹、ZIP 或 RAR 压缩包。')
        self.folder.set(str(path.absolute()))
        self.workflow = None
        self.prepared_input = None
        self.input_phase = 'new'
        self.rows.clear()
        self.selected.clear()
        self.tree.delete(*self.tree.get_children())
        self.summary.set('尚未扫描')
        self.status.set(PHASES['new'])
        self.archive_text.set('RAR 将生成在源文件夹旁边，只包含 PDF，保留子目录。')
        self._controls()

    def _begin(self, *, scan_only=False):
        text = self.folder.get().strip().strip('"')
        if not text:
            raise ValueError('请拖入或选择一个文件夹、ZIP 或 RAR。')
        source = Path(os.path.abspath(text))
        overwrite, auto = self.overwrite.get(), self.auto.get()
        reusable = (self.workflow is not None and self.workflow.phase == 'scanned' and
                    self.prepared_input is not None and self.prepared_input.source == source and
                    self.workflow.overwrite == overwrite and not scan_only)
        self.pending_scan_only = scan_only
        self.input_cancel.clear()
        if not reusable:
            self.workflow = None
            self.prepared_input = None
            self.selected.clear()
            self.rows.clear()
            self.tree.delete(*self.tree.get_children())
        def import_and_run():
            emit = lambda kind, value: self.events.put((kind, value))
            if not reusable:
                prepared = self.importer.prepare(source, cancel=self.input_cancel, emit=emit)
                self.prepared_input = prepared
                if self.input_cancel.is_set():
                    from local_services import Cancelled
                    raise Cancelled('已停止导入，后续转换未执行。')
                self.workflow = Workflow(prepared.folder, emit=emit, overwrite=overwrite,
                                         delivery_name=prepared.source.stem if prepared.is_archive else None,
                                         prepared_input=prepared)
                self.workflow.cancel = self.input_cancel
                (self.workflow.job_dir / 'input.json').write_text(json.dumps({
                    'source': str(prepared.source), 'folder': str(prepared.folder),
                    'archive': prepared.is_archive, 'source_hash': prepared.source_hash,
                    'extraction_report': prepared.report,
                }, ensure_ascii=False, indent=2), encoding='utf-8')
                emit('input_ready', prepared)
            self.prepared_input.verify_source()
            if scan_only:
                return self.workflow.scan()
            return self.workflow.prepare()
        self.progress['value'] = 0
        self._work(import_and_run, auto=auto and not scan_only)

    def _work(self, operation, *, auto=False):
        if self.busy:
            return
        self.busy = True
        self._controls()
        def worker():
            error = None
            try:
                result = operation()
                workflow = self.workflow
                if auto and result and workflow and workflow.phase == 'review' and workflow.documents:
                    workflow.cleanup([d.source for d in workflow.documents if not d.recycled])
            except Exception as exc:
                error = str(exc)
                workflow = self.workflow
                if isinstance(exc, ArchivePending):
                    self.input_phase = 'waiting_input'
                    self.events.put(('state', 'waiting_input'))
                elif workflow is None:
                    self.input_phase = 'error'
                    self.events.put(('state', 'error'))
                elif workflow.phase not in ('error', 'stopped'):
                    try:
                        workflow._failed(exc)
                    except Exception as report_error:
                        workflow.phase = 'error'
                        error += f'\n任务报告写入也失败：{report_error}'
                        self.events.put(('state', 'error'))
            finally:
                self.events.put(('done', error))
        threading.Thread(target=worker, daemon=True).start()

    def scan(self):
        try:
            self._begin(scan_only=True)
        except Exception as exc:
            messagebox.showerror('不能扫描', str(exc))

    def start(self):
        try:
            self._begin()
        except Exception as exc:
            messagebox.showerror('不能开始', str(exc))

    def resume(self):
        if self.workflow is None and self.input_phase == 'waiting_input':
            self._begin(scan_only=self.pending_scan_only)
        else:
            self._work(self.workflow.resume, auto=self.auto.get())

    def stop(self):
        self.input_cancel.set()
        self.status.set('正在停止；请等待当前安全检查结束')
        if self.workflow:
            self.workflow.cancel.set()

    def select_all(self, checked):
        if not self.workflow or self.workflow.phase != 'review' or self.busy:
            return
        self.selected = {key for key, row in self.rows.items() if not row['recycled']} if checked else set()
        self._render(list(self.rows.values()))

    def _toggle(self, event):
        if self.busy or not self.workflow or self.workflow.phase != 'review':
            return
        if self.tree.identify_column(event.x) != '#1':
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        source = self.tree.item(item, 'tags')[0]
        if not self.rows[source]['recycled']:
            self.selected.symmetric_difference_update({source})
            self._render(list(self.rows.values()))

    def cleanup(self):
        chosen = sorted(self.selected)
        if not chosen:
            messagebox.showinfo('尚未选择', '请先点击“全选可清理项”或勾选 Word。')
            return
        outcome = ('任务完成后自动清理解压目录，原压缩包和最终 RAR 会保留。'
                   if self.prepared_input and self.prepared_input.is_archive
                   else 'PDF 和最终 RAR 会保留，工作副本和备份自动清理。')
        if messagebox.askyesno('确认清理', f'已生成并验证 RAR。\n\n请确认已抽查 PDF 的排版、图片和内容。\n\n将选中的 {len(chosen)} 份 Word 移入回收站？\n{outcome}'):
            self._work(lambda: self.workflow.cleanup(chosen))

    def _render(self, rows):
        self.rows = {row['source']: row for row in rows}
        self.selected.intersection_update(key for key, row in self.rows.items() if not row['recycled'])
        self.tree.delete(*self.tree.get_children())
        for source, row in self.rows.items():
            mark = '✓' if row['recycled'] else ('☑' if source in self.selected else '☐')
            self.tree.insert('', 'end', values=(mark, str(Path(source).relative_to(self.workflow.root)), row['status']), tags=(source,))
        self.summary.set(f'Word {len(rows)} 份  ·  已校验 {sum(bool(row["pdf_hash"]) for row in rows)} 份  ·  已选 {len(self.selected)} 份')

    def _controls(self):
        phase = self.workflow.phase if self.workflow else self.input_phase
        for control in (self.choose_btn, self.archive_btn, self.scan_btn, self.start_btn, self.overwrite_btn, self.auto_btn):
            control.configure(state='disabled' if self.busy else 'normal')
        self.entry.configure(state='disabled' if self.busy else 'normal')
        self.stop_btn.configure(state='normal' if self.busy else 'disabled')
        self.resume_btn.configure(state='normal' if not self.busy and phase in ('waiting', 'waiting_input') else 'disabled')
        for control in (self.select_btn, self.clear_btn, self.clean_btn):
            control.configure(state='normal' if not self.busy and phase == 'review' else 'disabled')
        self.output_btn.configure(state='normal' if self.workflow and self.workflow.archive else 'disabled')

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.log_box.configure(state='normal')
                    self.log_box.insert('end', value + '\n')
                    self.log_box.see('end')
                    self.log_box.configure(state='disabled')
                elif kind == 'state':
                    self.status.set(PHASES.get(value, value))
                    self.progress.stop()
                    if value in ('extracting', 'decrypting', 'waiting', 'waiting_input', 'verifying', 'archiving', 'cleaning'):
                        self.progress.configure(mode='indeterminate')
                        self.progress.start(20)
                    else:
                        self.progress.configure(mode='determinate')
                        self.progress['value'] = 100 if value in ('review', 'complete') else 0
                elif kind == 'files':
                    self._render(value)
                elif kind == 'input_ready':
                    prefix = '解压处理目录（原压缩包保留）：' if value.is_archive else '处理目录：'
                    self.archive_text.set(prefix + str(value.folder))
                elif kind == 'cleanup_warning':
                    messagebox.showwarning('交付完成，部分中间文件未清理', value)
                elif kind == 'progress':
                    current, total = value
                    self.progress['value'] = current * 100 / max(total, 1)
                elif kind == 'done':
                    self.busy = False
                    self.progress.stop()
                    self._controls()
                    if self.workflow and self.workflow.archive:
                        self.archive_text.set('交付文件：' + str(self.workflow.archive))
                    if value:
                        extra = '\n\n任务报告：\n' + str(self.workflow.job_dir) if self.workflow else ''
                        if self.input_phase == 'waiting_input' and self.workflow is None:
                            messagebox.showinfo('等待压缩包解密', value)
                        else:
                            messagebox.showerror('流程已停止', value + extra)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def open_output(self):
        if self.workflow and self.workflow.archive:
            os.startfile(str(self.workflow.archive.parent))

    def _close(self):
        if self.busy:
            messagebox.showinfo('任务正在运行', '请先点击“停止”，等待任务停止后再关闭窗口。')
            return
        self.root.destroy()


def main():
    root = TkinterDnD.Tk()
    WorkflowApp(root)
    root.mainloop()
