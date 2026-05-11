import asyncio
from Astandy import StandClient
import Astandy
from Astandy.generated.listeners import (
    GetPlayerInventoryRequest,
    GetPlayerRequest,
    GetTradeOpenSaleRequestsRequest,
    MarketplaceRemoteEventListenerOnTradeRequestOpenedUpdate,
    MarketplaceRemoteEventListenerOnTradeRequestClosedUpdate,
    CreateSaleRequest,
    CancelRequestRequest,
    CreatePurchaseRequestBySaleRequest,
    OnPlayerAvatarChangedEvent
)
from collections import defaultdict
import random
from datetime import datetime, timedelta
import time
from aiogram import Bot, Dispatcher, types
from aiogram.client.bot import DefaultBotProperties
from aiogram.filters import Command

client = StandClient("", host="")

tg_token = ""
group_id = None
topic_id = None
bot = Bot(
    token=tg_token,
    default=DefaultBotProperties(parse_mode="HTML")
)
users = [
    123456789,
]

last_event_time = time.time()
SKIN_NAMES = {
    240097: "G22_Flock",
    1240097: "G22_FlockST",
    240091: "M4_Flock",
    240052: "MP7_FestalWrap",
    220013: "Tec9_TieDye",
    240095: "USP_Ghosts",
    240021: "SM1014_Serpent",
    240072: "USP_Corrode",
    240243: "Mallard_FallingLeaves",
    240246: "SM1014_FallingLeaves",
}

SKINS_CONFIG = {
    240097: {
        "name": "G22_Flock",#название скина
        "parse_lots": 50000,#количество лотов которое будет парсить при старте для автопокупки
        "buy_price": 10.0,#максимальная цена покупки пустого паттерн скина
        "min_price": 50.0,#минимальный прайс от которого будет подсос
        "max_price": 63000.0,#максимальный прайс до которого будет подсос
    },
}

PERIODIC_PARSE_INTERVAL = 10800

parsed_market_skins = []
parsed_market_lock = asyncio.Lock()

initial_parse_complete = False
trade_queue = asyncio.Queue()
ready_event = asyncio.Event()
is_paused = False
my_name = None
my_avatar_id = None

session_profit = 0.0
sold_skins = []
profit_lock = asyncio.Lock()

first_start = True

class AntispamTracker:
    def __init__(self):
        self.user_actions = {}
        self.blacklist = set()
        self.lock = asyncio.Lock()
        self.MAX_ACTIONS = 3
        self.TIME_WINDOW = 300
        self.BLACKLIST_TIME = 259200
        self.blacklist_expiry = {}

    async def add_action(self, avatar_id, action_type):
        if not avatar_id:
            return False

        async with self.lock:
            if avatar_id in self.blacklist:
                if time.time() < self.blacklist_expiry.get(avatar_id, 0):
                    return False
                else:
                    self.blacklist.remove(avatar_id)
                    del self.blacklist_expiry[avatar_id]

            if avatar_id not in self.user_actions:
                self.user_actions[avatar_id] = []

            current_time = time.time()
            self.user_actions[avatar_id].append({
                "time": current_time,
                "type": action_type
            })

            self.user_actions[avatar_id] = [
                action for action in self.user_actions[avatar_id]
                if current_time - action["time"] <= self.TIME_WINDOW
            ]

            if len(self.user_actions[avatar_id]) >= self.MAX_ACTIONS:
                self.blacklist.add(avatar_id)
                self.blacklist_expiry[avatar_id] = current_time + self.BLACKLIST_TIME
                del self.user_actions[avatar_id]
                print(f"\n🚫 Пользователь {avatar_id} добавлен в черный список")

            return True

    async def is_blacklisted(self, avatar_id):
        async with self.lock:
            if avatar_id in self.blacklist:
                if time.time() < self.blacklist_expiry.get(avatar_id, 0):
                    return True
                else:
                    self.blacklist.remove(avatar_id)
                    if avatar_id in self.blacklist_expiry:
                        del self.blacklist_expiry[avatar_id]
            return False

    async def get_user_stats(self, avatar_id):
        async with self.lock:
            if avatar_id in self.user_actions:
                return {
                    "action_count": len(self.user_actions[avatar_id]),
                    "actions": self.user_actions[avatar_id][-5:]
                }
            return {"action_count": 0, "actions": []}

    async def remove_from_blacklist(self, avatar_id):
        async with self.lock:
            if avatar_id in self.blacklist:
                self.blacklist.remove(avatar_id)
                if avatar_id in self.blacklist_expiry:
                    del self.blacklist_expiry[avatar_id]
                print(f"\n✅ Пользователь {avatar_id} удален из черного списка")
                return True
            return False

