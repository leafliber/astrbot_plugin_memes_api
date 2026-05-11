import asyncio
import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional, Union, cast

import httpx
from astrbot.api import logger
from pydantic import BaseModel


class NetworkError(Exception):
    pass


class MemeGeneratorException(Exception):
    message: str

    def __str__(self):
        return self.message


class RequestError(MemeGeneratorException):
    error: str
    status: Optional[int] = None
    url: Optional[str] = None


class IOError_(MemeGeneratorException):
    error: str


class ImageDecodeError(MemeGeneratorException):
    error: str


class ImageEncodeError(MemeGeneratorException):
    error: str


class ImageAssetMissing(MemeGeneratorException):
    path: str


class DeserializeError(MemeGeneratorException):
    error: str


class ImageNumberMismatch(MemeGeneratorException):
    min: int
    max: int
    actual: int


class TextNumberMismatch(MemeGeneratorException):
    min: int
    max: int
    actual: int


class TextOverLength(MemeGeneratorException):
    text: str


class MemeFeedback(MemeGeneratorException):
    feedback: str


BASE_URL = "http://127.0.0.1:2233"


def set_base_url(url: str):
    global BASE_URL
    BASE_URL = url


async def download_url(url: str) -> bytes:
    async with httpx.AsyncClient() as client:
        for i in range(3):
            try:
                resp = await client.get(url, timeout=20)
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                logger.warning(f"Error downloading {url}, retry {i}/3: {e}")
                await asyncio.sleep(3)
    raise NetworkError(f"{url} 下载失败！")


async def send_request(
    router: str,
    request_type: Literal["POST", "GET"],
    response_type: Literal["JSON", "BYTES", "TEXT"],
    **kwargs,
):
    async with httpx.AsyncClient(timeout=60) as client:
        request_method = client.post if request_type == "POST" else client.get
        try:
            resp = await request_method(BASE_URL + router, **kwargs)
        except httpx.ConnectError as e:
            logger.error(f"无法连接到 meme-generator-rs API ({BASE_URL + router}): {e}")
            raise NetworkError(f"无法连接到 meme-generator-rs API ({BASE_URL}): {e}") from e
        except httpx.TimeoutException as e:
            logger.error(f"请求 meme-generator-rs API 超时 ({BASE_URL + router}): {e}")
            raise NetworkError(f"请求超时 ({BASE_URL + router}): {e}") from e
        status_code = resp.status_code
        if status_code == 200:
            if response_type == "JSON":
                return resp.json()
            elif response_type == "BYTES":
                return resp.content
            else:
                return resp.text
        elif status_code == 500:
            result = resp.json()
            code: int = result["code"]
            message: str = result["message"]
            data: dict = result["data"]
            if code == 410:
                exc = RequestError(message)
                exc.error = data.get("error", "")
                exc.status = status_code
                exc.url = router
                raise exc
            elif code == 420:
                exc = IOError_(message)
                exc.error = data.get("error", "")
                raise exc
            elif code == 510:
                exc = ImageDecodeError(message)
                exc.error = data.get("error", "")
                raise exc
            elif code == 520:
                exc = ImageEncodeError(message)
                exc.error = data.get("error", "")
                raise exc
            elif code == 530:
                exc = ImageAssetMissing(message)
                exc.path = data.get("path", "")
                raise exc
            elif code == 540:
                exc = DeserializeError(message)
                exc.error = data.get("error", "")
                raise exc
            elif code == 550:
                exc = ImageNumberMismatch(message)
                exc.min = data.get("min", 0)
                exc.max = data.get("max", 0)
                exc.actual = data.get("actual", 0)
                raise exc
            elif code == 551:
                exc = TextNumberMismatch(message)
                exc.min = data.get("min", 0)
                exc.max = data.get("max", 0)
                exc.actual = data.get("actual", 0)
                raise exc
            elif code == 560:
                exc = TextOverLength(message)
                exc.text = data.get("text", "")
                raise exc
            elif code == 570:
                exc = MemeFeedback(message)
                exc.feedback = data.get("feedback", "")
                raise exc
            else:
                logger.warning(f"meme-generator-rs API 返回未知错误码 {code}: {message}")
                raise MemeGeneratorException(message)
        else:
            logger.error(f"meme-generator-rs API 返回未处理的状态码 {status_code}: {router}")
            resp.raise_for_status()


class ImageResponse(BaseModel):
    image_id: str


class ImagesResponse(BaseModel):
    image_ids: list[str]


async def upload_image(image: bytes) -> str:
    payload = {"type": "data", "data": base64.b64encode(image).decode()}
    result = await send_request("/image/upload", "POST", "JSON", json=payload)
    return ImageResponse.model_validate(result).image_id


async def get_image(image_id: str) -> bytes:
    return await send_request(f"/image/{image_id}", "GET", "BYTES")


async def get_version() -> str:
    return cast(str, await send_request("/meme/version", "GET", "TEXT"))


