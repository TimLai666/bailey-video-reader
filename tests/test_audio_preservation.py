"""Generated audio only: verify payloads AND priming, padding and source timing.

No model, network, downloaded media or speech transcript is used by these tests.
"""
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import reader


def ffmpeg(*args):
    subprocess.run(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, args)], check=True)


def make_audio(tmp_path, codec="aac", suffix="m4a", rate=48000, channels=2):
    source = tmp_path / ("source." + suffix)
    expression = "0.4*cos(2*PI*659*t)" + ("|0.3*sin(2*PI*997*t)" if channels == 2 else "")
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc={expression}:s={rate}:d=1.237", "-c:a", codec, source)
    return source


def native_pcm(path, channels, codec="pcm_f32le"):
    return subprocess.check_output(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                                    "-map", "0:a:0", "-c:a", codec, "-f", "f32le" if codec == "pcm_f32le" else "s16le", "pipe:1"])


@pytest.mark.parametrize("rate,channels", [(44100, 1), (48000, 2)])
def test_primed_aac_native_pcm_and_timeline(tmp_path, rate, channels):
    source = make_audio(tmp_path, rate=rate, channels=channels)
    source_hash = reader.sha256(source)
    legacy = tmp_path / "legacy.mka"
    ffmpeg("-i", source, "-map", "0:a:0", "-c:a", "copy", legacy)
    source_pcm, legacy_pcm = native_pcm(source, channels), native_pcm(legacy, channels)
    # Deliberately nonzero source onset: simply ignoring a quiet prefix is unsafe.
    assert source_pcm[:channels * 4] != b"\0" * (channels * 4)
    assert len(legacy_pcm) - len(source_pcm) == 1024 * channels * 4
    assert legacy_pcm != source_pcm
    out = tmp_path / "out"; out.mkdir()
    retained = reader.preserve_original_audio(source, out, 0)
    assert retained["file"] == "audio_original.m4a"
    assert native_pcm(out / retained["file"], channels) == source_pcm
    verification = retained["verification"]
    assert verification["decoded_sample_timeline_equal"] and verification["packet_payloads_equal"]
    assert verification["source"]["decoded_sample_frames"] * channels * 4 == len(source_pcm)
    assert verification["source"]["first_decoded_pts_seconds_exact"] == "0"
    assert "1024" in verification["source"]["packet_edges"]["first"].values()
    assert reader.sha256(source) == source_hash


@pytest.mark.parametrize("codec,suffix,expected,rate,channels", [
    ("libopus", "webm", "mka", 48000, 2),
    ("pcm_s16le", "wav", "wav", 24000, 1),
    ("pcm_s24le", "wav", "wav", 44100, 2),
    ("libmp3lame", "mp3", "mp3", 44100, 2),
    ("flac", "flac", "flac", 48000, 2),
    ("alac", "m4a", "m4a", 44100, 2),
])
def test_native_audio_containers_keep_decode(tmp_path, codec, suffix, expected, rate, channels):
    source = make_audio(tmp_path, codec, suffix, rate, channels)
    out = tmp_path / "out"; out.mkdir()
    retained = reader.preserve_original_audio(source, out, 0)
    assert retained["file"] == "audio_original." + expected
    signature = retained["verification"]
    assert signature["source"]["decoded_pcm_sha256"] == signature["retained"]["decoded_pcm_sha256"]
    assert signature["source"]["decoded_timeline_sha256"] == signature["retained"]["decoded_timeline_sha256"]


def test_selected_audio_with_nonzero_pts(tmp_path):
    source = make_audio(tmp_path)
    shifted = tmp_path / "shifted.m4a"
    ffmpeg("-itsoffset", "1", "-i", source, "-c:a", "copy", shifted)
    out = tmp_path / "out"; out.mkdir()
    retained = reader.preserve_original_audio(shifted, out, 0)
    before, after = (retained["verification"][key] for key in ("source", "retained"))
    assert before["first_decoded_pts_seconds_exact"] != "0"
    assert before["first_decoded_pts_seconds_exact"] == after["first_decoded_pts_seconds_exact"]


@pytest.mark.parametrize("field", ["packet_payload_sha256", "decoded_pcm_sha256", "decoded_timeline_sha256",
                                   "decoded_sample_frames", "extradata_sha256"])
def test_failed_equivalence_never_keeps_accepted_audio(tmp_path, monkeypatch, field):
    source = make_audio(tmp_path)
    out = tmp_path / "out"; out.mkdir()
    real = reader.audio_preservation_signature
    def tampered(path, index, deadline):
        result = real(path, index, deadline)
        if path.parent == out:
            result[field] = "controlled mismatch"
        return result
    monkeypatch.setattr(reader, "audio_preservation_signature", tampered)
    with pytest.raises(RuntimeError, match="Original audio preservation changed"):
        reader.preserve_original_audio(source, out, 0)
    assert list(out.iterdir()) == []


