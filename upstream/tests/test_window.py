"""Analysis window (--from/--to) and frame width (--frame-width) — issues #16, #17.

The load-bearing property in #16 is that a window shifts the *analysis*, not the
*clock*: a frame at 30:32 must still be reported as 30:32 when the caller asked
for minutes 28-43. The reporter shipped a wrong timecode to a colleague because
their ffmpeg-clip workaround lost that offset, so it gets its own test.
"""
import pytest

from claude_real_video.core import (_hhmmss, _shift_times, _window_args,
                                    parse_timecode)


@pytest.mark.parametrize("raw,expected", [
    ("90", 90.0), (90, 90.0), (90.5, 90.5),
    ("1:30", 90.0), ("0:01:30.5", 90.5),
    ("2:00:00", 7200.0), ("0", 0.0),
    (None, None), ("", None), ("  ", None),
])
def test_parse_timecode(raw, expected):
    assert parse_timecode(raw) == expected


@pytest.mark.parametrize("bad", ["a:b", "1:2:3:4", "-5", "abc"])
def test_parse_timecode_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_timecode(bad)


def test_window_args_seeks_before_input_and_uses_duration():
    # -ss must be an input option (fast seek) and the tail must be a DURATION,
    # because an input-side seek restarts the output clock at zero. -t is an
    # input option too: on the output side ffmpeg decodes the whole input, so
    # showinfo logs frames that are never written and the count mismatch in
    # extract_frames() discards every timestamp.
    pre, post = _window_args(90, 120)
    assert pre == ["-ss", "90.000", "-t", "30.000"]
    assert post == []


def test_window_args_open_ended():
    assert _window_args(90, None) == (["-ss", "90.000"], [])
    assert _window_args(None, 60) == (["-t", "60.000"], [])
    assert _window_args(None, None) == ([], [])


def test_window_duration_is_an_input_option():
    """A --to window must not put -t after -i.

    Regression: with an output-side -t, ffmpeg decodes past the window end, so
    the showinfo log (which is the only place source PTS survives — issue #7)
    covers more frames than were written. extract_frames() ends with
    `times if len(times) == count else []`, so the mismatch threw away *every*
    timestamp: no frames.json, no `frame timestamps:` line in MANIFEST.txt, and
    no error. A --to run silently lost the clock that #16 exists to preserve.
    """
    for start, end in ((None, 60), (90, 120), (0, 15)):
        pre, post = _window_args(start, end)
        assert "-t" in pre, f"-t must be an input option for --from {start} --to {end}"
        assert post == [], f"nothing belongs after -i, got {post}"


def test_window_args_puts_seek_before_duration():
    # -ss then -t: the duration is measured from the seek point, not from 0.
    pre, _ = _window_args(90, 120)
    assert pre.index("-ss") < pre.index("-t")


def test_window_args_rejects_inverted_window():
    with pytest.raises(ValueError):
        _window_args(120, 60)


def test_shift_times_restores_source_clock():
    # the whole point of #16
    assert _shift_times([0.0, 12.5], 1800) == [1800.0, 1812.5]


def test_shift_times_is_a_noop_without_a_window():
    assert _shift_times([1.0, 2.0], None) == [1.0, 2.0]
    assert _shift_times([1.0, 2.0], 0) == [1.0, 2.0]


def test_hhmmss():
    assert _hhmmss(1832) == "0:30:32"
    assert _hhmmss(0) == "0:00:00"


def test_window_and_width_are_cache_keyed():
    """A run with a different window or width must not hit the memory cache.
    cli.py's analysis_params is the guard; an option missing from it silently
    stops working on a re-run (the comment there records that happening twice)."""
    import inspect

    from claude_real_video import cli
    src = inspect.getsource(cli.main)
    params = src.split("analysis_params = {")[1].split("}")[0]
    for key in ('"start"', '"end"', '"frame_width"'):
        assert key in params, f"{key} missing from analysis_params — cache would ignore it"
