import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app_controller import Controller


def row(source, recycled=False, pdf_hash=''):
    return {'source': source, 'pdf': source + '.pdf', 'source_hash': 'h', 'snapshot': '', 'pages': 2,
            'pdf_hash': pdf_hash, 'status': '校验通过 · 2 页', 'recycled': recycled}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.pushed = []
        self.controller = Controller(notify=self.pushed.append, importer=object())
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_accept_input_rejects_other_files(self):
        other = self.root / 'a.txt'
        other.write_text('x', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.controller.accept_input(str(other))
        state = self.controller.accept_input(str(self.root))
        self.assertEqual(state['input']['kind'], 'folder')
        self.assertEqual(state['phase'], 'new')

    def test_selection_only_in_review_and_skips_recycled(self):
        c = self.controller
        c.accept_input(str(self.root))
        c.workflow = SimpleNamespace(phase='converting', root=self.root, archive=None, pdfs=[], job_dir=self.root)
        a, b = str(self.root / 'a.docx'), str(self.root / 'b.docx')
        c.on_event('files', [row(a, pdf_hash='x'), row(b, recycled=True, pdf_hash='y')])
        c.select_all(True)
        self.assertEqual(c.snapshot()['counts']['selected'], 0)
        c.workflow.phase = 'review'
        c.select_all(True)
        state = c.snapshot()
        self.assertEqual(state['counts']['selected'], 1)
        self.assertEqual({f['name'] for f in state['files']}, {'a.docx', 'b.docx'})
        self.assertTrue(state['canCleanup'])
        c.select(a, False)
        self.assertEqual(c.snapshot()['counts']['selected'], 0)

    def test_done_with_error_reports_and_unlocks(self):
        c = self.controller
        c.accept_input(str(self.root))
        c.busy = True
        c.workflow = SimpleNamespace(phase='error', root=self.root, archive=None, pdfs=[], job_dir=self.root)
        c.on_event('log', '开始')
        c.on_event('done', '已有同名 PDF')
        state = c.snapshot()
        self.assertFalse(state['busy'])
        self.assertEqual(state['error']['message'], '已有同名 PDF')
        self.assertEqual(state['displayPhase'], 'error')
        self.assertEqual(state['logs'][0]['text'], '开始')
        self.assertTrue(self.pushed)

    def test_waiting_input_is_a_notice_not_an_error(self):
        c = self.controller
        c.accept_input(str(self.root))
        c.busy = True
        c.input_phase = 'waiting_input'
        c.on_event('state', 'waiting_input')
        c.on_event('done', '压缩包等待审批')
        state = c.snapshot()
        self.assertIsNone(state['error'])
        self.assertEqual(state['notice']['message'], '压缩包等待审批')
        self.assertTrue(state['canResume'])

    def test_interrupted_decryption_wait_still_offers_resume(self):
        c = self.controller
        c.accept_input(str(self.root))
        c.busy = True
        c.workflow = SimpleNamespace(phase='waiting', root=self.root, archive=None, pdfs=[], job_dir=self.root)
        c.on_event('done', '已停止等待')
        state = c.snapshot()
        self.assertTrue(state['canResume'])
        self.assertIsNone(state['error'])
        self.assertEqual(state['notice']['message'], '已停止等待')

    def test_cleanup_requires_selection(self):
        with self.assertRaises(ValueError):
            self.controller.cleanup()


if __name__ == '__main__':
    unittest.main()
