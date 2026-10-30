import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.news.errors import NewsSourceUnavailableError, NewsTranscriptUnavailableError
from app.news.video import (
    VideoAnalysis, VideoAnalyzer, VideoEvidence, VideoOptions, _Scanner,
    _same_text, _thread_call, _transcribe, describe_frame,
)
from app.news.youtube import Transcript, TranscriptSegment, YouTubeExtractor, _transcript_segments


def test_subtitle_segments_keep_original_timestamps_and_clean_text():
    fetched = [
        {"start": 2.25, "duration": 1.5, "text": " Цена\n  2990 &amp; выше "},
        SimpleNamespace(start=4, duration=2, text="Вторая строка"),
        {"start": "bad", "text": "skip"},
    ]
    assert _transcript_segments(fetched) == (
        TranscriptSegment(2.25, 1.5, "Цена 2990 & выше"),
        TranscriptSegment(4, 2, "Вторая строка"),
    )


@pytest.mark.asyncio
async def test_youtube_combines_subtitles_ocr_and_vision_in_time_order():
    meta = AsyncMock()
    meta.fetch_metadata.return_value = {"title": "Видео", "duration": 20}
    subs = AsyncMock()
    subs.fetch_transcript.return_value = Transcript(
        "Реплика", language="ru", segments=(TranscriptSegment(2.0, 1.0, "Реплика"),),
    )
    analyzer = AsyncMock()
    analyzer.analyze.return_value = VideoAnalysis(
        timeline=[VideoEvidence(1.5, "Текст на экране (OCR)", "Цена: 2990"),
                  VideoEvidence(3.0, "Описание кадра локальной моделью", "Меню игры")],
        metadata={"frames_scanned": 40, "vision_frames": 1},
    )
    source = await YouTubeExtractor(meta, subs, video_analyzer=analyzer).extract(
        "https://youtube.com/watch?v=abcdefghijk", vision_client="local-model",
    )
    assert source.raw_text.index("[00:00:01.500] Текст") < source.raw_text.index("[00:00:02.000] Субтитры")
    assert source.raw_text.index("[00:00:02.000] Субтитры") < source.raw_text.index("[00:00:03.000] Описание")
    assert source.metadata["video_analysis"]["frames_scanned"] == 40
    assert analyzer.analyze.await_args.kwargs["need_audio"] is False
    assert analyzer.analyze.await_args.kwargs["vision_client"] == "local-model"


@pytest.mark.asyncio
async def test_missing_subtitles_use_local_speech_result():
    meta = AsyncMock()
    meta.fetch_metadata.return_value = {"title": "Видео", "duration": 20}
    subs = AsyncMock()
    subs.fetch_transcript.side_effect = NewsTranscriptUnavailableError("нет субтитров")
    analyzer = AsyncMock()
    analyzer.analyze.return_value = VideoAnalysis(
        transcript=Transcript("Распознанная речь", "ru", True, (TranscriptSegment(1, 2, "Распознанная речь"),)),
        timeline=[VideoEvidence(3, "Текст на экране (OCR)", "Текст")],
        warnings=["Субтитров нет, использован Whisper"], metadata={"speech_source": "whisper"},
    )
    source = await YouTubeExtractor(meta, subs, video_analyzer=analyzer).extract("https://youtu.be/abcdefghijk")
    assert "[00:00:01.000] Речь (Whisper): Распознанная речь" in source.raw_text
    assert source.metadata["analysis_warnings"] == ["Субтитров нет, использован Whisper"]
    assert analyzer.analyze.await_args.kwargs["need_audio"] is True


def test_duplicate_detection_never_drops_changed_numbers():
    assert _same_text("Цена: 2 990 руб.", "Цена: 2 990 руб.")
    assert not _same_text("Цена: 2 990 руб.", "Цена: 3 490 руб.")
    assert not _same_text("Очень длинный текст " * 10 + "выйдет", "Очень длинный текст " * 10 + "отменён")


