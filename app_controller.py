"""UI-agnostic controller: drives the existing workflow and publishes plain-JSON state.

The web front end (web/) talks to this class only through `web_app.Api`. It holds the
same orchestration the Tkinter window used to do (import -> scan/prepare -> resume ->
cleanup) without touching pdf_workflow / archive_input / local_services.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from archive_input import ArchiveImporter, ArchivePending
from local_services import Cancelled
from pdf_workflow import Workflow

PHASES = {
    'new': '拖入文件夹、ZIP 或 RAR，然后点击开始', 'scanned': '扫描完成，可以开始',
    'extracting': '正在解压，原压缩包会保留', 'waiting_input': '压缩包等待绿盾解密批准',
    'converting': '正在调用 WPS 转换', 'decrypting': '正在检查绿盾加密状态',
    'waiting': '等待绿盾解密批准', 'verifying': '正在校验全部 PDF',
    'archiving': '正在打包并校验 RAR', 'review': '交付物就绪，等待你确认清理',
    'cleaning': '正在移入回收站', 'complete': '全部完成',
    'stopped': '已停止', 'error': '遇到问题，已阻断后续操作',
}
INDETERMINATE = {'extracting', 'decrypting', 'waiting', 'waiting_input', 'verifying', 'archiving', 'cleaning'}
LOG_LIMIT = 500


class Controller:
    def __init__(self, notify=None, importer=None):
        self.notify = notify or (lambda state: None)
        self.lock = threading.RLock()
        self.importer = importer or ArchiveImporter()
        self.input_cancel = threading.Event()
        self._reset_input()
        self.overwrite = False
        self.auto = False

    # ---------- state ----------
    def _reset_input(self, path=''):
        self.path = path
        self.workflow = None
        self.prepared_input = None
        self.input_phase = 'new'
        self.display_phase = 'new'
        self.pending_scan_only = False
        self.busy = False
        self.busy_kind = ''
        self.rows = {}
        self.selected = set()
        self.logs = []
        self.progress = None
        self.processing_folder = ''
        self.error = None
        self.notice = None
        self.cleanup_warning = ''
        self.started_at = None

    def _phase(self):
        return self.workflow.phase if self.workflow else self.input_phase

    def snapshot(self):
        with self.lock:
            phase = self._phase()
            workflow = self.workflow
            root = workflow.root if workflow else None
            files = []
            for source, row in self.rows.items():
                try:
                    rel = str(Path(source).relative_to(root)) if root else source
                except ValueError:
                    rel = source
                files.append({'source': source, 'name': rel, 'status': row['status'],
                              'pages': row.get('pages', 0), 'exported': row.get('exported', False),
                              'verified': bool(row.get('pdf_hash')),
                              'recycled': row['recycled'], 'selected': source in self.selected})
            path = Path(self.path) if self.path else None
            archive = workflow.archive if workflow and workflow.archive else None
            display = self.display_phase
            return {
                'busy': self.busy, 'busyKind': self.busy_kind,
                'phase': phase, 'displayPhase': display,
                'statusText': PHASES.get(display, display),
                'input': None if not path else {
                    'path': str(path), 'name': path.name or str(path),
                    'kind': 'archive' if path.suffix.lower() in ('.zip', '.rar') else 'folder',
                },
                'processingFolder': self.processing_folder,
                'options': {'overwrite': self.overwrite, 'auto': self.auto},
                'files': files,
                'counts': {'words': len(files), 'verified': sum(f['verified'] for f in files),
                           'selected': len(self.selected), 'recycled': sum(f['recycled'] for f in files),
                           'pdfs': len(workflow.pdfs) if workflow else 0},
                'progress': {'indeterminate': display in INDETERMINATE,
                             'current': self.progress[0] if self.progress else 0,
                             'total': self.progress[1] if self.progress else 0},
                'archive': None if not archive else {'path': str(archive), 'name': archive.name,
                                                      'folder': str(archive.parent)},
                'isArchiveInput': bool(self.prepared_input and self.prepared_input.is_archive),
                'canResume': not self.busy and phase in ('waiting', 'waiting_input'),
                'canCleanup': not self.busy and phase == 'review',
                'logs': self.logs[-LOG_LIMIT:],
                'error': self.error, 'notice': self.notice,
                'cleanupWarning': self.cleanup_warning,
                'startedAt': self.started_at,
            }

    def _push(self):
        self.notify(self.snapshot())

    # ---------- workflow events (any thread) ----------
    def on_event(self, kind, value):
        with self.lock:
            if kind == 'log':
                self.logs.append({'time': time.strftime('%H:%M:%S'), 'text': str(value)})
            elif kind == 'state':
                self.display_phase = value
                if value not in INDETERMINATE and value != 'converting':
                    self.progress = None
            elif kind == 'files':
                self.rows = {row['source']: row for row in value}
                self.selected.intersection_update(k for k, r in self.rows.items() if not r['recycled'])
            elif kind == 'input_ready':
                self.processing_folder = str(value.folder)
            elif kind == 'cleanup_warning':
                self.cleanup_warning = str(value)
            elif kind == 'progress':
                self.progress = tuple(value)
            elif kind == 'done':
                self.busy = False
                self.busy_kind = ''
                if self.workflow is not None:
                    self.display_phase = self.workflow.phase
                if value:
                    if self._phase() in ('waiting', 'waiting_input'):
                        self.notice = {'title': PHASES[self._phase()], 'message': value}
                    else:
                        self.error = {'title': '流程已停止', 'message': value,
                                      'jobDir': str(self.workflow.job_dir) if self.workflow else ''}
        self._push()

    # ---------- commands ----------
    def set_options(self, overwrite=None, auto=None):
        with self.lock:
            if self.busy:
                return self.snapshot()
            if overwrite is not None:
                self.overwrite = bool(overwrite)
            if auto is not None:
                self.auto = bool(auto)
        self._push()
        return self.snapshot()

    def accept_input(self, value):
        with self.lock:
            if self.busy:
                raise ValueError('任务正在运行，请先停止。')
            path = Path(str(value).strip().strip('"'))
            if not path.exists() or (not path.is_dir() and path.suffix.lower() not in ('.zip', '.rar')):
                raise ValueError('请拖入存在的文件夹、ZIP 或 RAR 压缩包。')
            self._reset_input(str(path.absolute()))
        self._push()
        return self.snapshot()

    def reset(self):
        with self.lock:
            if self.busy:
                raise ValueError('任务正在运行，请先停止。')
            self._reset_input()
        self._push()
        return self.snapshot()

    def dismiss(self):
        with self.lock:
            self.error = None
            self.notice = None
        self._push()

    def scan(self):
        self._begin(scan_only=True)

    def start(self):
        self._begin(scan_only=False)

    def _begin(self, *, scan_only):
        with self.lock:
            if self.busy:
                return
            if not self.path:
                raise ValueError('请拖入或选择一个文件夹、ZIP 或 RAR。')
            source = Path(os.path.abspath(self.path))
            overwrite, auto = self.overwrite, self.auto
            reusable = (self.workflow is not None and self.workflow.phase == 'scanned' and
                        self.prepared_input is not None and self.prepared_input.source == source and
                        self.workflow.overwrite == overwrite and not scan_only)
            self.pending_scan_only = scan_only
            self.input_cancel.clear()
            self.error = None
            self.notice = None
            if not reusable:
                self.workflow = None
                self.prepared_input = None
                self.selected.clear()
                self.rows.clear()
                self.input_phase = 'new'
        emit = self.on_event

        def import_and_run():
            if not reusable:
                prepared = self.importer.prepare(source, cancel=self.input_cancel, emit=emit)
                self.prepared_input = prepared
                if self.input_cancel.is_set():
                    raise Cancelled('已停止导入，后续转换未执行。')
                workflow = Workflow(prepared.folder, emit=emit, overwrite=overwrite,
                                    delivery_name=prepared.source.stem if prepared.is_archive else None,
                                    prepared_input=prepared)
                workflow.cancel = self.input_cancel
                (workflow.job_dir / 'input.json').write_text(json.dumps({
                    'source': str(prepared.source), 'folder': str(prepared.folder),
                    'archive': prepared.is_archive, 'source_hash': prepared.source_hash,
                    'extraction_report': prepared.report,
                }, ensure_ascii=False, indent=2), encoding='utf-8')
                with self.lock:
                    self.workflow = workflow
                emit('input_ready', prepared)
            self.prepared_input.verify_source()
            if scan_only:
                return self.workflow.scan()
            return self.workflow.prepare()

        self._work(import_and_run, auto=auto and not scan_only, kind='scan' if scan_only else 'run')

    def _work(self, operation, *, auto=False, kind='run'):
        with self.lock:
            if self.busy:
                return
            self.busy = True
            self.busy_kind = kind
            self.cleanup_warning = '' if kind != 'cleanup' else self.cleanup_warning
            if kind in ('run', 'scan') and self.started_at is None:
                self.started_at = time.time()
        self._push()

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
                    self.on_event('state', 'waiting_input')
                elif workflow is None:
                    self.input_phase = 'error'
                    self.on_event('state', 'error')
                elif workflow.phase not in ('error', 'stopped'):
                    try:
                        workflow._failed(exc)
                    except Exception as report_error:
                        workflow.phase = 'error'
                        error += f'\n任务报告写入也失败：{report_error}'
                        self.on_event('state', 'error')
            finally:
                self.on_event('done', error)
        threading.Thread(target=worker, daemon=True).start()

    def resume(self):
        with self.lock:
            if self.busy:
                return
            self.error = None
            self.notice = None
            if self.workflow is None and self.input_phase == 'waiting_input':
                scan_only = self.pending_scan_only
            else:
                scan_only = None
        if scan_only is not None:
            self._begin(scan_only=scan_only)
        elif self.workflow:
            self._work(self.workflow.resume, auto=self.auto)

    def stop(self):
        self.input_cancel.set()
        with self.lock:
            if self.workflow:
                self.workflow.cancel.set()
            self.logs.append({'time': time.strftime('%H:%M:%S'), 'text': '正在停止；请等待当前安全检查结束。'})
        self._push()

    def select(self, source, checked):
        with self.lock:
            if self.busy or not self.workflow or self.workflow.phase != 'review':
                return
            row = self.rows.get(source)
            if row and not row['recycled']:
                (self.selected.add if checked else self.selected.discard)(source)
        self._push()

    def select_all(self, checked):
        with self.lock:
            if self.busy or not self.workflow or self.workflow.phase != 'review':
                return
            self.selected = {k for k, r in self.rows.items() if not r['recycled']} if checked else set()
        self._push()

    def cleanup(self):
        with self.lock:
            chosen = sorted(self.selected)
            if not chosen:
                raise ValueError('请先勾选要清理的 Word。')
            workflow = self.workflow
        self._work(lambda: workflow.cleanup(chosen), kind='cleanup')

    def open_output(self):
        if self.workflow and self.workflow.archive:
            os.startfile(str(self.workflow.archive.parent))

    def open_job_dir(self):
        if self.workflow and self.workflow.job_dir.exists():
            os.startfile(str(self.workflow.job_dir))
