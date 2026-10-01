"""Self-generated WebM fixtures: duration fallback, never browser-capture proof."""
import builtins
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import reader
from capture_queue import queue_reader as queue
from capture_queue import webm_duration as repair


@pytest.fixture
def clips(tmp_path):
    ordinary = tmp_path / "ordinary.webm"
    live = tmp_path / "live.webm"
    cmd = ["/usr/bin/ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=96x64:r=5",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.2", "-c:v", "libvpx-vp9",
           "-deadline", "realtime", "-cpu-used", "8", "-threads", "1", "-c:a", "libopus", str(ordinary)]
    subprocess.run(cmd, check=True, capture_output=True)
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-i", str(ordinary), "-map", "0", "-c", "copy", "-live", "1", str(live)],
                   check=True, capture_output=True)
    assert "duration" not in reader.probe(live)["format"]
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    return ordinary, live, temporary


def isolate_temporary(monkeypatch, directory):
    original = repair.tempfile.TemporaryDirectory
    monkeypatch.setattr(repair.tempfile, "TemporaryDirectory", lambda **kwargs: original(dir=directory, **kwargs))


def test_live_recovery_preserves_packets_original_and_skips_asr(clips, tmp_path, monkeypatch):
    normal, live, temporary = clips
    isolate_temporary(monkeypatch, temporary)
    before = live.read_bytes()
    original_import = builtins.__import__
    def guard(name, *args, **kwargs):
        assert name.split('.')[0] not in {"faster_whisper", "onnxruntime", "ctranslate2", "huggingface_hub", "av"}
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guard)
    out = tmp_path / "result"
    m = reader.process(reader.parser().parse_args([str(live), "-o", str(out), "--skip-asr", "--width", "96"]))
    p = m["duration_verification"]
    assert m["status"] == "complete_without_asr" and m["source_duration"] > 1
    assert p["source_sha256"] == hashlib.sha256(before).hexdigest() == m["source"]["sha256"]
    assert p["verifier_sha256"] == m["webm_duration_sha256"]
    assert len(p["packet_summary"]["streams"]) == 2
    assert p["original_file_modified"] is False and p["asr_performed"] is False
    assert live.read_bytes() == before and list(temporary.iterdir()) == []
    assert json.loads((out / "asr.json").read_text())["segments"] == []
    assert (out / "audio_original.mka").stat().st_size > 0


def test_normal_duration_never_enters_fallback(clips, monkeypatch):
    normal, live, temporary = clips
    monkeypatch.setattr(repair, "_packet_signature", lambda *args: pytest.fail("unexpected fallback"))
    duration, provenance = repair.resolve_duration(normal, reader.probe(normal))
    assert duration > 1 and provenance is None


def test_video_without_audio(clips, tmp_path):
    normal, live, temporary = clips
    silent = tmp_path / "silent.webm"
    subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-i", str(normal), "-an", "-c:v", "copy", "-live", "1", str(silent)], check=True)
    result = queue.media_probe(silent)
    assert result["audio_status"] == "missing_audio"
    assert result["duration_verification"]["packet_summary"]["streams"]["0"]["packet_count"] > 0


def test_queue_ready_accepts_verified_original_bytes(clips, tmp_path):
    normal, live, temporary = clips
    q = tmp_path / "queue"; queue.initialize(q)
    sid = str(uuid.uuid4()); job = queue.register(q, sid, "synthetic live WebM")
    expected = queue.hashfile(live)
    result = queue.ready(q, job["job_id"], sid, str(live), expected)
    assert result["state"] == "ready"
    p, j = queue.load_job(q, job["job_id"])
    media, receipt = queue.verify_ready(p, j)
    assert media.read_bytes() == live.read_bytes() and receipt["sha256"] == expected
    assert receipt["probe"]["duration_verification"]["source_sha256"] == expected


