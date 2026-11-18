# voice norm telegram bot

Telegram bot that replies to every voice message with a loudness-normalized version.

For groups: in @BotFather -> /mybots -> your bot -> Bot Settings ->
Group Privacy -> Turn OFF (or make the bot a group admin), otherwise the bot
never sees voice messages in groups.

## build and run

```
docker compose up -d --build
```
