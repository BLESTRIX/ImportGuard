import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "import_id", "job_id", "method", "path", "status", "duration_ms"):
            if hasattr(record, key):
                result[key] = getattr(record, key)
        if record.exc_info:
            result["exception"] = self.formatException(record.exc_info)
        return json.dumps(result)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
