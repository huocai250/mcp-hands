"""MCP server: secrets, passphrases and verification helpers (stdlib only).

Nothing here ever touches the disk: passwords, TOTP codes and hashes are computed in
memory and only the caller sees them. The module stays pure ASCII and writes nothing
to stdout except protocol JSON.
"""
import base64
import hashlib
import hmac
import math
import os
import random
import re
import secrets
import string
import struct
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

LOWER = "abcdefghijklmnopqrstuvwxyz"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIGITS = "0123456789"
# Quoting characters are left out on purpose: generated secrets stay shell-transportable.
SYMBOLS = "!@#$%^&*()-_=+[]{}<>?,.:;~"
AMBIGUOUS = "Il1O0o"
MAX_ITEMS = 50

COMMON = (
    "password", "passw0rd", "123456", "12345678", "qwerty", "qwertyuiop", "letmein", "admin",
    "welcome", "iloveyou", "abc123", "monkey", "dragon", "master", "football", "princess",
    "sunshine", "trustno1", "shadow", "superman", "batman", "starwars", "whatever", "secret",
)

# ~200 common English words, used only by passphrase_gen (no network, no wordlist file).
_WORDS = """
able about above acid across action actor adapt admit adopt
adult after again agent agree ahead alarm album alert alien
alive alley allow almost alone along already also alter always
amber among amount anchor ancient angel anger angle animal ankle
answer antenna any apart apple apply april arcade arch arctic
argue armor army around arrange arrive arrow artist ascend ash
aside asked asleep asset assist assume atlas atom attack attend
august aunt author autumn avenue avoid awake award aware awful
bacon badge bagel baker balance balcony ballad bamboo banana band
bank banner barrel basic basin basket batch beach beacon beam
bean beard beast beaver begin behind belief belong below bench
berry beside best better beyond bicycle bill binary birch bird
birth biscuit bishop bitter black blade blame blanket blast blaze
blend bless blind block blossom blue board boat body bonus
book border borrow bottle bottom bought bounce bound bowl box
brain branch brave bread break breeze brick bridge bright bring
broad bronze brook brother brown brush bubble bucket budget buffalo
build bundle burger burst butter button cabin cable cactus camel
camera camp canal candle candy canoe canyon capable capital captain
carbon cargo carpet carrot castle casual cattle cause cavern cedar
""".split()
WORDS = tuple(dict.fromkeys(_WORDS))


