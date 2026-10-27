from copy import deepcopy
from dataclasses import asdict, dataclass, replace

from app.domain.exceptions import InvalidMediaSelectionError
from app.domain.models import SourcePost


@dataclass(frozen=True)
class AlbumState:
    catalog: dict[int, dict]
    selection: list[int]
    initial: list[int]
    revision: int
    original_count: int


def initialize_album(post: SourcePost, selection: list[int], *, automatic: bool = False) -> None:
    catalog = {str(i): asdict(item) for i, item in enumerate(post.media_items, 1)}
    post.metadata = {
        **post.metadata,
        "original_media_count": len(catalog),
        "selected_media_numbers": list(selection),
        "artwork_album": {
            "catalog": catalog, "selection": list(selection), "initial": list(selection),
            "revision": 0, "automatic_selection": automatic,
        },
    }
    post.media_items = [replace(post.media_items[n - 1], order=i) for i, n in enumerate(selection)]


def read_album(post_data: dict) -> AlbumState:
    metadata = post_data.get("metadata", {})
    album = metadata.get("artwork_album")
    if album is not None:
        catalog = {int(n): item for n, item in album["catalog"].items()}
        return AlbumState(
            catalog, list(album["selection"]), list(album["initial"]), album["revision"],
            metadata.get("original_media_count", len(catalog)),
        )
    # Old drafts retain only selected images. Preserve their original numbering.
    media = post_data.get("media_items", [])
    numbers = metadata.get("selected_media_numbers", list(range(1, len(media) + 1)))
    if len(numbers) != len(media):
        numbers = list(range(1, len(media) + 1))
    return AlbumState(
        dict(zip(numbers, media, strict=True)), list(numbers), list(numbers), 0,
        metadata.get("original_media_count", len(media)),
    )


def select_album(post_data: dict, numbers: list[int], *, limit: int | None) -> dict:
    album = read_album(post_data)
    if any(type(n) is not int or n not in album.catalog for n in numbers):
        raise InvalidMediaSelectionError("Такого номера нет в исходном альбоме.")
    if len(set(numbers)) != len(numbers):
        raise InvalidMediaSelectionError("Каждый номер можно указать только один раз.")
    if limit is not None and len(numbers) > limit:
        raise InvalidMediaSelectionError(f"Можно выбрать максимум {limit} изображений. Сначала исключите лишнее.")
    data = deepcopy(post_data)
    data["media_items"] = [{**album.catalog[n], "order": i} for i, n in enumerate(numbers)]
    metadata = data.setdefault("metadata", {})
    metadata["original_media_count"] = album.original_count
    metadata["selected_media_numbers"] = list(numbers)
    metadata["artwork_album"] = {
        "catalog": {str(n): item for n, item in album.catalog.items()},
        "selection": list(numbers), "initial": album.initial,
        "revision": album.revision + 1, "automatic_selection": False,
    }
    return data
