from pathlib import Path
import tempfile
import unittest

from local_services import RarArchiver, ServiceError


class ArchiveSafetyTests(unittest.TestCase):
    def test_creation_never_sets_password_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'one.pdf'
            pdf.write_bytes(b'sample')
            archiver = RarArchiver(executable='not-invoked.exe')
            calls = []
            archiver._run = lambda args, **kwargs: calls.append(args)
            archiver.verify = lambda *args: None
            archiver.create(root, [pdf], root / 'output.rar', root)
            self.assertFalse(any(str(a).startswith('-p') for a in calls[0]))

    def test_member_omission_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'sample.rar'
            target.write_bytes(b'Rar!\x1a\x07\x01\x00')
            archiver = RarArchiver(executable='not-invoked.exe')
            calls = []
            def run(args, **kwargs):
                calls.append(args[0])
                if args[0] == 'lb':
                    return 'one.pdf\n'.encode('utf-16')
                return b''
            archiver._run = run
            with self.assertRaisesRegex(ServiceError, '清单'):
                archiver.verify(root, [root / 'one.pdf', root / 'two.pdf'], target)
            self.assertEqual(calls, ['t', 'lb'])

    def test_fake_rar_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'sample.rar'
            target.write_bytes(b'PK\x03\x04fake zip')
            archiver = RarArchiver(executable='not-invoked.exe')
            with self.assertRaisesRegex(ServiceError, 'RAR5'):
                archiver.verify(root, [], target)


if __name__ == '__main__':
    unittest.main()
