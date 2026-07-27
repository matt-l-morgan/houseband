"""Tests for launching a run through the HTTP API.

The launch path is a translation layer and nothing else: a JSON body becomes an
argv list for a detached child. That makes it exactly the kind of code that
breaks silently, because a parameter the server accepts but forgets to pass
produces a run that succeeds at the wrong thing. A producer who picks an 8-bar
clip and receives 16 bars has no way to tell whether the setting was ignored or
the composer disobeyed, and nothing downstream will ever flag it.

So these tests assert on the argv the child would actually receive, with Popen
stubbed out. No agent runs and no tokens are spent.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from houseband import server
from houseband.types import BAR_CHOICES, BARS_DEFAULT


class FakePopen:
    """Records the command it was handed and pretends to still be running."""

    last: list[str] | None = None

    def __init__(self, command, **kwargs):
        FakePopen.last = list(command)
        self.kwargs = kwargs
        self.pid = 4242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def send_signal(self, sig):
        return None


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client whose runs land in a temp directory and never spawn anything."""
    monkeypatch.setattr(server.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(server, "_runs_dir", lambda: tmp_path.resolve())
    FakePopen.last = None
    with TestClient(server.app) as test_client:
        yield test_client


def _argv_value(flag: str) -> str | None:
    command = FakePopen.last or []
    if flag not in command:
        return None
    return command[command.index(flag) + 1]


class TestClipLength:
    def test_the_chosen_length_reaches_the_child(self, client):
        response = client.post(
            "/api/runs", json={"prompt": "dub techno loop at 124", "bars": 8}
        )
        assert response.status_code == 200
        assert response.json()["bars"] == 8
        assert _argv_value("--bars") == "8"

    def test_omitting_it_still_pins_a_length_explicitly(self, client):
        """The default is passed, not left to the child.

        Relying on the child's own default means the number recorded in
        request.json and the number the composer was told could drift apart
        the moment either default changes.
        """
        client.post("/api/runs", json={"prompt": "boom bap loop"})
        assert _argv_value("--bars") == str(BARS_DEFAULT)

    @pytest.mark.parametrize("bars", BAR_CHOICES)
    def test_every_offered_length_is_accepted(self, client, bars):
        """The page builds its picker from BAR_CHOICES, so anything in that
        tuple has to be accepted or the UI offers an option that 422s."""
        response = client.post("/api/runs", json={"prompt": "loop", "bars": bars})
        assert response.status_code == 200, response.text

    @pytest.mark.parametrize("bars", [0, 7, 12, 15, 64, -16])
    def test_an_unsupported_length_is_refused(self, client, bars):
        """Rejected rather than rounded.

        A clip whose length is not a multiple of a 4-bar phrase does not loop
        cleanly, and the loop-usability judge would mark the composer down for
        our arithmetic. Silently snapping to the nearest legal value would hide
        that the request was not honoured.
        """
        response = client.post("/api/runs", json={"prompt": "loop", "bars": bars})
        assert response.status_code == 422
        assert "bars must be one of" in response.text

    def test_the_length_is_recorded_before_the_child_starts(self, client, tmp_path):
        """request.json is what makes a run listable if the child dies at once."""
        run_id = client.post(
            "/api/runs", json={"prompt": "halftime loop", "bars": 32}
        ).json()["run_id"]
        recorded = json.loads((tmp_path / run_id / "request.json").read_text())
        assert recorded["bars"] == 32
        assert recorded["prompt"] == "halftime loop"


class TestLengthChoicesServedToThePage:
    def test_the_page_is_offered_exactly_the_supported_lengths(self, client):
        lengths = client.get("/api/config").json()["lengths"]
        assert [entry["bars"] for entry in lengths] == list(BAR_CHOICES)

    def test_exactly_one_length_is_marked_default(self, client):
        lengths = client.get("/api/config").json()["lengths"]
        defaults = [entry["bars"] for entry in lengths if entry["default"]]
        assert defaults == [BARS_DEFAULT]

    def test_each_length_quotes_a_seconds_range(self, client):
        """A producer picks a length in seconds. Bars only mean something once
        you know the tempo, and the composer picks the tempo, so a range is the
        only honest thing to show."""
        for entry in client.get("/api/config").json()["lengths"]:
            assert 0 < entry["seconds_fast"] < entry["seconds_slow"]
            assert "seconds" in entry["note"]

    def test_longer_clips_are_reported_as_longer(self, client):
        lengths = client.get("/api/config").json()["lengths"]
        spans = [entry["seconds_fast"] for entry in lengths]
        assert spans == sorted(spans)


class TestOtherRunParameters:
    def test_an_empty_prompt_is_refused_before_anything_launches(self, client):
        response = client.post("/api/runs", json={"prompt": "   "})
        assert response.status_code == 400
        assert FakePopen.last is None

    def test_an_unknown_model_is_refused(self, client):
        """Caught here rather than as a 404 from the API three minutes in."""
        response = client.post(
            "/api/runs", json={"prompt": "loop", "model": "claude-nonexistent-9"}
        )
        assert response.status_code == 400

    def test_teams_and_rounds_reach_the_child(self, client):
        client.post("/api/runs", json={"prompt": "loop", "teams": 2, "rounds": 4})
        assert _argv_value("--teams") == "2"
        assert _argv_value("--rounds") == "4"

    def test_the_credential_is_never_placed_on_the_command_line(self, client, monkeypatch):
        """argv is world-readable in the process table. The key goes through the
        child environment or not at all."""
        monkeypatch.setitem(server._CREDENTIAL, "api_key", "sk-ant-probe-value")
        try:
            client.post("/api/runs", json={"prompt": "loop"})
            assert "sk-ant-probe-value" not in " ".join(FakePopen.last or [])
        finally:
            server._CREDENTIAL.pop("api_key", None)


class TestARunOutlivesTheServer:
    """A run is a detached child and the log is the only shared state, so a
    restarted server has to be able to adopt a pipeline it never launched.

    This is not a hypothetical. Restarting the server to pick up new code while a
    run was composing reported that run as ``interrupted`` with ``live: false``,
    because liveness was read out of the in-process table alone. The pipeline was
    fine; the UI said it was dead and offered a Cancel button that did nothing.
    """

    def _mid_run(self, tmp_path, run_id="20260101-000000-abcd"):
        """A run directory whose log stops mid-flight, with no terminal event."""
        run_dir = tmp_path / run_id
        run_dir.mkdir()
        (run_dir / "request.json").write_text(json.dumps({"prompt": "loop"}))
        (run_dir / "events.jsonl").write_text(
            json.dumps({"kind": "composer.thinking", "seq": 7, "message": "working"})
            + "\n"
        )
        return run_dir

    def test_a_live_pipeline_this_server_never_started_reads_as_running(
        self, client, tmp_path, monkeypatch
    ):
        run_dir = self._mid_run(tmp_path)
        (run_dir / "child.pid").write_text("31337\n")
        monkeypatch.setattr(
            server, "_pid_is_this_run", lambda pid, run_id: pid == 31337
        )

        body = client.get(f"/api/runs/{run_dir.name}/status").json()
        assert body["status"] == "running"
        assert body["live"] is True

    def test_a_dead_pipeline_still_reads_as_interrupted(
        self, client, tmp_path, monkeypatch
    ):
        """Adoption must not turn a stale pid file into a permanently 'running'
        run, or the UI would never admit a crash."""
        run_dir = self._mid_run(tmp_path)
        (run_dir / "child.pid").write_text("31337\n")
        monkeypatch.setattr(server, "_pid_is_this_run", lambda pid, run_id: False)

        body = client.get(f"/api/runs/{run_dir.name}/status").json()
        assert body["status"] == "interrupted"
        assert body["live"] is False

    def test_a_recycled_pid_is_not_mistaken_for_the_pipeline(self, tmp_path):
        """The pid check corroborates against the command line, because pids are
        reused and signalling a stranger is the one thing cancel must never do."""
        assert server._pid_is_this_run(os.getpid(), "20260101-000000-abcd") is False

    def test_the_launcher_records_the_pid(self, client, tmp_path):
        run_id = client.post("/api/runs", json={"prompt": "loop"}).json()["run_id"]
        assert (tmp_path / run_id / "child.pid").read_text().strip() == "4242"

    def test_an_adopted_run_can_still_be_cancelled(
        self, client, tmp_path, monkeypatch
    ):
        run_dir = self._mid_run(tmp_path)
        (run_dir / "child.pid").write_text("31337\n")
        monkeypatch.setattr(server, "_pid_is_this_run", lambda pid, run_id: pid == 31337)
        signalled: list[int] = []
        monkeypatch.setattr(server.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(server.os, "killpg", lambda pgid, sig: signalled.append(pgid))
        # The close-out watcher would poll a pid this test never created.
        monkeypatch.setattr(server, "_watch_adopted", lambda *a, **k: None)

        body = client.post(f"/api/runs/{run_dir.name}/cancel").json()
        assert body["cancelled"] is True
        assert signalled == [31337]

    def test_cancelling_a_finished_run_is_refused_not_signalled(
        self, client, tmp_path, monkeypatch
    ):
        run_dir = self._mid_run(tmp_path)
        (run_dir / "child.pid").write_text("31337\n")
        monkeypatch.setattr(server, "_pid_is_this_run", lambda pid, run_id: False)
        monkeypatch.setattr(
            server.os, "killpg", lambda *a: pytest.fail("signalled a dead run")
        )

        body = client.post(f"/api/runs/{run_dir.name}/cancel").json()
        assert body["cancelled"] is False


class TestPreviewsDoNotDoubleTheCards:
    """A preview and its judged counterpart are the same music.

    The loop renders a clip as soon as a composer finishes, so a producer has
    something to audition immediately, and renders it again under a blinded id for
    the panel. Listing both showed six cards for three takes with nothing marking
    which pair was a duplicate, which defeats the point of a browser whose only
    job is helping someone choose between takes.
    """

    def _run(self, tmp_path, run_id="20260101-000000-fold"):
        run_dir = tmp_path / run_id
        run_dir.mkdir()
        return run_dir

    def _write(self, run_dir, events):
        (run_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
        )

    def _preview(self, seq, team, round_no=1):
        return {
            "kind": "artifact.rendered",
            "seq": seq,
            "round": round_no,
            "team": team,
            "message": f"{team} is ready to play",
            "data": {
                "candidate_id": f"preview-{team}-r{round_no}",
                "preview": True,
                "audio": f"round{round_no}/{team}/preview.oga",
                "midi": f"round{round_no}/{team}/out.mid",
                "daw_bundle": f"round{round_no}/{team}/daw/{team}.zip",
            },
        }

    def _judged(self, seq, team, cid, round_no=1):
        return {
            "kind": "artifact.rendered",
            "seq": seq,
            "round": round_no,
            "team": team,
            "data": {
                "candidate_id": cid,
                "audio": f"round{round_no}/artifacts/{cid}.oga",
                "midi": f"round{round_no}/{team}/out.mid",
            },
        }

    # round_no before dimension so a positional call matches _judged's shape. The
    # other order meant `_verdict(3, "arena", "r1c1", 1)` set the *dimension* to 1
    # and left the round at its default, which put a round-2 score on a round-1
    # key and made the fold look broken when it was not.
    def _verdict(self, seq, team, cid, round_no=1, dimension="rhythm_groove"):
        return {
            "kind": "judge.verdict",
            "seq": seq,
            "round": round_no,
            "team": team,
            "dimension": dimension,
            "data": {"candidate_id": cid, "dimension": dimension, "score": 7},
        }

    def test_three_takes_produce_three_cards(self, client, tmp_path):
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "arena"), self._preview(2, "crate"),
            self._judged(3, "arena", "r1c1"), self._judged(4, "crate", "r1c2"),
            self._verdict(5, "arena", "r1c1"), self._verdict(6, "crate", "r1c2"),
        ])
        body = client.get(f"/api/runs/{run_dir.name}/candidates").json()
        assert [c["candidate_id"] for c in body["candidates"]] == ["r1c1", "r1c2"]

    def test_the_judged_id_is_the_one_that_survives(self, client, tmp_path):
        """It is the id the panel, the Elo table and the coach all use."""
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "arena"),
            self._judged(2, "arena", "r1c1"),
            self._verdict(3, "arena", "r1c1"),
        ])
        card = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"][0]
        assert card["candidate_id"] == "r1c1"
        assert card["superseded_ids"] == ["preview-arena-r1"]

    def test_a_preview_is_listed_while_it_is_the_only_render(self, client, tmp_path):
        """Before judging it is the only thing there is to play, and hiding it
        would leave a finished composer with nothing to audition."""
        run_dir = self._run(tmp_path)
        self._write(run_dir, [self._preview(1, "arena")])
        cards = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"]
        assert [c["candidate_id"] for c in cards] == ["preview-arena-r1"]
        assert cards[0]["preview"] is True

    def test_folding_does_not_wait_for_the_scores(self, client, tmp_path):
        """The blinded render is the same music the moment it exists.

        Gating the fold on scores meant that during judging a round showed both
        the preview and its blinded twin, so three takes read as five or six
        cards and only settled once the final verdict arrived. That is the window
        a producer is actually looking at the page.
        """
        run_dir = self._run(tmp_path)
        self._write(run_dir, [self._preview(1, "arena"), self._judged(2, "arena", "r1c1")])
        cards = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"]
        assert [c["candidate_id"] for c in cards] == ["r1c1"]
        # And it is playable, because the blinded event's own audio is there.
        assert cards[0]["artifacts"]["audio"] == "round1/artifacts/r1c1.oga"

    def test_a_scored_take_wins_the_card_over_an_unscored_one(self, client, tmp_path):
        """Only reachable in odd logs, but the card should carry the verdicts."""
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "arena"),
            self._judged(2, "arena", "r1c1"),
            self._judged(3, "arena", "r1c9"),
            self._verdict(4, "arena", "r1c9"),
        ])
        cards = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"]
        scored = [c for c in cards if c["superseded_ids"]]
        assert [c["candidate_id"] for c in scored] == ["r1c9"]

    def test_the_daw_bundle_from_the_preview_survives_the_fold(self, client, tmp_path):
        """The judged event never mentions the bundle, so folding is the only way
        the download reaches the card that is actually drawn."""
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "crate"),
            self._judged(2, "crate", "r1c2"),
            self._verdict(3, "crate", "r1c2"),
        ])
        card = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"][0]
        assert card["artifacts"]["daw_bundle"] == "round1/crate/daw/crate.zip"

    def test_the_judged_render_wins_where_both_have_one(self, client, tmp_path):
        """The scores on the card describe the judged render, so that is the audio
        the card has to play."""
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "crate"),
            self._judged(2, "crate", "r1c2"),
            self._verdict(3, "crate", "r1c2"),
        ])
        card = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"][0]
        assert card["artifacts"]["audio"] == "round1/artifacts/r1c2.oga"

    def test_feedback_left_on_the_preview_follows_the_take(self, client, tmp_path):
        """The signal the coach values most, and the easiest to lose.

        A producer rates a clip the moment it finishes rendering. If that rating
        vanished when judging caught up, the loop would train on nothing while
        appearing to work.
        """
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "crate"),
            self._judged(2, "crate", "r1c2"),
            self._verdict(3, "crate", "r1c2"),
        ])
        (run_dir / "feedback.jsonl").write_text(
            json.dumps({
                "candidate_id": "preview-crate-r1", "round": 1, "team": "crate",
                "verdict": "keep", "kept_tracks": ["drums"], "note": "kit is usable",
            }) + "\n", encoding="utf-8",
        )
        card = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"][0]
        assert card["feedback_count"] == 1
        assert card["feedback"]["verdict"] == "keep"

    def test_rating_a_preview_id_still_validates_after_it_is_folded(
        self, client, tmp_path
    ):
        """A page loaded mid-composition holds preview ids. Rating a take must not
        start failing because judging happened to finish first."""
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "crate"),
            self._judged(2, "crate", "r1c2"),
            self._verdict(3, "crate", "r1c2"),
        ])
        response = client.post(
            f"/api/runs/{run_dir.name}/feedback",
            json={"candidate_id": "preview-crate-r1", "verdict": "maybe"},
        )
        assert response.status_code == 200, response.text

    def test_the_card_keeps_its_position_when_scores_land(self, client, tmp_path):
        """The take upgrades in place rather than jumping to the end of the row.

        A producer comparing takes should not have the row reorder under them
        because one composer's scores arrived first.
        """
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "arena"), self._preview(2, "crate"),
            # crate is judged first, but arena previewed first.
            self._judged(3, "crate", "r1c2"), self._verdict(4, "crate", "r1c2"),
            self._judged(5, "arena", "r1c1"), self._verdict(6, "arena", "r1c1"),
        ])
        cards = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"]
        assert [c["team"] for c in cards] == ["arena", "crate"]

    def test_a_take_absorbs_only_its_own_preview(self, client, tmp_path):
        """Previews are matched to takes by team and round, so a card must never
        absorb a preview belonging to a different composer."""
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "arena"),
            self._judged(2, "arena", "r1c1"), self._verdict(3, "arena", "r1c1"),
        ])
        index = server._candidate_index(run_dir)
        arena = next(e for e in index.values() if e["candidate_id"] == "r1c1")
        assert arena["superseded_ids"] == ["preview-arena-r1"]

    def test_rounds_do_not_fold_into_each_other(self, client, tmp_path):
        """Round 2's take is a different piece of music, not an upgrade of round 1's."""
        run_dir = self._run(tmp_path)
        self._write(run_dir, [
            self._preview(1, "arena", 1),
            self._judged(2, "arena", "r1c1", 1), self._verdict(3, "arena", "r1c1", 1),
            self._preview(4, "arena", 2),
            self._judged(5, "arena", "r2c1", 2), self._verdict(6, "arena", "r2c1", 2),
        ])
        cards = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"]
        assert [(c["candidate_id"], c["round"]) for c in cards] == [
            ("r1c1", 1), ("r2c1", 2)
        ]


