from html import escape

from app.domain.exceptions import MediaValidationError
from app.domain.models import SourcePost
from app.utils.tags import hashtags
from app.utils.text import provider_label, shorten, telegram_html_length

DEFAULT_TEMPLATE = (
    '🖼 <b>{original_title}</b>\n{translated_title_block}\n{description_block}'
    '{author_block}'
    '🔗 <a href="{source_url}">{source_label}</a>\n'
    '{published_at_block}{media_count_block}\n{hashtags_block}'
)


class CaptionService:
    def __init__(self, limit: int = 1024) -> None:
        self.limit = limit

    def build(self, post: SourcePost, tags: list[str], template: str = DEFAULT_TEMPLATE) -> str:
        title = post.title or "Без названия"
        translated_title = str(post.metadata.get("title_translation") or "").strip()
        if translated_title.casefold() == title.casefold():
            translated_title = ""
        description = shorten(post.description, 240)
        current_tags = list(tags)

        def render() -> str:
            provider_name = provider_label(post.provider)
            author_name = escape(post.author_name or "Неизвестный автор")
            author = (
                f'<a href="{escape(post.author_url, quote=True)}">{author_name}</a>'
                if post.author_url else author_name
            )
            values = {
                # Preserve the combined title placeholder in existing custom templates.
                "title": escape(f"{title} (TL: {translated_title})" if translated_title else title),
                "original_title": escape(title),
                "translated_title_block": f"<i>↳ {escape(translated_title)}</i>\n" if translated_title else "",
                "author_name": author_name,
                "author_url": escape(post.author_url, quote=True),
                "author_block": f"🎨 {author}\n" if post.provider != "direct" else "",
                "source_url": escape(post.normalized_url, quote=True),
                "provider_name": escape(provider_name),
                "source_label": escape(f"Оригинал на {provider_name}" if post.provider != "direct" else "Открыть оригинал"),
                "description_block": f"{escape(description)}\n\n" if description else "",
                "published_at_block": f"📅 Дата оригинала: {post.published_at:%d.%m.%Y}\n" if post.published_at else "",
                "media_count": str(len(post.media_items)),
                "media_count_block": f"📚 Изображений: {len(post.media_items)}\n" if len(post.media_items) > 1 else "",
                "hashtags": hashtags(current_tags),
                "hashtags_block": f"🏷 {hashtags(current_tags)}" if current_tags else "",
            }
            try:
                return template.format_map(values).rstrip()
            except (KeyError, ValueError, IndexError, AttributeError) as error:
                raise MediaValidationError(
                    "Ошибка caption_template канала. Проверьте placeholders в docs/configuration.md."
                ) from error

        caption = render()
        if telegram_html_length(caption) <= self.limit:
            return caption
        while description and telegram_html_length(caption) > self.limit:
            description = shorten(description, max(0, len(description) - (telegram_html_length(caption) - self.limit) - 1))
            caption = render()
        while current_tags and telegram_html_length(caption) > self.limit:
            current_tags.pop()
            caption = render()
        if translated_title and telegram_html_length(caption) > self.limit:
            translated_title = ""
            caption = render()
        while len(title) > 1 and telegram_html_length(caption) > self.limit:
            target_length = max(1, len(title) - (telegram_html_length(caption) - self.limit))
            shortened_title = "…" if target_length == 1 else title[: target_length - 1].rstrip() + "…"
            if shortened_title == title:
                break
            title = shortened_title
            caption = render()
        if telegram_html_length(caption) > self.limit:
            raise MediaValidationError("Обязательные поля подписи превышают лимит Telegram")
        return caption

    def build_custom(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise MediaValidationError("Подпись не может быть пустой")
        if len(value) > self.limit:
            raise MediaValidationError(
                f"Подпись превышает лимит Telegram: {len(value)} из {self.limit} символов"
            )
        return escape(value)
