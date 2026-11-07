"""Folder/ZIP/RAR intake. Extract members as streams into a fresh directory."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import ntpath
import os
from pathlib import Path
import shutil
import stat
import sys
import threading
import uuid
import zipfile

from local_services import Cancelled, GreenShield, ServiceError, find_rar, run_process
from pdf_workflow import file_hash, is_link
from task_files import available_path, remove_owned_file, remove_owned_tree

MAX_ENTRIES = 10000
MAX_UNPACKED_BYTES = 20 * 1024 ** 3


class InputError(ServiceError):
    pass


class ArchivePending(InputError):
    pass


@dataclass
class PreparedInput:
    source: Path
    folder: Path
    is_archive: bool
    source_hash: str = ''
    file_count: int = 0
    report: str = ''
    owned_parent: str = ''
    report_parent: str = ''
    extracted_files: list[str] = field(default_factory=list)

    def verify_source(self):
        if self.is_archive and file_hash(self.source) != self.source_hash:
            raise InputError('原压缩包在扫描后发生变化，请重新扫描。')

    def cleanup_success(self, pdfs=()):
        if not self.is_archive:
            return
        self.verify_source()
        if not self.owned_parent or self.folder.parent.resolve() != Path(self.owned_parent).resolve():
            raise InputError('无法确认解压目录属于本次任务，未执行清理。')
        if self.folder.resolve() == self.source.parent.resolve() or self.source.resolve().is_relative_to(self.folder.resolve()):
            raise InputError('不能把原压缩包或所在目录当作中间文件清理。')
        if self.folder.exists():
            allowed = set(self.extracted_files) | {str(Path(p).relative_to(self.folder)) for p in pdfs}
            actual = {str(p.relative_to(self.folder)) for p in self.folder.rglob('*') if p.is_file()}
            if actual - allowed:
                raise InputError('解压目录新增了本次任务以外的文件，已保留该目录。')
            remove_owned_tree(self.folder, parent=self.owned_parent)
        if self.report:
            remove_owned_file(self.report, parent=self.report_parent)


def member_path(name):
    """Validate Windows paths even if tests run on a different platform."""
    name = name.replace('\\', '/')
    if not name or name.startswith('/') or ntpath.splitdrive(name)[0]:
        raise InputError(f'压缩包成员路径无效：{name!r}')
    parts = name.rstrip('/').split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    for part in parts:
        if (not part or part in ('.', '..') or part.endswith((' ', '.')) or
                any(ord(c) < 32 or c in '<>:"|?*' for c in part) or
                part.split('.')[0].upper() in reserved):
            raise InputError(f'压缩包成员路径不适用于 Windows：{name!r}')
    return Path(*parts)


def _validate_members(items, destination):
    if not items:
        raise InputError('压缩包为空。')
    if len(items) > MAX_ENTRIES:
        raise InputError('压缩包条目超过 10,000 个，请拆分后处理。')
    total = 0
    entries = {}
    validated = []
    for info, name, directory, size, link, password in items:
        relative = member_path(name)
        if link:
            raise InputError(f'压缩包含链接或特殊文件，停止解压：{name}')
        if password:
            raise InputError('压缩包本身有密码。请先手动解压，再拖入解压后的文件夹。')
        if size < 0:
            raise InputError(f'压缩包成员大小无效：{name}')
        key = str(relative).casefold()
        if key in entries:
            raise InputError(f'压缩包有重名条目，不能保证不覆盖：{name}')
        entries[key] = directory
        total += size
        validated.append((info, relative, directory, size))
    for _, relative, _, _ in validated:
        for parent in relative.parents:
            if str(parent).casefold() in entries and not entries[str(parent).casefold()]:
                raise InputError(f'压缩包中文件与文件夹同名：{relative}')
    if total > MAX_UNPACKED_BYTES:
        raise InputError('解压后超过 20 GB，请拆分后处理。')
    if shutil.disk_usage(destination).free < total + 64 * 1024 ** 2:
        raise InputError('剩余磁盘空间不足，尚未解压。')
    return validated


def _zip_name(info):
    # Legacy Windows ZIPs often store GBK names without the UTF-8 flag.
    if not info.flag_bits & 0x800:
        try:
            return info.filename.encode('cp437').decode('gb18030')
        except (UnicodeError, LookupError):
            pass
    return info.filename


def extract_archive(source, destination, report):
    """Worker entry: CRC-check every stream; never give filenames to an extractor."""
    source, destination = Path(source), Path(destination).resolve()
    if not destination.is_dir() or any(destination.iterdir()):
        raise InputError('解压目标必须是新建的空文件夹。')
    if source.suffix.lower() == '.zip':
        archive = zipfile.ZipFile(source)
        infos = archive.infolist()
        items = []
        for info in infos:
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            link = kind not in (0, stat.S_IFREG, stat.S_IFDIR)
            if info.create_system == 0 and info.external_attr & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                link = True
            items.append((info, _zip_name(info), info.is_dir(), info.file_size, link, bool(info.flag_bits & 1)))
    elif source.suffix.lower() == '.rar':
        import rarfile
        # Use the installed WinRAR backend, with no PATH dependency or shell.
        backend = find_rar().with_name('UnRAR.exe')
        rarfile.UNRAR_TOOL = str(backend if backend.is_file() else find_rar())
        rarfile.tool_setup(unrar=True, unar=False, bsdtar=False, sevenzip=False, sevenzip2=False, force=True)
        archive = rarfile.RarFile(source)
        if len(archive.volumelist()) > 1:
            archive.close()
            raise InputError('暂不处理分卷压缩包，请先完整解压后拖入文件夹。')
        items = [(i, i.filename, i.is_dir(), i.file_size,
                  i.is_symlink() or bool(i.file_redir) or (not i.is_file() and not i.is_dir()),
                  i.needs_password()) for i in archive.infolist()]
    else:
        raise InputError('只支持文件夹、ZIP 和 RAR。')
    with archive:
        validated = _validate_members(items, destination)
        records = []
        for info, relative, directory, expected_size in validated:
            target = destination / relative
            if not target.resolve().is_relative_to(destination):
                raise InputError('压缩包成员超出解压目录。')
            if directory:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            # Python owns the destination; the backend can only return file bytes.
            with archive.open(info) as reader, target.open('xb') as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b''):
                    written += len(chunk)
                    if written > expected_size:
                        raise InputError(f'实际解压大小超过压缩包记录：{relative}')
                    writer.write(chunk)
            if written != expected_size:
                raise InputError(f'解压文件不完整：{relative}')
            records.append({'path': str(relative), 'bytes': written})
    if not records:
        raise InputError('压缩包里没有文件。')
    Path(report).write_text(json.dumps({'files': records, 'file_count': len(records)}, ensure_ascii=False, indent=2), encoding='utf-8')


class ArchiveImporter:
    def __init__(self, shield=None):
        self.shield = shield or GreenShield()
        self.pending = set()

    def _decrypt_archive(self, source, cancel, emit):
        with source.open('rb') as stream:
            header = stream.read(8)
        encrypted = len(header) == 8 and header[:3] == b'\x87\x7d\x1c' and header[4:] == b'\x90\x10\x29\x01'
        if not encrypted:
            return
        if self.shield.status().get('status') != 'ready':
            raise InputError('压缩包被绿盾保护，但绿盾常驻服务尚未就绪。')
        result = self.shield.check([source], cancel)
        if result.get('status') == 'encrypted':
            key = str(source)
            if key not in self.pending:
                self.pending.add(key)
                emit('log', '压缩包本身受绿盾保护，正在申请官方解密。')
                self.shield.request([source], cancel)
            for _ in range(6):
                if cancel.is_set():
                    raise Cancelled('已停止等待压缩包解密。已提交的申请不会撤销。')
                result = self.shield.wait([source], cancel)
                if result.get('status') in ('decrypted', 'already_decrypted'):
                    break
                if result.get('status') != 'pending':
                    raise InputError(f'压缩包解密未确认：{result}')
            else:
                raise ArchivePending('压缩包尚待绿盾解密批准。批准后点击“继续等待解密”。')
        final = self.shield.check([source], cancel)
        if final.get('status') not in ('decrypted', 'already_decrypted') or final.get('warnings') or final.get('unreadableFiles', 0):
            raise InputError(f'无法确认压缩包已解密：{final}')
        self.pending.discard(str(source))

    def prepare(self, value, *, cancel=None, emit=None):
        cancel = cancel if cancel is not None else threading.Event()
        emit = emit or (lambda *args: None)
        source = Path(os.path.abspath(value))
        if not source.exists():
            raise InputError('输入路径不存在，请拖入文件夹、ZIP 或 RAR。')
        for parent in (source, *source.parents):
            if is_link(parent):
                raise InputError('输入路径不能包含链接或联接点。')
        if source.is_dir():
            return PreparedInput(source, source, False)
        if source.suffix.lower() not in ('.zip', '.rar'):
            raise InputError('只支持文件夹、ZIP 和 RAR，请勿拖入单个 Word 或 PDF。')
        if cancel.is_set():
            raise Cancelled('已停止导入。')
        emit('state', 'extracting')
        self._decrypt_archive(source, cancel, emit)
        original_hash = file_hash(source)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        parent = source.parent.resolve()
        # A long ZIP stem plus its own top-level folder can exceed Green Shield's
        # 512-byte path limit. Keep the new wrapper short; retain identity in input.json.
        while True:
            destination = available_path(parent, '解压中')
            try:
                destination.mkdir(exist_ok=False)
                break
            except FileExistsError:
                continue
        report_dir = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'WPSPDFWorkflow' / 'imports'
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir.resolve() / f'{stamp}.json'
        emit('log', f'开始解压：{source.name}。原压缩包保留，不覆盖已有目录。')
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--archive-worker']
        else:
            command = [sys.executable, Path(__file__).absolute()]
        try:
            run_process([*command, source, destination, report], cancel=cancel, timeout=1800)
            data = json.loads(report.read_text(encoding='utf-8'))
            # Windows app-container overlays can resolve the directory to C:
            # before creation but the actual new file to D:. Capture ownership
            # from the existing file, not the earlier virtual parent path.
            report = report.resolve(strict=True)
            if cancel.is_set():
                raise Cancelled('已停止导入。')
            if file_hash(source) != original_hash:
                raise InputError('解压期间原压缩包发生变化，停止处理。')
            while True:
                complete = available_path(parent, '解压文件')
                try:
                    destination.rename(complete)
                    break
                except FileExistsError:
                    continue
        except Exception as exc:
            if isinstance(exc, Cancelled):
                raise
            detail = str(exc)
            if report.exists():
                detail = json.loads(report.read_text(encoding='utf-8')).get('error', detail)
            raise InputError(f'解压未完成，后续步骤未执行：{detail}\n未完成目录：{destination}') from exc
        emit('log', f'解压完成：{data["file_count"]} 个文件。处理目录：{complete}')
        return PreparedInput(source, complete, True, original_hash, data['file_count'], str(report),
                             str(parent), str(report.parent), [f['path'] for f in data['files']])


def worker_main(arguments):
    try:
        extract_archive(*arguments)
    except Exception as exc:
        Path(arguments[2]).write_text(json.dumps({'error': str(exc)}, ensure_ascii=False), encoding='utf-8')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(worker_main(sys.argv[1:4]))
