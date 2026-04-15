"""Structured logging."""
import structlog
import logging
from xmclaw.utils.paths import get_logs_dir


def setup_logging():
    log_dir = get_logs_dir()
    log_dir.mkdir(exist_ok=True)

    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
        handlers=[
            logging.FileHandler(log_dir / "xmclaw.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger()


logger = setup_logging()
