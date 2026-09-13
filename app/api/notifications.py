import httpx

from app.core.logging import logger
from app.core.state import ADMIN_CONFIG


async def send_telegram_message(text, *, token=None, chat_id=None, parse_mode=None):
    """发送 Telegram 消息。

    返回 ``(ok, message)``，方便设置页的测试按钮把真实错误反馈给用户。

    这里默认不带 parse_mode：旧消息里大量使用 ``**粗体**``，Telegram 的
    legacy Markdown 并不支持这种语法，API 会直接返回 400，导致告警发不出去。
    不指定 parse_mode 至少能保证通知可靠送达，Markdown 符号最多原样显示。
    """
    token = token if token is not None else ADMIN_CONFIG.get('tg_bot_token')
    chat_id = chat_id if chat_id is not None else ADMIN_CONFIG.get('tg_chat_id')
    token = str(token or '').strip()
    chat_id = str(chat_id or '').strip()

    if not token or not chat_id:
        msg = 'Telegram Bot Token 或 Chat ID 未配置'
        logger.warning(f"⚠️ TG 发送跳过: {msg}")
        return False, msg

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                detail = resp.text[:300]
                msg = f'Telegram API 返回 {resp.status_code}: {detail}'
                logger.error(f"❌ TG 发送失败: {msg}")
                return False, msg
            return True, '发送成功'
    except Exception as e:
        msg = str(e)
        logger.error(f"❌ TG 发送失败: {msg}")
        return False, msg
