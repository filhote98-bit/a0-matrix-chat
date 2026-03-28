"""Prompt injection defense for the Matrix Chat plugin.

Sanitizes all untrusted external content (messages, usernames, captions)
before it reaches the LLM agent context.
"""

import os
import re
import unicodedata

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
MAX_MESSAGE_CONTENT = 4096
MAX_USERNAME = 100
MAX_BULK_INPUT_CHARS = 200_000
MAX_MESSAGE_LIMIT = 500

# ---------------------------------------------------------------------------
# Zero-width and invisible characters to strip
# ---------------------------------------------------------------------------
_INVISIBLE_CHARS = re.compile(
    "["
    "\u200b"  # zero-width space
    "\u200c"  # zero-width non-joiner
    "\u200d"  # zero-width joiner
    "\u200e"  # left-to-right mark
    "\u200f"  # right-to-left mark
    "\u2060"  # word joiner
    "\u2061"  # function application
    "\u2062"  # invisible times
    "\u2063"  # invisible separator
    "\u2064"  # invisible plus
    "\ufeff"  # zero-width no-break space / BOM
    "\u00ad"  # soft hyphen
    "\u034f"  # combining grapheme joiner
    "\u061c"  # arabic letter mark
    "\u115f"  # hangul choseong filler
    "\u1160"  # hangul jungseong filler
    "\u17b4"  # khmer vowel inherent aq
    "\u17b5"  # khmer vowel inherent aa
    "\u180e"  # mongolian vowel separator
    "\u2028"  # line separator
    "\u2029"  # paragraph separator
    "\u202a"  # left-to-right embedding
    "\u202b"  # right-to-left embedding
    "\u202c"  # pop directional formatting
    "\u202d"  # left-to-right override
    "\u202e"  # right-to-left override
    "\u202f"  # narrow no-break space
    "\ufff9"  # interlinear annotation anchor
    "\ufffa"  # interlinear annotation separator
    "\ufffb"  # interlinear annotation terminator
    "]+"
)

# ---------------------------------------------------------------------------
# Injection patterns (compiled once at module load)
# ---------------------------------------------------------------------------
_INJECTION_PHRASES = [
    r"ignore all previous instructions",
    r"ignore prior instructions",
    r"ignore above instructions",
    r"ignore the above",
    r"disregard all previous",
    r"disregard prior instructions",
    r"forget all previous",
    r"forget your instructions",
    r"you are now",
    r"you must now",
    r"you will now",
    r"you should now",
    r"from now on",
    r"pretend you are",
    r"act as if",
    r"roleplay as",
    r"new instructions:",
    r"override:",
    r"system:",
    r"SYSTEM:",
    r"reminder:",
    r"important:",
    r"attention:",
    r"actually,? (?:the user|i) (?:want|meant|need)",
    r"\[INST\]",
    r"\[/INST\]",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"<<SYS>>",
    r"<</SYS>>",
    r"</s>",
    r"<\|endoftext\|>",
    r"<\|system\|>",
    r"<\|user\|>",
    r"<\|assistant\|>",
    r"Human:",
    r"Assistant:",
    r"### Instruction",
    r"### System",
    r"## System",
    r"the (?:previous|above|preceding) instructions (?:are|were)",
    r"do not follow (?:the|your) (?:previous|original)",
]

_INJECTION_RE = re.compile(
    r"^\s*(?:" + "|".join(_INJECTION_PHRASES) + r")",
    re.IGNORECASE | re.MULTILINE,
)

# Delimiter tags we use to wrap content
_DELIMITER_TAGS = [
    "<matrix_user_content>",
    "</matrix_user_content>",
    "<matrix_messages>",
    "</matrix_messages>",
]

_DELIMITER_RE = re.compile(
    "|".join(re.escape(tag) for tag in _DELIMITER_TAGS),
    re.IGNORECASE,
)

# Matrix room ID pattern: !<opaque_id>:<server_name>
_ROOM_ID_RE = re.compile(r"^![a-zA-Z0-9._=\-/]+:[a-zA-Z0-9.\-]+(:\d+)?$")

# Matrix user ID pattern: @<localpart>:<server_name>
_USER_ID_RE = re.compile(r"^@[a-zA-Z0-9._=\-/]+:[a-zA-Z0-9.\-]+(:\d+)?$")


