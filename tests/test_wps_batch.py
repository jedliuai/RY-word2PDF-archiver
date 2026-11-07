from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from wps_worker import run_jobs


class WpsSessionTests(unittest.TestCase):
    def test_all_documents_share_one_application_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = Mock()
            app.Documents.Count = 0
            def open_doc(source, **kwargs):
                app.Documents.Count += 1
                doc = Mock()
                doc.ComputeStatistics.return_value = 2
                doc.SaveAs2.side_effect = lambda target, **options: Path(target).write_bytes(b'%PDF-sample')
                doc.Close.side_effect = lambda **options: setattr(app.Documents, 'Count', app.Documents.Count - 1)
                return doc
            app.Documents.Open.side_effect = open_doc
            jobs = [{'source': str(root / f'{i}.docx'), 'output': str(root / f'{i}.pdf')} for i in range(4)]
            results = []
            with (patch('win32com.client.DispatchEx', return_value=app) as dispatch,
                  patch('pythoncom.CoInitialize'), patch('pythoncom.CoUninitialize')):
                run_jobs(jobs, lambda index, pages: results.append((index, pages)))
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(app.Documents.Open.call_count, 4)
            app.Quit.assert_called_once()
            self.assertEqual(results, [(1, 2), (2, 2), (3, 2), (4, 2)])


if __name__ == '__main__':
    unittest.main()
