# Social Auto-Posting — Platform Approval Guide (Founder Actions)

> **Что это.** Пошаговый гайд для **тебя (фаундера)**: что зарегистрировать, что подготовить и что
> именно сдать YouTube / Instagram(Meta) / TikTok, чтобы Quip получил право **постить клипы от имени
> подключённых юзеров** (multi-tenant publish-on-behalf). Сделано после ресёрча официальных доков
> (mid-2026); каждое требование привязано к официальному источнику.
>
> **Статус кода.** Бэкенд-логика автопостинга уже написана и **ДРЕМЛЕТ** (`services/worker/app/social/`):
> ничто её не импортит, Modal-cron не зарегистрирован — прод не затронут. Дальнейшая разводка функционала
> (миграция БД, OAuth-роуты, UI, включение cron) ждёт, пока **ты не пройдёшь аппрувы по этому гайду**.
>
> **Порядок (по нарастанию боли):** **YouTube** (легче всего, ~1–4 недели) → **Instagram** (App Review +
> Business Verification, ~4–8 недель) → **TikTok** (App Review **+** отдельный Content Posting API audit,
> ~2–6 недель с итерациями). Подаваться можно параллельно, как только готовы общие веб-ассеты (§1).

---

## 0. Карта решений (прочитай первым)

| Площадка | Сколько гейтов | Что именно одобряют | Скоупы | Главный блокер |
|---|---|---|---|---|
| **YouTube** | 1 (OAuth sensitive-scope verification) + опц. quota-audit | право любому юзеру дать `youtube.upload` без warning'а | `youtube.upload` + `youtube.readonly` (**sensitive**, НЕ `youtube` — он restricted → CASA $) | в статусе *Testing* refresh-токены живут **7 дней** → обязателен переход *In production* |
| **Instagram** | 2 (App Review + Business Verification) | Advanced Access к `instagram_business_content_publish` | `instagram_business_basic` + `instagram_business_content_publish` | нельзя нажать «Request Advanced Access», пока не сделан **≥1 реальный publish-вызов**; юзер обязан иметь **Business/Creator** аккаунт |
| **TikTok** | 2 (App Review + **Content Posting API audit**) | публичный Direct Post (audit снимает форс `SELF_ONLY`) | `user.info.basic` + `video.publish` (**НЕ** `video.upload` — это черновик-в-инбокс) | запрет **вшитого вотермарка/лого**; сайт-не-лендинг |

**Одно решение, которое меняет всё (прими сразу):**
- **YouTube — бери только `youtube.upload` + `youtube.readonly`.** Они *sensitive* → обычная проверка Google + демо-видео (~10 дней). Полный `youtube` — *restricted* → обязательный ежегодный сторонний **CASA**-аудит безопасности (≈$15k–$75k, недели/месяцы). Не запрашивай его.

---

## 1. Общие ассеты — собери ОДИН раз, переиспользуй для всех трёх

Все три площадки требуют, по сути, одно и то же про твой сайт/право. Сделай это первым — потом просто
переиспользуешь во всех трёх заявках.

### 1.1. Сайт `quip.ink` — полноценный публичный продуктовый сайт
- ❌ НЕ лендинг, НЕ «coming soon», НЕ страница-логин. (TikTok заворачивает за это №1; Google открывает homepage **залогаут** и проверяет.)
- ✅ Публично описывает продукт **и конкретно интеграцию с соцсетями** («Quip публикует ваши короткие клипы в ваш YouTube/Instagram/TikTok по расписанию, которое вы задаёте»).
- ✅ В футере — **видимые без логина** ссылки на **Privacy Policy** и **Terms of Service** (рабочие, не 404, не за меню/логином).
- ✅ Хостинг на верифицированном домене `quip.ink` (Google требует Search Console — §2.3).

