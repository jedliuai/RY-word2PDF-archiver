"""Exercise TkDND registration, Tcl path parsing and archive-to-scan integration."""
from pathlib import Path
import sys
import tempfile
import types
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tkinterdnd2 import COPY, REFUSE_DROP, TkinterDnD
from desktop_app import WorkflowApp
from unittest.mock import patch

with tempfile.TemporaryDirectory(prefix='wpspdf-drop-') as directory:
    archive = Path(directory) / '中文 含空格 {测试}.zip'
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('客户 A/合同.docx', b'test source, no conversion')
        stream.writestr('客户 A/附件.pdf', b'test pdf, no validation')
    root = TkinterDnD.Tk()
    app = WorkflowApp(root)
    failures = []
    def exception(kind, value, traceback):
        failures.append(value)
        root.destroy()
    root.report_callback_exception = exception
    data = root.tk.call('list', str(archive))
    # Tcl list is represented as a Python tuple by wantobjects; convert it to
    # the exact serialized %D string emitted by the native drop binding.
    data = root.tk.call('format', '%s', data)
    assert app.on_drop(types.SimpleNamespace(data=data)) == COPY
    assert Path(app.folder.get()) == archive
    assert app.workflow is None  # dropping selects; explicit Start runs the pipeline.
    app.busy = True
    assert app.on_drop(types.SimpleNamespace(data=data)) == REFUSE_DROP
    app.busy = False
    with patch('desktop_app.messagebox.showwarning') as warning:
        multiple = root.tk.call('format', '%s', root.tk.call('list', str(archive), str(archive.parent)))
        assert app.on_drop(types.SimpleNamespace(data=multiple)) == REFUSE_DROP
        warning.assert_called_once()
    app.scan()
    attempts = 0
    def check():
        global attempts
        attempts += 1
        if app.busy:
            assert attempts < 150, 'input scan timed out'
            root.after(100, check)
            return
        assert app.workflow.phase == 'scanned'
        assert len(app.workflow.documents) == 1
        assert app.prepared_input.is_archive
        assert app.prepared_input.source == archive
        assert app.prepared_input.folder != archive.parent
        assert (app.workflow.job_dir / 'input.json').exists()
        assert str(app.clean_btn['state']) == 'disabled'
        print('GUI_DROP_SCAN_PASS', flush=True)
        root.destroy()
    root.after(300, check)
    root.mainloop()
    if failures:
        raise failures[0]