antispam = AntispamTracker()

dp = Dispatcher()

async def check_command_access(message: types.Message) -> bool:
    if message.chat.id != group_id or message.message_thread_id != topic_id:
        return False

    if message.from_user.id not in users:
        await message.answer(
            f"❌ <b>У вас нет прав на использование команд!</b>\n"
            f"Ваш ID: <code>{message.from_user.id}</code>",
            parse_mode="HTML"
        )
        return False

    return True

@dp.message(Command("stat"))
async def cmd_stat(message: types.Message):
    if not await check_command_access(message):
        return

    try:
        balance = await get_balance(client)
        open_requests = await get_open_requests(client)
        all_skins_on_sale = len(open_requests)

        stat_message = (
            f"<blockquote>💰 Баланс: <b>{round(balance, 2)} G</b></blockquote>\n\n"
            f"<blockquote>📦 Всего скинов на продаже: <b>{all_skins_on_sale}</b></blockquote>"
        )

        await message.reply(stat_message, parse_mode="HTML")

    except Exception as e:
        error_message = f"❌ Ошибка получения статистики: {e}"
        await message.reply(error_message)

@dp.message(Command("cancel_all"))
async def cmd_cancel_all(message: types.Message):
    global is_paused

    if not await check_command_access(message):
        return

    if is_paused:
        await message.answer(
            "⚠️ <b>Бот уже выполняет отмену скинов. Пожалуйста, подождите.</b>",
            parse_mode="HTML"
        )
        return

    is_paused = True

    status_msg = await message.answer(
        "⏸️ <b>Бот приостановлен. Начинаю отмену всех скинов...</b>\n"
        "<i>Это может занять некоторое время...</i>",
        parse_mode="HTML"
    )

    try:
        open_requests = await get_open_requests(client)
        total_before = len(open_requests)

        if total_before == 0:
            await status_msg.edit_text(
                "📭 <b>Нет активных скинов для отмены.</b>\n"
                "🟢 <b>Бот продолжает работу.</b>",
                parse_mode="HTML"
            )
            is_paused = False
            return

        await status_msg.edit_text(
            f"⏸️ <b>Бот приостановлен.</b>\n"
            f"🔄 Отменяю скины: <b>0/{total_before}</b>\n"
            f"<i>Пожалуйста, подождите...</i>",
            parse_mode="HTML"
        )

        cancelled_count = await otm_all_skins(client)
        new_balance = await get_balance(client)

        await status_msg.edit_text(
            f"✅ <b>Операция завершена!</b>\n\n"
            f"📊 <b>Результат:</b>\n"
            f"  • Отменено скинов: <b>{cancelled_count}/{total_before}</b>\n"
            f"  • Осталось в продаже: <b>{total_before - cancelled_count}</b>\n"
            f"  • Текущий баланс: <b>{round(new_balance, 2)} G</b>\n\n"
            f"🟢 <b>Бот продолжает работу.</b>",
            parse_mode="HTML"
        )

    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка при отмене скинов:</b>\n"
            f"<code>{e}</code>\n\n"
            f"⚠️ <b>Бот возобновляет работу.</b>",
            parse_mode="HTML"
        )

    finally:
        is_paused = False