### 1.2. Privacy Policy `https://quip.ink/privacy` — обязательные пункты (консолидировано)
Самые жёсткие требования у Google (Limited Use). Если покроешь их — Meta и TikTok тоже довольны.
- ☐ **Дословная Limited Use аттестация** (вставь предложением, с ссылкой на «Limited Use»):
  > *"Quip's use and transfer of information received from Google APIs to any other app will adhere to the
  > [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
  > including the Limited Use requirements."*
- ☐ **Что именно за данные** берём, по каждому скоупу: YouTube channel info (`youtube.readonly`) + загрузка видео (`youtube.upload`); IG профиль/медиа (`instagram_business_basic`) + публикация (`instagram_business_content_publish`); TikTok creator info + публикация (`video.publish`).
- ☐ **Как используем** — только чтобы публиковать СВОИ клипы юзера в ЕГО собственный канал/аккаунт по расписанию.
- ☐ **Как храним** — OAuth-токены **зашифрованы** (у нас Fernet), только токены юзера; не агрегируем чужие данные.
- ☐ **Как делимся** — список суб-процессоров; **никакой рекламы / продажи / брокеринга** данных; никакого чтения людьми (Google Limited Use 4 пункта).
- ☐ **Удаление/отзыв** — юзер может отключиться и запросить удаление, **выполняем в течение 30 дней** (требование YouTube Developer Policy + Meta).
- ☐ **Ссылки** на [YouTube ToS](https://www.youtube.com/t/terms) и [Google Privacy Policy](https://policies.google.com/privacy) (обязательно при использовании YouTube API).
- ☐ **Контактный email** для privacy-запросов.

### 1.3. Terms of Service `https://quip.ink/terms`
- ☐ Публичная HTTPS-страница, видимая **без логина** (TikTok), на том же домене.

### 1.4. Data Deletion (удаление данных)
- ☐ Нужен **endpoint удаления** или страница-инструкция. Meta требует один из двух: **Data Deletion
  Request Callback URL** (POST-endpoint, возвращает JSON `{ "url": "<status>", "confirmation_code": "<code>" }`,
  подпись HMAC-SHA256) **ИЛИ** **Data Deletion Instructions URL**. Google требует путь удаления (30 дней).

### 1.5. Юр-лицо + документы (для Meta Business Verification)
- ☐ Зарегистрированное юр-лицо Quip (LLC/ИП/ТОО/Ltd).
- ☐ Документы (**цветные, не обрезанные, ≤12 мес**, видны имя+адрес): свидетельство о регистрации/инкорпорации **+** один из {налоговый документ, бизнес-лицензия, недавняя коммуналка/банковская выписка на имя бизнеса}.
- ☐ Точные легальные имя/адрес/телефон/сайт, **совпадающие** с документами.
- ☐ Доступ админа к Business Portfolio (Meta Business Manager).

### 1.6. Quip-специфика: чистый рендер без вотермарка
- ☐ TikTok **прямо запрещает** вшитый лого/вотермарк/ссылку/промо-текст на публикуемом контенте. У Quip
  рендер сейчас вжигает вотермарк (`drawtext`). → для постинга через API нужен **отдельный
  watermark-free рендер**. (Это и фича «без вотермарка» для платных планов заодно.)

### 1.7. App-иконка (форматы под каждую — подготовь сразу)
- YouTube (consent screen logo): **квадрат 120×120 px, JPG/PNG/BMP, ≤1 МБ**.
- Instagram (app icon): **1024×1024 px**, без логотипов Meta/IG.
- TikTok (app icon): **1024×1024 px, JPG/PNG, ≤5 МБ**, без отсылок к TikTok/соцсетям.

---

## 2. YouTube — самый лёгкий вход (делай первым)

**Цель:** перевести OAuth-приложение в **In production** и пройти **sensitive-scope verification**. Без этого
multi-tenant постинг невозможен (в *Testing* refresh-токены умирают за 7 дней — §2.2.3).

### 2.1. Google Cloud проект + API + OAuth-клиент
1. Cloud Console → **New Project** (напр. `quip-prod`). Запиши **Project number (числовой)** и **Project ID**.
2. **APIs & Services → Library** → «YouTube Data API v3» → **Enable**.
3. **Google Auth Platform → Clients** → **Create client** → **Application type: Web application**.
   - **Authorized JavaScript origins:** `https://quip.ink`.
   - **Authorized redirect URIs:** `https://quip.ink/api/auth/youtube/callback` (должно совпадать **байт-в-байт** с тем, что шлёт код: схема/регистр/слеш).
   - Сохрани **Client ID** + **Client secret**.

### 2.2. Google Auth Platform (consent screen) — каждое поле
**2.2.1. Branding** (`console.cloud.google.com/auth/branding`):
| Поле | Значение |
|---|---|
| App name | `Quip` (без «Google»/«YouTube», без намёка на эндорсмент; совпадает с homepage + видео) |
| User support email | твой email / Google Group |
| App logo | **120×120 px, JPG/PNG/BMP, ≤1 МБ** (`icon.png`) |
| Application home page | `https://quip.ink` |
| Privacy policy URL | `https://quip.ink/privacy` (тот же домен) |
| Terms of service URL | `https://quip.ink/terms` |
| Authorized domains | `quip.ink` (верифицирован в Search Console — §2.3) |
| Developer contact | **живой email** — сюда пишет Trust & Safety; мёртвый = ревью застрянет |

**2.2.2. Audience** (`console.cloud.google.com/auth/audience`):
- **User Type = External.**
- Нажми **Publish app** → статус **In production** (кнопка запускает верификацию).

**2.2.3. ⚠️ Ловушка 7 дней (ради чего всё это):** в статусе *Testing* при External + скоупы шире name/email
Google выдаёт refresh-токен, **истекающий через 7 дней**. Планировщик молча потеряет авторизацию каждого
юзера через неделю. Лечится ТОЛЬКО переходом *In production* + верификацией.

**2.2.4. Data Access** (`console.cloud.google.com/auth/scopes`): **Manually add scopes** →
- `https://www.googleapis.com/auth/youtube.upload`
- `https://www.googleapis.com/auth/youtube.readonly`
- Держи этот список **идентичным** тому, что просит код и что показано в демо-видео (расхождение = частый отказ).

### 2.3. Brand/domain verification (Search Console)
1. Google-аккаунт должен быть **Owner/Editor** Cloud-проекта.
2. **Search Console** → добавить **Domain property** для `quip.ink` → подтвердить (надёжнее всего **DNS TXT**).
3. Тот же Google-аккаунт для Search Console и Cloud-проекта. **~2–3 рабочих дня.**

### 2.4. Homepage (см. §1.1) — должна описывать YouTube-интеграцию и иметь ссылку на privacy.

### 2.5. Privacy policy (см. §1.2) — с дословной Limited Use аттестацией.

### 2.6. Демо-видео (главная причина отказов)
- Залить на YouTube как **Unlisted** (НЕ Private), **на английском**, в **проде** на верифицированном домене.
- Обязано показать: **(а)** consent-screen с **точным именем приложения** И **`client_id` в адресной строке**; **(б)** реальное использование **каждого** скоупа в приложении.
- **Перед записью:** отозвать свой прошлый доступ на `myaccount.google.com/permissions` (Remove access for Quip) и **подождать ~1 мин** — иначе Google пропустит экран согласия и видео авто-отклонят.
- Расширь popup, чтобы `client_id=` читался в URL.

**Сценарий (2–5 мин):** 1) `quip.ink` залогаут, показать фичу → 2) Connect YouTube → 3) **пауза на consent**, расширить, прочитать имя + `client_id` + оба скоупа → 4) Allow → 5) показать чтение канала (`youtube.readonly`) → 6) запланировать Short и показать **реальную публикацию в канал** (`youtube.upload`), открыть канал и убедиться.