@pytest.mark.asyncio
async def test_video_fact_retry_splits_truncated_material_without_losing_evidence(monkeypatch):
    from app.news.lmstudio_client import LMStudioNewsClient
    from app.news.worker_errors import LlmOutputTruncatedError, SourceTooLargeError
    from app.news.worker_models import FactBundle

    client = LMStudioNewsClient(None, base_url="http://127.0.0.1:1234/v1", model="local")
    async def facts(text, index, total, budget):
        if "\n" in text:
            raise LlmOutputTruncatedError("truncated")
        return FactBundle(summary=text)
    monkeypatch.setattr(client, "_extract_facts", facts)
    result = await client._extract_video_facts("первый\nвторой\nтретий\nчетвёртый", 1, 1, 10000, [3])
    assert [item.summary for item in result] == ["первый", "второй", "третий", "четвёртый"]
    with pytest.raises(SourceTooLargeError, match="chunk limit"):
        await client._extract_video_facts("первый\nвторой", 1, 1, 10000, [0])


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["truncated", "invalid", "ollama"])
async def test_video_reduction_splits_failed_groups(monkeypatch, error_type):
    import json
    from app.news.lmstudio_client import LMStudioNewsClient
    from app.news.worker_errors import LlmOutputError, LlmOutputTruncatedError, OllamaOutputError
    from app.news.worker_models import FactBundle

    client = LMStudioNewsClient(None, base_url="http://127.0.0.1:1234/v1", model="local")
    error = {"truncated": LlmOutputTruncatedError, "invalid": LlmOutputError,
             "ollama": OllamaOutputError}[error_type]
    async def structured(system, user, _model):
        assert "случайные кадры" in system
        entries = json.loads(user)["evidence"]
        if len(entries) > 1:
            raise error("invalid output")
        return FactBundle.model_validate(entries[0])
    monkeypatch.setattr(client, "_structured_chat", structured)
    source = [FactBundle(summary="первый"), FactBundle(summary="второй")]
    result = await client._reduce_group(source, 1, 1, 10000, 5000, True)
    assert [item.summary for item in result] == ["первый", "второй"]


@pytest.mark.asyncio
async def test_video_rewrite_uses_split_retries_and_keeps_each_fragment(monkeypatch):
    import json
    from app.news.lmstudio_client import LMStudioNewsClient
    from app.news.models import ExtractedNewsSource, NewsSourceKind
    from app.news.worker_errors import LlmOutputTruncatedError
    from app.news.worker_models import FactBundle, NewsDraft

    client = LMStudioNewsClient(None, base_url="http://127.0.0.1:1234/v1", model="local",
                                max_source_chars_per_chunk=4000, max_source_chunks=64)
    complete_fragments = []
    async def structured(_system, user, response_model):
        if issubclass(response_model, NewsDraft):
            return NewsDraft(headline="Черновик", body="Публикация")
        text = json.loads(user)["source_fragment"]
        if len(text) > 2000:
            raise LlmOutputTruncatedError("truncated")
        complete_fragments.append(text)
        return FactBundle(summary=text[:20])
    monkeypatch.setattr(client, "_structured_chat", structured)
    raw = "A" * 4000 + "\n" + "B" * 4000 + "\n" + "C" * 4000
    source = ExtractedNewsSource(kind=NewsSourceKind.YOUTUBE, source_id="test", source_url=None,
                                 normalized_url=None, title="Видео", raw_text=raw,
                                 metadata={"video_analysis": {}})
    result = await client.rewrite(source)
    assert result.headline == "Черновик"
    assert "".join(complete_fragments) == raw.replace("\n", "")


def test_video_output_schemas_remain_compatible_with_local_models():
    from app.news.worker_models import VideoFactBundle, VideoNewsDraft
    assert VideoFactBundle.model_json_schema()["properties"]["facts"]["items"] == {"type": "string"}
    assert VideoNewsDraft.model_json_schema()["properties"]["warnings"]["items"] == {"type": "string"}


