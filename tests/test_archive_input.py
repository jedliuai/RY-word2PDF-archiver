from pathlib import Path
import tempfile
import unittest
import zipfile
import stat
import threading
from unittest.mock import patch

from archive_input import ArchiveImporter, InputError


class ArchiveInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.importer = ArchiveImporter()

    def tearDown(self):
        self.temp.cleanup()

    def zip(self, names):
        archive = self.root / '中文 有空格.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            for name, data in names:
                stream.writestr(name, data)
        return archive

    def test_folder_is_used_directly(self):
        result = self.importer.prepare(self.root)
        self.assertEqual(result.folder, self.root)
        self.assertFalse(result.is_archive)

    def test_zip_preserves_original_and_subdirectories(self):
        archive = self.zip([('客户 A/合同.docx', b'word'), ('说明.pdf', b'pdf')])
        original = archive.read_bytes()
        result = self.importer.prepare(archive)
        self.assertEqual((result.folder / '客户 A/合同.docx').read_bytes(), b'word')
        self.assertEqual(archive.read_bytes(), original)
        self.assertNotEqual(result.folder, self.root)
        self.assertEqual(result.folder.name, '解压文件')

    def test_traversal_is_rejected(self):
        archive = self.zip([('../outside.docx', b'bad')])
        with self.assertRaises(InputError):
            self.importer.prepare(archive)
        self.assertFalse((self.root / 'outside.docx').exists())

    def test_duplicate_case_and_reserved_names_are_rejected(self):
        cases = [[('合同.docx', b'a'), ('合同.DOCX', b'b')], [('CON.pdf', b'a')],
                 [('C:/outside.pdf', b'a')], [('..\\outside.pdf', b'a')],
                 [('nested/other.pdf:stream', b'a')]]
        for names in cases:
            with self.subTest(names=names):
                with self.assertRaises(InputError):
                    self.importer.prepare(self.zip(names))

    def test_symlink_is_rejected(self):
        archive = self.root / 'link.zip'
        info = zipfile.ZipInfo('link')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, 'w') as stream:
            stream.writestr(info, '../outside')
        with self.assertRaises(InputError):
            self.importer.prepare(archive)

    def test_corrupt_zip_leaves_original_unchanged(self):
        archive = self.root / '坏包.zip'
        archive.write_bytes(b'broken zip')
        with self.assertRaises(InputError):
            self.importer.prepare(archive)
        self.assertEqual(archive.read_bytes(), b'broken zip')

    def test_repeated_import_uses_different_directories(self):
        archive = self.zip([('one.docx', b'word')])
        first = self.importer.prepare(archive)
        second = self.importer.prepare(archive)
        self.assertNotEqual(first.folder, second.folder)
        self.assertEqual(second.folder.name, '解压文件 (2)')
        self.assertTrue((first.folder / 'one.docx').exists())

    def test_modified_archive_after_scan_is_not_reused(self):
        archive = self.zip([('one.docx', b'word')])
        result = self.importer.prepare(archive)
        archive.write_bytes(b'modified archive')
        with self.assertRaisesRegex(InputError, '发生变化'):
            result.verify_source()

    def test_cancelled_input_never_extracts(self):
        from local_services import Cancelled
        archive = self.zip([('one.docx', b'word')])
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            self.importer.prepare(archive, cancel=cancel)

    def test_unsupported_file_is_rejected(self):
        path = self.root / 'single.docx'
        path.touch()
        with self.assertRaises(InputError):
            self.importer.prepare(path)

    def test_cleanup_refuses_changed_original_or_unexpected_added_files(self):
        archive = self.zip([('one.docx', b'word')])
        prepared = self.importer.prepare(archive)
        (prepared.folder / 'new-note.txt').write_text('user note')
        with self.assertRaisesRegex(InputError, '新增'):
            prepared.cleanup_success()
        self.assertTrue(prepared.folder.exists())
        (prepared.folder / 'new-note.txt').unlink()
        archive.write_bytes(b'new original')
        with self.assertRaisesRegex(InputError, '发生变化'):
            prepared.cleanup_success()
        self.assertTrue(prepared.folder.exists())


if __name__ == '__main__':
    unittest.main()