async def get_meme_keys() -> list[str]:
    return cast(list[str], await send_request("/meme/keys", "GET", "JSON"))


async def search_memes(query: str, include_tags: bool = False) -> list[str]:
    return cast(
        list[str],
        await send_request(
            "/meme/search",
            "GET",
            "JSON",
            params={"query": query, "include_tags": include_tags},
        ),
    )


class ParserFlags(BaseModel):
    short: bool = False
    long: bool = True
    short_aliases: list[str] = []
    long_aliases: list[str] = []


class MemeBoolean(BaseModel):
    name: str
    type: Literal["boolean", "string", "integer", "float"]
    description: Optional[str] = None
    parser_flags: ParserFlags = ParserFlags()


class BooleanOption(MemeBoolean):
    type: Literal["boolean"] = "boolean"
    default: Optional[bool] = None


class StringOption(MemeBoolean):
    type: Literal["string"] = "string"
    default: Optional[str] = None
    choices: Optional[list[str]] = None


class IntegerOption(MemeBoolean):
    type: Literal["integer"] = "integer"
    default: Optional[int] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None


class FloatOption(MemeBoolean):
    type: Literal["float"] = "float"
    default: Optional[float] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None


class MemeParams(BaseModel):
    min_images: int = 0
    max_images: int = 0
    min_texts: int = 0
    max_texts: int = 0
    default_texts: list[str] = []
    options: list[Union[BooleanOption, StringOption, IntegerOption, FloatOption]] = []


class MemeShortcut(BaseModel):
    pattern: str
    humanized: Optional[str] = None
    names: list[str] = []
    texts: list[str] = []
    options: dict[str, Union[bool, str, int, float]] = {}


class MemeInfo(BaseModel):
    key: str
    params: MemeParams = MemeParams()
    keywords: list[str] = []
    shortcuts: list[MemeShortcut] = []
    tags: set[str] = set()
    date_created: Optional[datetime] = None
    date_modified: Optional[datetime] = None


async def get_meme_info(meme_key: str) -> MemeInfo:
    result = await send_request(f"/memes/{meme_key}/info", "GET", "JSON")
    return MemeInfo.model_validate(result)


async def get_meme_infos() -> list[MemeInfo]:
    resp = cast(
        list[dict[str, Any]],
        await send_request("/meme/infos", "GET", "JSON"),
    )
    return [MemeInfo.model_validate(meme_info) for meme_info in resp]


async def generate_meme_preview(meme_key: str) -> bytes:
    result = await send_request(f"/memes/{meme_key}/preview", "GET", "JSON")
    image_id = ImageResponse.model_validate(result).image_id
    return await get_image(image_id)


@dataclass
class Image:
    name: str
    data: bytes


async def generate_meme(
    meme_key: str,
    images: list[Image],
    texts: list[str],
    options: dict[str, Union[bool, str, int, float]],
) -> bytes:
    image_dicts: list[dict[str, str]] = []
    for image in images:
        image_id = await upload_image(image.data)
        image_dicts.append({"name": image.name, "id": image_id})
    payload = {"images": image_dicts, "texts": texts, "options": options}
    result = await send_request(f"/memes/{meme_key}", "POST", "JSON", json=payload)
    image_id = ImageResponse.model_validate(result).image_id
    return await get_image(image_id)


@dataclass
class Meme:
    key: str
    _info: MemeInfo

    @property
    def info(self) -> MemeInfo:
        return deepcopy(self._info)

    async def generate(
        self,
        images: list[Image],
        texts: list[str],
        options: dict[str, Union[bool, str, int, float]],
    ) -> bytes:
        return await generate_meme(self.key, images, texts, options)

    async def generate_preview(self) -> bytes:
        return await generate_meme_preview(self.key)


async def get_memes() -> list[Meme]:
    meme_infos = await get_meme_infos()
    return [Meme(info.key, info) for info in meme_infos]


class MemeProperties(BaseModel):
    disabled: bool = False
    hot: bool = False
    new: bool = False


class RenderMemeListParams(BaseModel):
    meme_properties: dict[str, MemeProperties] = {}
    exclude_memes: list[str] = []
    sort_by: Literal["key", "keywords", "keywords_pinyin", "date_created", "date_modified"] = "keywords_pinyin"
    sort_reverse: bool = False
    text_template: str = "{index}. {keywords}"
    add_category_icon: bool = True


async def render_meme_list(
    meme_properties: dict[str, MemeProperties] = {},
    exclude_memes: list[str] = [],
    sort_by: Literal["key", "keywords", "keywords_pinyin", "date_created", "date_modified"] = "keywords_pinyin",
    sort_reverse: bool = False,
    text_template: str = "{index}. {keywords}",
    add_category_icon: bool = True,
) -> bytes:
    result = await send_request(
        "/tools/render_list",
        "POST",
        "JSON",
        json=RenderMemeListParams(
            meme_properties=meme_properties,
            exclude_memes=exclude_memes,
            sort_by=sort_by,
            sort_reverse=sort_reverse,
            text_template=text_template,
            add_category_icon=add_category_icon,
        ).model_dump(),
    )
    image_id = ImageResponse.model_validate(result).image_id
    return await get_image(image_id)