def test_video_output_preserves_long_fact_by_splitting_it_into_valid_items():
    from app.news.worker_models import VideoFactBundle, VideoNewsDraft
    long_text = "важная деталь " * 100
    bundle = VideoFactBundle(summary="Кратко", facts=[long_text])
    draft = VideoNewsDraft(headline="Новость", body="Текст", facts_used=[long_text])
    for values in (bundle.facts, draft.facts_used):
        assert len(values) > 1 and all(len(value) <= 500 for value in values)
        assert " ".join(values) == long_text.strip()


def test_video_evidence_selection_keeps_speech_context_and_screen_text():
    from app.news.video_grounding import select_video_evidence
    from app.news.worker_models import NewsDraft
    draft = NewsDraft(headline="Konami готовит ремейк Silent Hill", body="Релиз ожидается в 2027 году")
    raw = "\n".join([
        "[00:03:25.000] Субтитры: Ремейк Silent Hill 2 вышел ранее",
        "[00:03:38.000] Субтитры: По данным портала, ремейк первой части",
        "[00:03:41.000] Субтитры: должен выйти в 2027 году",
        "[00:03:42.000] Текст на экране (OCR): KONAMI | Silent Hill",
        "[00:08:01.000] Субтитры: Совершенно другая история",
    ])
    evidence = select_video_evidence(raw, draft, 2000)
    assert "По данным портала" in evidence and "2027 году" in evidence
    assert "Текст на экране (OCR): KONAMI | Silent Hill" in evidence
    assert "Совершенно другая история" not in evidence


def test_video_evidence_selection_covers_distinct_topics_in_a_digest():
    from app.news.video_grounding import select_video_evidence
    from app.news.worker_models import NewsDraft
    draft = NewsDraft(headline="Silent Hill и Emberville", body=(
        "По данным портала MP First, ремейк Silent Hill может выйти в 2027 году.\n\n"
        "Emberville перенесена на 7 января из-за отзывов к демоверсии."))
    raw = "\n".join([
        "[00:03:25.000] Субтитры: Ремейк Silent Hill анонсирован",
        "[00:03:38.000] Субтитры: По данным портала MP First",
        "[00:03:41.000] Субтитры: релиз возможен в 2027 году",
        "[00:07:06.000] Текст на экране (OCR): Emberville",
        "[00:07:48.000] Субтитры: игру планировали выпустить в раннем доступе",
        "[00:07:53.000] Субтитры: но выход перенесли на 7 января",
        "[00:07:59.000] Субтитры: на основе отзывов к демоверсии",
    ])
    evidence = select_video_evidence(raw, draft, 2000)
    assert "MP First" in evidence and "2027 году" in evidence
    assert "Текст на экране (OCR): Emberville" in evidence
    assert "раннем доступе" in evidence and "7 января" in evidence


@pytest.mark.asyncio
async def test_video_draft_is_checked_against_original_timestamped_evidence(monkeypatch):
    from app.news.lmstudio_client import LMStudioNewsClient
    from app.news.models import ExtractedNewsSource, NewsSourceKind
    from app.news.structured_client import NEWS_SYSTEM_PROMPT, VIDEO_CHECK_SYSTEM_PROMPT
    from app.news.worker_models import VideoNewsDraft
    client = LMStudioNewsClient(None, base_url="http://127.0.0.1:1234/v1", model="local")
    calls = []
    async def structured(system, user, _model):
        calls.append(system)
        if system == NEWS_SYSTEM_PROMPT:
            return VideoNewsDraft(headline="Silent Hill выйдет в 2027 году", body="Релиз подтверждён")
        assert system.startswith(VIDEO_CHECK_SYSTEM_PROMPT) and "не используй" in system.casefold()
        assert "По данным портала" in user and "[00:00:01.000]" in user
        assert '"speech_is_automatic":true' in user
        return VideoNewsDraft(headline="Silent Hill может выйти в 2027 году", body="По данным портала, релиз возможен в 2027 году")
    monkeypatch.setattr(client, "_structured_chat", structured)
    source = ExtractedNewsSource(kind=NewsSourceKind.YOUTUBE, source_id="test", source_url=None,
        normalized_url=None, title="Видео", raw_text="[00:00:01.000] Субтитры: По данным портала, Silent Hill может выйти в 2027 году",
        metadata={"video_analysis": {}, "transcript_generated": True})
    result = await client.rewrite(source)
    assert result.headline == "Silent Hill может выйти в 2027 году"
    assert calls[0] == NEWS_SYSTEM_PROMPT and calls[1].startswith(VIDEO_CHECK_SYSTEM_PROMPT)
    assert "имена" in result.warnings[0]


