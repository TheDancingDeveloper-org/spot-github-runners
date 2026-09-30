"""Human-readable errors and one physical line per logging record."""
from __future__ import annotations

import json
import logging
import re
import subprocess


def single_line(value: object) -> str:
    return ' '.join(str(value).split())


def error_detail(value: object) -> str:
    """Summarize Google error envelopes without dumping their JSON structure."""
    raw = (value.stderr or value.stdout or str(value)) if isinstance(value, subprocess.CalledProcessError) else str(value)
    prefix, separator, body = raw.partition('{')
    if separator:
        try:
            payload = json.loads('{' + body)
        except (ValueError, TypeError):
            pass
        else:
            error = payload.get('error', payload) if isinstance(payload, dict) else None
            if isinstance(error, dict) and isinstance(error.get('message'), str):
                errors = error.get('errors', [])
                errors = errors if isinstance(errors, list) else []
                reasons = [str(item['reason']) for item in errors
                           if isinstance(item, dict) and item.get('reason')]
                if error.get('status'):
                    reasons.append(str(error['status']))
                suffix = f" ({', '.join(dict.fromkeys(reasons))})" if reasons else ''
                code = error.get('code')
                if code is not None and not re.search(r'HTTP \d+', prefix):
                    prefix += f'Google API HTTP {code}: '
                return single_line(prefix + error['message'] + suffix)
    return single_line(raw)[:1200]


class SingleLineFormatter(logging.Formatter):
    """Flatten messages, formatted arguments, stack information and exceptions."""
    def format(self, record: logging.LogRecord) -> str:
        return single_line(super().format(record))


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(SingleLineFormatter('%(asctime)s %(levelname)s %(message)s'))