@dp.message(Command("buy"))
async def handle_buy_skin(message: types.Message):
    if not await check_command_access(message):
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "❌ <b>Использование:</b> /buy <id_лота>",
            parse_mode="HTML"
        )
        return

    sale_id = args[1]

    status_msg = await message.answer(
        f"🔄 <b>Покупаю лот {sale_id}...</b>",
        parse_mode="HTML"
    )

    try:
        success = await buy_skin_by_id(client, sale_id)

        if success:
            new_balance = await get_balance(client)

            await status_msg.edit_text(
                f"✅ <b>Лот {sale_id} успешно куплен!</b>\n\n"
                f"💰 Новый баланс: <b>{round(new_balance, 2)} G</b>",
                parse_mode="HTML"
            )
        else:
            await status_msg.edit_text(
                f"❌ <b>Не удалось купить лот {sale_id}</b>\n"
                f"Возможно, лот уже продан или произошла ошибка",
                parse_mode="HTML"
            )

    except Exception as e:
        error_text = str(e).lower()

        if "недостаточно" in error_text or "insufficient" in error_text or "1531" in error_text:
            balance = await get_balance(client)
            await status_msg.edit_text(
                f"❌ <b>Недостаточно средств для покупки!</b>\n"
                f"💰 Текущий баланс: <b>{round(balance, 2)} G</b>",
                parse_mode="HTML"
            )
        else:
            await status_msg.edit_text(
                f"❌ <b>Ошибка при покупке лота {sale_id}:</b>\n"
                f"<code>{e}</code>",
                parse_mode="HTML"
            )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if not await check_command_access(message):
        return

    try:
        balance = await get_balance(client)
        player_name = await get_name(client)
        open_requests = await get_open_requests(client)

        status_text = (
            f"🤖 <b>Статус бота</b>\n"
            f"👤 Игрок: <code>{player_name}</code>\n"
            f"💳 Баланс: <b>{balance:.2f} G</b>\n"
            f"📊 Открытых запросов: <b>{len(open_requests)}</b>\n"
            f"⏸️ Приостановлен: <b>{'Да' if is_paused else 'Нет'}</b>"
        )
        await message.answer(status_text, parse_mode="HTML")

    except Exception as e:
        await message.answer(f"❌ <b>Ошибка получения статуса:</b>\n<code>{e}</code>", parse_mode="HTML")

async def send_sale_notification(skin_name, sale_price, net_profit, total_profit, pattern, sticker_status, balance):
    message_part1 = (
        f"<blockquote>creator @Filyatrayder</blockquote>\n\n"
    )

    message_part2 = (
        f"<blockquote>Skin: {skin_name}\n"
        f"sale price: {sale_price} G\n"
        f"pattern: {pattern}\n"
        f"magic: {sticker_status}</blockquote>\n\n"
    )

    message_part3 = (
        f"<blockquote>💵 profit: {net_profit} G\n"
        f"📈 total profit: {round(total_profit, 2)} G\n"
        f"Balance: {round(balance, 2)} G</blockquote>"
    )

    full_message = message_part1 + message_part2 + message_part3

    try:
        await bot.send_message(
            chat_id=group_id,
            text=full_message,
            message_thread_id=topic_id
        )
    except Exception:
        pass

async def otm_all_skins(client) -> int:
    try:
        open_requests = await get_open_requests(client)

        if not open_requests:
            return 0

        cancelled_count = 0
        total_requests = len(open_requests)

        for i, req in enumerate(open_requests, 1):
            if await cancel_sale_request(client, req.id):
                cancelled_count += 1

            await asyncio.sleep(0.3)

        return cancelled_count

    except Exception:
        raise

async def get_my_inventory(client):
    request = GetPlayerInventoryRequest()

    response = client.raw.InventoryRemoteService.getPlayerInventoryEncryptedResponse(
        await client.send_request(
            *client.raw.InventoryRemoteService.getPlayerInventoryEncryptedRequest(
                request,
                client.cipher
            )
        ),
        client.cipher
    )

    return response

async def get_inventory_items(client):
    inv = await get_my_inventory(client)
    return inv.playerInventory.inventoryItems

async def get_open_requests(client):
    request = Astandy.generated.GetPlayerOpenRequestsRequest()

    response = client.raw.MarketplaceRemoteService.getPlayerOpenRequests2Response(
        await client.send_request(
            *client.raw.MarketplaceRemoteService.getPlayerOpenRequests2Request(request)
        )
    )
    return response.openRequests

async def get_balance(client):
    inv = await get_my_inventory(client)
    return inv.playerInventory.currencies[1].value

async def get_name(client):
    request = GetPlayerRequest()

    response = client.raw.PlayerRemoteService.getPlayer2Response(
        await client.send_request(
            *client.raw.PlayerRemoteService.getPlayer2Request(request)
        )
    )

    return response.player.name