@pytest.mark.asyncio
async def test_video_draft_warns_when_early_access_qualifier_disappears(monkeypatch):
    from app.news.lmstudio_client import LMStudioNewsClient
    from app.news.models import ExtractedNewsSource, NewsSourceKind
    from app.news.worker_models import VideoNewsDraft
    client = LMStudioNewsClient(None, base_url="http://127.0.0.1:1234/v1", model="local")
    draft = VideoNewsDraft(headline="Emberville перенесли", body="Игра выйдет 7 января")
    async def structured(_system, _user, _model):
        return draft
    monkeypatch.setattr(client, "_structured_chat", structured)
    source = ExtractedNewsSource(kind=NewsSourceKind.YOUTUBE, source_id="test", source_url=None,
        normalized_url=None, title="Видео", raw_text=(
            "[00:01:00.000] Текст на экране (OCR): Emberville\n"
            "[00:01:02.000] Субтитры: ранний доступ перенесли на 7 января"),
        metadata={"video_analysis": {}})
    checked = await client._check_video_draft(source, draft)
    assert any("ранний доступ" in warning for warning in checked.warnings)


@pytest.mark.asyncio
async def test_video_fact_extraction_has_bounded_parallelism_and_preserves_order(monkeypatch):
    import asyncio
    import json
    from app.news.lmstudio_client import LMStudioNewsClient
    from app.news.models import ExtractedNewsSource, NewsSourceKind
    from app.news.worker_models import FactBundle, NewsDraft

    client = LMStudioNewsClient(None, base_url="http://127.0.0.1:1234/v1", model="local",
                                max_source_chars_per_chunk=4000, max_source_chunks=64)
    active = maximum = 0
    observed = []
    async def facts(_text, index, _total, _budget):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(.02 if index == 1 else .005)
            return FactBundle(summary=str(index))
        finally:
            active -= 1
    async def final(_system, user, _model):
        observed.extend(item["summary"] for item in json.loads(user)["evidence_from_all_chunks"])
        return NewsDraft(headline="Черновик", body="Публикация")
    monkeypatch.setattr(client, "_extract_facts", facts)
    monkeypatch.setattr(client, "_structured_chat", final)
    source = ExtractedNewsSource(kind=NewsSourceKind.YOUTUBE, source_id="test", source_url=None,
        normalized_url=None, title="Видео", raw_text="A" * 12000, metadata={"video_analysis": {}})
    await client.rewrite(source)
    assert maximum == 2
    assert observed == ["1", "2", "3"]


