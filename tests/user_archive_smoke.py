"""Explicitly run the user's ZIP through prepare only; never clean its Words."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_input import ArchiveImporter
from pdf_workflow import Workflow

source = next(Path('测试文件').absolute().glob('*.zip'))
emit = lambda kind, value: print(kind, value, flush=True) if kind in ('log', 'state') else None
prepared = ArchiveImporter().prepare(source, emit=emit)
flow = Workflow(prepared.folder, emit=emit, delivery_name=source.stem, prepared_input=prepared)
(flow.job_dir / 'input.json').write_text(json.dumps({
    'source': str(source), 'folder': str(prepared.folder), 'source_hash': prepared.source_hash,
}, ensure_ascii=False, indent=2), encoding='utf-8')
Path('.test-artifacts/user-input-latest.json').write_text(json.dumps({
    'folder': str(prepared.folder), 'job_dir': str(flow.job_dir),
}, ensure_ascii=False), encoding='utf-8')
print('INPUT_FOLDER',prepared.folder,flush=True)
result = flow.prepare()
print('RESULT',result,flow.phase,flush=True)
print('REPORT',flow.job_dir,flush=True)
if result:
    assert flow.phase == 'review'
    assert len(flow.documents) == 9
    assert len(flow.pdfs) == 12
    assert all(Path(d.source).is_file() and not d.recycled for d in flow.documents)
    prepared.verify_source()
    print('USER_ZIP_SMOKE_PASS',flow.archive,flush=True)
