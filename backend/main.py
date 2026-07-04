import base64
import json
import importlib.util
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Video Downloader API")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("yt_downloader")

CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000",
)
INFO_CACHE_TTL_SECONDS = int(os.getenv("INFO_CACHE_TTL_SECONDS", "300"))
INFO_FETCH_TIMEOUT_SECONDS = int(os.getenv("INFO_FETCH_TIMEOUT_SECONDS", "90"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "3600"))
JOB_EVENT_INTERVAL_SECONDS = float(os.getenv("JOB_EVENT_INTERVAL_SECONDS", "0.5"))
YTDLP_REMOTE_COMPONENTS = os.getenv("YTDLP_REMOTE_COMPONENTS", "ejs:github")
YTDLP_EXTRACTOR_ARGS = os.getenv("YTDLP_EXTRACTOR_ARGS", "").strip()
YTDLP_FORCE_IPV4 = os.getenv("YTDLP_FORCE_IPV4", "1") == "1"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in CORS_ORIGINS.split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

APP_DIR = Path(__file__).parent
FRONTEND_DIST_DIR = APP_DIR.parent / "frontend" / "dist"
DOWNLOADS_DIR = APP_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)
JOBS_DB_PATH = APP_DIR / "jobs.db"

COOKIES_FILE = APP_DIR / "cookies.txt"
ENV_COOKIES_FILE = Path(tempfile.gettempdir()) / "yt-dlp-cookies.txt"
KNOWN_BROWSERS = [
    {"value": "safari", "label": "Safari"},
    {"value": "chrome", "label": "Chrome"},
    {"value": "firefox", "label": "Firefox"},
    {"value": "brave", "label": "Brave"},
]
COOKIE_RETRY_PATTERNS = (
    "does not support cookies",
    "only images are available for download",
    "there is no video in this post",
    "requested format is not available",
    "nsig extraction failed",
    "cookies-from-browser",
    "browser cookies",
    "cookie database",
    "database is locked",
    "failed to decrypt",
    "could not find",
    "permission denied",
)
FINAL_JOB_STATUSES = {"completed", "failed"}
DOWNLOAD_PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%.*?(?:at\s+(?P<speed>\S+))?(?:\s+ETA\s+(?P<eta>[0-9:]+))?"
)

info_cache: dict[str, dict[str, Any]] = {}
info_cache_lock = threading.Lock()

download_jobs: dict[str, dict[str, Any]] = {}
download_jobs_lock = threading.Lock()
download_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)


def normalize_cookie_content(raw_cookies: str) -> str:
    content = raw_cookies.strip()

    if (
        len(content) >= 2
        and content[0] == content[-1]
        and content[0] in {"'", '"'}
    ):
        content = content[1:-1].strip()

    if "\\n" in content and "\n" not in content:
        content = content.replace("\\r\\n", "\n").replace("\\n", "\n")
    if "\\t" in content and "\t" not in content:
        content = content.replace("\\t", "\t")

    return content.strip() + "\n"


def ensure_env_cookies_file() -> Path | None:
    raw_cookies = os.getenv("YTDLP_COOKIES_CONTENT")
    encoded_cookies = os.getenv("YTDLP_COOKIES_BASE64")

    if not raw_cookies and encoded_cookies:
        try:
            raw_cookies = base64.b64decode("".join(encoded_cookies.split())).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            logger.warning("YTDLP_COOKIES_BASE64 is invalid and will be ignored")
            return None

    if not raw_cookies:
        return None

    ENV_COOKIES_FILE.write_text(normalize_cookie_content(raw_cookies))
    ENV_COOKIES_FILE.chmod(0o600)
    return ENV_COOKIES_FILE


def get_cookies_file() -> Path | None:
    if COOKIES_FILE.exists():
        return COOKIES_FILE
    if os.getenv("YTDLP_COOKIES_CONTENT") or os.getenv("YTDLP_COOKIES_BASE64"):
        return ensure_env_cookies_file()
    if ENV_COOKIES_FILE.exists():
        return ENV_COOKIES_FILE
    return ensure_env_cookies_file()


def describe_cookies_file(cookies_file: Path | None) -> dict[str, Any]:
    if not cookies_file or not cookies_file.exists():
        return {"loaded": False, "lines": 0, "domains": []}

    try:
        lines = cookies_file.read_text().splitlines()
    except OSError:
        return {"loaded": False, "lines": 0, "domains": []}

    domains = sorted(
        {
            parts[0].lstrip(".")
            for line in lines
            if line.strip() and not line.startswith("#")
            for parts in [line.split("\t")]
            if len(parts) >= 7 and parts[0]
        }
    )

    return {
        "loaded": True,
        "lines": len(lines),
        "domains": domains[:8],
        "has_youtube": any("youtube.com" in domain or "google.com" in domain for domain in domains),
        "has_instagram": any("instagram.com" in domain for domain in domains),
    }


class DownloadJobRequest(BaseModel):
    url: str
    format_id: str = "bestvideo+bestaudio/best"
    audio_only: bool = False
    browser: str = "none"


