#!/usr/bin/env python3
"""Non-blocking Telegram notification delivery."""

import logging
import queue
import threading
from typing import Any, Dict, Optional

import requests


logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send Telegram messages on a background worker."""

    def __init__(self, config: Dict[str, Any]):
        telegram = config.get("telegram", {})
        self.enabled = bool(telegram.get("enabled", False))
        self.bot_token = str(telegram.get("bot_token", "")).strip()
        self.chat_id = str(telegram.get("chat_id", "")).strip()
        self.timeout = float(telegram.get("timeout", 10))
        self._messages: queue.Queue[Optional[str]] = queue.Queue(maxsize=200)
        self._worker: Optional[threading.Thread] = None

        if self.enabled and (not self.bot_token or not self.chat_id):
            logger.warning("Telegram通知已启用，但bot_token或chat_id未配置，通知将被禁用")
            self.enabled = False

        if self.enabled:
            self._worker = threading.Thread(
                target=self._run,
                daemon=True,
                name="telegram-notifier",
            )
            self._worker.start()

    def send(self, message: str) -> bool:
        if not self.enabled:
            return False
        try:
            self._messages.put_nowait(message)
            return True
        except queue.Full:
            logger.warning("Telegram通知队列已满，丢弃一条通知")
            return False

    def stop(self) -> None:
        if not self.enabled:
            return
        try:
            self._messages.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            message = self._messages.get()
            if message is None:
                return
            try:
                self._deliver(message)
            except Exception as exc:
                logger.error("发送Telegram通知失败：%s", exc)

    def test(self, message: str) -> None:
        """Send immediately so the dashboard can report Telegram API errors."""
        if not self.enabled:
            raise RuntimeError("Telegram通知未启用")
        self._deliver(message)

    def _deliver(self, message: str) -> None:
        endpoint = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        response = requests.post(
            endpoint,
            json={"chat_id": self.chat_id, "text": message},
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except Exception as exc:
            detail = ''
            try:
                detail = response.json().get('description', '')
            except Exception:
                pass
            raise RuntimeError(detail or str(exc)) from exc