def test_truncated_live_refused_even_when_tool_exit_is_zero(clips, tmp_path, monkeypatch):
    normal, live, temporary = clips
    isolate_temporary(monkeypatch, temporary)
    truncated = tmp_path / "truncated.webm"; raw = live.read_bytes(); truncated.write_bytes(raw[:len(raw)*3//4])
    metadata = reader.probe(truncated)
    with pytest.raises(ValueError, match="warnings/errors|tool failed"):
        repair.resolve_duration(truncated, metadata)
    assert list(temporary.iterdir()) == []
    assert truncated.read_bytes() == raw[:len(raw)*3//4]
    with pytest.raises(ValueError):
        queue.media_probe(truncated)


def test_malformed_packet_input_is_refused(clips, tmp_path, monkeypatch):
    normal, live, temporary = clips
    isolate_temporary(monkeypatch, temporary)
    bad = tmp_path / "bad.webm"; bad.write_bytes(b"not WebM")
    with pytest.raises(ValueError, match="tool failed|no verifiable packets"):
        repair.resolve_duration(bad, reader.probe(live))
    assert list(temporary.iterdir()) == []


def test_timeout_kills_child_and_cleans_temporary(clips, monkeypatch):
    normal, live, temporary = clips
    isolate_temporary(monkeypatch, temporary)
    original_popen = subprocess.Popen
    children = []
    def sleeping_child(*args, **kwargs):
        child = original_popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        children.append(child)
        return child
    metadata = reader.probe(live)
    monkeypatch.setattr(repair.subprocess, "Popen", sleeping_child)
    with pytest.raises(TimeoutError):
        repair.resolve_duration(live, metadata, timeout_seconds=.15)
    assert children and all(p.poll() is not None for p in children)
    assert list(temporary.iterdir()) == []


def test_external_sigterm_unwinds_child_and_temporary(clips, tmp_path):
    normal, live, temporary = clips
    launcher = tmp_path / "interrupt-reader.py"
    marker = tmp_path / "child-pid.txt"
    launcher.write_text('''import sys, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import reader
from capture_queue import webm_duration as repair
original = subprocess.Popen
def controlled_child(command, **kwargs):
    if command[0] == '/usr/bin/ffmpeg':
        Path(command[-1]).write_bytes(b'partial test remux')
        child = original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        Path(sys.argv[3]).write_text(str(child.pid))
        return child
    return original(command, **kwargs)
repair.subprocess.Popen = controlled_child
reader.process(reader.parser().parse_args([sys.argv[2], '-o', sys.argv[4], '--skip-asr']))
''')
    process = subprocess.Popen([sys.executable, str(launcher), str(ROOT), str(live), str(marker), str(tmp_path / "out")],
                               env={"TMPDIR": str(temporary)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists(), process.communicate(timeout=1)
        child_pid = int(marker.read_text())
        assert list(temporary.glob("reader-webm-duration-*"))
        process.terminate()
        process.communicate(timeout=5)
        assert process.returncode != 0
        assert not Path(f"/proc/{child_pid}").exists()
        assert list(temporary.iterdir()) == []
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


@pytest.mark.parametrize("limit,value,match", [("MAX_PACKET_BYTES", 8, "metadata"), ("MAX_PACKETS", 1, "packet count"),
                                             ("MAX_LINE_BYTES", 8, "Oversized")])
def test_packet_resource_limits(clips, monkeypatch, limit, value, match):
    normal, live, temporary = clips
    isolate_temporary(monkeypatch, temporary)
    monkeypatch.setattr(repair, limit, value)
    with pytest.raises(ValueError, match=match):
        repair.resolve_duration(live, reader.probe(live))
    assert list(temporary.iterdir()) == []


def test_source_byte_limit_before_hashing(clips, monkeypatch):
    normal, live, temporary = clips
    monkeypatch.setattr(repair, "MAX_INPUT_BYTES", 1)
    monkeypatch.setattr(repair, "_hash", lambda *args: pytest.fail("oversized input was read"))
    with pytest.raises(ValueError, match="256 MiB"):
        repair.resolve_duration(live, reader.probe(live))


def test_low_disk_refused_and_cleaned(clips, monkeypatch):
    normal, live, temporary = clips
    isolate_temporary(monkeypatch, temporary)
    usage = shutil.disk_usage(temporary)
    monkeypatch.setattr(repair.shutil, "disk_usage", lambda p: type(usage)(usage.total, usage.total, 0))
    with pytest.raises(ValueError, match="disk space"):
        repair.resolve_duration(live, reader.probe(live))
    assert list(temporary.iterdir()) == []


def test_changed_remux_packet_signature_refused(clips, monkeypatch):
    normal, live, temporary = clips
    isolate_temporary(monkeypatch, temporary)
    original = repair._packet_signature
    calls = 0
    def altered(path, deadline):
        nonlocal calls
        calls += 1
        result = original(path, deadline)
        if calls == 2: result["streams"]["0"]["packet_sha256"] = "0" * 64
        return result
    monkeypatch.setattr(repair, "_packet_signature", altered)
    with pytest.raises(ValueError, match="changed packet"):
        repair.resolve_duration(live, reader.probe(live))
    assert list(temporary.iterdir()) == []


def test_missing_duration_non_webm_is_not_guessed(tmp_path):
    with pytest.raises(ValueError, match="local WebM only"):
        repair.resolve_duration(tmp_path / "absent.mp4", {"format": {"format_name": "mov,mp4"}})


@pytest.mark.parametrize("duration", [0, -1, "nan", "inf"])
def test_invalid_declared_duration_not_repaired(duration):
    with pytest.raises(ValueError, match="Invalid media duration"):
        repair.resolve_duration("unused.webm", {"format": {"duration": duration}})
