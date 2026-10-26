"""Preserve complete telemetry frames when ROS/navigation text shares SSH stdout."""
import json
from pathlib import Path
import shutil
import threading

PREFIX = '__ELEVATOR_EVENT__'


class EventBoundaryWriter:
    """Start each event on a fresh line, even if another print has no newline."""
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.RLock()

    def write(self, text):
        with self.lock:
            self.stream.write(('\n' if text.startswith(PREFIX) else '') + text)
        return len(text)

    def flush(self):
        with self.lock:
            self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def split_events(line):
    """Extract complete marked JSON objects without altering sequences or payloads.

    Invalid marked JSON is forwarded to the existing strict recorder to fail;
    missing, corrupt or duplicated events are never invented or renumbered.
    """
    decoder = json.JSONDecoder()
    events, text = [], []
    cursor = 0
    while True:
        marker = line.find(PREFIX, cursor)
        if marker < 0:
            text.append(line[cursor:])
            break
        text.append(line[cursor:marker])
        start = marker + len(PREFIX)
        start += len(line[start:]) - len(line[start:].lstrip())
        try:
            _, end = decoder.raw_decode(line, start)
        except (ValueError, TypeError):
            events.append(PREFIX + line[start:])
            break
        events.append(PREFIX + line[start:end])
        cursor = end
    return events, ''.join(text)


def framed_child(base):
    class FramedChild(base):
        def relay(self):
            try:
                for raw in iter(self.process.stdout.readline, b''):
                    line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                    self.log.write(line+'\n')
                    events, ordinary = split_events(line)
                    if self.on_line is not None:
                        for event in events:
                            self.on_line(event)
                    if ordinary.strip():
                        print('['+self.label+'] '+ordinary, flush=True)
            finally:
                self.process.stdout.close()
    return FramedChild


def preserve_failure(session, exit_code, destination):
    """Keep diagnostics outside formal results before the old recorder cleans up."""
    if exit_code == 0 and not session.fatal:
        return
    directory = Path(destination)/session.run_id
    with session.lock:
        directory.mkdir(parents=True, exist_ok=True)
        for name in ['events_stream', 'observations_stream', 'timing_stream']:
            stream = getattr(session, name, None)
            if stream is not None and not stream.closed:
                stream.flush()
        for name in ['run.json', 'events.jsonl', 'observations.jsonl', 'perception_timing.jsonl']:
            path = session.directory/name
            if path.is_file():
                shutil.copy2(path, directory/name)
        if (session.directory/'logs').is_dir():
            shutil.copytree(session.directory/'logs', directory/'logs', dirs_exist_ok=True)
        status = {'run_id':session.run_id, 'exit_code':exit_code,
                  'recorder_error':session.fatal, 'recorded_event_count':len(session.events),
                  'next_expected_sequence':len(session.events)+1,
                  'last_event_sequences':[e.get('event_sequence') for e in session.events[-10:]],
                  'formal_success_result':False, 'images_copied':False}
        (directory/'failure.json').write_text(json.dumps(status, ensure_ascii=False, indent=2)+'\n')
