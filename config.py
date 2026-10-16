"""All settings in one place. Every value can be overridden with an environment variable."""
import os
from dataclasses import dataclass


def _env(name, default, cast=str):
    value = os.environ.get(name)
    return default if value is None else cast(value)


@dataclass
class Config:
    data_dir: str = "~/.mayacardio"
    mock_host: str = "127.0.0.1"
    mock_port: int = 7100
    mock_timeout: float = 3.0        # seconds to wait for a mock reply before reconnecting
    poll_idle: float = 0.5           # idle wait between polls; each poll = 1 fsync-ed commit (bigger = fewer writes, longer loss window)
    cloud_url: str = "http://127.0.0.1:8000"
    http_timeout: float = 2.0        # per request; offline switch worst case ~ 2*2 s + 1 s probe gap
    batch_size: int = 3000           # samples per upload request
    max_batches_per_cycle: int = 10  # bounded catch-up so the loop stays responsive
    probe_interval: float = 1.0      # seconds between health probes when idle
    fail_threshold: int = 2          # consecutive failures -> offline
    success_threshold: int = 2       # consecutive successes -> online
    local_window_limit: int = 60     # windows processed per loop in offline mode

    @classmethod
    def from_env(cls):
        d = cls()
        d.data_dir = _env("PIPELINE_DATA_DIR", d.data_dir)
        d.mock_host = _env("MOCK_HOST", d.mock_host)
        d.mock_port = _env("MOCK_PORT", d.mock_port, int)
        d.cloud_url = _env("CLOUD_URL", d.cloud_url)
        d.http_timeout = _env("HTTP_TIMEOUT", d.http_timeout, float)
        d.batch_size = _env("BATCH_SIZE", d.batch_size, int)
        d.data_dir = os.path.expanduser(d.data_dir)
        return d

    @property
    def device_db(self):
        return os.path.join(self.data_dir, "device.db")

    @property
    def runtime_file(self):
        return os.path.join(self.data_dir, "runtime.json")

    @property
    def lock_file(self):
        return os.path.join(self.data_dir, "run.lock")
