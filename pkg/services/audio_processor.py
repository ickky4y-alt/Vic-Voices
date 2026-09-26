import os
import subprocess
from pathlib import Path

from .ffmpeg_service import FFmpegService


class AudioProcessor:
    def __init__(self, app=None):
        self.app = app

    @staticmethod
    def ffmpeg_executable():
        configured = os.environ.get("FFMPEG_PATH")
        candidates = [
            Path(configured) if configured else None,
            Path(__file__).resolve().parents[1] / "ffmpeg" / "ffmpeg-9.0.1-essentials_build" / "bin" / "ffmpeg.exe",
            Path(__file__).resolve().parents[1] / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path(__file__).resolve().parents[1] / "ffmpeg" / "bin" / "ffmpeg",
            Path("ffmpeg"),
        ]
        for candidate in candidates:
            if candidate and candidate.exists():
                return str(candidate)
        return "ffmpeg"

    @staticmethod
    def ffprobe_executable():
        configured = os.environ.get("FFPROBE_PATH")
        candidates = [
            Path(configured) if configured else None,
            Path(__file__).resolve().parents[1] / "ffmpeg" / "ffmpeg-9.0.1-essentials_build" / "bin" / "ffprobe.exe",
            Path(__file__).resolve().parents[1] / "ffmpeg" / "bin" / "ffprobe.exe",
            Path(__file__).resolve().parents[1] / "ffmpeg" / "bin" / "ffprobe",
            Path("ffprobe"),
        ]
        for candidate in candidates:
            if candidate and candidate.exists():
                return str(candidate)
        return "ffprobe"

    @staticmethod
    def get_duration_seconds(file_path):
        ffprobe = AudioProcessor.ffprobe_executable()
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(file_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            return float(result.stdout.strip())
        except Exception:
            return None

    @staticmethod
    def normalize_audio(input_path, output_path, format_name="wav"):
        try:
            FFmpegService().convert(input_path, output_path, sample_rate=44100, channels=1)
            return Path(output_path).exists()
        except (OSError, RuntimeError, ValueError):
            return False
