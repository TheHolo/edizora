"""Select timestamped video evidence for a local check of a proposed draft."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from app.news.worker_models import NewsDraft

_TIMED_LINE = re.compile(r"^\[(\d+):(\d{2}):(\d{2})(?:\.\d+)?\] (.+)$")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = frozenset({
    "автор", "будет", "будут", "важные", "видео", "года", "году", "данные", "игра", "игре",
    "игры", "известно", "издание", "компания", "которые", "который", "новая", "новости", "новую",
    "новый", "одной", "планирует", "после", "проекта", "серии", "стало", "также", "теперь",
    "части", "через", "часть", "этого", "этой", "это", "the", "and", "that", "with", "from",
})
_SPEECH = ("Субтитры:", "Речь (Whisper):")
_OCR = ("Текст на экране (OCR",)
_VISION = ("Возможное описание кадра",)


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN.findall(text.casefold())
            if len(token) >= 4 and token not in _STOP_WORDS}


def select_video_evidence(raw_text: str, draft: NewsDraft, max_characters: int) -> str:
    """Return relevant original lines with their timestamps, prioritizing speech and OCR."""
    sections = [draft.headline, draft.lead, *draft.body.split("\n\n")]
    queries = [tokens for section in sections if (tokens := _tokens(section))]
    if not queries or max_characters < 128:
        return ""

    windows: dict[int, list[tuple[int, str, str]]] = defaultdict(list)
    for order, line in enumerate(raw_text.splitlines()):
        match = _TIMED_LINE.match(line)
        if not match:
            continue
        label = match.group(4)
        if not label.startswith((*_SPEECH, *_OCR, *_VISION)):
            continue
        seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
        windows[seconds // 30].append((order, line, label))
    if not windows:
        return ""

    vocabulary = {window: _tokens(" ".join(row[2] for row in rows)) for window, rows in windows.items()}
    frequency = Counter(token for tokens in vocabulary.values() for token in tokens)
    anchors: dict[int, set[str]] = {}
    for query in queries:
        scores = {window: sum(1 + math.log((len(windows) + 1) / (frequency[token] + 1))
                              for token in tokens & query)
                  for window, tokens in vocabulary.items()}
        ranked = sorted(scores, key=lambda window: (-scores[window], window))
        best = scores[ranked[0]]
        for anchor in ranked[:2]:
            if best <= 0 or scores[anchor] < best * 0.65:
                break
            if anchor in anchors or len(anchors) < 8:
                anchors.setdefault(anchor, set()).update(query)
    if not anchors:
        return ""
    # Give each topic room in the verification context, instead of letting the
    # headline consume the entire budget for a multi-topic video.
    selected: dict[int, str] = {}
    remaining = max_characters
    for position, (anchor, query) in enumerate(anchors.items()):
        quota = remaining // (len(anchors) - position)
        used = 0
        seen_ocr: set[str] = set()
        rows = [row for window in (anchor, anchor + 1, anchor - 1) for row in windows.get(window, [])]
        relevant_ocr = [row for row in rows if row[2].startswith(_OCR) and _tokens(row[2]) & query]
        speech = [row for row in rows if row[2].startswith(_SPEECH)]
        relevant_vision = [row for row in rows if row[2].startswith(_VISION) and _tokens(row[2]) & query]
        for order, line, label in [*relevant_ocr, *speech, *relevant_vision]:
            if order in selected:
                continue
            if label.startswith(_OCR):
                normalized = " ".join(label.split()).casefold()
                if normalized in seen_ocr or len(seen_ocr) >= 3:
                    continue
                seen_ocr.add(normalized)
            cost = len(line) + 2
            if used + cost <= quota:
                selected[order] = line
                used += cost
        remaining -= used
    return "\n".join(line for _, line in sorted(selected.items()))
