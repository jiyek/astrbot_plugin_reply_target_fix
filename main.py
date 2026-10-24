"""Correct the @ mention and quoted message used by AstrBot replies.

AstrBot's result decorator always attaches the current incoming message and
sender.  That is right for "B quotes A and asks the bot a question", but wrong
for "B is answering A and the bot replies to A".  The target therefore has to
be inferred from meaning, not from the presence of a quote alone.
"""

from __future__ import annotations

import asyncio
import json
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import At, Reply

_TARGET_RE = re.compile(r'"?target"?\s*:\s*"?(current|quoted)"?', re.IGNORECASE)
_QUESTION_WORDS = (
    "吗",
    "呢",
    "怎么",
    "为什么",
    "为啥",
    "谁",
    "哪里",
    "哪一",
    "哪个",
    "多少",
    "是否",
    "能不能",
    "可不可以",
    "是不是",
    "有没有",
    "如何",
    "什么",
)


class ReplyTargetFixPlugin(Star):
    """Rewrite the @/quote target based on what the reply is answering."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.enabled = self._as_bool(config.get("enabled", True))
        self.target_mode = str(config.get("target_mode", "semantic") or "").strip()
        if self.target_mode not in {"semantic", "quoted", "current"}:
            self.target_mode = "semantic"
        self.fix_private = self._as_bool(config.get("fix_private", False))
        self.skip_self_quote = self._as_bool(config.get("skip_self_quote", True))
        self.require_bot_addressed = self._as_bool(
            config.get("require_bot_addressed", False)
        )
        self.semantic_timeout = float(config.get("semantic_timeout", 12) or 12)
        self.semantic_timeout = max(3.0, min(30.0, self.semantic_timeout))

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)

    @staticmethod
    def _plain_text(components) -> str:
        parts = []
        for comp in components or []:
            text = getattr(comp, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        return " ".join(parts).strip()

    @staticmethod
    def _heuristic_target(current_text: str) -> str:
        """Fallback when the semantic classifier is unavailable.

        A question is assumed to be aimed at the current speaker; a short
        statement that quotes someone is assumed to be an answer to the quoted
        message.
        """

        text = " ".join(str(current_text or "").split())
        if not text:
            return "quoted"
        if "?" in text or "？" in text:
            return "current"
        if any(word in text for word in _QUESTION_WORDS):
            return "current"
        if len(text) <= 24:
            return "quoted"
        return "current"

    async def _classify_target(
        self,
        event: AstrMessageEvent,
        current_text: str,
        quoted_text: str,
        reply_text: str,
    ) -> str:
        """Ask the active chat model who the generated reply is aimed at."""

        if self.target_mode != "semantic":
            return self.target_mode
        if not getattr(self, "context", None):
            return self._heuristic_target(current_text)

        prompt = (
            "你是群聊回复目标判定器。只输出一个 JSON，不要解释。\n\n"
            f"当前发言人 B 的消息：\n{current_text or '（无文字）'}\n\n"
            f"B 引用的 A 的消息：\n{quoted_text or '（无文字）'}\n\n"
            f"bot 准备发送的回复：\n{reply_text or '（无文字）'}\n\n"
            "判断 bot 这条回复主要是在回复谁：\n"
            '- 回答 B 新提出的问题、回应 B 的新内容：{"target":"current"}\n'
            '- 回应 A 被引用的原消息、替 A 继续话题：{"target":"quoted"}\n'
            "不要只根据“谁引用了谁”机械判断，要看回复内容本身。"
        )
        system_prompt = (
            "你只做回复目标分类。输出必须是合法 JSON，"
            '格式为 {"target":"current"} 或 {"target":"quoted"}。'
        )
        try:
            provider = await self.context.get_using_provider_async(
                event.unified_msg_origin
            )
            if provider is None:
                return self._heuristic_target(current_text)
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                ),
                timeout=self.semantic_timeout,
            )
            raw = str(getattr(response, "completion_text", "") or "").strip()
            match = _TARGET_RE.search(raw)
            if match:
                return match.group(1).lower()
            try:
                payload = json.loads(raw)
                target = str(payload.get("target", "")).lower()
                if target in {"current", "quoted"}:
                    return target
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fallback must never break replies
            logger.debug("[ReplyTargetFix] semantic target check failed: %s", exc)
        return self._heuristic_target(current_text)

    @filter.on_decorating_result(priority=-50000)
    async def fix_reply_target(self, event: AstrMessageEvent) -> None:
        """Rewrite At/Reply components just before the result is sent."""

        if not self.enabled or self.target_mode == "current":
            return
        if event.get_platform_name() != "aiocqhttp":
            return
        if event.is_private_chat() and not self.fix_private:
            return
        if self.require_bot_addressed and not event.is_at_or_wake_command:
            return
        if event.get_extra("_reply_target_fix_wrapped", False):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return
        has_at = any(isinstance(comp, At) for comp in result.chain)
        has_reply = any(isinstance(comp, Reply) for comp in result.chain)
        if not has_at and not has_reply:
            return

        quoted = next(
            (comp for comp in event.get_messages() if isinstance(comp, Reply)),
            None,
        )
        if quoted is None:
            return

        quoted_id = str(getattr(quoted, "id", "") or "").strip()
        quoted_sender = str(
            getattr(quoted, "sender_id", None)
            or getattr(quoted, "qq", None)
            or ""
        ).strip()
        if not quoted_id or quoted_sender in {"", "0", "None"}:
            return
        if quoted_sender == str(event.get_self_id()):
            if self.skip_self_quote:
                return
        if quoted_id == str(getattr(event.message_obj, "message_id", "")):
            return

        current_text = event.get_message_str()
        quoted_text = str(getattr(quoted, "message_str", "") or "").strip()
        if not quoted_text:
            quoted_text = self._plain_text(getattr(quoted, "chain", None))
        reply_text = result.get_plain_text()

        target = await self._classify_target(
            event,
            current_text,
            quoted_text,
            reply_text,
        )
        if target != "quoted":
            logger.debug(
                "[ReplyTargetFix] keep current target: current_sender=%s quoted=%s",
                event.get_sender_id(),
                quoted_sender,
            )
            return

        quoted_name = str(getattr(quoted, "sender_nickname", "") or "").strip()
        original_send = event.send
        if not callable(original_send):
            return

        async def send_with_target(message) -> None:
            chain = getattr(message, "chain", None)
            if chain is not None:
                cleaned = [
                    comp
                    for comp in chain
                    if not isinstance(comp, (At, Reply))
                ]
                if has_at:
                    cleaned.insert(0, At(qq=quoted_sender, name=quoted_name))
                if has_reply:
                    cleaned.insert(0, Reply(id=quoted_id))
                message.chain = cleaned
            await original_send(message)

        event.send = send_with_target
        event.set_extra("_reply_target_fix_wrapped", True)
        logger.debug(
            "[ReplyTargetFix] corrected target: message=%s sender=%s",
            quoted_id,
            quoted_sender,
        )
