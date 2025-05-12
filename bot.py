import asyncio
import logging
import pickle
import os
import threading
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.client.bot import DefaultBotProperties
from steam.client import SteamClient
from steam.enums import EResult

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    filename="bot.log",
    filemode="a",
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "BOT_TOKEN"
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# Машина состояний
class AuthStates(StatesGroup):
    username = State()
    password = State()
    steam_guard = State()
    steam_2fa = State()


# Хранилище данных на пользователя
user_sessions = {}
processed_messages = set()


def save_session(client: SteamClient, user_id: int):
    """Сохраняет куки сессии в файл."""
    try:
        with open(f"session_{user_id}.pkl", "wb") as f:
            pickle.dump(client._session, f)
        logger.info(f"Session saved for {user_id}")
    except Exception as e:
        logger.error(f"Error saving session for {user_id}: {str(e)}")


def load_session(client: SteamClient, user_id: int):
    """Загружает куки сессии из файла."""
    session_file = f"session_{user_id}.pkl"
    try:
        if not os.path.exists(session_file) or os.path.getsize(session_file) == 0:
            logger.warning(f"Session file {session_file} is missing or empty")
            return False
        with open(session_file, "rb") as f:
            client._session = pickle.load(f)
        logger.info(f"Session loaded for {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error loading session for {user_id}: {str(e)}")
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                logger.info(f"Removed corrupted session file {session_file}")
            except Exception as rm_e:
                logger.error(f"Failed to remove session file {session_file}: {str(rm_e)}")
        return False


