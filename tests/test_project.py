"""Offline regression tests; never update a real index or install packages."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import search
import mcp_server
import index_watcher


class SearchTests(unittest.TestCase):
    def test_live_filters_and_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'Report.txt').write_text('hello')
            Path(directory, 'report-dir').mkdir()
            result = mcp_server.search_files('report', project_path=directory,
                                             entry_type='file', min_size='5B')
            self.assertEqual(result['total'], 1)
            self.assertEqual(result['backend'], 'find')
            self.assertEqual(result['results'][0]['size'], 5)
            self.assertEqual(mcp_server.search_files('report', project_path=directory,
                                                     offset=2)['shown'], 0)

    def test_auto_tries_locate_when_plocate_fails(self):
        args = search.parser().parse_args(['needle'])
        calls = []
        def run_indexed(binary, args):
            calls.append(binary)
            if binary.endswith('/plocate'):
                raise search.SearchError('broken plocate index')
            return []
        with mock.patch.object(search.shutil, 'which', side_effect=lambda name: '/bin/' + name), \
             mock.patch.object(search, 'run_indexed', side_effect=run_indexed):
            result = search.execute_search(args)
        self.assertEqual(calls, ['/bin/plocate', '/bin/locate'])
        self.assertEqual(result.backend, 'locate')
        self.assertIn('broken plocate index', result.fallback_reason)

    def test_argument_validation(self):
        for kwargs in ({'query': 1}, {'query': 'a', 'regex': 'false'},
                       {'query': 'a', 'backend': 'invalid'},
                       {'query': 'a', 'count': True},
                       {'query': 'a', 'timeout': float('nan')}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                mcp_server.search_files(**kwargs)

    def test_busy_refresh_is_not_success(self):
        events = [{'detected_at': '2026-01-01T00:00:00+00:00'}]
        self.assertEqual(search._events_since_refresh({'events': events,
            'last_refresh': {'status': 'already_running',
                             'completed_at': '2026-01-02T00:00:00+00:00'}}), events)

    def test_history_limit_and_masks(self):
        events = index_watcher.normalize_event_history([
            {'paths': [{'path': '/example', 'mask': index_watcher.IN_CREATE}]}
            for _ in range(150)])
        self.assertEqual(len(events), 100)
        self.assertEqual(events[0]['paths'][0]['mask_human'], 'IN_CREATE')

    def test_refresh_uses_private_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory, 'index.db'))
            with mock.patch.dict(os.environ, {search.USER_INDEX_DATABASE_ENV: database}), \
                 mock.patch.object(search.shutil, 'which', return_value='/bin/plocate'), \
                 mock.patch.object(search, 'find_updatedb', return_value='/bin/updatedb'), \
                 mock.patch.object(search.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')), \
                 mock.patch.object(search, 'index_status', return_value={'database_modified': None}):
                self.assertEqual(search.refresh_index()['status'], 'updated')
            self.assertTrue(Path(database + '.lock').is_file())
            self.assertEqual(Path(database + '.lock').stat().st_mode & 0o777, 0o600)


class WatcherTests(unittest.TestCase):
    def test_events_continue_after_watch_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, 'watched')
            root.mkdir()
            state = str(Path(directory, 'state.json'))
            refreshed = threading.Event()
            with mock.patch.dict(os.environ, {search.WATCHER_STATE_ENV: state}), \
                 mock.patch.object(index_watcher, 'PRUNED_ROOTS', frozenset()), \
                 mock.patch.object(search, 'refresh_index', return_value={'status': 'updated'}):
                watcher = index_watcher.InotifyWatcher(str(root), .05, .1, 1)
                original_refresh = watcher.refresh
                def refresh():
                    result = original_refresh()
                    refreshed.set()
                    return result
                watcher.refresh = refresh
                thread = threading.Thread(target=watcher.run, daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 3
                    while not watcher.path_to_wd and time.monotonic() < deadline:
                        time.sleep(.01)
                    child = root / 'new-directory'
                    child.mkdir()
                    self.assertTrue(refreshed.wait(3), 'directory creation was not detected')
                    # Allow the rebuild following refresh to finish.
                    time.sleep(.15)
                    refreshed.clear()
                    (child / 'new-file.txt').touch()
                    self.assertTrue(refreshed.wait(3), 'events stopped after rebuilding watches')
                finally:
                    watcher.stop_requested = True
                    thread.join(3)
                self.assertFalse(thread.is_alive())


class MCPTests(unittest.TestCase):
    def test_stdio_client_and_malformed_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'needle.txt').touch()
            requests = [
                {'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
                {'method': 'tools/list'},
                {'method': 'tools/call', 'params': {'name': 'search_files', 'arguments':
                    {'query': 'needle', 'project_path': directory}}},
                {'method': 'tools/call', 'params': {'name': 'index_status'}},
                {'method': 'tools/call', 'params': {'name': 'watcher_status'}},
                {'method': 'initialize', 'params': ['invalid']},
                {'method': 'initialize', 'params': {'protocolVersion': []}},
                {'method': 'tools/call', 'params': {'name': []}},
                {'method': 'tools/call', 'params': {'name': 'search_files', 'arguments': []}},
                {'method': 'ping'},
            ]
            messages = [dict(request, jsonrpc='2.0', id=i) for i, request in enumerate(requests)]
            messages.insert(1, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            env = dict(os.environ, LINUX_FILE_SEARCH_WATCHER_STATE=str(Path(directory, 'missing.json')))
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')],
                input='\n'.join(json.dumps(message) for message in messages) + '\n',
                text=True, capture_output=True, timeout=20, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            replies = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(len(replies), len(requests))
            tools = replies[1]['result']['tools']
            self.assertEqual({tool['name'] for tool in tools}, {'search_files', 'index_status', 'watcher_status'})
            for tool in tools:
                self.assertTrue(tool['annotations']['readOnlyHint'])
                self.assertFalse(tool['annotations']['destructiveHint'])
                self.assertFalse(tool['annotations']['openWorldHint'])
            self.assertEqual(replies[2]['result']['structuredContent']['total'], 1)
            self.assertIn('database_exists', replies[3]['result']['structuredContent'])
            self.assertEqual(replies[-1]['result'], {})
            for reply in replies[5:8]:
                self.assertIn('error', reply)
            self.assertTrue(replies[8]['result']['isError'])


class InstallerTests(unittest.TestCase):
    def run_installer(self, *args):
        return subprocess.run(['bash', str(ROOT / 'scripts/install.sh'), *args],
                              capture_output=True, text=True, timeout=10)

    def test_preserve_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, 'linux-whole-system-file-search')
            self.assertEqual(self.run_installer('--target', directory).returncode, 0)
            extra = destination / 'keep.txt'
            extra.write_text('keep')
            self.assertEqual(self.run_installer('--target', directory).returncode, 0)
            self.assertEqual(self.run_installer('--target', directory, '--uninstall').returncode, 0)
            self.assertEqual(extra.read_text(), 'keep')
            self.assertFalse((destination / 'SKILL.md').exists())

    def test_refuse_symlinked_scripts(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, 'linux-whole-system-file-search')
            external = Path(directory, 'external')
            external.mkdir()
            destination.mkdir()
            (destination / 'scripts').symlink_to(external, target_is_directory=True)
            self.assertNotEqual(self.run_installer('--target', directory).returncode, 0)
            self.assertEqual(list(external.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
