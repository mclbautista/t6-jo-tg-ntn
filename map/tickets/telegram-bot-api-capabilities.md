# Telegram Bot API Capabilities and Limits in Forum-Topic Groups

This document summarizes the official Telegram Bot API specifications regarding bot visibility, topic management, message forwarding metadata, reactions, edit updates, cross-group posting, deployment modes, and rate limits.

---

## 1. Receiving Messages in an Intake Topic (Privacy Mode & Admin Rights)

By default, Telegram bots operate with **Group Privacy Mode enabled**. When privacy mode is enabled, a bot in a group or supergroup only receives:
* Messages starting with a forward slash (`/`) command.
* Direct replies to the bot's own messages.
* Service messages (e.g., thread creation, user joins).
* Messages where the bot is explicitly mentioned by username (`@botusername`).

To receive **every message** posted in an intake topic, one of the following configurations is required:

1. **Disable Privacy Mode**:
   * **Action**: Disable Privacy Mode via [@BotFather](https://t.me/BotFather) (`/setprivacy` -> `Disable`).
   * **Requirement**: If the bot was added to the group prior to changing this setting, it must be removed and re-added to the group for the change to take effect.
2. **Promote Bot to Group Administrator**:
   * **Action**: Grant the bot Administrator permissions within the group.
   * **Effect**: Bots with administrator status automatically bypass Privacy Mode and receive all messages posted in all topics across the group.

### Documentation Links
* [Telegram Bot Features — Privacy Mode](https://core.telegram.org/bots/features#privacy-mode)
* [Telegram Bot FAQ — What messages will be sent to my bot?](https://core.telegram.org/bots/faq#what-messages-will-be-sent-to-my-bot)

---

## 2. Topic Representation in Updates and Restricting to One Topic

### How Topics Appear in Updates
In supergroups with forum topics enabled (`Chat.is_forum = True`):
* Messages sent inside a forum topic contain the boolean field `Message.is_topic_message = True`.
* The topic is identified by `Message.message_thread_id` (an `Integer`), which corresponds to the `message_id` of the thread creation message (or `1` for the General topic).

### Restricting Handling to a Single Topic
To process messages exclusively from a specific intake topic:
1. Store the target topic ID (e.g., `TARGET_THREAD_ID`).
2. When receiving an incoming `Update` (`Update.message`), inspect `message.message_thread_id`.
3. If `message.message_thread_id` is missing (`null`) or does not equal `TARGET_THREAD_ID`, discard the update.

### Documentation Links
* [Telegram Bot API — Message Object](https://core.telegram.org/bots/api#message) (`message_thread_id`, `is_topic_message`)
* [Telegram Bot API — ForumTopic Object](https://core.telegram.org/bots/api#forumtopic)

---

## 3. Forwarded Message Metadata (`forward_origin`)

Forwarded messages carry origin metadata inside the `forward_origin` field of the `Message` object. This field contains a `MessageOrigin` object, which is a tagged union differentiated by the `type` property.

### Metadata Types & Fields

| `type` | Origin Class | Fields Carried | Description / Edge Cases |
| :--- | :--- | :--- | :--- |
| `"user"` | `MessageOriginUser` | `date`, `sender_user` (`User` object) | Standard forward from a user who allows account linking in forward settings. |
| `"hidden_user"` | `MessageOriginHiddenUser` | `date`, `sender_user_name` (`String`) | Occurs when the original sender enabled "Forwarded Messages" privacy (`Nobody`/`My Contacts`). Contains only plain text display name; no user ID or account link is available. |
| `"chat"` | `MessageOriginChat` | `date`, `sender_chat` (`Chat`), `author_signature` (optional) | Messages forwarded from a group chat or an anonymous group admin. |
| `"channel"` | `MessageOriginChannel` | `date`, `chat` (`Chat`), `message_id`, `author_signature` (optional) | Messages forwarded from a channel. Includes original `message_id` in the channel. |

### Documentation Links
* [Telegram Bot API — Message Object (`forward_origin`)](https://core.telegram.org/bots/api#message)
* [Telegram Bot API — MessageOrigin](https://core.telegram.org/bots/api#messageorigin)

---

## 4. Setting Message Reactions (`setMessageReaction`)

### Capabilities & Rules
* **Method**: `setMessageReaction`
* **Parameters**:
  * `chat_id`: Target chat identifier.
  * `message_id`: Target message identifier.
  * `reaction`: Array of `ReactionType` objects. Passing an empty array or omitting clears the bot's reaction.
  * `is_big` (optional): Boolean for animated large reaction effect.
* **Limits**: Non-premium bot accounts can set a maximum of **1 reaction per message**.

### Supported Reaction Types
1. **Emoji (`ReactionTypeEmoji`)**:
   * Syntax: `{"type": "emoji", "emoji": "👍"}`
   * Accepts standard Unicode reaction emojis allowed by Telegram (e.g., 👍, 👎, ❤️, 🔥, 🎉, 🤔, 👀, 💯, 👏, 😁, etc.).
2. **Custom Emoji (`ReactionTypeCustomEmoji`)**:
   * Syntax: `{"type": "custom_emoji", "custom_emoji_id": "<id>"}`
   * Allowed only if the custom emoji is already present on the message or explicitly allowed by group admins.
3. **Paid Reactions (`ReactionTypePaid`)**:
   * Bots **cannot** send paid star reactions.

### Documentation Links
* [Telegram Bot API — setMessageReaction](https://core.telegram.org/bots/api#setmessagereaction)
* [Telegram Bot API — ReactionType](https://core.telegram.org/bots/api#reactiontype)

---

## 5. Arrival and Filtering of Edited Messages (`edited_message`)

### Update Structure
When a user edits a previously sent message, Telegram sends an `Update` object where:
* `Update.message` is **null**.
* `Update.edited_message` contains the updated `Message` object.

### Safe Filtering Strategy
* **Application Level**: In your update handler/router, process updates only if `Update.message != null`. Ignore any update containing `Update.edited_message` or `Update.edited_channel_post`.
* **API Level**: When calling `setWebhook` or `getUpdates`, pass `allowed_updates = ["message"]`. This instructs Telegram servers to filter out edit updates before delivering them to your application.

### Documentation Links
* [Telegram Bot API — Update Object](https://core.telegram.org/bots/api#update)
* [Telegram Bot API — getUpdates](https://core.telegram.org/bots/api#getupdates) / [setWebhook](https://core.telegram.org/bots/api#setwebhook)

---

## 6. Posting Messages into a Topic of a Different Group

### Method & Parameters
To send a message into a specific forum topic in a target group:
* Call `sendMessage` (or `sendPhoto`, `sendDocument`, etc.).
* Set `chat_id` to the target group's unique ID (e.g., `-1001234567890`).
* Set `message_thread_id` to the target topic's thread ID in that group.

### Requirements
* The bot must be a member of the destination group.
* The bot must have permission to post in the destination group/topic (or be an Administrator).

### Documentation Links
* [Telegram Bot API — sendMessage](https://core.telegram.org/bots/api#sendmessage)

---

## 7. Deployment Considerations (Polling vs. Webhook) & Rate Limits

### Long Polling vs. Webhook in Docker

| Feature | Long Polling (`getUpdates`) | Webhook (`setWebhook`) |
| :--- | :--- | :--- |
| **Network Overhead** | Low (maintains long HTTP connection). | Event-driven (HTTP POST per update). |
| **Infrastructure Needs** | None (works behind NAT, firewalls, and dynamic IPs). | Requires public domain, open incoming port, SSL/TLS cert, reverse proxy. |
| **Docker Complexity** | **Very Low** (standalone container with outgoing internet access). | **Higher** (requires ingress controller, domain DNS, SSL renewal). |
| **Low-Volume Fit (~12 msgs/day)** | **Optimal** (minimal resource usage, zero incoming network exposure). | Overkill for low volume unless running on serverless/scale-to-zero infrastructure. |

### Relevant Rate Limits (~12 Messages/Day Volume)

Telegram enforces the following global rate limits:
* **Per Chat Limit**: Up to 1 message per second in a specific chat/topic.
* **Group Chat Limit**: Up to 20 messages per minute per group.
* **Global Bot Limit**: Up to 30 messages per second across all chats.

**Assessment at ~12 msgs/day**:
At ~12 messages per day, the bot operates several orders of magnitude below all Telegram API rate limits. No complex queue or rate-limiting middleware is required, beyond standard exception handling for HTTP 429 (`retry_after`) responses.

### Documentation Links
* [Telegram Bot FAQ — Rate Limits](https://core.telegram.org/bots/faq#my-bot-is-hits-rate-limits-how-do-i-avoid-this)
* [Telegram Bot API — getUpdates](https://core.telegram.org/bots/api#getupdates)