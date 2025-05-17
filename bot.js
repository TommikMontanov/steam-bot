const { Telegraf, session, Markup } = require('telegraf');
const SteamUser = require('steam-user');
const SteamCommunity = require('steamcommunity');
const http = require('http');
const https = require('https');


require('dotenv').config();

console.log('BOT_TOKEN:', process.env.BOT_TOKEN); // 👉 добавь эту строку

const bot = new Telegraf(process.env.BOT_TOKEN);
const userSessions = {};

bot.use(session({ defaultSession: () => ({}) }));

bot.start((ctx) => {
  ctx.reply('👋 Привет! Я бот для входа в Steam и фарма часов.',
    Markup.keyboard([
      ['🔑 Войти', '📊 Статус'],
      ['🚀 Старт', '🛑 Стоп'],
      ['🚪 Выйти']
    ]).resize()
  );
});

bot.hears('🔑 Войти', (ctx) => {
  const chatId = ctx.chat.id;

  if (ctx.session.loggedIn) {
    ctx.reply('✅ Вы уже вошли в Steam. Используйте команды "📊 Статус", "🚀 Старт" или "🚪 Выйти".');
    return;
  }

  ctx.session = {
    step: 'awaiting_login',
    loggedIn: false,
  };
  ctx.reply('Введите логин Steam:');
  console.log(`[DEBUG] Вход инициирован для chatId: ${chatId}`);
});

bot.hears('📊 Статус', async (ctx) => {
  const chatId = ctx.chat.id;
  const session = ctx.session;
  const client = userSessions[chatId]?.steamClient;

  if (!session.loggedIn || !client?.steamID) {
    ctx.reply('❌ Сессия не активна. Используйте "Войти".');
    return;
  }

  const personaStates = {
    0: 'Оффлайн',
    1: 'Онлайн',
    2: 'Занят',
    3: 'Нет на месте',
    4: 'Не беспокоить',
    5: 'Спит',
    6: 'Играет',
  };

  try {
    const level = await new Promise((resolve, reject) => {
      client.getSteamLevel((err, level) => {
        if (err) reject(err);
        else resolve(level);
      });
    });

    const friendIDs = Object.keys(client.myFriends || {});
    const friendsCount = friendIDs.length;
    const onlineFriends = friendIDs.filter(id => client.users[id]?.persona?.state === 1).map(id => client.users[id].player_name || id);

    const state = personaStates[client.personaState] || 'Неизвестно';

    let game = 'Нет';
    if (client.richPresence && client.richPresence.length > 0) {
      game = client.richPresence[0]?.name || 'Игра';
    } else if (client.playingAppIDs && client.playingAppIDs.length > 0) {
      game = `AppID ${client.playingAppIDs[0]}`;
    }

    ctx.reply(`📊 <b>Статус аккаунта:</b>
🔗 В сети: ${state}
⭐️ Уровень: ${level}
👥 Друзей: ${friendsCount}
🟢 Онлайн-друзей: ${onlineFriends.length > 0 ? onlineFriends.length + ' (' + onlineFriends.join(', ') + ')' : 'Нет'}
🎮 Игра: ${game}`, { parse_mode: 'HTML' });

  } catch (e) {
    console.log('[STATUS ERROR]', e.message);
    ctx.reply('❌ Ошибка получения статуса.');
  }
});

bot.hears('🚀 Старт', (ctx) => {
  const chatId = ctx.chat.id;
  const session = ctx.session;
  const client = userSessions[chatId]?.steamClient;

  if (!session.loggedIn || !client) {
    ctx.reply('❌ Сначала войдите в Steam командой "🔑 Войти".');
    return;
  }

  session.step = 'awaiting_appid';
  ctx.reply('Введите AppID игры для фарма (например, 730 для CS2):');
});

