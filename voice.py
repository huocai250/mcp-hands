"""Voice call queue for mcp-hands 4.1: let the persona actually speak, on the phone.

The chat client cannot play audio inline, and a bare WAV link just sits there unopened.
So the phone gets a page instead: open `http://<pc>:8890/voice` once, keep it open, and
every line the persona queues is spoken out loud by the **phone's own** text-to-speech.
No API keys, no audio files, no app changes.

The same page can listen back (Chrome's SpeechRecognition): what the user says lands in
the queue as an inbound line, and the persona reads it with `call_listen` on her next
turn. That is a two-way voice call with the tools we already have.
"""
import json
import os
import threading
import time

MAX_LINES = 200


class VoiceQueue:
    """Tiny JSON-backed queue: outbound lines to speak, plus what the phone heard."""

    def __init__(self, path, max_lines=MAX_LINES):
        self.path = path
        self.max_lines = int(max_lines)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._data = self._load()
        self._seq = max([item.get("seq") or 0 for item in self._data["lines"]] or [0])

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("lines", [])
        data.setdefault("calls", [])
        return data

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------- writes
    def say(self, text, voice="", rate=1.0, interrupt=False, kind="say"):
        """Queue a line for the phone to speak."""
        body = str(text or "").strip()
        if not body:
            return None
        with self._lock:
            self._seq += 1
            line = {"seq": self._seq, "text": body[:2000], "voice": str(voice or ""),
                    "rate": float(rate or 1.0), "kind": kind, "at": time.time(),
                    "spoken": False, "interrupt": bool(interrupt)}
            self._data["lines"].append(line)
            if len(self._data["lines"]) > self.max_lines:
                self._data["lines"] = self._data["lines"][-self.max_lines:]
            self._save()
        return line

    def heard(self, text, source="phone"):
        """The phone recognised the user speaking - the persona can pick it up."""
        body = str(text or "").strip()
        if not body:
            return None
        with self._lock:
            self._seq += 1
            line = {"seq": self._seq, "text": body[:2000], "voice": "", "rate": 1.0,
                    "kind": "heard", "source": source, "at": time.time(), "spoken": True,
                    "read": False, "interrupt": False}
            self._data["lines"].append(line)
            if len(self._data["lines"]) > self.max_lines:
                self._data["lines"] = self._data["lines"][-self.max_lines:]
            self._save()
        return line

    def mark_spoken(self, seq):
        with self._lock:
            for line in self._data["lines"]:
                if line.get("seq") == int(seq):
                    line["spoken"] = True
            self._save()
        return True

    def mark_read(self, seq):
        """`spoken` means the phone pronounced it; `read` means the persona saw it."""
        with self._lock:
            for line in self._data["lines"]:
                if line.get("seq") == int(seq):
                    line["read"] = True
            self._save()
        return True

    def call_event(self, event, note=""):
        with self._lock:
            self._data["calls"].append({"event": str(event), "note": str(note)[:200],
                                        "at": time.time()})
            self._data["calls"] = self._data["calls"][-50:]
            self._save()
        return True

    # -------------------------------------------------------------------- reads
    def pending(self, since=0, kind="say"):
        """Lines still worth returning.

        kind="say"  -> what the phone has not pronounced yet
        kind="heard"-> what the user said and the persona has not read yet
        kind=""     -> everything after `since` (the page polling for transcripts)
        """
        with self._lock:
            lines = [dict(item) for item in self._data["lines"]]
        since = int(since or 0)
        out = []
        for line in lines:
            if kind and line.get("kind") != kind:
                continue
            seq = int(line.get("seq") or 0)
            if kind == "heard":
                if not line.get("read") and seq > since:
                    out.append(line)
                continue
            if since:
                if seq > since:
                    out.append(line)
                continue
            if not line.get("spoken") or line.get("interrupt"):
                out.append(line)
        return out

    def unread_heard(self, mark=True):
        """What the phone heard from the user that the persona has not read yet."""
        with self._lock:
            out = [dict(item) for item in self._data["lines"]
                   if item.get("kind") == "heard" and not item.get("read")]
            if mark:
                for item in self._data["lines"]:
                    if item.get("kind") == "heard":
                        item["read"] = True
                self._save()
        return out

    def recent(self, limit=20):
        with self._lock:
            lines = [dict(item) for item in self._data["lines"]]
        return lines[-int(limit):]

    def clear(self):
        with self._lock:
            removed = len(self._data["lines"])
            self._data["lines"] = []
            self._save()
        return removed

    def stats(self):
        with self._lock:
            lines = list(self._data["lines"])
            calls = list(self._data["calls"])
        opened = [item for item in calls if item.get("event") == "page_open"]
        started = [item for item in calls if item.get("event") == "start"]
        return {"lines": len(lines),
                "unspoken": len([l for l in lines if l.get("kind") == "say" and not l.get("spoken")]),
                "unread_heard": len([l for l in lines if l.get("kind") == "heard" and not l.get("read")]),
                "calls": len(calls),
                "last": (calls[-1]["event"] if calls else ""),
                # Two different questions: has the phone ever loaded the page, and did the
                # user actually tap 开始通话 (which is what unlocks autoplay).
                "page_opened": bool(opened),
                "page_opened_at": (opened[-1]["at"] if opened else 0),
                "connected": bool(started),
                "connected_at": (started[-1]["at"] if started else 0),
                "last_event": (calls[-1].get("event") if calls else ""),
                "last_event_at": (calls[-1].get("at") if calls else 0),
                "active_hint": (lines[-1]["at"] if lines else 0)}
