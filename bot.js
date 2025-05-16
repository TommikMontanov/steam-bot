const { Telegraf, session, Markup } = require('telegraf');
const SteamUser = require('steam-user');

require('dotenv').config();
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
  ctx.session.step = 'awaiting_login';
  ctx.reply('Введите логин от Steam:');
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

  let level = '?';
  let friends = 0;
  let onlineFriends = [];
  let state = 'Неизвестно';
  let game = 'Нет';

  try {
  const getSteamLevelAsync = () => new Promise((resolve, reject) => {
    client.getSteamLevel((err, level) => {
      if (err) reject(err);
      else resolve(level);
    });
  });

  level = await getSteamLevelAsync();

  const friendIDs = Object.keys(client.myFriends || {});
  friends = friendIDs.length;

  for (let id of friendIDs) {
    const info = client.users[id];
    if (info?.persona?.state === 1) onlineFriends.push(info.player_name || id);
  }

  state = personaStates[client?.personaState || 0];

  if (client?.richPresence?.length > 0) {
    game = client.richPresence[0]?.name || 'Игра';
  } else if (client?.playingAppIDs?.length > 0) {
    game = `AppID ${client.playingAppIDs[0]}`;
  }

} catch (e) {
  console.log('[STATUS ERROR]', e.message);
}

  ctx.reply(`📊 <b>Статус аккаунта:</b>
🔗 В сети: ${state}
⭐️ Уровень: ${level}
👥 Друзей: ${friends}
🟢 Онлайн-друзей: ${onlineFriends.length > 0 ? onlineFriends.length + ' (' + onlineFriends.join(', ') + ')' : 'Нет'}
🎮 Игра: ${game}`, { parse_mode: 'HTML' });
});

bot.hears('🚀 Старт', async (ctx) => {
  const chatId = ctx.chat.id;
  const session = ctx.session;
  const client = userSessions[chatId]?.steamClient;

  if (!session.loggedIn || !client?.steamID) {
    ctx.reply('❌ Вы не вошли в аккаунт Steam.');
    return;
  }

  ctx.session.step = 'awaiting_appid';
  ctx.reply('📥 Введите AppID игры для фарма часов (например, 730):');
});

bot.hears('🛑 Стоп', (ctx) => {
  const chatId = ctx.chat.id;
  const session = ctx.session;
  const client = userSessions[chatId]?.steamClient;

  if (!session.loggedIn || !client?.steamID) {
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
    ctx.reply('👋 Вы вышли из аккаунта Steam.');
  } else {
    ctx.reply('❌ Вы не вошли в аккаунт.');
  }
});

bot.on('text', async (ctx) => {
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
    userSessions[chatId] = { steamClient: client };

    client.logOn({
      accountName: login,
      password: password
    });

    client.on('loggedOn', () => {
      ctx.session.loggedIn = true;
      ctx.session.step = null;
      ctx.reply('✅ Вы успешно вошли в Steam!');
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
    });
  } else if (step === 'awaiting_guard') {
    const code = ctx.message.text;
    if (ctx.session.guardCallback) {
      ctx.session.guardCallback(code);
      ctx.session.step = null;
    }
  } else if (step === 'awaiting_appid') {
    const appid = parseInt(ctx.message.text);
    if (isNaN(appid)) {
      ctx.reply('❌ Неверный AppID. Введите число.');
      return;
    }

    const client = userSessions[chatId]?.steamClient;
    client.gamesPlayed([appid]);
    ctx.session.step = null;
    ctx.reply(`🎮 Запущена игра с AppID ${appid} для фарма часов.`);
  }
});

bot.launch();

process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
