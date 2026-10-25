import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from telemetry_transport import PREFIX, EventBoundaryWriter, framed_child, preserve_failure, split_events


def event(sequence, **values):
    return PREFIX+json.dumps({'event_sequence':sequence, 'run_id':'run', **values}, ensure_ascii=False)


class TransportTests(unittest.TestCase):
    def test_event_embedded_in_navigation_text_is_extracted(self):
        events, text = split_events('navigation: '+event(534)+' accepted')
        self.assertEqual(events, [event(534)])
        self.assertEqual(text, 'navigation:  accepted')

    def test_multiple_events_and_unicode_share_one_line(self):
        events, text = split_events('回退 '+event(1)+event(2)+' 完成')
        self.assertEqual(events, [event(1),event(2)])
        self.assertEqual(text, '回退  完成')

    def test_marker_in_json_string_does_not_create_extra_event(self):
        original = event(1, evidence=PREFIX+'不是新事件')
        self.assertEqual(split_events(original), ([original], ''))

    def test_corrupt_json_is_forwarded_for_strict_recorder_failure(self):
        self.assertEqual(split_events('log '+PREFIX+'{"broken":'), ([PREFIX+'{"broken":'], 'log '))

    def test_plain_logs_never_become_events(self):
        self.assertEqual(split_events('navigation done'), ([], 'navigation done'))

    def test_gap_and_duplicate_sequences_are_not_rewritten(self):
        frames, _ = split_events(event(1)+event(3)+event(3))
        self.assertEqual([json.loads(f[len(PREFIX):])['event_sequence'] for f in frames], [1,3,3])

    def test_boundary_writer_separates_unfinished_log_from_event(self):
        output = io.StringIO()
        writer = EventBoundaryWriter(output)
        writer.write('navigation: ')
        self.assertEqual(writer.write(event(1)+'\n'), len(event(1)+'\n'))
        writer.write('sent\n')
        lines = output.getvalue().splitlines()
        self.assertEqual(lines, ['navigation: ',event(1),'sent'])

    def test_relay_preserves_raw_log_and_delivers_embedded_frames(self):
        child = framed_child(object).__new__(framed_child(object))
        raw = ('navigation '+event(1)+' accepted\r\n'+event(2)+'\n').encode()
        child.process = SimpleNamespace(stdout=io.BytesIO(raw))
        child.log = io.StringIO()
        child.label = 'lower'
        received = []
        child.on_line = received.append
        with contextlib.redirect_stdout(io.StringIO()) as human:
            child.relay()
        self.assertEqual(received, [event(1),event(2)])
        self.assertIn('navigation '+event(1), child.log.getvalue())
        self.assertIn('navigation  accepted', human.getvalue())
        self.assertTrue(child.process.stdout.closed)

    def test_concurrent_navigation_chatter_does_not_lose_1000_events(self):
        output = io.StringIO()
        writer = EventBoundaryWriter(output)
        def chatter():
            for _ in range(1000):
                writer.write('nav ')
                time.sleep(0)
                writer.write('accepted\n')
        def telemetry():
            for sequence in range(1,1001):
                writer.write(event(sequence)+'\n')
                time.sleep(0)
        workers = [threading.Thread(target=fn) for fn in (chatter,telemetry)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        frames = [f for line in output.getvalue().splitlines() for f in split_events(line)[0]]
        self.assertEqual([json.loads(f[len(PREFIX):])['event_sequence'] for f in frames], list(range(1,1001)))

    def test_failed_logs_survive_cleanup_without_copying_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root/'pending'
            (pending/'logs').mkdir(parents=True)
            (pending/'images').mkdir()
            (pending/'logs/lower.log').write_text('raw frame')
            (pending/'images/test.jpg').write_bytes(b'image')
            stream = (pending/'events.jsonl').open('w')
            try:
                stream.write('{"event_sequence":1}\n')
                session = SimpleNamespace(run_id='run',fatal='sequence gap',directory=pending,
                    lock=threading.RLock(),events=[{'event_sequence':1}],events_stream=stream)
                preserve_failure(session, 1, root/'diagnostics')
                status = json.loads((root/'diagnostics/run/failure.json').read_text())
                self.assertFalse(status['formal_success_result'])
                self.assertEqual(status['next_expected_sequence'], 2)
                self.assertEqual((root/'diagnostics/run/events.jsonl').read_text(),'{"event_sequence":1}\n')
                self.assertTrue((root/'diagnostics/run/logs/lower.log').exists())
                self.assertFalse((root/'diagnostics/run/images').exists())
            finally:
                stream.close()

    def test_successful_recording_is_left_on_original_save_path(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/'diagnostics'
            preserve_failure(SimpleNamespace(fatal=None), 0, destination)
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