def test_scanner_reads_often_and_sends_new_uncertain_text_for_vision(monkeypatch, tmp_path):
    import cv2
    import app.news.video as video

    class Capture:
        def __init__(self, _path): self.index = -1
        def isOpened(self): return True
        def get(self, prop):
            return self.index * 500 if prop == cv2.CAP_PROP_POS_MSEC else 2.0
        def grab(self):
            self.index += 1
            return self.index < 9
        def retrieve(self): return True, self.index
        def release(self): pass

    values = [
        (['Цена 100'], [.99]), (['Цена 100'], [.99]),
        (['Цена 120'], [.6]), (['Цена 120'], [.6]),
        (['Цена 140'], [.65]), ([], [.1]),
        (['Цена 120'], [.6]), (['Можно купить'], [.99]), (['Цена 120'], [.6]),
    ]
    def recognize(frame):
        texts, scores = values[frame]
        return SimpleNamespace(txts=texts, scores=scores)
    monkeypatch.setattr(cv2, "VideoCapture", Capture)
    monkeypatch.setattr(cv2, "imencode", lambda *args: (True, SimpleNamespace(tobytes=lambda: b"jpeg")))
    monkeypatch.setattr(video, "_make_ocr", lambda _options: recognize)
    scanner = _Scanner(tmp_path / "video.mp4", VideoOptions(sample_fps=2))
    import threading
    evidence, candidates, _ = scanner.read_batch(10, threading.Event())
    scanner.close()
    assert [item.text for item in evidence] == ["Цена 100", "Цена 120", "Цена 140", "Цена 120", "Можно купить", "Цена 120"]
    assert [item[0] for item in candidates] == [0, 1, 2, 2.5, 3, 4]
    assert scanner.samples == 9


@pytest.mark.asyncio
async def test_vision_never_sends_frames_to_remote_endpoint():
    with pytest.raises(NewsSourceUnavailableError, match="локальная"):
        await describe_frame(SimpleNamespace(base_url="https://example.com/v1"), b"image")
    with pytest.raises(NewsSourceUnavailableError, match="локальная"):
        await describe_frame(SimpleNamespace(base_url="http://192.168.1.2:1234/v1"), b"image")


@pytest.mark.asyncio
async def test_local_vision_request_disables_reasoning_and_uses_frame_bytes():
    from app.news.lmstudio_client import LMStudioNewsClient

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self, **kwargs):
            return {"output": [{"type": "message", "content": "Цена: 2990"}]}

    class Session:
        def __init__(self): self.calls = []
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    client = LMStudioNewsClient(session, base_url="http://127.0.0.1:1234/v1", model="local")
    assert await describe_frame(client, b"image") == "Цена: 2990"
    url, request = session.calls[0]
    assert url == "http://127.0.0.1:1234/api/v1/chat"
    assert request["json"]["reasoning"] == "off" and request["json"]["store"] is False
    assert request["allow_redirects"] is False
    assert request["json"]["input"][0]["data_url"] == "data:image/jpeg;base64,aW1hZ2U="


@pytest.mark.asyncio
async def test_vision_retries_when_reasoning_option_is_unsupported():
    import copy
    from app.news.lmstudio_client import LMStudioNewsClient

    class Response:
        def __init__(self, status, payload): self.status, self.payload = status, payload
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self, **kwargs): return self.payload

    class Session:
        def __init__(self): self.bodies = []
        def post(self, url, **kwargs):
            self.bodies.append(copy.deepcopy(kwargs["json"]))
            if len(self.bodies) == 1:
                return Response(400, {"error": "unknown reasoning setting"})
            return Response(200, {"output": [{"type": "message", "content": "Текст"}],
                                  "stats": {"total_output_tokens": 256}})

    session = Session()
    client = LMStudioNewsClient(session, base_url="http://127.0.0.1:1234/v1", model="local")
    assert await describe_frame(client, b"image") == "Текст [описание могло быть обрезано]"
    assert session.bodies[0]["reasoning"] == "off"
    assert "reasoning" not in session.bodies[1]


