"""The manual-transcript path must not run or require speech inference."""
import builtins
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import reader
import batch_reader


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "sample.mp4"
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "1.2",
                    "-c:v", "mpeg4", "-c:a", "aac", "-y", str(path)], check=True)
    return path


@pytest.fixture
def forbid_asr(monkeypatch):
    original_import = builtins.__import__
    forbidden = {"faster_whisper", "ctranslate2", "onnxruntime", "av", "huggingface_hub"}
    def guarded_import(name, *args, **kwargs):
        assert name.split(".")[0] not in forbidden, "skip attempted ASR import: " + name
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    def forbidden_call(*args, **kwargs):
        raise AssertionError("skip attempted transcription")
    monkeypatch.setattr(reader, "transcribe_audio", forbidden_call)
    original_version = reader.importlib.metadata.version
    def version(name):
        assert name == "Pillow", "skip required ASR package metadata"
        return original_version(name)
    monkeypatch.setattr(reader.importlib.metadata, "version", version)


def arguments(source, output, *extra):
    return reader.parser().parse_args([str(source), "-o", str(output), "--skip-asr",
                                      "--model", "/nonexistent/model-must-not-be-used",
                                      "--width", "96", "--interval", ".3", *extra])


def test_skip_keeps_frames_audio_and_exact_sidecar(media, tmp_path, forbid_asr):
    sidecar = tmp_path / "external.srt"
    raw = b'\xef\xbb\xbf1\r\n00:00:00,100 --> 00:00:00,900\r\nExternal words, not local ASR\r\n'
    sidecar.write_bytes(raw)
    source_hash = reader.sha256(media)
    out = tmp_path / "out"
    m = reader.process(arguments(media, out, "--captions", str(sidecar)))
    a = json.loads((out / "asr.json").read_text())
    c = json.loads((out / "captions.json").read_text())
    f = json.loads((out / "frames.json").read_text())
    links = json.loads((out / "alignment.json").read_text())
    assert m["status"] == "complete_without_asr"
    assert m["asr_status"] == a["status"] == "not_performed_user_skipped"
    assert m["transcript_status"] == "supplied_or_embedded_text_only"
    assert m["asr_elapsed_seconds"] == 0
    assert m["model_files"] == {} and m["model_source"] == {"status": "not_performed_user_skipped"}
    assert a["segments"] == [] and a["backend"] is None and a["origin"] == "not_transcribed"
    assert f["frames"] and links["utterances"] == []
    assert any(row["caption_ids"] for row in links["frame_caption_links"])
    assert c["tracks"][0]["origin"] == "supplied_sidecar"
    assert (out / c["tracks"][0]["raw_file"]).read_bytes() == raw
    assert c["tracks"][0]["source_sha256"] == reader.sha256(sidecar)
    assert (out / "audio_original.mka").stat().st_size > 0
    original_streams = reader.probe(out / "audio_original.mka")["streams"]
    assert original_streams[0]["codec_name"] == "aac"
    assert a["original_audio"]["scope"] == "full source stream"
    assert not (out / "audio_asr_16khz_mono.wav").exists()
    assert reader.sha256(media) == source_hash == m["source"]["sha256"]
    assert all(reader.sha256(out / k) == v for k, v in m["artifact_sha256"].items())


def test_skip_without_transcript_is_explicit(media, tmp_path, forbid_asr):
    out = tmp_path / "out"
    m = reader.process(arguments(media, out))
    a = json.loads((out / "asr.json").read_text())
    assert m["transcript_status"] == "no_transcript"
    assert a["segments"] == [] and a["gaps"][0]["start"] == 0
    assert "unclassified" in a["gaps"][0]["status"]
    assert "no_transcript" in (out / "READ_ME.txt").read_text()


def test_skip_silent_video_keeps_no_audio_explicit(media, tmp_path, forbid_asr):
    silent = tmp_path / "silent.mp4"
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-i", str(media), "-an", "-c:v", "copy", str(silent)], check=True)
    out = tmp_path / "out"
    m = reader.process(arguments(silent, out))
    a = json.loads((out / "asr.json").read_text())
    assert m["status"] == "complete_without_asr" and a["audio_status"] == "no_audio_track"
    assert "original_audio" not in a and not (out / "audio_original.mka").exists()


def test_skip_audio_only_keeps_no_frames(media, tmp_path, forbid_asr):
    audio = tmp_path / "audio.mka"
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-i", str(media), "-vn", "-c:a", "copy", str(audio)], check=True)
    out = tmp_path / "out"
    m = reader.process(arguments(audio, out))
    assert m["input_modality"] == "audio_only"
    assert json.loads((out / "frames.json").read_text())["frames"] == []
    assert (out / "audio_original.mka").is_file()


def test_default_and_legacy_force_still_select_asr():
    assert reader.parser().parse_args(["x", "-o", "y"]).skip_asr is False
    assert reader.parser().parse_args(["x", "-o", "y", "--force-audio-asr"]).skip_asr is False
    with pytest.raises(SystemExit):
        reader.parser().parse_args(["x", "-o", "y", "--skip-asr", "--force-audio-asr"])


def test_default_still_requires_model_for_audio(media, tmp_path):
    args = reader.parser().parse_args([str(media), "-o", str(tmp_path / "out"), "--model", str(tmp_path / "missing")])
    with pytest.raises(ValueError, match="Missing local model"):
        reader.process(args)


def test_skip_rejects_browser_bundle_mode_before_reading():
    with pytest.raises(ValueError, match="local media only"):
        reader.process(reader.parser().parse_args(["absent.json", "-o", "out", "--browser-evidence", "--skip-asr"]))


@pytest.mark.parametrize("script,argv", [("batch_reader.py", ["x", "-o", "y"]),
                                       ("capture_queue/queue_reader.py", ["--queue", "y", "drain", "--reader", "reader.py"])])
def test_unsupported_runners_reject_flag(script, argv):
    r = subprocess.run([sys.executable, str(ROOT / script), *argv, "--skip-asr"], capture_output=True, text=True)
    assert r.returncode == 2 and "unrecognized arguments: --skip-asr" in r.stderr


def test_skipped_output_is_not_batch_cache(media, tmp_path, forbid_asr):
    out = tmp_path / "out"
    m = reader.process(arguments(media, out))
    (out / "batch-job.json").write_text(json.dumps({"key": "example", "manifest_sha256": reader.sha256(out / "manifest.json")}))
    assert not batch_reader.validated_output(out, {"key": "example"})


def test_remux_failure_does_not_report_success(media, tmp_path, monkeypatch, forbid_asr):
    def fail(*args):
        raise RuntimeError("audio remux failed")
    monkeypatch.setattr(reader, "preserve_original_audio", fail)
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="audio remux failed"):
        reader.process(arguments(media, out))
    assert json.loads((out / "manifest.json").read_text())["status"] == "failed_partial"
