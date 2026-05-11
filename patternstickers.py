import asyncio
import time
from collections import defaultdict
from datetime import datetime

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
    CreatePurchaseRequestRequest,
    MountInventoryItemRequest,
)
from aiogram import Bot, Dispatcher, types
from aiogram.client.bot import DefaultBotProperties
from aiogram.filters import Command

client = StandClient("", host="")

tg_bot_token = ""
tg_group = None
topic_id = None

bot = Bot(
    token=tg_bot_token,
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
    240095: {
        "name": "USP_Ghosts", #название скина
        "parse_lots": 50000, #количество лотов которое будет парсить при старте для автопокупки
        "buy_price": 12.0,#максимальная цена покупки пустого паттерн скина
        "min_price": 200.0,#минимальный прайс от которого будет подсос
        "max_price": 63000.0,#максимальный прайс до которого будет подсос
        "max_sticker_buy_price": 15.0,#максимальная цена покупки стикера
    },
    240097: {
        "name": "G22_Flock",
        "parse_lots": 70000,
        "buy_price": 10.0,
        "min_price": 300.0,
        "max_price": 63000.0,
        "max_sticker_buy_price": 15.0,
    },
}

PERIODIC_PARSE_INTERVAL = 10800

parsed_market_skins = []
parsed_market_lock = asyncio.Lock()
crafting_lock = asyncio.Lock()

initial_parse_complete = False
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
                    self.blacklist_expiry.pop(avatar_id, None)

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

            return True

    async def is_blacklisted(self, avatar_id):
        async with self.lock:
            if avatar_id in self.blacklist:
                if time.time() < self.blacklist_expiry.get(avatar_id, 0):
                    return True
                else:
                    self.blacklist.remove(avatar_id)
                    self.blacklist_expiry.pop(avatar_id, None)
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
                self.blacklist_expiry.pop(avatar_id, None)
                print(f"\n✅ Пользователь {avatar_id} удален из черного списка")
                return True
            return False

antispam = AntispamTracker()

dp = Dispatcher()

def extract_sticker_map(mods) -> dict:
    mods = mods or {}
    result = {}

    for k, v in mods.items():
        if k.startswith("sticker_") and hasattr(v, "intValue") and v.intValue > 0:
            result[k] = v.intValue

    return result

def count_stickers(mods) -> int:
    return len(extract_sticker_map(mods))

def extract_single_sticker_id(mods):
    sticker_map = extract_sticker_map(mods)
    if len(sticker_map) != 1:
        return None
    return next(iter(sticker_map.values()))

def extract_sticker_counts(mods) -> dict:
    sticker_map = extract_sticker_map(mods)
    counts = defaultdict(int)

    for sticker_id in sticker_map.values():
        counts[sticker_id] += 1

    return dict(counts)

def has_any_sticker(mods) -> bool:
    return len(extract_sticker_map(mods)) > 0

def get_pattern_from_mods(mods):
    mods = mods or {}
    p_obj = mods.get("pattern")
    if p_obj and hasattr(p_obj, "intValue"):
        return p_obj.intValue
    return None

async def check_command_access(message: types.Message) -> bool:
    if message.chat.id != tg_group or message.message_thread_id != topic_id:
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

        stat_message = (
            f"<blockquote>💰 Баланс: <b>{round(balance, 2)} G</b></blockquote>\n\n"
            f"<blockquote>📦 Всего скинов на продаже: <b>{len(open_requests)}</b></blockquote>"
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
        await message.answer(
            f"❌ <b>Ошибка получения статуса:</b>\n<code>{e}</code>",
            parse_mode="HTML"
        )

async def send_sale_notification(
    skin_name,
    sale_price,
    net_profit,
    session_profit,
    pattern,
    sticker_status,
    balance
):
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
        f"<blockquote>💵 profit sale: {round(net_profit, 2)} G\n"
        f"📈 profit session: {round(session_profit, 2)} G\n"
        f"Balance: {round(balance, 2)} G</blockquote>"
    )

    full_message = message_part1 + message_part2 + message_part3

    try:
        await bot.send_message(
            chat_id=tg_group,
            text=full_message,
            message_thread_id=topic_id
        )
    except Exception:
        pass

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

