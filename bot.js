const { Telegraf, Markup } = require('telegraf');
const LocalSession = require('telegraf-session-local');
const SteamUser = require('steam-user');
const SteamCommunity = require('steamcommunity');
const http = require('http');
const https = require('https');
const fs = require('fs');

require('dotenv').config();

console.log('BOT_TOKEN:', process.env.BOT_TOKEN);

const bot = new Telegraf(process.env.BOT_TOKEN);

const userSessions = {};
const levelCache = {};
let appListCache = {};

const popularApps = {
  730: 'Counter-Strike 2',
  440: 'Team Fortress 2',
  570: 'Dota 2',
  271590: 'Grand Theft Auto V',
  1172470: 'Apex Legends',
  444090: 'Paladins',
  252490: 'Rust',
  4000: 'Garry\'s Mod',
  550: 'Left 4 Dead 2',
  578080: 'PUBG: BATTLEGROUNDS'
};

// Загрузка кэша приложений из файла
try {
  if (fs.existsSync('app_cache.json')) {
    appListCache = JSON.parse(fs.readFileSync('app_cache.json', 'utf8'));
    console.log('[INFO] Loaded app cache from app_cache.json');
  }
} catch (err) {
  console.error('[APP CACHE LOAD ERROR]', err);
}

async function getAppName(community, appid) {
  if (popularApps[appid]) {
    return popularApps[appid];
  }
  if (appListCache[appid]) {
    return appListCache[appid];
  }
  try {
    // Попробуем Steam Web API
    const response = await new Promise((resolve, reject) => {
      community.httpRequestGet(
        'https://api.steampowered.com/ISteamApps/GetAppList/v2/',
        (err, response, body) => {
          if (err) return reject(err);
          try {
            const data = JSON.parse(body);
            resolve(data);
          } catch (e) {
            reject(e);
          }
        }
      );
    });
    const apps = response.applist.apps;
    for (const app of apps) {
      appListCache[app.appid] = app.name;
    }
    // Сохраняем кэш в файл
    try {
      fs.writeFileSync('app_cache.json', JSON.stringify(appListCache, null, 2));
      console.log('[INFO] Saved app cache to app_cache.json');
    } catch (err) {
      console.error('[APP CACHE SAVE ERROR]', err);
    }
    return appListCache[appid] || `AppID ${appid} (название не найдено)`;
  } catch (err) {
    console.error('[APP LIST ERROR]', err);
    // Альтернативный запрос к Steam Store API
    try {
      const storeResponse = await new Promise((resolve, reject) => {
        community.httpRequestGet(
          `https://store.steampowered.com/api/appdetails?appids=${appid}`,
          (err, response, body) => {
            if (err) return reject(err);
            try {
              const data = JSON.parse(body);
              resolve(data);
            } catch (e) {
              reject(e);
            }
          }
        );
      });
      const appData = storeResponse[appid]?.data;
      if (appData?.name) {
        appListCache[appid] = appData.name;
        fs.writeFileSync('app_cache.json', JSON.stringify(appListCache, null, 2));
        console.log('[INFO] Saved app cache to app_cache.json from store API');
        return appData.name;
      }
      return `AppID ${appid} (название не найдено)`;
    } catch (storeErr) {
      console.error('[STORE API ERROR]', storeErr);
      return `AppID ${appid} (ошибка получения названия)`;
    }
  }
}

// Функция для периодической аутентификации веб-сессии через steam-user
function startWebLogOnInterval(chatId, client, community) {
  // Вызываем webLogOn сразу
  client.webLogOn((err) => {
    if (err) {
      console.error(`[WEBLOGON ОШИБКА] Не удалось выполнить начальный webLogOn для chatId: ${chatId}`, err);
    } else {
      console.log(`[INFO] Начальный webLogOn успешен для chatId: ${chatId}`);
      // Синхронизируем куки с community
      community.setCookies(client._sessionCookies || []);
    }
  });

  // Настраиваем периодический вызов webLogOn каждые 30 минут
  const interval = setInterval(() => {
    if (!userSessions[chatId] || !userSessions[chatId].steamClient.steamID) {
      clearInterval(interval);
      console.log(`[INFO] Остановлен интервал webLogOn для chatId: ${chatId}`);
      return;
    }
    client.webLogOn((err) => {
      if (err) {
        console.error(`[WEBLOGON ОШИБКА] Периодический webLogOn не удался для chatId: ${chatId}`, err);
      } else {
        console.log(`[INFO] Периодический webLogOn успешен для chatId: ${chatId}`);
        // Синхронизируем куки с community
        community.setCookies(client._sessionCookies || []);
      }
    });
  }, 30 * 60 * 1000); // 30 минут

  // Сохраняем интервал в userSessions для последующей очистки
  userSessions[chatId].webLogOnInterval = interval;
}