async def get_my_avatar_id(client):
    request = GetPlayerRequest()

    response = client.raw.PlayerRemoteService.getPlayer2Response(
        await client.send_request(
            *client.raw.PlayerRemoteService.getPlayer2Request(request)
        )
    )

    return response.player.avatarId

async def cancel_sale_request(client, request_id):
    request = CancelRequestRequest(requestId=request_id)

    try:
        client.raw.MarketplaceRemoteService.cancelRequest2Response(
            await client.send_request(
                *client.raw.MarketplaceRemoteService.cancelRequest2Request(request)
            )
        )
        return True
    except Exception:
        return False

async def create_sale_request(client, item_id, price):
    request = CreateSaleRequest(itemId=item_id, price=price)

    try:
        response = client.raw.MarketplaceRemoteService.createSaleResponse(
            await client.send_request(
                *client.raw.MarketplaceRemoteService.createSaleRequest(request)
            )
        )
        return response
    except Exception:
        return None

async def buy_skin_by_id(client, sale_id):
    request = CreatePurchaseRequestBySaleRequest(saleId=sale_id)

    try:
        client.raw.MarketplaceRemoteService.createPurchaseRequestBySale2Response(
            await client.send_request(
                *client.raw.MarketplaceRemoteService.createPurchaseRequestBySale2Request(request)
            )
        )
        return True
    except Exception:
        return False

async def find_all_skins_on_market(skin_id, pattern):
    global parsed_market_skins

    all_offers = []

    async with parsed_market_lock:
        for item_data in parsed_market_skins:
            if item_data["itemDefinitionId"] == skin_id:
                for offer in item_data["offers"]:
                    if offer.get("pattern") == pattern:
                        all_offers.append(offer)

    all_offers.sort(key=lambda x: x["price"])
    return all_offers

async def find_skin_in_inventory(client, skin_id, pattern):
    items = await get_inventory_items(client)

    for item in items:
        if item.itemDefinitionId == skin_id:
            mods = getattr(item, "modifications", {}) or {}
            p_obj = mods.get("pattern")
            if p_obj and hasattr(p_obj, "intValue") and p_obj.intValue == pattern:
                has_sticker = any(
                    k.startswith("sticker_") and hasattr(v, "intValue") and v.intValue > 0
                    for k, v in mods.items()
                )
                if not has_sticker:
                    return item.id

    return None

async def find_skin_in_open_requests(client, skin_id, pattern):
    requests = await get_open_requests(client)

    for req in requests:
        if req.itemDefinitionId == skin_id:
            mods = getattr(req, "modifications", {}) or {}
            p_obj = mods.get("pattern")
            if p_obj and hasattr(p_obj, "intValue") and p_obj.intValue == pattern:
                has_sticker = any(
                    k.startswith("sticker_") and hasattr(v, "intValue") and v.intValue > 0
                    for k, v in mods.items()
                )
                if not has_sticker:
                    return req.id, req.price

    return None, None

async def find_skin_on_market(skin_id, pattern):
    global parsed_market_skins

    async with parsed_market_lock:
        for item_data in parsed_market_skins:
            if item_data["itemDefinitionId"] == skin_id:
                for offer in item_data["offers"]:
                    if offer.get("pattern") == pattern:
                        return offer

    return None

async def get_purchase_skins_paginated(client, skin_id, lotov):
    all_requests = []
    seen_ids = set()
    page = 0
    chunk_size = 500

    while len(all_requests) < lotov:
        size = min(chunk_size, lotov - len(all_requests))

        request = GetTradeOpenSaleRequestsRequest(
            id=skin_id,
            page=page,
            size=size
        )

        try:
            response = client.raw.MarketplaceRemoteService.getFilteredTradeOpenSaleRequestsResponse(
                await client.send_request(
                    *client.raw.MarketplaceRemoteService.getFilteredTradeOpenSaleRequestsRequest(request)
                )
            )
        except Exception as e:
            if "1530" in str(e):
                page += 1
                continue
            raise

        open_requests = list(response.openRequests or [])

        if not open_requests:
            break

        new_requests = []
        for tr in open_requests:
            if tr.id not in seen_ids:
                seen_ids.add(tr.id)
                new_requests.append(tr)

        if not new_requests:
            break

        all_requests.extend(new_requests)
        page += 1

        if len(open_requests) < size:
            break

    return all_requests[:lotov]

