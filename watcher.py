"""
Folder watcher — drop a bank CSV into ~/budget-imports/ and it auto-imports.
Runs as a daemon thread inside the Flask process.
"""

import logging
import time
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import db
import importer

logger = logging.getLogger(__name__)


class CSVHandler(FileSystemEventHandler):
    def __init__(self, done_folder: Path):
        self.done_folder = done_folder

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != '.csv':
            return
        # Small delay to ensure the file is fully written
        time.sleep(0.8)
        self._process(path)

    def _process(self, path: Path):
        try:
            try:
                content = path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                content = path.read_text(encoding='latin-1')
        except Exception as e:
            logger.error(f'Could not read {path.name}: {e}')
            return

        added, skipped, bank = importer.import_csv_string(content)

        if bank:
            logger.info(f'{path.name} → {bank}: {added} imported, {skipped} skipped')
            db.log_import(path.name, bank, added, skipped)
            dest = self._unique_dest(path.name)
            path.rename(dest)
        else:
            logger.warning(f'Could not detect bank format for {path.name} — left in place')

    def _unique_dest(self, filename: str) -> Path:
        dest = self.done_folder / filename
        if not dest.exists():
            return dest
        stem, suffix = Path(filename).stem, Path(filename).suffix
        i = 1
        while dest.exists():
            dest = self.done_folder / f'{stem}_{i}{suffix}'
            i += 1
        return dest


def start_watcher(watch_folder: Path, done_folder: Path):
    handler  = CSVHandler(done_folder)
    observer = Observer()
    observer.schedule(handler, str(watch_folder), recursive=False)
    observer.start()
    logger.info(f'Watching {watch_folder} for bank CSVs...')
    try:
        while True:
            time.sleep(1)
    except Exception:
        observer.stop()
    observer.join()
