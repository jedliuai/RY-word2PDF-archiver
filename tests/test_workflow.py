import tempfile
import unittest
from pathlib import Path

from pdf_workflow import scan_folder, WorkflowError


class ScanSafetyTests(unittest.TestCase):
    def test_same_stem_is_blocked_before_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '合同.doc').touch()
            (root / '合同.docx').touch()
            with self.assertRaisesRegex(WorkflowError, '同名'):
                scan_folder(root)

    def test_recurses_and_ignores_word_lock_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '客户').mkdir()
            (root / '客户' / '说明.DOCX').touch()
            (root / '~$说明.docx').touch()
            (root / '客户' / '附件.PDF').touch()
            words, pdfs = scan_folder(root)
            self.assertEqual([p.name for p in words], ['说明.DOCX'])
            self.assertEqual([p.name for p in pdfs], ['附件.PDF'])


if __name__ == '__main__':
    unittest.main()
