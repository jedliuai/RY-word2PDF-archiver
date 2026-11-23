"""Windows desktop entry point and dedicated conversion worker."""
import sys

__version__ = '2.0.0'

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--wps-worker':
        from wps_worker import convert
        try:
            convert(*sys.argv[2:5])
        except Exception as exc:
            import json
            from pathlib import Path
            Path(sys.argv[4]).write_text(json.dumps({'error': str(exc)}, ensure_ascii=False), encoding='utf-8')
            sys.exit(1)
    elif len(sys.argv) > 1 and sys.argv[1] == '--archive-worker':
        from archive_input import worker_main
        sys.exit(worker_main(sys.argv[2:5]))
    elif len(sys.argv) > 1 and sys.argv[1] == '--wps-batch-worker':
        from wps_worker import batch_main
        sys.exit(batch_main(sys.argv[2]))
    elif '--classic' in sys.argv[1:]:
        # Previous Tkinter window, kept as a fallback.
        from desktop_app import main
        main()
    else:
        try:
            from web_app import main
        except ImportError as exc:
            print(f'新界面依赖缺失（{exc}），改用经典界面。请运行：python -m pip install -r requirements.txt')
            from desktop_app import main
        main()
