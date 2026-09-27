import os
from pathlib import Path


class ModelManager:
    def __init__(self, root=None):
        rvc_root = self.get_rvc_root()
        model_env = os.environ.get("MODEL_FOLDER")
        if root:
            self.root = Path(root)
        elif model_env and Path(model_env).is_dir():
            self.root = Path(model_env)
        else:
            self.root = rvc_root / "assets" / "weights"

        index_env = os.environ.get("INDEX_FOLDER")
        index_dir = Path(index_env) if index_env and Path(index_env).is_dir() else rvc_root / "assets" / "indices"

        self.search_roots = [self.root, rvc_root / "assets" / "weights"]
        self.index_roots = [index_dir, rvc_root / "logs"]

    def discover(self):
        models = []
        seen = set()
        for root in self.search_roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.pth")):
                resolved = path.resolve()
                if not path.is_file() or resolved in seen:
                    continue
                seen.add(resolved)
                index_path = self.find_index(path)
                models.append({
                    "name": path.stem,
                    "path": str(resolved),
                    "index_path": str(index_path) if index_path else "",
                    "type": "local",
                    "enabled": True,
                    "featured": False,
                    "source": "local",
                    "license": "unknown",
                })
        return models

    def find_index(self, model_path):
        model_path = Path(model_path)
        candidates = []
        for root in self.index_roots:
            if not root.exists():
                continue
            for index_path in root.rglob("*.index"):
                candidates.append(index_path)
        exact = next(
            (path for path in candidates if path.stem.lower() == model_path.stem.lower()),
            None,
        )
        if exact:
            return exact.resolve()
        compatible = next(
            (
                path
                for path in candidates
                if model_path.stem.lower().startswith(path.stem.lower() + "_")
            ),
            None,
        )
        return compatible.resolve() if compatible else None
        return None

    @staticmethod
    def file_metadata(path):
        candidate = Path(path)
        if not candidate.is_file():
            return {"exists": False, "readable": False, "non_empty": False, "size": 0}
        try:
            size = candidate.stat().st_size
            with candidate.open("rb") as stream:
                stream.read(1)
            return {"exists": True, "readable": True, "non_empty": size > 0, "size": size}
        except OSError:
            return {"exists": True, "readable": False, "non_empty": False, "size": 0}

    def first_model(self):
        models = self.discover()
        return models[0] if models else None

    @staticmethod
    def get_rvc_root():
        env_root = os.environ.get("RVC_ROOT")
        if env_root and Path(env_root).is_dir():
            return Path(env_root).resolve()
        return (Path(__file__).resolve().parents[1] / "rvc" / "Retrieval-based-Voice-Conversion-WebUI-main").resolve()

    @staticmethod
    def get_rvc_python():
        configured = os.environ.get("PYTHON_PATH")
        if configured:
            return Path(configured)
        return Path(__file__).resolve().parents[1] / "vvic-voices" / "Scripts" / "python.exe"
