"""Fail-closed PDF delivery workflow, independent of the desktop UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import threading
import time
import uuid

from local_services import (Cancelled, GreenShield, RarArchiver, ServiceError,
                            recycle_file, run_process)
from task_files import available_path, remove_owned_tree

WORD_SUFFIXES = {'.doc', '.docx', '.wps'}


class WorkflowError(ServiceError):
    pass


def is_link(path):
    if path.is_symlink():
        return True
    return os.name == 'nt' and bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def scan_folder(folder):
    root = Path(folder).absolute()
    if not root.is_dir():
        raise WorkflowError('请选择存在的文件夹。')
    # Do not silently skip directories, permission failures or junctions.
    for parent in (root, *root.parents):
        if is_link(parent):
            raise WorkflowError(f'目录包含链接或联接点，无法保证扫描完整：{parent}')
    if root == Path(root.anchor):
        raise WorkflowError('请选择具体业务文件夹，不要选择整个磁盘。')
    words, pdfs = [], []
    count = 0
    def onerror(error):
        raise WorkflowError(f'扫描失败，无法保证没有遗漏：{error}')
    for current, dirs, files in os.walk(root, onerror=onerror, followlinks=False):
        for name in dirs + files:
            if is_link(Path(current) / name):
                raise WorkflowError(f'发现链接或联接点，请先移出处理目录：{Path(current) / name}')
        for name in files:
            count += 1
            if count > 10000:
                raise WorkflowError('文件超过 10,000 个，请拆分文件夹后处理。')
            path = Path(current) / name
            if path.suffix.lower() in WORD_SUFFIXES and not name.startswith('~$'):
                words.append(path)
            elif path.suffix.lower() == '.pdf':
                pdfs.append(path)
    by_target = {}
    for word in words:
        key = str(word.with_suffix('.pdf')).casefold()
        if key in by_target:
            raise WorkflowError(f'同名源文档会覆盖同一 PDF：{by_target[key]} 和 {word}。请先改名。')
        by_target[key] = word
    return sorted(words), sorted(pdfs)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_pdf(path, expected_pages=None):
    from pypdf import PdfReader
    path = Path(path)
    try:
        with path.open('rb') as stream:
            if stream.read(5) != b'%PDF-':
                raise WorkflowError('不是明文 PDF（可能仍被绿盾加密）')
            stream.seek(max(0, path.stat().st_size - 2048))
            if b'%%EOF' not in stream.read():
                raise WorkflowError('PDF 尾部不完整')
            stream.seek(0)
            reader = PdfReader(stream)
            if reader.is_encrypted and not reader.decrypt(''):
                raise WorkflowError('PDF 打开需要密码')
            pages = len(reader.pages)
            if pages < 1:
                raise WorkflowError('PDF 没有页面')
            for page in reader.pages:
                contents = page.get_contents()
                if contents is not None:
                    contents.get_data()
            if expected_pages is not None and pages != expected_pages:
                raise WorkflowError(f'页数不一致：Word {expected_pages} 页 / PDF {pages} 页')
            return pages
    except Exception as exc:
        raise WorkflowError(f'PDF 校验失败 [{path.name}]：{exc}') from exc


@dataclass
class Document:
    source: str
    pdf: str
    source_hash: str
    snapshot: str = ''
    pages: int = 0
    pdf_hash: str = ''
    status: str = '待转换'
    recycled: bool = False
    exported: bool = False


class WpsConverter:
    def convert_many(self, jobs, job_dir, cancel, on_result):
        if not jobs:
            return
        job_dir = Path(job_dir)
        manifest = job_dir / 'batch-manifest.json'
        manifest.write_text(json.dumps({'jobs': jobs}, ensure_ascii=False), encoding='utf-8')
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--wps-batch-worker', manifest]
        else:
            command = [sys.executable, Path(__file__).with_name('wps_worker.py'), '--batch', manifest]
        completed = 0
        current_index = -1
        last_progress = time.monotonic()
        def tick():
            nonlocal completed, current_index, last_progress
            status_file = job_dir / 'batch-status.json'
            if status_file.exists():
                try:
                    status = json.loads(status_file.read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    status = {'index': current_index}
                if status['index'] != current_index:
                    current_index = status['index']
                    last_progress = time.monotonic()
            if time.monotonic() - last_progress > 180:
                raise WorkflowError('当前文档超过 180 秒没有完成，已停止专用工作进程；请检查 WPS 弹窗。')
            while completed < len(jobs):
                result = job_dir / f'batch-result-{completed + 1}.json'
                if not result.exists():
                    break
                pages = int(json.loads(result.read_text(encoding='utf-8'))['pages'])
                if pages < 1:
                    raise WorkflowError('WPS 未返回有效页数。')
                completed += 1
                on_result(completed, pages)
        try:
            run_process(command, cancel=cancel, timeout=180 * (len(jobs) + 1), tick=tick)
        except ServiceError as exc:
            error_file = job_dir / 'batch-error.json'
            if error_file.exists():
                raise WorkflowError(json.loads(error_file.read_text(encoding='utf-8'))['error']) from exc
            raise
        if completed != len(jobs):
            raise WorkflowError('WPS 批量结果有遗漏，禁止进入后续步骤。')

    def convert(self, source, output, job_dir, cancel):
        result = Path(job_dir) / f'worker-{uuid.uuid4().hex}.json'
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--wps-worker']
        else:
            command = [sys.executable, Path(__file__).with_name('wps_worker.py')]
        try:
            run_process([*command, source, output, result], cancel=cancel, timeout=180)
        except ServiceError as exc:
            if result.exists():
                error = json.loads(result.read_text(encoding='utf-8')).get('error')
                if error:
                    raise WorkflowError(error) from exc
            raise
        try:
            pages = int(json.loads(result.read_text(encoding='utf-8'))['pages'])
        except Exception as exc:
            raise WorkflowError('WPS 未返回有效的转换结果，禁止继续。') from exc
        if pages < 1 or not Path(output).is_file() or Path(output).stat().st_size == 0:
            raise WorkflowError('WPS 转换结果为空，禁止继续。')
        return pages


class Workflow:
    def __init__(self, root, *, emit=None, converter=None, shield=None,
                 archiver=None, recycler=None, data_dir=None, overwrite=False, delivery_name=None,
                 prepared_input=None):
        self.root = Path(root).absolute()
        self.delivery_name = delivery_name or self.root.name
        if Path(self.delivery_name).name != self.delivery_name or any(c in self.delivery_name for c in '/\\:'):
            raise WorkflowError('交付名称不能包含路径。')
        self.emit = emit or (lambda *args: None)
        self.converter = converter or WpsConverter()
        self.shield = shield or GreenShield()
        self.archiver = archiver
        self.recycler = recycler or recycle_file
        self.overwrite = overwrite
        self.cancel = threading.Event()
        self.phase = 'new'
        self.documents = []
        self.pdfs = []
        self.pdf_hashes = {}
        self.archive = None
        self.archive_hash = ''
        self.pending_requested = False
        self.wait_context = 'prepare'
        self.sources_verified = False
        self.error = ''
        self.prepared_input = prepared_input
        self.intermediates_cleaned = False
        self.storage_removed = False
        self.cleanup_warning = ''
        self.id = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        base = Path(data_dir) if data_dir else Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'WPSPDFWorkflow' / 'jobs'
        # Resolve Windows packaged-app AppData redirection before atomic renames.
        # A virtual C: path may actually live on D: in the Codex desktop container.
        base.mkdir(parents=True, exist_ok=True)
        base = base.resolve()
        self.jobs_parent = base
        self.job_dir = base / self.id
        if self.job_dir.absolute().is_relative_to(self.root):
            raise WorkflowError('任务记录目录不能在待处理目录内部，请选择具体业务子文件夹。')
        self.job_dir.mkdir(parents=True, exist_ok=False)
        self.job_dir = self.job_dir.resolve(strict=True)
        self.jobs_parent = self.job_dir.parent

    def log(self, message):
        self.emit('log', message)
        if self.storage_removed:
            return
        with (self.job_dir / 'run.log').open('a', encoding='utf-8') as stream:
            stream.write(f'{datetime.now():%H:%M:%S} {message}\n')

    def save(self):
        if self.storage_removed:
            return
        data = {'root': str(self.root), 'phase': self.phase, 'error': self.error,
                'documents': [asdict(d) for d in self.documents],
                'pdfs': [str(p) for p in self.pdfs], 'pdf_hashes': self.pdf_hashes,
                'archive': str(self.archive) if self.archive else None,
                'archive_hash': self.archive_hash, 'pending_requested': self.pending_requested,
                'sources_verified': self.sources_verified, 'wait_context': self.wait_context}
        temp = self.job_dir / 'report.tmp'
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.job_dir / 'report.json')

    def update(self, phase, message):
        self.phase = phase
        self.log(message)
        self.save()
        self.emit('state', phase)
        self.emit('files', [asdict(d) for d in self.documents])

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled('已停止；Word 不会进入后续清理。')

    def scan(self):
        words, self.pdfs = scan_folder(self.root)
        # Map existing names case-insensitively, including .PDF on Windows.
        existing = {str(p).casefold(): p for p in self.pdfs}
        self.documents = [Document(str(p), str(existing.get(str(p.with_suffix('.pdf')).casefold(), p.with_suffix('.pdf'))), file_hash(p)) for p in words]
        snapshots = self.job_dir / '源文档核对副本'
        snapshots.mkdir()
        for index, document in enumerate(self.documents):
            self.check_cancel()
            source = Path(document.source)
            snapshot = snapshots / f'{index}{source.suffix}'
            shutil.copy2(source, snapshot)
            if file_hash(snapshot) != document.source_hash:
                raise WorkflowError(f'扫描期间源文档发生变化：{source.name}')
            document.snapshot = str(snapshot)
        if not words and not self.pdfs:
            raise WorkflowError('没有找到 Word/WPS 文档或 PDF。')
        self.update('scanned', f'扫描完成：{len(words)} 份 Word/WPS，已有 {len(self.pdfs)} 份 PDF。')

    def _source_unchanged(self, document):
        source = Path(document.source)
        if is_link(source) or file_hash(source) != document.source_hash:
            raise WorkflowError(f'源文档已变化，必须重新转换：{source.name}')

    def _inventory_unchanged(self, *, check_sources=True):
        words, pdfs = scan_folder(self.root)
        expected = {Path(d.source) for d in self.documents if not d.recycled}
        if set(words) != expected or set(pdfs) != set(self.pdfs):
            raise WorkflowError('目录中文件有新增或缺失，请重新扫描，禁止清理。')
        for d in self.documents:
            if check_sources and not d.recycled:
                self._source_unchanged(d)

    def prepare(self):
        try:
            if self.phase == 'new':
                self.scan()
            if self.phase != 'scanned':
                raise WorkflowError('当前任务不能重复转换，请重新开始。')
            self.check_cancel()
            self._inventory_unchanged()
            if self.shield.status().get('status') != 'ready':
                raise WorkflowError('绿盾常驻服务或解密策略未就绪，未启动转换。')
            if self.archiver is None:
                self.archiver = RarArchiver()
            collisions = [d for d in self.documents if Path(d.pdf).exists()]
            if collisions and not self.overwrite:
                raise WorkflowError(f'已有 {len(collisions)} 份同名 PDF，不能直接作为本次转换结果。请先备份移走，或勾选“备份旧 PDF 后重新转换”。')
            self.update('converting', '在后台使用一个 WPS 会话连续导出；单份卡住 180 秒时停止。')
            exports = self.job_dir / 'exports'
            exports.mkdir()
            jobs = [{'source': d.source, 'output': str(exports / f'{index}.pdf')}
                    for index, d in enumerate(self.documents, 1)]
            def publish(index, pages):
                d = self.documents[index - 1]
                self.check_cancel()
                # Export outside the source tree: failures leave no partial delivery PDF.
                output = exports / f'{index}.pdf'
                if not output.is_file() or output.stat().st_size == 0:
                    raise WorkflowError(f'WPS 没有生成非空 PDF：{Path(d.source).name}')
                target = Path(d.pdf)
                if target.exists():
                    if not self.overwrite:
                        raise WorkflowError(f'转换期间出现同名 PDF：{target.name}')
                    backup = self.job_dir / '旧PDF备份' / target.relative_to(self.root)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                # copy first, atomically publish on the target volume (may differ from AppData).
                temporary = target.with_name(f'.wpspdf-{self.id}-{index}.tmp')
                try:
                    shutil.copy2(output, temporary)
                    os.replace(temporary, target)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                d.status = '已导出，待解密校验'
                d.exported = True
                d.pages = pages
                self.log(f'[{index}/{len(self.documents)}] 已导出：{Path(d.source).relative_to(self.root)}')
                self.save()
                self.emit('files', [asdict(item) for item in self.documents])
                self.emit('progress', (index, len(self.documents)))
            if hasattr(self.converter, 'convert_many'):
                self.converter.convert_many(jobs, self.job_dir, self.cancel, publish)
            else:
                # Lightweight injected converters used by deterministic tests.
                for index, job in enumerate(jobs, 1):
                    pages = self.converter.convert(Path(job['source']), Path(job['output']), self.job_dir, self.cancel)
                    publish(index, pages)
            words, self.pdfs = scan_folder(self.root)
            if set(words) != {Path(d.source) for d in self.documents}:
                raise WorkflowError('转换期间 Word 清单发生变化，请重新扫描。')
            self._inventory_unchanged(check_sources=False)
            self.update('decrypting', f'检查全部 {len(self.pdfs)} 份 PDF，以及源 Word 和核对副本的加密状态。')
            return self._decrypt_and_finish()
        except Exception as exc:
            self._failed(exc)
            raise

    def _decryption_scope(self, include_snapshots=False):
        targets = [self.root]
        if include_snapshots and self.documents:
            targets.append(self.job_dir / '源文档核对副本')
        count = 0
        def scan_error(error):
            raise WorkflowError(f'不能完整扫描解密目录：{error}')
        for target in targets:
            for directory, children, files in os.walk(target, onerror=scan_error, followlinks=False):
                for name in children + files:
                    if is_link(Path(directory) / name):
                        raise WorkflowError('解密目录包含链接，停止以避免处理无关路径。')
                count += len(files)
        return targets, count

    def _ensure_decrypted(self, targets, context, expected_count=None):
        self.check_cancel()
        self.wait_context = context
        if expected_count is None:
            expected_count = len(targets)
        if expected_count > 10000:
            raise WorkflowError('PDF 与核对副本合计超过绿盾 10,000 文件上限，请拆分任务。')
        result = self.shield.check(targets, self.cancel)
        status = result.get('status')
        self.log(f'绿盾检测：扫描 {result.get("scannedFiles", expected_count)} 个文件，'
                 f'仍加密 {result.get("encryptedRemaining", 0)} 个，'
                 f'不可读 {result.get("unreadableFiles", 0)} 个。')
        if 'scannedFiles' in result and result['scannedFiles'] != expected_count:
            raise WorkflowError('绿盾扫描数量与目标文件数量不同，不能确认全部完成。')
        if result.get('warnings') or result.get('unreadableFiles', 0):
            raise WorkflowError(f'绿盾扫描有遗漏或不可读文件：{result}')
        if status == 'encrypted':
            self.update('decrypting', '发现受保护文件，等待官方解密后再比较原先验证的内容。')
            if not self.pending_requested:
                # Record before submission: ambiguous pipe errors must not trigger a retry.
                self.pending_requested = True
                self.save()
                self.log(f'正在按文件夹提交一次官方解密申请（{len(targets)} 个目录），不逐文件申请；如出现审批窗口，请按公司流程处理。')
                result = self.shield.request(targets, self.cancel)
                self.log(f"解密申请返回：{result.get('status')}")
            self.update('waiting', '等待解密批准；不会重复提交申请。')
            for _ in range(6):
                self.check_cancel()
                result = self.shield.wait(targets, self.cancel)
                status = result.get('status')
                if status in ('decrypted', 'already_decrypted'):
                    break
                if status != 'pending':
                    raise WorkflowError(f'解密未完成：{result}')
                self.log(f"仍待审批：剩余 {result.get('encryptedRemaining', '?')} 份。")
            else:
                self.update('waiting', '两分钟内未获解密确认，已暂停。批准后点击“继续等待解密”。')
                return False
        elif status not in ('decrypted', 'already_decrypted'):
            raise WorkflowError(f'无法确认解密状态：{result}')
        self.check_cancel()
        final = self.shield.check(targets, self.cancel)
        if final.get('status') not in ('decrypted', 'already_decrypted') or final.get('warnings') or final.get('unreadableFiles', 0):
            raise WorkflowError(f'仍有加密、不可读或未确认的 PDF：{final}')
        if 'scannedFiles' in final and final['scannedFiles'] != expected_count:
            raise WorkflowError('绿盾复核数量不一致，不能确认全部完成。')
        self.log(f'绿盾复核通过：{expected_count} 个文件均可读，已知加密头已消失。继续进行内容校验。')
        self.pending_requested = False
        self.save()
        return True

    def _decrypt_and_finish(self):
        targets, count = self._decryption_scope(include_snapshots=True)
        if not self._ensure_decrypted(targets, 'prepare', count):
            return False
        for document in self.documents:
            self.check_cancel()
            source_hash = file_hash(document.source)
            if source_hash != file_hash(document.snapshot):
                raise WorkflowError(f'源 Word 与转换前副本内容不同，必须重新检查：{Path(document.source).name}')
            document.source_hash = source_hash
        self.sources_verified = True
        self._inventory_unchanged()
        self.update('verifying', '解密完成，逐页解析 PDF 内容流，并核对新导出的 PDF 与源 Word 的页数。')
        generated = {Path(d.pdf): d for d in self.documents}
        for index, pdf in enumerate(self.pdfs, 1):
            self.check_cancel()
            document = generated.get(pdf)
            pages = validate_pdf(pdf, document.pages if document is not None else None)
            digest = file_hash(pdf)
            self.pdf_hashes[str(pdf)] = digest
            if document is not None:
                document.pdf_hash = digest
                document.status = f'校验通过 · {pages} 页'
            self.log(f'[{index}/{len(self.pdfs)}] PDF 校验通过：{pdf.relative_to(self.root)}（{pages} 页）')
        self._inventory_unchanged()
        self.update('archiving', 'PDF 全部通过，开始创建 RAR 并检查完整性、清单和内容。')
        # Keep transient archives inside the private task directory; visible
        # deliveries contain no timestamps or random task identifiers.
        partial = self.job_dir / '待验证.rar'
        self.archiver.create(self.root, self.pdfs, partial, self.job_dir, self.cancel)
        self._verify_inputs()
        self.check_cancel()
        # AppData and the input can be on different volumes: first copy beside
        # the delivery, then publish without overwriting any existing result.
        pending = available_path(self.root.parent, f'{self.delivery_name}-PDF-生成中', '.rar')
        with partial.open('rb') as reader, pending.open('xb') as writer:
            shutil.copyfileobj(reader, writer)
        if file_hash(pending) != file_hash(partial):
            raise WorkflowError('压缩包发布不完整，禁止清理。')
        while True:
            final_archive = available_path(self.root.parent, f'{self.delivery_name}-PDF', '.rar')
            try:
                pending.rename(final_archive)
                break
            except FileExistsError:
                continue
        self.archive = final_archive
        self.archive_hash = file_hash(self.archive)
        self.update('review', 'RAR 已验证，交付物就绪。请抽查 PDF 后全选并清理 Word。')
        if not self.documents:
            self.update('complete', 'PDF 归档完成，本次没有需要清理的 Word。')
            self._cleanup_intermediates()
        return True

    def resume(self):
        if self.phase != 'waiting':
            raise WorkflowError('当前没有等待中的解密任务。')
        self.cancel.clear()
        try:
            self._inventory_unchanged(check_sources=False)
            if self.wait_context == 'cleanup':
                targets, count = self._decryption_scope()
                if not self._ensure_decrypted(targets, 'cleanup', count):
                    return False
                self._verify_inputs()
                if not self.archive or file_hash(self.archive) != self.archive_hash:
                    raise WorkflowError('RAR 已缺失或变化，禁止清理。')
                self.archiver.verify(self.root, self.pdfs, self.archive, self.cancel)
                self.update('review', '抽查后的解密和内容复核通过，请再次确认清理选中的 Word。')
                return True
            return self._decrypt_and_finish()
        except Exception as exc:
            self._failed(exc)
            raise

    def _verify_inputs(self):
        self.check_cancel()
        self._inventory_unchanged()
        for pdf in self.pdfs:
            if file_hash(pdf) != self.pdf_hashes.get(str(pdf)):
                raise WorkflowError(f'PDF 在校验后发生变化，禁止清理：{pdf.name}')

    def cleanup(self, selected):
        if self.phase != 'review':
            raise WorkflowError('只有全部检查和 RAR 打包通过后才能清理。')
        candidates = {d.source: d for d in self.documents if not d.recycled}
        selected = list(dict.fromkeys(str(p) for p in selected))
        if not selected or any(p not in candidates for p in selected):
            raise WorkflowError('请选择本次任务中校验通过的 Word。')
        try:
            self._inventory_unchanged(check_sources=False)
            targets, count = self._decryption_scope()
            # Viewing the PDF during manual review can transparently re-encrypt it.
            # Official decryption must restore the SAME verified bytes, not a new baseline.
            if not self._ensure_decrypted(targets, 'cleanup', count):
                return False
            self._verify_inputs()
            if not self.archive or file_hash(self.archive) != self.archive_hash:
                raise WorkflowError('RAR 已缺失或变化，禁止清理。')
            self.archiver.verify(self.root, self.pdfs, self.archive, self.cancel)
            self._verify_inputs()
            self.update('cleaning', f'重新检查全部通过，准备将 {len(selected)} 份 Word 移入回收站。')
            for source in selected:
                self.check_cancel()
                d = candidates[source]
                # Re-check immediately before EACH recycle, including remaining inventory.
                self._verify_inputs()
                if file_hash(self.archive) != self.archive_hash:
                    raise WorkflowError('清理期间 RAR 发生变化，已停止。')
                self.recycler(Path(source))
                if Path(source).exists():
                    raise WorkflowError(f'回收站操作后文件仍存在：{source}')
                d.recycled = True
                d.status = '已移入回收站'
                self.log(f'已移入回收站：{Path(source).relative_to(self.root)}')
                self.save()
                self.emit('files', [asdict(item) for item in self.documents])
            remaining = sum(not d.recycled for d in self.documents)
            self.update('review' if remaining else 'complete',
                        f'本次清理 {len(selected)} 份，保留 {remaining} 份；RAR：{self.archive}')
            if not remaining:
                self._cleanup_intermediates()
        except Exception as exc:
            self._failed(exc)
            raise

    def _cleanup_intermediates(self):
        """Called only after the original workflow has fully completed."""
        if self.phase != 'complete':
            raise WorkflowError('任务未完成，不能清理工作文件。')
        if self.intermediates_cleaned:
            return
        try:
            if not self.archive or file_hash(self.archive) != self.archive_hash:
                raise WorkflowError('最终压缩包缺失或变化，未清理中间文件。')
            if self.prepared_input:
                self.prepared_input.cleanup_success(self.pdfs)
            # Never remove original folders, even when all their Word files
            # have been recycled. Only the task's newly-created data directory.
            remove_owned_tree(self.job_dir, parent=self.jobs_parent)
            self.storage_removed = True
            self.intermediates_cleaned = True
            self.log('任务完成，中间文件已自动清理。原始输入和最终 RAR 已保留。')
        except Exception as exc:
            self.cleanup_warning = str(exc)
            # rmtree can fail after removing some files. Do not recreate the
            # working directory, or turn an intact delivery into a failed task.
            self.emit('log', f'交付已完成，但部分中间文件暂未清理：{exc}')
            self.emit('cleanup_warning', self.cleanup_warning)

    def _failed(self, exc):
        self.error = str(exc)
        if self.pending_requested and self.phase in ('waiting', 'decrypting'):
            self.update('waiting', f'解密等待已暂停：{exc}。可点击“继续等待解密”，不会重复申请。')
            return
        self.update('stopped' if isinstance(exc, Cancelled) else 'error',
                    f'流程停止：{exc}。已生成文件和已完成清理的记录保留在任务报告中。')