### 2.7. Сабмит верификации
1. **Google Auth Platform → Publish app → Verification Center → Prepare for verification → Submit.**
2. **Пер-скоуп обоснование** (шаблоны):
   - `youtube.upload`: *"Quip (quip.ink) — multi-tenant SaaS: каждый юзер подключает свой YouTube-канал и планирует публикацию своих Shorts. `youtube.upload` нужен для `videos.insert` — публикации СВОИХ клипов юзера в ЕГО канал по его расписанию. Это ядро продукта; более узкого скоупа для загрузки видео не существует. Другие данные аккаунта не трогаем."*
   - `youtube.readonly`: *"Читаем метаданные собственного канала юзера (channel ID, базовая инфа), чтобы показать, в какой канал пойдёт загрузка, и статус. Только данные авторизованного юзера; чужие не агрегируем."*
3. **Срок:** официально «до 10 дней», реально несколько дней–недель + раунды. **Отвечай Trust & Safety быстро** (на Developer contact + support email).

### 2.8. Отдельно: Quota Audit (только при росте >100 загрузок/день)
- Дефолт (2026): **~100 загрузок/день на ВЕСЬ проект** (общая на всех юзеров), 1 unit/загрузка. Старое «1600 units = 6/день» **устарело**.
- Нужно **только** когда суммарно >100/день. Тогда — форма **`https://support.google.com/youtube/contact/yt_api_form`** (другая команда, недели-месяцы). Поля: Project number (числовой), use case, ожидаемый объём, endpoints (`youtube.videos.insert`), privacy/terms, демо-креды.

