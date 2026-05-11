import asyncio
import io
import random
import re
import tempfile
import traceback
import zipfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

import filetype
from dateutil.relativedelta import relativedelta

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image as CompImage, Plain, At as CompAt, Node, Reply
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig

from .api import (
    Meme,
    MemeInfo,
    MemeShortcut,
    BooleanOption,
    StringOption,
    IntegerOption,
    FloatOption,
    Image as MemeImage,
    MemeProperties,
    NetworkError,
    MemeGeneratorException,
    RequestError,
    IOError_ as MemeIOError,
    ImageDecodeError,
    ImageEncodeError,
    ImageAssetMissing,
    DeserializeError,
    ImageNumberMismatch,
    TextNumberMismatch,
    TextOverLength,
    MemeFeedback,
    set_base_url,
    download_url,
    render_meme_list,
    render_meme_statistics,
    generate_meme,
    generate_meme_preview,
    search_memes as api_search_memes,
    image_inspect,
    image_flip_horizontal,
    image_flip_vertical,
    image_rotate,
    image_resize,
    image_crop,
    image_grayscale,
    image_invert,
    image_merge_horizontal,
    image_merge_vertical,
    image_gif_split,
    image_gif_merge,
    image_gif_reverse,
    image_gif_change_duration,
)
from .manager import MemeManager, MemeMode
from .recorder import MemeRecorder, SessionIdType


DATA_DIR = Path("data/astrbot_plugin_memes_api")


class MemesApiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._init_config(config)
        set_base_url(self.base_url)

        self.meme_manager = MemeManager(DATA_DIR / "meme_manager.yml")
        self.recorder = MemeRecorder(DATA_DIR / "meme_records.json")
        self._memes_loaded = False
        self._meme_dict: dict[str, Meme] = {}
        self._keyword_map: dict[str, Meme] = {}
        self._shortcut_patterns: list[tuple[re.Pattern, Meme, MemeShortcut]] = []

    def _init_config(self, config: AstrBotConfig):
        self.base_url = config.get("meme_generator_base_url", "http://127.0.0.1:2233")
        self.command_prefixes = config.get("memes_command_prefixes", [])
        self.disabled_list = config.get("memes_disabled_list", [])
        policy = config.get("memes_params_mismatch_policy", {})
        self.policy_too_much_text = policy.get("too_much_text", "ignore")
        self.policy_too_few_text = policy.get("too_few_text", "ignore")
        self.policy_too_much_image = policy.get("too_much_image", "ignore")
        self.policy_too_few_image = policy.get("too_few_image", "ignore")
        self.use_sender_when_no_image = config.get("memes_use_sender_when_no_image", False)
        self.use_default_when_no_text = config.get("memes_use_default_when_no_text", False)
        self.random_meme_show_info = config.get("memes_random_meme_show_info", True)
        list_config = config.get("memes_list_image_config", {})
        self.list_sort_by = list_config.get("sort_by", "keywords")
        self.list_sort_reverse = list_config.get("sort_reverse", False)
        self.list_text_template = list_config.get("text_template", "{keywords}")
        self.list_add_category_icon = list_config.get("add_category_icon", True)
        self.list_label_new_timedelta_days = list_config.get("label_new_timedelta_days", 30)
        self.list_label_hot_threshold = list_config.get("label_hot_threshold", 21)
        self.list_label_hot_days = list_config.get("label_hot_days", 7)
        multi_config = config.get("memes_multiple_image_config", {})
        self.direct_send_threshold = multi_config.get("direct_send_threshold", 10)
        self.send_zip_file = multi_config.get("send_zip_file", True)
        self.send_forward_msg = multi_config.get("send_forward_msg", False)

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        logger.info(f"表情包插件开始加载，API 地址: {self.base_url}")
        try:
            from .api import get_version
            version = await get_version()
            logger.info(f"meme-generator-rs 版本: {version}")
        except Exception as e:
            logger.warning(f"无法连接到 meme-generator-rs API ({self.base_url}): {e}")
            logger.warning("请确认 meme-generator-rs 服务已启动，否则所有表情功能将不可用")

        await self.meme_manager.init(disabled_list=self.disabled_list)
        self._build_maps()
        self._memes_loaded = True

        meme_count = len(self.meme_manager.get_memes())
        keyword_count = len(self._keyword_map)
        shortcut_count = len(self._shortcut_patterns)
        logger.info(f"表情包插件加载完成，共 {meme_count} 个表情，{keyword_count} 个关键词，{shortcut_count} 个快捷指令")

        if meme_count == 0:
            logger.warning("未加载到任何表情，请检查 meme-generator-rs API 是否正常运行")

    def _build_maps(self):
        self._meme_dict = {}
        self._keyword_map = {}
        self._shortcut_patterns = []

        for meme in self.meme_manager.get_memes():
            self._meme_dict[meme.key] = meme
            for keyword in meme.info.keywords:
                key = keyword.lower()
                if key not in self._keyword_map:
                    self._keyword_map[key] = meme
            for shortcut in meme.info.shortcuts:
                try:
                    pattern = re.compile(shortcut.pattern, re.IGNORECASE)
                    self._shortcut_patterns.append((pattern, meme, shortcut))
                except re.error:
                    logger.warning(f"表情 {meme.key} 的快捷指令正则无效: {shortcut.pattern}")

    async def terminate(self):
        pass

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        sender_id = event.get_sender_id()
        group_id = event.message_obj.group_id or ""
        return f"{sender_id}_{group_id}"

    def _match_prefix(self, text: str) -> Optional[str]:
        if not self.command_prefixes:
            return text
        for prefix in self.command_prefixes:
            if text.startswith(prefix):
                return text[len(prefix):].lstrip()
        return None

    async def _parse_message_params(self, event: AstrMessageEvent) -> tuple[list[str], list[MemeImage], list[str]]:
        texts: list[str] = []
        images: list[MemeImage] = []
        names: list[str] = []

        message_chain = event.message_obj.message

        reply_texts: list[str] = []
        reply_images: list[MemeImage] = []

        for seg in message_chain:
            if isinstance(seg, Reply):
                reply_chain = getattr(seg, 'chain', None) or []
                for reply_seg in reply_chain:
                    if isinstance(reply_seg, CompImage):
                        image_data = await self._extract_image_data_async(reply_seg)
                        if image_data:
                            reply_images.append(MemeImage(name="", data=image_data))
                    elif isinstance(reply_seg, Plain):
                        text = reply_seg.text.strip()
                        if text:
                            for word in text.split():
                                if word:
                                    reply_texts.append(word)
                if not reply_texts:
                    reply_msg_str = getattr(seg, 'message_str', None)
                    if reply_msg_str and reply_msg_str.strip():
                        for word in reply_msg_str.strip().split():
                            if word:
                                reply_texts.append(word)
                reply_sender_id = getattr(seg, 'sender_id', None)
                if reply_sender_id and reply_sender_id != 0:
                    reply_sender_id_str = str(reply_sender_id)
                    avatar_data = await self._get_user_avatar_async(reply_sender_id_str)
                    if avatar_data:
                        reply_sender_nick = getattr(seg, 'sender_nickname', None) or reply_sender_id_str
                        reply_images.insert(0, MemeImage(name=reply_sender_nick, data=avatar_data))
            elif isinstance(seg, CompAt):
                pass
            elif isinstance(seg, CompImage):
                image_data = await self._extract_image_data_async(seg)
                if image_data:
                    images.append(MemeImage(name="", data=image_data))
                else:
                    logger.debug(f"图片提取失败: file={seg.file}, url={seg.url}")
            elif isinstance(seg, Plain):
                words = seg.text.strip().split()
                for word in words:
                    if word == "自己":
                        pass
                    elif word.startswith("#"):
                        names.append(word[1:])
                    elif word.startswith("@") and len(word) > 1:
                        pass
                    else:
                        if word:
                            texts.append(word)

        plain_text = event.message_str.strip()
        for word in plain_text.split():
            if word == "自己":
                avatar_data = self._get_sender_avatar(event)
                if avatar_data:
                    sender_name = event.get_sender_name() or event.get_sender_id()
                    images.append(MemeImage(name=sender_name, data=avatar_data))
            elif word.startswith("@") and len(word) > 1:
                user_id = word[1:]
                avatar_data = self._get_user_avatar(user_id)
                if avatar_data:
                    images.append(MemeImage(name=user_id, data=avatar_data))

        for seg in message_chain:
            if isinstance(seg, CompAt):
                avatar_data = self._get_user_avatar(seg.qq)
                if avatar_data:
                    images.append(MemeImage(name=seg.qq, data=avatar_data))

        for i in range(len(names)):
            if i < len(images):
                images[i] = MemeImage(name=names[i], data=images[i].data)

        texts = reply_texts + texts
        images = reply_images + images

        return texts, images, names

    def _extract_image_data(self, img_seg: CompImage) -> Optional[bytes]:
        if img_seg.file and Path(img_seg.file).exists():
            return Path(img_seg.file).read_bytes()
        if img_seg.url:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    return None
                else:
                    return loop.run_until_complete(download_url(img_seg.url))
            except RuntimeError:
                return None
        return None

    async def _extract_image_data_async(self, img_seg: CompImage) -> Optional[bytes]:
        if img_seg.file and Path(img_seg.file).exists():
            return Path(img_seg.file).read_bytes()
        if img_seg.url:
            try:
                return await download_url(img_seg.url)
            except Exception:
                logger.warning(f"图片下载失败: {img_seg.url}")
        return None

    def _get_sender_avatar(self, event: AstrMessageEvent) -> Optional[bytes]:
        return None

    def _get_user_avatar(self, user_id: str) -> Optional[bytes]:
        return None

    async def _get_sender_avatar_async(self, event: AstrMessageEvent) -> Optional[bytes]:
        return None

    async def _get_user_avatar_async(self, user_id: str) -> Optional[bytes]:
        return None

    def _parse_options(self, text_parts: list[str]) -> tuple[dict[str, Any], list[str]]:
        options: dict[str, Any] = {}
        remaining: list[str] = []
        i = 0
        while i < len(text_parts):
            part = text_parts[i]
            if part.startswith("--"):
                key = part[2:]
                if "=" in key:
                    k, v = key.split("=", 1)
                    options[k] = v
                elif i + 1 < len(text_parts) and not text_parts[i + 1].startswith("-"):
                    options[key] = text_parts[i + 1]
                    i += 1
                else:
                    options[key] = True
            elif part.startswith("-") and len(part) > 1 and not part[1:].isdigit():
                key = part[1:]
                if i + 1 < len(text_parts) and not text_parts[i + 1].startswith("-"):
                    options[key] = text_parts[i + 1]
                    i += 1
                else:
                    options[key] = True
            else:
                remaining.append(part)
            i += 1
        return options, remaining

    async def _process_meme_generation(
        self,
        event: AstrMessageEvent,
        meme: Meme,
        images: list[MemeImage],
        texts: list[str],
        options: dict[str, Any] = {},
        show_info: bool = False,
    ):
        info = meme.info
        params = info.params

        logger.debug(f"开始生成表情 {meme.key}: 图片数={len(images)}, 文字数={len(texts)}, 需要: 图片={params.min_images}~{params.max_images}, 文字={params.min_texts}~{params.max_texts}")

        if params.min_images == 2 and len(images) == 1:
            avatar = await self._get_sender_avatar_async(event)
            if avatar:
                sender_name = event.get_sender_name() or event.get_sender_id()
                images.insert(0, MemeImage(name=sender_name, data=avatar))
                logger.debug(f"自动补充发送者头像作为第一张图片")

        if self.use_sender_when_no_image and params.min_images >= 1 and len(images) == 0:
            avatar = await self._get_sender_avatar_async(event)
            if avatar:
                sender_name = event.get_sender_name() or event.get_sender_id()
                images.append(MemeImage(name=sender_name, data=avatar))
                logger.debug(f"无图片时自动使用发送者头像")

        if self.use_default_when_no_text and params.min_texts > 0 and len(texts) == 0:
            texts = list(params.default_texts)
            logger.debug(f"无文字时自动使用默认文字: {texts}")

        text_range = (
            f"{params.min_texts} ~ {params.max_texts}"
            if params.min_texts != params.max_texts
            else str(params.min_texts)
        )
        image_range = (
            f"{params.min_images} ~ {params.max_images}"
            if params.min_images != params.max_images
            else str(params.min_images)
        )

        if len(texts) < params.min_texts:
            msg = f"文字数量不符，应为 {text_range}，实际传入 {len(texts)}"
            if self.policy_too_few_text == "ignore":
                logger.debug(f"表情 {meme.key}: {msg}，策略为 ignore，跳过")
                return
            if self.policy_too_few_text == "prompt":
                yield event.plain_result(msg)
                return
            elif self.policy_too_few_text == "get":
                pass

        if len(texts) > params.max_texts:
            msg = f"文字数量不符，应为 {text_range}，实际传入 {len(texts)}"
            if self.policy_too_much_text == "ignore":
                logger.debug(f"表情 {meme.key}: {msg}，策略为 ignore，跳过")
                return
            if self.policy_too_much_text == "prompt":
                yield event.plain_result(msg)
                return
            elif self.policy_too_much_text == "drop":
                texts = texts[: params.max_texts]

        if len(images) < params.min_images:
            msg = f"图片数量不符，应为 {image_range}，实际传入 {len(images)}"
            if self.policy_too_few_image == "ignore":
                logger.debug(f"表情 {meme.key}: {msg}，策略为 ignore，跳过")
                return
            if self.policy_too_few_image == "prompt":
                yield event.plain_result(msg)
                return
            elif self.policy_too_few_image == "get":
                pass

        if len(images) > params.max_images:
            msg = f"图片数量不符，应为 {image_range}，实际传入 {len(images)}"
            if self.policy_too_much_image == "ignore":
                logger.debug(f"表情 {meme.key}: {msg}，策略为 ignore，跳过")
                return
            if self.policy_too_much_image == "prompt":
                yield event.plain_result(msg)
                return
            elif self.policy_too_much_image == "drop":
                images = images[: params.max_images]

        try:
            result = await meme.generate(images, texts, options)
            logger.info(f"表情 {meme.key} 生成成功，结果大小: {len(result)} bytes")
        except ImageDecodeError as e:
            logger.warning(f"表情 {meme.key} 图片解码出错: {e.error}")
            yield event.plain_result(f"图片解码出错：{e.error}")
            return
        except ImageEncodeError as e:
            logger.warning(f"表情 {meme.key} 图片编码出错: {e.error}")
            yield event.plain_result(f"图片编码出错：{e.error}")
            return
        except ImageAssetMissing as e:
            logger.warning(f"表情 {meme.key} 缺少图片资源: {e.path}")
            yield event.plain_result(f"缺少图片资源：{e.path}")
            return
        except DeserializeError as e:
            logger.warning(f"表情 {meme.key} 选项解析出错: {e.error}")
            yield event.plain_result(f"表情选项解析出错：{e.error}")
            return
        except ImageNumberMismatch as e:
            num = f"{e.min} ~ {e.max}" if e.min != e.max else str(e.min)
            yield event.plain_result(f"图片数量不符，应为 {num}，实际传入 {e.actual}")
            return
        except TextNumberMismatch as e:
            num = f"{e.min} ~ {e.max}" if e.min != e.max else str(e.min)
            yield event.plain_result(f"文字数量不符，应为 {num}，实际传入 {e.actual}")
            return
        except TextOverLength as e:
            text = e.text
            repr_text = text if len(text) <= 10 else (text[:10] + "...")
            yield event.plain_result(f"文字过长：{repr_text}")
            return
        except MemeFeedback as e:
            yield event.plain_result(e.feedback)
            return
        except (RequestError, MemeIOError):
            logger.warning(traceback.format_exc())
            yield event.plain_result("请求出错")
            return
        except MemeGeneratorException as e:
            logger.warning(traceback.format_exc())
            yield event.plain_result(e.message)
            return
        except Exception as e:
            logger.warning(traceback.format_exc())
            yield event.plain_result(f"表情生成失败：{e}")
            return

        user_id = self._get_user_id(event)
        group_id = event.message_obj.group_id or ""
        self.recorder.record(meme.key, user_id, group_id)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(result)
            img_path = f.name

        if show_info:
            keywords = "、".join([f'"{keyword}"' for keyword in meme.info.keywords])
            chain = [
                Plain(f"关键词：{keywords}"),
                CompImage.fromFileSystem(img_path),
            ]
            yield event.chain_result(chain)
        else:
            yield event.image_result(img_path)

    async def _find_meme(self, meme_name: str) -> Optional[Meme]:
        found_memes = self.meme_manager.find(meme_name)
        if len(found_memes) == 1:
            return found_memes[0]
        if len(found_memes) == 0:
            try:
                searched = await self.meme_manager.search(meme_name)
                if len(searched) == 1:
                    return searched[0]
            except Exception:
                pass
        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        if not self._memes_loaded:
            logger.debug("表情包插件尚未加载完成，跳过消息处理")
            return

        message_str = event.message_str.strip()
        if not message_str:
            return

        matched_text = self._match_prefix(message_str)
        if matched_text is None and self.command_prefixes:
            logger.debug(f"消息 '{message_str}' 不匹配任何指令前缀 {self.command_prefixes}，跳过")
            return

        search_text = matched_text if matched_text is not None else message_str

        for cmd_prefix in ["表情包制作", "表情列表", "表情包列表", "头像表情包", "文字表情包",
                           "表情详情", "表情帮助", "表情示例",
                           "表情搜索",
                           "随机表情",
                           "禁用表情", "启用表情", "全局禁用表情", "全局启用表情",
                           "表情调用统计", "表情使用统计",
                           "图片操作", "图片工具",
                           "水平翻转", "左翻", "右翻",
                           "竖直翻转", "上翻", "下翻",
                           "旋转", "缩放", "裁剪",
                           "灰度图", "黑白",
                           "反相", "反色",
                           "横向拼接", "纵向拼接",
                           "gif分解", "gif合成", "gif倒放", "倒放", "gif变速"]:
            if search_text == cmd_prefix or search_text.startswith(cmd_prefix + " ") or search_text.startswith(cmd_prefix + "\t"):
                return

        matched_meme = None
        matched_shortcut = None
        matched_keyword = None

        for keyword, meme in self._keyword_map.items():
            if search_text.lower().startswith(keyword.lower()):
                rest = search_text[len(keyword):].strip()
                if rest == "" or rest[0] in (" ", "\t", "@", "#", "-") or rest[0].isascii():
                    if matched_keyword is None or len(keyword) > len(matched_keyword):
                        matched_keyword = keyword
                        matched_meme = meme

        if matched_meme is None:
            for pattern, meme, shortcut in self._shortcut_patterns:
                m = pattern.match(search_text)
                if m:
                    matched_meme = meme
                    matched_shortcut = (shortcut, m)
                    break

        if matched_meme is None:
            logger.debug(f"消息 '{search_text}' 未匹配到任何表情关键词或快捷指令")
            return

        logger.info(f"匹配到表情: {matched_meme.key} (关键词: {matched_keyword}, 快捷指令: {matched_shortcut is not None})")

        user_id = self._get_user_id(event)
        if not self.meme_manager.check(user_id, matched_meme.key):
            logger.debug(f"用户 {user_id} 无权使用表情 {matched_meme.key}")
            return

        texts, images, names = await self._parse_message_params(event)

        options: dict[str, Any] = {}

        if matched_shortcut:
            shortcut, match_obj = matched_shortcut
            args = match_obj.groupdict()
            shortcut_names = [name.format(**args) for name in shortcut.names]
            shortcut_texts = [text.format(**args) for text in shortcut.texts]
            shortcut_options = {
                name: value.format(**args) if isinstance(value, str) else value
                for name, value in shortcut.options.items()
            }
            texts = shortcut_texts + texts
            options.update(shortcut_options)
            for i, name in enumerate(shortcut_names):
                if i < len(images):
                    images[i] = MemeImage(name=name, data=images[i].data)

        plain_parts = [p for p in event.message_str.strip().split()
                       if p not in ("自己",) and not p.startswith("#") and not p.startswith("@")]
        extra_opts, remaining = self._parse_options(plain_parts)
        options.update(extra_opts)

        async for result in self._process_meme_generation(
            event, matched_meme, images, texts, options
        ):
            yield result
            return

    @filter.command("表情包制作", alias={"表情列表", "表情包列表"})
    async def meme_list(self, event: AstrMessageEvent):
        '''查看表情列表'''
        memes = self.meme_manager.get_memes()
        list_config = self.config.get("memes_list_image_config", {})
        label_new_timedelta_days = list_config.get("label_new_timedelta_days", self.list_label_new_timedelta_days)
        label_hot_threshold = list_config.get("label_hot_threshold", self.list_label_hot_threshold)
        label_hot_days = list_config.get("label_hot_days", self.list_label_hot_days)

        meme_generation_keys = self.recorder.get_meme_generation_keys(
            SessionIdType.GLOBAL,
            time_start=datetime.now(timezone.utc) - timedelta(days=label_hot_days),
        )

        meme_properties: dict[str, MemeProperties] = {}
        for meme in memes:
            new = False
            if meme.info.date_created:
                new = datetime.now(timezone.utc) - meme.info.date_created < timedelta(days=label_new_timedelta_days)
            hot = meme_generation_keys.count(meme.key) >= label_hot_threshold
            user_id = self._get_user_id(event)
            disabled = not self.meme_manager.check(user_id, meme.key)
            properties = MemeProperties(disabled=disabled, hot=hot, new=new)
            meme_properties[meme.key] = properties

        try:
            output = await render_meme_list(
                meme_properties=meme_properties,
                exclude_memes=self.disabled_list,
                sort_by=self.list_sort_by,
                sort_reverse=self.list_sort_reverse,
                text_template=self.list_text_template,
                add_category_icon=self.list_add_category_icon,
            )
        except MemeGeneratorException as e:
            logger.warning(f"表情列表图生成失败：{e}")
            yield event.plain_result("表情列表图生成失败")
            return

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(output)
            img_path = f.name

        chain = [
            Plain('触发方式："关键词 + 图片/文字/@某人"\n发送 "表情详情 + 关键词" 查看表情参数和预览\n目前支持的表情列表：'),
            CompImage.fromFileSystem(img_path),
        ]
        yield event.chain_result(chain)

    @filter.command("表情详情", alias={"表情帮助", "表情示例"})
    async def meme_info(self, event: AstrMessageEvent, meme_name: str = ""):
        '''查看表情详情和预览'''
        if not meme_name:
            yield event.plain_result("请输入表情关键词，如：表情详情 摸")
            return

        meme = await self._find_meme(meme_name)
        if not meme:
            found = self.meme_manager.find(meme_name)
            if not found:
                try:
                    found = await self.meme_manager.search(meme_name)
                except Exception:
                    found = []
            if not found:
                yield event.plain_result(f"表情 {meme_name} 不存在")
                return
            if len(found) > 1:
                lines = [f"{i+1}. {m.key} ({'/'.join(m.info.keywords)})" for i, m in enumerate(found[:10])]
                yield event.plain_result(f"找到多个表情，请更精确地指定：\n" + "\n".join(lines))
                return
            meme = found[0]

        info = meme.info
        params = info.params
        keywords = "、".join([f'"{keyword}"' for keyword in info.keywords])
        shortcuts = "、".join(
            [f'"{shortcut.humanized or shortcut.pattern}"' for shortcut in info.shortcuts]
        )
        tags = "、".join([f'"{tag}"' for tag in info.tags])
        image_num = f"{params.min_images}"
        if params.max_images > params.min_images:
            image_num += f" ~ {params.max_images}"
        text_num = f"{params.min_texts}"
        if params.max_texts > params.min_texts:
            text_num += f" ~ {params.max_texts}"
        default_texts = ", ".join([f'"{text}"' for text in params.default_texts])

        def option_info(option):
            parser_flags = option.parser_flags
            short_aliases = list(parser_flags.short_aliases)
            if parser_flags.short:
                short_aliases.insert(0, option.name[0])
            long_aliases = list(parser_flags.long_aliases)
            if parser_flags.long:
                long_aliases.insert(0, option.name)
            text = f"{'/'.join([f'-{flag}' for flag in short_aliases])}"
            if text:
                text += "/"
            text += f"{'/'.join([f'--{flag}' for flag in long_aliases])}"
            if not isinstance(option, BooleanOption):
                text += f" <{option.name.upper()}>"
            text += f"  {option.description or ''}"
            addition_texts = []
            if isinstance(option, (IntegerOption, FloatOption)):
                if option.minimum is not None:
                    addition_texts.append(f"最小值：{option.minimum}")
                if option.maximum is not None:
                    addition_texts.append(f"最大值：{option.maximum}")
            if isinstance(option, StringOption):
                if option.choices:
                    choices = "、".join([f'"{c}"' for c in option.choices])
                    addition_texts.append(f"可选值：{choices}")
            if option.default is not None:
                addition_texts.append(f"默认值：{option.default}")
            addition_text = "，".join(addition_texts)
            if addition_text:
                text += f"（{addition_text}）"
            return text

        options_info = "\n".join(["  " + option_info(option) for option in params.options])

        info_text = (
            f"表情名：{meme.key}"
            + f"\n关键词：{keywords}"
            + (f"\n快捷指令：{shortcuts}" if shortcuts else "")
            + (f"\n标签：{tags}" if tags else "")
            + f"\n需要图片数目：{image_num}"
            + f"\n需要文字数目：{text_num}"
            + (f"\n默认文字：[{default_texts}]" if default_texts else "")
            + (f"\n其他选项：\n{options_info}" if options_info else "")
        )

        try:
            preview = await meme.generate_preview()
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(preview)
                img_path = f.name
            chain = [
                Plain(info_text),
                Plain("\n表情预览：\n"),
                CompImage.fromFileSystem(img_path),
            ]
            yield event.chain_result(chain)
        except Exception as e:
            logger.warning(f"表情预览生成失败: {e}")
            yield event.plain_result(info_text)

    @filter.command("表情搜索")
    async def meme_search(self, event: AstrMessageEvent, query: str = ""):
        '''搜索表情'''
        if not query:
            yield event.plain_result("请输入搜索关键词，如：表情搜索 摸")
            return

        try:
            meme_keys = await api_search_memes(query, include_tags=True)
        except Exception as e:
            yield event.plain_result(f"搜索失败：{e}")
            return

        found_memes = [self.meme_manager.get_meme(key) for key in meme_keys]
        found_memes = [m for m in found_memes if m is not None]

        if not found_memes:
            yield event.plain_result(f'未找到与 "{query}" 相关的表情')
            return

        lines = [f"* {m.key} ({'/'.join(m.info.keywords)})" for m in found_memes[:20]]
        yield event.plain_result(f"搜索结果：\n" + "\n".join(lines))

    @filter.command("随机表情")
    async def random_meme(self, event: AstrMessageEvent):
        '''随机制作一个表情'''
        if not self._memes_loaded:
            return

        texts, images, names = await self._parse_message_params(event)

        num_images = len(images)
        num_texts = len(texts)

        candidates = []
        for meme in self.meme_manager.get_memes():
            params = meme.info.params
            if params.min_images <= num_images <= params.max_images and params.min_texts <= num_texts <= params.max_texts:
                candidates.append(meme)

        if not candidates:
            yield event.plain_result("没有符合当前图片/文字数量的表情")
            return

        meme = random.choice(candidates)

        async for result in self._process_meme_generation(
            event, meme, images, texts, show_info=self.random_meme_show_info
        ):
            yield result

    @filter.command("禁用表情")
    async def block_meme(self, event: AstrMessageEvent, meme_name: str = ""):
        '''禁用表情（管理员）'''
        if not meme_name:
            yield event.plain_result("请输入表情关键词，如：禁用表情 摸")
            return

        meme = await self._find_meme(meme_name)
        if not meme:
            yield event.plain_result(f"表情 {meme_name} 不存在")
            return

        user_id = self._get_user_id(event)
        self.meme_manager.block(user_id, meme.key)
        yield event.plain_result(f"表情 {meme.key} 禁用成功")

    @filter.command("启用表情")
    async def unblock_meme(self, event: AstrMessageEvent, meme_name: str = ""):
        '''启用表情（管理员）'''
        if not meme_name:
            yield event.plain_result("请输入表情关键词，如：启用表情 摸")
            return

        meme = await self._find_meme(meme_name)
        if not meme:
            yield event.plain_result(f"表情 {meme_name} 不存在")
            return

        user_id = self._get_user_id(event)
        self.meme_manager.unblock(user_id, meme.key)
        yield event.plain_result(f"表情 {meme.key} 启用成功")

    @filter.command("全局禁用表情")
    async def global_block_meme(self, event: AstrMessageEvent, meme_name: str = ""):
        '''全局禁用表情（超级用户）'''
        if not meme_name:
            yield event.plain_result("请输入表情关键词")
            return

        meme = await self._find_meme(meme_name)
        if not meme:
            yield event.plain_result(f"表情 {meme_name} 不存在")
            return

        self.meme_manager.change_mode(MemeMode.WHITE, meme.key)
        yield event.plain_result(f"表情 {meme.key} 已设为白名单模式")

    @filter.command("全局启用表情")
    async def global_unblock_meme(self, event: AstrMessageEvent, meme_name: str = ""):
        '''全局启用表情（超级用户）'''
        if not meme_name:
            yield event.plain_result("请输入表情关键词")
            return

        meme = await self._find_meme(meme_name)
        if not meme:
            yield event.plain_result(f"表情 {meme_name} 不存在")
            return

        self.meme_manager.change_mode(MemeMode.BLACK, meme.key)
        yield event.plain_result(f"表情 {meme.key} 已设为黑名单模式")

    @filter.command("表情调用统计", alias={"表情使用统计"})
    async def meme_statistics(self, event: AstrMessageEvent):
        '''查看表情调用统计'''
        message_str = event.message_str.strip()

        is_my = "我的" in message_str
        is_global = "全局" in message_str

        if is_my and is_global:
            id_type = SessionIdType.USER
        elif is_my:
            id_type = SessionIdType.GROUP_USER
        elif is_global:
            id_type = SessionIdType.GLOBAL
        else:
            id_type = SessionIdType.GROUP

        stat_type = "24h"
        type_patterns = [
            (r"24小时|1天", "24h"),
            (r"本日|今日", "day"),
            (r"一周|7天", "7d"),
            (r"本周", "week"),
            (r"30天", "30d"),
            (r"本月|月度", "month"),
            (r"一年", "1y"),
            (r"本年|年度", "year"),
            (r"日", "24h"),
            (r"周", "7d"),
            (r"月", "30d"),
            (r"年", "1y"),
        ]
        for pattern, t in type_patterns:
            if re.search(pattern, message_str):
                stat_type = t
                break

        meme_name = None
        for part in message_str.split():
            if part not in ("表情调用统计", "表情使用统计", "我的", "全局",
                           "日", "24小时", "1天", "本日", "今日",
                           "周", "一周", "7天", "本周",
                           "月", "30天", "本月", "月度",
                           "年", "一年", "本年", "年度"):
                meme_name = part
                break

        meme = None
        if meme_name:
            meme = await self._find_meme(meme_name)

        now = datetime.now().astimezone()
        if stat_type == "24h":
            start = now - timedelta(days=1)
            td = timedelta(hours=1)
            fmt = "%H:%M"
            humanized = "24小时"
        elif stat_type == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            td = timedelta(hours=1)
            fmt = "%H:%M"
            humanized = "本日"
        elif stat_type == "7d":
            start = now - timedelta(days=7)
            td = timedelta(days=1)
            fmt = "%m/%d"
            humanized = "7天"
        elif stat_type == "week":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
                days=now.weekday()
            )
            td = timedelta(days=1)
            fmt = "%a"
            humanized = "本周"
        elif stat_type == "30d":
            start = now - timedelta(days=30)
            td = timedelta(days=1)
            fmt = "%m/%d"
            humanized = "30天"
        elif stat_type == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            td = timedelta(days=1)
            fmt = "%m/%d"
            humanized = "本月"
        elif stat_type == "1y":
            start = now - relativedelta(years=1)
            td = relativedelta(months=1)
            fmt = "%y/%m"
            humanized = "一年"
        else:
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            td = relativedelta(months=1)
            fmt = "%b"
            humanized = "本年"

        user_id = self._get_user_id(event)
        group_id = event.message_obj.group_id or ""

        if meme:
            meme_times = self.recorder.get_meme_generation_times(
                id_type, meme_key=meme.key, time_start=start,
                user_id=user_id, group_id=group_id,
            )
            meme_keys = [meme.key] * len(meme_times)
        else:
            records = self.recorder.get_records(
                id_type, time_start=start,
                user_id=user_id, group_id=group_id,
            )
            records = [r for r in records if self.meme_manager.get_meme(r.meme_key)]
            meme_times = []
            meme_keys = []
            for r in records:
                try:
                    meme_times.append(datetime.fromisoformat(r.time))
                except Exception:
                    continue
                meme_keys.append(r.meme_key)

        if not meme_times:
            yield event.plain_result("暂时没有表情调用记录")
            return

        meme_times.sort()

        def fmt_time(time: datetime) -> str:
            if stat_type in ["24h", "7d", "30d", "1y"]:
                return (time + td).strftime(fmt)
            return time.strftime(fmt)

        time_counts: list[tuple[str, int]] = []
        stop = start + td
        count = 0
        key = fmt_time(start)
        for time in meme_times:
            while time >= stop:
                time_counts.append((key, count))
                key = fmt_time(stop)
                stop += td
                count = 0
            count += 1
        time_counts.append((key, count))
        while stop <= now:
            key = fmt_time(stop)
            stop += td
            time_counts.append((key, 0))

        key_counts: dict[str, int] = {}
        for k in meme_keys:
            key_counts[k] = key_counts.get(k, 0) + 1
        key_counts = dict(sorted(key_counts.items(), key=lambda item: item[1]))

        if meme:
            meme_kw = "/".join(meme.info.keywords)
            title = (
                f'表情"{meme_kw}"{humanized}调用统计'
                f"（总调用次数为 {key_counts.get(meme.key, 0)}）"
            )
            try:
                output = await render_meme_statistics(title, "time_count", time_counts)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    f.write(output)
                    img_path = f.name
                yield event.image_result(img_path)
            except MemeGeneratorException as e:
                logger.warning(f"表情调用统计图生成失败：{e}")
        else:
            title = f"{humanized}表情调用统计（总调用次数为 {sum(key_counts.values())}）"
            meme_counts: list[tuple[str, int]] = []
            for k, count in key_counts.items():
                if m := self.meme_manager.get_meme(k):
                    meme_counts.append(("/".join(m.info.keywords), count))

            chain = []
            try:
                output = await render_meme_statistics(title, "meme_count", meme_counts)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    f.write(output)
                    img_path1 = f.name
                chain.append(CompImage.fromFileSystem(img_path1))
            except MemeGeneratorException as e:
                logger.warning(f"表情调用统计图生成失败：{e}")

            try:
                output = await render_meme_statistics(title, "time_count", time_counts)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    f.write(output)
                    img_path2 = f.name
                chain.append(CompImage.fromFileSystem(img_path2))
            except MemeGeneratorException as e:
                logger.warning(f"表情调用统计图生成失败：{e}")

            if chain:
                yield event.chain_result(chain)

    @filter.command("图片操作", alias={"图片工具"})
    async def image_operations_help(self, event: AstrMessageEvent):
        '''查看图片操作帮助'''
        yield event.plain_result(
            "简单图片操作，支持的操作：\n"
            "1、水平翻转/左翻/右翻\n"
            "2、竖直翻转/上翻/下翻\n"
            "3、旋转\n"
            "4、缩放\n"
            "5、裁剪\n"
            "6、灰度图/黑白\n"
            "7、反相/反色\n"
            "8、横向拼接\n"
            "9、纵向拼接\n"
            "10、gif分解\n"
            "11、gif合成\n"
            "12、gif倒放/倒放\n"
            "13、gif变速"
        )

    @filter.command("水平翻转", alias={"左翻", "右翻"})
    async def flip_horizontal(self, event: AstrMessageEvent):
        '''水平翻转图片'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张图片")
            return
        try:
            result = await image_flip_horizontal(img_data)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("竖直翻转", alias={"上翻", "下翻"})
    async def flip_vertical(self, event: AstrMessageEvent):
        '''竖直翻转图片'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张图片")
            return
        try:
            result = await image_flip_vertical(img_data)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("旋转")
    async def rotate(self, event: AstrMessageEvent, angle: float = None):
        '''旋转图片'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张图片")
            return
        try:
            result = await image_rotate(img_data, angle)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("缩放")
    async def resize(self, event: AstrMessageEvent, size: str = ""):
        '''缩放图片，如：缩放 100x100 或 缩放 50%'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张图片")
            return

        width = None
        height = None
        match1 = re.fullmatch(r"(\d{1,4})?[*xX, ](\d{1,4})?", size)
        match2 = re.fullmatch(r"(\d{1,3})%", size)
        if match1:
            w = match1.group(1)
            h = match1.group(2)
            if not w and h:
                height = int(h)
            elif w and not h:
                width = int(w)
            elif w and h:
                width = int(w)
                height = int(h)
        elif match2:
            try:
                info = await image_inspect(img_data)
                ratio = int(match2.group(1)) / 100
                width = int(info.width * ratio)
                height = int(info.height * ratio)
            except Exception:
                yield event.plain_result("获取图片信息失败")
                return
        else:
            yield event.plain_result("请使用正确的尺寸格式，如：100x100、100x、50%")
            return

        try:
            result = await image_resize(img_data, width, height)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("裁剪")
    async def crop(self, event: AstrMessageEvent, crop_spec: str = ""):
        '''裁剪图片，如：裁剪 0,0,100,100 或 裁剪 100x100 或 裁剪 2:1'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张图片")
            return

        try:
            info = await image_inspect(img_data)
        except Exception:
            yield event.plain_result("获取图片信息失败")
            return

        match1 = re.fullmatch(r"(\d{1,4})[, ](\d{1,4})[, ](\d{1,4})[, ](\d{1,4})", crop_spec)
        match2 = re.fullmatch(r"(\d{1,4})[*xX, ](\d{1,4})", crop_spec)
        match3 = re.fullmatch(r"(\d{1,2})[:：比](\d{1,2})", crop_spec)

        if match1:
            left = int(match1.group(1))
            top = int(match1.group(2))
            right = int(match1.group(3))
            bottom = int(match1.group(4))
        else:
            if match2:
                width = int(match2.group(1))
                height = int(match2.group(2))
            elif match3:
                wp = int(match3.group(1))
                hp = int(match3.group(2))
                size = min(info.width / wp, info.height / hp)
                width = int(wp * size)
                height = int(hp * size)
            else:
                yield event.plain_result("请使用正确的裁剪格式，如：0,0,100,100、100x100、2:1")
                return
            left = (info.width - width) // 2
            top = (info.height - height) // 2
            right = left + width
            bottom = top + height

        try:
            result = await image_crop(img_data, left, top, right, bottom)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("灰度图", alias={"黑白"})
    async def grayscale(self, event: AstrMessageEvent):
        '''将图片转为灰度图'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张图片")
            return
        try:
            result = await image_grayscale(img_data)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("反相", alias={"反色"})
    async def invert(self, event: AstrMessageEvent):
        '''将图片反相'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张图片")
            return
        try:
            result = await image_invert(img_data)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("横向拼接")
    async def merge_horizontal(self, event: AstrMessageEvent):
        '''横向拼接多张图片'''
        images = await self._get_all_images(event)
        if len(images) < 2:
            yield event.plain_result("请发送至少两张图片")
            return
        try:
            result = await image_merge_horizontal(images)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("纵向拼接")
    async def merge_vertical(self, event: AstrMessageEvent):
        '''纵向拼接多张图片'''
        images = await self._get_all_images(event)
        if len(images) < 2:
            yield event.plain_result("请发送至少两张图片")
            return
        try:
            result = await image_merge_vertical(images)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("gif分解")
    async def gif_split(self, event: AstrMessageEvent):
        '''分解GIF为多帧'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张GIF图片")
            return
        try:
            frames = await image_gif_split(img_data)
            await self._send_multiple_images(event, frames)
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("gif合成")
    async def gif_merge(self, event: AstrMessageEvent, duration: float = None):
        '''合成多张图片为GIF'''
        images = await self._get_all_images(event)
        if len(images) < 2:
            yield event.plain_result("请发送至少两张图片")
            return
        try:
            result = await image_gif_merge(images, duration)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result, suffix=".gif"))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("gif倒放", alias={"倒放"})
    async def gif_reverse(self, event: AstrMessageEvent):
        '''倒放GIF'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张GIF图片")
            return
        try:
            result = await image_gif_reverse(img_data)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result, suffix=".gif"))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    @filter.command("gif变速")
    async def gif_change_duration(self, event: AstrMessageEvent, speed: str = ""):
        '''调整GIF播放速度'''
        img_data = await self._get_first_image(event)
        if not img_data:
            yield event.plain_result("请发送一张GIF图片")
            return

        p_float = r"\d{0,3}\.?\d{1,3}"
        duration = None

        if match := re.fullmatch(rf"({p_float})fps", speed, re.I):
            duration = 1 / float(match.group(1))
        elif match := re.fullmatch(rf"({p_float})(m?)s", speed, re.I):
            duration = (
                float(match.group(1)) / 1000 if match.group(2) else float(match.group(1))
            )
        else:
            try:
                info = await image_inspect(img_data)
                avg_duration = info.average_duration or 0.1
                if match := re.fullmatch(rf"({p_float})(?:x|X|倍速?)", speed):
                    duration = avg_duration / float(match.group(1))
                elif match := re.fullmatch(rf"({p_float})%", speed):
                    duration = avg_duration / (float(match.group(1)) / 100)
                else:
                    yield event.plain_result("请使用正确的倍率格式，如：0.5x、50%、20FPS、0.05s")
                    return
            except Exception:
                yield event.plain_result("获取图片信息失败")
                return

        if duration is not None and duration < 0.02:
            yield event.plain_result(
                f"帧间隔必须大于 0.02 s（小于等于 50 FPS），\n"
                f"当前帧间隔为 {duration:.3f} s ({1 / duration:.1f} FPS)"
            )
            return

        try:
            result = await image_gif_change_duration(img_data, duration)
            yield event.chain_result([CompImage.fromFileSystem(self._save_temp_image(result, suffix=".gif"))])
        except MemeGeneratorException as e:
            yield event.plain_result(f"操作失败：{e}")

    async def _get_first_image(self, event: AstrMessageEvent) -> Optional[bytes]:
        for seg in event.message_obj.message:
            if isinstance(seg, CompImage):
                return await self._extract_image_data_async(seg)
            elif isinstance(seg, Reply):
                reply_chain = getattr(seg, 'chain', None) or []
                for reply_seg in reply_chain:
                    if isinstance(reply_seg, CompImage):
                        data = await self._extract_image_data_async(reply_seg)
                        if data:
                            return data
        return None

    async def _get_all_images(self, event: AstrMessageEvent) -> list[bytes]:
        images = []
        for seg in event.message_obj.message:
            if isinstance(seg, CompImage):
                data = await self._extract_image_data_async(seg)
                if data:
                    images.append(data)
            elif isinstance(seg, Reply):
                reply_chain = getattr(seg, 'chain', None) or []
                for reply_seg in reply_chain:
                    if isinstance(reply_seg, CompImage):
                        data = await self._extract_image_data_async(reply_seg)
                        if data:
                            images.append(data)
        return images

    def _save_temp_image(self, data: bytes, suffix: str = ".png") -> str:
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.write(data)
        f.close()
        return f.name

    async def _send_multiple_images(self, event: AstrMessageEvent, images: list[bytes]):
        if len(images) <= self.direct_send_threshold:
            chain = []
            for img in images:
                path = self._save_temp_image(img)
                chain.append(CompImage.fromFileSystem(path))
            yield event.chain_result(chain)
        else:
            if self.send_zip_file:
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, img in enumerate(images):
                        ext = filetype.guess_extension(img) or "png"
                        zf.writestr(f"{i}.{ext}", img)
                zip_buffer.seek(0)
                time_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                filename = f"memes_{time_str}.zip"
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
                    f.write(zip_buffer.getvalue())
                    zip_path = f.name
                from astrbot.api.message_components import File as CompFile
                yield event.chain_result([CompFile(file=zip_path, name=filename)])
            elif self.send_forward_msg:
                chain = []
                for img in images:
                    path = self._save_temp_image(img)
                    node = Node(
                        uin=int(event.get_sender_id() or "0"),
                        name=event.get_sender_name() or "memes",
                        content=[CompImage.fromFileSystem(path)],
                    )
                    chain.append(node)
                yield event.chain_result(chain)
            else:
                chain = []
                for img in images:
                    path = self._save_temp_image(img)
                    chain.append(CompImage.fromFileSystem(path))
                yield event.chain_result(chain)
