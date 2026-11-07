"""Local integrations. No network access; no permanent-delete fallback."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import threading
import hashlib
import tempfile


class ServiceError(RuntimeError):
    pass


class Cancelled(ServiceError):
    pass


def run_process(args, *, cwd=None, cancel=None, timeout=300, tick=None):
    """Drain output through a file to avoid PIPE deadlocks; kill only our child."""
    import tempfile
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            [str(a) for a in args], cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        start = time.monotonic()
        try:
            while proc.poll() is None:
                if tick:
                    tick()
                if cancel and cancel.is_set():
                    raise Cancelled('已停止；已经完成的文件操作不会撤销。')
                if time.monotonic() - start > timeout:
                    raise ServiceError(f'操作超过 {timeout} 秒，已停止专用工作进程。请检查 WPS 是否有弹窗。')
                time.sleep(0.1)
            if tick:
                tick()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        output.seek(0)
        data = output.read()
        if proc.returncode:
            detail = data[-4000:].decode('utf-8', errors='replace')
            raise ServiceError(f'本机程序返回错误 {proc.returncode}：{detail}')
        return data


class GreenShield:
    """Same current-user named pipe and JSON protocol as LdDecryptMcp.cs."""
    PIPE = r'\\.\pipe\GreenShieldQuickApply.Mcp.v1'

    def call(self, action, paths=(), *, cancel=None, timeout=45, **options):
        import pywintypes
        import win32con
        import win32file
        import win32pipe
        payload = {'action': action, **options}
        if paths:
            payload['paths'] = [str(Path(p).absolute()) for p in paths]
        try:
            win32pipe.WaitNamedPipe(self.PIPE, 3000)
            handle = win32file.CreateFile(
                self.PIPE, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0, None, win32con.OPEN_EXISTING, 0, None,
            )
        except pywintypes.error as exc:
            raise ServiceError('无法连接绿盾常驻程序，请先启动 LdDecryptHotkey.exe。') from exc
        try:
            win32file.WriteFile(handle, (json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
            start = time.monotonic()
            data = bytearray()
            while b'\n' not in data:
                if cancel and cancel.is_set():
                    raise Cancelled('已停止等待；已提交的绿盾申请仍可能由后台继续处理。')
                if time.monotonic() - start > timeout:
                    raise ServiceError('绿盾接口响应超时，请检查后台状态。不要重复点击申请。')
                _, available, _ = win32pipe.PeekNamedPipe(handle, 0)
                if available:
                    _, chunk = win32file.ReadFile(handle, min(available, 65536))
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        raise ServiceError('绿盾接口响应过大。')
                else:
                    time.sleep(0.1)
            result = json.loads(data.split(b'\n', 1)[0].decode('utf-8-sig'))
            if not result.get('ok'):
                raise ServiceError(f"绿盾操作未完成：{result.get('error', result)}")
            return result
        finally:
            handle.Close()

    def status(self):
        return self.call('status')

    def check(self, paths, cancel=None):
        return self.call('check', paths, cancel=cancel)

    def request(self, paths, cancel=None):
        # Never force, never resubmit while waiting. Official UI may require approval.
        return self.call('request', paths, cancel=cancel, wait=False, force=False,
                         reason='Word 转 PDF 归档：对用户指定的处理目录和原始核对副本目录整体申请解密，完成后核对 PDF 与源文件。')

    def wait(self, paths, cancel=None):
        return self.call('wait', paths, cancel=cancel, timeout=45, timeoutSeconds=20)


def find_rar():
    candidates = [shutil.which('rar')]
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for access in (winreg.KEY_READ | winreg.KEY_WOW64_64KEY, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WinRAR.exe', 0, access) as key:
                        candidates.append(str(Path(winreg.QueryValue(key, None)).parent / 'Rar.exe'))
                except OSError:
                    pass
    for base in ('ProgramFiles', 'ProgramFiles(x86)'):
        candidates.append(str(Path(os.environ.get(base, 'C:/Program Files')) / 'WinRAR' / 'Rar.exe'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).absolute()
    raise ServiceError('没有找到 Rar.exe。请安装 WinRAR（7-Zip 无法创建 RAR），然后重新开始。')


class RarArchiver:
    def __init__(self, executable=None):
        self.executable = Path(executable) if executable else find_rar()

    def _run(self, args, *, cwd=None, cancel=None):
        # Ignore configuration, environment presets, passwords and recursive expansion.
        return run_process([self.executable, *args], cwd=cwd, cancel=cancel, timeout=1800)

    def create(self, root, pdfs, target, job_dir, cancel=None):
        root, target = Path(root), Path(target)
        if target.exists():
            raise ServiceError('目标 RAR 已存在，停止以避免覆盖。')
        listing = Path(job_dir) / 'pdf-files.txt'
        # UTF-16 list accepted by -scul, including Chinese names and spaces.
        names = [str(p.relative_to(root)) for p in pdfs]
        if any('\n' in n or '\r' in n for n in names):
            raise ServiceError('文件名含换行，无法安全打包。')
        listing.write_text('\n'.join(names) + '\n', encoding='utf-16')
        # -p- is for reading without a prompt. On creation it sets password '-'.
        # No password switch is allowed here: deliveries must open without a password.
        self._run(['a', '-cfg-', '-ma5', '-r-', '-m3', '-y', '-scul',
                   target, '@' + str(listing)], cwd=root, cancel=cancel)
        self.verify(root, pdfs, target, cancel)

    def verify(self, root, pdfs, target, cancel=None):
        target = Path(target)
        with target.open('rb') as stream:
            if stream.read(8) != b'Rar!\x1a\x07\x01\x00':
                raise ServiceError('交付文件不是有效的 RAR5 文件。')
        self._run(['t', '-cfg-', '-y', '-p-', target], cancel=cancel)
        raw = self._run(['lb', '-cfg-', '-p-', '-scur', target], cancel=cancel)
        listing = raw.decode('utf-16').lstrip('\ufeff')
        actual = [n.strip().replace('\\', '/').casefold() for n in listing.splitlines() if n.strip()]
        expected = [str(p.relative_to(root)).replace('\\', '/').casefold() for p in pdfs]
        if sorted(actual) != sorted(expected):
            raise ServiceError('RAR 内文件清单与全部 PDF 不一致，禁止清理 Word。')
        import rarfile
        with rarfile.RarFile(target) as metadata:
            if metadata.needs_password():
                raise ServiceError('交付 RAR 意外带有密码，禁止清理 Word。')
        # Verify archived plaintext, not just the archive's internal CRC.
        with tempfile.TemporaryDirectory(prefix='wpspdf-rar-check-') as directory:
            self._run(['x', '-cfg-', '-y', '-p-', target, str(Path(directory)) + os.sep], cancel=cancel)
            for pdf in pdfs:
                extracted = Path(directory) / pdf.relative_to(root)
                def digest(path):
                    value = hashlib.sha256()
                    with path.open('rb') as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                            value.update(chunk)
                    return value.digest()
                if not extracted.is_file() or digest(extracted) != digest(pdf):
                    raise ServiceError(f'RAR 内容与明文 PDF 不一致：{pdf.name}')


def recycle_file(path):
    """Send2Trash uses the Windows recycle operation; errors never fall back to unlink."""
    from send2trash import send2trash
    send2trash(str(Path(path).absolute()))
