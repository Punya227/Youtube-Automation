"""Title / description / tag generation for uploads.

Sources, in priority order:
1. A sidecar JSON override (``metadata/<video stem>.json`` or ``incoming/<video stem>.json``).
   Any field it sets is used verbatim (after YouTube-limit sanitising).
2. The Anthropic API, when ``ANTHROPIC_API_KEY`` is configured.
3. A deterministic local template built from the filename / topic.

Nothing here looks at the video pixels or audio, so generated text is only ever
based on the filename, the optional topic and the sidecar hints. Prompts and
templates are written so they never claim to describe what is shown in the video.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("ysa.metadata")

TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_TOTAL_MAX = 500
MAX_HASHTAGS = 5  # YouTube ignores all hashtags when a video has more than 15

# YouTube video categories that accept uploads (region-independent subset).
CATEGORIES = {
    "1": "Film & Animation",
    "2": "Autos & Vehicles",
    "10": "Music",
    "15": "Pets & Animals",
    "17": "Sports",
    "19": "Travel & Events",
    "20": "Gaming",
    "22": "People & Blogs",
    "23": "Comedy",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
    "29": "Nonprofits & Activism",
}

_CATEGORY_KEYWORDS = {
    "19": ["travel", "trip", "tour", "beach", "mountain", "kashmir", "goa", "himalaya", "city", "places", "vlog"],
    "10": ["music", "song", "cover", "beat", "guitar", "piano", "singing"],
    "20": ["game", "gaming", "gameplay", "minecraft", "pubg", "bgmi", "valorant"],
    "17": ["cricket", "football", "sport", "gym", "workout", "fitness", "match"],
    "23": ["funny", "comedy", "prank", "meme", "joke"],
    "26": ["howto", "how to", "tutorial", "recipe", "cooking", "diy", "tips", "hack", "makeup"],
    "27": ["learn", "explained", "facts", "history", "lesson", "education", "study"],
    "28": ["tech", "ai", "science", "coding", "gadget", "review", "space"],
    "15": ["cat", "dog", "pet", "animal", "bird"],
    "1": ["animation", "film", "short film", "cinematic", "mythology"],
}

_NOISE_TOKENS = re.compile(
    r"^(img|vid|video|mov|dsc|pxl|final|edit|edited|export|copy|v\d+|\d{6,}|\d{4}-\d{2}-\d{2}|short|shorts)$",
    re.IGNORECASE,
)


@dataclass
class VideoMetadata:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    category_id: str = "22"
    language: str = "en"
    source: str = "template"         # template | anthropic | override | mixed
    contains_synthetic_media: bool | None = None
    audio_language: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class SidecarError(ValueError):
    pass


# --- sidecar overrides -----------------------------------------------------------

SIDECAR_KEYS = {"title", "description", "tags", "hashtags", "language", "category_id", "topic",
                "contains_synthetic_media", "audio_language"}


def find_sidecar(video_path: Path, metadata_dir: Path, incoming_dir: Path | None = None) -> Path | None:
    stem = video_path.stem
    candidates = [metadata_dir / f"{stem}.json", video_path.with_suffix(".json")]
    if incoming_dir is not None:
        candidates.append(incoming_dir / f"{stem}.json")
    return next((c for c in candidates if c.is_file()), None)


def load_sidecar(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SidecarError(f"{path.name}: invalid JSON ({exc.msg} at line {exc.lineno})") from exc
    except OSError as exc:
        raise SidecarError(f"{path.name}: cannot be read ({exc})") from exc
    if not isinstance(data, dict):
        raise SidecarError(f"{path.name}: top level must be a JSON object")
    unknown = set(data) - SIDECAR_KEYS
    if unknown:
        log.warning("Ignoring unknown keys in %s: %s", path.name, sorted(unknown))
    for key in ("tags", "hashtags"):
        if key in data and not (isinstance(data[key], list) and all(isinstance(t, str) for t in data[key])):
            raise SidecarError(f"{path.name}: '{key}' must be a list of strings")
    for key in ("title", "description", "language", "topic", "audio_language"):
        if key in data and not isinstance(data[key], str):
            raise SidecarError(f"{path.name}: '{key}' must be a string")
    if "category_id" in data:
        data["category_id"] = str(data["category_id"])
        if data["category_id"] not in CATEGORIES:
            raise SidecarError(f"{path.name}: unknown category_id {data['category_id']}")
    return {k: v for k, v in data.items() if k in SIDECAR_KEYS}


# --- helpers --------------------------------------------------------------------

def humanize_filename(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)            # camelCase -> words
    tokens = re.split(r"[\s_\-\.\+,()\[\]]+", stem)
    words = [t for t in tokens if t and not _NOISE_TOKENS.match(t)]
    text = " ".join(words).strip()
    if not text:
        return ""
    if text.isascii():
        text = " ".join(w if w.isupper() and len(w) > 1 else w.capitalize() for w in text.split())
    return text


def detect_language(text: str, default: str) -> str:
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    if re.search(r"[஀-௿]", text):
        return "ta"
    if re.search(r"[ఀ-౿]", text):
        return "te"
    if re.search(r"[ঀ-৿]", text):
        return "bn"
    return default


def guess_category(text: str, default: str) -> str:
    lowered = f" {text.lower()} "
    for category_id, words in _CATEGORY_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words):
            return category_id
    return default


def _clean_text(value: str) -> str:
    # YouTube rejects '<' and '>' in titles and descriptions.
    return value.replace("<", "").replace(">", "").replace("\x00", "").strip()


def _truncate_bytes(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip()


def normalize_hashtag(tag: str) -> str:
    cleaned = re.sub(r"[^\w]", "", tag.lstrip("#"), flags=re.UNICODE)
    return f"#{cleaned}" if cleaned else ""


def sanitize(meta: VideoMetadata) -> VideoMetadata:
    """Enforce YouTube's metadata limits."""
    title = re.sub(r"\s+", " ", _clean_text(meta.title))
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "…"
    if not title:
        title = "New Short"

    hashtags: list[str] = []
    for tag in meta.hashtags:
        normalized = normalize_hashtag(tag)
        if normalized and normalized.lower() not in {h.lower() for h in hashtags}:
            hashtags.append(normalized)
    if "#shorts" not in {h.lower() for h in hashtags}:
        hashtags.append("#Shorts")
    hashtags = hashtags[:MAX_HASHTAGS]
    if "#shorts" not in {h.lower() for h in hashtags}:
        hashtags[-1] = "#Shorts"

    tags: list[str] = []
    total = 0
    for raw in meta.tags:
        tag = re.sub(r"\s+", " ", _clean_text(raw.lstrip("#"))).replace(",", " ").strip()
        if not tag or tag.lower() in {t.lower() for t in tags} or len(tag) > 100:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if tags else 0)  # quotes + comma separators
        if total + cost > TAGS_TOTAL_MAX:
            break
        tags.append(tag)
        total += cost

    description = _clean_text(meta.description)
    # Hashtags are appended to the description at upload time; reserve room for them.
    reserve = len(("\n\n" + " ".join(hashtags)).encode("utf-8"))
    description = _truncate_bytes(description, DESCRIPTION_MAX_BYTES - reserve)

    category = meta.category_id if meta.category_id in CATEGORIES else "22"
    language = (meta.language or "en").strip()[:10] or "en"
    return VideoMetadata(title=title, description=description, tags=tags, hashtags=hashtags,
                         category_id=category, language=language, source=meta.source,
                         contains_synthetic_media=meta.contains_synthetic_media,
                         audio_language=meta.audio_language, warnings=list(meta.warnings))


