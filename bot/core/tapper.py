import asyncio
import heapq
from random import randint
from time import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp_proxy import ProxyConnector
from pyrogram import Client

from bot.api.combo import claim_daily_combo, get_combo_cards
from bot.api.telegram import get_me_telegram
from bot.config import settings
from bot.utils.logger import logger
from bot.exceptions import InvalidSession

from bot.api.auth import login
from bot.api.clicker import (
    apply_boost,
    get_profile_data,
    get_upgrades,
    buy_upgrade,
    get_boosts,
    claim_daily_cipher,
    send_taps,
    get_config,
)
from bot.api.exchange import select_exchange
from bot.api.tasks import get_nuxt_builds, get_tasks, get_daily
from bot.utils.scripts import decode_cipher, get_headers
from bot.utils.tg_web_data import get_tg_web_data
from bot.utils.proxy import check_proxy


class Tapper:
    def __init__(self, tg_client: Client):
        self.session_name = tg_client.name
        self.tg_client = tg_client

    async def run(self, proxy: str | None) -> None:
        access_token_created_time = 0
        turbo_time = 0
        active_turbo = False

        headers = get_headers(name=self.tg_client.name)

        proxy_conn = ProxyConnector().from_url(proxy) if proxy else None
        http_client = aiohttp.ClientSession(
            headers=headers, connector=proxy_conn
        )

        if proxy:
            await check_proxy(
                http_client=http_client,
                proxy=proxy,
                session_name=self.session_name,
            )

        tg_web_data = await get_tg_web_data(
            tg_client=self.tg_client,
            proxy=proxy,
            session_name=self.session_name,
        )

        while True:
            try:
                if http_client.closed:
                    if proxy_conn:
                        if not proxy_conn.closed:
                            proxy_conn.close()

                    proxy_conn = (
                        ProxyConnector().from_url(proxy) if proxy else None
                    )
                    http_client = aiohttp.ClientSession(
                        headers=headers, connector=proxy_conn
                    )

                if time() - access_token_created_time >= 3600:
                    await get_nuxt_builds(http_client=http_client)

                    access_token = await login(
                        http_client=http_client,
                        tg_web_data=tg_web_data,
                        session_name=self.session_name,
                    )

                    if not access_token:
                        continue

                    http_client.headers[
                        'Authorization'
                    ] = f'Bearer {access_token}'

                    access_token_created_time = time()

                    await get_me_telegram(http_client=http_client)
                    game_config = await get_config(http_client=http_client)

                    profile_data = await get_profile_data(
                        http_client=http_client
                    )

                    last_passive_earn = profile_data['lastPassiveEarn']
                    earn_on_hour = profile_data['earnPassivePerHour']

                    # Форматируем числа как целые с разделением тысяч через точку
                    last_passive_earn_str = f"{int(last_passive_earn):,}".replace(',', '.')
                    earn_on_hour_str = f"{int(earn_on_hour):,}".replace(',', '.')

                    logger.info(f"{self.session_name} | Последний пассивный доход: <g>+{last_passive_earn_str}</g> | Доход каждый час: <y>{earn_on_hour_str}</y>")

                    available_energy = profile_data.get('availableTaps', 0)
                    balance = int(profile_data.get('balanceCoins', 0))

                    upgrades_data = await get_upgrades(http_client=http_client)

                    upgrades = upgrades_data['upgradesForBuy']
                    daily_combo = upgrades_data.get('dailyCombo')
                    if daily_combo:
                        bonus = daily_combo['bonusCoins']
                        is_claimed = daily_combo['isClaimed']
                        upgraded_list = daily_combo['upgradeIds']

                        if not is_claimed:
                            combo_cards = await get_combo_cards(
                                http_client=http_client
                            )

                            cards = combo_cards['combo']
                            date = combo_cards['date']

                            available_combo_cards = [
                                data for data in upgrades
                                if data['isAvailable'] is True
                                and data['id'] in cards
                                and data['id'] not in upgraded_list
                                and data['isExpired'] is False
                                and data.get('cooldownSeconds', 0) == 0
                                and data.get('maxLevel', data['level']) >= data['level']
                                and (
                                    data.get('condition') is None
                                    or data['condition'].get('_type') != 'SubscribeTelegramChannel'
                                )
                            ]

                            start_bonus_round = datetime.strptime(date, "%d-%m-%y").replace(hour=15)
                            end_bonus_round = start_bonus_round + timedelta(days=1)

                            if start_bonus_round <= datetime.now() < end_bonus_round:
                                common_price = sum([upgrade['price'] for upgrade in available_combo_cards])
                                need_cards_count = len(cards)
                                possible_cards_count = len(available_combo_cards)
                                is_combo_accessible = need_cards_count == possible_cards_count

                                if not is_combo_accessible:
                                    logger.info(f"{self.session_name} | "
                                                f"<r>Ежедневное комбо не применимо</r>, вы можете купить только {possible_cards_count} из {need_cards_count} карт!")

                                if balance < common_price:
                                    logger.info(f"{self.session_name} | "
                                                f"<r>Ежедневное комбо не применимо</r>, у вас недостаточно монет. Нужно <y>{common_price:,}</y> монет, а у вас всего <r>{balance:,}</r> монет!")

                                if common_price < bonus and balance > common_price and is_combo_accessible:
                                    for upgrade in available_combo_cards:
                                        upgrade_id = upgrade['id']
                                        level = upgrade['level']
                                        price = upgrade['price']
                                        profit = upgrade['profitPerHourDelta']

                                        logger.info(f"{self.session_name} | "
                                                    f"Спим 5 сек. перед покупкой <r>комбо</r> карт <e>{upgrade_id}</e>")

                                        await asyncio.sleep(delay=5)

                                        status, upgrades = await buy_upgrade(
                                            http_client=http_client,
                                            upgrade_id=upgrade_id,
                                        )

                                        if status is True:
                                            earn_on_hour += profit
                                            balance -= price                                                                            
                                            price_str = f"{price:,}".replace(',', '.')
                                            earn_on_hour_str = f"{earn_on_hour:,}".replace(',', '.')
                                            balance_str = f"{balance:,}".replace(',', '.')
                                            profit_str = f"{profit:,}".replace(',', '.')
                                    
                                            logger.success(
                                                    f"{self.session_name} | "
                                                    f"Успешно улучшено <e>{upgrade_id}</e> с ценой <r>{price_str}</r> до <m>{level}</m> уровня | "
                                                    f"Оставшиеся деньги: <e>{balance_str}</e>")


                                            await asyncio.sleep(delay=1)

                                            await asyncio.sleep(delay=2)

                                    status = await claim_daily_combo(
                                        http_client=http_client
                                    )
                                    if status is True:
                                        logger.success(f"{self.session_name} | Успешно собрано ежедневное комбо | "
                                                       f"Бонус: <g>+{bonus:,}</g>")

                    tasks = await get_tasks(http_client=http_client)

                    daily_task = tasks[-1]
                    rewards = daily_task['rewardsByDays']
                    is_completed = daily_task['isCompleted']
                    days = daily_task['days']

                    await asyncio.sleep(delay=2)

                    if is_completed is False:
                        status = await get_daily(http_client=http_client)
                        if status is True:
                            logger.success(f"{self.session_name} | Успешно получено ежедневная награда | Дни: <m>{days}</m> | Награда в монетах: {rewards[days - 1]['rewardCoins']}")

                    await asyncio.sleep(delay=2)

                    daily_cipher = game_config.get('dailyCipher')
                    if daily_cipher:
                        cipher = daily_cipher['cipher']
                        bonus = daily_cipher['bonusCoins']
                        is_claimed = daily_cipher['isClaimed']

                        if not is_claimed and cipher:
                            decoded_cipher = decode_cipher(cipher=cipher)

                            status = await claim_daily_cipher(
                                http_client=http_client, cipher=decoded_cipher
                            )
                            if status is True:
                                logger.success(f"{self.session_name} | "
                                               f"Успешно собран ежедневный шифр: <y>{decoded_cipher}</y> | "
                                               f"Бонус: <g>+{bonus:,}</g>")

                        await asyncio.sleep(delay=2)

                    exchange_id = profile_data.get('exchangeId')
                    if not exchange_id:
                        status = await select_exchange(
                            http_client=http_client, exchange_id='bybit'
                        )
                        if status is True:
                            logger.success(f"{self.session_name} | Успешно выбрана биржа <y>Bybit</y>")

                taps = randint(
                    a=settings.RANDOM_TAPS_COUNT[0],
                    b=settings.RANDOM_TAPS_COUNT[1],
                )

                if active_turbo:
                    taps += settings.ADD_TAPS_ON_TURBO
                    if time() - turbo_time > 20:
                        active_turbo = False
                        turbo_time = 0

                player_data = await send_taps(
                    http_client=http_client,
                    available_energy=available_energy,
                    taps=taps,
                )

                if not player_data:
                    continue

                available_energy = player_data.get('availableTaps', 0)
                new_balance = int(player_data.get('balanceCoins', 0))
                calc_taps = new_balance - balance
                balance = new_balance
                total = int(player_data.get('totalCoins', 0))
                earn_on_hour = player_data['earnPassivePerHour']

                balance_str = f"{balance:,}".replace(',', '.')
                calc_taps_str = f"{calc_taps:,}".replace(',', '.')
                total_str = f"{total:,}".replace(',', '.')
                # Activate buying upgrades on new accounts
                if earn_on_hour == 0:
                    earn_on_hour = 100

                logger.success(f"{self.session_name} | Успешный тап! | Баланс: <c>{balance_str}</c> (<g>+{calc_taps_str}</g>) | Всего: <e>{total_str}</e>")

                if active_turbo is False:
                    if settings.AUTO_UPGRADE is True:
                        failed_attempts = 0
                        for _ in range(settings.UPGRADES_COUNT):
                            available_upgrades = [
                                data
                                for data in upgrades
                                if data['isAvailable'] is True
                                   and data['isExpired'] is False
                                   and data.get('cooldownSeconds', 0) == 0
                                   and data.get('maxLevel', data['level'])
                                   >= data['level']
                                   and (
                                           data.get('condition') is None
                                           or data['condition'].get('_type')
                                           != 'SubscribeTelegramChannel'
                                   )
                            ]

                            queue = []

                            for upgrade in available_upgrades:
                                upgrade_id = upgrade['id']
                                level = upgrade['level']
                                price = upgrade['price']
                                profit = upgrade['profitPerHourDelta']

                                significance = profit / max(price, 1)

                                free_money = balance - settings.BALANCE_TO_SAVE
                                max_price_limit = earn_on_hour * 5

                                if (
                                        (free_money * 0.7) >= price
                                        and level <= settings.MAX_LEVEL
                                        and profit > 0
                                        and price < settings.MAX_UPGRADE_PRICE
                                        and price < max_price_limit):
                                    heapq.heappush(queue, (-significance, upgrade_id, upgrade))

                            if not queue:
                                continue

                            top_card = heapq.nsmallest(1, queue)[0]

                            upgrade = top_card[2]

                            upgrade_id = upgrade['id']
                            level = upgrade['level']
                            price = upgrade['price']
                            profit = upgrade['profitPerHourDelta']

                            logger.info(f"{self.session_name} | Спим 5 секунд перед улучшением <e>{upgrade_id}</e>")
                            await asyncio.sleep(delay=5)

                            status, upgrades = await buy_upgrade(
                                http_client=http_client, upgrade_id=upgrade_id
                            )

                            if status is True:
                                earn_on_hour += profit
                                balance -= price
                                
                                price_str = f"{price:,}".replace(',', '.')
                                earn_on_hour_str = f"{earn_on_hour:,}".replace(',', '.')
                                balance_str = f"{balance:,}".replace(',', '.')
                                profit_str = f"{profit:,}".replace(',', '.')
                                    
                                logger.success(
                                        f"{self.session_name} | "
                                        f"Успешно улучшено <e>{upgrade_id}</e> с ценой <r>{price_str}</r> до <m>{level}</m> уровня | "
                                        f"Оставшиеся деньги: <e>{balance_str}</e>")
                                await asyncio.sleep(delay=1)
                            else:
                                failed_attempts += 1
                                if failed_attempts >= len(available_upgrades):
                                    continue

                    if available_energy < settings.MIN_AVAILABLE_ENERGY:
                        boosts = await get_boosts(http_client=http_client)
                        energy_boost = next(
                            (
                                boost
                                for boost in boosts
                                if boost['id'] == 'BoostFullAvailableTaps'
                            ),
                            {},
                        )

                        if (
                                settings.APPLY_DAILY_ENERGY is True
                                and energy_boost.get('cooldownSeconds', 0) == 0
                                and energy_boost.get('level', 0)
                                <= energy_boost.get('maxLevel', 0)
                        ):
                            logger.info(
                                f'{self.session_name} | Спим 5 сек. перед применением буста энергии'
                            )
                            await asyncio.sleep(delay=5)

                            status = await apply_boost(
                                http_client=http_client,
                                boost_id='BoostFullAvailableTaps',
                            )
                            if status is True:
                                logger.success(f"{self.session_name} | Успешно применён буст энергии")

                                await asyncio.sleep(delay=1)

                                continue

                        await http_client.close()
                        if proxy_conn:
                            if not proxy_conn.closed:
                                proxy_conn.close()

                        random_sleep = randint(
                            settings.SLEEP_BY_MIN_ENERGY[0],
                            settings.SLEEP_BY_MIN_ENERGY[1],
                        )

                        logger.info(
                            f'{self.session_name} | Минимальная энергия достигнута: <y>{available_energy}</y>'
                        )
                        logger.info(
                            f'{self.session_name} | Спим {random_sleep:,} сек.'
                        )

                        await asyncio.sleep(delay=random_sleep)

                        access_token_created_time = 0

            except InvalidSession as error:
                raise error

            except Exception as error:
                logger.error(f"{self.session_name} | Неизвестная ошибка: {error}")
                await asyncio.sleep(delay=3)

            else:
                sleep_between_clicks = randint(
                    a=settings.SLEEP_BETWEEN_TAP[0],
                    b=settings.SLEEP_BETWEEN_TAP[1],
                )

                if active_turbo is True:
                    sleep_between_clicks = 4

                logger.info(f'Sleep {sleep_between_clicks}s')
                await asyncio.sleep(delay=sleep_between_clicks)


async def run_tapper(tg_client: Client, proxy: str | None):
    try:
        await Tapper(tg_client=tg_client).run(proxy=proxy)
    except InvalidSession:
        logger.error(f"{tg_client.name} | Неправильная сессия")