async def get_sticker_market_offers(client, sticker_id: int, lotov: int = 20):
    all_requests = []
    seen_ids = set()
    page = 0
    chunk_size = 100

    while len(all_requests) < lotov:
        size = min(chunk_size, lotov - len(all_requests))

        request = GetTradeOpenSaleRequestsRequest(
            id=sticker_id,
            page=page,
            size=size
        )

        try:
            response = client.raw.MarketplaceRemoteService.getFilteredTradeOpenSaleRequestsResponse(
                await client.send_request(
                    *client.raw.MarketplaceRemoteService.getFilteredTradeOpenSaleRequestsRequest(request)
                )
            )
        except Exception:
            return all_requests

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

async def buy_sticker_from_market(client, sticker_id: int, max_price: float):
    offers = await get_sticker_market_offers(client, sticker_id, lotov=20)

    if not offers:
        return False

    offers = sorted(offers, key=lambda x: x.price)

    for offer in offers:
        price = round(offer.price, 2)

        if price > max_price:
            return False

        ok = await buy_skin_by_id(client, offer.id)
        if ok:
            return True

    return False

async def attach_sticker(client, sticker_item_id: str, skin_item_id: str, slot_name: str):
    request = MountInventoryItemRequest(
        consumedItemId=sticker_item_id,
        modifiedItemId=skin_item_id,
        modificationName=slot_name
    )

    try:
        response = client.raw.InventoryRemoteService.mountInventoryItemEncryptedResponse(
            await client.send_request(
                *client.raw.InventoryRemoteService.mountInventoryItemEncryptedRequest(
                    request,
                    client.cipher
                )
            ),
            client.cipher
        )
        return response
    except Exception:
        return None

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

async def find_sticker_in_inventory(client, sticker_id: int):
    items = await get_inventory_items(client)

    for item in items:
        if item.itemDefinitionId == sticker_id:
            return item.id

    return None

async def wait_for_sticker_in_inventory(client, sticker_id: int, timeout: float = 5.0, delay: float = 0.25):
    deadline = time.time() + timeout

    while time.time() < deadline:
        item_id = await find_sticker_in_inventory(client, sticker_id)
        if item_id:
            return item_id
        await asyncio.sleep(delay)

    return None

async def find_skin_in_inventory_for_stickers(client, skin_id, pattern, target_stickers: dict):
    items = await get_inventory_items(client)

    exact_match = None
    empty_match = None

    for item in items:
        if item.itemDefinitionId != skin_id:
            continue

        mods = getattr(item, "modifications", {}) or {}
        current_pattern = get_pattern_from_mods(mods)

        if current_pattern != pattern:
            continue

        current_stickers = extract_sticker_map(mods)

        if current_stickers == target_stickers and target_stickers:
            exact_match = item.id
            break

        if not current_stickers:
            empty_match = item.id

    if exact_match:
        return "exact", exact_match

    if empty_match:
        return "empty", empty_match

    return None, None

async def wait_for_skin_in_inventory_for_stickers(client, skin_id, pattern, target_stickers: dict,
                                                  timeout: float = 5.0, delay: float = 0.25):
    deadline = time.time() + timeout

    while time.time() < deadline:
        mode, item_id = await find_skin_in_inventory_for_stickers(client, skin_id, pattern, target_stickers)
        if item_id:
            return mode, item_id
        await asyncio.sleep(delay)

    return None, None

async def find_skin_in_open_requests_for_stickers(client, skin_id, pattern, target_stickers: dict):
    requests = await get_open_requests(client)

    exact_match = None
    empty_match = None

    for req in requests:
        if req.itemDefinitionId != skin_id:
            continue

        mods = getattr(req, "modifications", {}) or {}
        current_pattern = get_pattern_from_mods(mods)

        if current_pattern != pattern:
            continue

        current_stickers = extract_sticker_map(mods)

        if current_stickers == target_stickers and target_stickers:
            exact_match = (req.id, req.price)
            break

        if not current_stickers:
            empty_match = (req.id, req.price)

    if exact_match:
        return "exact", exact_match[0], exact_match[1]

    if empty_match:
        return "empty", empty_match[0], empty_match[1]

    return None, None, None

