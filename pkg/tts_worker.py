import importlib.util
import sys


module_path = __file__.replace("tts_worker.py", "tts_processing.py")
spec = importlib.util.spec_from_file_location("vic_voices_tts_processing", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.generate_speech(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] or None, sys.argv[5] or "neutral", float(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else None)