bot.use(new LocalSession({ database: 'sessions.json' }).middleware());

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

  // Проверяем, действительно ли пользователь авторизован
 if (ctx.session.loggedIn && userSessions[chatId]?.steamClient?.steamID) {
    ctx.reply('✅ Вы уже вошли в Steam. Используйте команды "📊 Статус", "🚀 Старт" или "🚪 Выйти".');
    return;
  }

  // Очищаем старую сессию
  if (userSessions[chatId]) {
    userSessions[chatId].steamClient.logOff();
    if (userSessions[chatId].webLogOnInterval) {
      clearInterval(userSessions[chatId].webLogOnInterval);
    }
    delete userSessions[chatId];
  }

  ctx.session = {
    step: 'awaiting_login',
    loggedIn: false,
    currentAppID: null
  };
  ctx.reply('Введите логин Steam:');
  console.log(`[DEBUG] Вход инициирован для chatId: ${chatId}, session:`, ctx.session);
});

bot.hears('📊 Статус', async (ctx) => {
  const chatId = ctx.chat.id;
  const session = ctx.session;
  const client = userSessions[chatId]?.steamClient;
  const community = userSessions[chatId]?.community;

  if (!session.loggedIn || !client || !client.steamID || !community) {
    return ctx.reply('❌ Сессия не активна. Используйте "🔑 Войти".');
  }

  try {
    // Получаем данные пользователя через SteamCommunity
    let onlineState = 'Неизвестно';
    let level = 'Неизвестно';
    try {
      const user = await new Promise((resolve, reject) => {
        community.getSteamUser(client.steamID, (err, user) => {
          if (err) reject(err);
          else resolve(user);
        });
      });
      console.log('[DEBUG] getSteamUser response for chatId:', chatId, user);
      onlineState = user?.onlineState === 'online' ? 'Онлайн' : user?.onlineState || 'Неизвестно';

      // Получаем уровень через Steam Web API
      const steamID = client.steamID.toString();
      if (levelCache[steamID]) {
        level = levelCache[steamID];
      } else {
        const steamApiKey = process.env.STEAM_API_KEY;
        if (steamApiKey) {
          const levelResponse = await new Promise((resolve, reject) => {
            community.httpRequestGet(
              `https://api.steampowered.com/IPlayerService/GetSteamLevel/v1/?key=${steamApiKey}&steamid=${steamID}`,
              (err, response, body) => {
                if (err) return reject(err);
                try {
                  const data = JSON.parse(body);
                  resolve(data.response.player_level);
                } catch (e) {
                  reject(e);
                }
              }
            );
          });
          level = levelResponse ?? 'Неизвестно';
          if (typeof level === 'number') {
            levelCache[steamID] = level;
          }
        } else {
          console.error('[LEVEL ERROR] Steam API key is missing for chatId:', chatId);
        }
      }
    } catch (err) {
      console.error('[LEVEL ERROR] for chatId:', chatId, err);
    }

    // Получаем список друзей
    const friends = client.myFriends || {};
    const friendIDs = Object.keys(friends).filter(id => friends[id] === SteamUser.EFriendRelationship.Friend);
    console.log('[DEBUG] friendIDs for chatId:', chatId, friendIDs);

    // Формируем сообщение
    console.log('[DEBUG] client.personaState for chatId:', chatId, client.personaState);
    let msg = `📊 <b>Статус аккаунта:</b>\n` +
              `⭐️ Уровень: ${level}\n` +
              `👥 Друзей: ${friendIDs.length}\n`;

    // Получаем статус и имя друзей
    if (friendIDs.length > 0) {
      try {
        const limitedFriendIDs = friendIDs.slice(0, 25);
        const personas = await new Promise((resolve, reject) => {
          client.getPersonas(limitedFriendIDs, (err, personas) => {
            if (err) reject(err);
            else resolve(personas);
          });
        });

        let onlineFriendsCount = 0;
        let friendNames = [];
        if (personas && typeof personas === 'object') {
          for (const id of limitedFriendIDs) {
            if (personas[id]?.persona_state === 1) {
              onlineFriendsCount++;
              if (personas[id]?.player_name) {
                friendNames.push(personas[id].player_name);
              }
            }
          }
          msg += `🟢 Онлайн-друзей: ${onlineFriendsCount > 0 ? onlineFriendsCount + ' (' + friendNames.join(', ') + ')' : 'Нет'}\n`;
        } else {
          msg += `🟢 Онлайн-друзей: Нет данных\n`;
          console.log('[DEBUG] Personas is invalid for chatId:', chatId, personas);
        }
      } catch (err) {
        console.error('[PERSONAS ERROR] for chatId:', chatId, err);
        msg += `🟢 Онлайн-друзей: Нет данных\n`;
      }
    } else {
      msg += `🟢 Онлайн-друзей: Нет\n`;
    }

    // Определяем статус
    console.log('[DEBUG] playingAppIDs:', client.playingAppIDs, 'richPresence:', client.richPresence, 'session.currentAppID:', session.currentAppID, 'chatId:', chatId);
    let status = onlineState;
    if (session.currentAppID) {
      status = await getAppName(community, session.currentAppID);
    }

    msg += `🎮 Статус: ${status}`;

    ctx.reply(msg, { parse_mode: 'HTML' });

  } catch (e) {
    console.error('[STATUS ERROR] for chatId:', chatId, e);
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
  console.log(`[DEBUG] Start command initiated for chatId: ${chatId}`);
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
  session.currentAppID = null;
  ctx.reply('🛑 Фарм остановлен.');
  console.log(`[DEBUG] Game stopped for chatId: ${chatId}`);
});

bot.hears('🚪 Выйти', (ctx) => {
  const chatId = ctx.chat.id;
  const session = ctx.session;
  const client = userSessions[chatId]?.steamClient;

  if (client) {
    client.logOff();
    if (userSessions[chatId]?.webLogOnInterval) {
      clearInterval(userSessions[chatId].webLogOnInterval);
    }
    delete userSessions[chatId];
    session.loggedIn = false;
    session.step = null;
    session.currentAppID = null;
    ctx.reply('👋 Вы вышли из аккаунта Steam.');
    console.log(`[DEBUG] User logged out for chatId: ${chatId}`);
  } else {
    ctx.reply('❌ Вы не вошли в аккаунт.');
  }
});

bot.on('text', async (ctx) => {
  const chatId = ctx.chat.id;
  const step = ctx.session.step;

  console.log(`[DEBUG] Text received for chatId: ${chatId}, step: ${step}, message: ${ctx.message.text}`);

  if (step === 'awaiting_login') {
    ctx.session.login = ctx.message.text;
    ctx.session.step = 'awaiting_password';
    ctx.reply('Введите пароль от Steam:');
    console.log(`[DEBUG] Login received for chatId: ${chatId}, login: ${ctx.session.login}, new step: ${ctx.session.step}`);
  } else if (step === 'awaiting_password') {
    const login = ctx.session.login;
    const password = ctx.message.text;

    const client = new SteamUser({ 
      promptSteamGuardCode: false, 
      httpTimeout: 30000,
      protocol: SteamUser.EConnectionProtocol.TCP
    });
    const community = new SteamCommunity();

    userSessions[chatId] = { steamClient: client, community };

    const loginTimeout = setTimeout(() => {
      ctx.reply('❌ Время ожидания входа истекло. Попробуйте снова с "🔑 Войти".');
      client.logOff();
      if (userSessions[chatId]?.webLogOnInterval) {
        clearInterval(userSessions[chatId].webLogOnInterval);
      }
      delete userSessions[chatId];
      ctx.session.loggedIn = false;
      ctx.session.step = null;
      console.log(`[DEBUG] Login timeout for chatId: ${chatId}`);
    }, 60000);

    client.on('loggedOn', () => {
      clearTimeout(loginTimeout);
      ctx.session.loggedIn = true;
      ctx.session.step = null;
      client.setPersona(SteamUser.EPersonaState.Online);
      console.log('[DEBUG] Logged on, personaState:', client.personaState, 'steamID:', client.steamID?.toString());
      
      // Запускаем webLogOn и периодическое обновление
      startWebLogOnInterval(chatId, client, community);
      
      ctx.reply('✅ Вы успешно вошли в Steam!');
      console.log(`[DEBUG] Пользователь ${login} вошёл в Steam (chatId: ${chatId})`);
    });

    client.on('playingState', (blocked, playingApp) => {
      console.log(`[DEBUG] Playing state changed for chatId: ${chatId}, blocked: ${blocked}, playingApp: ${playingApp}`);
    });

    client.on('steamGuard', (domain, callback) => {
      clearTimeout(loginTimeout);
      ctx.session.guardCallback = callback;
      ctx.session.step = 'awaiting_guard';
      ctx.reply(`📩 Введите код Steam Guard${domain ? ' из почты ' + domain : ' из мобильного приложения'}:`);
      console.log(`[DEBUG] Steam Guard requested for chatId: ${chatId}, domain: ${domain}`);

      const guardTimeout = setTimeout(() => {
        ctx.reply('❌ Время ввода кода Steam Guard истекло. Попробуйте снова с "🔑 Войти".');
        client.logOff();
        if (userSessions[chatId]?.webLogOnInterval) {
          clearInterval(userSessions[chatId].webLogOnInterval);
        }
        delete userSessions[chatId];
        ctx.session.loggedIn = false;
        ctx.session.step = null;
        console.log(`[DEBUG] Steam Guard timeout for chatId: ${chatId}`);
      }, 30000);

      ctx.session.guardTimeout = guardTimeout;
    });

    client.on('error', (err) => {
      clearTimeout(loginTimeout);
      ctx.session.loggedIn = false;
      ctx.session.step = null;
      ctx.reply(`❌ Ошибка входа: ${err.message}. Попробуйте снова с "🔑 Войти".`);
      client.logOff();
      if (userSessions[chatId]?.webLogOnInterval) {
        clearInterval(userSessions[chatId].webLogOnInterval);
      }
      delete userSessions[chatId];
      console.log(`[DEBUG] Ошибка входа для chatId: ${chatId}: ${err.message}`);
    });

    client.on('disconnected', (eresult, msg) => {
      clearTimeout(loginTimeout);
      ctx.session.loggedIn = false;
      ctx.session.step = null;
      ctx.reply(`❌ Соединение с Steam потеряно: ${msg || 'Неизвестная ошибка'}. Попробуйте снова с "🔑 Войти".`);
      if (userSessions[chatId]?.webLogOnInterval) {
        clearInterval(userSessions[chatId].webLogOnInterval);
      }
      delete userSessions[chatId];
      console.log(`[DEBUG] Disconnected для chatId: ${chatId}: eresult=${eresult}, msg=${msg}`);
    });

    try {
      client.logOn({
        accountName: login,
        password: password
      });
      ctx.reply('⏳ Пытаемся войти...');
      console.log(`[DEBUG] Attempting login for chatId: ${chatId}, login: ${login}`);
    } catch (err) {
      clearTimeout(loginTimeout);
      ctx.reply('❌ Ошибка при попытке входа. Попробуйте снова с "🔑 Войти".');
      console.log(`[DEBUG] Login attempt error for chatId: ${chatId}: ${err.message}`);
    }
  } else if (step === 'awaiting_guard') {
    const code = ctx.message.text;
    if (ctx.session.guardCallback) {
      clearTimeout(ctx.session.guardTimeout);
      ctx.session.guardCallback(code);
      ctx.session.step = null;
      ctx.reply('✅ Код Steam Guard отправлен, продолжаем вход...');
      console.log(`[DEBUG] Steam Guard code submitted for chatId: ${chatId}, code: ${code}`);
    } else {
      ctx.reply('❌ Ошибка: Steam Guard callback отсутствует. Попробуйте снова с "🔑 Войти".');
      ctx.session.step = null;
      console.log(`[DEBUG] No guard callback for chatId: ${chatId}`);
    }
  } else if (step === 'awaiting_appid') {
    const appid = parseInt(ctx.message.text);
    if (isNaN(appid) || appid <= 0) {
      ctx.reply('❌ Неверный AppID. Введите положительное число (например, 730 для CS2).');
      console.log(`[DEBUG] Invalid AppID received for chatId: ${chatId}, input: ${ctx.message.text}`);
      return;
    }

    const client = userSessions[chatId]?.steamClient;
    const community = userSessions[chatId]?.community;

    if (!client || !community) {
      ctx.reply('❌ Клиент Steam не найден. Сначала войдите в аккаунт.');
      ctx.session.step = null;
      ctx.session.loggedIn = false;
      console.log(`[DEBUG] No client found for chatId: ${chatId}`);
      return;
    }

    ctx.session.currentAppID = appid;
    client.gamesPlayed([appid]);
    console.log(`[DEBUG] Games played set to AppID: ${appid} for chatId: ${chatId}`);
    ctx.session.step = null;
    ctx.reply(`🎮 Запущена игра с AppID ${appid} для фарма часов.`);
  } else {
    ctx.reply('❓ Пожалуйста, используйте команду, например "🔑 Войти".');
    console.log(`[DEBUG] No valid step for chatId: ${chatId}, session:`, ctx.session);
  }
});

// Создаем HTTP-сервер для Render
const PORT = process.env.PORT || 3000;
const SELF_URL = 'https://steam-bot-g7ef.onrender.com';

http.createServer((req, res) => {
  res.writeHead(200);
  res.end('Bot is running');
}).listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});

// Пинг для Render
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
