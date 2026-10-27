"""Knowledge indexing boundaries and incremental update regressions."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import knowledge


class KnowledgeRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'notes_1'
        self.folder.mkdir()
        self.db = patch.object(knowledge, 'DB', str(self.root / 'index.db'))
        self.db.start()
        knowledge._cancel.clear()

    def tearDown(self):
        knowledge._cancel.clear()
        self.db.stop()
        self.temp.cleanup()

    def test_subsecond_same_size_edit(self):
        p = self.folder / 'note.txt'
        p.write_text('alpha')
        os.utime(p, (1000, 1000))
        knowledge.index([str(self.folder)])
        p.write_text('bravo')
        os.utime(p, (1000.25, 1000.25))
        knowledge.index([str(self.folder)])
        self.assertTrue(knowledge.search('bravo'))
        self.assertFalse(knowledge.search('alpha'))

    def test_folder_boundary_and_literal_wildcards(self):
        for name in ('notes_1', 'notes_12', 'notesX1'):
            folder = self.root / name
            folder.mkdir(exist_ok=True)
            (folder / 'note.txt').write_text('searchable')
        knowledge.index([str(self.root)])
        hits = knowledge.search('searchable', folder=str(self.folder))
        self.assertEqual([str(self.folder / 'note.txt')], [h['path'] for h in hits])

    def test_secrets_and_escaping_symlink_are_not_indexed(self):
        (self.folder / 'credentials.json').write_text('sensitive')
        outside = self.root / 'outside.txt'
        outside.write_text('sensitive')
        try:
            (self.folder / 'link.txt').symlink_to(outside)
        except OSError:                          # Windows without Developer Mode can't make symlinks
            knowledge.index([str(self.folder)])
            self.assertFalse(knowledge.search('sensitive'))
            self.skipTest('Secret file checked; the symlink part needs Developer Mode or admin rights on Windows')
        knowledge.index([str(self.folder)])
        self.assertFalse(knowledge.search('sensitive'))

    def test_old_index_protected_entries_are_filtered(self):
        p = self.folder / 'credentials.json'
        p.write_text('sensitive')
        with patch.object(knowledge, '_allowed', return_value=True):
            knowledge.index([str(self.folder)])
        self.assertFalse(knowledge.search('sensitive'))

    def test_oversized_file_is_removed_on_reindex(self):
        p = self.folder / 'note.txt'
        p.write_text('searchable')
        knowledge.index([str(self.folder)])
        with patch.object(knowledge, 'MAX_FILE', 3):
            knowledge.index([str(self.folder)])
        self.assertFalse(knowledge.search('searchable'))

    def test_cancelled_scan_preserves_existing_index(self):
        (self.folder / 'note.txt').write_text('searchable')
        knowledge.index([str(self.folder)])
        knowledge.cancel()
        knowledge.index([str(self.folder)])
        self.assertTrue(knowledge.search('searchable'))
        self.assertEqual('cancelled', knowledge.status()['state'])
