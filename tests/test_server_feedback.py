"""Producer feedback and the variation browser's data source.

These exercise the endpoints the same way the page does: through the real ASGI
app, against a real run directory on disk. The point of an event-sourced pipeline
is that the log is the interface, so a fixture written through the real
:class:`EventLog` proves something about the contract, whereas a mocked reader
would only prove the mock works.

Two properties here are worth more than the rest:

* an unknown ``candidate_id`` is refused, because a feedback record no artifact
  backs would end up in the coach's evidence as an unfalsifiable claim
* the credential endpoints still never echo a key, which is a rule this module's
  new neighbours must not have quietly broken
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from houseband import config as cfg
from houseband import server
from houseband.events import EventLog, read_events
from houseband.types import DIMENSION_TITLES

FAKE_KEY = "sk-ant-api03-notarealkeybutshapedlikeone0000000000"


@pytest.fixture
def runs_root(tmp_path, monkeypatch):
    """Point the server's runs directory at a temp dir for the whole test."""
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    config = cfg.Config(
        runs_dir=root,
        playbooks_dir=tmp_path / "playbooks",
    )
    config.playbooks_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "load", lambda: config)
    return root


@pytest.fixture
def client(runs_root):
    return TestClient(server.app)


def _write_run(runs_root, run_id="demo"):
    """A two-team round with one preview, one judged pair, and a sidecar.

    Written through EventLog so sequence numbers, framing and scrubbing are the
    same code the pipeline uses.
    """
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = EventLog(run_dir / "events.jsonl")
    log.emit("run.started", "two teams", teams=["carbide", "lumen"], rounds=1)
    log.emit("round.started", "Round 1", round=1)

    # A per-team preview, rendered before the pool is blinded.
    (run_dir / "round1" / "carbide").mkdir(parents=True, exist_ok=True)
    log.emit(
        "artifact.rendered",
        "carbide is ready to play",
        round=1,
        team="carbide",
        candidate_id="preview-carbide-r1",
        preview=True,
        audio="round1/carbide/preview.oga",
        piano_roll="round1/carbide/preview.png",
        midi="round1/carbide/out.mid",
    )

    for slot, team in ((1, "carbide"), (2, "lumen")):
        candidate_id = f"r1c{slot}"
        team_dir = run_dir / "round1" / team
        team_dir.mkdir(parents=True, exist_ok=True)
        (team_dir / "out.mid").write_bytes(b"not really midi")
        (team_dir / "out.score.json").write_text(
            json.dumps(
                {
                    "key": "Am",
                    "time_sig": [4, 4],
                    "tempo_map": [[0, 124.0]],
                    "total_bars": 16,
                    "duration": 31.0,
                    "sections": [{"name": "loop", "start_bar": 0, "bars": 16}],
                    "tracks": [
                        {"name": "drums", "patch": 0, "is_drum": True, "note_count": 128},
                        {"name": "bass", "patch": 38, "is_drum": False, "note_count": 64},
                        {"name": "pad", "patch": 89, "is_drum": False, "note_count": 32},
                    ],
                }
            ),
            encoding="utf-8",
        )
        log.emit(
            "artifact.rendered",
            f"{candidate_id} artifacts ready",
            round=1,
            team=team,
            candidate_id=candidate_id,
            audio=f"round1/artifacts/{candidate_id}.oga",
            piano_roll=f"round1/artifacts/{candidate_id}.png",
            midi=f"round1/{team}/out.mid",
            program=f"round1/{team}/program.py",
        )
        log.emit(
            "gate.passed",
            "Valid: 3 tracks.",
            round=1,
            team=team,
            candidate_id=candidate_id,
        )
        log.emit(
            "judge.verdict",
            f"Melody: {slot + 5}/10 for {candidate_id}",
            round=1,
            dimension="melody",
            candidate_id=candidate_id,
            score=slot + 5,
            samples=[slot + 5],
            spread=0,
            rationale="Contour is fine.",
            findings=[{"claim": "pads are muddy", "track": "pad", "severity": "minor"}],
        )

    log.emit("run.finished", "done")
    return run_dir


def _post(client, run_id, **body):
    payload = {"candidate_id": "r1c1", "verdict": "keep"}
    payload.update(body)
    return client.post(f"/api/runs/{run_id}/feedback", json=payload)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


