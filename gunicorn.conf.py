"""Keep signed media URLs out of Render's Gunicorn logs."""
import logging
import os
import re

from gunicorn.glogging import Logger


class SupplierFileRedaction(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if "/tms/supplier-files/" in message:
            record.msg = re.sub(r"(/tms/supplier-files/[^\s?]+)\?[^\s\"']+",
                                r"\1?[redacted]", message)
            record.args = ()
        return True


class SafeRenderLogger(Logger):
    def setup(self, cfg):
        super().setup(cfg)
        self.error_log.addFilter(SupplierFileRedaction())


if os.getenv("RENDER") == "true":
    timeout = 120  # Batch imports verify multiple read-only carrier endpoints before committing.
    # U is the path only. Avoid query strings, raw request lines and referrers.
    access_log_format = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s'
    logger_class = SafeRenderLogger
