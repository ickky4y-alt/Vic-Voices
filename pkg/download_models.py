import os
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RVC_ROOT = BASE_DIR / "pkg" / "rvc" / "Retrieval-based-Voice-Conversion-WebUI-main"

MODELS = [
    {
        "url": "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/hubert_base.pt",
        "target": RVC_ROOT / "assets" / "hubert_base" / "hubert_base.pt",
    },
    {
        "url": "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/hubert_base.pt",
        "target": RVC_ROOT / "assets" / "hubert_base" / "pytorch_model.bin",
    },
    {
        "url": "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt",
        "target": RVC_ROOT / "assets" / "rmvpe" / "rmvpe.pt",
    },
]


def download_foundation_models():
    """Ensure foundation HuBERT and RMVPE models are available."""
    for item in MODELS:
        target = item["target"]
        if not target.exists() or target.stat().st_size < 1000000:
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