@pytest.mark.asyncio
async def test_thread_cancellation_waits_until_local_work_stops():
    started = asyncio.Event()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    def work(stop):
        loop.call_soon_threadsafe(started.set)
        stop.wait(5)
        loop.call_soon_threadsafe(finished.set)

    task = asyncio.create_task(_thread_call(work))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_whisper_uses_local_cpu_and_preserves_segment_times(monkeypatch, tmp_path):
    import numpy as np
    import threading
    import app.news.video as video
    import imageio_ffmpeg

    monkeypatch.setenv("HF_XET_CACHE", str(tmp_path / "xet"))

    captured = {}
    class Model:
        def __init__(self, name, **kwargs): captured.update({"name": name, **kwargs})
        def transcribe(self, audio, **kwargs):
            captured["audio"] = audio
            return iter([SimpleNamespace(start=1.25, end=3.5, text="Привет")]), SimpleNamespace(language="ru")
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video, "_decode_audio", lambda *args: np.array([0.0], dtype=np.float32))
    result = _transcribe(tmp_path / "audio.mp3", VideoOptions(cache_dir=tmp_path), threading.Event())
    assert captured["device"] == "cpu" and captured["compute_type"] == "int8"
    assert isinstance(captured["audio"], np.ndarray)
    assert result.segments == (TranscriptSegment(1.25, 2.25, "Привет"),)


def test_audio_decode_uses_local_ffmpeg(tmp_path):
    import threading
    import wave
    import numpy as np
    import imageio_ffmpeg
    from app.news.video import _decode_audio

    audio = tmp_path / "test.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000)
    result = _decode_audio(audio, imageio_ffmpeg.get_ffmpeg_exe(), threading.Event())
    assert isinstance(result, np.ndarray) and len(result) == 16000
    assert not (tmp_path / "decoded-audio.f32").exists()


@pytest.mark.asyncio
async def test_video_over_duration_limit_fails_before_downloading():
    with pytest.raises(Exception, match="лимита"):
        await VideoAnalyzer(VideoOptions(max_seconds=60)).analyze(
            "https://youtube.com/watch?v=abcdefghijk", metadata={"duration": 61},
            need_audio=False, vision_client=None,
        )