### 2.9. Чек-лист YouTube
- [ ] Cloud-проект (Project number + ID записаны); YouTube Data API v3 включён.
- [ ] OAuth Web client; redirect `https://quip.ink/api/auth/youtube/callback`; Client ID/secret.
- [ ] Branding: имя `Quip`, лого 120×120 ≤1МБ, домены, **живой Developer contact**.
- [ ] Audience: External + **In production** (Publish app).
- [ ] Data Access: ровно `youtube.upload` + `youtube.readonly`.
- [ ] `quip.ink` верифицирован в Search Console (DNS TXT).
- [ ] Homepage (описывает YT-интеграцию) + `/privacy` (с Limited Use) + `/terms`.
- [ ] Демо-видео (Unlisted, EN, прод, имя+`client_id`+оба скоупа, доступ отозван до записи).
- [ ] Пер-скоуп обоснования.

---

## 3. Instagram (Meta) — App Review + Business Verification

**Цель:** **Advanced Access** к `instagram_business_content_publish` (постинг от имени любого юзера).

### 3.1. Создание Meta-приложения
1. **developers.facebook.com → My Apps → Create App.**
2. Имя «Quip» + бизнес-email.
3. **App type = Business** (НЕ Consumer/Gaming).
4. Привязать **Business Portfolio** (то юр-лицо, что будешь верифицировать — §3.3).
5. **Products → + Add Product → Instagram.**
6. Внутри Instagram выбрать конфигурацию **«Instagram API with Instagram Login»** (Business Login).
   - ✅ **Facebook-страница НЕ нужна** (офиц.: на этом пути Page не требуется). Юзеру нужен только **professional** IG-аккаунт.
   - Set **Valid OAuth Redirect URI** (напр. `https://quip.ink/api/auth/instagram/callback`), **Deauthorize callback URL**, **Data Deletion Request URL**.

### 3.2. Скоупы (ровно два)
- ☐ `instagram_business_basic` — чтение профиля/медиа подключённого pro-аккаунта (нужен).
- ☐ `instagram_business_content_publish` — создание контейнеров и **публикация Reels** (ядро).
- ❌ НЕ запрашивай `..._manage_messages` / `..._manage_comments` (каждый лишний скоуп = больше поверхности ревью и отказов).

### 3.3. Business Verification (гейтит Advanced Access)
1. **App Dashboard → Settings → Basic** → привязать app к Business Portfolio.
2. **Meta Business Settings → Security Center** → submit документы (см. §1.5: цветные, ≤12 мес, имя+адрес совпадают).
3. Meta может прислать **код подтверждения** (тел/почта).

### 3.4. Settings → Basic (заполнить ВСЁ до сабмита)
- ☐ App icon **1024×1024**, без марок Meta. ☐ App Domains `quip.ink`. ☐ Privacy Policy URL. ☐ Terms URL.
- ☐ **User Data Deletion** — callback (JSON `{url, confirmation_code}`) ИЛИ instructions URL.
- ☐ Deauthorize callback. ☐ Бизнес-email. ☐ Category. ☐ **Data Use Checkup** пройден. ☐ Valid OAuth Redirect URI.

### 3.5. ⚠️ ≥1 успешный API-вызов (кнопка «Request Advanced Access» серая, пока его нет)
- В Dev-mode добавь **свой** IG **professional** аккаунт тестером → подключи через реальный флоу Quip → сделай реальный publish:
  - `POST https://graph.instagram.com/v23.0/<IG_USER_ID>/media` (`media_type=REELS`, `video_url=<публичный 9:16 mp4>`, `caption=...`) → `creation_id`.
  - poll `status_code` до `FINISHED` → `POST .../media_publish` (`creation_id=...`).
  - `GET .../me?fields=user_id,username` — закрывает вызов для `instagram_business_basic`.
- Подожди **до 2 дней** (Meta логирует вызов) → кнопка станет активной. (Вызовы — в пределах **30 дней** до сабмита.)

