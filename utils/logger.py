import logging
from pathlib import Path


def setup_logger(log_path: Path, debug: bool = False, mirror_path: Path | None = None) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("job_scanner")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    console = logging.StreamHandler(); console.setFormatter(formatter); console.setLevel(logger.level)
    file_handler = logging.FileHandler(log_path, encoding="utf-8"); file_handler.setFormatter(formatter); file_handler.setLevel(logging.DEBUG)
    logger.addHandler(console); logger.addHandler(file_handler)
    if mirror_path is not None and mirror_path.resolve() != log_path.resolve():
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        mirror = logging.FileHandler(mirror_path, encoding="utf-8")
        mirror.setFormatter(formatter)
        mirror.setLevel(logging.DEBUG)
        logger.addHandler(mirror)
    return logger