async def periodic_market_parser():
    global parsed_market_skins, initial_parse_complete, first_start

    while True:
        by_item = {}
        total_records = 0

        for skin_id, cfg in SKINS_CONFIG.items():
            lots_to_parse = cfg["parse_lots"]
            skin_name = SKIN_NAMES.get(skin_id, skin_id)

            try:
                offers = await get_purchase_skins_paginated(
                    client,
                    skin_id,
                    lots_to_parse
                )
            except Exception:
                continue

            records = []
            for tr in offers:
                mods = getattr(tr, "modifications", {}) or {}

                has_sticker = any(
                    k.startswith("sticker_") and hasattr(v, "intValue") and v.intValue > 0
                    for k, v in mods.items()
                )

                if has_sticker:
                    continue

                pattern_val = None
                p_obj = mods.get("pattern")
                if p_obj and hasattr(p_obj, "intValue"):
                    pattern_val = p_obj.intValue

                records.append({
                    "id": tr.id,
                    "pattern": pattern_val,
                    "price": round(tr.price, 2),
                    "avatarId": tr.creator.avatarId
                })

            by_item[skin_id] = records
            total_records += len(records)

        async with parsed_market_lock:
            parsed_market_skins = [
                {"itemDefinitionId": k, "offers": v}
                for k, v in by_item.items()
            ]

        if not initial_parse_complete:
            initial_parse_complete = True
            ready_event.set()

        await asyncio.sleep(PERIODIC_PARSE_INTERVAL)

@client.MarketplaceRemoteEventListenerOnTradeRequestOpened()
async def on_trade_opened(client: StandClient, update: MarketplaceRemoteEventListenerOnTradeRequestOpenedUpdate):
    global last_event_time
    last_event_time = time.time()
    global is_paused

    if is_paused:
        return

    await ready_event.wait()

    try:
        await process_opened_trade(update)
    except Exception:
        pass

@client.MarketplaceRemoteEventListenerOnTradeRequestClosed()
async def on_trade_closed(client: StandClient, update: MarketplaceRemoteEventListenerOnTradeRequestClosedUpdate):
    global last_event_time
    last_event_time = time.time()
    global is_paused

    if is_paused:
        return

    await ready_event.wait()

    try:
        await process_closed_trade(update)
    except Exception:
        pass

async def restart_client():
    global client

    try:
        await client.stop()
    except Exception:
        pass

    await asyncio.sleep(2)

    try:
        await client.start()
    except Exception:
        pass

def register_handlers():
    @client.MarketplaceRemoteEventListenerOnTradeRequestOpened()
    async def on_trade_opened(client, update):
        global last_event_time
        last_event_time = time.time()

        if is_paused:
            return

        await ready_event.wait()
        await process_opened_trade(update)

    @client.MarketplaceRemoteEventListenerOnTradeRequestClosed()
    async def on_trade_closed(client, update):
        global last_event_time
        last_event_time = time.time()

        if is_paused:
            return

        await ready_event.wait()
        await process_closed_trade(update)