class RenderMemeStatisticsParams(BaseModel):
    title: str
    statistics_type: Literal["meme_count", "time_count"]
    data: list[tuple[str, int]]


async def render_meme_statistics(
    title: str,
    statistics_type: Literal["meme_count", "time_count"],
    data: list[tuple[str, int]],
) -> bytes:
    result = await send_request(
        "/tools/render_statistics",
        "POST",
        "JSON",
        json=RenderMemeStatisticsParams(
            title=title,
            statistics_type=statistics_type,
            data=data,
        ).model_dump(),
    )
    image_id = ImageResponse.model_validate(result).image_id
    return await get_image(image_id)


class ImageInfo(BaseModel):
    width: int
    height: int
    frame_count: int = 1
    average_duration: Optional[float] = None


async def image_inspect(image: bytes) -> ImageInfo:
    image_id = await upload_image(image)
    result = await send_request(f"/image/{image_id}/inspect", "GET", "JSON")
    return ImageInfo.model_validate(result)


async def image_flip_horizontal(image: bytes) -> bytes:
    image_id = await upload_image(image)
    result = await send_request(
        "/image/flip_horizontal", "POST", "JSON", json={"image_id": image_id}
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_flip_vertical(image: bytes) -> bytes:
    image_id = await upload_image(image)
    result = await send_request(
        "/image/flip_vertical", "POST", "JSON", json={"image_id": image_id}
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_rotate(image: bytes, angle: Optional[float] = None) -> bytes:
    image_id = await upload_image(image)
    payload: dict[str, Any] = {"image_id": image_id}
    if angle is not None:
        payload["angle"] = angle
    result = await send_request(
        "/image/rotate", "POST", "JSON", json=payload
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_resize(image: bytes, width: Optional[int] = None, height: Optional[int] = None) -> bytes:
    image_id = await upload_image(image)
    payload: dict[str, Any] = {"image_id": image_id}
    if width is not None:
        payload["width"] = width
    if height is not None:
        payload["height"] = height
    result = await send_request(
        "/image/resize", "POST", "JSON", json=payload
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_crop(image: bytes, left: int, top: int, right: int, bottom: int) -> bytes:
    image_id = await upload_image(image)
    result = await send_request(
        "/image/crop",
        "POST",
        "JSON",
        json={"image_id": image_id, "left": left, "top": top, "right": right, "bottom": bottom},
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_grayscale(image: bytes) -> bytes:
    image_id = await upload_image(image)
    result = await send_request(
        "/image/grayscale", "POST", "JSON", json={"image_id": image_id}
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_invert(image: bytes) -> bytes:
    image_id = await upload_image(image)
    result = await send_request(
        "/image/invert", "POST", "JSON", json={"image_id": image_id}
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_merge_horizontal(images: list[bytes]) -> bytes:
    image_ids = []
    for img in images:
        image_id = await upload_image(img)
        image_ids.append(image_id)
    result = await send_request(
        "/image/merge_horizontal", "POST", "JSON", json={"image_ids": image_ids}
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_merge_vertical(images: list[bytes]) -> bytes:
    image_ids = []
    for img in images:
        image_id = await upload_image(img)
        image_ids.append(image_id)
    result = await send_request(
        "/image/merge_vertical", "POST", "JSON", json={"image_ids": image_ids}
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_gif_split(image: bytes) -> list[bytes]:
    image_id = await upload_image(image)
    result = await send_request(
        "/image/gif_split", "POST", "JSON", json={"image_id": image_id}
    )
    resp = ImagesResponse.model_validate(result)
    return [await get_image(img_id) for img_id in resp.image_ids]


async def image_gif_merge(images: list[bytes], duration: Optional[float] = None) -> bytes:
    image_ids = []
    for img in images:
        image_id = await upload_image(img)
        image_ids.append(image_id)
    payload: dict[str, Any] = {"image_ids": image_ids}
    if duration is not None:
        payload["duration"] = duration
    result = await send_request(
        "/image/gif_merge", "POST", "JSON", json=payload
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_gif_reverse(image: bytes) -> bytes:
    image_id = await upload_image(image)
    result = await send_request(
        "/image/gif_reverse", "POST", "JSON", json={"image_id": image_id}
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)


async def image_gif_change_duration(image: bytes, duration: float) -> bytes:
    image_id = await upload_image(image)
    result = await send_request(
        "/image/gif_change_duration",
        "POST",
        "JSON",
        json={"image_id": image_id, "duration": duration},
    )
    resp = ImageResponse.model_validate(result)
    return await get_image(resp.image_id)