### 3.6. Скринкаст для App Review (по shot'ам)
- **1080p+**, **английский UI** (или субтитры), **начни залогаут**, показать **весь логин**, **грант скоупов**, медленно, с **аннотациями названий скоупов**. Аудио можно не писать (Meta советует аннотации).
1. `quip.ink` залогаут → регистрация/вход в Quip.
2. **Connect Instagram** → Business Login → юзер логинится в СВОЙ pro-аккаунт.
3. Экран согласия со скоупами `instagram_business_basic` + `instagram_business_content_publish` → **Allow** (аннотация).
4. Показать загруженный профиль (`instagram_business_basic`).
5. Запланировать Reel (показать UI расписания — твой use case).
6. Показать **публикацию Reel** в аккаунт (вживую или прокрутить расписание) (аннотация `..._content_publish`).
7. Открыть Instagram → **опубликованный Reel вживую**.

### 3.7. Тексты для сабмита (шаблоны)
- **Use case (на каждый скоуп):** «Quip (quip.ink) — планировщик контента: юзер грузит длинные видео, мы режем вертикальные клипы (Reels) и **публикуем по расписанию в ЕГО собственный professional IG-аккаунт**. `instagram_business_basic` — аутентификация и показ подключённого аккаунта/результата. `instagram_business_content_publish` — создание контейнеров `media_type=REELS` и публикация Reels по заданному юзером расписанию в ЕГО аккаунт; без действия юзера ничего не публикуется.»
- **Инструкции ревьюеру:** 1) зайти на quip.ink с тест-логином; 2) Connect Instagram с тест-аккаунтом; 3) New Reel → загрузить семпл → Schedule/Publish; 4) увидеть статус Published; 5) открыть IG-аккаунт — Reel опубликован.
- **Креды:** отдельный **тест-логин Quip** + отдельный **IG professional тест-аккаунт** (НЕ личные Meta-креды). Приложение должно быть доступно и рабочее.

### 3.8. Онбординг-гейт: каждый юзер — Business/Creator
- Личные IG-аккаунты **не публикуются через API**. В коннект-флоу заранее сказать: «нужен Business/Creator (Settings → Account type and tools → Switch to professional)», после OAuth **детектить** и показывать явную ошибку (без тихого фейла).

### 3.9. Сабмит
- **App Review → Permissions and Features** → у `instagram_business_content_publish` → **Request Advanced Access** (серая до §3.5) → Edit → use case + инструкции + креды + скринкаст → **Submit**. Business Verification должна быть пройдена.
- **Срок:** дни–2–4 недели/раунд; **ожидай 1–3 раунда** (скринкаст — частая причина отказа).

### 3.10. Чек-лист Instagram
- [ ] App type Business + Instagram product + «Instagram API with Instagram Login».
- [ ] Скоупы ровно `instagram_business_basic` + `instagram_business_content_publish`.
- [ ] Business Verification пройдена (юр-документы).
- [ ] Settings→Basic: иконка 1024×1024, privacy, terms, data deletion, redirect, Data Use Checkup.
- [ ] ≥1 реальный publish сделан (кнопка Advanced Access активна).
- [ ] Скринкаст (1080p, EN, залогаут→грант→schedule→publish→живой Reel).
- [ ] Use case + инструкции ревьюеру + тест-креды (Quip + IG professional).

---

## 4. TikTok — App Review + Content Posting API audit (самый тяжёлый)

**Два независимых гейта:** (1) **App Review** одобряет продукты/скоупы; (2) **Content Posting API audit**
снимает форс `SELF_ONLY` → разрешает **публичные** Direct Post. До audit'а **каждый** пост форсится приватным.

### 4.1. Аккаунт + приложение + продукты + скоупы
1. developers.tiktok.com → аккаунт + **Organization** (компания как владелец) → **Connect an app**.
2. Заполни базовые поля: **App name** (без «TikTok»/«Shorts»/«Reels»), **icon 1024×1024 ≤5МБ**, описание (показывается юзеру на экране авторизации), **Web URL**, **Terms URL**, **Privacy Policy URL** (обе требуют verification).
3. **Add products:**
   - **Login Kit** (OAuth) + **Redirect URI** (напр. `https://quip.ink/api/auth/tiktok/callback`).
   - **Content Posting API** → **включить «Direct Post»** конфигурацию.
