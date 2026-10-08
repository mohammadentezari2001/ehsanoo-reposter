"""Telegram API gateway: copying posts and classifying API failures."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import Chat, Message, MessageId

from .database import Post

log = logging.getLogger(__name__)


class RepostError(Exception):
    """Base class for repost failures."""


class PermanentRepostError(RepostError):
    """The source message can never be copied (deleted, invalid, inaccessible)."""


class ChannelError(RepostError):
    """The destination channel refuses writes (permissions, wrong chat)."""


class TransientRepostError(RepostError):
    """Temporary failure (network, flood wait).  Worth retrying later."""


#: Fragments that mean "this source message is gone for good".
_MISSING_FRAGMENTS = (
    "message to copy not found",
    "message to forward not found",
    "message to reply not found",
    "message_id_invalid",
    "message identifier is not specified",
    "message can't be copied",
    "message cannot be copied",
    "message to edit not found",
)

#: Fragments that mean "we cannot write to the destination right now".
_CHANNEL_FRAGMENTS = (
    "chat not found",
    "chat_write_forbidden",
    "not enough rights",
    "have no rights",
    "bot was kicked",
    "bot is not a member",
    "peer_id_invalid",
    "channel_invalid",
    "not a member of the chat",
    "user is not a member",
)


def describe(exc: Exception) -> str:
    """Short single-line description of a Telegram exception."""
    text = getattr(exc, "message", None) or str(exc)
    return " ".join(str(text).split())


def classify(exc: Exception) -> RepostError:
    """Translate an aiogram exception into a bot-level error."""
    if isinstance(exc, TelegramRetryAfter):
        return TransientRepostError(f"flood wait {exc.retry_after}s")
    if isinstance(exc, (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError)):
        return TransientRepostError(f"network/server problem: {describe(exc)}")
    if isinstance(exc, TelegramForbiddenError):
        return ChannelError(f"forbidden: {describe(exc)}")
    if isinstance(exc, TelegramEntityTooLarge):
        return PermanentRepostError("file too large to copy")
    if isinstance(exc, TelegramBadRequest):
        message = describe(exc).lower()
        if any(fragment in message for fragment in _MISSING_FRAGMENTS):
            return PermanentRepostError(describe(exc))
        if any(fragment in message for fragment in _CHANNEL_FRAGMENTS):
            return ChannelError(describe(exc))
        return PermanentRepostError(describe(exc))
    return TransientRepostError(describe(exc))


class TelegramGateway:
    """Everything that talks to Telegram."""

    #: How long channel updates are suppressed around a publish.  A copy creates
    #: a brand new message in the channel, which comes back to us as a
    #: ``channel_post`` update -- and the long-poll can deliver it *before* the
    #: copy call itself returns.  Without this window the bot would archive its
    #: own reposts and then repost those, forever.
    SEND_WINDOW_SECONDS = 120.0
    #: Grace period after a publish finishes.
    SEND_GRACE_SECONDS = 20.0

    def __init__(self, bot: Bot, channel_id: str | int, admin_chat_id: int) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self.admin_chat_id = admin_chat_id
        self.channel_numeric_id: int | None = None
        #: New message ids this process just created.
        self._just_created: set[int] = set()
        self._just_created_order: list[int] = []
        self._send_window_until: float = 0.0

    # ------------------------------------------------------------ send window

    def in_send_window(self) -> bool:
        return time.monotonic() < self._send_window_until

    def _open_send_window(self) -> None:
        self._send_window_until = time.monotonic() + self.SEND_WINDOW_SECONDS

    def _close_send_window(self) -> None:
        self._send_window_until = time.monotonic() + self.SEND_GRACE_SECONDS

    # ----------------------------------------------------------- channel info

    async def resolve_channel(self) -> Chat:
        chat = await self.bot.get_chat(self.channel_id)
        self.channel_numeric_id = chat.id
        return chat

    async def channel_title(self) -> str:
        try:
            chat = await self.resolve_channel()
            return chat.title or str(chat.id)
        except Exception as exc:
            return f"<unresolved: {describe(exc)}>"

    # ---------------------------------------------------------------- copying

    async def copy_post(self, post: Post) -> list[int]:
        """Copy an archived post into the channel.

        Single messages use ``copyMessage``; albums use ``copyMessages`` so the
        whole media group lands as one album.  Telegram's copy endpoints strip
        the "forwarded from" header, so the result looks like a normal post.
        """
        if not post.message_ids:
            raise PermanentRepostError("archive entry has no message ids")

        target = self.channel_numeric_id or self.channel_id
        self._open_send_window()
        try:
            if len(post.message_ids) == 1:
                message = await self.bot.copy_message(
                    chat_id=target,
                    from_chat_id=post.chat_id,
                    message_id=post.message_ids[0],
                )
                created = [message.message_id]
            else:
                results: list[MessageId] = await self.bot.copy_messages(
                    chat_id=target,
                    from_chat_id=post.chat_id,
                    message_ids=sorted(post.message_ids),
                )
                created = [item.message_id for item in results]
        finally:
            self._close_send_window()

        self._remember_created(created)
        return created

    def _remember_created(self, message_ids: list[int]) -> None:
        """Remember ids we created, for updates that arrive after the window."""
        for message_id in message_ids:
            if message_id not in self._just_created:
                self._just_created.add(message_id)
                self._just_created_order.append(message_id)
        while len(self._just_created_order) > 2000:
            self._just_created.discard(self._just_created_order.pop(0))

    def is_own_repost(self, message_id: int) -> bool:
        return message_id in self._just_created

    # ----------------------------------------------------------------- probing

    async def probe_message(
        self, source_chat_id: int | str, message_id: int, probe_chat_id: int
    ) -> Message:
        """Copy a message to ``probe_chat_id`` and immediately delete it.

        This is the only way the Bot API lets us ask "does message N still
        exist?" -- there is no getMessage endpoint.
        """
        sent = await self.bot.copy_message(
            chat_id=probe_chat_id,
            from_chat_id=source_chat_id,
            message_id=message_id,
        )
        try:
            await self.bot.delete_message(chat_id=probe_chat_id, message_id=sent.message_id)
        except Exception as exc:  # the copy is kept either way
            log.debug("Could not delete probe copy %s: %s", sent.message_id, exc)
        return sent

    # -------------------------------------------------------------- messaging

    async def send_admin(self, text: str, **kwargs: Any) -> Message | None:
        if not self.admin_chat_id:
            log.warning("No admin chat id configured; dropping message: %s", text[:80])
            return None
        try:
            return await self.bot.send_message(self.admin_chat_id, text, **kwargs)
        except Exception as exc:
            log.error("Could not message the admin: %s", describe(exc))
            return None