def compose_description(description: str, hashtags: list[str]) -> str:
    """Final description sent to YouTube: body text followed by the hashtag line."""
    body = description.strip()
    present = {h.lower() for h in re.findall(r"#\w+", body)}
    missing = [h for h in hashtags if h.lower() not in present]
    if missing:
        body = f"{body}\n\n{' '.join(missing)}" if body else " ".join(missing)
    return _truncate_bytes(body, DESCRIPTION_MAX_BYTES)


# --- generator -------------------------------------------------------------------

_SYSTEM_PROMPT = """You write YouTube Shorts metadata for a single creator's channel.
You are given only the video's filename, an optional topic from the creator and optional hints.
You have NOT seen or heard the video. Never describe specific visuals, scenes, people, events or
claims that are not stated in the inputs, and never imply that you watched the video.
Write accurate, non-clickbait, SEO-friendly text grounded only in the given topic.
Do not invent facts, statistics, locations or promises. Do not use '<' or '>' characters.
Write in the requested language; if the inputs are clearly in another language, use that language
and report its ISO 639-1 code."""


class MetadataGenerator:
    def __init__(self, api_key: str = "", model: str = "claude-opus-5-5", default_language: str = "en",
                 default_category_id: str = "22", client: Any = None):
        self.api_key = api_key
        self.model = model
        self.default_language = default_language
        self.default_category_id = default_category_id if default_category_id in CATEGORIES else "22"
        self._client = client

    @property
    def ai_available(self) -> bool:
        return self._client is not None or bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic  # imported lazily so the app runs without the package configured
            self._client = anthropic.Anthropic(api_key=self.api_key, max_retries=3, timeout=60.0)
        return self._client

    # public -------------------------------------------------------------------
    def generate(self, filename: str, topic: str | None = None, sidecar: dict | None = None) -> VideoMetadata:
        sidecar = dict(sidecar or {})
        topic = (sidecar.get("topic") or topic or "").strip()
        subject = topic or humanize_filename(filename) or "New video"
        language = sidecar.get("language") or detect_language(f"{filename} {topic}", self.default_language)
        override_fields = {k for k in ("title", "description", "tags", "hashtags") if sidecar.get(k)}

        if override_fields == {"title", "description", "tags", "hashtags"}:
            base = VideoMetadata(title="", description="", source="override")
        elif self.ai_available:
            try:
                base = self._generate_with_anthropic(filename, topic, subject, language, sidecar)
            except Exception as exc:  # network, auth, refusal, malformed output...
                log.warning("Anthropic metadata generation failed, using local template: %s", exc)
                base = self._generate_template(subject, language)
                base.warnings.append(f"AI generation unavailable ({type(exc).__name__}); used template")
        else:
            base = self._generate_template(subject, language)

        merged = VideoMetadata(
            title=sidecar.get("title") or base.title,
            description=sidecar.get("description") or base.description,
            tags=sidecar.get("tags") or base.tags,
            hashtags=sidecar.get("hashtags") or base.hashtags,
            category_id=sidecar.get("category_id") or base.category_id
            or guess_category(subject, self.default_category_id),
            language=language if sidecar.get("language") else (base.language or language),
            source=base.source if not override_fields else ("override" if base.source == "override" else "mixed"),
            contains_synthetic_media=sidecar.get("contains_synthetic_media"),
            audio_language=sidecar.get("audio_language"),
            warnings=base.warnings,
        )
        return sanitize(merged)

    # template ------------------------------------------------------------------
    def _generate_template(self, subject: str, language: str) -> VideoMetadata:
        words = [w for w in re.findall(r"\w+", subject, flags=re.UNICODE) if len(w) > 2]
        tags = [subject] + [w for w in words if w.lower() not in {"the", "and", "for", "with"}][:10] + ["Shorts"]
        hashtags = [normalize_hashtag(w) for w in words[:3]] + ["#Shorts"]
        if language == "hi":
            description = f"{subject}\n\nऐसे और शॉर्ट्स के लिए चैनल को सब्सक्राइब करें।"
        else:
            description = f"{subject}\n\nSubscribe for more Shorts like this."
        return VideoMetadata(
            title=subject,
            description=description,
            tags=tags,
            hashtags=hashtags,
            category_id=guess_category(subject, self.default_category_id),
            language=language,
            source="template",
        )

    # anthropic -----------------------------------------------------------------
    def _generate_with_anthropic(self, filename: str, topic: str, subject: str, language: str,
                                 sidecar: dict) -> VideoMetadata:
        client = self._get_client()
        hints = {k: v for k, v in sidecar.items() if k in ("title", "description", "tags", "hashtags")}
        user_prompt = (
            f"Filename: {filename}\n"
            f"Topic from creator: {topic or '(none - infer cautiously from the filename only)'}\n"
            f"Working subject: {subject}\n"
            f"Requested language (ISO 639-1): {language}\n"
            f"Creator-fixed fields (keep consistent with these): {json.dumps(hints, ensure_ascii=False) if hints else 'none'}\n\n"
            "Return: a title under 90 characters; a 2-4 sentence description (no hashtags inside it) that "
            "frames the topic without describing specific footage; 8-15 search tags; 3-5 hashtags including "
            "#Shorts; the best category_id; and the language code you wrote in."
        )
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "category_id": {"type": "string", "enum": sorted(CATEGORIES)},
                "language": {"type": "string"},
            },
            "required": ["title", "description", "tags", "hashtags", "category_id", "language"],
            "additionalProperties": False,
        }
        request = dict(
            model=self.model,
            max_tokens=4000,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        )
        response = client.messages.create(**request)
        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("model declined the request")
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        data = json.loads(text)
        return VideoMetadata(
            title=str(data["title"]),
            description=str(data["description"]),
            tags=[str(t) for t in data.get("tags", [])],
            hashtags=[str(h) for h in data.get("hashtags", [])],
            category_id=str(data.get("category_id") or self.default_category_id),
            language=str(data.get("language") or language),
            source="anthropic",
        )