class TestFeedbackRoundTrip:
    def test_post_then_get_returns_what_was_recorded(self, client, runs_root):
        _write_run(runs_root)
        response = _post(
            client,
            "demo",
            round=1,
            kept_tracks=["drums", "bass"],
            discarded_tracks=["pad"],
            note="drums are usable as-is",
        )
        assert response.status_code == 200, response.text
        recorded = response.json()["recorded"]
        assert recorded["verdict"] == "keep"
        assert recorded["kept_tracks"] == ["drums", "bass"]
        assert recorded["discarded_tracks"] == ["pad"]
        # Team is filled in from the log, so the page does not have to be right
        # about it for the record to be attributable.
        assert recorded["team"] == "carbide"

        stored = client.get("/api/runs/demo/feedback").json()
        assert stored["count"] == 1
        assert stored["feedback"][0]["note"] == "drums are usable as-is"
        assert stored["feedback"][0]["recorded_at"]

    def test_the_file_is_append_only(self, client, runs_root):
        run_dir = _write_run(runs_root)
        _post(client, "demo", round=1, verdict="keep")
        _post(client, "demo", round=1, verdict="discard", note="changed my mind")

        lines = (run_dir / "feedback.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert [json.loads(line)["verdict"] for line in lines] == ["keep", "discard"]

        # The candidates endpoint shows the current opinion, and says how many
        # times it was recorded rather than pretending there was only ever one.
        card = _card(client, "demo", "r1c1")
        assert card["feedback"]["verdict"] == "discard"
        assert card["feedback_count"] == 2

    def test_track_names_are_stripped_and_deduped(self, client, runs_root):
        _write_run(runs_root)
        response = _post(
            client, "demo", round=1, kept_tracks=[" drums ", "drums", "", "bass"]
        )
        assert response.json()["recorded"]["kept_tracks"] == ["drums", "bass"]

    def test_unknown_candidate_is_rejected(self, client, runs_root):
        _write_run(runs_root)
        response = _post(client, "demo", candidate_id="r9c9")
        assert response.status_code == 400
        assert "r9c9" in response.json()["detail"]
        # Nothing was written, so a stale page cannot pollute the record.
        assert client.get("/api/runs/demo/feedback").json()["count"] == 0

    def test_malformed_candidate_id_is_rejected(self, client, runs_root):
        _write_run(runs_root)
        assert _post(client, "demo", candidate_id="../../etc/passwd").status_code == 400
        assert _post(client, "demo", candidate_id="").status_code == 400

    def test_an_overlong_note_is_refused_rather_than_truncated(self, client, runs_root):
        _write_run(runs_root)
        response = _post(client, "demo", round=1, note="x" * (server.MAX_NOTE_CHARS + 1))
        assert response.status_code == 400

    def test_feedback_on_a_missing_run_is_a_404(self, client, runs_root):
        assert _post(client, "nosuchrun").status_code == 404

    def test_a_preview_can_be_rated(self, client, runs_root):
        """Previews are playable before judging, which is when a producer listens."""
        _write_run(runs_root)
        response = _post(client, "demo", candidate_id="preview-carbide-r1", round=1)
        assert response.status_code == 200
        assert response.json()["recorded"]["team"] == "carbide"


class TestFeedbackReachesTheLog:
    def test_it_appears_as_an_event(self, client, runs_root):
        run_dir = _write_run(runs_root)
        _post(
            client,
            "demo",
            round=1,
            kept_tracks=["drums"],
            discarded_tracks=["pad"],
            note="keeping the kit",
        )
        events = read_events(run_dir / "events.jsonl")
        feedback = [event for event in events if event.kind == "producer.feedback"]
        assert len(feedback) == 1
        assert feedback[0].seq == max(event.seq for event in events)
        assert feedback[0].data["kept_tracks"] == ["drums"]
        assert feedback[0].data["discarded_tracks"] == ["pad"]
        # The message is the coach-facing rendering, so it is legible in the raw
        # feed without expanding the payload.
        assert "Kept: drums" in feedback[0].message
        assert "Deleted: pad" in feedback[0].message

    def test_it_does_not_make_a_finished_run_look_interrupted(self, client, runs_root):
        """The regression this file exists to pin.

        Feedback is appended after ``run.finished``, so anything reading "the last
        event" as "how the run ended" reports every rated run as interrupted.
        """
        _write_run(runs_root)
        assert client.get("/api/runs/demo/status").json()["status"] == "finished"
        _post(client, "demo", round=1)
        assert client.get("/api/runs/demo/status").json()["status"] == "finished"
        listed = client.get("/api/runs").json()["runs"]
        assert [run["status"] for run in listed if run["run_id"] == "demo"] == ["finished"]

    def test_a_key_shaped_note_is_scrubbed_out_of_both_records(self, client, runs_root):
        run_dir = _write_run(runs_root)
        _post(client, "demo", round=1, note=f"see {FAKE_KEY} for context")
        assert FAKE_KEY not in (run_dir / "feedback.jsonl").read_text(encoding="utf-8")
        assert FAKE_KEY not in (run_dir / "events.jsonl").read_text(encoding="utf-8")

    def test_the_event_stream_replays_it(self, client, runs_root):
        _write_run(runs_root)
        _post(client, "demo", round=1, note="replay me")
        with client.stream("GET", "/api/runs/demo/events?from_seq=0") as response:
            body = "".join(chunk for chunk in response.iter_text())
        assert "producer.feedback" in body
        assert "replay me" in body


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _card(client, run_id, candidate_id, round_no=None):
    payload = client.get(f"/api/runs/{run_id}/candidates").json()
    for card in payload["candidates"]:
        if card["candidate_id"] == candidate_id and (round_no is None or card["round"] == round_no):
            return card
    raise AssertionError(f"{candidate_id} not in {[c['candidate_id'] for c in payload['candidates']]}")


class TestCandidates:
    def test_artifacts_scores_and_tracks(self, client, runs_root):
        _write_run(runs_root)
        card = _card(client, "demo", "r1c1")
        assert card["artifacts"]["audio"] == "round1/artifacts/r1c1.oga"
        assert card["artifacts"]["piano_roll"] == "round1/artifacts/r1c1.png"
        assert card["artifacts"]["midi"] == "round1/carbide/out.mid"
        assert card["team"] == "carbide"
        assert card["round"] == 1
        assert card["gate"] == {"ok": True, "message": "Valid: 3 tracks."}

        melody = [score for score in card["scores"] if score["dimension"] == "melody"]
        assert melody and melody[0]["score"] == 6
        assert melody[0]["title"] == "Melody"
        assert melody[0]["findings"] == 1

        # Sidecar metadata is what makes the card say key, tempo and bar count.
        assert card["key"] == "Am"
        assert card["tempo"] == 124.0
        assert card["time_sig"] == "4/4"
        assert card["total_bars"] == 16
        assert card["tracks_from"] == "sidecar"
        assert [track["name"] for track in card["tracks"]] == ["drums", "bass", "pad"]
        assert card["tracks"][0]["is_drum"] is True

    def test_a_judged_take_absorbs_its_own_preview(self, client, runs_root):
        """One card per take, not two.

        This test previously asserted the opposite, that the preview and the
        judged render were listed side by side as distinct cards. They are the
        same music: the loop renders a clip as soon as a composer finishes so
        there is something to audition, then renders it again under a blinded id
        for the panel. Listing both gave a producer six cards for three takes
        with nothing marking which pair was the duplicate, which is precisely
        the confusion a variation browser exists to remove.
        """
        _write_run(runs_root)
        payload = client.get("/api/runs/demo/candidates").json()
        ids = {card["candidate_id"] for card in payload["candidates"]}
        assert ids == {"r1c1", "r1c2"}
        assert not [card for card in payload["candidates"] if card["preview"]]

        carbide = _card(client, "demo", "r1c1")
        assert carbide["superseded_ids"] == ["preview-carbide-r1"]
        # lumen never previewed, so it absorbed nothing.
        assert _card(client, "demo", "r1c2")["superseded_ids"] == []

    def test_order_is_as_generated_not_ranked(self, client, runs_root):
        _write_run(runs_root)
        payload = client.get("/api/runs/demo/candidates").json()
        order = [card["candidate_id"] for card in payload["candidates"]]
        # carbide leads because its preview was the first artifact of the run, and
        # a take keeps the position it was first drawn at when its scores land.
        assert order == ["r1c1", "r1c2"]
        # r1c2 scores higher, so a ranked payload would have inverted the pair.
        assert _card(client, "demo", "r1c2")["weighted_total"] > _card(client, "demo", "r1c1")["weighted_total"]

    def test_feedback_is_attached_to_the_card(self, client, runs_root):
        _write_run(runs_root)
        assert _card(client, "demo", "r1c1")["feedback"] is None
        _post(client, "demo", round=1, kept_tracks=["drums"])
        card = _card(client, "demo", "r1c1")
        assert card["feedback"]["kept_tracks"] == ["drums"]
        assert card["feedback_count"] == 1
        # And only to that card.
        assert _card(client, "demo", "r1c2")["feedback"] is None

    def test_ids_reused_across_rounds_stay_separate_takes(self, client, runs_root):
        """Older runs minted ``c1`` every round. Six takes must not fold into three."""
        run_dir = runs_root / "reused"
        run_dir.mkdir(parents=True)
        log = EventLog(run_dir / "events.jsonl")
        for round_no in (1, 2):
            log.emit(
                "artifact.rendered",
                "ready",
                round=round_no,
                team="carbide",
                candidate_id="c1",
                audio=f"r{round_no}/carbide/candidate.oga",
            )
        log.emit("run.finished", "done")

        payload = client.get("/api/runs/reused/candidates").json()
        assert [(card["candidate_id"], card["round"]) for card in payload["candidates"]] == [
            ("c1", 1),
            ("c1", 2),
        ]
        assert payload["candidates"][0]["artifacts"]["audio"] == "r1/carbide/candidate.oga"
        assert payload["candidates"][1]["artifacts"]["audio"] == "r2/carbide/candidate.oga"

        # Feedback naming a round lands on that round's take.
        _post(client, "reused", candidate_id="c1", round=2, verdict="discard")
        assert _card(client, "reused", "c1", 1)["feedback"] is None
        assert _card(client, "reused", "c1", 2)["feedback"]["verdict"] == "discard"

    def test_absolute_artifact_paths_are_normalised(self, client, runs_root):
        """The page's files endpoint only takes run-relative paths."""
        run_dir = runs_root / "absolute"
        run_dir.mkdir(parents=True)
        log = EventLog(run_dir / "events.jsonl")
        log.emit(
            "artifact.rendered",
            "ready",
            round=1,
            team="carbide",
            candidate_id="r1c1",
            audio=str(run_dir / "round1" / "a.oga"),
        )
        card = _card(client, "absolute", "r1c1")
        assert card["artifacts"]["audio"] == "round1/a.oga"

    def test_candidates_on_a_missing_run_is_a_404(self, client, runs_root):
        assert client.get("/api/runs/nosuchrun/candidates").status_code == 404

    def test_track_names_fall_back_to_the_judges_findings(self, client, runs_root):
        """A take with no sidecar still gets a track list from the findings.

        The per-stem keep/discard controls need names, and with no sidecar on disk
        the judges' findings are the only place a track is named.
        """
        run_dir = runs_root / "nosidecar"
        run_dir.mkdir(parents=True)
        log = EventLog(run_dir / "events.jsonl")
        log.emit(
            "judge.verdict",
            "Melody: 7/10 for cref",
            round=1,
            dimension="melody",
            candidate_id="cref",
            score=7,
            findings=[
                {"claim": "lead is static", "track": "lead"},
                {"claim": "pad is wide", "track": "pad"},
                {"claim": "no anchor", "track": None},
            ],
        )
        card = _card(client, "nosidecar", "cref")
        assert [track["name"] for track in card["tracks"]] == ["lead", "pad"]
        assert card["tracks_from"] == "judge findings"


class TestSyntheticFixture:
    """The checked-in fixture, read through the same endpoint the page uses.

    Skipped rather than failed when it is absent: it is regenerable
    (``scripts/synthetic_run.py --run-id synthetic``) and a fresh clone that has
    not run it yet should not see a red suite.
    """

    @pytest.fixture
    def real_client(self, monkeypatch):
        config = cfg.Config()
        if not (config.runs_dir / "synthetic" / "events.jsonl").is_file():
            pytest.skip("runs/synthetic is not present")
        monkeypatch.setattr(cfg, "load", lambda: config)
        return TestClient(server.app)

    def test_the_fixture_yields_playable_scored_candidates(self, real_client):
        payload = real_client.get("/api/runs/synthetic/candidates").json()
        candidates = payload["candidates"]
        assert len(candidates) >= 4

        playable = [card for card in candidates if card["artifacts"].get("audio")]
        assert playable, "no candidate in the fixture has audio to audition"
        for card in playable:
            assert card["artifacts"]["piano_roll"]
            # Every path is servable by the artifact endpoint as given.
            response = real_client.get(
                f"/api/runs/synthetic/files/{card['artifacts']['audio']}"
            )
            assert response.status_code == 200
            assert response.headers["content-type"] == "audio/ogg"

        scored = [card for card in candidates if card["scores"]]
        assert scored, "no candidate in the fixture has scores"
        for card in scored:
            # Counted against the contract rather than a literal, because the
            # rubric gains and loses dimensions and this endpoint reports whatever
            # the log holds.
            assert len(card["scores"]) >= 6
            titled = [score for score in card["scores"] if score["dimension"] in DIMENSION_TITLES]
            assert len(titled) == len(card["scores"]), "an unrecognised dimension leaked through"
            assert card["mean_score"] is not None
            assert card["weighted_total"] is not None

        # Per-stem feedback is the high-value part of this feature, so at least
        # some takes have to arrive with a track list. Not all of them: the
        # reference has no sidecar, and a judge that anchored nothing to a track
        # leaves nothing to derive one from.
        assert any(card["tracks"] for card in scored)

        # Ids repeat across the fixture's two rounds; each round is its own take.
        pairs = [(card["candidate_id"], card["round"]) for card in candidates]
        assert len(pairs) == len(set(pairs))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_absent_bundle_is_a_clean_404(self, client, runs_root):
        _write_run(runs_root)
        response = client.get("/api/runs/demo/export/r1c1")
        assert response.status_code == 404
        assert "r1c1" in response.json()["detail"]

    def test_a_bundle_on_disk_is_served(self, client, runs_root):
        run_dir = _write_run(runs_root)
        exports = run_dir / "exports"
        exports.mkdir()
        (exports / "r1c1.zip").write_bytes(b"PK\x03\x04 pretend")
        response = client.get("/api/runs/demo/export/r1c1")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "r1c1.zip" in response.headers["content-disposition"]
        assert response.content == b"PK\x03\x04 pretend"

    def test_the_candidates_endpoint_advertises_it(self, client, runs_root):
        run_dir = _write_run(runs_root)
        assert _card(client, "demo", "r1c1")["export"]["available"] is False
        (run_dir / "exports").mkdir()
        (run_dir / "exports" / "r1c1.zip").write_bytes(b"PK\x03\x04")
        card = _card(client, "demo", "r1c1")
        assert card["export"] == {"available": True, "file": "exports/r1c1.zip"}

    def test_a_refusal_to_build_one_says_why(self, client, runs_root):
        """The 404 carries the exporter's own complaint when it has one.

        ``out.mid`` in the fixture is not a MIDI file, so on-demand generation
        cannot succeed. "The exporter raised X" is a far more useful answer than
        "not found", and it is the difference between a producer filing a bug and
        a producer fixing their run.
        """
        pytest.importorskip("houseband.export")
        _write_run(runs_root)
        detail = client.get("/api/runs/demo/export/r1c1").json()["detail"]
        assert "r1c1" in detail
        assert len(detail) > len("No DAW export bundle for r1c1 in this run.")

    def test_generation_is_not_attempted_for_an_unknown_candidate(self, client, runs_root):
        _write_run(runs_root)
        response = client.get("/api/runs/demo/export/r9c9")
        assert response.status_code == 404
        assert not (runs_root / "demo" / "exports").exists()

    @pytest.mark.parametrize(
        "candidate_id",
        [
            "../../../etc/passwd",
            "..%2f..%2f..%2fetc%2fpasswd",
            "..",
            ".",
            "r1c1/../../../../etc/passwd",
            "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "r1c1\\..\\..\\secret",
        ],
    )
    def test_path_traversal_is_rejected(self, client, runs_root, candidate_id):
        _write_run(runs_root)
        # A file that would be a tempting target if the id reached the filesystem.
        (runs_root.parent / "secret").write_text("do not serve me", encoding="utf-8")
        response = client.get(f"/api/runs/demo/export/{candidate_id}")
        assert response.status_code in (400, 404), response.text
        assert "do not serve me" not in response.text
        assert "root:" not in response.text

    def test_a_malformed_run_id_is_rejected_too(self, client, runs_root):
        assert client.get("/api/runs/..%2f..%2f/export/r1c1").status_code in (400, 403, 404)

    def test_a_zip_symlinked_out_of_the_run_is_not_served(self, client, runs_root, tmp_path):
        """rglob finds it; the containment check on the resolved path does not."""
        run_dir = _write_run(runs_root)
        outside = tmp_path / "elsewhere" / "r1c1.zip"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"PK\x03\x04 outside")
        (run_dir / "r1c1.zip").symlink_to(outside)
        response = client.get("/api/runs/demo/export/r1c1")
        assert response.status_code == 404
        assert b"outside" not in response.content


# ---------------------------------------------------------------------------
# The credential rule, re-checked
# ---------------------------------------------------------------------------


class TestCredentialIsNeverEchoed:
    def test_no_endpoint_returns_the_submitted_key(self, client, runs_root, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(server, "_CREDENTIAL", {})
        _write_run(runs_root)
        try:
            posted = client.post("/api/credential", json={"api_key": FAKE_KEY})
            assert posted.status_code == 200
            assert posted.json()["configured"] is True
            for response in (
                posted,
                client.get("/api/credential"),
                client.get("/api/config"),
                client.get("/api/runs"),
                client.get("/api/runs/demo/status"),
                client.get("/api/runs/demo/candidates"),
                client.get("/api/runs/demo/feedback"),
            ):
                assert FAKE_KEY not in response.text
                assert FAKE_KEY[:20] not in response.text
        finally:
            client.delete("/api/credential")