class TestFindingsComeFromDisk:
    """The event log records a finding count, not the findings.

    ``judge.verdict`` carries a score and ``findings: 3``, because one round of
    full findings is over 100KB and the log is replayed in full on every page
    load. The text lives in ``round<N>/verdicts.json``. Nothing merged the two,
    so a card had a score and nothing else: no rationale, no bar-anchored claim,
    no suggested revision. That is the entire substance of the feedback loop, and
    the only reason to look at a verdict at all.
    """

    def _run_with_verdicts(self, tmp_path, run_id="20260101-000000-find"):
        run_dir = tmp_path / run_id
        (run_dir / "round1").mkdir(parents=True)
        (run_dir / "events.jsonl").write_text(
            "\n".join(
                json.dumps(e)
                for e in [
                    {
                        "kind": "artifact.rendered", "seq": 1, "round": 1, "team": "crate",
                        "data": {"candidate_id": "r1c1", "audio": "round1/artifacts/r1c1.oga"},
                    },
                    {
                        "kind": "judge.verdict", "seq": 2, "round": 1, "team": "crate",
                        "dimension": "melody",
                        # A count, exactly as the pipeline writes it.
                        "data": {"candidate_id": "r1c1", "dimension": "melody",
                                 "score": 2, "findings": 1},
                    },
                ]
            ) + "\n",
            encoding="utf-8",
        )
        (run_dir / "round1" / "verdicts.json").write_text(
            json.dumps({
                "round": 1,
                "verdicts": {
                    "r1c1": {
                        "candidate_id": "r1c1", "team": "crate",
                        "dimensions": [{
                            "dimension": "melody", "score": 2, "samples": [2, 2, 3],
                            "rationale": "The lead never states a single-note line.",
                            "findings": [{
                                "claim": "saw_lead strikes block chords, not a melody.",
                                "bar_start": 0, "bar_end": 15, "track": "saw_lead",
                                "severity": "major",
                                "suggested_revision": "Pick one note per chord.",
                            }],
                        }],
                    }
                },
            }),
            encoding="utf-8",
        )
        return run_dir

    def _melody(self, client, run_dir):
        card = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"][0]
        return next(s for s in card["scores"] if s["dimension"] == "melody")

    def test_the_finding_text_reaches_the_card(self, client, tmp_path):
        run_dir = self._run_with_verdicts(tmp_path)
        melody = self._melody(client, run_dir)
        assert len(melody["finding_list"]) == 1
        finding = melody["finding_list"][0]
        assert finding["claim"].startswith("saw_lead strikes block chords")
        assert finding["suggested_revision"] == "Pick one note per chord."
        assert (finding["bar_start"], finding["bar_end"]) == (0, 15)
        assert finding["severity"] == "major"

    def test_the_rationale_reaches_the_card(self, client, tmp_path):
        """Absent from the event log entirely, so this only works off disk."""
        run_dir = self._run_with_verdicts(tmp_path)
        assert self._melody(client, run_dir)["rationale"].startswith("The lead never")

    def test_the_count_matches_the_list(self, client, tmp_path):
        """The count is what the card's badge shows, so it has to agree with the
        text underneath it."""
        run_dir = self._run_with_verdicts(tmp_path)
        melody = self._melody(client, run_dir)
        assert melody["findings"] == len(melody["finding_list"])

    def test_a_track_named_only_in_a_finding_reaches_the_track_list(
        self, client, tmp_path
    ):
        """The per-stem keep/discard controls need names. With no sidecar the
        findings are the only place they appear."""
        run_dir = self._run_with_verdicts(tmp_path)
        card = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"][0]
        assert [t["name"] for t in card["tracks"]] == ["saw_lead"]
        assert card["tracks_from"] == "judge findings"

    def test_a_missing_verdicts_file_is_not_an_error(self, client, tmp_path):
        """A run killed mid-judging has scores in the log and no file yet."""
        run_dir = tmp_path / "20260101-000000-nofile"
        run_dir.mkdir()
        (run_dir / "events.jsonl").write_text(
            json.dumps({
                "kind": "judge.verdict", "seq": 1, "round": 1, "team": "crate",
                "dimension": "melody",
                "data": {"candidate_id": "r1c1", "dimension": "melody", "score": 4},
            }) + "\n",
            encoding="utf-8",
        )
        card = client.get(f"/api/runs/{run_dir.name}/candidates").json()["candidates"][0]
        melody = next(s for s in card["scores"] if s["dimension"] == "melody")
        assert melody["score"] == 4
        assert melody.get("finding_list", []) == []

    def test_a_corrupt_verdicts_file_does_not_take_the_card_down(
        self, client, tmp_path
    ):
        run_dir = self._run_with_verdicts(tmp_path)
        (run_dir / "round1" / "verdicts.json").write_text("{not json", encoding="utf-8")
        response = client.get(f"/api/runs/{run_dir.name}/candidates")
        assert response.status_code == 200
        assert response.json()["candidates"][0]["scores"][0]["score"] == 2


