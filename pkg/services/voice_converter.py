import os
import json
import subprocess
import sys
import threading
from pathlib import Path

from .audio_processor import AudioProcessor
from .model_manager import ModelManager

_worker = None
_worker_lock = threading.Lock()
_worker_io_lock = threading.Lock()
_warmup_started = False


class RVCService:
    def __init__(self, app=None):
        self.app = app
        self.manager = ModelManager()

    @staticmethod
    def rvc_root():
        return ModelManager.get_rvc_root()

    @staticmethod
    def python_executable():
        configured = os.environ.get("PYTHON_PATH")
        if configured and Path(configured).exists():
            return str(Path(configured).resolve())
        rvc_root = ModelManager.get_rvc_root()
        rvc_python = rvc_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if rvc_python.exists():
            return str(rvc_python.resolve())
        return sys.executable

    def available_models(self):
        return [Path(item["path"]) for item in self.manager.discover()]

    def _worker_process(self):
        global _worker
        with _worker_lock:
            if _worker is None or _worker.poll() is not None:
                root = self.rvc_root()
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(root) + os.pathsep + environment.get("PYTHONPATH", "")
                environment["PYTHONIOENCODING"] = "utf-8"
                if os.environ.get("RVC_DEVICE", "cpu").lower() == "cpu":
                    environment["CUDA_VISIBLE_DEVICES"] = ""
                hubert_env = os.environ.get("HUBERT_MODEL_PATH")
                if hubert_env and Path(hubert_env).exists():
                    environment["HUBERT_MODEL_PATH"] = str(Path(hubert_env).resolve())
                rmvpe_env = os.environ.get("RMVPE_PATH")
                if rmvpe_env and Path(rmvpe_env).exists():
                    environment["RMVPE_PATH"] = str(Path(rmvpe_env).resolve())
                _worker = subprocess.Popen([self.python_executable(), str(root / "infer" / "cli.py"), "--server"], cwd=str(root), env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
                ready = _worker.stdout.readline().strip()
                if ready != "RVC_SERVER_READY":
                    raise RuntimeError("RVC worker did not start correctly.")
            return _worker

    def warmup_models(self):
        for model in self.manager.discover():
            self._request({"action": "warmup", "model": model["path"]})

    def start_warmup(self):
        global _warmup_started
        with _worker_lock:
            if _warmup_started:
                return
            _warmup_started = True
        threading.Thread(target=self.warmup_models, name="rvc-model-warmup", daemon=True).start()

    def _request(self, payload):
        worker = self._worker_process()
        with _worker_io_lock:
            worker.stdin.write(json.dumps(payload) + "\n")
            worker.stdin.flush()
            while True:
                line = worker.stdout.readline()
                if not line:
                    raise RuntimeError("RVC worker stopped unexpectedly.")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not response.get("ok"):
                    raise RuntimeError(response.get("error", "RVC worker failed."))
                return response

    def validate_model(self, model_path):
        candidate = Path(model_path).resolve()
        metadata = self.manager.file_metadata(candidate)
        result = {**metadata, "path": str(candidate), "valid": False, "error": ""}
        if not metadata["exists"] or not metadata["readable"] or not metadata["non_empty"]:
            result["error"] = "Model file is missing, unreadable, or empty."
            return result
        if candidate.suffix.lower() != ".pth":
            result["error"] = "Model file must use the .pth extension."
            return result
        root = self.rvc_root()
        script = root / "infer" / "cli.py"
        python = self.python_executable()
        if not script.is_file():
            result["error"] = "RVC infer/cli.py is missing."
            return result
        try:
            probe = subprocess.run([python, str(script), "--model", str(candidate), "--list-speakers"], cwd=str(root), capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            result["error"] = str(exc)
            return result
        if probe.returncode != 0:
            result["error"] = (probe.stderr or probe.stdout or "RVC rejected the model.").strip()[-1000:]
            return result
        result["valid"] = True
        result["speakers"] = probe.stdout.strip()
        return result

    def health_check(self):
        root = self.rvc_root()
        script = root / "infer" / "cli.py"
        models = self.manager.discover()
        checks = {"rvc_directory": root.is_dir(), "rvc_cli": script.is_file(), "model_count": len(models), "model_paths_valid": bool(models) and all(Path(item["path"]).is_file() for item in models), "python": False, "torch": False, "rvc_imports": False, "device": "cpu"}
        python = self.python_executable()
        try:
            probe = subprocess.run([python, "-c", "import torch; print('cuda' if torch.cuda.is_available() else 'cpu')"], cwd=str(root), capture_output=True, text=True, timeout=30, check=False)
            checks["python"] = probe.returncode == 0
            checks["torch"] = probe.returncode == 0
            checks["device"] = probe.stdout.strip() or "cpu"
            imports = subprocess.run([python, "-c", "import infer.vc.modules; import infer.cli"], cwd=str(root), capture_output=True, text=True, timeout=30, check=False)
            checks["rvc_imports"] = imports.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
        checks["ready"] = all((checks["rvc_directory"], checks["rvc_cli"], checks["model_count"] > 0, checks["model_paths_valid"], checks["python"], checks["torch"], checks["rvc_imports"]))
        return checks

    def convert(self, input_path, model_name, output_path, pitch=0, f0_method="rmvpe", index_rate=0.75, protect=0.33, model_path=None, index_path=None, rms_mix_rate=1.0, output_format=None):
        input_file = Path(input_path).resolve()
        if not input_file.is_file():
            raise FileNotFoundError("Input audio was not found.")
        chosen_model = Path(model_path).resolve() if model_path else next((path for path in self.available_models() if path.stem.lower() == (model_name or "").strip().lower()), None)
        if not chosen_model or not chosen_model.is_file() or chosen_model.suffix.lower() != ".pth":
            raise FileNotFoundError("The selected local voice model is unavailable.")
        root = self.rvc_root()
        script = root / "infer" / "cli.py"
        if not script.is_file():
            raise FileNotFoundError("The installed RVC inference entry point is unavailable.")
        output_file = Path(output_path).resolve()
        output_file.parent.mkdir(parents=True, exist_ok=True)
        if not 0 <= float(index_rate) <= 1 or not 0 <= float(protect) <= 0.5 or not 0 <= float(rms_mix_rate) <= 1:
            raise ValueError("Invalid RVC conversion settings.")
        index_file = Path(index_path).resolve() if index_path else None
        if index_file and (not index_file.is_file() or index_file.suffix.lower() != ".index"):
            raise FileNotFoundError("The selected RVC index file is unavailable.")
        rmvpe_env = os.environ.get("RMVPE_PATH")
        if rmvpe_env and Path(rmvpe_env).exists():
            _rp = Path(rmvpe_env)
            rmvpe_file = _rp if _rp.is_file() else _rp / "rmvpe.pt"
        else:
            rmvpe_file = root / "assets" / "rmvpe" / "rmvpe.pt"
        if f0_method == "rmvpe" and not rmvpe_file.is_file():
            f0_method = "pm"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(root) + os.pathsep + environment.get("PYTHONPATH", "")
        environment["PYTHONIOENCODING"] = "utf-8"
        if os.environ.get("RVC_DEVICE", "cpu").lower() == "cpu":
            environment["CUDA_VISIBLE_DEVICES"] = ""
        configured_timeout = int(os.environ.get("CONVERSION_TIMEOUT", "600"))
        duration = AudioProcessor.get_duration_seconds(input_file) or 0
        duration_timeout = int(duration * 15) + 300
        timeout_seconds = max(configured_timeout, duration_timeout)
        self._request({"action": "convert", "model": str(chosen_model), "input": str(input_file), "output": str(output_file), "pitch": int(pitch), "f0_method": str(f0_method), "index": str(index_file) if index_file else "", "index_rate": float(index_rate), "rms_mix_rate": float(rms_mix_rate), "protect": float(protect), "output_format": output_format or output_file.suffix.lower().lstrip(".") or "wav"})
        if not output_file.is_file() or output_file.stat().st_size == 0:
            raise FileNotFoundError("RVC conversion did not produce a valid output file.")
        return {"command": [str(root / "infer" / "cli.py"), "--server"], "returncode": 0, "stdout": "", "model": str(chosen_model), "output_path": str(output_file)}