def test_failed_verification_marks_cli_partial(tmp_path, monkeypatch):
    source = make_audio(tmp_path)
    def fail(*args):
        raise RuntimeError("Controlled decoded sample mismatch")
    monkeypatch.setattr(reader, "audio_preservation_signature", fail)
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="decoded sample mismatch"):
        reader.process(reader.parser().parse_args([str(source), "--skip-asr", "-o", str(out)]))
    assert json.loads((out / "manifest.json").read_text())["status"] == "failed_partial"
    assert json.loads((out / "asr.json").read_text())["status"] == "failed_or_not_run"


def test_local_asr_still_decodes_source_not_remux(tmp_path, monkeypatch):
    source = make_audio(tmp_path)
    out = tmp_path / "out"; out.mkdir()
    calls = []
    class FakeModel:
        def __init__(self, *args, **kwargs):
            assert kwargs["local_files_only"] is True
        def transcribe(self, path, **kwargs):
            calls.append(path)
            return [], SimpleNamespace(language="en", language_probability=1, duration_after_vad=0)
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(disable_telemetry_events=lambda: None))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    result = reader.transcribe_audio(source, out, 0, tmp_path / "fake-model", "en", 0, 1, 1)
    expected = tmp_path / "expected.wav"
    ffmpeg("-ss", "0", "-i", source, "-t", "1", "-map", "0:0", "-ar", "16000", "-ac", "1", expected)
    assert reader.sha256(out / result["working_audio"]["file"]) == reader.sha256(expected)
    assert calls == [str(out / result["working_audio"]["file"])]
    assert result["original_audio"]["verification"]["native_decoded_pcm_equal"]


def test_only_confirmed_unselected_timecode_warning_is_classified(tmp_path):
    source = tmp_path / "timecode.mov"
    ffmpeg("-f", "lavfi", "-i", "color=s=64x64:r=25:d=1", "-f", "lavfi", "-i",
           "sine=frequency=659:sample_rate=48000:duration=1", "-c:v", "mpeg4", "-c:a", "aac",
           "-timecode", "00:00:00:00", source)
    metadata = reader.probe(source)
    assert any(s.get("codec_tag_string") == "tmcd" for s in metadata["streams"])
    out = tmp_path / "out"; out.mkdir()
    retained = reader.preserve_original_audio(source, out, 1)
    assert retained["verification"]["source"]["classified_non_audio_warnings"] == [
        "Unsupported codec with id 0 for input stream 2"]
    assert retained["verification"]["native_decoded_pcm_equal"]


def test_bounded_runner_does_not_accept_other_warnings():
    from capture_queue.webm_duration import _run_strict
    allowed = b"Unsupported codec with id 0 for input stream 2"
    _run_strict([sys.executable, "-c", "import sys; sys.stderr.write(" + repr(allowed.decode() + "\n") + ")"],
                time.monotonic() + 5, allowed_stderr_lines=(allowed,))
    with pytest.raises(ValueError, match="warnings/errors"):
        _run_strict([sys.executable, "-c", "import sys; sys.stderr.write('File ended prematurely\\n')"],
                    time.monotonic() + 5, allowed_stderr_lines=(allowed,))
    with pytest.raises(ValueError, match="warnings/errors"):
        _run_strict([sys.executable, "-c", "import sys; sys.stderr.write(" + repr(allowed.decode() + "\n") + ")"],
                    time.monotonic() + 5)


def test_multi_audio_selection_is_explicit_and_verified(tmp_path):
    source = tmp_path / "two-tracks.m4a"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
           "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1",
           "-map", "0:a", "-map", "1:a", "-c:a", "aac", source)
    with pytest.raises(ValueError, match="Multiple audio tracks"):
        reader.process(reader.parser().parse_args([str(source), "--skip-asr", "-o", str(tmp_path / "refused")]))
    out = tmp_path / "selected"
    manifest = reader.process(reader.parser().parse_args([str(source), "--skip-asr", "--audio-stream", "1", "-o", str(out)]))
    asr = json.loads((out / "asr.json").read_text())
    assert manifest["audio_stream_index"] == asr["stream_index"] == 1
    retained = asr["original_audio"]
    assert retained["retained_stream_index"] == 0
    assert retained["verification"]["native_decoded_pcm_equal"]
    assert reader.audio_preservation_signature(source, 0, time.monotonic() + 30)["decoded_pcm_sha256"] != retained["verification"]["source"]["decoded_pcm_sha256"]


def test_source_mutation_refuses_retained_audio(tmp_path, monkeypatch):
    source = make_audio(tmp_path)
    out = tmp_path / "out"; out.mkdir()
    real = reader.audio_preservation_signature
    def mutate(path, index, deadline):
        result = real(path, index, deadline)
        if path.parent == out:
            with source.open("ab") as file:
                file.write(b"controlled source mutation")
        return result
    monkeypatch.setattr(reader, "audio_preservation_signature", mutate)
    with pytest.raises(RuntimeError, match="Source changed"):
        reader.preserve_original_audio(source, out, 0)
    assert list(out.iterdir()) == []