4. **Скоупы — ровно:** `user.info.basic` + `video.publish` (Direct Post, без тапа). ❌ **НЕ** `video.upload` (это черновик-в-инбокс, требует дотапа в приложении TikTok; лишние скоупы → задержка ревью).

### 4.2. Сайт (причина отказа №1)
- ☐ `quip.ink` — полноценный публичный сайт (не лендинг/логин).
- ☐ **Privacy Policy + Terms видимы БЕЗ логина**, без копания в меню, ссылки рабочие.
- ☐ Владение policy-URL верифицировано в портале. ☐ Контент сайта совпадает с описанием приложения и скоупами. ☐ Домен в демо-видео = Web URL.

### 4.3. Обязательный compliant posting UX (строится в Sandbox; ревьюер его проверяет)
Даже если Quip *планирует* посты, экран подтверждения «Post to TikTok» обязан реализовать всё:
- ☐ Запросить `POST /v2/post/publish/creator_info/query/` и **визуально показать ник + аватар** креатора.
- ☐ Проверять длину клипа против `max_video_post_duration_sec`.
- ☐ **Селектор приватности** из `privacy_level_options`, **без дефолтного значения** (кнопка Post неактивна, пока не выбрано).
- ☐ Тогглы **Comment/Duet/Stitch** — **не отмечены по умолчанию**, **серые** если `*_disabled`. (Для фото — без Duet/Stitch.)
- ☐ Disclosure **«Your Brand» / «Branded Content»** (`brand_organic_toggle` / `brand_content_toggle`); при Branded Content — `SELF_ONLY` запрещён (тултип «Branded content visibility cannot be set to private»).
- ☐ **Music Usage Confirmation** — точный текст над кнопкой Post: *«By posting, you agree to TikTok's Music Usage Confirmation»* (с Branded Content — добавить «Branded Content Policy and»), с гиперссылками.
- ☐ **Превью** клипа. ☐ **Явное согласие** до отправки байтов в TikTok.

### 4.4. Запрет вотермарка
- Content Sharing Guidelines: нельзя добавлять **brand name, logo, watermark, link или промо-текст** на публикуемый контент. → постить **watermark-free рендер** (см. §1.6). Вшитый «Made with Quip»/лого/ссылка = нарушение и провал audit'а.

### 4.5. Sandbox (строй и демонстрируй ДО аппрува)
- До 5 sandbox'ов/приложение; добавь **свои** TikTok тест-аккаунты (до 10) как Target users.
- Посты форсятся `SELF_ONLY` (приватно). Это позволяет **показать весь флоу** (connect → creator_info → Direct Post приватно) для демо-видео. Публичный постинг откроется только после audit'а.
- Rate limit init-эндпоинта: **6 запросов/мин на токен юзера**.

### 4.6. PULL_FROM_URL — верификация домена (для отдачи клипов с cdn.quip.ink / R2)
- Если `source: PULL_FROM_URL` — домен/URL-prefix источника должен быть **верифицирован** в портале (Manage URL properties → Domain или URL Prefix, через DNS-подпись).
- Требования к media-URL: **https**, **без редиректов**, доступен ≥1 час (download timeout = 1ч).
- Верифицируй `cdn.quip.ink` (домен надёжнее узкого prefix). Альтернатива — **chunked FILE_UPLOAD** (чанки 5–64 МБ, финальный ≤128 МБ, ≤1000 чанков). Для multi-tenant PULL_FROM_URL проще.

### 4.7. Сабмит App Review + audit
- **Демо-видео:** до **5 файлов, ≤50 МБ каждый**, записаны в **Sandbox**, домен в кадре = `quip.ink`, показать **все** продукты и скоупы end-to-end.
- **Форма audit'а** просит: пер-продукт/скоуп объяснение, intended use (multi-tenant, per-user OAuth, постинг по расписанию с заранее взятым согласием), **ожидаемый объём креаторов** (задаёт app-level 24h cap — указывай реалистично), UX-мокапы экрана постинга. Убери лишние скоупы.
- Passing audit снимает `SELF_ONLY` → можно `PUBLIC_TO_EVERYONE`.

