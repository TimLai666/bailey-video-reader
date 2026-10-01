"""`--text-anchors` under `--from` — issue #16's trap one layer down.

Anchors become `eq(n,N)` in the select filter, and an input-side `-ss` restarts
the filter's `n` at 0. The cue *times* stay on the source clock (that is #16's
contract), but the *frame-index* clock does shift, so a source-clock cue time
has to be rebased or every anchor lands `start * fps` frames late.

Measured on a 27-minute recording with `--from 5:00 --to 7:00` (windowed decode
is n: 0..3599): 141 anchors were computed with an n range of 1184..48944. The 8
anchors that should have fired were n=9000..12600 — outside the range, so they
never fired — while 9 anchors from the call's first two minutes did fire. One
forced an extra frame at 00:05:39.467, which is 300 + 1184/30 where 39.47s is
the start of cue #1. A caption 39 seconds in forced a frame 5m39s in: worse
than a no-op, because the frame looks legitimate in the output.
"""
import subprocess

import pytest

from claude_real_video import core
from claude_real_video.core import _text_anchor_frames


def test_anchors_are_frame_numbers_on_the_source_clock_without_a_window():
    assert _text_anchor_frames([0.0, 2.0, 10.5], 30.0) == [0, 60, 315]


def test_anchors_are_rebased_onto_the_window():
    # Same cues, window starting at 300s: each anchor drops by start * fps, so
    # they land inside the windowed decode's n instead of far past its end.
    assert _text_anchor_frames([300.0, 302.0, 310.5], 30.0, origin=300.0) == [0, 60, 315]


def test_the_unrebased_anchors_would_fall_outside_a_windowed_decode():
    # The regression this guards: a 120s window at 30fps decodes n: 0..3599, so
    # every source-clock anchor from a 300s offset is out of range — and the
    # in-range ones it does produce point at unrelated moments.
    times = [300.0, 339.47, 360.0, 419.0]
    frames_in_window = 120 * 30
    assert all(n >= frames_in_window for n in _text_anchor_frames(times, 30.0))
    rebased = _text_anchor_frames(times, 30.0, origin=300.0)
    assert all(0 <= n < frames_in_window for n in rebased)
    assert rebased == [0, 1184, 1800, 3570]


def test_origin_defaults_to_zero():
    times = [5.0, 12.0]
    assert _text_anchor_frames(times, 25.0) == _text_anchor_frames(times, 25.0, origin=0.0)


def test_min_gap_still_thins_on_the_source_clock():
    # min_gap compares cue times, not rebased frame numbers, so a window must
    # not change which cues survive the thinning — only their frame numbers.
    times = [300.0, 300.4, 300.9, 302.0, 302.5, 303.5]
    assert _text_anchor_frames(times, 10.0) == [3000, 3020, 3035]
    assert _text_anchor_frames(times, 10.0, origin=300.0) == [0, 20, 35]


@pytest.mark.parametrize("origin", [0.0, 4.0])
def test_anchors_never_go_negative_for_in_window_cues(origin):
    assert all(n >= 0 for n in _text_anchor_frames([4.0, 6.0, 8.0], 10.0, origin=origin))


def test_out_of_window_cues_do_not_become_anchors(tmp_path, monkeypatch):
    """The call site's other half: rebasing alone is not enough, because a cue
    before the window would rebase to a negative n and one after it to an n past
    the window's end. Both have to be filtered out before thinning, or min_gap
    spends its budget on cues that can never fire."""
    import shutil

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg not installed")

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=10:size=320x240:rate=10", str(src)],
        capture_output=True)

    # 1.0 and 9.0 are outside [4, 8]; 5.0 and 6.5 are inside.
    monkeypatch.setattr(core, "_subtitle_cue_times",
                        lambda *a, **k: [1.0, 5.0, 6.5, 9.0])
    seen = {}
    real = core.extract_frames

    def spy(video, frames_dir, scene, fps_floor, **kw):
        seen["anchors"] = kw.get("anchors")
        return real(video, frames_dir, scene, fps_floor, **kw)

    monkeypatch.setattr(core, "extract_frames", spy)
    core.process(str(src), str(tmp_path / "out"), text_anchors=True,
                 do_transcribe=False, start=4, end=8)

    # Rebased onto the window (10fps): 5.0 -> 10, 6.5 -> 25. Nothing from 1.0
    # (which would be -30) or 9.0 (which would be 50, past the window's 40).
    assert seen["anchors"] == [10, 25]