# ---------------------------------------------------------------------------
# Text normalization (Unicode defense)
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    """Normalize Unicode to defeat homoglyph and invisible-char attacks."""
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_CHARS.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Sanitization functions
# ---------------------------------------------------------------------------

def sanitize_content(text: str, max_length: int = MAX_MESSAGE_CONTENT) -> str:
    """Sanitize a Matrix message body for safe LLM consumption."""
    if not text:
        return ""
    text = _normalize_text(text)
    text = _DELIMITER_RE.sub(_escape_tag, text)
    text = _INJECTION_RE.sub("[blocked: suspected prompt injection]", text)
    text = text[:max_length]
    return text


def sanitize_username(name: str, max_length: int = MAX_USERNAME) -> str:
    """Sanitize a Matrix username / display name."""
    if not name:
        return "Unknown"
    name = _normalize_text(name)
    name = name[:max_length]
    name = name.replace("\n", " ").replace("\r", " ")
    name = _DELIMITER_RE.sub(_escape_tag, name)
    name = _INJECTION_RE.sub("[blocked]", name)
    return name


def sanitize_room_name(name: str, max_length: int = MAX_USERNAME) -> str:
    """Sanitize a Matrix room name/topic."""
    if not name:
        return "unknown"
    name = _normalize_text(name)
    name = name[:max_length]
    name = name.replace("\n", " ").replace("\r", " ")
    name = _DELIMITER_RE.sub(_escape_tag, name)
    name = _INJECTION_RE.sub("[blocked]", name)
    return name


def truncate_bulk(text: str, max_length: int = MAX_BULK_INPUT_CHARS) -> str:
    """Truncate large message batches."""
    if len(text) <= max_length:
        return text
    suffix = "\n[... truncated for safety ...]"
    return text[:max_length - len(suffix)] + suffix


def clamp_limit(limit: int, default: int = 100, maximum: int = MAX_MESSAGE_LIMIT) -> int:
    """Clamp a user-provided message limit to a safe range."""
    if limit < 1:
        return default
    return min(limit, maximum)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_room_id(value: str, name: str = "room_id") -> str:
    """Validate that a string is a valid Matrix room ID.

    Returns the validated string or raises ValueError.
    """
    if not value:
        raise ValueError(f"{name} is required.")
    value = value.strip()
    if not _ROOM_ID_RE.match(value):
        raise ValueError(
            f"Invalid {name}: must be a Matrix room ID (e.g. !abc123:matrix.org)."
        )
    return value


def validate_user_id(value: str, name: str = "user_id") -> str:
    """Validate that a string is a valid Matrix user ID."""
    if not value:
        raise ValueError(f"{name} is required.")
    value = value.strip()
    if not _USER_ID_RE.match(value):
        raise ValueError(
            f"Invalid {name}: must be a Matrix user ID (e.g. @user:matrix.org)."
        )
    return value


# ---------------------------------------------------------------------------
# Auth key generation
# ---------------------------------------------------------------------------

def generate_auth_key(length: int = 32) -> str:
    """Generate a cryptographically secure URL-safe auth key."""
    import secrets
    return secrets.token_urlsafe(length)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def require_auth(config: dict) -> None:
    """Raise ValueError if Matrix credentials are not configured."""
    server = config.get("server", {})
    homeserver = (server.get("homeserver", "") or "").strip()
    if not homeserver:
        raise ValueError(
            "No Matrix homeserver configured. "
            "Set MATRIX_HOMESERVER or configure the homeserver "
            "in the Matrix Chat plugin settings."
        )
    access_token = (server.get("access_token", "") or "").strip()
    password = (server.get("password", "") or "").strip()
    if not access_token and not password:
        raise ValueError(
            "No Matrix credentials configured. "
            "Set MATRIX_ACCESS_TOKEN or MATRIX_PASSWORD, or configure "
            "credentials in the Matrix Chat plugin settings."
        )


# ---------------------------------------------------------------------------
# Secure file write helper
# ---------------------------------------------------------------------------

def secure_write_json(path, data, indent: int = 2):
    """Write JSON to a file with restrictive permissions (0o600) and atomic rename."""
    import json
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        with open(path, "w") as f:
            json.dump(data, f, indent=indent)
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _escape_tag(match: re.Match) -> str:
    """Replace angle brackets in a matched delimiter tag so it's inert."""
    return match.group(0).replace("<", "&lt;").replace(">", "&gt;")
