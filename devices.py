"""Device pairing for mcp-hands 4.0: who is allowed to drive this PC.

The proxy used to accept any caller that could reach the port. With
`devices.mode = "allowlist"` only *approved* devices may use it:

  * a request from an unknown upstream key is refused (401) and remembered as a
    **pending** device;
  * the user approves it once in the console / with `--devices-approve`, and that
    device can then use the PC;
  * every device can be named and revoked; the last-seen time and call count are
    tracked, so "which device did what" is answerable.

Keys are stored as salted hashes, never in clear text.
"""
import hashlib
import json
import os
import secrets
import threading
import time

MODES = ("off", "allowlist")


def _hash(key, salt):
    return hashlib.sha256(("%s|%s" % (salt, key)).encode("utf-8")).hexdigest()


def mask_key(key):
    text = str(key or "")
    if len(text) <= 10:
        return "***"
    return text[:6] + "…" + text[-4:]


class DeviceStore:
    """JSON-backed device registry (no SQLite needed: it stays tiny)."""

    def __init__(self, path, salt=""):
        self.path = path
        self.salt = str(salt or secrets.token_hex(8))
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("salt", self.salt)
        data.setdefault("devices", {})
        data.setdefault("pending", {})
        self.salt = data["salt"]
        return data

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # -------------------------------------------------------------------- check
    def identify(self, key):
        """Device record for an upstream key, or None."""
        text = str(key or "").strip()
        if not text:
            return None
        digest = _hash(text, self.salt)
        with self._lock:
            for device_id, device in self._data["devices"].items():
                if device.get("hash") == digest:
                    device["last_seen"] = time.time()
                    device["calls"] = int(device.get("calls") or 0) + 1
                    self._save()
                    return dict(device, id=device_id)
        return None

    def remember_pending(self, key, user_agent="", ip=""):
        """Unknown key: park it as a pending request the user can approve."""
        digest = _hash(str(key or ""), self.salt)
        with self._lock:
            for pending_id, item in self._data["pending"].items():
                if item.get("hash") == digest:
                    item["seen"] = int(item.get("seen") or 0) + 1
                    item["last_seen"] = time.time()
                    self._save()
                    return dict(item, id=pending_id)
            pending_id = "pend_" + secrets.token_hex(6)
            item = {"id": pending_id, "hash": digest, "masked": mask_key(key),
                    "user_agent": str(user_agent or "")[:160], "ip": str(ip or ""),
                    "first_seen": time.time(), "last_seen": time.time(), "seen": 1}
            self._data["pending"][pending_id] = item
            self._save()
            return dict(item)

    def approve(self, pending_id, name=""):
        with self._lock:
            item = self._data["pending"].pop(str(pending_id), None)
            if not item:
                return None
            device_id = "dev_" + secrets.token_hex(6)
            device = {"id": device_id, "name": str(name or item.get("masked") or device_id),
                      "hash": item["hash"], "masked": item.get("masked"),
                      "created": time.time(), "last_seen": 0, "calls": 0,
                      "user_agent": item.get("user_agent", "")}
            self._data["devices"][device_id] = device
            self._save()
            return dict(device)

    def reject(self, pending_id):
        with self._lock:
            item = self._data["pending"].pop(str(pending_id), None)
            if item:
                self._save()
            return bool(item)

    def revoke(self, device_id):
        with self._lock:
            device = self._data["devices"].pop(str(device_id), None)
            if device:
                self._save()
            return bool(device)

    def rename(self, device_id, name):
        with self._lock:
            device = self._data["devices"].get(str(device_id))
            if not device:
                return None
            device["name"] = str(name or device.get("name"))
            self._save()
            return dict(device)

    # -------------------------------------------------------------------- reads
    def devices(self):
        with self._lock:
            items = [dict(device, id=key) for key, device in self._data["devices"].items()]
        items.sort(key=lambda item: item.get("created") or 0)
        return items

    def pending(self):
        with self._lock:
            items = [dict(item, id=key) for key, item in self._data["pending"].items()]
        items.sort(key=lambda item: item.get("first_seen") or 0)
        return items

    def stats(self):
        devices = self.devices()
        return {"devices": len(devices), "pending": len(self.pending()),
                "calls": sum(int(d.get("calls") or 0) for d in devices),
                "last_seen": max([d.get("last_seen") or 0 for d in devices] or [0])}