@pytest.mark.asyncio
async def test_video_analyzer_routes_uncertain_frames_and_cleans_temp_files(monkeypatch, tmp_path):
    import threading
    import app.news.video as video

    closed = []
    class Scanner:
        samples = 2
        eof = False
        def __init__(self, path, options):
            assert path.is_file()
        def read_batch(self, count, stop):
            self.eof = True
            return [VideoEvidence(1.0, "Текст на экране (OCR, неуверенно)", "Цена 100")], [(1.0, b"jpeg")], 1.0
        def close(self): closed.append(True)

    def download(url, directory, options, audio, stop):
        p = directory / ("audio.dat" if audio else "video.dat")
        p.write_bytes(b"data")
        return p

    def transcribe(path, options, stop):
        return Transcript("Речь", "ru", True, (TranscriptSegment(0.5, 1.0, "Речь"),))

    async def thread_call(func, *args):
        return func(*args, threading.Event())

    vision = AsyncMock(return_value="На экране цена 100")
    monkeypatch.setattr(video, "_thread_call", thread_call)
    monkeypatch.setattr(video, "_download", download)
    monkeypatch.setattr(video, "_transcribe", transcribe)
    monkeypatch.setattr(video, "_Scanner", Scanner)
    monkeypatch.setattr(video, "describe_frame", vision)
    analyzer = VideoAnalyzer(VideoOptions(cache_dir=tmp_path))
    result = await analyzer.analyze("https://youtube.com/watch?v=abcdefghijk", metadata={"duration": 3},
                                    need_audio=True, vision_client="model")
    assert result.transcript.text == "Речь"
    assert result.metadata["vision_frames"] == 1 and result.metadata["frames_scanned"] == 2
    assert result.metadata["uncertain_ocr_events"] == 1
    assert len(result.timeline) == 2 and "Описание".casefold() in result.timeline[1].kind.casefold()
    assert closed == [True]
    vision.assert_awaited_once_with("model", b"jpeg")
    assert list((tmp_path / "tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_failed_vision_cancels_other_requests_and_cleans_video(monkeypatch, tmp_path):
    import threading
    import app.news.video as video
    cancelled = asyncio.Event()

    class Scanner:
        samples = 2
        eof = False
        def __init__(self, path, options): pass
        def read_batch(self, count, stop):
            self.eof = True
            return [], [(0, b"bad"), (1, b"pending")], 1
        def close(self): pass

    def download(url, directory, options, audio, stop):
        p = directory / "video.dat"
        p.write_bytes(b"video")
        return p

    async def thread_call(func, *args): return func(*args, threading.Event())

    async def describe(client, image):
        if image == b"bad":
            await asyncio.sleep(0)
            raise NewsSourceUnavailableError("модель недоступна")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(video, "_thread_call", thread_call)
    monkeypatch.setattr(video, "_download", download)
    monkeypatch.setattr(video, "_Scanner", Scanner)
    monkeypatch.setattr(video, "describe_frame", describe)
    with pytest.raises(NewsSourceUnavailableError, match="не завершён"):
        await VideoAnalyzer(VideoOptions(cache_dir=tmp_path)).analyze(
            "https://youtube.com/watch?v=abcdefghijk", metadata={"duration": 3},
            need_audio=False, vision_client="model",
        )
    assert cancelled.is_set()
    assert list((tmp_path / "tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_worker_prepares_draft_using_combined_youtube_evidence():
    from app.news.facade import NewsSourceFacade
    from app.news.home_runtime import HomeNewsWorker
    from app.news.worker_models import NewsDraft, NewsTask
    metadata = AsyncMock()
    metadata.fetch_metadata.return_value = {"title": "Ролик", "duration": 10}
    subtitles = AsyncMock()
    subtitles.fetch_transcript.return_value = Transcript("Речь", "ru", True, (TranscriptSegment(1, 1, "Речь"),))
    analyzer = AsyncMock()
    analyzer.analyze.return_value = VideoAnalysis(
        timeline=[VideoEvidence(2, "Текст на экране (OCR)", "Цена 2990")],
        warnings=["Проверьте OCR"], metadata={"frames_scanned": 20},
    )
    facade = NewsSourceFacade(youtube=YouTubeExtractor(metadata, subtitles, video_analyzer=analyzer))
    llm = SimpleNamespace(model="local", base_url="http://127.0.0.1:1234/v1",
                          max_source_chunks=16, youtube_max_source_chunks=64, _disable_reasoning=False)
    async def rewrite(source):
        assert llm.max_source_chunks == 64
        assert llm._disable_reasoning is True
        return NewsDraft(headline="Черновик", body="Текст")
    llm.rewrite = AsyncMock(side_effect=rewrite)
    task = NewsTask.model_validate({"id": 1, "lease_token": "test", "input_payload":
                                   {"kind": "youtube", "source_url": "https://youtu.be/abcdefghijk"}})
    api = SimpleNamespace(lease_seconds=1800, progress=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())
    await HomeNewsWorker(api=api, extractor=facade, ollama=llm)._process(task)
    source = llm.rewrite.await_args.args[0]
    assert "[00:00:01.000] Субтитры: Речь" in source.raw_text
    assert "[00:00:02.000] Текст на экране (OCR): Цена 2990" in source.raw_text
    result = api.complete.await_args.args[1]
    assert result.draft.warnings == ["Проверьте OCR"]
    assert result.source.raw_text.startswith("processed-locally sha256=")
    assert llm.max_source_chunks == 16
    assert llm._disable_reasoning is False
    api.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_worker_reports_safe_error_code_without_exception_text():
    from app.news.errors import MissingNewsDependencyError
    from app.worker_v1 import NewsAdapter

    api = SimpleNamespace(task_payload=lambda task: {"task_id": task["id"]}, call=AsyncMock())
    adapter = NewsAdapter(api, {"id": 42})
    await adapter.fail(None, MissingNewsDependencyError("sensitive detail"), retryable=False)
    payload = api.call.await_args.args[1]
    assert payload["error_code"] == "missing_news_dependency"
    assert "sensitive detail" not in str(payload)
