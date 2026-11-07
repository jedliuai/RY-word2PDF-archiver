from pathlib import Path
import tempfile
import unittest

from task_files import available_path, remove_owned_tree


class TaskFileTests(unittest.TestCase):
    def test_names_use_plain_numbers_on_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(available_path(root, '资料-PDF', '.rar').name, '资料-PDF.rar')
            (root / '资料-PDF.rar').touch()
            self.assertEqual(available_path(root, '资料-PDF', '.rar').name, '资料-PDF (2).rar')

    def test_cannot_delete_outside_the_owned_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / 'owned'
            owned.mkdir()
            other = root / 'originals'
            other.mkdir()
            with self.assertRaises(ValueError):
                remove_owned_tree(other, parent=owned)
            self.assertTrue(other.exists())


if __name__ == '__main__':
    unittest.main()
