import os
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RVC_ROOT = Path(os.environ.get("RVC_ROOT") or BASE_DIR / "pkg" / "rvc" / "Retrieval-based-Voice-Conversion-WebUI-main")

_hubert_env = os.environ.get("HUBERT_MODEL_PATH")
if _hubert_env:
    _hp = Path(_hubert_env)
    HUBERT_DIR = _hp if _hp.is_dir() or not _hp.suffix else _hp.parent
else:
    HUBERT_DIR = RVC_ROOT / "assets" / "hubert_base"

_rmvpe_env = os.environ.get("RMVPE_PATH")
if _rmvpe_env:
    _rp = Path(_rmvpe_env)
    RMVPE_FILE = _rp if _rp.is_file() or _rp.suffix == ".pt" else _rp / "rmvpe.pt"
else:
    RMVPE_FILE = RVC_ROOT / "assets" / "rmvpe" / "rmvpe.pt"

MODELS = [
    {
        "url": "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/hubert_base/config.json",
        "target": HUBERT_DIR / "config.json",
        "min_size": 500,
    },
    {
        "url": "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/hubert_base/preprocessor_config.json",
        "target": HUBERT_DIR / "preprocessor_config.json",
        "min_size": 100,
    },
    {
        "url": "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/hubert_base/pytorch_model.bin",
        "target": HUBERT_DIR / "pytorch_model.bin",
        "min_size": 100000000,
    },
    {
        "url": "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt",
        "target": RMVPE_FILE,
        "min_size": 10000000,
    },
]


def download_foundation_models():
    """Ensure foundation HuBERT (Transformers format) and RMVPE models are available."""
    for item in MODELS:
        target = Path(item["target"])
        min_size = item.get("min_size", 1000)
        if target.exists() and target.stat().st_size >= min_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading RVC foundation model {target.name}...")
        try:
            req = urllib.request.Request(
                item["url"],
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req) as resp, open(target, "wb") as out:
                out.write(resp.read())
            print(f"Successfully downloaded {target.name}")
        except Exception as e:
            print(f"Warning: Failed to download {target.name}: {e}")


if __name__ == "__main__":
    download_foundation_models()