**Сценарий демо (в Sandbox, домен quip.ink в кадре):** 1) открыть quip.ink, показать footer Privacy/Terms (грузятся без логина) → 2) Connect TikTok → consent со скоупами (basic+publish) → авторизация sandbox-аккаунтом → 3) показать запрошенный **creator_info** (ник+аватар) → 4) пройти compliant-экран (приватность без дефолта; тогглы; disclosure; точная Music Usage строка; превью **без вотермарка**) → 5) явное согласие → 6) показать пост в профиле тест-аккаунта как `SELF_ONLY`.

### 4.8. Имя/иконка
- Имя без «TikTok»/соц-брендов; иконка — твой бренд, без визуальных отсылок к TikTok. (Quip уже ок.)

### 4.9. Чек-лист TikTok
- [ ] Dev-аккаунт + Organization; app; иконка 1024×1024 ≤5МБ; имя без соц-брендов.
- [ ] `quip.ink` — полный сайт; Privacy + Terms видимы без логина; policy-URL верифицированы.
- [ ] Login Kit + redirect; Content Posting API + **Direct Post ON**; скоупы `user.info.basic` + `video.publish` (без `video.upload`).
- [ ] `cdn.quip.ink` верифицирован как URL property (https, без редиректов) ИЛИ chunked FILE_UPLOAD.
- [ ] Compliant posting UX собран в Sandbox (creator_info ник+аватар; приватность без дефолта; тогглы; disclosure; Music Usage строка; превью; согласие).
- [ ] **Watermark-free рендер** для API-постинга.
- [ ] Sandbox: ≥1 свой тест-аккаунт; флоу постит `SELF_ONLY`.
- [ ] Демо-видео (до 5×≤50МБ, sandbox, домен в кадре, все скоупы); пер-скоуп объяснение; intended use; ожидаемый объём; UX-мокапы.
- [ ] Submit App Review → на Live завершить **Content Posting API audit**.

---

## 5. Quip-специфичные блокеры (свести воедино)
1. 🔴 **Watermark-free рендер** — обязателен для TikTok (запрет вшитого брендинга); пригодится и Insta/YT.
2. 🟠 **IG: юзер обязан быть Business/Creator** — детектить тип аккаунта на онбординге, иначе publish молча не пройдёт.
3. 🟠 **YouTube: статус In production** — иначе 7-дневные refresh-токены убьют планировщик.
4. 🟢 **Шифрование токенов** — уже в коде (`app/social/tokens.py`, Fernet); упомянуть в privacy policy «токены зашифрованы».
5. 🟢 **Публичный URL клипа** — уже есть (`storage.resolve_media_url` → presigned/CDN), нужен для IG/TikTok PULL_FROM_URL.

---

## 6. Мастер-чек-лист «что сдать» (high-level)

**Общее (1 раз):**
- [ ] `quip.ink` — полный публичный сайт + `/privacy` (Limited Use) + `/terms` + data-deletion, всё видимо без логина.
- [ ] Юр-лицо + документы (для Meta).
- [ ] Watermark-free рендер.
- [ ] Иконки в нужных форматах (120×120 для Google; 1024×1024 для Meta/TikTok).

**YouTube (первым):** Cloud-проект + OAuth client + Branding/Audience(In production)/Data Access(2 sensitive скоупа) + Search Console + демо-видео(Unlisted/EN/прод/`client_id`) + пер-скоуп обоснования.

**Instagram:** Business app + Instagram Login + Business Verification + ≥1 publish-вызов + скринкаст(1080p/EN) + use case + тест-креды (Quip + IG professional).

**TikTok:** app + Login Kit + Content Posting API(Direct Post) + 2 скоупа + сайт(Privacy/Terms без логина) + verified `cdn.quip.ink` + compliant UX в Sandbox + watermark-free + до 5 демо-видео(≤50МБ) → App Review → audit.

---

## Источники
Все требования взяты из официальных доков (mid-2026): TikTok for Developers (content-posting-api, content-sharing-guidelines,
app-review-guidelines, add-a-sandbox, media-transfer-guide); Meta for Developers (instagram-platform: content-publishing,
app-review, access-levels, business-verification, data-deletion-callback); Google (Auth Platform branding/audience/scopes,
oauth2 production-readiness sensitive/restricted/brand verification, api-services-user-data-policy, youtube v3 quota & compliance).
Полные URL — в отчётах ресёрча сессии (см. `docs/JOURNAL.md` запись 2026-06-27).
