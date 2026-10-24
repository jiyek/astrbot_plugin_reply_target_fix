# 引用目标修正

修正 AstrBot 回复时的 @ 和引用目标。

## 问题

AstrBot 的 `result_decorate` 在发送前固定使用：

- `event.get_sender_id()` 作为 @ 对象
- `event.message_obj.message_id` 作为引用消息

因此它永远是“回复当前这条消息”。但实际语义有两种：

```text
B：[引用 A 的消息] @bot 这个你怎么看
```

这是 B 在问 bot，应该 @B、引用 B 的消息。

```text
B：[引用 A 的消息] 他是梁文峰
```

这是 B 在回答 A。如果 bot 要接 A 的话，应该 @A、引用 A 的原消息，而不是 @B、引用 B 的回答。

所以不能只看“有没有引用”，必须看回复内容在回应谁。

## 这个插件做什么

在 `on_decorating_result` 最后阶段，先等 AstrBot 把默认的 `At`/`Reply` 加完，再包装 `event.send`：

- 如果语义上是在回复当前发言人 B，保留 AstrBot 默认目标。
- 如果语义上是在回复被引用的 A，把 `At` 改成 A，把 `Reply` 改成 A 的原消息 ID。

只处理 `Aiocqhttp`，私聊默认不处理。

## 判断方式

默认 `target_mode = semantic`：

1. 读取当前消息、被引用的消息、bot 准备发送的回复。
2. 用当前聊天模型做一次很小的 JSON 分类：
   - `{"target":"current"}`：回复 B
   - `{"target":"quoted"}`：回复 A
3. 分类失败或超时，退回本地启发式：
   - 当前消息是问句 → 回复 B
   - 当前消息是短陈述/回答 → 回复 A

这会多一次轻量 LLM 调用。如果不想多调用，可改成：

- `target_mode = quoted`：只要群聊带引用就引用原消息，不判断语义。
- `target_mode = current`：完全关闭修正。

## 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `target_mode` | `semantic` | `semantic` / `quoted` / `current` |
| `require_bot_addressed` | `false` | 只处理 @bot 的消息 |
| `fix_private` | `false` | 私聊也修正 |
| `skip_self_quote` | `true` | 用户引用 bot 自己的消息时仍回复当前用户 |
| `semantic_timeout` | `12` | 语义判断超时（秒） |

## 与关闭核心开关的关系

如果只需要“不要 @ 错/引用错”，不要求保留引用，最省事的配置是关闭：

```text
platform_settings.reply_with_mention = false
platform_settings.reply_with_quote = false
```

想保留正确的 @/引用时，使用本插件。插件需要核心先加上 `At`/`Reply`，所以这两个开关要保持开启。

## 限制

- 语义分类是额外的一次 LLM 调用，不能保证 100% 正确；可在日志中查看 `[ReplyTargetFix]`。
- 如果 bot 的回复文本本身就没有指向 A 或 B，分类器也只能猜。
- 只处理 `aiocqhttp`；其他平台适配器的引用字段不同。
