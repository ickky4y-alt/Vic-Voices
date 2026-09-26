from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
from threading import Lock

_pipeline = None
_pipeline_lock = Lock()

VOICE_CATALOG = [
    {"id": "af_heart", "name": "Heart", "gender": "female"},
    {"id": "af_bella", "name": "Bella", "gender": "female"},
    {"id": "af_nicole", "name": "Nicole", "gender": "female"},
    {"id": "af_sarah", "name": "Sarah", "gender": "female"},
    {"id": "af_sky", "name": "Sky", "gender": "female"},
    {"id": "bf_emma", "name": "Emma", "gender": "female"},
    {"id": "bf_isabella", "name": "Isabella", "gender": "female"},
    {"id": "am_adam", "name": "Adam", "gender": "male"},
    {"id": "am_michael", "name": "Michael", "gender": "male"},
    {"id": "bm_george", "name": "George", "gender": "male"},
    {"id": "bm_lewis", "name": "Lewis", "gender": "male"},
]
VOICE_IDS = {voice["id"] for voice in VOICE_CATALOG}
STYLE_CATALOG = [
    {"id": "neutral", "name": "Neutral", "description": "Balanced everyday delivery.", "speed": 1.0, "preview": "This is a clear, natural voice preview."},
    {"id": "angry", "name": "Angry", "description": "Sharper pacing and stronger emphasis.", "speed": 1.12, "preview": "I need you to listen carefully. This matters right now!"},
    {"id": "calm", "name": "Calm", "description": "Relaxed pacing with more space.", "speed": 0.86, "preview": "Take a slow breath. Everything is going to be okay."},
    {"id": "sad", "name": "Sad", "description": "Slower, subdued delivery.", "speed": 0.82, "preview": "Some days feel heavier than others, and that is okay."},
    {"id": "excited", "name": "Excited", "description": "Bright, energetic pacing.", "speed": 1.18, "preview": "This is incredible! We are about to begin!"},
    {"id": "emotional", "name": "Emotional", "description": "Expressive pacing and pauses.", "speed": 0.92, "preview": "I will remember this moment for the rest of my life."},
]
STYLE_MAP = {style["id"]: style for style in STYLE_CATALOG}


def available_voices():
    return VOICE_CATALOG.copy()


def _load_pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    try:
        from kokoro import KPipeline
    except ImportError as exc:
        raise RuntimeError("Kokoro is not installed. Install kokoro and soundfile in a compatible Python environment.") from exc
    with _pipeline_lock:
        if _pipeline is None:
            _pipeline = KPipeline(lang_code="a")
    return _pipeline


def _generate_local(text: str, voice: str, output_path: str, ffmpeg_path: str | None = None, style: str = "neutral", speed: float | None = None):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Text cannot be empty.")
    if len(text) > 2000:
        raise ValueError("Text must be 2000 characters or fewer.")
    if voice not in VOICE_IDS:
        raise ValueError("That preset voice is not available.")
    if style not in STYLE_MAP:
        raise ValueError("That speech style is not available.")
    selected_speed = float(speed if speed is not None else STYLE_MAP[style]["speed"])
    if not 0.5 <= selected_speed <= 2.0:
        raise ValueError("Speech speed must be between 0.5 and 2.0.")
    try:
        import soundfile as sf
    except ImportError as exc:
        raise RuntimeError("SoundFile is not installed. Install kokoro and soundfile in a compatible Python environment.") from exc
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    import numpy as np

    audio_chunks = []
    for _, _, audio in _load_pipeline()(text.strip(), voice=voice, speed=selected_speed):
        if audio is not None:
            audio_chunks.append(audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio))
    if not audio_chunks:
        raise RuntimeError("Kokoro did not return any audio.")

    with tempfile.TemporaryDirectory() as temp_dir:
        wav_path = Path(temp_dir) / "speech.wav"
        sf.write(str(wav_path), np.concatenate(audio_chunks), 24000, format="WAV")
        if output.suffix.lower() == ".wav":
            os.replace(wav_path, output)
        else:
            if not ffmpeg_path or (not Path(ffmpeg_path).is_file() and not shutil.which(ffmpeg_path)):
                raise RuntimeError("FFmpeg is required to create MP3 output.")
            completed = subprocess.run([ffmpeg_path, "-y", "-i", str(wav_path), "-codec:a", "libmp3lame", "-q:a", "4", str(output)], capture_output=True, text=True)
            if completed.returncode != 0:
                raise RuntimeError("FFmpeg could not encode the generated audio as MP3.")
    return output


def generate_speech(text: str, voice: str, output_path: str, ffmpeg_path: str | None = None, style: str = "neutral", speed: float | None = None):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Text cannot be empty.")
    if len(text) > 2000:
        raise ValueError("Text must be 2000 characters or fewer.")
    if voice not in VOICE_IDS:
        raise ValueError("That preset voice is not available.")
    if style not in STYLE_MAP:
        raise ValueError("That speech style is not available.")
    if os.environ.get("VIC_VOICES_TTS_WORKER") == "1":
        return _generate_local(text, voice, output_path, ffmpeg_path, style, speed)
    worker_venv = Path(__file__).resolve().parent / "rvc" / "Retrieval-based-Voice-Conversion-WebUI-main" / ".venv"
    worker_python = worker_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if worker_python.is_file() and not _module_available("kokoro"):
        completed = subprocess.run([str(worker_python), str(Path(__file__).with_name("tts_worker.py")), text, voice, output_path, ffmpeg_path or "", style, str(speed or "")], capture_output=True, text=True, env={**os.environ, "VIC_VOICES_TTS_WORKER": "1"})
        if completed.returncode != 0:
            worker_error = completed.stderr.lower()
            if "readtimeout" in worker_error or "huggingface.co" in worker_error:
                raise RuntimeError("Kokoro model download timed out. Check the connection and try Generate again.")
            raise RuntimeError("Kokoro speech generation failed. Check the local runtime and try again.")
        return Path(output_path)
    return _generate_local(text, voice, output_path, ffmpeg_path, style, speed)


def _module_available(name):
    import importlib.util
    return importlib.util.find_spec(name) is not None