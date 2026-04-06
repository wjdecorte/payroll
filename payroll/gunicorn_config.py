import os

from uvicorn.workers import UvicornWorker  # noqa: F401

# Worker class — UvicornWorker enables async/ASGI support under Gunicorn
worker_class = "uvicorn.workers.UvicornWorker"

# Number of worker processes; tune to (2 × CPU cores) + 1 for I/O-bound apps
workers = int(os.environ.get("WORKERS", 2))

# Bind address
bind = "0.0.0.0:9000"

# Logging
accesslog = "-"  # stdout
errorlog = "-"  # stdout
loglevel = os.environ.get("LOG_LEVEL", "info")

# Timeouts
timeout = 120
keepalive = 5
