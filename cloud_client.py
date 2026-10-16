"""HTTP client for the cloud service. Network problems -> CloudUnavailable (counts towards going
offline). The server answering with an error -> CloudRejected (the network itself is fine)."""
import gzip
import json

import requests


class CloudUnavailable(Exception):
    pass


class CloudRejected(Exception):
    pass


class CloudClient:
    def __init__(self, base_url, timeout=3.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.bytes_sent = 0
        self.bytes_received = 0

    def _call(self, method, path, body=None, params=None, stream=False):
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json"}
        if data and len(data) > 1024:                     # JSON compresses well: fewer bytes over a weak link
            data = gzip.compress(data, 3)
            headers["Content-Encoding"] = "gzip"
        try:
            r = self.session.request(method, self.base + path, data=data, params=params, stream=stream,
                                     headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise CloudUnavailable(str(e))
        self.bytes_sent += len(data or b"")
        if r.status_code >= 500:
            raise CloudUnavailable("server error %d: %s" % (r.status_code, r.text[:200]))
        if r.status_code >= 400:
            raise CloudRejected("%d: %s" % (r.status_code, r.text[:300]))
        return r

    def health(self):
        self._call("GET", "/health")

    def post_samples(self, payload):
        r = self._call("POST", "/v1/samples", payload)
        self.bytes_received += len(r.content)
        return r.json()

    def post_results(self, results):
        r = self._call("POST", "/v1/results", {"results": results})
        self.bytes_received += len(r.content)
        return r.json()

    def status(self):
        return self._call("GET", "/v1/status").json()

    def export_lines(self, session):
        r = self._call("GET", "/v1/export", params={"session": session}, stream=True)
        try:
            for line in r.iter_lines():
                if line:
                    yield line.decode()
        except requests.RequestException as e:
            raise CloudUnavailable(str(e))

    def result_lines(self, session):
        r = self._call("GET", "/v1/results", params={"session": session})
        return [l for l in r.text.splitlines() if l]
