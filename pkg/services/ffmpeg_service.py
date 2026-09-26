import os
import shutil
import subprocess
from pathlib import Path


class FFmpegService:
    def __init__(self, ffmpeg_path=None, ffprobe_path=None):
        project_root = Path(__file__).resolve().parents[1]
        default_ffmpeg = project_root / "ffmpeg" / "ffmpeg-9.0.1-essentials_build" / "bin" / "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        default_ffprobe = project_root / "ffmpeg" / "ffmpeg-9.0.1-essentials_build" / "bin" / "ffprobe.exe" if os.name == "nt" else "ffprobe"
        self.ffmpeg_path = self._resolve(ffmpeg_path or os.environ.get("FFMPEG_PATH") or default_ffmpeg)
        self.ffprobe_path = self._resolve(ffprobe_path or os.environ.get("FFPROBE_PATH") or default_ffprobe)

    @staticmethod
    def _resolve(executable):
        candidate = Path(executable)
        if candidate.is_file():
            return candidate
        located = shutil.which(str(executable))
        return Path(located) if located else candidate

    def health_check(self):
        return {"ffmpeg": self._version(self.ffmpeg_path), "ffprobe": self._version(self.ffprobe_path)}

    @staticmethod
    def _version(executable):
        if not executable.is_file() and not shutil.which(str(executable)):
            return False
        try:
            result = subprocess.run([str(executable), "-version"], capture_output=True, text=True, timeout=15, check=False)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def probe(self, input_path):
        if not self.ffprobe_path.is_file() and not shutil.which(str(self.ffprobe_path)):
            raise FileNotFoundError("Bundled FFprobe is unavailable.")
        result = subprocess.run([str(self.ffprobe_path), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(Path(input_path).resolve())], capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            raise ValueError("The audio file could not be read by FFprobe.")
        return result.stdout

    def convert(self, input_path, output_path, sample_rate=16000, channels=1):
        if not self.ffmpeg_path.is_file() and not shutil.which(str(self.ffmpeg_path)):
            raise FileNotFoundError("Bundled FFmpeg is unavailable.")
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [str(self.ffmpeg_path), "-y", "-i", str(Path(input_path).resolve()), "-vn", "-ar", str(sample_rate), "-ac", str(channels), str(output)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg could not prepare the audio file.")
        return output
