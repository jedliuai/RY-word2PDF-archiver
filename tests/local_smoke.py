"""Opt-in live WPS/Green Shield/RAR smoke test on newly generated fixtures only.

Run from the project root: python tests/local_smoke.py
Not part of unittest discovery. May show official Green Shield approval UI.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docx import Document
from pypdf import PdfWriter
from pdf_workflow import Workflow
import uuid


def main():
    base = Path(__file__).resolve().parents[1] / '.test-artifacts' / uuid.uuid4().hex[:8]
    root = base / '联调样本'
    (root / '客户 A').mkdir(parents=True)
    first = Document()
    first.add_heading('Workflow sample', 0)
    first.add_paragraph('Generated test document. Page one.')
    first.add_page_break()
    first.add_paragraph('Page two. This document is safe to recycle after verification.')
    first.save(root / '两页合同.docx')
    second = Document()
    second.add_paragraph('Nested folder sample.')
    second.save(root / '客户 A' / '说明.docx')
    pdf = PdfWriter()
    pdf.add_blank_page(width=595, height=842)
    pdf.write(root / '已有附件.pdf')
    flow = Workflow(root, data_dir=base / 'jobs',
                    emit=lambda kind, value: print(kind, value, flush=True) if kind in ('log', 'state') else None)
    print('FIXTURE_ROOT', root, flush=True)
    result = flow.prepare()
    print('PREPARE', result, flow.phase, flush=True)
    if result:
        assert flow.phase == 'review'
        assert all(Path(d.source).exists() for d in flow.documents)
        # Only files created above, no business documents.
        flow.cleanup([d.source for d in flow.documents])
        assert flow.phase == 'complete'
        print('LIVE_SMOKE_PASS', flow.archive, flush=True)
    else:
        print('LIVE_SMOKE_PENDING_APPROVAL', flow.job_dir, flush=True)


if __name__ == '__main__':
    main()
