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
from steam.enums import EResult, EPersonaState

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
    try:
        with open(f"session_{user_id}.pkl", "rb") as f:
            client._session = pickle.load(f)
        logger.info(f"Session loaded for {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error loading session for {user_id}: {str(e)}")
        return False


def run_client_events(client: SteamClient, user_id: int):
    """Запускает обработку событий SteamClient в отдельном потоке."""
    try:
        client.run_forever()
        logger.info(f"SteamClient event loop stopped for {user_id}")
    except Exception as e:
        logger.error(f"Error in SteamClient event loop for {user_id}: {str(e)}")


async def try_relogin(client: SteamClient, username: str, password: str, user_id: int) -> bool:
    """Пытается восстановить сессию через relogin или login."""
    try:
        if load_session(client, user_id):
            result = client.relogin()
        else:
            result = client.login(username, password)
        logger.info(f"Relogin attempt for {username}: {result}")
        if result == EResult.OK:
            save_session(client, user_id)
            threading.Thread(target=run_client_events, args=(client, user_id), daemon=True).start()
            return True
        elif result in (EResult.AccountLogonDenied, EResult.AccountLoginDeniedNeedTwoFactor):
            logger.info(f"Relogin for {username} requires 2FA or Steam Guard")
            return False
        else:
            logger.error(f"Relogin failed for {username}: {result.name}")
            return False
    except Exception as e:
        logger.error(f"Error during relogin for {user_id}: {str(e)}")
        return False


