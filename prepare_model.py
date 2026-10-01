"""Explicit public model download only; reader.py itself is offline."""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.update(HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_XET="1",
                  HF_HOME=str(ROOT / ".cache" / "huggingface"))
from huggingface_hub import snapshot_download

REPO = "Systran/faster-whisper-small"
REVISION = "536b0662742c02347bc0e980a01041f333bce120"
target = ROOT / "models" / "faster-whisper-small"
snapshot_download(REPO, revision=REVISION, token=False, local_dir=target,
                  allow_patterns=["*.json", "*.txt", "model.bin", "README.md"], max_workers=2)
files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in target.glob("*") if p.is_file()}
(ROOT / "model-source.json").write_text(json.dumps({"repo": REPO, "revision": REVISION,
    "license": "MIT", "url": "https://huggingface.co/" + REPO, "files": files}, indent=2)+"\n")
print(target)