async def try_relogin(client: SteamClient, username: str, password: str, user_id: int, max_attempts: int = 5) -> bool:
    """Пытается восстановить сессию через relogin или login с несколькими попытками."""
    for attempt in range(max_attempts):
        try:
            if load_session(client, user_id):
                result = client.relogin()
            else:
                result = client.login(username, password)
            logger.info(f"Relogin attempt {attempt + 1} for {username}: {result}")
            if result == EResult.OK:
                save_session(client, user_id)
                if session := user_sessions.get(user_id):
                    if "farming_games" in session and session["farming_games"]:
                        client.games_played(session["farming_games"])
                        logger.info(f"Restored farming for {user_id}: {session['farming_games']}")
                threading.Thread(target=run_client_forever, args=(client, user_id), daemon=True).start()
                return True
            elif result in (EResult.AccountLogonDenied, EResult.AccountLoginDeniedNeedTwoFactor):
                logger.info(f"Relogin for {username} requires 2FA or Steam Guard")
                return False
            elif result == EResult.TryAnotherCM:
                logger.warning(f"TryAnotherCM for {username}, switching CM server")
                client.disconnect()
                await asyncio.sleep(10)
                continue
            else:
                logger.error(f"Relogin failed for {username}: {result.name}")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error during relogin for {user_id} on attempt {attempt + 1}: {str(e)}")
            if "429" in str(e):
                await asyncio.sleep(60)
            elif "Ran out of input" in str(e):
                logger.warning(f"Session file corrupted for {user_id}, attempting fresh login")
                result = client.login(username, password)
                if result == EResult.OK:
                    save_session(client, user_id)
                    if session := user_sessions.get(user_id):
                        if "farming_games" in session and session["farming_games"]:
                            client.games_played(session["farming_games"])
                            logger.info(f"Restored farming for {user_id}: {session['farming_games']}")
                    threading.Thread(target=run_client_forever, args=(client, user_id), daemon=True).start()
                    return True
                else:
                    logger.error(f"Fresh login failed for {username}: {result.name}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(10)
    return False


def run_client_forever(client: SteamClient, user_id: int):
    """Запускает бесконечный цикл обработки событий SteamClient в отдельном потоке."""
    try:
        logger.info(f"Starting run_forever for user {user_id}")
        client.run_forever()
        logger.info(f"SteamClient run_forever stopped for {user_id}")
    except Exception as e:
        logger.error(f"Error in SteamClient run_forever for {user_id}: {str(e)}")
        # Попытка переподключения
        if user_id in user_sessions:
            session = user_sessions[user_id]
            if session.get("password"):
                logger.info(f"Attempting to reconnect for user {user_id}")
                asyncio.run_coroutine_threadsafe(
                    try_relogin(client, session["username"], session["password"], user_id),
                    asyncio.get_event_loop()
                )


async def get_game_name(client: SteamClient, app_id: int) -> str:
    """Получает название игры по AppID."""
    try:
        await asyncio.sleep(1)
        info = client.get_product_info([app_id])
        return info["apps"][app_id]["common"]["name"] if info and "apps" in info and app_id in info["apps"] else str(
            app_id)
    except Exception as e:
        logger.error(f"Error getting game name for AppID {app_id}: {str(e)}")
        return str(app_id)


async def is_session_valid(client: SteamClient, username: str, password: str, user_id: int) -> bool:
    """Проверяет валидность сессии без вызова выхода."""
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            if not client.connected:
                logger.info(f"Client not connected for {user_id}, attempting reconnect (attempt {attempt + 1})")
                client.reconnect()
                await asyncio.sleep(2)

            if not client.logged_on:
                logger.info(f"Session not logged on for {user_id}, attempting reconnect (attempt {attempt + 1})")
                if password:
                    logger.info(f"Reconnect failed for {user_id}, attempting login (attempt {attempt + 1})")
                    result = client.login(username, password)
                    if result == EResult.OK:
                        save_session(client, user_id)
                        session = user_sessions.get(user_id)
                        if session and "farming_games" in session and session["farming_games"]:
                            client.games_played(session["farming_games"])
                            logger.info(f"Restored farming for {user_id}: {session['farming_games']}")
                        threading.Thread(target=run_client_forever, args=(client, user_id), daemon=True).start()
                        return True
                    logger.warning(f"Login failed for {user_id}, result: {result}")
                elif not client.logged_on:
                    logger.info(f"Session still not logged on after reconnect for {user_id}, will retry")
            else:
                session = user_sessions.get(user_id)
                if session and "farming_games" in session and session["farming_games"]:
                    client.games_played(session["farming_games"])  # Поддерживаем фарминг
                    logger.debug(f"Farming games active for {user_id}: {session['farming_games']}")
                logger.info(f"Session valid (logged_on=True) for {user_id}")
                return True
        except Exception as e:
            logger.error(f"Session validation failed for {user_id} on attempt {attempt + 1}: {str(e)}")
            if "429" in str(e):
                logger.warning(f"Rate limit hit for {user_id}, pausing for 60 seconds")
                await asyncio.sleep(60)
            elif attempt < max_attempts - 1:
                await asyncio.sleep(10)
    logger.error(f"Session validation failed for {user_id} after {max_attempts} attempts")
    return False


@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if session and await is_session_valid(session["client"], session["username"], session.get("password"),
                                          message.from_user.id):
        await message.answer("Вы уже авторизованы! Используйте /logout или /start_farm.")
        return

    await message.answer("Введи логин Steam:")
    await state.set_state(AuthStates.username)


@dp.message(Command("logout"))
async def logout(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if session:
        try:
            session["client"].logout()
            session["client"].disconnect()
            logger.info(f"User {message.from_user.id} logged out")
        except Exception as e:
            logger.error(f"Error during logout for user {message.from_user.id}: {str(e)}")
        session["password"] = None
        session["farming_games"] = []
        if os.path.exists(f"session_{message.from_user.id}.pkl"):
            os.remove(f"session_{message.from_user.id}.pkl")
            logger.info(f"Session file removed for {message.from_user.id}")
    await message.answer("Вы вышли из аккаунта! Используйте /start для входа.")
    await state.clear()


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if session:
        session["password"] = None
        session["farming_games"] = []
        logger.info(f"Session cancelled for user {message.from_user.id}")
    await message.answer("Авторизация отменена. Начните заново с /start.")
    await state.clear()


@dp.message(Command("start_farm"))
async def start_farm(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if not session or not await is_session_valid(session["client"], session["username"], session.get("password"),
                                                 message.from_user.id):
        await message.answer("Вы не авторизованы. Попробуйте /start.")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("Укажите AppID игры, например: /start_farm 440")
        return

    try:
        app_ids = [int(app_id) for app_id in args[1].split(",")]
        if len(app_ids) > 32:
            await message.answer("Нельзя фармить более 32 игр!")
            return
    except ValueError:
        await message.answer("AppID должен быть числом или списком чисел, разделенных запятыми.")
        return

    client = session["client"]
    session["farming_games"] = app_ids
    try:
        client.games_played(app_ids)
        game_names = [await get_game_name(client, app_id) for app_id in app_ids]
        await message.answer(f"✅ Фарм начат: {', '.join(game_names)}")
        logger.info(f"Started farming for {message.from_user.id}: {app_ids}")
    except Exception as e:
        session["farming_games"] = []
        logger.error(f"Error starting farm for {message.from_user.id}: {str(e)}")
        await message.answer(f"❌ Ошибка: {str(e)}. Попробуйте снова.")


@dp.message(Command("stop_farm"))
async def stop_farm(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if not session or not await is_session_valid(session["client"], session["username"], session.get("password"),
                                                 message.from_user.id):
        await message.answer("Вы не авторизованы. /start.")
        return

    client = session["client"]
    if "farming_games" not in session or not session["farming_games"]:
        await message.answer("Фарм не запущен.")
        return

    try:
        client.games_played([])
        game_names = [await get_game_name(client, app_id) for app_id in session["farming_games"]]
        session["farming_games"] = []
        await message.answer(f"🛑 Фарм остановлен: {', '.join(game_names)}")
        logger.info(f"Stopped farming for {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error stopping farm for {message.from_user.id}: {str(e)}")
        await message.answer(f"❌ Ошибка: {str(e)}. Попробуйте снова.")


@dp.message(AuthStates.username)
async def get_username(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Введите корректный логин.")
        return
    await state.update_data(username=message.text)
    await message.answer("Введите пароль:")
    await state.set_state(AuthStates.password)


@dp.message(AuthStates.password)
async def get_password(message: Message, state: FSMContext):
    data = await state.get_data()
    username = data["username"]
    password = message.text

    await message.answer("⏳ Вхожу в аккаунт...")
    client = SteamClient()
    user_sessions[message.from_user.id] = {
        "client": client,
        "username": username,
        "password": password,
        "attempts": 0,
        "farming_games": [],
        "is_authenticating": True,
    }

    try:
        result = client.login(username, password)
        logger.info(f"Login attempt for {username}: {result}")
    except Exception as e:
        logger.error(f"Login error for {username}: {str(e)}")
        user_sessions[message.from_user.id]["is_authenticating"] = False
        await message.answer(f"❌ Ошибка: {str(e)}. Попробуйте /start.")
        await state.clear()
        return

    if result == EResult.AccountLogonDenied:
        await message.answer("📧 Введи код Steam Guard:")
        await state.set_state(AuthStates.steam_guard)
    elif result == EResult.AccountLoginDeniedNeedTwoFactor:
        await message.answer("📱 Введи код 2FA:")
        await state.set_state(AuthStates.steam_2fa)
    elif result == EResult.OK:
        user_sessions[message.from_user.id]["is_authenticating"] = False
        save_session(client, message.from_user.id)
        threading.Thread(target=run_client_forever, args=(client, message.from_user.id), daemon=True).start()
        await message.answer("✅ Вход успешен! Используйте /start_farm или /logout.")
        await state.clear()
    else:
        user_sessions[message.from_user.id]["is_authenticating"] = False
        await message.answer(f"❌ Ошибка: {result.name}. Попробуйте /start.")
        await state.clear()


@dp.message(AuthStates.steam_guard)
async def get_email_code(message: Message, state: FSMContext):
    session = user_sessions.get(message.from_user.id)
    if not session:
        await message.answer("Сессия истекла. Начните заново с /start.")
        await state.clear()
        return

    session["attempts"] = session.get("attempts", 0) + 1
    if session["attempts"] > 3:
        await message.answer("❌ Слишком много попыток. Начните заново с /start.")
        session["is_authenticating"] = False
        await state.clear()
        return

    client = session["client"]
    username = session["username"]
    password = session["password"]
    code = message.text

    try:
        result = client.login(username, password, auth_code=code)
        logger.info(f"Steam Guard attempt for {username}: {result}")
    except Exception as e:
        logger.error(f"Steam Guard error for {username}: {str(e)}")
        session["is_authenticating"] = False
        await message.answer(f"❌ Ошибка: {str(e)}. Попробуйте /start.")
        await state.clear()
        return

    if result == EResult.OK:
        session["is_authenticating"] = False
        save_session(client, message.from_user.id)
        threading.Thread(target=run_client_forever, args=(client, message.from_user.id), daemon=True).start()
        await message.answer("✅ Вход успешен! Используйте /start_farm или /logout.")
        await state.clear()
    elif result in (EResult.InvalidPassword, EResult.InvalidLoginAuthCode, EResult.AccountLogonDenied):
        await message.answer("❌ Неверный код Steam Guard. Попробуйте снова:")
    else:
        session["is_authenticating"] = False
        await message.answer(f"❌ Ошибка: {result.name}. Попробуйте /start.")
        await state.clear()


@dp.message(AuthStates.steam_2fa)
async def get_2fa_code(message: Message, state: FSMContext):
    session = user_sessions.get(message.from_user.id)
    if not session:
        await message.answer("Сессия истекла. Начните заново с /start.")
        await state.clear()
        return

    session["attempts"] = session.get("attempts", 0) + 1
    if session["attempts"] > 3:
        await message.answer("❌ Слишком много попыток. Начните заново с /start.")
        session["is_authenticating"] = False
        await state.clear()
        return

    client = session["client"]
    username = session["username"]
    password = session["password"]
    code = message.text

    try:
        result = client.login(username, password, two_factor_code=code)
        logger.info(f"2FA attempt for {username}: {result}")
    except Exception as e:
        logger.error(f"2FA error for {username}: {str(e)}")
        session["is_authenticating"] = False
        await message.answer(f"❌ Ошибка: {str(e)}. Попробуйте /start.")
        await state.clear()
        return

    if result == EResult.OK:
        session["is_authenticating"] = False
        save_session(client, message.from_user.id)
        threading.Thread(target=run_client_forever, args=(client, message.from_user.id), daemon=True).start()
        await message.answer("✅ Вход успешен! Используйте /start_farm или /logout.")
        await state.clear()
    elif result in (EResult.InvalidPassword, EResult.TwoFactorCodeMismatch, EResult.AccountLoginDeniedNeedTwoFactor):
        await message.answer("❌ Неверный код 2FA. Попробуйте снова:")
    else:
        session["is_authenticating"] = False
        await message.answer(f"❌ Ошибка: {result.name}. Попробуйте /start.")
        await state.clear()


async def check_online_status():
    """Цикл проверки статуса пользователей каждые 60 секунд."""
    last_notification = {}  # Для ограничения уведомлений
    while True:
        for user_id, session in list(user_sessions.items()):
            if session.get("is_authenticating", False):
                logger.debug(f"Skipping check for {user_id} due to ongoing authentication")
                continue
            client = session["client"]
            username = session["username"]
            password = session.get("password")
            try:
                if not client.connected or not client.logged_on:
                    logger.info(f"Session for {user_id} not connected/logged on, attempting reconnect")
                    if password:
                        logger.info(f"Attempting relogin for {user_id}")
                        if await try_relogin(client, username, password, user_id):
                            logger.info(f"Session restored for {user_id}")
                            if "farming_games" in session and session["farming_games"]:
                                client.games_played(session["farming_games"])
                                logger.info(f"Restored farming for {user_id}: {session['farming_games']}")
                        else:
                            logger.warning(f"Session for {user_id} requires 2FA/Steam Guard, user not notified")
                    else:
                        logger.warning(f"Session for {user_id} expired (no password)")
                        current_time = asyncio.get_event_loop().time()
                        if user_id not in last_notification or (current_time - last_notification[user_id]) > 3600:
                            try:
                                await bot.send_message(user_id, "⚠️ Сессия истекла. Войдите заново с /start.")
                                last_notification[user_id] = current_time
                            except Exception as e:
                                logger.error(f"Failed to notify {user_id}: {str(e)}")
                        user_sessions.pop(user_id, None)
                else:
                    logger.debug(f"Session for {user_id} is alive and connected")
            except Exception as e:
                logger.error(f"Error checking online status for {user_id}: {str(e)}")
                if "429" in str(e):
                    logger.warning(f"Rate limit hit for {user_id}, pausing checks")
                    await asyncio.sleep(60)
                else:
                    logger.warning(f"Non-critical error for {user_id}, will retry")
                    if "farming_games" in session and session["farming_games"]:
                        logger.info(f"Preserving session for {user_id} due to active farming")
                    else:
                        current_time = asyncio.get_event_loop().time()
                        if user_id not in last_notification or (current_time - last_notification[user_id]) > 3600:
                            try:
                                await bot.send_message(user_id,
                                                       "⚠️ Сессия истекла из-за ошибки. Войдите заново с /start.")
                                last_notification[user_id] = current_time
                            except Exception as e:
                                logger.error(f"Failed to notify {user_id}: {str(e)}")
                        user_sessions.pop(user_id, None)
        await asyncio.sleep(60)  # Проверка каждые 60 секунд


async def main():
    asyncio.create_task(check_online_status())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