async def load_user_data(client: SteamClient, user_id: int, max_attempts: int = 3) -> bool:
    """Пытается загрузить данные пользователя с повторными попытками."""
    for attempt in range(max_attempts):
        try:
            if client.steam_id:
                client.get_user(client.steam_id)
                logger.info(f"User data loaded for {user_id} on attempt {attempt + 1}")
                return True
            else:
                logger.warning(f"No steam_id available for {user_id} on attempt {attempt + 1}")
                return False
        except Exception as e:
            logger.error(f"Error loading user data for {user_id} on attempt {attempt + 1}: {str(e)}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(2)
    logger.error(f"Failed to load user data for {user_id} after {max_attempts} attempts")
    return False


@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if session and await is_session_valid(session["client"], session["username"], session.get("password"),
                                          message.from_user.id):
        await message.answer("Вы уже авторизованы! Используйте /status для проверки статуса или /logout для выхода.")
        return

    if session:
        user_sessions.pop(message.from_user.id, None)
        logger.info(f"Removed invalid session for user {message.from_user.id}")

    await message.answer("Привет! Введи логин от Steam:")
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
            logger.info(f"User {message.from_user.id} logged out")
        except Exception as e:
            logger.error(f"Error during logout for user {message.from_user.id}: {str(e)}")
        user_sessions.pop(message.from_user.id, None)
        if os.path.exists(f"session_{message.from_user.id}.pkl"):
            os.remove(f"session_{message.from_user.id}.pkl")
            logger.info(f"Session file removed for {message.from_user.id}")
    await message.answer("Вы успешно вышли из аккаунта!")
    await state.clear()


@dp.message(Command("status"))
async def status(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if not session or not await is_session_valid(session["client"], session["username"], session.get("password"),
                                                 message.from_user.id):
        if session:
            user_sessions.pop(message.from_user.id, None)
            logger.info(f"Removed invalid session for user {message.from_user.id}")
        await message.answer("Вы не авторизованы. Войдите с помощью /start.")
        return

    client = session["client"]
    # Пытаемся загрузить данные пользователя
    await load_user_data(client, message.from_user.id)
    await asyncio.sleep(1)  # Даем время на загрузку данных

    status_info = ["<b>📊 Статус аккаунта Steam</b>"]
    errors = []

    # Проверяем каждый атрибут отдельно
    try:
        steam_id = client.steam_id if client.steam_id else None
        status_info.append(f"<b>SteamID64:</b> {steam_id.id64 if steam_id else 'Недоступно'}")
        status_info.append(f"<b>Account ID:</b> {steam_id.account_id if steam_id else 'Недоступно'}")
        status_info.append(f"<b>Instance ID:</b> {steam_id.instance_id if steam_id else 'Недоступно'}")
        status_info.append(f"<b>Тип аккаунта:</b> {steam_id.type.name if steam_id else 'Недоступно'}")
        status_info.append(f"<b>Профиль:</b> {steam_id.community_url if steam_id else 'Недоступно'}")
        logger.info(f"SteamID data accessed for {message.from_user.id}")
    except Exception as e:
        errors.append(f"SteamID data: {str(e)}")
        logger.error(f"Error accessing SteamID data for {message.from_user.id}: {str(e)}")

    try:
        user_name = client.user.name if client.user and hasattr(client.user,
                                                                'name') and client.user.name else "Недоступно"
        status_info.append(f"<b>Имя:</b> {user_name}")
        logger.info(f"User name accessed for {message.from_user.id}")
    except Exception as e:
        errors.append(f"User name: {str(e)}")
        logger.error(f"Error accessing user name for {message.from_user.id}: {str(e)}")

    try:
        user_level = client.user.level if client.user and hasattr(client.user, 'level') else "Недоступно"
        status_info.append(f"<b>Уровень:</b> {user_level}")
        logger.info(f"User level accessed for {message.from_user.id}")
    except Exception as e:
        errors.append(f"User level: {str(e)}")
        logger.error(f"Error accessing user level for {message.from_user.id}: {str(e)}")

    try:
        friends_count = len(client.friends) if client.friends and hasattr(client.friends, '__len__') else 0
        status_info.append(f"<b>Друзей:</b> {friends_count}")
        logger.info(f"Friends count accessed for {message.from_user.id}")
    except Exception as e:
        errors.append(f"Friends count: {str(e)}")
        logger.error(f"Error accessing friends count for {message.from_user.id}: {str(e)}")

    try:
        user_state = client.user.state.name if client.user and hasattr(client.user,
                                                                       'state') and client.user.state else "Недоступно"
        status_info.append(f"<b>Статус:</b> {user_state}")
        logger.info(f"User state accessed for {message.from_user.id}")
    except Exception as e:
        errors.append(f"User state: {str(e)}")
        logger.error(f"Error accessing user state for {message.from_user.id}: {str(e)}")

    if errors:
        status_info.append(
            "<b>Ошибки:</b> Некоторые данные недоступны. Попробуйте /status еще раз или /force_reconnect.")
        logger.error(f"Status errors for {message.from_user.id}: {'; '.join(errors)}")
        await message.answer("\n".join(status_info))
    else:
        await message.answer("\n".join(status_info))
        logger.info(f"Status requested for {message.from_user.id}: success")


@dp.message(Command("relogin"))
async def relogin(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if not session:
        await message.answer("Вы не авторизованы. Войдите с помощью /start.")
        return

    client = session["client"]
    username = session["username"]
    password = session.get("password")

    if not password:
        await message.answer("Пароль не сохранен. Пожалуйста, войдите заново с /start.")
        user_sessions.pop(message.from_user.id, None)
        await state.clear()
        return

    await message.answer("⏳ Пытаюсь восстановить сессию...")
    try:
        if await try_relogin(client, username, password, message.from_user.id):
            await message.answer("✅ Сессия восстановлена! Используйте /status для проверки.")
        else:
            await message.answer("📧 Введи код из Email (Steam Guard) или код 2FA:")
            await state.set_state(AuthStates.steam_guard)
    except Exception as e:
        logger.error(f"Error during relogin for {message.from_user.id}: {str(e)}")
        user_sessions.pop(message.from_user.id, None)
        await message.answer("❌ Ошибка при восстановлении сессии. Войдите заново с /start.")
        await state.clear()


@dp.message(Command("force_reconnect"))
async def force_reconnect(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if not session:
        await message.answer("Вы не авторизованы. Войдите с помощью /start.")
        return

    client = session["client"]
    username = session["username"]
    password = session.get("password")

    await message.answer("⏳ Пытаюсь восстановить соединение...")
    try:
        client.reconnect()
        await asyncio.sleep(1)
        if client.logged_on:
            await load_user_data(client, message.from_user.id)
            await message.answer("✅ Соединение восстановлено! Используйте /status для проверки.")
            logger.info(f"Force reconnect successful for {message.from_user.id}")
        elif password and await try_relogin(client, username, password, message.from_user.id):
            await message.answer("✅ Сессия восстановлена через relogin! Используйте /status для проверки.")
        else:
            await message.answer("📧 Введи код из Email (Steam Guard) или код 2FA:")
            await state.set_state(AuthStates.steam_guard)
    except Exception as e:
        logger.error(f"Error during force reconnect for {message.from_user.id}: {str(e)}")
        await message.answer("❌ Ошибка при восстановлении соединения. Попробуйте /relogin или /start.")


@dp.message(Command("check_session"))
async def check_session(message: Message):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    session = user_sessions.get(message.from_user.id)
    if not session:
        await message.answer("Нет активной сессии.")
        return
    client = session["client"]
    is_valid = await is_session_valid(client, session["username"], session.get("password"), message.from_user.id)
    user_available = client.user is not None
    steam_id_available = client.steam_id is not None
    friends_available = client.friends is not None
    await message.answer(
        f"Сессия {'валидна' if is_valid else 'невалидна'}.\n"
        f"Logged on: {client.logged_on}\n"
        f"SteamID: {client.steam_id if steam_id_available else 'Недоступно'}\n"
        f"User data: {'Доступно' if user_available else 'Недоступно'}\n"
        f"Friends: {'Доступно' if friends_available else 'Недоступно'}"
    )
    logger.info(
        f"Session check for {message.from_user.id}: valid={is_valid}, logged_on={client.logged_on}, user={user_available}, steam_id={steam_id_available}, friends={friends_available}")


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    if message.message_id in processed_messages:
        return
    processed_messages.add(message.message_id)

    if message.from_user.id in user_sessions:
        user_sessions.pop(message.from_user.id, None)
        logger.info(f"Session cancelled for user {message.from_user.id}")
    await message.answer("Процесс авторизации отменен. Начните заново с /start.")
    await state.clear()


@dp.message(AuthStates.username)
async def get_username(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, введите корректный логин.")
        return
    await state.update_data(username=message.text)
    await message.answer("Теперь введи пароль:")
    await state.set_state(AuthStates.password)


@dp.message(AuthStates.password)
async def get_password(message: Message, state: FSMContext):
    data = await state.get_data()
    username = data["username"]
    password = message.text

    await message.answer("⏳ Пытаюсь войти в аккаунт...")

    client = SteamClient()
    user_sessions[message.from_user.id] = {"client": client, "username": username, "password": password, "attempts": 0}

    try:
        result = client.login(username, password)
        logger.info(f"Login attempt for {username}: {result}")
    except Exception as e:
        logger.error(f"Login error for {username}: {str(e)}")
        user_sessions.pop(message.from_user.id, None)
        await message.answer(f"❌ Ошибка входа: {str(e)}. Попробуйте снова с /start.")
        await state.clear()
        return

    if result == EResult.AccountLogonDenied:
        await message.answer("📧 Введи код из Email (Steam Guard):")
        await state.set_state(AuthStates.steam_guard)
    elif result == EResult.AccountLoginDeniedNeedTwoFactor:
        await message.answer("📱 Введи код из мобильного приложения Steam (2FA):")
        await state.set_state(AuthStates.steam_2fa)
    elif result == EResult.OK:
        save_session(client, message.from_user.id)
        threading.Thread(target=run_client_events, args=(client, message.from_user.id), daemon=True).start()
        await success_login(message, client, state)
        await state.clear()
    else:
        await message.answer(f"❌ Вход не выполнен: {result.name}. Попробуйте снова с /start.")
        user_sessions.pop(message.from_user.id, None)
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
        user_sessions.pop(message.from_user.id, None)
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
        user_sessions.pop(message.from_user.id, None)
        await message.answer(f"❌ Ошибка: {str(e)}. Попробуйте снова с /start.")
        await state.clear()
        return

    if result == EResult.OK:
        save_session(client, message.from_user.id)
        threading.Thread(target=run_client_events, args=(client, message.from_user.id), daemon=True).start()
        await success_login(message, client, state)
        await state.clear()
    elif result in (EResult.InvalidPassword, EResult.InvalidLoginAuthCode, EResult.AccountLogonDenied):
        await message.answer("❌ Неверный код Steam Guard. Попробуйте ввести код снова:")
    else:
        await message.answer(f"❌ Ошибка: {result.name}. Попробуйте снова с /start.")
        user_sessions.pop(message.from_user.id, None)
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
        user_sessions.pop(message.from_user.id, None)
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
        user_sessions.pop(message.from_user.id, None)
        await message.answer(f"❌ Ошибка: {str(e)}. Попробуйте снова с /start.")
        await state.clear()
        return

    if result == EResult.OK:
        save_session(client, message.from_user.id)
        threading.Thread(target=run_client_events, args=(client, message.from_user.id), daemon=True).start()
        await success_login(message, client, state)
        await state.clear()
    elif result in (EResult.InvalidPassword, EResult.TwoFactorCodeMismatch, EResult.AccountLoginDeniedNeedTwoFactor):
        await message.answer("❌ Неверный код 2FA. Попробуйте ввести код снова:")
    else:
        await message.answer(f"❌ Ошибка: {result.name}. Попробуйте снова с /start.")
        user_sessions.pop(message.from_user.id, None)
        await state.clear()


async def success_login(message: Message, client: SteamClient, state: FSMContext):
    try:
        # Пытаемся загрузить данные пользователя
        await load_user_data(client, message.from_user.id)
        await asyncio.sleep(1)  # Даем время на загрузку данных

        status_info = ["<b>✅ Успешный вход в Steam!</b>"]
        errors = []

        try:
            user_name = client.user.name if client.user and hasattr(client.user,
                                                                    'name') and client.user.name else "Недоступно"
            status_info.append(f"<b>Имя:</b> {user_name}")
            logger.info(f"User name accessed for {message.from_user.id}")
        except Exception as e:
            errors.append(f"User name: {str(e)}")
            logger.error(f"Error accessing user name for {message.from_user.id}: {str(e)}")

        try:
            profile_url = client.steam_id.community_url if client.steam_id else "Недоступно"
            status_info.append(f"<b>Профиль:</b> {profile_url}")
            logger.info(f"Profile URL accessed for {message.from_user.id}")
        except Exception as e:
            errors.append(f"Profile URL: {str(e)}")
            logger.error(f"Error accessing profile URL for {message.from_user.id}: {str(e)}")

        try:
            last_logon = client.user.last_logon if client.user and hasattr(client.user,
                                                                           'last_logon') and client.user.last_logon else "Недоступно"
            status_info.append(f"<b>Последний вход:</b> {last_logon}")
            logger.info(f"Last logon accessed for {message.from_user.id}")
        except Exception as e:
            errors.append(f"Last logon: {str(e)}")
            logger.error(f"Error accessing last logon for {message.from_user.id}: {str(e)}")

        try:
            friends_count = len(client.friends) if client.friends and hasattr(client.friends, '__len__') else 0
            status_info.append(f"<b>Друзей:</b> {friends_count}")
            logger.info(f"Friends count accessed for {message.from_user.id}")
        except Exception as e:
            errors.append(f"Friends count: {str(e)}")
            logger.error(f"Error accessing friends count for {message.from_user.id}: {str(e)}")

        try:
            user_state = client.user.state.name if client.user and hasattr(client.user,
                                                                           'state') and client.user.state else "Недоступно"
            status_info.append(f"<b>Статус:</b> {user_state}")
            logger.info(f"User state accessed for {message.from_user.id}")
        except Exception as e:
            errors.append(f"User state: {str(e)}")
            logger.error(f"Error accessing user state for {message.from_user.id}: {str(e)}")

        if errors:
            status_info.append("<b>Ошибки:</b> Некоторые данные недоступны. Попробуйте /status.")
            logger.error(f"Login errors for {message.from_user.id}: {'; '.join(errors)}")
            await message.answer("\n".join(status_info))
        else:
            await message.answer("\n".join(status_info))
            logger.info(f"Successful login for {message.from_user.id}")

        user_sessions[message.from_user.id]["password"] = None  # Очищаем пароль
    except Exception as e:
        logger.error(f"Error formatting response for {message.from_user.id}: {str(e)}")
        await message.answer("✅ Вход успешен, но данные временно недоступны. Попробуйте /status.")


async def is_session_valid(client: SteamClient, username: str, password: str, user_id: int) -> bool:
    try:
        if not client.logged_on:
            logger.info(f"Session not logged on for {user_id}, attempting reconnect")
            client.reconnect()
            await asyncio.sleep(1)
            if not client.logged_on and password:
                logger.info(f"Reconnect failed for {user_id}, attempting relogin")
                if await try_relogin(client, username, password, user_id):
                    logger.info(f"Session restored via relogin for {user_id}")
                    return True
                return False
            elif not client.logged_on:
                logger.info(f"Session still not logged on after reconnect for {user_id}")
                return False
        logger.info(f"Session valid (logged_on=True) for {user_id}")
        return True
    except Exception as e:
        logger.error(f"Session validation failed for {user_id}: {str(e)}")
        return False


async def keep_session_alive():
    while True:
        for user_id, session in list(user_sessions.items()):
            client = session["client"]
            username = session["username"]
            password = session.get("password")
            try:
                if not client.logged_on:
                    logger.info(f"Session for {user_id} not logged on, attempting reconnect")
                    client.reconnect()
                    await asyncio.sleep(1)
                    if not client.logged_on and password:
                        logger.info(f"Reconnect failed for {user_id}, attempting relogin")
                        await try_relogin(client, username, password, user_id)
                if client.logged_on:
                    await load_user_data(client, user_id)
                    logger.info(f"Session for {user_id} is alive")
                else:
                    logger.warning(f"Session for {user_id} expired")
                    user_sessions.pop(user_id, None)
            except Exception as e:
                logger.error(f"Error keeping session alive for {user_id}: {str(e)}")
                user_sessions.pop(user_id, None)
        await asyncio.sleep(30)  # Проверять каждые 30 секунд


async def main():
    asyncio.create_task(keep_session_alive())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