async def ensure_stickers_on_skin(client, skin_item_id: str, target_stickers: dict, max_sticker_buy_price: float = 12.0):
    current_skin_item_id = skin_item_id

    async with crafting_lock:
        for slot_name, sticker_definition_id in target_stickers.items():
            sticker_item_id = await find_sticker_in_inventory(client, sticker_definition_id)

            if not sticker_item_id:
                bought = await buy_sticker_from_market(
                    client,
                    sticker_definition_id,
                    max_sticker_buy_price
                )

                if not bought:
                    return False, None

                sticker_item_id = await wait_for_sticker_in_inventory(
                    client,
                    sticker_definition_id,
                    timeout=5.0
                )

                if not sticker_item_id:
                    return False, None

            attach_result = await attach_sticker(client, sticker_item_id, current_skin_item_id, slot_name)

            if not attach_result:
                return False, None

            try:
                if hasattr(attach_result, "modifiedItem") and attach_result.modifiedItem:
                    current_skin_item_id = attach_result.modifiedItem.id
            except Exception:
                pass

            await asyncio.sleep(0.25)

    return True, current_skin_item_id

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
            skin_name = cfg["name"]

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

                has_sticker = has_any_sticker(mods)
                if has_sticker:
                    continue

                pattern_val = get_pattern_from_mods(mods)

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
    global is_paused

    last_event_time = time.time()

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
    global is_paused

    last_event_time = time.time()

    if is_paused:
        return

    await ready_event.wait()

    try:
        await process_closed_trade(update)
    except Exception:
        pass

async def restart_client():
    global client, my_name, my_avatar_id

    try:
        await client.stop()
    except Exception:
        pass

    await asyncio.sleep(2)

    client = StandClient(
        "YOUR_STAND_TOKEN",
        host="YOUR_HOST"
    )

    register_handlers()

    await client.start()

    my_name = await get_name(client)
    my_avatar_id = await get_my_avatar_id(client)

    for skin_id in SKINS_CONFIG:
        await client.subscribe_trade(skin_id)

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