def _limit(value, default, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(low, min(n, high))


def _verdict(bits):
    if bits < 25:
        return "very weak"
    if bits < 35:
        return "weak"
    if bits < 50:
        return "fair"
    if bits < 65:
        return "good"
    if bits < 80:
        return "strong"
    return "very strong"


def _crack_time(bits, guesses_per_second=1e10):
    """Rough offline-cracking time for a key space of 2**bits."""
    try:
        seconds = (2.0 ** float(bits)) / float(guesses_per_second)
    except OverflowError:
        return "effectively forever"
    if seconds < 1:
        return "under a second"
    units = (("years", 31557600.0), ("days", 86400.0), ("hours", 3600.0), ("minutes", 60.0))
    for name, size in units:
        if seconds >= size:
            return "%.1f %s" % (seconds / size, name)
    return "%.1f seconds" % seconds


def _mask(value):
    """sk-abcd...wxyz: never print a full secret."""
    v = str(value)
    if len(v) <= 6:
        return v[:1] + "..."
    return v[:4] + "..." + v[-4:]


def _mask_match(rule, matched):
    if rule == "assignment":
        sep = "=" if "=" in matched else ":"
        key, _, val = matched.partition(sep)
        return key + sep + _mask(val)
    if rule == "url_credentials":
        head, _, tail = matched.rpartition("@")
        scheme, _, creds = head.partition("://")
        user, _, _pw = creds.partition(":")
        return "%s://%s:%s@%s" % (scheme, user, _mask(_pw), tail)
    return _mask(matched)


srv = Server("pwd")


# --------------------------------------------------------------------- generation
@srv.tool("password_gen", "Generate random passwords with the secrets module and report their entropy.",
          {"type": "object", "properties": {"length": {"type": "integer", "default": 20}, "digits": {"type": "boolean", "default": True}, "symbols": {"type": "boolean", "default": True}, "upper": {"type": "boolean", "default": True}, "exclude_ambiguous": {"type": "boolean", "default": True}, "count": {"type": "integer", "default": 1}}, "required": []})
def password_gen(length=20, digits=True, symbols=True, upper=True, exclude_ambiguous=True, count=1):
    n = _limit(length, 20, 4, 512)
    cnt = _limit(count, 1, 1, MAX_ITEMS)
    classes = [LOWER]
    if upper:
        classes.append(UPPER)
    if digits:
        classes.append(DIGITS)
    if symbols:
        classes.append(SYMBOLS)
    if exclude_ambiguous:
        classes = ["".join(c for c in pool if c not in AMBIGUOUS) for pool in classes]
    pool = "".join(classes)
    if not pool:
        raise ValueError("no character class left; enable digits, symbols or upper")
    rng = random.SystemRandom()
    out = []
    for _ in range(cnt):
        # One character from every enabled class, then the rest from the whole pool.
        chars = [secrets.choice(pool) for pool in classes]
        chars += [secrets.choice(pool) for _ in range(n - len(chars))]
        rng.shuffle(chars)
        out.append("".join(chars))
    bits = n * math.log(len(pool), 2)
    rows = ["length=%d alphabet=%d classes=%d entropy=%.1f bits" % (n, len(pool), len(classes), bits),
            "verdict=%s" % _verdict(bits)]
    rows += ["%d. %s" % (i, pwd) for i, pwd in enumerate(out, 1)]
    return "\n".join(rows)


@srv.tool("password_strength", "Score a password: length, classes, entropy, common-pattern penalties, verdict, advice.",
          {"type": "object", "properties": {"password": {"type": "string"}}, "required": ["password"]})
def password_strength(password):
    s = "" if password is None else str(password)
    if not s:
        raise ValueError("password must not be empty")
    present = []
    pool = 0
    if re.search(r"[a-z]", s):
        present.append("lower")
        pool += len(LOWER)
    if re.search(r"[A-Z]", s):
        present.append("upper")
        pool += len(UPPER)
    if re.search(r"[0-9]", s):
        present.append("digit")
        pool += len(DIGITS)
    if re.search(r"[^0-9A-Za-z]", s):
        present.append("symbol")
        pool += len(SYMBOLS)
    other = len(set(c for c in s if not c.isascii()))
    raw = len(s) * math.log(pool, 2) if pool else 0.0

    low = s.lower()
    # Leet-normalized copy catches "p@ssw0rd"-style dictionary words.
    table = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s", "7": "t"})
    leet = low.translate(table)
    penalties = []
    hits = [word for word in COMMON if word in low or word in leet]
    if hits:
        penalties.append(("common word %s" % ",".join(hits[:3]), min(24.0, 12.0 * len(hits))))
    runs = re.findall(r"(.)\1{2,}", s)
    if runs:
        penalties.append(("repeated characters %s" % ",".join(sorted(set(runs))[:3]), min(12.0, 6.0 * len(set(runs)))))
    keys = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiop", "asdfghjkl", "zxcvbnm")
    seq = 0
    for row in keys:
        for i in range(len(row) - 2):
            if row[i:i + 3] in low:
                seq += 1
    if seq:
        penalties.append(("keyboard/alpha sequence", min(16.0, 8.0 * seq)))
    dates = re.findall(r"(?:19|20)\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", s)
    if dates:
        penalties.append(("date-like %s" % ",".join(sorted(set(dates))[:3]), min(20.0, 10.0 * len(set(dates)))))
    if len(present) == 1:
        penalties.append(("single character class (%s only)" % present[0], 15.0))
    if len(s) < 8:
        penalties.append(("shorter than 8 characters", 20.0))
    elif len(s) < 12:
        penalties.append(("shorter than 12 characters", 8.0))
    penalty = min(raw, sum(bits for _label, bits in penalties))
    adjusted = max(0.0, raw - penalty)
    verdict = _verdict(adjusted)

    advice = []
    if pool:
        need = int(math.ceil(max(0.0, 65.0 - adjusted) / math.log(pool, 2))) if adjusted < 65 else 0
        if need:
            advice.append("add about %d more character(s), or 1-2 more character classes" % need)
    if len(present) < 3:
        advice.append("mix in uppercase, digits and symbols (you use %s)" % (", ".join(present) or "none"))
    if any(label.startswith("common word") for label, _ in penalties):
        advice.append("drop the dictionary word and the leet substitution: use passphrase_gen(words=5)")
    if any(label.startswith("date-like") for label, _ in penalties):
        advice.append("remove the date/year; birthdays and years are guessed first")
    if any(label.startswith("repeated") for label, _ in penalties):
        advice.append("replace the repeated run with unrelated characters")
    if len(s) < 12:
        advice.append("prefer 16+ characters, or a 5-word passphrase")
    if not advice:
        advice.append("keep this length or longer, and never reuse it across services")

    rows = [
        "length=%d" % len(s),
        "classes=%s (%d of 4) ascii_only=%s non_ascii_chars=%d"
        % (", ".join(present) or "none", len(present), "yes" if other == 0 else "no", other),
        "charset_size=%d raw_entropy=%.1f bits" % (pool, raw),
    ]
    if penalties:
        rows.append("penalties=%s total=-%.1f bits" % ("; ".join("%s (-%.1f)" % (label, bits) for label, bits in penalties), penalty))
    else:
        rows.append("penalties=(none) total=-0.0 bits")
    rows += [
        "adjusted_entropy=%.1f bits" % adjusted,
        "offline_guess_time=%s (at 1e10 guesses/s)" % _crack_time(adjusted),
        "verdict=%s" % verdict,
        "advice=%s" % " | ".join(advice),
    ]
    return "\n".join(rows)


@srv.tool("passphrase_gen", "Generate a diceware-style passphrase from an embedded 200-word list.",
          {"type": "object", "properties": {"words": {"type": "integer", "default": 4}, "separator": {"type": "string", "default": "-"}, "count": {"type": "integer", "default": 1}}, "required": []})
def passphrase_gen(words=4, separator="-", count=1):
    n = _limit(words, 4, 2, 12)
    cnt = _limit(count, 1, 1, MAX_ITEMS)
    sep = "-" if separator is None else str(separator)
    out = [sep.join(secrets.choice(WORDS) for _ in range(n)) for _ in range(cnt)]
    bits = n * math.log(len(WORDS), 2)
    rows = ["wordlist=%d words=%d separator=%r entropy=%.1f bits" % (len(WORDS), n, sep, bits),
            "verdict=%s" % _verdict(bits)]
    rows += ["%d. %s" % (i, phrase) for i, phrase in enumerate(out, 1)]
    return "\n".join(rows)


@srv.tool("totp_now", "RFC 6238 TOTP for a base32 secret: current code plus seconds remaining.",
          {"type": "object", "properties": {"secret": {"type": "string"}, "digits": {"type": "integer", "default": 6}, "period": {"type": "integer", "default": 30}, "algo": {"type": "string", "default": "sha1"}}, "required": ["secret"]})
def totp_now(secret, digits=6, period=30, algo="sha1"):
    raw = str(secret or "").strip().replace(" ", "").replace("-", "").replace("=", "").upper()
    if not raw:
        raise ValueError("secret must not be empty")
    pad = (-len(raw)) % 8
    try:
        key = base64.b32decode(raw + "=" * pad, casefold=True)
    except Exception as exc:
        raise ValueError("secret is not valid base32 (%s)" % exc)
    name = str(algo or "sha1").strip().lower().replace("-", "")
    if name not in ("sha1", "sha256", "sha512"):
        raise ValueError("algo must be sha1, sha256 or sha512 (got %r)" % algo)
    ndigits = _limit(digits, 6, 1, 10)
    step = _limit(period, 30, 1, 3600)
    stamp = int(time.time())
    counter = stamp // step
    digest = hmac.new(key, struct.pack(">Q", counter), name).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    code = value % (10 ** ndigits)
    return "\n".join([
        "secret_base32=%s (%d chars)" % (_mask(raw), len(raw)),
        "code=%s" % str(code).zfill(ndigits),
        "digits=%d period=%d algo=%s" % (ndigits, step, name),
        "counter=%d" % counter,
        "seconds_remaining=%d" % (step - (stamp % step)),
        "unix_now=%d" % stamp,
    ])


@srv.tool("hash_compare", "Compare text against an expected hash in constant time (hmac.compare_digest).",
          {"type": "object", "properties": {"text": {"type": "string"}, "expected_hash": {"type": "string"}, "algo": {"type": "string", "default": "sha256"}}, "required": ["text", "expected_hash"]})
def hash_compare(text, expected_hash, algo="sha256"):
    name = str(algo or "sha256").strip().lower().replace("-", "")
    want = str(expected_hash or "").strip().lower()
    if ":" in want:  # tolerate "sha256:<hex>" input
        prefix, _, rest = want.partition(":")
        if prefix.replace("-", "") in hashlib.algorithms_available:
            name, want = prefix.replace("-", ""), rest.strip()
    if name not in hashlib.algorithms_available:
        raise ValueError("unknown algo %r (try md5, sha1, sha256, sha512)" % algo)
    computed = hashlib.new(name, str(text if text is not None else "").encode("utf-8")).hexdigest()
    match = bool(want) and hmac.compare_digest(computed, want)
    return "\n".join([
        "match=%s" % str(match).lower(),
        "algo=%s" % name,
        "expected_chars=%d computed_chars=%d" % (len(want), len(computed)),
        "computed=%s" % computed,
        "provided=%s" % (want or "(empty)"),
    ])


SECRET_RULES = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{12,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("url_credentials", re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("assignment", re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,}")),
]


@srv.tool("secret_scan_text", "Flag likely secrets in text (keys, tokens, private keys, passwords); values are masked.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def secret_scan_text(text):
    body = "" if text is None else str(text)
    lines = body.splitlines()
    rows = []
    for lineno, line in enumerate(lines, 1):
        for rule, rx in SECRET_RULES:
            for m in rx.finditer(line):
                rows.append("line %d: %s: %s" % (lineno, rule, _mask_match(rule, m.group(0))))
    unique = list(dict.fromkeys(rows))
    head = "findings=%d lines=%d chars=%d" % (len(unique), len(lines), len(body))
    if not unique:
        return head + "\n(no likely secret found)"
    return "\n".join([head] + unique)


@srv.tool("uuid_v4", "Generate random version-4 UUIDs.",
          {"type": "object", "properties": {"count": {"type": "integer", "default": 1}}, "required": []})
def uuid_v4(count=1):
    cnt = _limit(count, 1, 1, MAX_ITEMS)
    rows = ["count=%d version=4 entropy=122.0 bits" % cnt]
    rows += ["%d. %s" % (i, uuid.uuid4()) for i in range(1, cnt + 1)]
    return "\n".join(rows)


@srv.tool("random_string", "Random string over a custom alphabet (default letters+digits), with entropy.",
          {"type": "object", "properties": {"length": {"type": "integer", "default": 24}, "alphabet": {"type": "string", "default": ""}}, "required": []})
def random_string(length=24, alphabet=""):
    n = _limit(length, 24, 1, 4096)
    pool = str(alphabet or "") or (string.ascii_letters + DIGITS)
    pool = "".join(dict.fromkeys(pool))
    if len(pool) < 2:
        raise ValueError("alphabet needs at least 2 distinct characters")
    value = "".join(secrets.choice(pool) for _ in range(n))
    bits = n * math.log(len(pool), 2)
    return ("length=%d alphabet=%d entropy=%.1f bits\nverdict=%s\nvalue=%s"
            % (n, len(pool), bits, _verdict(bits), value))


@srv.tool("pin_gen", "Generate numeric PIN codes (leading zeros allowed).",
          {"type": "object", "properties": {"length": {"type": "integer", "default": 6}}, "required": []})
def pin_gen(length=6):
    n = _limit(length, 6, 3, 32)
    value = "".join(secrets.choice(DIGITS) for _ in range(n))
    bits = n * math.log(10, 2)
    return ("length=%d alphabet=10 entropy=%.1f bits\nverdict=%s\npin=%s"
            % (n, bits, _verdict(bits), value))


# Fixed, network-free samples; the TOTP sample is the RFC 6238 / Authenticator test secret.
SAMPLES = {
    "password_gen": {"length": 20, "digits": True, "symbols": True, "upper": True, "exclude_ambiguous": True, "count": 2},
    "password_strength": {"password": "Tr0ub4dor&3"},
    "passphrase_gen": {"words": 4, "separator": "-", "count": 2},
    "totp_now": {"secret": "JBSWY3DPEHPK3PXP", "digits": 6, "period": 30, "algo": "sha1"},
    "hash_compare": {"text": "abc", "expected_hash": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "algo": "sha256"},
    "secret_scan_text": {"text": "OPENAI_API_KEY=sk-abcdef0123456789wxyz\npassword=hunter2secret\n-----BEGIN RSA PRIVATE KEY-----"},
    "uuid_v4": {"count": 3},
    "random_string": {"length": 24, "alphabet": ""},
    "pin_gen": {"length": 6},
}

# Nothing in this server needs the network or credentials.
SAMPLES_OPTIONAL = set()


def build():
    return srv


if __name__ == "__main__":
    srv.run()