def now_ts() -> float:
    return time.time()


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def build_download_headers(filename: str) -> dict[str, str]:
    safe_name = sanitize_filename(filename).strip() or "download"
    ascii_name = (
        unicodedata.normalize("NFKD", safe_name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    ascii_name = re.sub(r"\s+", " ", ascii_name).strip(" .") or "download"
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", ascii_name)
    encoded_name = quote(safe_name, safe="")
    return {
        "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}',
    }


def detect_platform(data: dict[str, Any]) -> tuple[str, str]:
    extractor = (data.get("extractor_key") or data.get("extractor") or "").lower()
    source_url = (
        data.get("webpage_url")
        or data.get("original_url")
        or data.get("url")
        or ""
    ).lower()

    if "instagram" in extractor or "instagram.com" in source_url:
        return "instagram", "Instagram"
    if "youtube" in extractor or "youtu" in source_url:
        return "youtube", "YouTube"
    if "facebook" in extractor or "facebook.com" in source_url:
        return "facebook", "Facebook"
    return "video", "Video"


def detect_platform_from_url(url: str) -> str:
    source_url = (url or "").lower()
    if "instagram.com" in source_url:
        return "instagram"
    if "youtu" in source_url or "youtube.com" in source_url:
        return "youtube"
    if "facebook.com" in source_url:
        return "facebook"
    return "video"


def has_node_runtime() -> bool:
    return shutil.which("node") is not None


def has_local_ejs_package() -> bool:
    return importlib.util.find_spec("yt_dlp_ejs") is not None


def get_yt_dlp_runtime_args() -> list[str]:
    args: list[str] = []

    if YTDLP_FORCE_IPV4:
        args.append("--force-ipv4")

    if has_node_runtime():
        args.extend(["--js-runtimes", "node"])

    if not has_local_ejs_package() and YTDLP_REMOTE_COMPONENTS:
        args.extend(["--remote-components", YTDLP_REMOTE_COMPONENTS])

    if YTDLP_EXTRACTOR_ARGS:
        args.extend(["--extractor-args", YTDLP_EXTRACTOR_ARGS])

    return args


def summarize_yt_dlp_error(detail: str, browser: str) -> str:
    detail_lower = (detail or "").lower()

    if (
        "failed to resolve" in detail_lower
        or "temporary failure in name resolution" in detail_lower
        or "nodename nor servname provided" in detail_lower
        or "name or service not known" in detail_lower
    ):
        return (
            "O backend nao conseguiu resolver o dominio da plataforma. "
            "Verifique sua conexao com a internet e o DNS da maquina, e tente novamente."
        )

    if (
        "connection refused" in detail_lower
        or "network is unreachable" in detail_lower
        or "timed out" in detail_lower
        or "timeout" in detail_lower
    ):
        return (
            "A conexao com a plataforma falhou ou expirou antes da resposta. "
            "Confirme se a internet esta estavel e tente novamente em alguns instantes."
        )

    if "ssl" in detail_lower and "certificate" in detail_lower:
        return (
            "A conexao segura com a plataforma falhou por causa do certificado SSL. "
            "Verifique a data do sistema, proxy/VPN e certificados da maquina."
        )

    if "ffmpeg is not installed" in detail_lower or "ffprobe and ffmpeg not found" in detail_lower:
        return (
            "O backend precisa do ffmpeg para finalizar este download. "
            "Instale o ffmpeg e execute o setup novamente."
        )

    if "instagram sent an empty media response" in detail_lower:
        if browser == "none":
            return (
                "O Instagram nao retornou dados para esta publicacao. Normalmente isso acontece "
                "quando o post exige login, e privado/restrito, foi removido, ou o Instagram bloqueou "
                "a consulta sem cookies. Abra o link em uma janela anonima: se nao carregar ali, escolha "
                "um navegador logado no app ou adicione cookies em `backend/cookies.txt`."
            )

        return (
            "O Instagram nao retornou dados para esta publicacao mesmo usando os cookies selecionados. "
            "Confirme se o link abre no navegador escolhido, se voce esta logado na conta certa, e tente "
            "fechar/reabrir o navegador antes de repetir."
        )

    if "login required" in detail_lower or "requested content is not available" in detail_lower:
        return (
            "Esta midia exige autenticacao da conta. Tente novamente escolhendo um navegador logado "
            "ou forneca `backend/cookies.txt`."
        )

    if "there is no video in this post" in detail_lower or "only images are available" in detail_lower:
        return (
            "Este post do Instagram nao tem um video baixavel, ou o Instagram retornou apenas imagens. "
            "Tente um Reel/video direto ou use cookies de uma conta que consiga ver a publicacao."
        )

    if "sign in to confirm you're not a bot" in detail_lower or "http error 429" in detail_lower:
        if browser == "none":
            return (
                "A plataforma bloqueou esta requisicao com verificacao anti-bot. "
                "No Railway, configure `YTDLP_COOKIES_BASE64` com cookies atuais. "
                "Se ja estiver configurado, o YouTube provavelmente bloqueou o IP do servidor."
            )

        return (
            "A plataforma ainda bloqueou esta requisicao mesmo com a configuracao atual. "
            "Confirme se os cookies estao atuais e pertencem a uma conta que consegue abrir o video. "
            "Se persistir no Railway, o YouTube provavelmente bloqueou o IP do servidor."
        )

    if "unable to fetch gvs po token" in detail_lower or "visitor data" in detail_lower:
        return (
            "Este video esta exigindo validacao extra. O app ja habilitou o solver JS, "
            "mas pode ser necessario usar cookies da sua conta para concluir a verificacao."
        )

    return detail.strip() or "Falha no yt-dlp"


def build_public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "url": job["url"],
        "format_id": job["format_id"],
        "browser": job["browser"],
        "status": job["status"],
        "stage": job["stage"],
        "progress": job["progress"],
        "speed": job["speed"],
        "eta": job["eta"],
        "error": job["error"],
        "title": job["title"],
        "filename": job["filename"],
        "audio_only": job["audio_only"],
        "cookie_fallback_used": job["cookie_fallback_used"],
        "file_url": job["file_url"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "completed_at": job["completed_at"],
    }


def init_jobs_db() -> None:
    with sqlite3.connect(JOBS_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                format_id TEXT NOT NULL,
                browser TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress REAL NOT NULL,
                speed TEXT,
                eta TEXT,
                error TEXT,
                title TEXT,
                filename TEXT,
                audio_only INTEGER NOT NULL,
                cookie_fallback_used INTEGER NOT NULL,
                file_url TEXT,
                media_type TEXT,
                file_path TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                workdir TEXT
            )
            """
        )
        conn.commit()


def persist_job_state(job: dict[str, Any]) -> None:
    init_jobs_db()
    with sqlite3.connect(JOBS_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO download_jobs (
                id, url, format_id, browser, status, stage, progress, speed, eta, error,
                title, filename, audio_only, cookie_fallback_used, file_url, media_type,
                file_path, created_at, updated_at, completed_at, workdir
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                url = excluded.url,
                format_id = excluded.format_id,
                browser = excluded.browser,
                status = excluded.status,
                stage = excluded.stage,
                progress = excluded.progress,
                speed = excluded.speed,
                eta = excluded.eta,
                error = excluded.error,
                title = excluded.title,
                filename = excluded.filename,
                audio_only = excluded.audio_only,
                cookie_fallback_used = excluded.cookie_fallback_used,
                file_url = excluded.file_url,
                media_type = excluded.media_type,
                file_path = excluded.file_path,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                completed_at = excluded.completed_at,
                workdir = excluded.workdir
            """,
            (
                job["id"],
                job["url"],
                job["format_id"],
                job["browser"],
                job["status"],
                job["stage"],
                job["progress"],
                job["speed"],
                job["eta"],
                job["error"],
                job["title"],
                job["filename"],
                int(job["audio_only"]),
                int(job["cookie_fallback_used"]),
                job["file_url"],
                job.get("media_type"),
                job.get("file_path"),
                job["created_at"],
                job["updated_at"],
                job["completed_at"],
                job.get("workdir"),
            ),
        )
        conn.commit()


def delete_persisted_job(job_id: str) -> None:
    init_jobs_db()
    with sqlite3.connect(JOBS_DB_PATH) as conn:
        conn.execute("DELETE FROM download_jobs WHERE id = ?", (job_id,))
        conn.commit()


def row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": row["url"],
        "format_id": row["format_id"],
        "audio_only": bool(row["audio_only"]),
        "browser": row["browser"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": row["progress"],
        "speed": row["speed"],
        "eta": row["eta"],
        "error": row["error"],
        "title": row["title"],
        "filename": row["filename"],
        "media_type": row["media_type"] or ("audio/mpeg" if row["audio_only"] else "video/mp4"),
        "file_path": row["file_path"],
        "file_url": row["file_url"],
        "cookie_fallback_used": bool(row["cookie_fallback_used"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "workdir": row["workdir"],
    }


def load_persisted_jobs() -> None:
    init_jobs_db()
    now = now_ts()
    persisted_jobs: dict[str, dict[str, Any]] = {}
    expired_job_ids: list[str] = []
    expired_paths: list[Path] = []

    with sqlite3.connect(JOBS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM download_jobs ORDER BY created_at DESC").fetchall()

    for row in rows:
        job = row_to_job(row)
        is_final = job["status"] in FINAL_JOB_STATUSES
        is_expired = now - job["updated_at"] > JOB_TTL_SECONDS

        if is_final and is_expired:
            expired_job_ids.append(job["id"])
            if job.get("workdir"):
                expired_paths.append(Path(job["workdir"]))
            continue

        if not is_final:
            job.update(
                status="failed",
                stage="Interrompido",
                error="O backend foi reiniciado durante este download. Tente novamente.",
                completed_at=now,
                updated_at=now,
                progress=min(job.get("progress") or 0.0, 99.0),
            )

        file_path = job.get("file_path")
        if job["status"] == "completed" and file_path and not Path(file_path).exists():
            job.update(
                status="failed",
                stage="Arquivo indisponivel",
                error="O arquivo deste download nao esta mais disponivel no backend.",
                file_path=None,
                file_url=None,
                completed_at=job["completed_at"] or now,
                updated_at=now,
            )

        persisted_jobs[job["id"]] = job
        persist_job_state(job)

    with download_jobs_lock:
        download_jobs.clear()
        download_jobs.update(persisted_jobs)

    for job_id in expired_job_ids:
        delete_persisted_job(job_id)

    for path in expired_paths:
        shutil.rmtree(path, ignore_errors=True)


def list_recent_jobs(limit: int = 10) -> list[dict[str, Any]]:
    if not download_jobs:
        load_persisted_jobs()
    cleanup_expired_jobs()
    with download_jobs_lock:
        jobs = sorted(
            (dict(job) for job in download_jobs.values()),
            key=lambda current: current["created_at"],
            reverse=True,
        )
    return [build_public_job(job) for job in jobs[:limit]]


def cache_key(url: str, browser: str) -> str:
    cookie_mode = get_runtime_config()["cookie_mode"]
    return f"{url.strip()}::{browser.strip()}::{cookie_mode}"


def cleanup_expired_jobs() -> None:
    expired_paths: list[Path] = []
    expired_job_ids: list[str] = []
    now = now_ts()

    with download_jobs_lock:
        for job_id, job in download_jobs.items():
            is_final = job["status"] in FINAL_JOB_STATUSES
            is_expired = now - job["updated_at"] > JOB_TTL_SECONDS
            if is_final and is_expired:
                expired_job_ids.append(job_id)
                if job.get("workdir"):
                    expired_paths.append(Path(job["workdir"]))

        for job_id in expired_job_ids:
            download_jobs.pop(job_id, None)

    for job_id in expired_job_ids:
        delete_persisted_job(job_id)

    for path in expired_paths:
        shutil.rmtree(path, ignore_errors=True)


def get_cached_info(key: str) -> dict[str, Any] | None:
    with info_cache_lock:
        entry = info_cache.get(key)
        if not entry:
            return None
        if entry["expires_at"] <= now_ts():
            info_cache.pop(key, None)
            return None
        return entry["payload"]


def set_cached_info(key: str, payload: dict[str, Any]) -> None:
    with info_cache_lock:
        info_cache[key] = {
            "payload": payload,
            "expires_at": now_ts() + INFO_CACHE_TTL_SECONDS,
        }


def extract_video_formats(data: dict[str, Any]) -> list[dict[str, Any]]:
    formats = []
    seen = set()

    for item in data.get("formats", []):
        vcodec = item.get("vcodec", "none")
        acodec = item.get("acodec", "none")
        height = item.get("height")
        fps = item.get("fps")
        resolution = item.get("resolution")
        format_note = item.get("format_note")
        ext = (item.get("ext") or "mp4").lower()
        protocol = (item.get("protocol") or "").lower()

        if vcodec == "none":
            continue
        if "m3u8" in protocol:
            continue

        if height:
            label = f"{height}p"
        elif resolution and resolution != "audio only":
            label = resolution
        elif format_note and format_note != "audio only":
            label = format_note
        else:
            label = ext.upper()

        if fps and fps > 30 and height:
            label += f" {int(fps)}fps"

        dedupe_key = (label, ext, round(fps or 0))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        if acodec and acodec != "none":
            format_selector = item["format_id"]
        else:
            format_selector = f"{item['format_id']}+bestaudio/best"

        formats.append(
            {
                "format_id": format_selector,
                "label": label,
                "height": height,
                "fps": fps,
                "ext": ext,
                "filesize": item.get("filesize") or item.get("filesize_approx"),
            }
        )

    formats.sort(
        key=lambda current: (
            current["height"] or 0,
            current.get("fps") or 0,
            current.get("filesize") or 0,
        ),
        reverse=True,
    )
    return formats


def is_running_in_container() -> bool:
    if os.getenv("APP_CONTAINERIZED") == "1":
        return True

    if any(key.startswith("RAILWAY_") for key in os.environ):
        return True

    if Path("/.dockerenv").exists():
        return True

    cgroup_file = Path("/proc/1/cgroup")
    if cgroup_file.exists():
        try:
            return "docker" in cgroup_file.read_text()
        except OSError:
            return False

    return False


def get_runtime_config() -> dict[str, Any]:
    cookies_file = get_cookies_file()
    cookie_status = describe_cookies_file(cookies_file)
    if cookies_file:
        cookie_source = "backend/cookies.txt" if cookies_file == COOKIES_FILE else "variavel de ambiente"
        return {
            "cookie_mode": "file",
            "default_browser": "none",
            "browser_options": [{"value": "none", "label": "Usar cookies"}],
            "cookie_help": f"Cookies carregados via {cookie_source}. O backend usara esses cookies para YouTube e Instagram.",
            "cookie_status": cookie_status,
            "info_cache_ttl_seconds": INFO_CACHE_TTL_SECONDS,
            "info_fetch_timeout_seconds": INFO_FETCH_TIMEOUT_SECONDS,
            "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
            "js_runtime": "node" if has_node_runtime() else "indisponivel",
            "challenge_solver": "local" if has_local_ejs_package() else YTDLP_REMOTE_COMPONENTS,
            "yt_dlp_force_ipv4": YTDLP_FORCE_IPV4,
            "yt_dlp_extractor_args": bool(YTDLP_EXTRACTOR_ARGS),
        }

    if is_running_in_container():
        return {
            "cookie_mode": "none",
            "default_browser": "none",
            "browser_options": [{"value": "none", "label": "Sem cookies"}],
            "cookie_help": "O backend esta rodando em container e nao consegue ler os navegadores do host. Use 'Sem cookies' ou monte um backend/cookies.txt para YouTube e Instagram.",
            "cookie_status": cookie_status,
            "info_cache_ttl_seconds": INFO_CACHE_TTL_SECONDS,
            "info_fetch_timeout_seconds": INFO_FETCH_TIMEOUT_SECONDS,
            "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
            "js_runtime": "node" if has_node_runtime() else "indisponivel",
            "challenge_solver": "local" if has_local_ejs_package() else YTDLP_REMOTE_COMPONENTS,
            "yt_dlp_force_ipv4": YTDLP_FORCE_IPV4,
            "yt_dlp_extractor_args": bool(YTDLP_EXTRACTOR_ARGS),
        }

    return {
        "cookie_mode": "browser",
        "default_browser": "none",
        "browser_options": [{"value": "none", "label": "Sem cookies"}, *KNOWN_BROWSERS],
        "cookie_help": "O app inicia em 'Sem cookies' para reduzir falhas no YouTube. Use cookies do navegador apenas quando YouTube ou Instagram exigirem autenticacao extra.",
        "cookie_status": cookie_status,
        "info_cache_ttl_seconds": INFO_CACHE_TTL_SECONDS,
        "info_fetch_timeout_seconds": INFO_FETCH_TIMEOUT_SECONDS,
        "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
        "js_runtime": "node" if has_node_runtime() else "indisponivel",
        "challenge_solver": "local" if has_local_ejs_package() else YTDLP_REMOTE_COMPONENTS,
        "yt_dlp_force_ipv4": YTDLP_FORCE_IPV4,
        "yt_dlp_extractor_args": bool(YTDLP_EXTRACTOR_ARGS),
    }


def cookie_args(browser: str) -> list[str]:
    config = get_runtime_config()
    cookies_file = get_cookies_file()

    if cookies_file:
        return ["--cookies", str(cookies_file)]

    if config["cookie_mode"] == "none" and browser != "none":
        raise HTTPException(
            status_code=400,
            detail="Cookies do navegador nao estao disponiveis neste ambiente. Use 'Sem cookies' ou forneca backend/cookies.txt.",
        )

    if browser and browser != "none":
        return ["--cookies-from-browser", browser]

    return []


def run_yt_dlp(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="yt-dlp nao esta instalado no backend") from exc


def should_retry_without_browser_cookies(browser: str, stderr: str) -> bool:
    if browser == "none" or get_cookies_file():
        return False

    stderr_lower = (stderr or "").lower()
    return any(pattern in stderr_lower for pattern in COOKIE_RETRY_PATTERNS)


def build_yt_dlp_command(args: list[str], browser: str) -> list[str]:
    return [
        "yt-dlp",
        "--no-playlist",
        *get_yt_dlp_runtime_args(),
        *cookie_args(browser),
        *args,
    ]


def instagram_playlist_args(url: str) -> list[str]:
    if detect_platform_from_url(url) == "instagram":
        return ["--playlist-items", "1"]
    return []


def parse_yt_dlp_json_output(stdout: str) -> dict[str, Any]:
    output = (stdout or "").strip()
    if not output:
        raise json.JSONDecodeError("empty yt-dlp output", "", 0)

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass

    parsed_items: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            parsed_items.append(item)

    if not parsed_items:
        raise json.JSONDecodeError("invalid yt-dlp json output", output, 0)

    for item in parsed_items:
        video_ext = (item.get("video_ext") or "").lower()
        if item.get("formats") or item.get("url") or (video_ext and video_ext != "none"):
            return item

    return parsed_items[0]


def normalize_info_payload(data: dict[str, Any], retried_without_cookies: bool, cached: bool) -> dict[str, Any]:
    platform_id, platform_label = detect_platform(data)
    formats = extract_video_formats(data)
    formats.insert(
        0,
        {
            "format_id": "bestvideo+bestaudio/best",
            "label": "Melhor qualidade disponivel",
            "height": None,
            "filesize": None,
        },
    )

    return {
        "title": data.get("title", "Unknown"),
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "uploader": data.get("uploader"),
        "platform": platform_id,
        "platform_label": platform_label,
        "source_url": data.get("webpage_url") or data.get("original_url"),
        "formats": formats,
        "cookie_fallback_used": retried_without_cookies,
        "cached": cached,
    }


def fetch_video_info(url: str, browser: str) -> dict[str, Any]:
    def fetch_info(active_browser: str) -> subprocess.CompletedProcess[str]:
        return run_yt_dlp(
            build_yt_dlp_command(["--dump-json", *instagram_playlist_args(url), url], active_browser),
            timeout=INFO_FETCH_TIMEOUT_SECONDS,
        )

    result = fetch_info(browser)
    retried_without_cookies = False

    if result.returncode != 0 and should_retry_without_browser_cookies(browser, result.stderr):
        fallback_result = fetch_info("none")
        if fallback_result.returncode == 0:
            result = fallback_result
            retried_without_cookies = True

    if result.returncode != 0:
        detail = summarize_yt_dlp_error(result.stderr.strip(), browser)
        raise HTTPException(status_code=400, detail=detail)

    data = parse_yt_dlp_json_output(result.stdout)
    payload = normalize_info_payload(data, retried_without_cookies, cached=False)

    if (
        len(payload["formats"]) == 1
        and not retried_without_cookies
        and should_retry_without_browser_cookies(browser, result.stderr)
    ):
        fallback_result = fetch_info("none")
        if fallback_result.returncode == 0:
            payload = normalize_info_payload(parse_yt_dlp_json_output(fallback_result.stdout), True, cached=False)

    return payload


def get_job_or_404(job_id: str) -> dict[str, Any]:
    if not download_jobs:
        load_persisted_jobs()
    cleanup_expired_jobs()
    with download_jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job nao encontrado")
        return dict(job)


def update_job(job_id: str, **changes: Any) -> dict[str, Any]:
    with download_jobs_lock:
        job = download_jobs[job_id]
        job.update(changes)
        job["updated_at"] = now_ts()
        updated_job = dict(job)

    persist_job_state(updated_job)
    return updated_job


def create_job_state(payload: DownloadJobRequest) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    workdir = DOWNLOADS_DIR / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    timestamp = now_ts()
    return {
        "id": job_id,
        "url": payload.url.strip(),
        "format_id": payload.format_id,
        "audio_only": payload.audio_only,
        "browser": payload.browser,
        "status": "queued",
        "stage": "Aguardando vaga para iniciar",
        "progress": 0.0,
        "speed": None,
        "eta": None,
        "error": None,
        "title": None,
        "filename": None,
        "media_type": "audio/mpeg" if payload.audio_only else "video/mp4",
        "file_path": None,
        "file_url": None,
        "cookie_fallback_used": False,
        "created_at": timestamp,
        "updated_at": timestamp,
        "completed_at": None,
        "workdir": str(workdir),
    }


def retry_download_job(job_id: str) -> dict[str, Any]:
    job = get_job_or_404(job_id)
    if job["status"] not in FINAL_JOB_STATUSES:
        raise HTTPException(status_code=409, detail="Este job ainda nao terminou")

    return create_download_job(
        DownloadJobRequest(
            url=job["url"],
            format_id=job["format_id"],
            audio_only=job["audio_only"],
            browser=job["browser"],
        )
    )


def resolve_download_options(audio_only: bool, format_id: str) -> tuple[str, list[str], str, str]:
    if audio_only:
        return "bestaudio/best", ["-x", "--audio-format", "mp3"], "mp3", "audio/mpeg"
    return format_id, ["--merge-output-format", "mp4"], "mp4", "video/mp4"


def resolve_video_format_selector(url: str, format_id: str) -> str:
    platform = detect_platform_from_url(url)

    if platform == "youtube":
        if format_id == "bestvideo+bestaudio/best":
            return (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                "/bestvideo[ext=mp4]+bestaudio"
                "/best[ext=mp4][vcodec!=none][acodec!=none]"
                "/bestvideo+bestaudio/best"
            )

        if "+" in format_id:
            video_format_id = format_id.split("+", 1)[0]
            return (
                f"{video_format_id}+bestaudio[ext=m4a]"
                f"/{format_id}"
                "/best[ext=mp4][vcodec!=none][acodec!=none]"
                "/best"
            )

        return (
            f"{format_id}"
            "/best[ext=mp4][vcodec!=none][acodec!=none]"
            "/best"
        )

    if platform == "instagram":
        if format_id == "bestvideo+bestaudio/best":
            return "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best/best[ext=mp4][vcodec!=none]/best[vcodec!=none]/best"
        if "+" in format_id:
            return format_id
        return f"{format_id}/best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best[ext=mp4][vcodec!=none]/best[vcodec!=none]"

    return format_id


def parse_progress_line(line: str) -> dict[str, Any]:
    cleaned = line.strip()
    if not cleaned:
        return {}

    match = DOWNLOAD_PROGRESS_RE.search(cleaned)
    if match:
        return {
            "status": "running",
            "stage": "Baixando arquivo",
            "progress": float(match.group("percent")),
            "speed": match.group("speed"),
            "eta": match.group("eta"),
        }

    lowered = cleaned.lower()
    if "destination" in lowered:
        return {"status": "running", "stage": "Preparando arquivo"}
    if "merging formats" in lowered or "[merger]" in lowered:
        return {"status": "running", "stage": "Mesclando audio e video", "progress": 97.0}
    if "extracting audio" in lowered or "[extractaudio]" in lowered:
        return {"status": "running", "stage": "Convertendo audio", "progress": 97.0}
    if "deleting original file" in lowered:
        return {"status": "running", "stage": "Finalizando", "progress": 99.0}
    return {}


def is_video_codec_compatible(video_codec: str | None, audio_codec: str | None) -> bool:
    return video_codec == "h264" and audio_codec in {None, "aac", "mp3"}


def find_output_file(workdir: Path, out_ext: str) -> Path | None:
    files = list(workdir.glob(f"*.{out_ext}"))
    if files:
        return files[0]

    fallback_files = [item for item in workdir.iterdir() if item.is_file()]
    return fallback_files[0] if fallback_files else None


def find_best_media_file(workdir: Path, audio_only: bool, preferred_ext: str) -> Path | None:
    if audio_only:
        return find_output_file(workdir, preferred_ext)

    def first_file_with_video(candidates: list[Path]) -> Path | None:
        fallback = None
        for candidate in candidates:
            if fallback is None:
                fallback = candidate

            video_codec, _ = probe_media_codecs(candidate)
            if video_codec:
                return candidate

        return fallback

    preferred_video_exts = [preferred_ext, "mp4", "mov", "mkv", "webm"]
    for ext in preferred_video_exts:
        candidates = list(workdir.glob(f"*.{ext}"))
        if candidates:
            selected = first_file_with_video(candidates)
            if selected:
                return selected

    media_files = [
        item
        for item in workdir.iterdir()
        if item.is_file() and item.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi"}
    ]
    if media_files:
        selected = first_file_with_video(media_files)
        if selected:
            return selected

    fallback_files = [item for item in workdir.iterdir() if item.is_file()]
    return fallback_files[0] if fallback_files else None


def ensure_playable_instagram_video(file_path: Path) -> Path:
    normalized_path = file_path.with_name(f"{file_path.stem}_playable.mp4")

    conversion_attempts = [
        [
            "ffmpeg",
            "-y",
            "-i",
            str(file_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-movflags",
            "+faststart",
            "-c",
            "copy",
            str(normalized_path),
        ],
        [
            "ffmpeg",
            "-y",
            "-i",
            str(file_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-movflags",
            "+faststart",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(normalized_path),
        ],
        [
            "ffmpeg",
            "-y",
            "-i",
            str(file_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-movflags",
            "+faststart",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(normalized_path),
        ],
    ]

    last_error = ""
    for cmd in conversion_attempts:
        if normalized_path.exists():
            normalized_path.unlink(missing_ok=True)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            last_error = "Tempo esgotado ao converter o video do Instagram"
            continue

        if result.returncode == 0 and normalized_path.exists():
            video_codec, audio_codec = probe_media_codecs(normalized_path)
            if is_video_codec_compatible(video_codec, audio_codec):
                break
            last_error = (
                "O Instagram gerou um MP4 com codec de video pouco compativel. "
                "Tentando uma conversao mais ampla."
            )
            continue

        last_error = result.stderr.strip() or "Falha ao converter o video do Instagram"
    else:
        raise HTTPException(
            status_code=500,
            detail=last_error or "Falha ao converter o video do Instagram para um MP4 compativel",
        )

    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        pass

    return normalized_path


def probe_media_codecs(file_path: Path) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name",
                "-of",
                "json",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="ffprobe nao esta instalado no backend") from exc
    except subprocess.TimeoutExpired:
        return None, None

    if result.returncode != 0:
        return None, None

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None, None

    video_codec = None
    audio_codec = None
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        codec_name = stream.get("codec_name")
        if codec_type == "video" and not video_codec:
            video_codec = codec_name
        if codec_type == "audio" and not audio_codec:
            audio_codec = codec_name

    return video_codec, audio_codec


def ensure_compatible_video(file_path: Path) -> Path:
    video_codec, audio_codec = probe_media_codecs(file_path)
    is_already_compatible = (
        file_path.suffix.lower() == ".mp4"
        and is_video_codec_compatible(video_codec, audio_codec)
    )
    if is_already_compatible:
        return file_path

    normalized_path = file_path.with_name(f"{file_path.stem}_playable.mp4")
    can_stream_copy = is_video_codec_compatible(video_codec, audio_codec)

    conversion_attempts: list[list[str]] = []
    if can_stream_copy:
        conversion_attempts.append(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(file_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-movflags",
                "+faststart",
                "-c",
                "copy",
                str(normalized_path),
            ]
        )

    conversion_attempts.extend(
        [
            [
                "ffmpeg",
                "-y",
                "-i",
                str(file_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-movflags",
                "+faststart",
                "-c:v",
                "h264_videotoolbox",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(normalized_path),
            ],
            [
                "ffmpeg",
                "-y",
                "-i",
                str(file_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-movflags",
                "+faststart",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(normalized_path),
            ],
        ]
    )

    last_error = ""
    for cmd in conversion_attempts:
        if normalized_path.exists():
            normalized_path.unlink(missing_ok=True)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail="ffmpeg nao esta instalado no backend") from exc
        except subprocess.TimeoutExpired:
            last_error = "Tempo esgotado ao converter o video para um MP4 compativel"
            continue

        if result.returncode == 0 and normalized_path.exists():
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                pass
            return normalized_path

        last_error = result.stderr.strip() or "Falha ao converter o video para um MP4 compativel"

    raise HTTPException(
        status_code=500,
        detail=last_error or "Falha ao converter o video para um MP4 compativel",
    )


def run_download_process(
    args: list[str],
    browser: str,
    job_id: str | None = None,
) -> tuple[int, str, bool]:
    attempts = [browser]
    if browser != "none" and not COOKIES_FILE.exists():
        attempts.append("none")

    used_cookie_fallback = False
    last_output = ""

    for index, active_browser in enumerate(attempts):
        if job_id:
            stage = "Tentando novamente sem cookies" if index > 0 else "Inicializando download"
            update_job(job_id, status="running", stage=stage, error=None)

        cmd = build_yt_dlp_command(args, active_browser)
        logger.info("Starting yt-dlp attempt for job=%s browser=%s", job_id, active_browser)

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail="yt-dlp nao esta instalado no backend") from exc

        output_lines: list[str] = []
        assert process.stdout is not None

        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                output_lines.append(line)
                output_lines = output_lines[-200:]

                if job_id:
                    progress_update = parse_progress_line(line)
                    if progress_update:
                        update_job(job_id, **progress_update)

        return_code = process.wait()
        last_output = "\n".join(output_lines)

        if return_code == 0:
            return return_code, last_output, used_cookie_fallback

        if active_browser != "none" and should_retry_without_browser_cookies(browser, last_output):
            logger.warning("Retrying job=%s without browser cookies", job_id)
            used_cookie_fallback = True
            continue

        return return_code, last_output, used_cookie_fallback

    return 1, last_output, used_cookie_fallback


def execute_download_job(job_id: str) -> None:
    cleanup_expired_jobs()
    job = get_job_or_404(job_id)
    workdir = Path(job["workdir"])
    platform = detect_platform_from_url(job["url"])

    yt_format, postprocess, out_ext, media_type = resolve_download_options(job["audio_only"], job["format_id"])
    if not job["audio_only"]:
        yt_format = resolve_video_format_selector(job["url"], yt_format)
    output_template = str(workdir / "%(title)s.%(ext)s")

    args = [
        "--newline",
        "-f",
        yt_format,
        *instagram_playlist_args(job["url"]),
        *postprocess,
        "-o",
        output_template,
        job["url"],
    ]

    download_semaphore.acquire()
    try:
        update_job(job_id, status="running", stage="Preparando download", media_type=media_type, progress=1.0)
        return_code, output_text, used_cookie_fallback = run_download_process(
            args,
            browser=job["browser"],
            job_id=job_id,
        )

        if return_code != 0:
            logger.warning("Download job failed id=%s error=%s", job_id, output_text.splitlines()[-1] if output_text else "")
            update_job(
                job_id,
                status="failed",
                stage="Falhou",
                error=summarize_yt_dlp_error(output_text, job["browser"]),
                completed_at=now_ts(),
            )
            shutil.rmtree(workdir, ignore_errors=True)
            return

        file_path = find_best_media_file(workdir, job["audio_only"], out_ext)
        if not file_path:
            update_job(
                job_id,
                status="failed",
                stage="Falhou",
                error="Arquivo baixado nao encontrado",
                completed_at=now_ts(),
            )
            shutil.rmtree(workdir, ignore_errors=True)
            return

        if not job["audio_only"]:
            update_job(job_id, status="running", stage="Verificando compatibilidade do video", progress=98.0)
            if platform == "instagram":
                file_path = ensure_playable_instagram_video(file_path)
            file_path = ensure_compatible_video(file_path)

        update_job(
            job_id,
            status="completed",
            stage="Concluido",
            progress=100.0,
            speed=None,
            eta=None,
            filename=sanitize_filename(file_path.name),
            title=file_path.stem,
            file_path=str(file_path),
            file_url=f"/download-jobs/{job_id}/file",
            cookie_fallback_used=used_cookie_fallback,
            completed_at=now_ts(),
        )
        logger.info("Download job completed id=%s file=%s", job_id, file_path.name)
    except Exception as exc:
        logger.exception("Unexpected error while running job=%s", job_id)
        update_job(
            job_id,
            status="failed",
            stage="Falhou",
            error=str(exc),
            completed_at=now_ts(),
        )
        shutil.rmtree(workdir, ignore_errors=True)
    finally:
        download_semaphore.release()


def create_download_job(payload: DownloadJobRequest) -> dict[str, Any]:
    request = DownloadJobRequest(
        url=payload.url.strip(),
        format_id=payload.format_id,
        audio_only=payload.audio_only,
        browser=payload.browser,
    )

    if not request.url:
        raise HTTPException(status_code=400, detail="URL obrigatoria")

    cleanup_expired_jobs()
    job = create_job_state(request)

    with download_jobs_lock:
        download_jobs[job["id"]] = job

    persist_job_state(job)

    worker = threading.Thread(target=execute_download_job, args=(job["id"],), daemon=True)
    worker.start()
    logger.info("Created download job id=%s audio_only=%s", job["id"], job["audio_only"])
    return build_public_job(job)


@app.on_event("startup")
def load_download_jobs_on_startup() -> None:
    ensure_env_cookies_file()
    init_jobs_db()
    load_persisted_jobs()


@app.get("/config")
def get_config():
    cleanup_expired_jobs()
    return get_runtime_config()


@app.get("/info")
def get_video_info(
    url: str = Query(...),
    browser: str = Query("none"),
):
    cleanup_expired_jobs()
    lookup_key = cache_key(url, browser)
    cached_payload = get_cached_info(lookup_key)

    if cached_payload:
        logger.info("Info cache hit url=%s browser=%s", url, browser)
        return {**cached_payload, "cached": True}

    try:
        payload = fetch_video_info(url.strip(), browser)
        set_cached_info(lookup_key, payload)
        logger.info("Fetched video info url=%s browser=%s", url, browser)
        return payload
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Tempo esgotado ao buscar video")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Falha ao processar informacoes do video")


@app.post("/download-jobs")
def start_download_job(payload: DownloadJobRequest):
    return create_download_job(payload)


@app.get("/download-jobs")
def get_recent_download_jobs(limit: int = Query(10, ge=1, le=50)):
    return list_recent_jobs(limit=limit)


@app.get("/download-jobs/{job_id}")
def get_download_job(job_id: str):
    return build_public_job(get_job_or_404(job_id))


@app.post("/download-jobs/{job_id}/retry")
def restart_download_job(job_id: str):
    return retry_download_job(job_id)


@app.get("/download-jobs/{job_id}/events")
def stream_download_job(job_id: str):
    def event_stream():
        last_payload = None
        while True:
            try:
                job = get_job_or_404(job_id)
            except HTTPException:
                yield 'data: {"status":"failed","error":"Job nao encontrado"}\n\n'
                break

            payload = json.dumps(build_public_job(job), ensure_ascii=False)
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            else:
                yield ": keep-alive\n\n"

            if job["status"] in FINAL_JOB_STATUSES:
                break

            time.sleep(JOB_EVENT_INTERVAL_SECONDS)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/download-jobs/{job_id}/file")
def download_job_file(job_id: str):
    job = get_job_or_404(job_id)
    file_path = job.get("file_path")
    filename = job.get("filename")

    if job["status"] != "completed" or not file_path or not filename:
        raise HTTPException(status_code=409, detail="Arquivo ainda nao esta pronto")

    path = Path(file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Arquivo do job nao encontrado")

    return FileResponse(
        path,
        media_type=job["media_type"],
        filename=filename,
        headers=build_download_headers(filename),
    )


@app.get("/download")
def download_video(
    url: str = Query(...),
    format_id: str = Query("bestvideo+bestaudio/best"),
    audio_only: bool = Query(False),
    browser: str = Query("none"),
):
    tmpdir = Path(tempfile.mkdtemp())
    platform = detect_platform_from_url(url)

    try:
        yt_format, postprocess, out_ext, media_type = resolve_download_options(audio_only, format_id)
        if not audio_only:
            yt_format = resolve_video_format_selector(url, yt_format)
        output_template = str(tmpdir / "%(title)s.%(ext)s")

        args = [
            "--newline",
            "-f",
            yt_format,
            *instagram_playlist_args(url),
            *postprocess,
            "-o",
            output_template,
            url,
        ]

        download_semaphore.acquire()
        try:
            return_code, output_text, _ = run_download_process(args, browser=browser)
        finally:
            download_semaphore.release()

        if return_code != 0:
            stderr_lines = [line for line in output_text.splitlines() if line]
            detail = summarize_yt_dlp_error(stderr_lines[-1] if stderr_lines else output_text, browser)
            raise HTTPException(status_code=400, detail=detail)

        file_path = find_best_media_file(tmpdir, audio_only, out_ext)
        if not file_path:
            raise HTTPException(status_code=500, detail="Arquivo baixado nao encontrado")

        if not audio_only:
            if platform == "instagram":
                file_path = ensure_playable_instagram_video(file_path)
            file_path = ensure_compatible_video(file_path)

        filename = sanitize_filename(file_path.name)

        def iterfile():
            try:
                with open(file_path, "rb") as file_stream:
                    while chunk := file_stream.read(1024 * 1024):
                        yield chunk
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        return StreamingResponse(
            iterfile(),
            media_type=media_type,
            headers=build_download_headers(filename),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error in direct download endpoint")
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))


if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="assets")


@app.get("/")
def serve_frontend_index():
    index_path = FRONTEND_DIST_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend build nao encontrado")
    return FileResponse(index_path)


@app.get("/{full_path:path}")
def serve_frontend_route(full_path: str):
    index_path = FRONTEND_DIST_DIR / "index.html"
    candidate_path = FRONTEND_DIST_DIR / full_path

    if candidate_path.is_file():
        return FileResponse(candidate_path)
    if index_path.exists():
        return FileResponse(index_path)

    raise HTTPException(status_code=404, detail="Frontend build nao encontrado")