async def find_exact_skin_in_inventory(client, skin_id, pattern, target_stickers: dict):
    items = await get_inventory_items(client)

    for item in items:
        if item.itemDefinitionId != skin_id:
            continue

        mods = getattr(item, "modifications", {}) or {}
        current_pattern = get_pattern_from_mods(mods)

        if current_pattern != pattern:
            continue

        current_stickers = extract_sticker_map(mods)

        if current_stickers == target_stickers:
            return item.id

    return None

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
    min_price = cfg["min_price"]
    max_price = cfg["max_price"]
    max_sticker_buy_price = cfg.get("max_sticker_buy_price", 12.0)

    target_price = round(req.price, 2)

    if target_price < min_price or target_price > max_price:
        return

    mods = getattr(req, "modifications", {}) or {}
    pattern = get_pattern_from_mods(mods)

    if pattern is None:
        return

    target_stickers = extract_sticker_map(mods)
    stickers_count = len(target_stickers)

    if stickers_count == 0:
        return

    if stickers_count != 1:
        return

    print(f"\n🔔 Стикерный скин выставлен: {skin_name} | цена: {target_price} G | паттерн: {pattern} | стикер: {target_stickers} | продавец: {req.creator.name}")

    mode, request_id, current_price = await find_skin_in_open_requests_for_stickers(
        client,
        skin_id,
        pattern,
        target_stickers
    )

    if request_id:
        cancelled = await cancel_sale_request(client, request_id)

        if not cancelled:
            return

        await asyncio.sleep(0.6)

        inv_mode, item_id = await find_skin_in_inventory_for_stickers(
            client,
            skin_id,
            pattern,
            target_stickers
        )

        if not item_id:
            inv_mode, item_id = await find_skin_in_inventory_for_stickers(
                client,
                skin_id,
                pattern,
                {}
            )

        if not item_id:
            return

        if inv_mode == "empty":
            ok, item_id = await ensure_stickers_on_skin(
                client,
                item_id,
                target_stickers,
                max_sticker_buy_price=max_sticker_buy_price
            )

            if not ok or not item_id:
                return

            await asyncio.sleep(0.4)

        exact_item_id = await find_exact_skin_in_inventory(
            client,
            skin_id,
            pattern,
            target_stickers
        )
        if exact_item_id:
            item_id = exact_item_id

        result = await create_sale_request(client, item_id, target_price)

        if result:
            await antispam.add_action(req.creator.avatarId, "repriced_sticker_skin")

        return

    inv_mode, item_id = await find_skin_in_inventory_for_stickers(
        client,
        skin_id,
        pattern,
        target_stickers
    )

    if item_id:
        if inv_mode == "empty":
            ok, item_id = await ensure_stickers_on_skin(
                client,
                item_id,
                target_stickers,
                max_sticker_buy_price=max_sticker_buy_price
            )

            if not ok or not item_id:
                return

            await asyncio.sleep(0.4)

        exact_item_id = await find_exact_skin_in_inventory(
            client,
            skin_id,
            pattern,
            target_stickers
        )
        if exact_item_id:
            item_id = exact_item_id

        result = await create_sale_request(client, item_id, target_price)

        if result:
            await antispam.add_action(req.creator.avatarId, "inventory_sticker_listing")

        return

    all_market_offers = await find_all_skins_on_market(skin_id, pattern)

    if not all_market_offers:
        return

    bought_skin_price = None
    bought = False

    for i, market_offer in enumerate(all_market_offers, 1):
        market_price = market_offer["price"]
        market_id = market_offer["id"]

        if market_price > buy_limit:
            continue

        if await buy_skin_by_id(client, market_id):
            bought_skin_price = market_price
            bought = True
            break

    if not bought:
        return

    await asyncio.sleep(1.0)

    empty_mode, empty_item_id = await find_skin_in_inventory_for_stickers(
        client,
        skin_id,
        pattern,
        {}
    )

    if not empty_item_id:
        return

    ok, empty_item_id = await ensure_stickers_on_skin(
        client,
        empty_item_id,
        target_stickers,
        max_sticker_buy_price=max_sticker_buy_price
    )

    if not ok or not empty_item_id:
        return

    await asyncio.sleep(0.5)

    exact_item_id = await find_exact_skin_in_inventory(
        client,
        skin_id,
        pattern,
        target_stickers
    )
    if exact_item_id:
        empty_item_id = exact_item_id

    result = await create_sale_request(client, empty_item_id, target_price)

    if result:
        await antispam.add_action(req.creator.avatarId, "buy_skin_sticker_and_list")

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
    pattern = get_pattern_from_mods(mods) or "N/A"
    sticker_status = "со стикерами" if has_any_sticker(mods) else "без стикеров"

    async with profit_lock:
        session_profit += net_profit

        sold_skins.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "skin": skin_name,
            "price": sale_price,
            "net_profit": net_profit,
            "pattern": pattern,
            "has_sticker": has_any_sticker(mods)
        })

        current_balance = await get_balance(client)

    await send_sale_notification(
        skin_name=skin_name,
        sale_price=sale_price,
        net_profit=net_profit,
        session_profit=session_profit,
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
    global my_name, my_avatar_id

    await client.start()

    my_name = await get_name(client)
    my_avatar_id = await get_my_avatar_id(client)

    for skin_id in SKINS_CONFIG.keys():
        await client.subscribe_trade(skin_id)

    telegram_task = asyncio.create_task(dp.start_polling(bot))
    parser_task = asyncio.create_task(periodic_market_parser())
    reconnect_task = asyncio.create_task(reconnect_watcher())

    await ready_event.wait()

    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        parser_task.cancel()
        telegram_task.cancel()
        reconnect_task.cancel()

        try:
            await parser_task
        except asyncio.CancelledError:
            pass

        try:
            await telegram_task
        except asyncio.CancelledError:
            pass

        try:
            await reconnect_task
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