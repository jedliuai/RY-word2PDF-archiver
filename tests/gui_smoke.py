"""Launch, exercise scan controls, capture the actual native UI, then close."""
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tkinter as tk
from PIL import ImageGrab
from desktop_app import WorkflowApp
from tkinterdnd2 import TkinterDnD
from task_files import remove_owned_tree

sample = tempfile.TemporaryDirectory(prefix='wpspdf-gui-')
source = Path(sample.name) / '界面测试'
(source / '客户 A').mkdir(parents=True)
(source / '两页合同.docx').write_bytes(b'GUI scan fixture only')
(source / '客户 A' / '说明.docx').write_bytes(b'GUI scan fixture only')

root = TkinterDnD.Tk()
root.attributes('-topmost', True)
app = WorkflowApp(root)
app.folder.set(str(source))
output = Path('.test-artifacts/ui-preview.png').absolute()
output.parent.mkdir(parents=True, exist_ok=True)
root.after(200, app.scan)
attempts = 0


def finish():
    global attempts
    attempts += 1
    if app.busy or not app.workflow:
        if attempts > 100:
            raise RuntimeError('GUI scan timed out')
        root.after(100, finish)
        return
    assert app.workflow.phase == 'scanned'
    assert len(app.tree.get_children()) == 2
    assert str(app.clean_btn['state']) == 'disabled'
    assert not app.auto.get()
    root.update_idletasks()
    root.lift()
    root.after(400, capture)


def capture():
    x, y = root.winfo_rootx(), root.winfo_rooty()
    ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(output)
    print('GUI_SMOKE_PASS', output, flush=True)
    root.destroy()


root.after(1000, finish)
root.after(15000, root.destroy)
root.mainloop()
if app.workflow and app.workflow.job_dir.exists():
    remove_owned_tree(app.workflow.job_dir, parent=app.workflow.jobs_parent)
sample.cleanup()
