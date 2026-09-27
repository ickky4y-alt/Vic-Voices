import os
from pathlib import Path
from sqlalchemy.engine import make_url

BASE_DIR = Path(__file__).resolve().parent.parent


def database_uri(default=None):
    raw_url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("MYSQL_URL")
        or os.environ.get("MYSQLURL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("DATABASE_PUBLIC_URL")
        or default
    )
    if not raw_url:
        return f"sqlite:///{BASE_DIR / 'instance' / 'vicvoices.db'}"

    if raw_url.startswith("postgres://"):
        raw_url = "postgresql://" + raw_url[11:]

    try:
        url = make_url(raw_url)
        if url.drivername == "mysql":
            url = url.set(drivername="mysql+mysqlconnector")
        if url.drivername == "mysql+mysqlconnector":
            query = dict(url.query)
            query.setdefault("use_pure", "true")
            url = url.set(query=query)
        return url.render_as_string(hide_password=False)
    except Exception:
        if raw_url.startswith("postgres://"):
            return raw_url.replace("postgres://", "postgresql://", 1)
        return raw_url


class GeneralConfig(object):
    SECRET_KEY = os.environ.get("SECRET_KEY", "JNbkMffSwgHLnAU_L_ABRwhF_Os_default_safe_production_secret_key")
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin1@gmail.com")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Victor@2024")
    TECH_SUPPORT = "08062648647"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = database_uri("mysql+mysqlconnector://root@127.0.0.1/vic_voices_db?use_pure=true")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "storage" / "uploads"))
    OUTPUT_FOLDER = os.environ.get("OUTPUT_FOLDER", str(BASE_DIR / "storage" / "outputs"))
    RVC_PATH = os.environ.get("RVC_PATH", str(BASE_DIR / "pkg" / "rvc"))
    RVC_ROOT = os.environ.get("RVC_ROOT", str(Path(RVC_PATH) / "Retrieval-based-Voice-Conversion-WebUI-main"))
    MODEL_FOLDER = os.environ.get("MODEL_FOLDER", str(Path(RVC_ROOT) / "assets" / "weights"))
    INDEX_FOLDER = os.environ.get("INDEX_FOLDER", str(Path(RVC_ROOT) / "assets" / "indices"))
    HUBERT_MODEL_PATH = os.environ.get("HUBERT_MODEL_PATH", str(Path(RVC_ROOT) / "assets" / "hubert_base"))
    RMVPE_PATH = os.environ.get("RMVPE_PATH", str(Path(RVC_ROOT) / "assets" / "rmvpe" / "rmvpe.pt"))
    TEMP_FOLDER = os.environ.get("TEMP_FOLDER", str(BASE_DIR / "storage" / "temp"))
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 50 * 1024 * 1024))
    PAYMENT_RECEIPT_MAX_BYTES = int(os.environ.get("PAYMENT_RECEIPT_MAX_BYTES", 8 * 1024 * 1024))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    WTF_CSRF_ENABLED = True
    _bundled_ffmpeg = BASE_DIR / "pkg" / "ffmpeg" / "ffmpeg-9.0.1-essentials_build" / "bin" / "ffmpeg.exe"
    _bundled_ffprobe = BASE_DIR / "pkg" / "ffmpeg" / "ffmpeg-9.0.1-essentials_build" / "bin" / "ffprobe.exe"
    FFMPEG_PATH = os.environ.get("FFMPEG_PATH", str(_bundled_ffmpeg) if os.name == "nt" and _bundled_ffmpeg.is_file() else "ffmpeg")
    FFPROBE_PATH = os.environ.get("FFPROBE_PATH", str(_bundled_ffprobe) if os.name == "nt" and _bundled_ffprobe.is_file() else "ffprobe")
    MAX_AUDIO_DURATION = int(os.environ.get("MAX_AUDIO_DURATION", 300))
    JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", 600))
    MAX_CONCURRENT_CONVERSIONS_PER_USER = int(os.environ.get("MAX_CONCURRENT_CONVERSIONS_PER_USER", 1))
    DAILY_CONVERSION_LIMIT = int(os.environ.get("DAILY_CONVERSION_LIMIT", 50))
    DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "")
    RVC_DEVICE = os.environ.get("RVC_DEVICE", "cpu")

    @staticmethod
    def init_app(app):
        Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
        Path(app.config["OUTPUT_FOLDER"]).mkdir(parents=True, exist_ok=True)
        Path(app.config["MODEL_FOLDER"]).mkdir(parents=True, exist_ok=True)
        Path(app.config["INDEX_FOLDER"]).mkdir(parents=True, exist_ok=True)
        Path(app.config["TEMP_FOLDER"]).mkdir(parents=True, exist_ok=True)


class LiveConfig(GeneralConfig):
    SECRET_KEY = os.environ.get("SECRET_KEY")
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")


class TestConfig(GeneralConfig):
    ADMIN_EMAIL = "admin1@gmail.com"
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


class DevelopmentConfig(GeneralConfig):
    DEBUG = True


class ProductionConfig(GeneralConfig):
    DEBUG = False
    SECRET_KEY = os.environ.get("SECRET_KEY") or "JNbkMffSwgHLnAU_L_ABRwhF_Os_default_safe_production_secret_key"
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL") or "admin1@gmail.com"
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "Victor@2024"
    SQLALCHEMY_DATABASE_URI = database_uri()
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True


config = {
    "development": DevelopmentConfig,
    "testing": TestConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
    "live": LiveConfig,
    "test": TestConfig,
}
