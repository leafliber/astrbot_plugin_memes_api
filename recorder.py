import json
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from astrbot.api import logger


class SessionIdType(Enum):
    GLOBAL = 0
    USER = 1
    GROUP = 2
    GROUP_USER = 3


@dataclass
class MemeRecord:
    time: str
    meme_key: str
    user_id: str
    group_id: str


class MemeRecorder:
    def __init__(self, path: Path):
        self.__path = path
        self.__records: list[MemeRecord] = []
        self.__lock = threading.Lock()
        self.__load()

    def __load(self):
        if self.__path.exists():
            try:
                with open(self.__path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.__records = [MemeRecord(**r) for r in data]
            except Exception:
                logger.warning("表情调用记录加载失败，将使用空记录")
                self.__records = []
        else:
            self.__records = []

    def __save(self):
        self.__path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.__path, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in self.__records], f, ensure_ascii=False)

    def record(self, meme_key: str, user_id: str, group_id: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        record = MemeRecord(
            time=now,
            meme_key=meme_key,
            user_id=user_id,
            group_id=group_id,
        )
        with self.__lock:
            self.__records.append(record)
            self.__save()

    def get_records(
        self,
        id_type: SessionIdType = SessionIdType.GLOBAL,
        meme_key: Optional[str] = None,
        time_start: Optional[datetime] = None,
        time_stop: Optional[datetime] = None,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> list[MemeRecord]:
        with self.__lock:
            records = self.__records[:]

        filtered = []
        for r in records:
            try:
                r_time = datetime.fromisoformat(r.time)
            except Exception:
                continue

            if id_type == SessionIdType.USER:
                if r.user_id != user_id:
                    continue
            elif id_type == SessionIdType.GROUP:
                if r.group_id != group_id:
                    continue
            elif id_type == SessionIdType.GROUP_USER:
                if r.user_id != user_id or r.group_id != group_id:
                    continue

            if meme_key and r.meme_key != meme_key:
                continue

            if time_start and r_time < time_start:
                continue

            if time_stop and r_time > time_stop:
                continue

            filtered.append(r)

        return filtered

    def get_meme_generation_times(
        self,
        id_type: SessionIdType = SessionIdType.GLOBAL,
        meme_key: Optional[str] = None,
        time_start: Optional[datetime] = None,
        time_stop: Optional[datetime] = None,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> list[datetime]:
        records = self.get_records(
            id_type=id_type,
            meme_key=meme_key,
            time_start=time_start,
            time_stop=time_stop,
            user_id=user_id,
            group_id=group_id,
        )
        times = []
        for r in records:
            try:
                times.append(datetime.fromisoformat(r.time))
            except Exception:
                continue
        return times

    def get_meme_generation_keys(
        self,
        id_type: SessionIdType = SessionIdType.GLOBAL,
        time_start: Optional[datetime] = None,
        time_stop: Optional[datetime] = None,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> list[str]:
        records = self.get_records(
            id_type=id_type,
            time_start=time_start,
            time_stop=time_stop,
            user_id=user_id,
            group_id=group_id,
        )
        return [r.meme_key for r in records]
