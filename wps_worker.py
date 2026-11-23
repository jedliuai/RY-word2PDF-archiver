"""Run inside a dedicated Python process; never attach to the user's active WPS."""
import json
import sys
import time
from pathlib import Path


def run_jobs(jobs, on_result, on_status=lambda *args: None):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    app = doc = None
    owned = False
    try:
        for progid in ('KWPS.Application', 'Wps.Application'):
            try:
                app = win32com.client.DispatchEx(progid)
                break
            except Exception:
                pass
        if app is None:
            raise RuntimeError('无法创建 WPS COM 会话，请确认安装并激活桌面版 WPS。')
        if app.Documents.Count:
            raise RuntimeError('WPS 返回的会话里已有其他文档。为保护你的工作，请保存并关闭 WPS 后重试。')
        owned = True
        app.Visible = False
        app.DisplayAlerts = 0
        for index, job in enumerate(jobs, 1):
            source, output = job['source'], job['output']
            on_status(index, source)
            doc = app.Documents.Open(str(source), ReadOnly=True, AddToRecentFiles=False)
            doc.Repaginate()
            pages = int(doc.ComputeStatistics(2))  # wdStatisticPages
            if pages < 1:
                raise RuntimeError('无法取得源文档页数，停止以防误删。')
            doc.SaveAs2(str(output), FileFormat=17)
            doc.Close(SaveChanges=False)
            doc = None
            if not Path(output).is_file() or Path(output).stat().st_size == 0:
                raise RuntimeError('WPS 没有生成非空 PDF。')
            on_result(index, pages)
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=False)
            except Exception:
                pass
        if app is not None and owned:
            try:
                if app.Documents.Count == 0:
                    app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def convert(source, output, result):
    run_jobs([{'source': str(source), 'output': str(output)}],
             lambda index, pages: Path(result).write_text(json.dumps({'pages': pages}), encoding='utf-8'))


def batch_main(manifest_path):
    manifest = Path(manifest_path)
    data = json.loads(manifest.read_text(encoding='utf-8'))
    directory = manifest.parent
    def write(name, payload):
        target = directory / name
        temporary = directory / (name + '.pending')
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        for attempt in range(50):
            try:
                temporary.replace(target)
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.02)
    try:
        write('batch-status.json', {'index': 0, 'source': '启动 WPS'})
        run_jobs(data['jobs'],
                 lambda index, pages: write(f'batch-result-{index}.json', {'pages': pages}),
                 lambda index, source: write('batch-status.json', {'index': index, 'source': source}))
        write('batch-status.json', {'index': len(data['jobs']) + 1, 'complete': True})
        return 0
    except Exception as exc:
        write('batch-error.json', {'error': str(exc)})
        return 1


if __name__ == '__main__':
    if sys.argv[1] == '--batch':
        sys.exit(batch_main(sys.argv[2]))
    convert(*sys.argv[1:4])