bot.hears('🛑 Стоп', (ctx) => {
  const chatId = ctx.chat.id;
  const session = ctx.session;
  const client = userSessions[chatId]?.steamClient;

  if (!session.loggedIn || !client) {
    ctx.reply('❌ Вы не вошли в аккаунт Steam.');
    return;
  }

  client.gamesPlayed([]);
  ctx.reply('🛑 Фарм остановлен.');
});

bot.hears('🚪 Выйти', (ctx) => {
  const chatId = ctx.chat.id;
  const session = ctx.session;
  const client = userSessions[chatId]?.steamClient;

  if (client) {
    client.logOff();
    delete userSessions[chatId];
    ctx.session.loggedIn = false;
    ctx.session.step = null;
    ctx.reply('👋 Вы вышли из аккаунта Steam.');
  } else {
    ctx.reply('❌ Вы не вошли в аккаунт.');
  }
});

bot.on('text', (ctx) => {
  const step = ctx.session.step;
  const chatId = ctx.chat.id;

  if (step === 'awaiting_login') {
    ctx.session.login = ctx.message.text;
    ctx.session.step = 'awaiting_password';
    ctx.reply('Введите пароль от Steam:');

  } else if (step === 'awaiting_password') {
    const login = ctx.session.login;
    const password = ctx.message.text;

    const client = new SteamUser();
    const community = new SteamCommunity();

    userSessions[chatId] = { steamClient: client, community };

    // Обработчики SteamUser
    client.on('loggedOn', () => {
      ctx.session.loggedIn = true;
      ctx.session.step = null;
      client.setPersona(SteamUser.EPersonaState.Online);
      ctx.reply('✅ Вы успешно вошли в Steam!');
      console.log(`[DEBUG] Пользователь ${login} вошёл в Steam (chatId: ${chatId})`);
    });

    client.on('steamGuard', (domain, callback) => {
      ctx.session.guardCallback = callback;
      ctx.session.step = 'awaiting_guard';
      ctx.reply(`📩 Введите код Steam Guard${domain ? ' из почты ' + domain : ''}:`);
    });

    client.on('error', (err) => {
      ctx.session.loggedIn = false;
      ctx.session.step = null;
      ctx.reply('❌ Ошибка входа: ' + err.message);
      delete userSessions[chatId];
      console.log(`[DEBUG] Ошибка входа для chatId ${chatId}: ${err.message}`);
    });

    client.logOn({
      accountName: login,
      password: password
    });

    ctx.reply('⏳ Пытаемся войти...');

  } else if (step === 'awaiting_guard') {
    const code = ctx.message.text;
    if (ctx.session.guardCallback) {
      ctx.session.guardCallback(code);
      ctx.session.step = null;
      ctx.reply('✅ Код Steam Guard отправлен, продолжаем вход...');
    }

  } else if (step === 'awaiting_appid') {
    const appid = parseInt(ctx.message.text);
    if (isNaN(appid)) {
      ctx.reply('❌ Неверный AppID. Введите число.');
      return;
    }

    const client = userSessions[chatId]?.steamClient;

    if (!client) {
      ctx.reply('❌ Клиент Steam не найден. Сначала войдите в аккаунт.');
      ctx.session.step = null;
      return;
    }

    client.gamesPlayed([appid]);
    ctx.session.step = null;
    ctx.reply(`🎮 Запущена игра с AppID ${appid} для фарма часов.`);
  }
});


// Создаем простой HTTP-сервер, чтобы Render.com знал, что приложение запущено
const PORT = process.env.PORT || 3000;
const SELF_URL = 'https://steam-bot-kb0y.onrender.com'; // ← Укажи тут свой настоящий адрес сайта!

http.createServer((req, res) => {
  res.writeHead(200);
  res.end('Bot is running');
}).listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});

// Пинг внешнего адреса каждые 30 секунд (рекомендуется для Render)
setInterval(() => {
  https.get(SELF_URL, (res) => {
    console.log(`[Heartbeat] Status code: ${res.statusCode}`);
  }).on('error', (err) => {
    console.error(`[Heartbeat] Ошибка: ${err.message}`);
  });
}, 30 * 1000);

bot.launch();

process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
