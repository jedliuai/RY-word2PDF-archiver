import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from pdf_workflow import Workflow, WorkflowError, validate_pdf
from local_services import Cancelled, ServiceError


def make_pdf(path, pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    writer.write(path)


class Converter:
    def __init__(self, *, pages=1, reported=1, fail=False, missing=False):
        self.pages, self.reported, self.fail, self.missing = pages, reported, fail, missing

    def convert(self, source, output, job_dir, cancel):
        if self.fail:
            raise ServiceError('模拟 WPS 转换失败')
        if not self.missing:
            make_pdf(output, self.pages)
        return self.reported


class Shield:
    def __init__(self, pending=False):
        self.pending = pending
        self.requests = 0
        self.clear = not pending

    def status(self):
        return {'status': 'ready'}

    def check(self, paths, cancel=None):
        return {'status': 'decrypted' if self.clear else 'encrypted'}

    def request(self, paths, cancel=None):
        self.requests += 1
        return {'status': 'submitted'}

    def wait(self, paths, cancel=None):
        return {'status': 'decrypted' if self.clear else 'pending', 'encryptedRemaining': 1}


class Archive:
    def __init__(self, fail=False):
        self.fail = fail
        self.created = False

    def create(self, root, pdfs, target, job_dir, cancel=None):
        if self.fail:
            raise ServiceError('模拟打包/成员校验失败')
        Path(target).write_bytes(b'simulated verified archive')
        self.created = True

    def verify(self, root, pdfs, target, cancel=None):
        if self.fail:
            raise ServiceError('模拟压缩包校验失败')


class PipelineSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / '客户 文件夹'
        self.root.mkdir()
        self.word = self.root / '合同.docx'
        self.word.write_bytes(b'test source')
        self.recycled = []

    def tearDown(self):
        self.temp.cleanup()

    def recycle(self, path):
        self.recycled.append(path)
        path.unlink()  # Only our generated fixture, never a user's document.

    def workflow(self, **options):
        return Workflow(self.root, data_dir=self.base / 'jobs', converter=options.pop('converter', Converter()),
                        shield=options.pop('shield', Shield()), archiver=options.pop('archiver', Archive()),
                        recycler=self.recycle, **options)

    def test_default_prepares_archive_but_never_deletes(self):
        flow = self.workflow()
        self.assertTrue(flow.prepare())
        self.assertEqual(flow.phase, 'review')
        self.assertTrue(flow.archive.exists())
        self.assertTrue(self.word.exists())
        self.assertEqual(self.recycled, [])
        flow.cleanup([self.word])
        self.assertEqual(flow.phase, 'complete')
        self.assertTrue(flow.documents[0].recycled)
        self.assertTrue(flow.intermediates_cleaned)
        self.assertFalse(flow.job_dir.exists())
        self.assertTrue(self.root.is_dir())
        self.assertTrue(self.word.with_suffix('.pdf').is_file())

    def test_existing_pdf_never_silently_trusted(self):
        make_pdf(self.word.with_suffix('.pdf'))
        flow = self.workflow()
        with self.assertRaisesRegex(WorkflowError, '同名 PDF'):
            flow.prepare()
        self.assertTrue(self.word.exists())
        self.assertFalse(flow.archiver.created)

    def test_explicit_reexport_backs_up_old_pdf(self):
        pdf = self.word.with_suffix('.pdf')
        make_pdf(pdf, 2)
        before = pdf.read_bytes()
        flow = self.workflow(overwrite=True)
        flow.prepare()
        self.assertEqual((flow.job_dir / '旧PDF备份' / pdf.name).read_bytes(), before)
        self.assertEqual(validate_pdf(pdf), 1)

    def test_page_mismatch_blocks_archive_and_cleanup(self):
        flow = self.workflow(converter=Converter(pages=1, reported=2))
        with self.assertRaisesRegex(WorkflowError, '页数不一致：Word 2 页 / PDF 1 页'):
            flow.prepare()
        self.assertFalse(flow.archiver.created)
        with self.assertRaises(WorkflowError):
            flow.cleanup([self.word])
        self.assertTrue(self.word.exists())

    def test_unreadable_second_page_stream_blocks_archive(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        page = writer.add_blank_page(width=595, height=842)
        stream = DecodedStreamObject()
        stream.set_data(b'BT ET')
        stream[NameObject('/Filter')] = NameObject('/UnsupportedForTest')
        page.replace_contents(stream)
        writer.write(self.root / '两页附件.pdf')
        flow = self.workflow()
        with self.assertRaisesRegex(WorkflowError, '两页附件.pdf.*UnsupportedForTest'):
            flow.prepare()
        self.assertFalse(flow.archiver.created)
        self.assertTrue(self.word.exists())

    def test_conversion_failure_keeps_word(self):
        flow = self.workflow(converter=Converter(fail=True))
        with self.assertRaises(ServiceError):
            flow.prepare()
        self.assertTrue(self.word.exists())
        self.assertFalse(flow.archiver.created)

    def test_transparent_source_encryption_is_compared_after_official_decryption(self):
        converter = Converter()
        original_convert = converter.convert
        def encrypt_after_open(source, output, job_dir, cancel):
            pages = original_convert(source, output, job_dir, cancel)
            source.write_bytes(b'encrypted:' + source.read_bytes())
            return pages
        converter.convert = encrypt_after_open
        shield = Shield()
        def check(paths, cancel=None):
            # Simulate official backend returning decrypted bytes on its own.
            files = [item for p in paths for item in (Path(p).rglob('*') if Path(p).is_dir() else [Path(p)]) if item.is_file()]
            for path in files:
                raw = path.read_bytes()
                if raw.startswith(b'encrypted:'):
                    path.write_bytes(raw[len(b'encrypted:'):])
            return {'status': 'decrypted'}
        shield.check = check
        flow = self.workflow(converter=converter, shield=shield)
        self.assertTrue(flow.prepare())
        self.assertEqual(flow.phase, 'review')

    def test_decryption_submits_whole_directories_in_one_request(self):
        shield = Shield(pending=True)
        requests = []
        original = shield.request
        def request(paths, cancel=None):
            requests.append(list(paths))
            return original(paths, cancel)
        shield.request = request
        flow = self.workflow(shield=shield)
        self.assertFalse(flow.prepare())
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0], [flow.root, flow.job_dir / '源文档核对副本'])
        self.assertTrue(all(p.is_dir() for p in requests[0]))

    def test_real_content_edit_during_conversion_is_blocked_after_decryption(self):
        converter = Converter()
        original_convert = converter.convert
        def edited(source, output, job_dir, cancel):
            pages = original_convert(source, output, job_dir, cancel)
            source.write_bytes(b'actually edited content')
            return pages
        converter.convert = edited
        flow = self.workflow(converter=converter)
        with self.assertRaisesRegex(WorkflowError, '副本内容不同'):
            flow.prepare()
        self.assertFalse(flow.archiver.created)
        self.assertTrue(self.word.exists())

    def test_stop_waiting_can_resume_without_second_request(self):
        shield = Shield(pending=True)
        flow = self.workflow(shield=shield)
        def stopped(paths, cancel=None):
            raise Cancelled('模拟停止等待')
        shield.wait = stopped
        with self.assertRaises(Cancelled):
            flow.prepare()
        self.assertEqual(flow.phase, 'waiting')
        shield.clear = True
        self.assertTrue(flow.resume())
        self.assertEqual(shield.requests, 1)

    def test_pdf_only_job_finishes_without_cleanup(self):
        self.word.unlink()
        make_pdf(self.root / '附件.pdf')
        flow = self.workflow()
        self.assertTrue(flow.prepare())
        self.assertEqual(flow.phase, 'complete')

    def test_review_reencryption_waits_then_requires_same_verified_pdf(self):
        shield = Shield()
        flow = self.workflow(shield=shield)
        flow.prepare()
        pdf = self.word.with_suffix('.pdf')
        verified = pdf.read_bytes()
        pdf.write_bytes(b'encrypted:' + verified)
        shield.clear = False
        self.assertFalse(flow.cleanup([self.word]))
        self.assertEqual(flow.phase, 'waiting')
        self.assertEqual(shield.requests, 1)
        self.assertTrue(self.word.exists())
        self.assertFalse(flow.resume())
        self.assertEqual(shield.requests, 1)
        # Official decryption restores the bytes, not a newly generated PDF.
        pdf.write_bytes(verified)
        shield.clear = True
        self.assertTrue(flow.resume())
        self.assertEqual(flow.phase, 'review')
        self.assertTrue(self.word.exists())
        flow.cleanup([self.word])
        self.assertEqual(flow.phase, 'complete')

    def test_review_decryption_cannot_reset_hash_to_modified_content(self):
        shield = Shield()
        flow = self.workflow(shield=shield)
        flow.prepare()
        shield.clear = False
        self.assertFalse(flow.cleanup([self.word]))
        make_pdf(self.word.with_suffix('.pdf'), 2)
        shield.clear = True
        with self.assertRaisesRegex(WorkflowError, 'PDF 在校验后发生变化'):
            flow.resume()
        self.assertTrue(self.word.exists())

    def test_pdf_that_requires_open_password_blocks_archive(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.encrypt('test-password')
        writer.write(self.root / '有密码.pdf')
        flow = self.workflow()
        with self.assertRaisesRegex(WorkflowError, '打开需要密码'):
            flow.prepare()
        self.assertTrue(self.word.exists())

    def test_pdf_with_permissions_password_can_be_archived(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.encrypt('', 'owner-password')
        attachment = self.root / '可打开的附件.pdf'
        writer.write(attachment)
        contents = attachment.read_bytes()
        flow = self.workflow()
        self.assertTrue(flow.prepare())
        self.assertEqual(flow.phase, 'review')
        self.assertTrue(flow.archiver.created)
        self.assertEqual(attachment.read_bytes(), contents)

    def test_missing_conversion_output_blocks(self):
        flow = self.workflow(converter=Converter(missing=True))
        with self.assertRaisesRegex(WorkflowError, '没有生成非空 PDF'):
            flow.prepare()
        self.assertTrue(self.word.exists())
        self.assertTrue(self.word.exists())

    def test_pending_resume_never_resubmits(self):
        shield = Shield(pending=True)
        flow = self.workflow(shield=shield)
        self.assertFalse(flow.prepare())
        self.assertEqual(flow.phase, 'waiting')
        self.assertEqual(shield.requests, 1)
        self.assertFalse(flow.resume())
        self.assertEqual(shield.requests, 1)
        shield.clear = True
        self.assertTrue(flow.resume())
        self.assertEqual(shield.requests, 1)
        self.assertTrue(self.word.exists())

    def test_archive_failure_blocks_cleanup(self):
        flow = self.workflow(archiver=Archive(fail=True))
        with self.assertRaises(ServiceError):
            flow.prepare()
        self.assertTrue(self.word.exists())
        self.assertEqual(flow.phase, 'error')

    def test_source_modified_after_archive_blocks_cleanup(self):
        flow = self.workflow()
        flow.prepare()
        self.word.write_bytes(b'new source content')
        with self.assertRaisesRegex(WorkflowError, '源文档已变化'):
            flow.cleanup([self.word])
        self.assertFalse(self.recycled)

    def test_pdf_modified_after_archive_blocks_cleanup(self):
        flow = self.workflow()
        flow.prepare()
        make_pdf(self.word.with_suffix('.pdf'), 2)
        with self.assertRaisesRegex(WorkflowError, 'PDF 在校验后发生变化'):
            flow.cleanup([self.word])
        self.assertTrue(self.word.exists())

    def test_archive_modified_blocks_cleanup(self):
        flow = self.workflow()
        flow.prepare()
        flow.archive.write_bytes(b'changed archive')
        with self.assertRaisesRegex(WorkflowError, 'RAR 已缺失或变化'):
            flow.cleanup([self.word])
        self.assertTrue(self.word.exists())

    def test_new_word_blocks_cleanup(self):
        flow = self.workflow()
        flow.prepare()
        (self.root / '新增.doc').write_bytes(b'new')
        with self.assertRaisesRegex(WorkflowError, '新增或缺失'):
            flow.cleanup([self.word])
        self.assertFalse(self.recycled)

    def test_cancel_before_conversion_blocks(self):
        flow = self.workflow()
        flow.cancel.set()
        with self.assertRaises(Cancelled):
            flow.prepare()
        self.assertEqual(flow.phase, 'stopped')
        self.assertTrue(self.word.exists())

    def test_unreadable_or_warning_never_passes_decryption(self):
        flow = self.workflow()
        flow.shield.check = lambda *args: {'status': 'decrypted', 'warnings': ['skipped file']}
        with self.assertRaisesRegex(WorkflowError, '遗漏或不可读'):
            flow.prepare()
        self.assertFalse(flow.archiver.created)

    def test_decryption_count_mismatch_cannot_pass(self):
        flow = self.workflow()
        flow.shield.check = lambda *args: {'status': 'decrypted', 'scannedFiles': 0}
        with self.assertRaisesRegex(WorkflowError, '扫描数量'):
            flow.prepare()
        self.assertFalse(flow.archiver.created)

    def test_partial_cleanup_failure_records_completed_items(self):
        other = self.root / '说明.docx'
        other.write_bytes(b'other source')
        flow = self.workflow()
        flow.prepare()
        def partial(path):
            if path == other:
                raise PermissionError('模拟被占用')
            self.recycle(path)
        flow.recycler = partial
        with self.assertRaises(PermissionError):
            flow.cleanup([self.word, other])
        self.assertTrue(flow.documents[0].recycled)
        self.assertTrue(other.exists())
        self.assertEqual(flow.phase, 'error')

    def test_cleanup_rejects_paths_outside_task(self):
        flow = self.workflow()
        flow.prepare()
        with self.assertRaises(WorkflowError):
            flow.cleanup([self.base / 'outside.docx'])
        self.assertFalse(self.recycled)

    def test_corrupt_existing_attachment_pdf_blocks_all_cleanup(self):
        (self.root / '损坏附件.pdf').write_bytes(b'%PDF-broken')
        flow = self.workflow()
        with self.assertRaisesRegex(WorkflowError, 'PDF 校验失败'):
            flow.prepare()
        self.assertTrue(self.word.exists())

    def test_selected_cleanup_can_keep_other_words(self):
        other = self.root / '保留.docx'
        other.write_bytes(b'other source')
        flow = self.workflow()
        flow.prepare()
        flow.cleanup([self.word])
        self.assertEqual(flow.phase, 'review')
        self.assertTrue(other.exists())
        flow.cleanup([other])
        self.assertEqual(flow.phase, 'complete')

    def test_deliveries_use_plain_names_and_keep_existing_results(self):
        previous = self.base / f'{self.root.name}-PDF.rar'
        previous.write_bytes(b'previous result')
        flow = self.workflow()
        flow.prepare()
        self.assertEqual(flow.archive.name, f'{self.root.name}-PDF (2).rar')
        self.assertEqual(previous.read_bytes(), b'previous result')
        self.assertNotIn(flow.id, flow.archive.name)

    def test_review_and_failure_keep_needed_work_files(self):
        flow = self.workflow()
        flow.prepare()
        self.assertEqual(flow.phase, 'review')
        self.assertTrue(flow.job_dir.exists())
        self.word.write_bytes(b'changed source')
        with self.assertRaises(WorkflowError):
            flow.cleanup([self.word])
        self.assertTrue(flow.job_dir.exists())
        self.assertTrue(self.word.exists())

    def test_cleanup_occupancy_does_not_mark_valid_delivery_as_failed(self):
        flow = self.workflow()
        flow.prepare()
        with patch('pdf_workflow.remove_owned_tree', side_effect=PermissionError('file occupied')):
            flow.cleanup([self.word])
        self.assertEqual(flow.phase, 'complete')
        self.assertTrue(flow.archive.is_file())
        self.assertTrue(flow.cleanup_warning)
        self.assertFalse(flow.intermediates_cleaned)
        self.assertTrue(flow.job_dir.exists())
        flow._cleanup_intermediates()
        self.assertFalse(flow.job_dir.exists())

    def test_archive_input_removes_only_its_extraction_and_job(self):
        from archive_input import ArchiveImporter
        import zipfile
        original = self.base / '原始资料.zip'
        with zipfile.ZipFile(original, 'w') as stream:
            stream.writestr('客户/合同.docx', b'fixture source')
        original_bytes = original.read_bytes()
        prepared = ArchiveImporter().prepare(original)
        flow = Workflow(prepared.folder, data_dir=self.base / 'jobs', converter=Converter(),
                        shield=Shield(), archiver=Archive(), recycler=self.recycle,
                        prepared_input=prepared, delivery_name=original.stem)
        flow.prepare()
        self.assertTrue(prepared.folder.exists())
        flow.cleanup([d.source for d in flow.documents])
        self.assertTrue(flow.intermediates_cleaned, flow.cleanup_warning)
        self.assertFalse(prepared.folder.exists())
        self.assertFalse(flow.job_dir.exists())
        self.assertFalse(Path(prepared.report).exists())
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(flow.archive.name, '原始资料-PDF.rar')
        self.assertTrue(flow.archive.exists())
        self.assertTrue(self.word.exists())  # unrelated original folder is untouched.


if __name__ == '__main__':
    unittest.main()
