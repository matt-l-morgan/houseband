"""Tests for the event log.

The scrubbing tests are the important ones. Users run this with their own
credential, and the event log is the file they are most likely to attach to a bug
report or paste into a chat, so a leaked key here is not a recoverable mistake.
Scrubbing is belt and braces over "the pipeline does not put keys in events" for
exactly that reason.

The tailing tests matter for a different reason: the web UI is a pure reader of
this file, so a reader that dies on a partially flushed line would make every
interrupted run unreadable.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from houseband.events import (
    REDACTED,
    Event,
    EventLog,
    Usage,
    read_events,
    scrub,
    tail_events,
)


class TestScrubbing:
    @pytest.mark.parametrize(
        "secret",
        [
            "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "sk-ant-oat01-BBBBBBBBBBBBBBBBBBBBBBBB",
            "sk-proj-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
            "whsec_DDDDDDDDDDDDDDDDDDDD",
            "ghp_EEEEEEEEEEEEEEEEEEEEEEEEEEEE",
            "github_pat_FFFFFFFFFFFFFFFFFFFFFFFF",
        ],
    )
    def test_known_credential_shapes_are_removed(self, secret):
        assert secret not in scrub(f"the key is {secret} ok")
        assert REDACTED in scrub(f"the key is {secret} ok")

    def test_bearer_tokens_are_removed(self):
        out = scrub("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
        assert "abcdefghijklmnop" not in out

    def test_sensitive_keys_are_dropped_by_name(self):
        """Regardless of the value's shape: a key stored under `api_key` should
        never survive just because it does not match a known prefix."""
        out = scrub({"api_key": "anything at all", "prompt": "keep me"})
        assert out["api_key"] == REDACTED
        assert out["prompt"] == "keep me"

    def test_scrubbing_recurses(self):
        out = scrub(
            {
                "rounds": [
                    {"token": "abc"},
                    {"nested": {"authorization": "xyz", "fine": "kept"}},
                ]
            }
        )
        assert out["rounds"][0]["token"] == REDACTED
        assert out["rounds"][1]["nested"]["authorization"] == REDACTED
        assert out["rounds"][1]["nested"]["fine"] == "kept"

    def test_ordinary_content_is_untouched(self):
        """Over-aggressive scrubbing would corrupt musical content, which is most
        of what these events carry."""
        text = 'gtr.chord(bar, 1, symbol="F#m9", dur=3.5, vel=58)'
        assert scrub(text) == text
        assert scrub({"bars": [4, 8], "track": "bass"}) == {"bars": [4, 8], "track": "bass"}

    def test_non_string_scalars_pass_through(self):
        assert scrub(7) == 7
        assert scrub(None) is None
        assert scrub(True) is True


class TestWriting:
    def test_emit_appends_jsonl(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.emit("run.started", "hello", round=1, team="crate", extra="value")
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["kind"] == "run.started"
        assert event["round"] == 1
        assert event["team"] == "crate"
        assert event["data"]["extra"] == "value"
        assert event["seq"] == 1

    def test_seq_increments_monotonically(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        for i in range(20):
            log.emit("warning", f"m{i}")
        events = read_events(tmp_path / "events.jsonl")
        assert [e.seq for e in events] == list(range(1, 21))

    def test_seq_is_unique_under_concurrency(self, tmp_path):
        """Composers run in a thread pool and all write to one log, so seq has to
        be assigned under the same lock as the write or the UI cannot dedupe."""
        log = EventLog(tmp_path / "events.jsonl")

        def worker(n):
            for i in range(25):
                log.emit("warning", f"t{n}-{i}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = read_events(tmp_path / "events.jsonl")
        assert len(events) == 150
        assert len({e.seq for e in events}) == 150
        assert sorted(e.seq for e in events) == list(range(1, 151))

    def test_message_and_data_are_both_scrubbed(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.emit(
            "warning",
            "failed with sk-ant-api03-SECRETVALUEHERE1234",
            api_key="sk-ant-oat01-ANOTHERSECRET999",
        )
        text = (tmp_path / "events.jsonl").read_text()
        assert "SECRETVALUEHERE" not in text
        assert "ANOTHERSECRET" not in text
        assert REDACTED in text

    def test_usage_accumulates_across_events(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.emit("judge.verdict", "a", usage=Usage(input_tokens=100, output_tokens=50))
        log.emit("judge.verdict", "b", usage=Usage(input_tokens=200, output_tokens=25))
        assert log.total_usage.input_tokens == 300
        assert log.output_tokens == 75

    def test_events_without_usage_do_not_disturb_the_total(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.emit("judge.verdict", "a", usage=Usage(output_tokens=10))
        log.emit("warning", "no usage here")
        assert log.output_tokens == 10


class TestUsage:
    def test_from_response_tolerates_a_missing_usage_block(self):
        class Bare:
            pass

        assert Usage.from_response(Bare()).output_tokens == 0

    def test_from_response_tolerates_missing_fields(self):
        class Partial:
            class usage:
                output_tokens = 42

        got = Usage.from_response(Partial())
        assert got.output_tokens == 42
        assert got.cache_read_input_tokens == 0

    def test_addition(self):
        total = Usage(input_tokens=1, output_tokens=2, cache_read_input_tokens=3) + Usage(
            input_tokens=10, output_tokens=20, cache_creation_input_tokens=5
        )
        assert total.input_tokens == 11
        assert total.output_tokens == 22
        assert total.cache_read_input_tokens == 3
        assert total.cache_creation_input_tokens == 5


class TestReading:
    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert read_events(tmp_path / "nope.jsonl") == []

    def test_malformed_lines_are_skipped(self, tmp_path):
        """A run killed mid-write leaves a partial final line, and a reader that
        died on it would make every interrupted run unreadable."""
        path = tmp_path / "events.jsonl"
        good = Event(kind="warning", message="fine", seq=1).model_dump_json()
        path.write_text(f"{good}\n" "not json at all\n" '{"kind": "trunca\n')
        events = read_events(path)
        assert len(events) == 1
        assert events[0].message == "fine"

    def test_blank_lines_are_ignored(self, tmp_path):
        path = tmp_path / "events.jsonl"
        good = Event(kind="warning", message="fine", seq=1).model_dump_json()
        path.write_text(f"\n{good}\n\n\n")
        assert len(read_events(path)) == 1


class TestTailing:
    def test_tail_yields_appended_events(self, tmp_path):
        path = tmp_path / "events.jsonl"
        log = EventLog(path)
        log.emit("run.started", "go")

        seen: list[Event] = []

        def reader():
            for event in tail_events(path, poll_interval=0.02, stop_after_idle=2.0):
                seen.append(event)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.1)
        log.emit("round.started", "r1")
        log.emit("run.finished", "done")
        thread.join(timeout=5)

        kinds = [e.kind for e in seen]
        assert "run.started" in kinds
        assert "round.started" in kinds
        assert kinds[-1] == "run.finished"

    def test_tail_stops_on_a_terminal_event(self, tmp_path):
        """The SSE endpoint depends on this: a stream that never closed would
        leak a thread per page load."""
        path = tmp_path / "events.jsonl"
        log = EventLog(path)
        log.emit("run.started", "go")
        log.emit("run.failed", "boom")

        collected = list(tail_events(path, poll_interval=0.01, stop_after_idle=2.0))
        assert collected[-1].kind == "run.failed"

    def test_from_seq_skips_already_seen_events(self, tmp_path):
        """Reconnect support: the UI passes the highest seq it has, and must not
        get duplicates."""
        path = tmp_path / "events.jsonl"
        log = EventLog(path)
        for i in range(5):
            log.emit("warning", f"m{i}")
        log.emit("run.finished", "done")

        collected = list(tail_events(path, from_seq=3, poll_interval=0.01, stop_after_idle=2.0))
        assert [e.seq for e in collected] == [4, 5, 6]

    def test_partial_final_line_is_not_consumed_early(self, tmp_path):
        """Only complete lines advance the read offset, otherwise a half-flushed
        event would be dropped rather than picked up on the next poll."""
        path = tmp_path / "events.jsonl"
        good = Event(kind="warning", message="one", seq=1).model_dump_json()
        path.write_text(f"{good}\n" + '{"kind":"warning","seq":2,"mess')

        seen: list[Event] = []

        def reader():
            for event in tail_events(path, poll_interval=0.02, stop_after_idle=1.0):
                seen.append(event)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.15)
        # Complete the truncated line, then terminate the stream.
        with path.open("a") as handle:
            handle.write('age":"two"}\n')
            handle.write(Event(kind="run.finished", message="done", seq=3).model_dump_json() + "\n")
        thread.join(timeout=5)

        assert [e.seq for e in seen] == [1, 2, 3]
        assert seen[1].message == "two"