class TestLegacyReferenceEntries:
    """Runs recorded before references were removed still have one on disk.

    The reference was a judging control rather than a take on offer, and the loop
    rendered it outside the candidate artifact path, so its card had nothing to
    play and no track list. It stays out of the listing so an old run reads the
    same as a new one.
    """

    def _run(self, tmp_path, candidate_id, team, run_id="20260101-000000-old"):
        run_dir = tmp_path / run_id
        run_dir.mkdir()
        (run_dir / "events.jsonl").write_text(
            "\n".join(
                json.dumps(e)
                for e in [
                    {
                        "kind": "artifact.rendered", "seq": 1, "round": 1, "team": "crate",
                        "data": {"candidate_id": "r1c1", "audio": "round1/artifacts/r1c1.oga"},
                    },
                    {
                        "kind": "judge.verdict", "seq": 2, "round": 1, "team": "crate",
                        "dimension": "melody",
                        "data": {"candidate_id": "r1c1", "dimension": "melody", "score": 7},
                    },
                    {
                        "kind": "judge.verdict", "seq": 3, "round": 1, "team": team,
                        "dimension": "melody",
                        "data": {"candidate_id": candidate_id, "dimension": "melody", "score": 6},
                    },
                ]
            ) + "\n",
            encoding="utf-8",
        )
        return run_dir

    def _ids(self, client, run_dir):
        body = client.get(f"/api/runs/{run_dir.name}/candidates").json()
        return [c["candidate_id"] for c in body["candidates"]]

    def test_a_legacy_reference_take_is_not_listed(self, client, tmp_path):
        run_dir = self._run(tmp_path, "r1ref", "reference")
        assert self._ids(client, run_dir) == ["r1c1"]

    def test_it_is_recognised_by_team_as_well_as_by_id(self, client, tmp_path):
        """Older runs used a bare ``ref``; the team attribution is the reliable
        signal and the id pattern is the fallback."""
        run_dir = self._run(tmp_path, "ref", "reference")
        assert self._ids(client, run_dir) == ["r1c1"]

    def test_a_real_candidate_whose_id_ends_in_ref_is_kept(self, client, tmp_path):
        """The regression this exists for.

        The first version of this exclusion tested ``endswith("ref")``, which also
        swallowed ordinary candidates. A take a producer could have used simply
        stopped appearing, with nothing to indicate why.
        """
        run_dir = self._run(tmp_path, "cref", "crate")
        assert self._ids(client, run_dir) == ["r1c1", "cref"]
