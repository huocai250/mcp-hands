"""Media outbox for mcp-hands 4.0: give the persona a mouth.

Until now the only way to show the user something was an ad-hoc file server. The
outbox is a small, deliberate delivery channel:

    media -> stored file -> short signed URL -> phone opens it (same Wi-Fi)

Properties that matter:
  * signed: the token is `id.hmac` so a URL cannot be guessed or enumerated;
  * expiring: every item has a TTL and is deleted by `purge()`;
  * optional one-shot: `once=True` deletes the file after the first fetch;
  * auditable: the bridge logs every fetch, and `list()` shows what is waiting.

Nothing here touches the network: storage + signing only.
"""
import hashlib
import hmac
import json
import os
import shutil
import threading
import time
import uuid

KINDS = ("image", "audio", "video", "text", "archive", "file", "qr")


def _token_secret(secret, item_id, expires, once):
    blob = "%s|%s|%s" % (item_id, int(expires), 1 if once else 0)
    return hmac.new(str(secret).encode("utf-8"), blob.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


class Outbox:
    """File-backed outbox; one index.json keeps the metadata."""

    def __init__(self, root, secret="mcp-hands", default_ttl=3600, max_items=200):
        self.root = root
        self.secret = secret or "mcp-hands"
        self.default_ttl = int(default_ttl)
        self.max_items = int(max_items)
        self.files = os.path.join(root, "files")
        self.index_path = os.path.join(root, "index.json")
        self._lock = threading.Lock()
        os.makedirs(self.files, exist_ok=True)
        self._index = self._load()

    # ------------------------------------------------------------------ storage
    def _load(self):
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self):
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._index, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)

    # ------------------------------------------------------------------- writes
    def add(self, path, kind="file", ttl=None, once=False, note=""):
        """Copy a file into the outbox and return its record (with token + url path)."""
        source = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(source):
            raise FileNotFoundError(source)
        item_id = uuid.uuid4().hex[:12]
        ext = os.path.splitext(source)[1].lower()[:10]
        target = os.path.join(self.files, item_id + ext)
        shutil.copyfile(source, target)
        expires = time.time() + float(ttl if ttl is not None else self.default_ttl)
        token = _token_secret(self.secret, item_id, expires, once)
        record = {"id": item_id, "kind": str(kind or "file"), "name": os.path.basename(source),
                  "stored": target, "bytes": os.path.getsize(target), "expires": expires,
                  "once": bool(once), "note": str(note or ""), "created": time.time(),
                  "fetches": 0, "token": "%s.%s" % (item_id, token)}
        with self._lock:
            self._index[item_id] = record
            self._save()
        self.purge()
        return record

    def add_bytes(self, data, name="item.bin", kind="file", ttl=None, once=False, note=""):
        """Same as add(), for bytes the caller already has in memory.

        The extension matters: `serve_outbox` picks the Content-Type from the stored
        file's suffix, and without it a QR image went out as application/octet-stream,
        which picky phone loaders refuse.
        """
        incoming = os.path.join(self.files, "incoming_" + uuid.uuid4().hex[:8])
        suffix = os.path.splitext(str(name or ""))[1].lower()[:10]
        try:
            with open(incoming, "wb") as fh:
                fh.write(data)
            record = self.add(incoming, kind=kind, ttl=ttl, once=once, note=note)
            if suffix and not record["stored"].lower().endswith(suffix):
                wanted = record["stored"] + suffix
                try:
                    os.replace(record["stored"], wanted)
                    record["stored"] = wanted
                    with self._lock:
                        if record["id"] in self._index:
                            self._index[record["id"]]["stored"] = wanted
                            self._save()
                except OSError:
                    pass
            if name:
                record["name"] = str(name)
                with self._lock:
                    if record["id"] in self._index:
                        self._index[record["id"]]["name"] = str(name)
                        self._save()
            return record
        finally:
            try:
                os.remove(incoming)
            except OSError:
                pass

    # -------------------------------------------------------------------- reads
    def resolve(self, token):
        """(record, path) for a valid token, else (None, reason)."""
        text = str(token or "").strip()
        if "." not in text:
            return None, "malformed token"
        item_id, signature = text.split(".", 1)
        with self._lock:
            record = self._index.get(item_id)
        if not record:
            return None, "unknown or expired item"
        if time.time() > float(record.get("expires") or 0):
            self.drop(item_id)
            return None, "expired"
        expected = _token_secret(self.secret, item_id, record["expires"], record.get("once"))
        if not hmac.compare_digest(expected, signature):
            return None, "bad signature"
        if record.get("once") and int(record.get("fetches") or 0) > 0:
            return None, "already used (one-shot link)"
        path = record.get("stored")
        if not path or not os.path.isfile(path):
            return None, "file is gone"
        return record, path

    def touch(self, item_id):
        """Count a fetch and honour one-shot items."""
        with self._lock:
            record = self._index.get(item_id)
            if not record:
                return None
            record["fetches"] = int(record.get("fetches") or 0) + 1
            once = bool(record.get("once"))
            self._save()
            copy = dict(record)
        if once:
            self.drop(item_id)
        return copy

    def list(self, include_expired=False, limit=50):
        now = time.time()
        with self._lock:
            items = list(self._index.values())
        out = [dict(item) for item in items
               if include_expired or float(item.get("expires") or 0) > now]
        out.sort(key=lambda item: item.get("created") or 0, reverse=True)
        return out[:int(limit)]

    def stats(self):
        items = self.list()
        return {"items": len(items),
                "bytes": sum(int(item.get("bytes") or 0) for item in items),
                "kinds": sorted({item.get("kind") for item in items})}

    # ------------------------------------------------------------------ cleanup
    def drop(self, item_id):
        with self._lock:
            record = self._index.pop(item_id, None)
            self._save()
        if record:
            try:
                os.remove(record.get("stored") or "")
            except OSError:
                pass
        return record

    def purge(self):
        """Drop expired items and enforce the item cap; returns how many were removed."""
        now = time.time()
        removed = 0
        with self._lock:
            expired = [key for key, item in self._index.items()
                       if float(item.get("expires") or 0) <= now]
            for key in expired:
                record = self._index.pop(key, None)
                if record:
                    try:
                        os.remove(record.get("stored") or "")
                    except OSError:
                        pass
                    removed += 1
            if len(self._index) > self.max_items:
                oldest = sorted(self._index.items(), key=lambda kv: kv[1].get("created") or 0)
                for key, record in oldest[:len(self._index) - self.max_items]:
                    self._index.pop(key, None)
                    try:
                        os.remove(record.get("stored") or "")
                    except OSError:
                        pass
                    removed += 1
            if removed:
                self._save()
        return removed