async def process_opened_trade(update: MarketplaceRemoteEventListenerOnTradeRequestOpenedUpdate):
    global my_avatar_id

    req = update.data.request

    if req.type != 1:
        return

    if req.creator.avatarId == my_avatar_id:
        return

    if await antispam.is_blacklisted(req.creator.avatarId):
        return

    skin_id = req.itemDefinitionId

    if skin_id not in SKINS_CONFIG:
        return

    cfg = SKINS_CONFIG[skin_id]

    skin_name = cfg["name"]
    buy_limit = cfg["buy_price"]

    target_price = round(req.price, 2)

    min_price = cfg["min_price"]
    max_price = cfg["max_price"]

    if target_price < min_price or target_price > max_price:
        return

    mods = getattr(req, "modifications", {}) or {}

    p_obj = mods.get("pattern")

    if not p_obj or not hasattr(p_obj, "intValue"):
        return

    pattern = p_obj.intValue

    has_sticker = any(
        k.startswith("sticker_") and hasattr(v, "intValue") and v.intValue > 0
        for k, v in mods.items()
    )

    if has_sticker:
        return

    print(f"\n🔔 Скин выставлен: {skin_name} | цена: {target_price} G | паттерн: {pattern} | продавец: {req.creator.name}")

    request_id, current_price = await find_skin_in_open_requests(client, skin_id, pattern)

    if request_id:
        if await cancel_sale_request(client, request_id):
            await asyncio.sleep(0.5)
            item_id = await find_skin_in_inventory(client, skin_id, pattern)
            if item_id:
                result = await create_sale_request(client, item_id, target_price)
                if result:
                    await antispam.add_action(req.creator.avatarId, "price_change")
        return

    item_id = await find_skin_in_inventory(client, skin_id, pattern)

    if item_id:
        result = await create_sale_request(client, item_id, target_price)
        if result:
            await antispam.add_action(req.creator.avatarId, "new_listing")
        return

    all_market_offers = await find_all_skins_on_market(skin_id, pattern)

    if not all_market_offers:
        return

    bought = False
    for i, market_offer in enumerate(all_market_offers, 1):
        market_price = market_offer["price"]
        market_id = market_offer["id"]

        if market_price > buy_limit:
            continue

        if await buy_skin_by_id(client, market_id):
            bought = True
            break

    if not bought:
        return

    await asyncio.sleep(1)

    item_id = await find_skin_in_inventory(client, skin_id, pattern)

    if not item_id:
        return

    result = await create_sale_request(client, item_id, target_price)

    if result:
        await antispam.add_action(req.creator.avatarId, "buy_and_list")

async def process_closed_trade(update: MarketplaceRemoteEventListenerOnTradeRequestClosedUpdate):
    global my_name, my_avatar_id, session_profit, sold_skins

    trade = update.data.request

    if trade.type != 1:
        return

    if trade.creator.avatarId != my_avatar_id or trade.creator.name != my_name:
        return

    if trade.reason != 1:
        return

    skin_id = trade.itemDefinitionId
    skin_name = SKIN_NAMES.get(skin_id, f"Unknown({skin_id})")

    net_profit = round(trade.price, 2)
    sale_price = round(net_profit / 0.8, 2)

    mods = getattr(trade, "modifications", {}) or {}

    p_obj = mods.get("pattern")
    pattern = p_obj.intValue if p_obj and hasattr(p_obj, "intValue") else "N/A"

    has_sticker = any(
        k.startswith("sticker_") and hasattr(v, "intValue") and v.intValue > 0
        for k, v in mods.items()
    )

    sticker_status = "со стикерами" if has_sticker else "без стикеров"

    async with profit_lock:
        session_profit += net_profit
        total_profit = session_profit

        sold_skins.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "skin": skin_name,
            "price": sale_price,
            "net_profit": net_profit,
            "pattern": pattern,
            "has_sticker": has_sticker
        })

        current_balance = await get_balance(client)

    await send_sale_notification(
        skin_name=skin_name,
        sale_price=sale_price,
        net_profit=net_profit,
        total_profit=total_profit,
        pattern=pattern,
        sticker_status=sticker_status,
        balance=current_balance
    )

async def reconnect_watcher():
    global last_event_time

    while True:
        await asyncio.sleep(15)

        silence = time.time() - last_event_time

        if silence > 60:
            await restart_client()
            last_event_time = time.time()

@client.OnConnect()
async def on_connect(c, _):
    print("\nКлиент подключен")

    global my_name, my_avatar_id

    try:
        my_name = await get_name(c)
        my_avatar_id = await get_my_avatar_id(c)
    except Exception:
        pass

    for skin_id in SKINS_CONFIG.keys():
        await c.subscribe_trade(skin_id)

async def main():
    global initial_parse_complete, my_name, my_avatar_id, session_profit, first_start

    await client.start()

    my_name = await get_name(client)
    my_avatar_id = await get_my_avatar_id(client)

    for skin_id in SKINS_CONFIG.keys():
        await client.subscribe_trade(skin_id)

    telegram_task = asyncio.create_task(dp.start_polling(bot))
    parser_task = asyncio.create_task(periodic_market_parser())

    await ready_event.wait()

    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        parser_task.cancel()
        telegram_task.cancel()
        try:
            await parser_task
        except asyncio.CancelledError:
            pass
        try:
            await telegram_task
        except asyncio.CancelledError:
            pass

        await client.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        pass