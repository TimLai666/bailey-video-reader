"""`--from`/`--to` must reach the subtitle paths too, not just Whisper.

`transcribe()` takes start/end, but `existing_subtitles()` did not — and it is
tried *first*, because reading captions the video already ships with is faster
and more accurate than re-transcribing. So on any captioned video the window
silently did not apply: a two-minute window produced the whole call's
transcript. `--to --help` promises "the frame budget and the transcript follow
the window", so this is a broken promise, not a missing nicety.

Cue times stay on the source clock (same contract as issue #16: a window shifts
the analysis, not the clock), so nothing here is about re-basing timestamps —
only about which cues survive.
"""
import json

import pytest

from claude_real_video import core
from claude_real_video.core import (_clip_cues, _cues_to_text,
                                    existing_subtitles)

CUES = [
    {"start": 10.0, "end": 12.0, "text": "before the window"},
    {"start": 55.0, "end": 61.5, "text": "straddles the start"},
    {"start": 70.0, "end": 72.0, "text": "inside"},
    {"start": 118.0, "end": 124.0, "text": "straddles the end"},
    {"start": 200.0, "end": 202.0, "text": "after the window"},
]


def _texts(segs):
    return [c["text"] for c in segs]


def test_clip_cues_keeps_the_window_and_both_straddlers():
    # A cue crossing a boundary is kept: half a sentence beats none, and the
    # times are on the source clock so the caller can still see it started
    # before the window.
    assert _texts(_clip_cues(CUES, 60.0, 120.0)) == [
        "straddles the start", "inside", "straddles the end"]


@pytest.mark.parametrize("start,end,expected", [
    (None, None, 5),          # no window at all — every cue, untouched
    (60.0, None, 4),          # --from only: open-ended tail
    (None, 120.0, 4),         # --to only: from zero
    (60.0, 120.0, 3),
    (0.0, 5.0, 0),            # window before the first cue
    (500.0, 600.0, 0),        # window past the last cue
])
def test_clip_cues_window_shapes(start, end, expected):
    assert len(_clip_cues(CUES, start, end)) == expected


def test_clip_cues_without_a_window_returns_the_same_list():
    # Not merely equal: an unwindowed run must not pay for a copy, and callers
    # rely on the untouched cues going straight to _write_transcript_json.
    assert _clip_cues(CUES, None, None) is CUES


def test_cues_to_text_writes_one_line_per_cue(tmp_path):
    out = tmp_path / "transcript.txt"
    assert _cues_to_text(CUES[1:4], str(out)) == str(out)
    assert out.read_text(encoding="utf-8") == (
        "straddles the start\ninside\nstraddles the end\n")


def test_cues_to_text_writes_nothing_when_every_cue_is_empty(tmp_path):
    out = tmp_path / "transcript.txt"
    assert _cues_to_text([{"start": 1.0, "end": 2.0, "text": ""}], str(out)) is None
    assert not out.exists()


SRT = """1
00:00:10,000 --> 00:00:12,000
before the window

2
00:00:55,000 --> 00:01:01,500
straddles the start

3
00:01:10,000 --> 00:01:12,000
inside

4
00:03:20,000 --> 00:03:22,000
after the window
"""


def _sidecar(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "call.mp4"
    src.write_bytes(b"")           # never opened: the sidecar branch returns first
    (tmp_path / "call.srt").write_text(SRT, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    return str(src), str(out)


def test_sidecar_subtitles_follow_the_window(tmp_path):
    src, out = _sidecar(tmp_path)
    assert existing_subtitles(src, src, out, 60.0, 120.0)

    txt = (tmp_path / "out" / "transcript.txt").read_text(encoding="utf-8")
    assert txt.splitlines() == ["straddles the start", "inside"]
    assert "before the window" not in txt and "after the window" not in txt

    segs = json.load(open(tmp_path / "out" / "transcript.json",
                          encoding="utf-8"))["segments"]
    # Source clock, not window-relative — a caller reporting 70.0 as 10.0 would
    # be the issue #16 bug all over again.
    assert [c["start"] for c in segs] == [55.0, 70.0]


def test_sidecar_subtitles_are_untouched_without_a_window(tmp_path):
    src, out = _sidecar(tmp_path)
    assert existing_subtitles(src, src, out)

    txt = (tmp_path / "out" / "transcript.txt").read_text(encoding="utf-8")
    assert "before the window" in txt and "after the window" in txt


def test_a_window_that_excludes_every_cue_reports_no_transcript(tmp_path, monkeypatch):
    # No text means no transcript path, so process() falls through to Whisper
    # instead of handing the caller an empty transcript.txt. The stub keeps the
    # fall-through off ffprobe: the placeholder source has no streams to read.
    monkeypatch.setattr(core, "_has_subtitle_stream", lambda _v: False)
    src, out = _sidecar(tmp_path)
    assert existing_subtitles(src, src, out, 500.0, 600.0) is None
    assert not (tmp_path / "out" / "transcript.txt").exists()


def test_a_failing_transcript_json_does_not_sink_the_run(tmp_path, monkeypatch):
    """Parity with the unwindowed path this replaces: master wrapped the
    transcript.json write in `except OSError: pass`, so a run that had already
    produced transcript.txt survived an unwritable json. Keep that — the json is
    a bonus next to the text, not a precondition for it."""
    def boom(*_a, **_k):
        raise OSError("no space left on device")
    monkeypatch.setattr(core, "_write_transcript_json", boom)

    src, out = _sidecar(tmp_path)
    assert existing_subtitles(src, src, out, 60.0, 120.0)          # windowed
    assert (tmp_path / "out" / "transcript.txt").exists()

    src2, out2 = _sidecar(tmp_path / "b")
    assert existing_subtitles(src2, src2, out2)                    # unwindowed
    assert (tmp_path / "b" / "out" / "transcript.txt").exists()
