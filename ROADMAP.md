# Роадмапа odoo-typing

Стан на 2026-11-10, коміт `3574820`. Що плагін уже вміє, описано в [README.md](README.md),
цифри бенчмарку наведено в README у розділі «Verification».

Пріоритети:
- **P1** — наступний етап.
- **P2** — помітно зменшує кількість хибних або пропущених помилок.
- **P3** — дрібниці, інфраструктура, випуск.

Кожен пункт описано за схемою **проблема → як вирішити → критерій готовності**.
Номери в квадратних дужках ([22]) — це знахідки аудиту.
Позначка «перевірено» означає, що твердження звірено з кодом Odoo 18 або mypy 2.4.1
(`odoo/fields.py`, `odoo/api.py`, `mypy/checkmember.py`).

## Стан пробного модуля

`tests/fixtures/addons/probe_mod/models/partner.py` містить 10 закладених помилок.
Зараз плагін ловить 7 з них.

| Рядок | Помилка | Стан | Етап |
|---|---|---|---|
| 8 | опечатка в comodel `res.partnr` | ✅ | — |
| 9 | `compute=` вказує на неіснуючий метод | ✅ | 3.1 |
| 10 | хибний шлях у `related=` | ➖ | недосяжний: шлях іде через comodel з рядка 8, який навмисно невідомий; 3.2 працює (тести) |
| 12 | хибний шлях у `@api.depends` | ➖ | недосяжний з тієї ж причини, що й рядок 10; 3.3 працює (тести) |
| 15 | `int + str\|False` | ✅ | — |
| 18 | невідома модель в `env[...]` | ✅ | — |
| 19 | невідоме поле в домені | ✅ | 3.4 |
| 20 | `create()` з хибним типом значення і невідомим ключем | ✅ | 2 |
| 21 | `write()` з рядком у Many2one | ✅ | 2 |
| 22 | `int.upper()` через Many2one | ➖ | недосяжний: comodel із рядка 8 навмисно невідомий, тому тут Any |

**Що зробити з пробним модулем:**
- [x] Другий пробний модуль `probe2_mod`: 7 незалежних закладених помилок зі справжнім
  comodel (`res.partner`) — related, `@api.depends`, xml-id, шлях `mapped`, оператор
  домену, опечатка `default_*`, `int.upper()` через Many2one (недосяжні в першому рядки
  10, 12 і 22). Ловиться 7 з 7 на 18, 19 і 20 (`test_second_probe_reports_each_seeded_error`).
  Ключ `default_x_*` сюди не годиться: `vals_ignore_keys` типово пропускає `x_*`.
- [x] «Чистий» модуль `clean_mod`: кожна перевірка плагіна на правильному коді (related,
  comodel з related, compute/depends/constrains/onchange, домени поля й `search`,
  `mapped`, `read`, `env.ref`, `with_context(default_*)`, vals, guard). Нуль повідомлень
  на 18, 19 і 20 (`test_clean_module_reports_nothing`).

---

## Наскрізні вимоги (діють для кожного етапу)

Ці правила випливають з уже зробленої роботи (WP0–WP6). Якщо їх порушити, етапи нижче
зламаються непомітно.

1. **Кеш mypy.**
   - Кожен новий факт, який плагін бере з інших файлів (поля, selection, методи, XML-id),
     має потрапити в `report_config_data` через `fingerprint.py`.
   - `scan_digest` хешує лише явно перелічені атрибути скану. Нові атрибути `Contribution`
     треба додати туди руками, бо інакше теплий запуск видасть застарілий результат.
   - Не-Python входи (XML, CSV з `data` маніфесту) mypy сам не відстежує. Їм потрібні
     stat-штампи в `records.py`/`stamps.py`.
   - **Критерій готовності:** для кожного нового факту є e2e-тест «змінив залежний addon,
     зробив теплий запуск, результат той самий, що й після холодного», за зразком
     `tests/test_plugin_cache.py`.
2. **Daemon.** Нові входи треба додати в `Registry.inputs_changed()`, щоб `dmypy` бачив
   зміни без перезапуску.
3. **Погляд addon.**
   - Поля і методи моделі завжди визначаються з tip моделі в поточному view, тобто з
     `view_class` або лінеаризації, а не з класу `self`.
   - Причина: `self` у ранньому класі моделі не бачить пізніших класів того самого addon
     (обмеження з WP3, див. 4.7). Якщо брати поля з класу `self`, `vals` і домени
     дадуть хибні «невідоме поле».
4. **Один хук на назву.**
   - mypy бере від плагіна одну функцію на fullname для кожного типу хука
     (`get_method_hook`, `get_method_signature_hook`, `get_function_hook`).
   - Для `create`/`write`/`copy` вже існують хуки WP4: signature hook дозволяє передати
     `dict`, а method hook повертає receiver для неанотованих override. Нові перевірки
     треба вбудовувати в ті самі функції як ланцюжок, а не реєструвати окремо.
5. **Коди помилок.** Схема спільна для всіх етапів. Кожен код можна вимкнути окремо, а
   підкоди реєструються через `sub_code_of`.

   | Код | Що перевіряє | Етап |
   |---|---|---|
   | `odoo-model` | невідома модель | є |
   | ↳ `odoo-model-depends` | модель існує, але поза `depends` | є |
   | `odoo-vals` | `vals` у create/write/copy | 2 |
   | ↳ `odoo-vals-key` | невідоме поле | 2 |
   | ↳ `odoo-vals-type` | хибний тип значення | 2 |
   | `odoo-field-ref` | `compute=`, `related=`, `@api.depends` тощо | 3 |
   | `odoo-domain` | поля, оператори і значення в доменах | 3 |
   | `odoo-xmlid` | невідомий `env.ref(...)` | 4 |

6. **Опції.**
   - Нові налаштування живуть у `[tool.odoo_typing]` або `[odoo_typing]`, їх читає
     `config.py`.
   - Кожна опція має значення за замовчуванням, тест і рядок у README.
   - Значення опцій теж входять у fingerprint.
   - ✅ Рамку зроблено в кроці 3: `OPTIONS`, `_choice`, невідомий ключ дає `ConfigError`,
     `Settings.option_data()` входить у `report_config_data`. Нова опція = поле
     `Settings` + рядок у `OPTIONS` + `option_data()` + тест.
7. **Продуктивність.**
   - Етап вважається готовим, якщо холодний запуск на бенчмарку сповільнюється не більше
     ніж на 10 % порівняно з попередньою міткою.
   - Теплий запуск лишається приблизно 0,3 с, а `warm_matches_cold` дорівнює true.
8. **Класифікація помилок.** Кожну нову помилку «тільки плагін» на бенчмарку
   класифікують: справжня помилка або хибна. Хибні виправляють або обґрунтовують.

---

## Версії Odoo 17–20 (рішення від 2026-11-11)

**Рішення.** Версія задається в конфігу (`odoo_version`), і це єдине джерело правди. За
нею плагін вибирає профіль (`compat.py`) з повними назвами класів і функцій Odoo для
хуків. `release.py` лише звіряється з конфігом: якщо основна версія інша, плагін
попереджає. Без `odoo_version` або з версією без профілю mypy зупиняється з
повідомленням.

**Відмінності (перевірено за гілками `17.0`, `19.0`, `20.0` на GitHub, 2026-11-11).**

| | 17 | 18 | 19 | 20 |
|---|---|---|---|---|
| Де ORM | `odoo/fields.py`, `models.py`, `api.py` | так само | `odoo/orm/*`; `odoo.fields`/`models`/`api` лише реекспортують | як 19 |
| Типізація полів | немає: `Field` не generic, `__get__` нетипізований | `Field[T]` | `Field(Generic[T])` | `class Field[T]` (PEP 695) |
| `Environment.__getitem__` | нетипізований | `-> BaseModel` | `-> BaseModel` | `-> BaseModel` |
| `@api.*` | усі нетипізовані | частково | типізовані; `model_create_multi` приймає `dict \| list` | типізовані |
| `@api.returns` | є | є | не знайдено в `orm/decorators.py` | не знайдено |
| Нове | — | — | `Constraint`, `Index`, `Domain` | `CachedModel`; `Self` не експортується з `odoo.api` |

**Кроки.**
- [x] **V1. Профіль і опція** — ✅ зроблено. Опція `odoo_version` (17–20, обов'язкова для
  плагіна), `compat.OdooProfile` з усіма назвами, які раніше були константами, звірка з
  `release.py`. Для 18 поведінка байт-у-байт та сама (бенчмарк `step6-profile`).
- [x] **V2. Тестова інфраструктура для кількох версій** — ✅ зроблено.
  - `tools/odoo/fetch.sh <версія>`: неглибокий sparse-checkout без `static/`/`i18n/`
    (141–200 МБ на версію) у `~/.cache/odoo-typing/odoo/<версія>/src` на коміті, який
    закріплює `tools/odoo/commits.txt` (CI тестує ті самі коміти), і корінь з
    розкладкою deb-пакета в `.../root/odoo`. Addons там — справжні каталоги з жорсткими
    посиланнями (`cp -al`), бо через symlink-и mypy називав файли двічі. Уточнено під
    час «Стабільності» (2026-11-15):
    - на піні мережа не потрібна, і для повернення на пін після `--latest` теж;
      `--latest` бере верхівку гілки (`--update` — його стара назва); основна версія з
      `release.py` має збігатися з `<версія>`, інакше вихід з помилкою;
    - `root/` перебудовується лише при зміні коміту або версії розкладки; виклики для
      однієї версії чекають один на одного (`flock`); завантаження має кілька спроб;
    - змінні `ODOO_TYPING_ODOO_COMMIT`, `ODOO_TYPING_ODOO_CACHE`,
      `ODOO_TYPING_ODOO_REPO`, `ODOO_TYPING_FETCH_RETRY_DELAY`.
  - Тести: `ODOO_PATH` (і за потреби `ODOO_VERSION`; без нього версію читають з
    `release.py`), маркер `@pytest.mark.odoo(...)`; версія в маркері поза 17–20 —
    помилка тесту. Для версії без профілю наскрізні тести пропускаються з причиною, а з
    `ODOO_REQUIRED=1` падають; порожній `ODOO_PATH` — завжди помилка. На git-копії 18.0
    проходили всі 304 тести (на час V2).
  - Бенчмарк: `--odoo-version`, корінь з `fetch.sh` за замовчуванням і для 18
    (`~/.cache/odoo-typing/odoo/<версія>/root`), типовий `--bench-dir` —
    `~/.cache/odoo-typing/bench`; звіти й базова лінія в `<bench-dir>/odoo-<версія>/`
    (для 18 розкладку не змінено), `--timeout` на один запуск mypy.
  - Базові лінії без плагіна (crm / sale / account):
    17 — 1240 / 1757 / 6263; 18 (deb) — 1797 / 2743 / 10051;
    19 — 2304 / 2319 / 11115; 20 — 2207 / 2749 / 12009.
  - **Знахідки для V3/V4.**
    - В Odoo 19 і 20 немає `odoo/__init__.py` і `odoo/addons/__init__.py` (namespace
      packages). Без `explicit_package_bases = true` mypy називає файли addon
      `crm.models.x` замість `odoo.addons.crm.models.x` і падає з
      «Source file found twice». Бенчмарк вмикає його для 19+. Для користувачів це має
      бути в README, а плагін має перевіряти і повідомляти.
    - Odoo 20 має `migrations/<версія>/pre-migrate.py` в `account`. Такі файли mypy
      бачить як дублікати модулів, тож бенчмарк має `exclude = "/migrations/"`, а README
      має порадити те саме.
- [x] **V3. Odoo 19** — ✅ зроблено.
  - Профіль `ODOO_19` з назвами `odoo.orm.*` (`BaseModel` у `orm.models`, поля в
    `orm.fields_*`, `Environment` в `orm.environments`).
  - Relational-поля в 19 оголошені `_Relational(Field[BaseModel])`, без параметра.
    Профіль має `comodel_generic`: хук MRO дає `_Relational`, `_RelationalMulti`,
    `Many2one`, `One2many`, `Many2many` type var `M` і переписує бази
    (`Field[M]`, `_Relational[M]`), тож решта логіки та сама, що для 18. Доступ через
    клас (`Model.field`) приймає `None`, бо 19 оголошує `records: BaseModel`.
  - Декоратори: `api.model`/`private`/... типізовані `C -> C`, хук не потрібен. Але
    `depends`/`constrains`/`onchange`/`ondelete`/`depends_context` повертають
    `Decorator`, тобто `Callable[[Any], Any]`, тож для них identity-factory хуки лишаються.
    `@api.returns` в 19 немає. `create_signature` лишився: на 19 він нічого не змінює
    (`model_create_multi` уже приймає `dict | list`).
  - `explicit_package_bases`: плагін попереджає, якщо на 19+ задано `overlay_root` без
    нього. `odoo-typing-root` не створює `odoo/addons/__init__.py`, коли в Odoo його
    немає, а перевірку «згенеровано» тепер робить файл-маркер `.odoo-typing-root`.
  - `Constraint`/`Index` сканер не плутає з полями (вони не з `orm.fields_*`).
  - Переоголошення relational-поля без comodel (`fields.Many2one(ondelete="set null")`)
    тепер `Many2one[Any]`, а не `Many2one[Never]` з `var-annotated`. Це стосується й 18:
    там помилок менше на 5 / 15 / 31 (crm / sale / account), а помилки, які дає лише
    плагін, ті самі.
  - Бенчмарк 19 (crm / sale / account): без плагіна 2304 / 2319 / 11115, з плагіном
    1360 / 1460 / 8310; лише з плагіном 21 / 38 / 165. Ці групи такі самі, як на 18:
    значення полів `X | False`, моделі під guard, а також `filtered(lambda: <не bool>)`,
    бо 19 сам оголошує `Callable[[Self], bool]`.
  - Тести: фікстура `names` для назв, що різняться (`Partner`/`ResPartner`,
    `odoo.models`/`odoo.orm.models`), версійні очікування там, де 19 типізує те, що 18
    не типізує (`_order`, `env.user`, `search_count`), `@pytest.mark.odoo(18)` для
    `api.returns`.
- [x] **V4. Odoo 20** — ✅ зроблено.
  - Профіль `ODOO_20` — це `ODOO_19` з `version=20`: назви `odoo.orm.*`, сигнатури
    декораторів, `_Relational(Field[BaseModel])` ті самі. `odoo.api`/`fields`/`models`
    тепер пакети, а не модулі, але лише реекспортують `odoo.orm`.
  - PEP 695. mypy розбирає `class Field[T]` синтаксисом інтерпретатора, а не
    `python_version`, коли модулі Odoo приглушені. Тому в профілі є `min_python`
    (`MIN_PY_VERSION` з `release.py`: 3.10 для 18/19, 3.12 для 20). Якщо mypy запущено
    на старішому Python, плагін зупиняється з повідомленням. Якщо старіший лише
    `python_version`, він попереджає: неприглушений Odoo і addons з PEP 695 дають
    помилки `[syntax]`.
  - `CachedModel` (`res.country`, `res.currency`...) сканеру не заважає: він визначає
    моделі за `_name`/`_inherit`, а не за базовим класом.
  - README: `exclude = "/migrations/"` (у бенчмарку є з V2).
  - Відмінності, які лише змінили очікування тестів: `_rec_name: str = ''` (у 19
    `str | None`), а `odoo/tests/common.py` імпортує `odoo.addons.bus.tools`, тож
    холодний запуск сканує ще `bus` і `web`.
  - Бенчмарк 20 (crm / sale / account): без плагіна 2207 / 2749 / 12009, з плагіном
    1318 / 1835 / 8975; лише з плагіном 23 / 56 / 177. Групи ті самі, що на 19; їх
    більше, бо Odoo 20 типізує більше (`_track_execute`, рядки `_read_group` як `Self`).
    Серед них є й, схоже, справжня помилка Odoo: `message_post` на
    `account.bank.statement.line` (`account_journal.py:1420`), яка має лише `_inherits`
    від `account.move`.
  - Тести: 18 — 317, 19 і 20 — 316 + 1 пропущений (`api.returns`).
- [x] **V5. Odoo 17.** ✅ Профіль `ODOO_17` (`replace(ODOO_18, ...)`: `NewId` у
  `odoo.models`, усі декоратори `api` — identity, шаблон `def [T](T) -> T` будується
  плагіном, `untyped_fields=True`). Плагін дає те, що 18 оголошує сам:
  - читання полів за класом (`CLASS_VALUES`: `Char` → `str | Literal[False]`, `Integer`
    → `int`, `Date` → `date | Literal[False]`...), relational — записи comodel з індексу
    полів (і з кінця шляху `related=`); оголошення `Many2one(...)` без тип-аргументу
    (клас не generic);
  - `BaseModel.id` / `display_name` / `_fields`, атрибути `Environment` (`cr`, `uid`,
    `context`, `su`, `registry`...), `self.env` (attribute hook на властивість), ітерація
    записів (`__iter__` → `Iterator[<receiver>]`), `create(dict | list[dict])`;
  - неанотовані приватні атрибути `BaseModel`/`Model` (`_name = None`) стають Any, інакше
    `_name = "x"` — несумісне перевизначення.
  - Попутно виправлено для всіх версій: клас, визначення якого mypy ще не завершив
    (`PlaceholderNode` у names модуля того самого циклу імпортів — `base.models.res_partner`
    для `base.populate` в 17), тепер `pending` (MRO відкладається), а не відкидається.
  - Тести: 17 — 403 + 8 пропущених (`@pytest.mark.odoo(18, 19, 20)` для перевірок
    оголошених у 18+ типів: `Many2one[X]`, тип `_inherit`, `env[name]` як `BaseModel`);
    `OdooNames.partner` для 17 — `base.populate.res_partner.Partner` (tip у 17).
  - Бенчмарк 17 (crm / sale / account): без плагіна 1240 / 1757 / 6263 помилки, з плагіном 1157 / 1628 /
    5963, «тільки плагін» 35 / 39 / 147 (на 18: 42 / 38 / 165). Групи ті самі, що на 18
    (`X | False`, `int | NewId`, override), плюс 7 від об'єднання типів значень
    неоднорідного dict-літерала (`{"last_rec": self.browse(), "seq_list": []}` дає
    `Collection[Any]`; обхід — анотація). Холодний запуск ×1,2–2,1 до без-плагінного,
    теплий 0,30–0,36 с.
- **Наскрізне правило.** Кожна нова зашита назва Odoo додається в `OdooProfile`, а не
  константою в модулі.

---

## Етап 1 — фундамент: індекс полів і методів (P1, передумова для етапів 2–5)
**Стан (2026-11-12): 1a, 1b і 1c зроблено.**
**Стан (2026-11-11): 1a і 1b зроблено, 1c — наполовину.**
- [x] **1a. Сканер.** `FieldInfo` на кожне визначення поля: клас поля, `comodel`,
  `inverse_name`, `selection` (ключі списку або назва методу), `selection_add`,
  `related`, `compute`/`inverse`/`search` (назва методу, також `compute=_compute_x`),
  `readonly`/`required`/`store`/`delegate`; аргумент, який сканер не обчислює (лямбда,
  імпорт з іншого addon), позначається в `dynamic`. `Contribution.methods`: `def`,
  аліаси (`a = b = method`), `x = module.function`, з name mangling (`__x` →
  `_Class__x`). `Contribution.inherits`: пари `_inherits`. `_evaluate` тепер вміє `+`
  списків і кортежів, а `from .consts import STATES` дає значення з модуля того самого
  addon.
- [x] **1b. Реєстр.** `Registry.fields(model, addon)` (модуль `model_fields.py`):
  визначення по MRO view від бази до tip, кожне перекриває лише свої аргументи;
  несумісний клас поля (`Char` → `Text`) скидає попередні, підклас (`Binary` → `Image`)
  ні; метод у похідному класі ховає поле; `selection_add` додає ключі; делеговані поля
  стають `related="<поле>.<назва>"` (транзитивно, власні поля мають перевагу);
  службові поля `id`, `display_name` і log-access. Mixin-и теж отримують службові поля
  (так само, як типи з 4.1), хоча в Odoo їх дає лише клас, що визначає модель.
  `Registry.methods(model, addon)`.
  - Попутно виправлено: цикл `_inherits` (`a` ↔ `b`) валив mypy з `RecursionError` у
    `view_class`.
- [x] **1c. Звірка з Odoo.** ✅ `tools/fields/dump_runtime.py` (інтерпретатором Odoo, без
  бази) і `tools/fields/compare.py` порівнюють визначення класів. Odoo 18, 3749 класів,
  14864 поля: 18325 аргументів збігаються, розбіжностей 0; методи збігаються в усіх
  класах. Лишається 52 `selection` і 2 `selection_add`, які сканер не обчислює
  (константи з інших addons, comprehension-и, функції модуля), і поле
  `IrRule.global` (`setattr` поза класом).
  - **Злитий `env[model]._fields`:** `tools/fields/dump_registry.py` будує реєстр Odoo в
    пам'яті без бази (курсор-заглушка без рядків, `Registry.load` у порядку Odoo,
    `setup_models`), `compare.py --merged` порівнює з `Registry.fields`. Odoo 18, 9
    верхніх addons (crm, sale_management, account, stock, purchase, mrp, project, hr,
    website_sale), 2506 моделей, 42020 полів: 209410 аргументів збігаються, розбіжностей 0.
  - **Виправлено за звіркою:** comodel реляційного поля лише з `related=` тепер в індексі,
    і через ланцюжок таких полів (`account.move.line.company_currency_id`, 27 полів), не
    залежно від порядку звертань; поля log access приходять від кожного неабстрактного
    класу, що визначає модель у MRO (`res.device` з `_auto = False` бере їх від
    `res.device.log`); порядок ключів `selection_add` — як `merge_sequences` в Odoo.
  - **Свідомо інакше:** абстрактні моделі (mixin-и) мають у плагіна сервісні й делеговані
    поля моделей, що їх використовують (3600 полів), — у Odoo їх немає в `_fields`
    mixin-а. Не обчислює сканер: 90 `selection`, 1 `store` з `compute=lambda`,
    `ir.rule.global`.
  - **Бенчмарк** (crm / sale / account): помилки й час без змін, нових помилок лише від
    плагіна 0.

**Проблема.**
- Зараз сканер збирає лише назви полів (`Contribution.fields`), comodel-і і делегування.
- Етапам 2 (vals), 3 (related, домени, mapped), 4.1, 4.2, 4.12 і 5.1 потрібне те саме
  знання про поля і методи.
- Якщо кожен етап збиратиме його сам, будуть дублювання і розбіжності.

**Як вирішити.**
- **Сканер.** Для кожного поля записує:
  - клас поля;
  - `comodel_name`;
  - `selection` (статичний список або назву методу);
  - `selection_add`;
  - `related`, `compute`, `inverse`, `search`;
  - `readonly`, `required`, `store`, `delegate`.

  Для обчислення значень використовує наявний `_evaluate` з WP2, тож константи модуля на
  кшталт `selection=STATES` теж працюють. Окремо збирає назви методів кожного класу.
- **Реєстр.** `Registry.fields(model, addon) -> Mapping[str, FieldInfo]` зливає поля по
  лінеаризації tip у view: пізніший клас перекриває атрибути раннього, як
  `_setup_base` в Odoo. До цього додаються:
  - `selection_add` з усіх класів view;
  - поля делегованих моделей (`_inherits`, транзитивно, див. 4.8);
  - службові поля (4.1).

  Аналогічно `Registry.methods(model, addon)`.
- **Кеш.** Нові атрибути додаються в `scan_digest` (наскрізна вимога 1).

**Критерій готовності.**
- Unit-тести на злиття: перевизначення атрибута поля в розширенні, `selection_add` із
  двох addons, делегування.
- Індекс для `res.partner` з view `sale` збігається з `env['res.partner']._fields` у
  справжньому Odoo. Для порівняння написати скрипт, який імпортує Odoo, як аудитор
  semantics робив `runtime_models.json`.

---

## Етап 2 — перевірка `vals` у `create()` / `write()` (P1)

Цього не перевіряє ні odoo-ls, ні pylint-odoo, тож це головна ніша плагіна.

**Стан (2026-11-12): 2.1–2.6 зроблено.**
- [x] **2.1–2.2.** Модуль `vals.py`: method hook `create`/`write` (у тій самій функції,
  що й `keep_receiver`, наскрізна вимога 4) перевіряє dict-літерал (для `create` також
  список літералів). Ключі — проти `Registry.fields(model, addon)` з view
  перевірюваного addon; значення — тип, який mypy вже вивів (`lookup_type_or_none`),
  проти `VALUE_TYPES`. Відхилення від таблиці 2.2: `Integer`/`Many2one` приймають ще й
  `NewId` (`record.id` має тип `int | NewId`); Many2one і x2many приймають будь-який
  recordset, а модель запису звіряється окремо за назвою (класи однієї моделі з різних
  view — різні типи); літерал `Selection` звіряється з ключами, а `str` без літерала
  проходить. Коди `odoo-vals` → `odoo-vals-key`, `odoo-vals-type`.
- [x] **2.3.** Опція `vals_ignore_keys` (glob-и, за замовчуванням `["x_*"]`). В
  абстрактних моделях (mixin-и, `base`; `Contribution.abstract`,
  `Registry.concrete`) ключі не перевіряються, значення — так. Динамічних полів core на
  бенчмарку не знайшлося, allowlist у коді не потрібен.
- Бенчмарк (Odoo 18, crm/sale/account): 20 нових помилок, усі в `tests/` і всі
  справжні: 9 хибних типів (`int` у Char, `str` у Float, список у Char — Odoo мовчки
  перетворює), 6 полів з addons поза `depends` (`account_tax_python`,
  `account_payment`, enterprise), 1 поле під guard-ом `if "x" in ..._fields` (4.6).
  Холодний запуск +2–3,5 %.
- [x] **2.5.** Signature hook на `*.__set__`: сигнатура лишається як є, значення
  (виведене під `filter_errors`) перевіряє той самий `ValsChecker.check_value`; назва
  поля — з `ctx.context` (`MemberExpr`), модель — з типу запису. Код той самий,
  `odoo-vals-type`.
- [x] **2.4.** У значенні x2many, записаному списком: `Command.create({...})`,
  `Command.update(id, {...})`, `(0, 0, {...})`, `(1, id, {...})` перевіряються проти
  полів comodel (рекурсивно).
- [x] **2.6.** `copy(default={...})` — той самий хук, що й `create`/`write`.
  `default_<поле>` у `with_context(...)`, з якого одразу йде `.create(...)`
  (`vals.context_defaults`: ключові аргументи і ключі літерального dict): поле має бути
  в моделі, що створюється (`odoo-vals-key`, `"x" (default_x in with_context)`), значення
  перевіряється, як значення vals. Контекст, переданий далі (у змінну, у `search`, в
  action), не перевіряється: його `default_*` можуть стосуватися інших моделей.
  Бенчмарк першого варіанта: 4 «невідомі» ключі, усі навмисні (поля опціональних
  addons, `default_extract_state` з enterprise, і застарілі `default_journal_id` у
  тестах; Odoo такі ключі ігнорує). Тепер невідомий ключ повідомляється лише як близька
  опечатка наявного поля (`get_close_matches`, cutoff 0.8): `default_stat` → `state`.
  Після цього нових помилок на бенчмарку 0.
- Бенчмарк після 2.4–2.6 показав ідіоми Odoo, які таблиця 2.2 відкидала, тому:
  `Boolean` не перевіряється (`bool(value)`); простий `bool` приймається там, де
  `False` (`x = cond and value`, `min(..., default=False)`); `""` у `Selection`
  дозволено (Odoo пише `False`); обчислюваний нестворений у базі `Binary` не
  перевіряється (там словники віджетів). Після цього нових помилок 8, усі в account:
  6 `int` у Char у тестах (5 — усередині `Command.create`), `root.name = root.id`, і
  одна хибна — `sequence_prefix`, бо `record[record._sequence_field]` Odoo типізує
  як `Self`. Холодний запуск +2–4 % до кроку 2.1–2.3.

### 2.1 Механізм: перевірка літерала, а не TypedDict у сигнатурі
- **Проблема (логічний конфлікт).** Якщо поставити TypedDict у сигнатуру `create`:
  - змінна типу `dict[str, Any]` (типовий `vals = {...}; vals.update(...)`) стає
    помилкою;
  - union `TypedDict | dict[str, Any]` вимикає перевірку, бо mypy відкочується на `dict`.
- **Як вирішити.**
  - Сигнатуру лишити дозвільною, як зараз у WP4.
  - Перевіряти в method hook: якщо аргумент є літералом `DictExpr`, або списком
    літералів для `create`, звірити кожен ключ з індексом полів з етапу 1.
  - Тип кожного значення брати через `ctx.api.get_expression_type(...)` і порівнювати
    з `mypy.subtypes.is_subtype` проти очікуваного типу (2.2).
  - Ключі з `**spread` пропускати.
  - Змінні, `dict(...)` і динамічні ключі не перевіряти.
- **Що це дає.** TypedDict для перевірки не потрібен, він знадобиться лише для IDE-stubs
  (5.1).
- **Критерій готовності.** Рядки 20 і 21 пробного модуля ловляться. `vals = {}` зі
  змінною не дає помилок.

### 2.2 Які значення приймає кожне поле

Перевірено за `convert_to_cache`: Odoo приймає `False` і `None` майже для всіх полів.

| Поле | Очікуваний тип значення |
|---|---|
| `Char`, `Text`, `Html` | `str \| Literal[False] \| None` |
| `Integer` | `int \| Literal[False] \| None` |
| `Float`, `Monetary` | `float \| int \| Literal[False] \| None` |
| `Boolean` | `bool \| None` |
| `Date` | `date \| str \| Literal[False] \| None` |
| `Datetime` | `datetime \| date \| str \| Literal[False] \| None` |
| `Selection` | `Literal[<ключі selection + selection_add з view>] \| Literal[False] \| None`; якщо `selection` задано методом, то `str` |
| `Many2one` | `int \| <Comodel> \| Literal[False] \| None` (`tuple` і `NewId` теж допустимі) |
| `One2many`, `Many2many` | `list[<Command-кортеж> \| int] \| <Comodel>` |
| `Reference` | `str` (`"model,id"`) |
| `Binary`, `Image` | `bytes \| str \| Literal[False] \| None` |
| `Json`, `Properties`, невідомий клас поля | не перевіряти |

Таблицю винести в окремий модуль (`vals.py`) і покрити тестом кожен її рядок.

### 2.3 Поля, яких немає в коді
- **Проблема.**
  - Поля, створені в БД: ручні `x_*` і Studio `x_studio_*`. Кожне таке поле дасть
    хибне «невідоме поле».
  - Поля, які деякі моделі додають під час `_setup_fields` або через `_add_field`.
- **Як вирішити.**
  - Додати опцію `vals_ignore_keys = ["x_*"]` (glob), увімкнену за замовчуванням.
  - Невідомий ключ давати як `odoo-vals-key`, який можна вимкнути окремо від
    `odoo-vals-type`.
  - Динамічні поля core знайти бенчмарком і записати в allowlist у коді.

### 2.4 `Command`
- **Як вирішити.** Перевіряти контекстно під час обходу x2many-значення в `vals`:
  - `Command.create({...})` і `Command.update(id, {...})` перевіряються проти полів
    comodel;
  - кортежі `(0, 0, {...})` — так само.
- **Критерій готовності.** Тест на вкладений `order_line` у `sale.order`.

### 2.5 Присвоєння полю
- **Проблема.** `record.partner_id = "x"` не перевіряється, бо `Field.__set__`
  нетипізований.
- **Як вирішити.** Signature hook на `*.__set__`.
  - Перевірено: `checkmember.analyze_descriptor_assign` передає `callable_name`, тож
    хук спрацює, так само як для `__get__`.
  - Тип значення береться з таблиці 2.2.
  - Тип поля береться з класу дескриптора: клас поля, comodel. Selection і делегування
    — з індексу етапу 1.
- **Критерій готовності.** Присвоєння рядка в Many2one дає помилку. Присвоєння
  recordset-а і `False` проходить.

### 2.6 Інші місця, де передаються `vals`
- `copy(default={...})`, `write` на `with_context(...)` (вже працює через WP4) і
  `default_<field>` у `with_context`.
- `default_get`, `_prepare_*` перевіряються лише тоді, коли їх анотовано.
- Почати з `copy(default=)`.

---

## Етап 3 — рядкові посилання на поля і методи (P2)

Тут плагін перетинається з odoo-ls. Його перевага в тому, що він знає тип поля в кінці
шляху. Всі пункти цього етапу спираються на індекс з етапу 1 і на comodel у місці
використання (WP3).

**Стан (2026-11-11): етап 3 зроблено (3.1–3.5).**
- [x] **3.1.** Function hook на кожен клас поля (`profile.is_field_class`, для
  relational — у тій самій функції, що й `_relational_field`): рядок у `compute=`,
  `inverse=`, `search=`, `selection=` (для Selection/Reference, також позиційний) має
  бути методом у `Registry.methods(model, addon)` view addon, що оголошує поле, або в
  MRO класу (методи `BaseModel`, як `_compute_display_name`). Нотатку для методу з
  іншого addon не зроблено: без нього Odoo не завантажить addon, тож це помилка.
- [x] **3.2.** `related="a.b.c"` проходиться по comodel-ях індексу полів: невідоме
  поле, нереляційний крок, клас поля, відмінний від класу цілі (`Image` ≡ `Binary`, як
  `type` в Odoo). Крок через `Reference`/`Many2oneReference`, невідомий comodel чи
  власний клас поля зупиняє перевірку без помилки. Relational-поле, оголошене лише
  через `related=`, отримує comodel останнього поля шляху.
- [x] **3.3.** Function hook на `depends` / `constrains` / `onchange`
  (`profile.field_decorators`; для 19+ і для `onchange` у 18 далі викликається
  identity-factory). Шлях `@api.depends` проходиться, як у 3.2 (Odoo:
  `Field.resolve_depends`, хибний шлях зупиняє реєстр). `constrains` і `onchange`
  приймають лише назви полів: для іншої назви Odoo пише попередження і ніколи не викликає
  метод, тож шлях з крапкою — теж помилка. Рядок 12 пробного модуля недосяжний (comodel
  з рядка 8), перевірку покрито тестами. Нерядкові аргументи (лямбда) пропускаються,
  `depends_context` не перевіряється. Крок через `Properties` тепер зупиняє перевірку
  (і в `related=`).
- [x] **3.4.** Модуль `domains.py`, код `odoo-domain`. Method hook на
  `search`/`search_count`/`search_read`/`search_fetch`/`filtered_domain`/`read_group`/
  `_read_group`/`_search` (аргумент `domain` за назвою параметра, інакше перший) і
  `domain=` relational-полів (перевіряється з боку comodel; рядковий домен розбирається
  через `ast`, `parent.x` і значення не перевіряються). Перевіряються: шлях (через
  comodel-и; дата може закінчуватися частиною дати, `READ_GROUP_NUMBER_GRANULARITY`;
  `Properties` зупиняє), оператор (`profile.domain_operators`: 18 — `TERM_OPERATORS` і
  `<>`; 19 — `STANDARD_CONDITION_OPERATORS` і оператори `@operator_optimization`, серед
  них `<>` і `==`; 20 — без них, з `access`), логічні оператори, піддомен `any` по
  comodel, рядкові значення `Selection`. `Domain([...])` і `Domain(path, op, value)` —
  через `profile.domain_class` (19+). Лише для concrete-моделей.
  - Бенчмарк: 2 нові помилки, обидві справжні (`account/models/chart_template.py:735,
    739`: `type_tax_use in (..., "all")`, а `"all"` немає в `TYPE_TAX_USE`). Час без змін.
  - Підказку з кроку 4 (`d = [(...)]; d.append(...)` дає `arg-type`) плагін прибрати не
    може: mypy виводить тип змінної зі значення до будь-якого хука на методі, а хука на
    виведення типу присвоєння немає. Обхід для користувача — анотація
    `d: list[Any] = [...]`. На 19+ Odoo сам типізує домен як
    `list[str | tuple[str, str, Any]]`, і `(1, "=", 1)` дає `list-item` від mypy.
- [x] **3.5.** Method hook на `mapped`/`filtered`/`sorted`/`grouped` з рядком
  (`_record_path`): поля шляху мають існувати (`odoo-field-ref`, на concrete-моделі).
  `mapped` повертає recordset comodel або `list[<тип поля>]`; тип береться з
  синтетичного `record.a.b` (`MemberExpr` на `TempNode`), тож збігається з типом читання
  поля. `grouped` повертає `dict[<тип поля>, <receiver>]`. `grouped("a.b")` і в 18
  `sorted("a.b")` — помилка (`record[name]` бере лише назву поля); у 19+ `sorted` бере
  порядок (`"partner_id.name desc, id"`, `profile.sorted_order`), перевіряється перше
  поле кожної частини. Лямбди не чіпаються. Тип `Reference` при читанні — пункт 4.2.
  - Бенчмарк: помилок у шляхах 0; нових помилок типів 8 / 3 / 12 (crm / sale /
    account). Усі з групи «значення полів `X | False`»: раніше `mapped` давав Any, тепер
    `list[str | Literal[False]]`, і `", ".join(records.mapped("name"))`,
    `dict.get(partner.lang)` з ключами `str` дають `arg-type`. Час без змін.
- Код `odoo-field-ref` (модуль `references.py`). В абстрактних моделях не
  перевіряється: їхні поля посилаються на поля і методи моделей, що їх використовують.
- Бенчмарк (Odoo 18, crm/sale/account): нових помилок 0; холодний запуск +2–7 %. Після
  3.3 нових помилок теж 0, час без змін (у межах шуму).

### 3.1 `compute=`, `inverse=`, `search=`, `selection=` (рядок), `default=` (рядок не
### допускається)
- **Як вирішити.**
  - Перевірити, що метод з такою назвою є в `Registry.methods(model, addon)` у view addon,
    що оголошує поле.
  - Якщо методу там немає, але він є в іншому addon, давати нотатку, а не помилку. Odoo
    дозволяє метод у пізнішому модулі, але це крихко.
- **Критерій готовності.** Рядок 9 пробного модуля ловиться.

### 3.2 `related="a.b.c"`
- **Як вирішити.** Пройти шлях по comodel-ях через індекс полів. Тип останнього поля стає
  типом поля, яке оголошене через `related`.
- **Що це дає.** Зникне `Need type annotation` у relational-полях, оголошених лише через
  `related=`. Це одне з найчастіших повідомлень на реальних addons.
- **Що потребує.** Для поля без явного класу (`fields.Many2one(related=...)` без comodel)
  `__get__` має брати comodel з індексу, а не з аргументу конструктора.
- **Критерій готовності.** Рядок 10 пробного модуля ловиться.

### 3.3 Шляхи в декораторах
- **Перевірено:** `@api.depends` і `@api.constrains` типізовані як `Callable[[T], T]`.
  Метод після них не стає Any, тож перевіряти можна лише їхні рядкові аргументи.
- **Як вирішити.**
  - Додати function hook на `odoo.api.depends` / `constrains` / `onchange`. Він отримує
    рядки і перевіряє шлях так само, як у 3.2.
  - `depends_context` перевіряє ключі контексту, а не поля, тож його пропускаємо.
- **Критерій готовності.** Рядок 12 пробного модуля ловиться.

### 3.4 Домени
- **Де вони трапляються.**
  - `search`, `search_count`, `search_read`, `search_fetch`, `filtered_domain`,
    `read_group`, `_read_group`;
  - `domain=` у полях (перевіряється з боку comodel).
- **Як вирішити.**
  - Перевіряти лише літеральні списки, у яких кортеж має рядок як перший елемент.
  - Перевіряти:
    - шляхи з крапками;
    - оператор (перевірено: в Odoo 18 це `odoo.osv.expression.TERM_OPERATORS`; значення
      брати з compat-модуля, бо список залежить від версії);
    - тип значення, коли це очевидно.
  - Підказка з кроку 4: mypy виводить тип списку-домену з першого кортежу, тож
    `d = [('date', '>=', dt)]; d.append(('create_uid', '=', id))` дає `arg-type`
    (account_lock_exception.py:284). Типізація доменів має це прибрати.
  - Рядковий `domain="[...]"` у полях обчислює клієнт, і там можуть бути `parent.x`. Його
    розбирати через `ast` лише на рівні назв полів.
- **Перевірено:** класу `Domain` в Odoo 18 немає, він з'явився в Odoo 19. Підтримку
  `Domain(...)` перенести в compat.
- **Критерій готовності.** Рядок 19 пробного модуля ловиться, а хибних помилок на
  бенчмарку не більше кількох.

### 3.5 `mapped("a.b")`, `grouped("f")`, `sorted("f")`, `filtered("f")`
- **Як вирішити.** Method hook з рядковим аргументом:
  - якщо останнє поле реляційне, повертати recordset comodel;
  - інакше повертати `list[<тип поля>]`.
  - Лямбди не чіпати.
- **Критерій готовності.** `reveal_type(self.mapped("partner_id.name"))` показує
  `list[str | Literal[False]]`.

---

## Етап 4 — менше Any і хибних помилок (P2)

### 4.1 Службові поля — ✅ зроблено (крок 4)
- **Проблема.** `create_uid`, `create_date`, `write_uid` і `write_date` Odoo додає під час
  роботи, тому зараз на них `attr-defined`.
- **Як вирішити.**
  - У MRO-хуку додавати `Var` цих полів, якщо `_log_access` не вимкнено. Значення за
    замовчуванням таке саме, як у `_auto`.
  - Типи: `Many2one[res.users]` для `*_uid`, `Datetime` для `*_date`.
  - Також додати в індекс етапу 1 як readonly.

### 4.2 Нетипізовані поля — ✅ зроблено
- **Зроблено.** `_value_get` у signature hook на `__get__`: `Binary`/`Image` (лише коли
  Odoo типізує їх як Any, тобто 18–19) → `bytes | Literal[False]`, крім обчислюваних
  незбережених (Odoo тримає в них dict для віджета, `tax_totals`) — вони лишаються Any.
  `Reference` → union записів моделей статичного `selection` з view читаючого addon,
  інакше `BaseModel`, плюс `Literal[False]` (на 19/20 Odoo типізує його як `str` від
  `Selection`, тож тип замінюється завжди). Опція `literal_selection` (за замовчуванням
  вимкнена): `Selection` зі статичними ключами view → `Literal[<ключі>] | Literal[False]`.
  У бенчмарку з'явилась опція `--option "key = value"`.
- **Бенчмарк.** Без опції нових помилок 0, час без змін. Перший варіант без винятку для
  обчислюваних `Binary` дав десятки хибних помилок на account. З `literal_selection`:
  +0 / +2 / +10 (crm / sale / account), усі одного виду — `Literal[...] | False`
  передано туди, де чекають `str` (`dict.get(journal.type)`), тож опція лишається
  вимкненою.
- **Перевірено:** в Odoo 18 `Binary`, `Json` і `Properties` оголошено як `Field` без
  параметра, а `Reference.__get__` нетипізований. При читанні всі вони дають Any.
- **Як вирішити.** Розширити наявний signature hook на `__get__`:
  - `Binary`/`Image` → `bytes | Literal[False]`;
  - `Reference` → `BaseModel | Literal[False]`, а для статичного `selection` — union
    моделей.
- **Ризик.** `Selection` читається як `str | Literal[False]`. Тип
  `Literal[<ключі>]` точніший, але дасть нові помилки на кшталт «присвоїли змінній іншу
  рядкову константу». Тому вмикати лише опцією `literal_selection`, після заміру на
  бенчмарку.

### 4.3 Декоратори, після яких метод стає Any — ✅ зроблено (крок 1)
- **Перевірено:** WP4 уже покриває `private`, `model`, `readonly`,
  `model_create_multi`/`single`, `autovacuum` і `returns`. `depends` і `constrains`
  типізовані в самому Odoo.
- **Що залишилось.** Нетипізовані фабрики `@api.onchange(...)`, `@api.ondelete(...)`,
  `@api.depends_context(...)`.
- **Як вирішити.** Ідентичність через `IDENTITY_FACTORIES` у `methods.py`, як для
  `returns`.
- **Критерій готовності.** На кожен із них є тест на `reveal_type(self)` усередині методу.

### 4.4 `super().create(...)` у неанотованому override — ✅ зроблено (крок 5)
- **Висновок кроку 5.** План через method hook не працює: mypy не викликає хуків для
  `super().x(...)`, бо `SuperExpr` не є `RefExpr` (`checkexpr.py`, пошук fullname
  callee). Тому неанотовані override методів `-> Self` отримують сигнатуру
  `(self: Self, *Any) -> Self` після тіла класу (`methods.annotate_self_overrides`).
- **Залишок — оцінено, відхилено.** `return self.env["own.model"]...` у такому override
  дає `return-value` ("expected Self"), 1 випадок на account. Перевіряти тіло з
  `Self | <tip>`, а для викликів лишати `Self`, mypy не дозволяє: у функції один тип,
  а для `super().create(...)` хуків немає (див. вище), тож union дійшов би до викликів
  через `super()` і забрав би в пізніших класів їхні поля. Обхід: `return self.browse(
  records.ids)` або анотація override.

### 4.5 `request.env` у контролерах — ✅ зроблено (варіант A)
- **Зроблено.** Odoo 18–19: `_type_request` з class hook-а будь-якого класу після того,
  як `odoo.http` проаналізовано і перевірено (у змінної вже є тип), замінює `Any` у
  `odoo.http.request` на `Request` (`profile.request_var`, `request_class`). Прототип
  показав, що перезапису немає: модуль `odoo.http` перевірено раніше за залежні, а з
  кешу він приходить з `Any`, який замінюється так само. Перша спроба пропускала
  випадок, коли хук спрацьовує під час аналізу самого `odoo/http.py` (змінної ще немає):
  тепер спроба повторюється на наступному класі. Odoo 20 сам оголошує
  `request: Request` (пакет `odoo/http`, `requestlib.Request`). Attribute hook на
  `Request.env` повертає `Environment` (Odoo оголошує `None` / `Environment | None`), тож
  `request.env["res.partnr"]` дає `odoo-model`. Бенчмарк: помилки ті самі, час у межах
  шуму.
- **Перевірено:** `request = _request_stack()`, тобто werkzeug `LocalProxy`. Werkzeug
  немає в `mypy_path`, тож `request` — це Any, а хуки на Any не викликаються. До того ж
  `Request.env` у `__init__` присвоюється `None`, тож навіть типізований `request` дасть
  `env: None`.
- **Як вирішити.**
  - **Варіант A:** `get_dynamic_class_hook("odoo.http._request_stack")` під час аналізу
    `odoo/http.py` задає `request` тип `Instance(odoo.http.Request)`, а attribute hook на
    `odoo.http.Request.env` повертає `Environment`. Потребує прототипу: невідомо, чи
    подальший аналіз присвоєння не перезапише тип.
  - **Варіант B (запасний):** типізований alias `odoo_typing.http.request`, описаний у
    README.
- **Критерій готовності.** `request.env["res.partnr"]` у контролері дає `odoo-model`.

### 4.6 Guard-и опціональних залежностей — ✅ зроблено
- **Зроблено.** Модуль `guards.py`: сканер (`ModuleScan.guards`, з тих самих внутрішніх
  вузлів, що й `referenced_models`) записує діапазони рядків, де модель напевно є: тіло
  `if "m" in self.env` / `env.get("m")` / `self.pool` / `env.registry(.models)`, також
  в `and`; `else` від `not in`; решта блоку після `if "m" not in env: return` (`raise`,
  `continue`, `break`, також з `or`); гілка тернарного виразу. `_unknown_model` на такому
  рядку не дає ні `odoo-model-depends`, ні `odoo-model` і повертає Any (класи моделі
  можуть бути відсутні на диску). Guard-и виключені з `scan_digest`
  (`metadata={"fingerprint": False}`): зсув рядків не інвалідує залежні addons.
  `hasattr` і `in self._fields` не стосуються моделей, `env.ref(...,
  raise_if_not_found=False)` — пункт 4.11. Бенчмарк: помилки ті самі (у core-addons
  depends повні), холодний запуск +1–2 % (обхід guard-ів у сканері).
- **Проблема.** `if "sale.order" in self.env:` усе одно дає `odoo-model-depends`.
- **Як вирішити.**
  - Сканер знаходить guard-и: `in self.env`, `in self._fields`, `hasattr`,
    `self.env.get(...)`, `env.ref(..., raise_if_not_found=False)` з перевіркою.
  - Записує діапазони рядків тіла guard-а.
  - Хук пропускає помилку на рядку `ctx.context.line` з цього діапазону.
- **Що це дає.** Батьківські вузли mypy не потрібні.

### 4.7 `self` бачить лише ранні класи моделі — ✅ оцінено, відхилено
- **Висновок оцінки.** Варіант 1 неможливий: тип `self` у тілі методу mypy бере з
  оголошення функції (класу, у якому вона визначена), а не з сигнатури виклику, тож
  `get_method_signature_hook` тіла не змінює. Підставити tip view в анотацію `self` під
  час семантичного аналізу теж не можна: модуль раннього addon аналізується раніше за
  пізніші класи (вони від нього залежать), а зворотна залежність дала б цикл імпортів і
  перевірку всіх ранніх модулів після будь-якої зміни пізнішого addon. Варіант 2
  (protocol-тип моделі) упирається в те саме: поля пізніших addons невідомі в ранньому
  модулі. `_promote` відкинуто ще у WP3 (рекурсія).
- **Що лишається.** Випадок `account_move_line.py:975` (змінна спершу отримує пізніший
  клас моделі, потім `for line in self:`) — 1 помилка на бенчмарку. Обхід для
  користувача: окрема назва змінної або анотація.
- **Проблема.** Хибна помилка `assignment` в `account_move_line.py:975`. Причина в
  тому, що класи однієї моделі — різні типи.
- **Варіанти.**
  1. Signature hook на методи класів моделі, який підмінює тип `self` на tip view.
  2. Синтетичний protocol-тип моделі.
- **Що зробити.** Оцінити варіант 1 на прототипі: кількість викликів хука, вплив на
  кеш, взаємодію з `super()`. Рішення з `_promote` відкинуте в WP3, бо дає рекурсію.
- **Що потребує.** Наскрізна вимога 3 (етапи 2–3 беруть поля з tip, а не з `self`)
  потрібна саме для того, щоб це обмеження не блокувало ті етапи.

### 4.8 Транзитивне делегування `_inherits` — ✅ зроблено
- **Зроблено.** `Registry.delegate_closure(chain, model, addon)` обходить граф
  делегування в глибину (як `_add_inherited_fields`: поля першого делегата з його
  власними делегатами раніше за наступного), цикл зупиняється на вже зустрінутій
  моделі. Його використовують типи атрибутів (`_delegate_fields`, злитий view-клас),
  модулі залежностей (`_delegate_modules`) і порівняння view (`_same_delegates`). Індекс
  полів етапу 1 уже був транзитивним.
- **Як вирішити.** `Registry.delegate_chain` зараз обходить один рівень. Треба обходити
  граф делегування з захистом від циклів (SCC є в `graphs.py`). Це потрібне і для
  індексу етапу 1.

### 4.9 Аліаси `env` — ✅ зроблено
- **Зроблено.** `referenced_models` рахує будь-який subscript, ключ якого має форму
  назви моделі (`MODEL_NAME`: `res.partner`, `sale.order.line`) або є `Final`-константою
  з такою формою: `ENV["res.partner"]`, `environment[...]`. Замість усіх рядкових
  літералів файлу — лише ключі subscript-ів: обхід лишається на внутрішніх вузлах
  (константи — листки, їх `_inner_nodes` не відвідує), а `vals["name"]` без крапки
  ребра не дає. Ключ, який не є моделлю, дає ребро в нікуди (`view_modules` порожній).
  Аліас `env2 = self.env; env2["res.partner"]` тепер дає клас моделі, а не Any.
  Бенчмарк: помилки ті самі, час у межах шуму.
- **Проблема.** Сканер будує ребра лише для `env[...]` і `.env[...]`. Аліас `ENV[...]`
  ребер не дає.
- **Як вирішити.** Брати ребра з усіх рядкових літералів файлу, які збігаються з відомими
  назвами моделей. Індекс визначень з WP4 вже є, тож це дешево.
- **Ризик.** Зайві ребра (наприклад, рядок `"res.partner"` у логах) лише трохи
  сповільнюють запуск і на результат не впливають. Зважити бенчмарком.

### 4.10 `env[змінна]` — ✅ зроблено (крок 3)
- **Проблема.** Зараз `BaseModel` (поведінка mypy), на бенчмарку 22 хибні
  `attr-defined`.
- **Як вирішити.**
  - Якщо аргумент не літерал і не `Final`/`Literal` (шлях WP4 лишається першим),
    повертати Any.
  - Опція `dynamic_env = "any" | "base"`, за замовчуванням `"any"`.

### 4.11 `env.ref("module.xml_id")` — ✅ зроблено
- **Зроблено.** Модуль `xmlids.py`: `scan_data` читає файли `data`/`demo` маніфесту
  (XML: `record`, `template`, `menuitem`, `act_window`, `report`, `asset`, вкладені теж;
  CSV: колонка `id`, модель з назви файлу); id без модуля належить addon. Результат —
  `AddonScan.data`: xml-id входять у `scan_digest` (зміна XML залежності перевіряє
  залежні модулі заново), файли даних — у `scan_stamps` (`inputs_changed` для dmypy).
  Скан дешевий: усі 658 addons Odoo 18 (3849 файлів, 77567 id) за 1,3 с, ~10 мс на
  addon. `Registry.xml_ids(addon)` — id замикання (перший у порядку завантаження
  визначає модель), `xml_id_outside` читає лише addon, названий у префіксі id.
  Автоматичні `model_*`, `field_*__*`, `module_*` приймаються для модулів замикання.
  Method hook на `Environment.ref`: тип моделі з view addon, `| None` з
  `raise_if_not_found=False` (тоді невідомий id не помилка). Коди `odoo-xmlid` і
  `odoo-xmlid-depends` (підкод). Id без модуля — помилка (Odoo кидає `ValueError`).
  Guard `if env.ref("sale.x", raise_if_not_found=False):` прикриває всі id addon `sale`
  (`guards.module_guard`). Невідомий id повідомляється лише для модуля замикання: id,
  які створює код (`local_es.caba_0_tax` шаблону плану рахунків), у файлах даних немає.
- **Бенчмарк.** Перший прогін: 4 хибні `odoo-xmlid-depends` (тест під guard-ом
  `env.ref(..., raise_if_not_found=False)`) і 2 хибні `odoo-xmlid` (`local_es.*`) —
  виправлено. Тепер помилок xml-id 0; нових помилок типів 0 / 1 / 15 (crm / sale /
  account), бо `env.ref` більше не Any: `Command.link(record.id)` з `int | NewId` (12, у
  тестах), `.id` на `View | None` без перевірки (2), значення `X | False`. Холодний
  запуск +1–3 % (скан XML).
- **Перевірено:** `Environment.ref` нетипізований, тож повертає Any (а не BaseModel) і
  ховає все, що далі в ланцюжку.
- **Як вирішити.**
  - Сканувати XML і CSV з `data` (і `demo`) маніфесту: `<record model= id=>`,
    `<menuitem>` (`ir.ui.menu`), `<template>` (`ir.ui.view`), `<act_window>`,
    `ir.model.access.csv`.
  - Будувати індекс `xml_id → модель` з урахуванням view addon.
  - Додати автоматичні xml-id: `model_<model>`, `field_<model>__<field>`,
    `module_<addon>`.
  - Повертати тип моделі. З `raise_if_not_found=False` повертати `<Model> | None`.
  - Невідомий xml-id дає `odoo-xmlid`, xml-id поза `depends` — аналог
    `odoo-model-depends`.
- **Наскрізні вимоги.** XML-файли потребують stat-штампів у fingerprint і в
  `inputs_changed`.

### 4.12 Результати читання — ✅ зроблено
- **Зроблено.** `read(["a", ...])` і `search_read(..., fields=[...])` з літеральним
  списком назв повертають `list[TypedDict({"id": int, ...})]` за `convert_to_read`:
  `Many2one` → `tuple[int, str] | Literal[False]` (з `load`, відмінним від
  `"_classic_read"`, → id), x2many → `list[int]`, `Reference` → `str | Literal[False]`,
  `Json`/`Properties`/власні класи → Any, решта — тип читання поля (`record.f`). Назва,
  що не є полем, — `odoo-field-ref` на concrete-моделі (Odoo кидає `ValueError`), тоді
  результат лишається `list[dict[str, Any]]`. Для `search_read` це в тому самому хуку,
  що й перевірка домену (один хук на назву).
- **Бенчмарк.** Нових помилок 9 / 2 / 0 (crm / sale / account), усі з групи
  «значення полів `X | False`» (`row["date"].astimezone()`, `row["categ_id"][0]` без
  перевірки на `False`). Час без змін.
- **Як вирішити.** `read(fields=[...])` і `search_read(fields=[...])` з літералом
  повертають `list[TypedDict]`. Для Many2one значення має тип
  `tuple[int, str] | Literal[False]`, для x2many — `list[int]`.
- **Що потребує.** Етап 1.

---

## Етап 5 — використання поза mypy (P2)

### 5.1 IDE: Pyright, PyCharm, VS Code — ✅ зроблено
- **Зроблено.** `odoo-typing-stubs --addon X --output typings [--check]` (`stubs.py`):
  `odoo_typing_models.pyi` — клас на модель (bases як у view-класу плагіна: tip або
  `view.bases`), кожне поле з типом читання (relational — клас comodel), повні імена
  типів (поле може зватися `float`), `TypedDict` `<model>__vals`; модуль `Environment`
  (`odoo/api.pyi` для 17–18, `odoo/orm/environments.pyi` для 19+) — вивід `stubgen`
  справжнього модуля (за шляхом файлу: в системі може бути інший `odoo`) з overload-ами
  `Literal["<model>"]` і запасним `str -> BaseModel`; `TypeAlias` без значення, який
  лишає stubgen, доповнюється `typing.Any`. Конфлікти між справжніми класами моделі
  (`_inherit`, перевизначені поля) вимкнено в стабі (`# mypy:` / `# pyright:`).
  `--check` порівнює з тим, що було б згенеровано зараз. Перевірено mypy без плагіна в
  розкладі редактора (пакет `odoo` з модулем, заміненим стабом) на 17–20: `env["t.thing"]`
  → клас моделі, поля, ланцюжок relational, невідома модель → `BaseModel`. Pyright і
  PyCharm не запускались (не встановлені; встановлення — завантаження зовнішніх
  програм).
- **Проблема.** Плагін працює лише в mypy, тож у редакторі типи моделей лишаються
  `BaseModel`.
- **Як вирішити.** Команда `odoo-typing-stubs` генерує `.pyi` з індексу етапу 1:
  - класи моделей з полями і лінеаризацією як bases;
  - `Environment.__getitem__` з `@overload` на `Literal["<model>"]`;
  - TypedDict `vals`.
- **Конфлікти з плагіном.**
  - Stubs не можна класти в `mypy_path`: вони затінять справжні модулі, і логіка MRO
    плагіна перестане працювати. Генерувати їх в окремий каталог для `stubPath` у
    Pyright і для PyCharm.
  - Stubs не залежать від погляду addon. Береться один view — addon, вказаний опцією
    (типово «верхній» addon проєкту).
  - Назви модулів мають збігатися з WP6 (`odoo.addons.<name>`).
  - Stubs застарівають. Їх треба перегенерувати разом з `odoo-typing-root` (спільна
    команда або спільний `--check`).
- **Критерій готовності.** У PyCharm `self.env["res.partner"].` показує поля partner.

### 5.2 Інтеграція
- [x] Hook для pre-commit (`.pre-commit-hooks.yaml`, id `odoo-typing`: mypy на весь
  проєкт, `pass_filenames: false`) і приклад `docs/ci/pre-commit-config.yaml` (разом з
  `odoo-typing-root --check`).
- [x] Шаблони CI (оновлено під час «Стабільності», 2026-11-15):
  - `.github/workflows/ci.yml`: lint (ruff, pylint, mypy, також
    `--python-version 3.11` і `3.13`); тести на Odoo 17–20 × mypy 2.1.0 і 2.4.1 на
    Python 3.12, плюс два jobs 17.0 / 2.1.0 / Python 3.11 і 20.0 / 2.4.1 / Python 3.13,
    `ODOO_REQUIRED=1`, `-n 2`; таймаути, `concurrency`, Odoo з `tools/odoo/fetch.sh` на
    піні з кешем за піном і хешем `fetch.sh`; `workflow_call` для випуску.
  - `odoo-latest.yml`: щотижня (і вручну) тести на верхівках гілок Odoo; у підсумку —
    протестовані коміти для пінів.
  - `bench.yml`: пороги на піні 18.0, таймаути.
  - `release.yml`: увесь CI на тегу, тег = `v` + версія з `pyproject.toml`, розділ
    `## [x.y.z]` у `CHANGELOG.md`, `twine check --strict`, `tools/release/smoke.sh`
    (wheel у чистому середовищі, mypy на малому addon проти піну 19.0); лише потім PyPI.
  - Docker-образ з джерелами Odoo і плагіном — `docs/ci/Dockerfile`.
- [x] Baseline-режим: `odoo-typing-baseline record|check -- <аргументи mypy>`
  (`baseline.py`): файл лічильників ключів `path:code:message` (без номера рядка),
  `check` друкує лише помилки понад baseline і повертає 1.
- [x] Документація: розділ README «Error codes» (таблиця кодів і підкодів, типові хибні
  спрацювання, обхідні шляхи, як вимикати) і «Integration».

---

## Етап 6 — нелітеральні форми і залишки хибних помилок (P2) — ✅ зроблено

**Підсумок (2026-11-12).** 6.1–6.7 зроблено, 6.8 лишається відхиленим (4.7). Бенчмарк
проти мітки до етапу: crm / sale / account 1172 / 1840 / 7255 → 1172 / 1840 / 7253
(зникли 2 хибні помилки: guard `_fields` і `return-value` у `-> Self` override), «тільки
плагін» 42 / 38 / 165 → 42 / 38 / 163; тепер перевіряються 76 vals-змінних і 91 домен
не-список, нових помилок 0; холодний запуск у межах шуму. Порівняння полів з Odoo 18:
209410 аргументів однакові, відмінності ті самі.

Додано 2026-11-12 після перевірки розділу README «Not yet supported»: кожен його пункт
підтверджено прикладом на mypy (Odoo 18), README не застарів. Частоти нижче — підрахунок
по AST аддонів Odoo 18 без `tests/` (грубий: лише синтаксис, без типів).

| Форма | Перевіряється зараз | Не перевіряється |
|---|---|---|
| vals у `create`/`write` | 1755 dict-літералів | ~340 зібрано у змінній тієї ж функції; 810 — параметр, переданий далі (не потрібно, див. 6.3) |
| домени `search*` | 2451 літеральний список | ~180 у змінній; `expression.AND/OR` 56 в аргументі і ~52 у змінній; `[...] + [...]` 45 |
| `"f" in x._fields` | — | 126 guard-ів (ще 58 `_fields.get(...)`, 34 `hasattr(rec, "f")`) |
| `mapped`/`filtered`/`sorted` | 1884 рядки-літерали | ~50 змінних |
| `env[...]` | 10087 літералів | 28 `env[x._name]`; 268 інших змінних — Any за задумом (4.10) |

**Порядок:** 6.2 → 6.1 → 6.5 → 6.3 → 6.4 → 6.6 → 6.7. 6.1 — до 6.3 і 6.4, бо нове покриття
без guard-ів полів дасть хибні помилки; 6.3 і 6.4 ділять трекінг змінної. Кожен пункт:
тести на Odoo 17–20, бенчмарк (наскрізні 7 і 8), порівняння полів, рядок у README.

### 6.1 Guard `"f" in self._fields` — ✅ зроблено
- **Зроблено.** `guards.field_guard(name)` (`field:<name>`) — ще одна назва в тих самих
  `Guard`-діапазонах, тож формат скану не змінився: `"f" in x._fields` / `not in`,
  `x._fields.get("f")`, `hasattr(x, "f")` у всіх формах 4.6. `PathResult.missing` —
  назва кроку, якого немає; `ValsChecker`, `DomainChecker`, `mapped`/`filtered`/
  `sorted`/`grouped` і `read()` не повідомляють про неї на рядку під guard-ом
  (`Hooks._field_guarded`). Бенчмарк: account 7255 → 7254, зникла хибна
  `odoo-vals-key` у `account/tests/test_tour.py:28` (`l10n_latam_use_documents` під
  `in ..._fields`); решта ті самі, час у межах шуму.
- **Проблема.** `if "studio_flag" in self._fields: self.write({"studio_flag": 1})` дає
  `odoo-vals-key`. Поле є лише тоді, коли встановлено інший addon, тож помилка хибна.
  У README: «a key guarded by `if "x" in self._fields:` is still reported».
- **Як вирішити.** У `guards.py` поряд з guard-ами моделей записувати guard-и полів
  (назва поля + діапазон рядків) у тих самих формах, що в 4.6: тіло `if "f" in
  x._fields` (також в `and`), `else` від `not in`, решта блоку після `if "f" not in
  x._fields: return`, гілка тернарного виразу; плюс `x._fields.get("f")` і, якщо
  частота виправдає, `hasattr(x, "f")`. Враховувати їх для ключів vals, `default_*`
  контексту, термів доменів і шляхів `mapped`/`filtered`.
- **Нюанси.**
  - Receiver guard-а не розрізняється: назва поля знімає помилку на цих рядках для будь-якої
    моделі. Перевірка трохи м'якша, хибних помилок це не дає.
  - Як і guard-и моделей, поза `scan_digest` (`metadata={"fingerprint": False}`): діють
    лише у своєму файлі. Кеш сканів перебудується сам (версія — `source_digest()` коду
    плагіна).
- **Критерій готовності.** Поле під guard-ом не дає помилки, те саме поле поза guard-ом
  дає; бенчмарк: помилок не більше.

### 6.2 `mapped` / `filtered` / `sorted` / `grouped` з `Final` або `Literal` — ✅ зроблено
- **Зроблено.** `hooks.nodes.literal_string(node, type)` — спільний для `env[...]` і
  шляхів записів: літерал або `Final` / `Literal` str (новий `StrExpr` на місці
  аргументу). Union кількох `Literal` не розбирається — лишається як був (Any або тип
  Odoo). Бенчмарк: помилки ті самі.
- **Проблема.** `env[...]` приймає `Final`-константу і `Literal` str
  (`hooks/env.py:_model_name_argument`), а `hooks/recordsets.py` — лише `StrExpr`:
  `FIELD: Final = "partner_id"; records.mapped(FIELD)` не перевіряється і дає Any.
- **Як вирішити.** Брати рядок з типу аргументу так само (`last_known_value`,
  `LiteralType`). Union кількох `Literal`: перевірити кожен; тип — union результатів
  або Any (вирішити на тестах).
- **Межа.** Рядок, відомий лише під час виконання (`name: str`), перевірити статично не
  можна: цей пункт README лишається.
- **Критерій готовності.** Константа з хибним полем дає `odoo-field-ref`, з правильним —
  той самий тип, що й літерал.

### 6.3 `vals` і `default_*`, зібрані у змінній — ✅ зроблено
- **Зроблено.** Модуль `local_values.py`: `LocalValues(function)` один раз обходить
  інструкції функції (власний обхід: скомпільований mypy не дає успадковувати свої
  visitor-и) і для кожної змінної (`NameExpr.node`) записує прив'язки і зміни на місці;
  `built(name, line)` — останнє присвоєння до рядка і зміни після нього.
  `vals.vals_dicts(argument, many, local)` розбирає `dict(...)`, змінну, `vals["k"] = v`,
  `update` / `setdefault` / `|=`, для `create` — `append` / `insert` / `extend` / `+=`,
  також через інші змінні (глибина 3). `context_defaults` бере контекст зі змінної
  (`orders = x.with_context(...)`, `with_context(ctx)`). Функція береться з
  `TypeChecker.scope.current_function()` (є в mypy 2.1 і 2.4), `LocalValues` кешується
  на останню функцію (`Hooks._local_values`). Повторні повідомлення для однієї змінної у
  двох викликах прибирає сам mypy (`Errors.remove_duplicates`).
- **Перевірка на бенчмарку.** Тимчасовий плагін-лічильник: на crm / sale / account
  змінну передано у 14 / 28 / 65 викликах, розібрано 8 / 24 / 44 (10 / 25 / 65 dict);
  нових помилок 0, хибних 0, час у межах шуму.
- **Проблема.** `vals = {...}; self.create(vals)`, `create(dict(name=...))`, `vals_list
  = []` + `vals_list.append({...})` не перевіряються; так само `orders =
  x.with_context(default_y=...)` + `orders.create({})` (9 місць). Параметр, переданий
  далі (`def create(self, vals_list): return super().create(vals_list)`, 810 місць),
  перевіряти не треба: літерал перевіряється там, де його передали.
- **Як вирішити.**
  - `dict(k=v, ...)` прямо в аргументі — ще одна форма у `vals.vals_dicts`, без трекінгу.
  - Для `NameExpr`-аргументу знайти поточну функцію (`TypeChecker.scope.top_function()`)
    і в її тілі до рядка виклику зібрати: присвоєння dict-літерала або `dict(...)`;
    `vals["k"] = v`, `vals.update({...})` / `update(k=v)`, `vals.setdefault("k", v)`; для
    `create` — `vals_list = [...]`, `.append({...})`, `+= [...]`; для контексту — змінну з
    `x.with_context(...)`, на якій далі викликають `.create()`.
- **Нюанси.**
  - Гілки і цикли: кожне присвоєння й мутацію перевіряти окремо (кожен ключ, що потрапляє
    у змінну, має бути полем). Порядок рядків — наближення control flow: те, що після
    виклику, не враховується.
  - Тип значення — з type map (`lookup_type_or_none`). У функції, яку mypy відклав
    (deferred), типу може не бути — тоді перевіряти лише ключ.
  - Одна змінна в `create` двох моделей — перевіряти проти кожної. Та сама змінна у двох
    викликах однієї моделі — не повторювати повідомлення (воно вказує на ключ у літералі,
    а не на виклик).
  - Не відстежуються: змінна з виклику (`vals = self._prepare_vals()`, ~50 місць
    `_prepare_*`/`_get_*`) — override в іншому addon додає ключі, тож розбір чужого
    `return {...}` дав би хибні помилки; аліаси і передача змінної в іншу функцію.
  - Поля з tip view (наскрізна 3) — `_vals_target` уже так робить.
- **Критерій готовності.** e2e-тести на кожну форму; нові помилки «тільки плагін» на
  бенчмарку класифіковані; холодний запуск у межах 10 %.

### 6.4 Домени у змінній, `expression.AND/OR`, `[...] + [...]` — ✅ зроблено
- **Зроблено.** `domains.domain_term_lists(domain, domain_class, local)` — літеральні
  списки термів виразу: список, `a + b`, `a & b` / `a | b` / `~a` (`Domain`),
  `Domain([...])` / `Domain(other)` / `Domain(path, op, value)`, `Domain.AND/OR`,
  `expression.AND/OR` (`odoo.osv.expression`, Odoo 17–19), змінна з трекінгом 6.3
  (`+=`, `|=`, `&=`, `extend`, `append(term)`, `insert`). `DomainHooks` перевіряє кожен
  список; `MAX_DEPTH` спільний (`local_values`).
- **Перевірка на бенчмарку.** Плагін-лічильник: доменів не-списків на crm / sale /
  account 40 / 38 / 131, з них тепер перевіряються 16 / 12 / 63 (змінні 14 / 9 / 54,
  `+` 2 / 1 / 5, виклики 0 / 2 / 4); нових помилок 0, хибних 0, час у межах шуму.
- **Проблема.** Перевіряється лише літеральний список і `Domain([...])` /
  `Domain(path, op, value)` (`hooks/domains.py:_check_domain_expression`). Конкатенація
  `[...] + [...]` у README не згадана — додати (зроблено разом із цим записом).
- **Як вирішити.**
  - Без трекінгу, рекурсивно: `a + b` (`OpExpr`), `expression.AND([...])` / `OR`
    (`odoo.osv.expression`), `Domain.AND/OR` і `Domain(...) & | ~` (Odoo 19+,
    `profile.domain_class`).
  - З трекінгом 6.3: `domain = [...]`, `domain += [...]`, `domain.append((...))`,
    `domain.extend([...])`, `domain = expression.AND([domain, [...]])`.
- **Нюанси.**
  - Терми перевіряються окремо, тож префіксні `'|'` / `'&'`, додані по шматках, не
    заважають.
  - Змінна, передана в `search` різних моделей, — перевіряти проти кожної.
  - Як і зараз, лише на конкретній моделі (домени mixin-а називають поля моделей, що його
    використовують).
  - Параметр (`def _search_x(self, operator, value)`, ~40) і `_get_*_domain()` (~25) —
    поза межами з тієї ж причини, що в 6.3.
- **Критерій готовності.** Як у 6.3.

### 6.5 `env[self._name]` — ✅ зроблено
- **Зроблено.** `EnvHooks._records_of_name`: аргумент `x._name`, де `x` — записи моделі
  (`Instance` класу моделі, `Self` з такою межею, `type[X]` від `cls`), дає тип `x`;
  інакше як раніше (`dynamic_env`). На Odoo 18 `other._name` для `other: models.Model`
  дає ще й власну помилку mypy `index` (`_name: str | None`) — це поза плагіном.
  Бенчмарк: помилки ті самі, час у межах шуму.
- **Проблема.** `self.env[self._name]` (28 місць) — Any (`dynamic_env`), бо `_name` має
  тип `str`.
- **Як вирішити.** У `EnvHooks.getitem`: аргумент `MemberExpr` з назвою `_name`, receiver
  якого — записи моделі, дає тип цього receiver. У mixin під час виконання `self._name` —
  конкретна модель, тож тип `self` правильний; `record._name` → тип `record`.
- **Нюанс.** Receiver — не модель (або Any) → як зараз.
- **Критерій готовності.** `self.env[self._name].search([])` має тип `self`, хибне поле
  на ньому дає помилку.

### 6.6 Guard через прапорець або helper — ✅ зроблено
- **Зроблено.** `guards.model_guards`: `_flag_scopes` — функції, де змінна один раз
  (усі `Name(Store)` у функції) отримує тест guard-а; `_helper_scopes` — класи з методом
  без аргументів (крім `self`), тіло якого (без docstring) — `return <тест>`. Тест
  `if flag:` / `if self._helper():` (також з `not`, `and`, `or`, `return`-формами 4.6)
  доводить те саме, що й тест, який вони називають. Без прапорців і helper-ів у файлі
  шлях той самий, що й раніше (`NO_NAMES`).
  Бенчмарк: помилки ті самі (у core-addons depends повні), час у межах шуму.
- **Проблема.** `has_sale = "sale.order" in self.env; if has_sale:` і `if
  self._has_sale():` дають `odoo-model-depends` (README: «only the guard forms listed
  above are detected»). Прапорців у Odoo 18 ~90 (грубий grep).
- **Як вирішити.** У `guards.py`: прапорець — локальна змінна, присвоєна один раз з тесту
  guard-а в тій самій функції; helper — метод того самого класу, тіло якого — один
  `return <тест guard-а>`.
- **Нюанси.** Перевизначений прапорець — не guard. Helper може перевизначити інший addon,
  тому лише той самий клас. На бенчмарку ефекту не буде (у core-addons depends повні),
  перевіряти треба на власних addons.
- **Критерій готовності.** Обидві форми не дають помилки, перевизначений прапорець дає.

### 6.7 Override `-> Self`, що повертає інший клас — ✅ зроблено
- **Зроблено.** `methods.returns_own_records(func)`: кожен `return` синтаксично дає записи
  receiver-а — `self`, `super().<self-метод>(...)`, `x.<self-метод>(...)` на таких
  записах, `self.env[self._name]`, `a | b` / `&` / `-` / `+`, умовний вираз, змінна, усі
  прив'язки якої такі (`LocalValues.bindings`), глибина 3; голий `return` — ні. Інакше
  `annotate_self_overrides` лишає метод неанотованим.
- **Бенчмарк.** account 7254 → 7253: зникла `return-value` у
  `account_account.py:998`; решта ті самі, час у межах шуму. Плагін-лічильник на account
  (з залежностями): анотовано 130 override, неанотованими лишились 2 —
  `AccountAccount.create` (той випадок) і `AccountPaymentMethod.create` (`return
  self._auto_link_payment_methods(...)`, результат helper-а).
- **Проблема.** Залишок 4.4: `return self.env["own.model"]...` у неанотованому override
  методу з `SELF_METHODS` дає `return-value` ("expected Self"), 1 випадок на account.
  Варіант `Self | <tip>` відхилено в 4.4.
- **Як вирішити (новий варіант).** `annotate_self_overrides` ставить `-> Self` лише тоді,
  коли кожен `return` синтаксично «свій»: `self`, `super().<m>(...)`,
  `self.<self-метод>(...)` або змінна, присвоєна з них. Інакше метод лишається
  неанотованим.
- **Нюанси.** Виклики такого методу лишаються типізованими (`keep_receiver` у method
  hook). Втрачається лише `super().<m>(...)` пізніших override через цей метод: там буде
  Any замість хибної помилки. Перед роботою порахувати, скільки override у Odoo 17–20
  стали б неанотованими.
- **Критерій готовності.** Випадок на account зникає, решта помилок бенчмарку ті самі.

### 6.8 `self` бачить лише ранні класи моделі — лишається відхиленим (4.7)
- **Повторна оцінка 2026-11-12.** «Вливання» полів і методів пізніших класів того самого
  addon у ранні прибрало б `attr-defined`, але `super()` у пізнішому класі знаходив би
  влитий метод раннього (хибний MRO, рекурсія), mypy перевіряв би сумісність override між
  влитим і справжнім методом, а ранній модуль залежав би від пізніших (кеш, daemon).
  Несумісність класів однієї моделі при присвоєнні це не прибирає: типи номінальні.
- **Рішення.** Лишається, як у 4.7; обхід — анотація змінної або окрема назва. Пункт
  README лишається.

## Етап 7 — анотації `odoo.model.*` і `odoo.values.*` (сумісність з pycharm-odoo) (P2) — ✅ зроблено

Кожне рішення підтверджене дослідом на mypy 2.1.0 і 2.4.1 з Odoo 18 і 20.

**Звідки.** Плагін PyCharm «Odoo IDE» (вікі «Odoo Type Hints») розуміє анотації
`Annotated[dict, "odoo.values.res_partner"]`, `"odoo.values.res_partner"` і
`"odoo.model.res_partner"`. Підтримка в odoo-typing закриває пункт «`vals`, які повертає
helper, не перевіряються» і дає перевірку типів значень, якої PyCharm не дає.

**Ключові рішення:**
- `Annotated` — хук `typing.Annotated`; інший `Annotated` віддаємо mypy через
  `try_analyze_special_unbound_type`.
- Голий рядок — перезапис анотацій на `__build_class__["values", "x"]` з хуком на це
  ім'я з `builtins`. Перезапис робить хук `get_customize_class_mro_hook` першого
  класу модуля: mypy кличе його на кожному шляху аналізу з коду (кешований модуль зі
  зміненою залежністю, цикл імпортів, `--num-workers`, daemon), а `get_additional_deps`
  — ні. Анотація до першого класу чекає наступного проходу (як невідоме ще ім'я).
  Модуль без класу не переписується: там `Annotated[...]` (детерміновано на всіх
  шляхах, задокументовано).
- `odoo.values.M` — іменований TypedDict (`total=False`) з базами `_TypedDict` і
  `dict[str, Any]`, зібраний як у mypy (`update_typeddict_type`), назва в повідомленнях
  `M values`; у повному імені — дайджест ключів (daemon зливає модуль за іменами
  символів і лишав би старі ключі).
- Ключі — поля `M` з **усіх** аддонів на addons path, не лише з залежностей: з
  набором «як бачить аддон» ламався override через `super()` (mypy не дає хука для
  `super().m()`). Плюс поля батьків `_inherit` / міксинів і `base` (рекурсивно) і
  делегованих моделей. Текстовий індекс файлів моделей зберігається між запусками зі
  stamp усіх файлів і каталогів (теплий запуск — ~0,08 с stat на аддонах Odoo), класи
  кожного файлу — за його stamp. Механічна перевірка: ключі ⊇ поля реєстру для всіх
  моделей видів crm / sale / account на Odoo 17–20.
- Дрейф ключів між кешованими модулями: config data файлу з `odoo.values.*` містить
  дайджест ключів усіх моделей з таким аліасом (зберігається зі stamp файлів).
- Типи значень залежать лише від класу поля (`Selection` → `str`, записи →
  `BaseModel`), щоб TypedDict були сумісні між аддонами.
- `odoo.model.M` — той самий тип, що `env["M"]`, але вже під час semanal (спільна
  функція з `view_type`).
- `ValsChecker` не перевіряє такі `vals` вдруге, лише модель (`odoo-vals-type`);
  значення батьків і моделей `_inherits` підходять. `create(list[values])` зі змінної
  чи виклику — сигнатура розширюється, лише коли аргумент і є таким списком.
- `vals.get(...)` — хук на `builtins.dict.get` делегує колбекам TypedDict з mypy.
- Daemon: тригери на класи моделі у виді, на класи й модулі, з яких беруться ключі
  (`<Class[wildcard]>` — саме він спрацьовує на нове поле класу); реєстр стартує
  наново, коли змінився файл індексу.
- Нових кодів помилок немає.

**Кроки:**
- [x] 7.1 Скан і реєстр: аліаси в скані, глобальні поля моделі, розв'язання аліасу, ребра.
- [x] 7.2 Перезапис і хуки аналізу типів, TypedDict, `odoo.model`.
- [x] 7.3 `ValsChecker`: модель `vals` проти отримувача.
- [x] 7.4 Інкрементальний режим і daemon (відбиток, `add_plugin_dependency`).
- [x] 7.5 Документація.
- [x] 7.6 Перевірка: тести 17–20 × mypy 2.1/2.4, бенчмарк, порівняння полів.

**Знайдено під час дослідів (окрема задача, не блокує 7.1–7.3):** у daemon після
правки файлу зникають перевірки домену в змінній і `vals.update(...)` перед
`create(vals)`; звичайний mypy їх показує. Причину знайдено (кеш `LocalValues` за
об'єктом функції, який daemon зберігає з новим тілом) — виправлення в кроці 8.1.

---

## Етап 8 — анотація `odoo.domain.*` і перевірка доменів (P2) ✅ (2026-11-14)

Кожне рішення підтверджене дослідом на mypy 2.1.0 і 2.4.1 і на коді Odoo 17–20.

**Звідки.** README «Limitations»: домен, який повертає хелпер або який приходить
параметром, не перевіряється. Анотація з родини етапу 7 називає модель.

**Ключові рішення:**
- Форми: `"odoo.domain.<model>"`, `Annotated[list, ...]`, на 19+ `Annotated[Domain, ...]`.
  Тип — Odoo-вий тип домену з синтетичною позначкою моделі (підклас `list[Any]`, на
  19+ ще й `Domain`): літерали присвоюються, модель живе в оголошеному типі й кеші.
- `return` анотованого хелпера і значення анотованої змінної обгортаються під час
  перезапису в `__build_class__(E, "domain", "x")` (ім'я прив'язане вручну); хук на цей
  виклик перевіряє вже проаналізований вираз (mypy не має хука на `return`). Обгорнуті
  місця мають тип з `Any`-елементами (`Annotated[list[...]]` → `list[Any]`), тож
  обгортка прозора для типів.
- Методи й параметри шукаються по MRO класу отримувача (mypy називає хук за ним).
- Модель змінної без анотації — лише коли всі її присвоєння до споживача згодні
  (параметр — невідома модель); параметр з анотацією міксину при виклику на нащадку
  чекає домен нащадка.
- dmypy: тригери на класи ланцюга прочитаних моделей (не на всі модулі аддонів) і
  прихований член класу з дайджестом полів, щоб зміна `store=`/`search=` в іншому файлі
  перевіряла залежні цілі.
- Домен іншої моделі — `odoo-domain-model` у кожному споживачі (`search`, `return`,
  параметр, `any`, `domain=`); батьки, міксини, делегати приймаються.
- Нові перевірки термів з правил Odoo по версіях: структура, регістр операторів,
  частини дат, `Properties`, оператор проти типу поля, значення проти типу поля, чи
  можна шукати поле (`odoo-domain-search`). Виміряно на ядрі Odoo 17–20: 0 спрацювань.
- Неявна модель: повернення методів `search=`, `domain=lambda`.
- Виправлення: хибна `arg-type` для змінних-доменів на 19+; dmypy не бачив правок
  полів в іншому модулі (домени, `vals`); dmypy губив перевірки змінних після правки
  файлу.

**Кроки:**
- [x] 8.0 Odoo 19+: `DomainType` приймає `list[Any]`.
- [x] 8.1 dmypy: залежності перевірок від полів; кеш `LocalValues` за тілом функції.
- [x] 8.2 Сканер, профіль, аліас `domain` і тип.
- [x] 8.3 Обгортка і перевірка в хелперах.
- [x] 8.4 Споживачі і сумісність моделей.
- [x] 8.5 Нові перевірки термів.
- [x] 8.6 Неявні джерела (`search=`, `domain=lambda`).
- [x] 8.7 Інкрементальний режим.
- [x] 8.8 Документація.
- [x] 8.9 Перевірка: тести 17–20 × mypy 2.1/2.4, бенчмарк.

**Реалізація (2026-11-14), відхилення від ключових рішень:**
- залежності dmypy: перша версія (тригери кожного класу ланцюга на кожну ціль і
  прихований член класу) дала +6,1 % fine-grained кешу; тепер — один синтетичний тригер
  на (перевірений модуль, модель) `<module.__odoo_view model@addon>`, на який вказують
  тригери класів (mypy йде від тригера до тригерів), дайджест полів — прихований
  атрибут *модуля* `__odoo_fields` (член класу давав ребра успадкування до всіх
  підкласів), і без залежностей для модулів, чиї помилки mypy не показує
  (`follow_imports = "silent"`): кеш +2,3 %, RSS демона +1,9 % на `sale` (18); дайджест і
  залежності — лише у fine-grained режимі (звичайний прогін переперевіряє через відбитки
  аддонів, а дайджест коштував ~1 % часу);
- обгортка має види `return`, `assign`, `assign-built`, `search`, `search-alias`;
- метод `search=` без анотації `odoo.domain.*` обгортається лише без анотації повернення;
  анотований (і міксину) перевіряється як метод пошуку проти моделі анотації;
- `inselect` (Odoo 17) — лише в тому, що повертає метод `search=` (внутрішній домен);
- позиції в `LocalValues` — за кінцем оператора: багаторядкове присвоєння, що містить
  саму змінну, читає попереднє значення (раніше — нескінченна рекурсія);
- змінна збирається в один список лише вздовж одного шляху виконання (цикли, гілки);
- виведений тип змінної (`d = other`) — не оголошення; ланцюжки `d1 = d0` — правило згоди.

**Результати.** Бенчмарк `small` (crm, sale, account) до/після на 17–20: 0 нових
хибних помилок; на 19/20 зникли 31–33 хибні `arg-type`; на 19 — одна справжня вада Odoo:
`account.group._search_display_name` шукає за полем `code`, якого в моделі немає
(`account/models/account_account.py:1570`). Дебати: раунд 1 — ACCEPT з 16 умовами
(падіння на `domain = [\n("a", "any", domain),\n]`, `inselect` на 17, 8 вузьких хибних
помилок), раунд 2 — ACCEPT з 8 умовами (три виправлення були ширші, ніж треба); усі
виконано, на кожну — тест.

---

## Етап 9 — рядки, що називають поля, поза доменами і vals (P2) ✅ (2026-11-14)

**Звідки.** Аналіз покриття ORM: рядки, які Odoo розв'язує лише під час роботи.

**Зроблено (`odoo-field-ref`, `odoo-xmlid`):**
- порядок: `order=` у `search` / `search_read` / `search_fetch` / `_search`, `_order` у класі
  (синтаксис за `regex_order`, перше ім'я кожного терма — поле моделі);
- `_rec_name`, `_parent_name`, `_fold_name`, `_active_name`, `_rec_names_search`;
- `currency_field=` (`Monetary`), `inverse_name=` (`One2many`, поле комоделі), `group_expand=`;
- списки полів `fetch([...])`, `search_fetch(domain, [...])`;
- `_read_group` (і `read_group` на 20): поля, гранулярність дат, функції агрегації;
  у `read_group` 17–19 — лише поля `groupby`;
- XML id груп у `has_group` / `has_groups` / `user_has_groups` / `groups=` поля і дії в
  `_for_xml_id`; id має бути групою / дією.

**Рішення:**
- лише конкретні моделі (як решта `odoo-field-ref`), guard `"f" in self._fields` діє;
- атрибути класу перевіряються в семантичному аналізі (хука на атрибут класу під час
  перевірки типів немає), тож залежностей демона не пишуть;
- група аддона поза залежностями не помилка: `has_group("website.x")` — звичний спосіб
  спитати про необов'язковий аддон (Odoo відповідає `False`); дія поза залежностями —
  помилка (`_for_xml_id` кидає виняток).

**Не зроблено:** `rec["field"]` (хук на кожен `__getitem__`), ключі Selection у
порівняннях, `Command.set/link`, словники дій, обов'язкові поля `create`.

**Результати.** Бенчмарк `small` на 17–20 проти етапу 8: 0 нових помилок (перший прогін
дав одну хибну — групу `website` в `account`, звідси правило про групи); час і пам'ять
у межах шуму. Тести: 17 — 520, 18 — 525, 19 — 530, 20 — 529 (mypy 2.4.1); 19 з mypy 2.1.0.

---

## Етап 10 — ключі Selection у порівняннях, id у командах, `ondelete=` (P2) ✅ (2026-11-15)

**Зроблено:**
- `odoo-selection`: літерал у `==` / `!=` / `in` / `not in` з полем `Selection` має бути
  його ключем. Хуки методів `str.__eq__` / `__ne__` і `__contains__` кортежу, списку,
  множини; контекст — `ComparisonExpr`, поле — операнд `record.field`.
- `odoo-vals-type`: `Command.link(x.id)` / `(4, x.id)` / `Command.set(x.ids)` /
  `(6, 0, x.ids)`, де `x` — записи іншої конкретної моделі, ніж комодель поля; кортеж з
  кодом поза 0–6.
- `odoo-field-ref`: `ondelete=` Many2one — невідома політика, `set null` з `required=True`.

**Рішення:**
- помилка з хука порівняння на union-операнді mypy читає як «оператор не підтримується»
  (ховає або підміняє на `Unsupported operand types`), тому повідомляється повз його
  ErrorWatcher-и (`errors._watchers`, із запасним звичайним шляхом);
- ключ, який додає `selection_add` аддона поза залежностями, не помилка
  (`provider.code == "custom"`), як група в `has_group`.

**Відхилено: `rec["field"]`.** На бенчмарку 3–5 хибних помилок на версію: у
нетипізованому коді Odoo mypy вважає записом те, що є словником
(`vals['payment_values']['payment_type']`, рядки `formatted_read_group`), а тип значення
додає сторонні `assignment` / `arg-type`.

**Результати.** Бенчмарк `small` на 17–20 проти етапу 9: нові лише `odoo-selection`, по
1–4 на версію, усі — мертві ключі в самому Odoo (`sale.order.state == "done"` на 19/20,
`account.move.state in {"draft", "sent"}`, `account_type not in ("asset_bank", ...)`,
`amount_type in ("regex", "from_transaction_details")`, `payment_method == "other"`).
Тести: 17 — 523, 18 — 528, 19 — 533, 20 — 532.

---

## Інфраструктура і продуктивність (P3)

- [x] **Перевірка версії Odoo** — ✅ зроблено (крок 2). Плагін зараз мовчки працює з будь-якою версією. При
  старті читати `release.version_info` і попереджати, якщо версія не 18.
- [x] **[22] Ребра поза closure.** Виявилось зробленим попутно: ребра беруться з
  `view_modules` / `class_modules`, а ті — з `model_tips` і `_candidates`, які бачать
  лише класи addons замикання. Тест
  `test_dependency_modules_leave_out_addons_outside_the_closure` це фіксує.
- [x] **[21] Скан на диску.** ✅ `scan_store.ScanStore`: скан кожного addon зберігається
  в `RecordStore` цілим (не по файлу: скан файлу залежить від констант інших файлів
  addon, `from .consts import STATES`) разом зі штампами прочитаних файлів, порядком
  завантаження і `source_digest()` плагіна; JSON-кодек зберігає точні типи (кортежі,
  frozenset, шляхи, датакласи сканера). `Registry(settings, index, scans)` бере
  незмінені скани з диска. Замір на замиканні `sale` (Odoo 18): після правки одного файлу
  `sale` скан замикання 0,14 с замість 1,10 с; холодний запуск бенчмарку +1 % (запис),
  теплий без змін. Тести `tests/test_scan_store.py`.
- [x] **dmypy і manifest.** Manifest-и вже були в штампах індексу (`index_stamps`), тож
  `inputs_changed()` їх бачив. Додано конфіг-файл: `_config_changed` порівнює його штамп
  при кожному оновленні демона і перечитує налаштування й профіль (новий реєстр);
  невалідний конфіг повідомляється, а старі налаштування лишаються. Як і для інших
  входів реєстру, нові налаштування діють на те, що dmypy перевіряє заново (змінений
  код); результати незмінених функцій лишаються з кешу демона.
  `test_dmypy_plugin_settings_follow_config_edits`.
- [x] **Відставання кодів у кеші.** ✅ Зроблено:
  `Fingerprints._outside_definers` додає до даних модуля digest моделей файлу, яких
  closure не визначає, разом з digest штампів коренів addons (з names-запису); для
  звичайного файлу (усі моделі в closure) нічого не додається, тож кеш не скидається.
  Список моделей зберігається на файл у `RecordStore` за штампом файлу, тож теплий
  запуск нічого не парсить. Новий чи видалений addon будь-де змінює штамп кореня і
  перевіряє заново лише файли з такими моделями. Перший варіант шукав addons, що
  визначають ці моделі (`addons_outside_defining`), і сканував addons поза closure
  (файли `base` посилаються на моделі `sale`, `account`...): холодний запуск
  сповільнювався, а `test_cold_run_scans_only_addons_of_loaded_files` падав. Core-addons
  Odoo (`<odoo_path>/addons`) виключено: їхні файли згадують моделі інших addons, тож
  залежність від списку addons перевіряла б заново весь `base` після появи будь-якого
  addon (на тестах це +50 % часу), а їхні помилки зазвичай приглушені. Лишається:
  addon поза closure, який почав визначати модель лише правкою файлу (без зміни
  каталогів). Тест
  `test_cache_addon_outside_closure_added_turns_unknown_into_depends`.
  Початковий опис: Коли з'являється addon поза closure, вибір між
  `odoo-model` і `odoo-model-depends` може відставати.
  - Не можна додавати digest усього індексу визначень у кожен модуль: тоді будь-яка
    зміна будь-де інвалідує весь кеш.
  - Як вирішити: для кожної моделі, на яку файл посилається (`referenced_models`, відомо
    до перевірки) і яка лежить поза closure, додати в fingerprint назву addon, що її
    визначає.
- [x] **Застарілий overlay.** ✅ `overlay.overlay_differences(index, root)`: addon без
  посилання, посилання на видалений addon, посилання на інший каталог.
  `odoo-typing-root --check` друкує розбіжності і повертає 1 (0, якщо все збігається;
  1 і для overlay, якого не генерували). Плагін один раз за процес, коли індекс addons
  уже завантажено (`_refresh_registry`), друкує попередження з першими трьома
  розбіжностями. Бенчмарк (разом з попереднім пунктом): помилки ті самі, холодний
  запуск без змін, теплий у межах шуму (+0,01–0,03 с).
- [x] **Великі проєкти.** ✅ Виміряно (2026-11-12, Odoo 18): один запуск mypy на 120
  core-addons (перші 120 без `l10n_*`, `test_*`, `hw_*`; `--exclude
  "/(migrations|upgrades)/"`, бо `upgrades/` теж дає дублікати модулів). Без плагіна:
  холодний 25,3 с, 682 МБ, теплий 0,68 с, 34572 помилки. З плагіном: холодний 34,0 с
  (×1,34), 756 МБ (+11 %), теплий 0,92 с (+0,24 с), 21070 помилок. Співвідношення таке
  саме, як на бенчмарку окремих addons (×1,3–1,8), тож профілювання не знадобилось.

---

## Продуктивність: аналіз і оптимізація (2026-11-13) — ✅ зроблено

**Аналіз.** Базовий вимір (`small`, `--jobs 1`, Odoo 18): холодний запуск crm / sale /
account 3,14 / 4,16 / 8,70 с без плагіна і 5,26 / 7,60 / 13,00 с з ним, пік RSS
224 / 265 / 321 → 250 / 301 / 357 МБ, теплий 0,19–0,22 → 0,30–0,35 с. Таймери в процесі
(`mypy.api.run`) на холодному sale: сканування addons 1,2 с, розбір файлів поза сканом
(тести) 0,65 с, хуки ~0,8 с, пошук хуків ~0,27 с (на account 0,56 с, 207 тис. викликів),
`addons_outside_defining` на account 0,63 с (текстовий пошук 0,27 с + скани addons-
кандидатів), решта — mypy на 312 модулях (+14 %), які додають ребра залежностей. Сам
плагін тримає ~6,5 МБ (`tracemalloc`); решта приросту пам'яті — це mypy на тих модулях.

**Зроблено** (кожен крок: тести 17–20, бенчмарк без змін у помилках, порівняння полів):
- [x] `node_index.NodeIndex`: вузли файлу за класом за один обхід; `referenced_models` і
  guard-и читають лише свої класи; guard-и за один прохід. Звірено зі старою реалізацією
  на ~33 тис. файлах Odoo 17–20: однакові вузли, моделі й guard-и; обробка після розбору
  удвічі швидша.
- [x] Файли поза сканами addons розбираються раз на штамп (`_UnreachedFile`): reference-и
  і guard-и з одного дерева, і `guarded()` більше не розбирає файл ще раз.
- [x] Пошук хуків кешується за повним ім'ям (`_HookCache`), скидається з новим реєстром
  чи налаштуваннями.
- [x] `encode` / `_canonical`: поля dataclass обчислюються раз на клас, листки першими —
  у 4 / 2,9 раза швидше, однаковий JSON на всіх 657 addons; `loaded_with` кешує addons
  моделі; текстовий пошук `_name = "..."` — на 22 % швидше.
- [x] **Спільний кеш сканів** `scan_cache_dir` (за замовчуванням
  `$XDG_CACHE_HOME/odoo-typing/scans`, `""` — лише кеш mypy): скан addon-а залежить лише
  від нього самого, тож ключ — шлях і префікс addon-а, а не корені проєкту; там же
  результати файлів поза сканами. Нечитабельний каталог → кеш mypy. Бенчмарк міряє
  ізольовано (`XDG_CACHE_HOME` у каталозі запуску) і додав третій запуск *rescan*.
- [x] Знайдено й виправлено: `source_digest` хешував лише модулі верхнього рівня, тож після
  розбиття на `hooks/`, `registry/`, `scanner/` зміни там не інвалідували записи.

**Результат** (`default`, 6 addons, `--jobs 1`, стара версія з worktree в тих самих умовах):
холодний −0…−4 %, *rescan* (новий кеш mypy, ті самі скани: інший worktree чи проєкт,
видалений `.mypy_cache`) −20…−30 % (sale 7,61 → 5,58 с, account 12,86 → 10,29 с,
website_sale 8,0 → 5,87 с); накладні витрати плагіна в rescan менші в 2,2–2,6 раза.
Помилки ті самі на всіх 6 addons, теплий запуск +3–4 мс (хеш усіх модулів пакета), пік
RSS ±2 МБ. У середовищі розробки стоїть `PYTHONDONTWRITEBYTECODE=1`, тож модулі без
`.pyc` (нові підпакети) компілювались у кожному теплому запуску (~40 мс з ~0,35 с) — і
старої, і нової версії; у встановленому пакеті `.pyc` є.

**Пам'ять** (після `stop`-перевірки цілі: швидкість є, пам'ять — ні). Розклад піку на sale
(вимикаючи частини плагіна в процесі): без плагіна 263 МБ, з ним 295; ребра залежностей
(+156 модулів mypy) ~19 МБ, хуки (типи в mypy) ~6 МБ, імпорт ~4 МБ, дані реєстру решта.
Перепис даних реєстру (глибокий розмір атрибутів після запуску): 18,1 МБ, з них злиті поля
6,5 МБ (кожен вигляд моделі — свої копії тих самих полів), скани 4,5, множини методів 1,5.
- [x] Однакові злиті `FieldInfo`, словники полів (`MappingProxyType`, тільки читання),
  MRO, tips / candidates (за ідентичністю класів), множини назв — один об'єкт; `fields()` не
  копіює словник, якщо немає `related=`-comodel-ів; `has_method` шукає в ланцюжку замість
  множини методів на кожен вигляд; рядки сканів інтерновані (`decode`, аргументи полів).
  Дані реєстру 18,1 → 12,5 МБ. Пік RSS холодного запуску, стара й нова версія по черзі по
  4 рази: sale 298,2 → 292,5 МБ, account 355,4 → 348,7 МБ (−6…−9 МБ у різних замірах,
  ~20 % того, що додає плагін), час −4…−6 %. Помилки й поля ті самі.
- [x] Addons, які читаються лише для `odoo-model-depends` (на account 9: `website`,
  `website_sale`, `sale`, `pos_sale`...), тримають множину визначених моделей і штампи
  (`_defined_models`, `_peeked`; `inputs_changed` їх перевіряє), а не весь скан; попередження
  про нерозібраний файл — раз на addon. account 355,2 → 347,7 МБ (проти `b664b4d`).
- Розклад, що лишилось (sale, у процесі): «голий» плагін (лише імпорт) +1 МБ, дані реєстру
  й сканів ~+8 МБ, решта ~+23 МБ — mypy на модулях і типах, яких вимагають ребра: без
  плагіна mypy не вантажить `resource`, `hr*` тощо, а ребра до розширень `res.company` /
  `res.users` тягнуть їхні addons, а ті — свої імпорти (див. відхилене нижче).
- Межа перевірена експериментами (sale, у процесі, ті самі помилки): mypy сам викидає тіла
  функцій тихих модулів (`fastparse`, `strip_function_bodies` для `ignore_errors`), тож
  ребра до моделей з `env[...]` у тілах інших addons нічого не додають — прибрати їх: 0 МБ,
  ті самі 2562 модулі. Навіть небезпечне обрізання всіх ребер від addons поза замиканням
  цілі (`hr*` через імпорт у `resource`) дає лише −4 МБ. Решта — модулі розширень моделей
  у замиканні самої цілі (`resource` для `res.company`, `web_tour` для `res.users`...):
  без них вигляди моделей неповні, тобто це і є функціональність плагіна.

**Оцінено, відхилено:**
- Типи `env[...]` / comodel-ів у межах модуля кешувати (`view_type`, ~0,1 с): у daemon mypy
  перевикористовує дерево модуля з новими класами — ризик застарілих типів.
- Referenced models файлів поза сканами брати з дерева mypy замість `ast.parse` (~0,4 с):
  для тихих модулів (`follow_imports = "silent"`) mypy може не мати тіл функцій.
- Дешеве підтвердження «визначає модель» без скану addon-а (`addons_outside_defining`,
  ~0,36 с на account): треба повторити логіку сканера (імпорти, `_register`, `_inherit` з
  Python-баз), інакше зміниться код помилки (`odoo-model` / `odoo-model-depends`).
- Менше ребер для модулів addons поза замиканням цілей (на sale ~156 модулів через імпорт
  `hr_work_entry_contract` у `resource`): плагін не знає цілей mypy, а від ребер тихих
  модулів Odoo залежать типи, які бачить код користувача.
- Паралельне сканування: сервер обмежений, а mypy однопотоковий; виграш з'їсть пересилання
  сканів між процесами.

## Стабільність (P3) — ✅ зроблено (2026-11-15)

**Звідки.** Прохід перед першим випуском: чотири дослідження з відтвореннями (кеш сканів
на диску, інвалідація кешу mypy і dmypy, сумісність і падіння, CI і випуск), виправлення
в трьох наборах файлів, рев'ю (26 знахідок) і другий раунд виправлень.

**Знайдено** (відтворено, якщо не сказано «за кодом»):
- *Кеш сканів.*
  - Штампи скану бралися після читання: файл, збережений під час скану, лишався
    застарілим на диску (і в спільному кеші) та в демоні. Скрипт, що переписує
    `models.py` одразу після читання сканером: другий запуск без змін досі знав стару
    модель, а `inputs_changed()` давав `False`.
  - Manifest не входив у штампи скану: додані чи прибрані data-файли лишали старі XML id
    назавжди, і в dmypy.
  - Запис `view-` брав штампи manifest-ів уже після читання індексу; запис `outside-`
    звірявся лише зі штампом файлу, хоча залежить від closure: після зміни `depends` і
    видалення addon теплий запуск далі називав addon, якого вже немає.
  - Каталог пакета без `__init__.py` не мав штампа: доданий пізніше `__init__.py` не
    помічався.
  - Сховище не розділене за інтерпретатором, хоча `ast.parse` бере граматику того
    Python, що працює (за кодом).
  - Нечитабельний файл (EACCES) зберігався як помилка розбору; `chmod` / `chown` не
    змінюють штамп (mtime, розмір), тож помилка лишалась назавжди.
  - Пошкоджений запис (`decode`: `TypeError`, `IndexError`; `json.loads`:
    `RecursionError`) валив запуск замість промаху; сканер друкував `SyntaxWarning`
    (`invalid escape sequence '\d'`) на холодних запусках.
- *Інвалідація кешу mypy і dmypy.*
  - Оновлення плагіна лишало застарілими кешовані результати модулів поза addons, зокрема
    ядра Odoo: знімок плагінів у кеші mypy — `none:<sha1 plugin.py>`, бо `__version__`
    немає, а модулі поза addons не мають даних конфігурації.
  - Кеш хуків за назвою в dmypy застарівав, коли оголошення (`odoo.domain.*`,
    `DomainType` на 19+) лежить у файлі, якого реєстр не читає (`tests/`, пакет поза
    addons): після правки — хибна `arg-type` або пропущена помилка.
  - `odoo.http.request` лишався `Any` у модулі без класів на теплому запуску, а на 17–19
    і на холодному, якщо після `odoo.http` не аналізувався жоден клас моделі: перевірки
    `request.env[...]` зникали.
  - У dmypy після видалення модуля з моделями файли, що їх використовували, отримують
    хибну `import-not-found` без номера рядка, до правки файлу або перезапуску; оновлення,
    яке лише видаляє файл, реєстр не оновлює.
  - Дані конфігурації модулів самого Odoo не містили `odoo_version` (за кодом).
- *Сумісність і падіння.*
  - `plugin(version)` ігнорував версію mypy: неперевірена mypy дала б INTERNAL ERROR, що
    вказує на mypy, або мовчки пропущені помилки.
  - SaaS-копія тієї ж основної версії мовчки перевірялась профілем, що міг не підходити;
    попередження радило непідтримувану версію (`set it to 21`).
  - INTERNAL ERROR без повідомлення від парсерів: XML у Shift_JIS / GBK / EUC-JP
    (наскрізно: exit 2), невідоме кодування, NUL у записі `data`, manifest з
    нехешованим ключем або надто глибоким виразом, рядковий домен з NUL (3.11) або
    глибоким виразом, цикл symlink-ів у `odoo_path` / `addons_path` / `overlay_root`
    (`RuntimeError` на 3.11/3.12), не-UTF-8 `pyproject.toml` біля `mypy.ini`.
  - Нечитабельний каталог у корені addons (`lost+found`) давав оманливу ConfigError або
    INTERNAL ERROR залежно від порядку лістингу; цикл symlink-ів у корені робив
    збережений індекс застарілим, а індекс файлів класів ходив по циклу (дублікати або
    зависання).
  - Тести: версія без профілю пропускала всі e2e, і CI лишався зеленим
    (`ODOO_REQUIRED=1 ODOO_VERSION=21` → «2 skipped», exit 0); `run_mypy` не бачив
    попереджень плагіна; фікстура gc спиралась на налаштування gc у mypy без запобіжника.
- *CI і випуск.*
  - CI брав верхівку гілки Odoo (`--update`) з незмінним ключем кешу `odoo-<v>-v1`:
    `main` міг почервоніти без коміту в репозиторії.
  - `echo "X=$(fetch.sh)"` ховав невдалий fetch (крок зелений), а порожній `ODOO_PATH`
    тихо брав системний Odoo 18 (відтворено обидва); `fetch.sh` перебудовував `root/` при
    кожному виклику; кеш зберігався лише після успішних тестів.
  - Немає `timeout-minutes` і `concurrency`; `release.yml` публікував без тестів і
    перевірок; тестувався лише Python 3.12, і два модульні тести падали на 3.11; типові
    шляхи `bench.py` вказували в старий scratch-каталог сесії.

**Зроблено.**
- *Код.*
  - Штампи беруться перед кожним читанням (файли й каталоги скану, manifest як частина
    скану, data-файли); `view-` бере штампи індексу, `outside-` звіряється ще й з digest
    closure.
  - Записи в підкаталозі інтерпретатора: `<cache_dir>/odoo_typing/<cache_tag>/` і
    `<scan_cache_dir>/<cache_tag>/` (`cpython-312`).
  - Нечитабельний файл, manifest чи каталог отримує штамп `(-1, -1)`, якого немає в
    жодного файлу. Про файл повідомляється (`cannot parse ...`); результат без нього
    тримається, поки він нечитабельний, і файл читається знову після `chmod` / `chown` —
    у запусках і в демоні, без нового реєстру на кожне оновлення. Так для сканів,
    data-файлів, індексу файлів класів і індексу addons.
  - Каталоги кореня addons з `.` на початку і `__pycache__` — не addons і не
    штампуються (git і IDE більше не інвалідують індекс); запис, який не вдається
    stat-нути (цикл), пропускає лише себе; індекс класів не ходить по циклах.
  - `__version__ = source_digest()` у `plugin.py`: оновлення плагіна — один повний
    холодний запуск. Модулі Odoo (`odoo`, `odoo.*`, не addons) мають `odoo_version=N` у
    даних конфігурації в будь-якій розкладці.
  - `_HookCache.forget_declared()` у `set_modules`: хуки з оголошених сигнатур
    вибираються знову на кожному оновленні демона. `_type_request()` при промаху кешу
    хуків типізує `request` уже під час перевірки самого `odoo.http`.
  - Попередження: mypy поза `>=2.1,<2.5` (`TESTED_MYPY`; `tests/test_mypy_internals.py`
    перевіряє внутрішні API mypy, на які спирається плагін), проміжна (SaaS) версія Odoo,
    непідтримувана основна версія, дерево без класів профілю
    (`profile_mismatch_warning`).
  - `errors.report_internal_error`: рядок `odoo-typing: internal error while ...` перед
    INTERNAL ERROR mypy з `get_additional_deps`, `report_config_data` і перевірки addons
    на зміни.
  - Парсери: попередження компілятора приглушені (сканер, manifest-и разом з `depends`,
    рядкові домени); `MemoryError` / `RecursionError` парсера — `too complex to parse`;
    XML у багатобайтовому кодуванні декодується своїм кодеком, у невідомому —
    пропускається; цикл symlink-ів і не-UTF-8 `pyproject.toml` — ConfigError;
    пошкоджений запис — промах.
- *CI і випуск* (див. 5.2 і V2): піни `tools/odoo/commits.txt`; `fetch.sh` з перевіркою
  основної версії, версією розкладки, поверненням на пін офлайн, кількома спробами;
  `ci.yml` з Odoo 17–20, двома jobs для Python 3.11 і 3.13, таймаутами, `concurrency` і
  кешем за піном і хешем `fetch.sh`; `odoo-latest.yml`; `release.yml` лише після CI на
  тегу, з перевіркою тегу і `CHANGELOG.md`, `twine check` і `tools/release/smoke.sh`;
  `bench.yml` на піні 18.0.
- *Тестова обв'язка.*
  - `_profile_or_fail`: версія без профілю з `ODOO_REQUIRED=1` — помилка; маркер
    `odoo(...)` поза 17–20 — помилка тесту; порожній `ODOO_PATH` — помилка.
  - `run_mypy` бачить stderr плагіна; `run_mypy_in_process` і `restore_gc` (база —
    `gc.get_freeze_count()` на старті: Python 3.12 одразу має 375 заморожених об'єктів).
  - Бюджет RSS на процес pytest (`ODOO_TYPING_TEST_RSS_MB`) рахується з фізичної пам'яті
    і кількості воркерів; перевищення зупиняє весь запуск.
  - Тести Python 3.11 у `test_plugin_unit.py` підміняють `sys.version_info`.
- Документація: CHANGELOG (розділ Robustness), `docs/how-it-works.md`,
  `docs/configuration.md`, `docs/checks.md`, `docs/development.md`, README.

**Рішення:**
- Неперевірена mypy — попередження, не вихід: нова mypy часто працює, а вимога пакета
  тримає звичайне встановлення в діапазоні.
- Без запобіжника від «гоночного» mtime (git racy-clean): зі штампом до читання лишається
  лише друга правка того самого розміру в межах одного тіку mtime, а Linux 6.13+ і цього
  не дає; запобіжник «mtime ближче 2 с — не зберігати» сканував би addon двічі після
  кожного збереження і перебудовував би реєстр демона.
- Записи за тегом інтерпретатора, а не спільні: скан залежить від граматики Python.
- Odoo в CI і локально — за пінами, а не верхівка гілки: новий коміт Odoo не змінює
  результат CI; верхівки перевіряє `odoo-latest.yml`, пін переносять разом з
  виправленнями.
- Jobs Python 3.11 і 3.13 лише 17.0 / 2.1.0 / 3.11 (найстаріше разом: Python Debian 12)
  і 20.0 / 2.4.1 / 3.13 (найновіше: Python Debian 13; Odoo 20 потребує 3.12+), а не вся
  матриця на кожному Python; запропонований третій, 19.0 / 2.4.1 / 3.11, відкинуто, щоб
  лишитись на 10 jobs.

**Відхилено або лишилось:**
- Ключ спільного сховища сканів (версія плагіна, інтерпретатор, `load_key`): при
  чергуванні налаштувань записи перезаписують один одного; це лише продуктивність.
- `MemoryError` / `RecursionError` парсера зберігається як `too complex to parse`, і
  справжня нестача пам'яті теж запам'ятовується до зміни файлу (рідко).
- `__version__` через PEP 562 (`__getattr__`), щоб `dmypy run` бачив будь-яку зміну
  плагіна: тоді strict-перевірка проєкту приймала б будь-який атрибут модуля; як і
  раніше, демон бачить лише зміну `plugin.py`.
- Пін дії публікації (`pypa/gh-action-pypi-publish@release/v1`) за sha, dependabot,
  нові major-версії дій (Node 20 застаріває): на рішення власника.
- `pytest-timeout`: потребує оновлення `uv.lock` з мережею.
- Обмеження dmypy після видалення чи перейменування модуля з моделями: API плагіна не
  дає перерахувати залежності іншого модуля; описано в `docs/how-it-works.md`, обхід —
  `dmypy restart`.
- Jobs Python 3.11 і 3.13 запустились у CI 2026-11-15: 3.13 зелений, 3.11 знайшов
  помилку імпорту (значення за замовчуванням `mappingproxy` у dataclass), виправлено в
  `60f6bab`; локально на сервері лише 3.12.

**Результати.**
- Тести (`pytest -n 6`, по одній версії): mypy 2.4.1 — 17: 693, 18: 698, 19: 704, 20: 703
  passed (62–89 с); mypy 2.1.0 — 19: 704, 17: 693 passed. Lint, pylint, mypy (і з
  `--python-version 3.11` / `3.13`) чисті.
- Бенчмарк `small` на пінах 17–20 (`--jobs 2`), 0bf1e54 проти цього проходу: помилки ті
  самі до одиниці на кожному addon кожної версії; холодний, теплий і rescan-час і пік RSS у
  межах шуму (наприклад, account на 18: 14,21 → 14,05 с, 354,6 → 353,2 МБ).
- `tools/bench/thresholds-18.json` перегенеровано на піні 18.0 (5c4488a): разом 245, як і
  раніше; додано `odoo-selection: 2` (мертві ключі в Odoo), `odoo-vals-key` 4 → 3, без
  `return-value`.

---

## Покращення після дослідження (P2–P3) — ✅ зроблено (2026-11-15)

**Звідки.** Чотири дослідження на реальних addons (бенчмарк `default` / `big` на 18,
`small` на 19 і 20, cProfile, перепис об'єктів, пробні addons, fuzz): список кандидатів —
у `plans/improvements-2026-11-15.md` (локально). Зроблено те, що дає найбільше за малий
обсяг; кожен крок: тести на 17–20 (на 18 і з mypy 2.1.0), бенчмарк.

**Зроблено.**
- *Пропущені перевірки.* Де `self` — змінна типу (`Self` у тілі неанотованого `create` /
  `copy`, `-> Self`, `self: T`), mypy називає хуки `"search of BaseModel"` і дає
  отримувача як `TypeVarType`: перевірки vals, доменів, шляхів і selection там мовчали.
  `receiver_type()` бере межу змінної, `_method_fullname` розбирає такі назви (лише без
  крапки: назви класів-виглядів містять `" of "` — помилку першої версії, яка вимикала
  хуки цих класів, знайшов бенчмарк, тест `test_hooks_of_view_classes_keep_their_names`).
- *Хибні помилки.* XML id з `_update_xmlids` (259 на big-18); атрибути `request`, які
  addons ставлять під час роботи (149); `filtered(lambda r: r.partner_id)` на 19+ (~21 %
  помилок «тільки плагін» на 19/20); `Command.link(rec.id)` з `NewId` на 17/18 (141);
  присвоєння анотованим полям (`partner.country_id = False`); `mapped("m2o")` з невідомим
  comodel; ключі `selection_add` addons поза замиканням у доменах і vals; імена
  `vals_ignore_keys` (`x_*`) поза vals, крім схожих на оголошене поле; `default_x=False`;
  значення змінної з іншої гілки `if` / `match` або з гілки з `return`. Опція
  `required_values` (за замовчуванням вимкнена).
- *Пам'ять.* XML id замикання — вигляд над словником кожного addon-а; спільний порожній
  `frozenset`; ключ пулу словників полів — хеш; злитий `FieldInfo` без змін — об'єкт скану.
- *Продуктивність.* Демон тримає індекс addons і розкодовані скани між реєстрами:
  оновлення після правки 0,22 → 0,066 с. Перевірки, що лише повідомляють, не працюють у
  модулях, чиї помилки mypy ховає. Кеш `_is_core` і digest `__odoo_fields`.
- *Стабільність.* Хуки «м'які» (`fail_soft`, `ODOO_TYPING_RAISE=1` у тестах); попередження
  про `follow_imports = "skip"`, затінений addon, нечитабельні `depends`; `scan_cache_dir`
  без домашнього каталогу; baseline з `pretty = true`; хибне «analyzed twice»; `b"..."`;
  внутрішні API mypy в `tests/test_mypy_internals.py`.

**Результати** (чистий A/B, `small`, `--jobs 1`, по черзі, 18 на піні): помилки «тільки
плагін» 245 → 214 (`thresholds-18.json` оновлено: `arg-type` 88 → 58, `odoo-vals-type`
24 → 23), нових помилок 0 на `default`-18 (−154) і `small`-19 (−85); холодний −1…−3 %,
rescan −2…−5 %, теплий без змін; пік RSS `sale` −4…−8 МБ, на 120 addons −15 МБ.

**Лишилось** (див. план): ліниві імпорти (−30 мс теплого), індекс і digest-и в спільному
сховищі, шлях підозри на selection (0,5–1,2 с холодного, ~6 МБ), guard-и через тестові
хелпери, `env.ref(id, False)` для id із даних замикання, `iterparse` великих XML,
`--shadow-file` (описано в docs), CI-job «усі core-addons без падінь», fuzz-тести, jobs
mypy 2.2/2.3.

---

## Власні класи полів і списки умов домену (P2) — ✅ зроблено (2026-11-16)

**Звідки.** Перевірка стороннього проєкту (5 addons на 18: власний модуль, `rabbitmq`,
OCA `queue_job`, `web_refresher`, `web_responsive`): з 33 помилок «тільки плагін» 21 —
про поля, оголошені власним класом поля (`chat_int_id = BigInteger(...)`,
`JobSerialized`, `Serialized` з `base_sparse_field`): сканер визнавав полем лише запис
`fields.<Клас>(...)`, тож таких полів для плагіна не існувало.

**Зроблено.**
- *Клас поля — за походженням, а не за записом*
  ([scanner/field_classes.py](src/odoo_typing/scanner/field_classes.py)). Сканер веде, до
  чого прив'язане кожне ім'я файлу (імпорти, класи, `Alias = Клас`), і від чого походить
  кожен клас. Класи Odoo — ті, що має `odoo.fields`; будь-який інший клас є класом поля,
  якщо таким є його база. Тому працюють `from odoo.fields import Char`,
  `from odoo import fields as f`, `import odoo`, і підкласи з addons. Список назв класів
  ніде не зашитий.
- *Між addons.* Скан одного addon-а не бачить інших (він спільний для проєктів), тому
  клас іншого addon-а лишається в скані повним іменем, а кожен модуль записує свої класи,
  що можуть бути класами полів (`ModuleScan.classes`, з повторним експортом через
  `__init__` пакета). Реєстр зв'язує їх, коли читає скани
  (`ScannedAddons.link_classes`); ім'я, яке не виявилось класом поля, полем не стає.
- *Що означає підклас.* Поле підкласу — поле класу Odoo, від якого він походить
  (`BigInteger` → `Integer`: значення перевіряються як цілі). Якщо клас визначає
  `convert_to_*`, його значення — власні й не перевіряються; якщо `__init__` — позиційні
  аргументи не читаються.
- *Списки умов домену* ([term_lists.py](src/odoo_typing/term_lists.py)). mypy виводить
  тип списку з першої умови, і `domain.append(("company_id", "in", ids))` після
  `[("groups_id", "=", group.id)]` був `arg-type`. Методи, що додають до списку трійок
  `(поле, оператор, значення)`, приймають трійку з будь-яким значенням або оператор.

**Результати.** На тому проєкті 173 → 152 помилки (усі 21 хибна і `append`). Тести на
17–20. Бенчмарк `default`-18: нових помилок 0, зникли 2 хибні в `account` (`append` умови
з іншим типом значення; `list-item` у домені), `crm` 1172 → 1169, `account` 7222 → 7219;
холодний, rescan і теплий час без змін (A/B по черзі), RSS у межах шуму.

**Другий крок (того ж дня): усе, що лишалось.**
- *Повторний експорт.* Кожен модуль записує в скан і імена, імпортовані з інших addons
  (`FieldClass` з однією базою); ланцюжок через будь-яку кількість addons розв'язується.
  Ім'я, яке модуль лише передає далі з бібліотеки чи з Odoo, читається з його джерела.
- *Класи поза addons* ([registry/sources.py](src/odoo_typing/registry/sources.py)).
  Модуль Odoo (під налаштованим пакетом `odoo`) або бібліотеки (на шляху пошуку
  інтерпретатора) розбирається без імпорту, коли до нього веде клас поля. Класи полів
  Odoo — імена `odoo.fields`, чиї класи походять від `Field`, де б їх не було визначено
  (19+: `odoo.orm.fields_*`); `Domain(...)` чи `Command(...)` у тілі класу — не поле.
- *Фабрики.* Функція, кожен `return` якої викликає клас поля, і
  `functools.partial(<клас>, ...)` описуються як клас, що від нього походить.
- *Позиційні аргументи.* Поле класу, якого скан не бачить, зберігає їх
  (`UnlinkedField.positional`); реєстр читає їх за параметрами класу Odoo
  (`Partner("res.partner")` → comodel, `Lines("t.line", "order_id")`, ключі selection).
- *`env.cr`.* Odoo 18 оголошує `Environment.cr` як `BaseCursor`, де немає `execute`,
  `fetchall`, `dbname`: кожен `env.cr.execute(...)` був `attr-defined`. Атрибут — класи
  курсора, які модуль виводить з оголошеного класу (`Cursor | TestCursor`); назви не
  зашиті.
- *`required_values` увімкнено за замовчуванням.* Обов'язкові поля і `display_name`
  читаються без `| Literal[False]`; `required_values = false` повертає типи Odoo.

Розбір модулів поза addons зберігається в сховищі сканів зі штампом файлу (запис на
пакет `odoo`), тож наступний запуск їх не розбирає.

**Результати другого кроку.** Той проєкт: 152 → 136 помилок, «тільки плагін» 11 → 6 (усі
шість — справжні знахідки). Тести на 17–20 (18 і з mypy 2.1.0). Бенчмарк `default`
(6 addons) на пінах 17, 18, 19 і 20, `5b38777` проти цього кроку: помилки «тільки
плагін» 257 → 184, 266 → 204, 288 → 214, 324 → 243; справді нових помилок 0 на кожній
версії (3–6 на версію — ті самі помилки в тих самих рядках з іншим текстом типу:
`str` замість `str | Literal[False]`). `thresholds-18.json` (`small`): 212 → 157
(`arg-type` 57 → 28, `union-attr` 26 → 14, `index` 16 → 10). Час: A/B по черзі на 18
(`account`, `sale`) — холодний і rescan у межах шуму (±0,5 %).

*Що знадобилось, щоб нових помилок не було.* Перша версія цього кроку давала 1–2 нові
помилки на addon: mypy бере тип змінної з першого значення, тож `date = move.date`
(обов'язкове поле, тепер `date`) робив помилкою `date = line.date` (необов'язкове) в
іншій гілці, а `rows.append((tag.display_name, ...))` — `rows = [(False, ...)]`. Тому
значення, яке задає тип змінної з кількома значеннями (перше; після порожнього `[]`,
`{}`, `None` — і наступне), лишає тип, який оголошує Odoo (`LocalValues.stored`).
`env.cr` звужується лише там, де оголошений клас не має `execute` (18): на 19 звуження
до `Cursor` давало нові помилки (`execute` повертає `None`).

Демон починає новий реєстр, коли змінюється і такий файл. Параметри, які фабрика сама
передає класу (`return fields.Float(required=True, **kwargs)`), записуються
(`FieldClass.passes`): значення їх не читаються, але поле позначає ці атрибути як
невідомі (`dynamic`), тож перевірки не роблять хибних висновків.

---

## Менше хибних помилок: тести, типи Odoo 19–20, опційний код (P2) — ✅ зроблено (2026-11-16)

**Звідки.** Власник попросив зменшити рівень хибних спрацювань. Розбір усіх помилок на
шести core addons (`default`) показав, що помилки «тільки плагін» (184–243 на версію) —
мала частина: з 11 891 помилки на 18 (плагін увімкнено) 11 247 — це
`"TestX" has no attribute "partner"`. mypy вважає визначенням атрибута лише
`self.x = ...` у методі екземпляра, а тести Odoo створюють записи в `setUpClass`
(`cls.partner = cls.env[...].create(...)`): помилкою був і цей рядок, і кожне подальше
читання, а записи за ними мали тип `Any`, тобто тести не перевірялись узагалі.

**Зроблено.**
- *Атрибути, які визначає метод класу*
  ([hooks/class_attributes.py](src/odoo_typing/hooks/class_attributes.py)). Хук MRO тепер
  викликається для кожного класу: перший параметр кожного `@classmethod` позначається як
  `self` методу, і mypy сам визначає атрибути `cls.x = ...` (типи, `cls.rows = []` /
  `.append`, перевірка пізніших присвоєнь). mypy тримає такі визначення лише для
  екземплярів, тому імена зберігаються в метаданих класу (отже, і в кеші), а
  `get_class_attribute_hook` знімає обмеження перед читанням на класі. Атрибут, який
  визначає лише метод екземпляра, лишається як у mypy.
- *Запис, збережений у класі, що не є моделлю,* читається як модель, яку бачить addon,
  що читає (`self.product` зі спільного тестового класу `product` у тесті `sale` має поля
  `sale`); так само для присвоєнь і для об'єднань (`X | None`). Щоб mypy проаналізував
  власні класи addon-а для такої моделі до тесту, файл отримує залежності від моделей, які
  називають тестові модулі інших addons, що він імпортує (`inherited_view_modules`, з
  імпортами цих модулів); клас модуля, який mypy завантажив, але ще не проаналізував,
  тепер ніколи не потрапляє в злитий клас (раніше мовчки пропускався).
- *Клас, чий `__new__` будує об'єкт іншого класу*
  ([hooks/built_classes.py](src/odoo_typing/hooks/built_classes.py)): Odoo 20 оголошує
  `env.context: frozendict`, а `frozendict.__new__` повертає `MappingProxyType`, тож кожен
  `env.context.get(...)` був `attr-defined` (310 на шести addons, і без плагіна теж).
  Клас без баз отримує побудований клас як базу. `return` читається з вихідного файлу:
  mypy викидає тіла функцій модулів, чиї помилки не показує.
- *Імена пакета `odoo` на 19+* ([package_names.py](src/odoo_typing/package_names.py)):
  `odoo` там не має `__init__.py`, а `Command`, `_`, `SUPERUSER_ID` прив'язує
  `odoo/init.py` (`odoo.Command = Command`). Ті самі імпорти додаються до порожнього
  модуля пакета; назви файлу й імен не зашиті.
- *Дрібніші правила, усі виведені з оголошень Odoo:* `self.env` контролера на 20
  (`Environment | None` → `Environment`); `__getattr__ = __getitem__` (`TableSQL` на 20) —
  хук атрибутів є методом, з якого його присвоєно; `browse(record.id)` на 18 приймає і
  `NewId`, як елементи списку; `mapped(функція)` на 19+ — список значень або записи
  (Odoo оголошує об'єднання, яке не приймає жоден `sum` чи `join`), а шлях, відомий лише
  під час роботи, — `Any`; поле через `_inherits` обов'язкове, якщо таким є поле батька
  (як `Field.setup_related`); `create_date` / `write_date` читаються без `False`;
  `_inherits = {...}` на 19 (атрибут оголошено як `frozendict`).
- *Код для addon-а поза залежностями* ([guards.py](src/odoo_typing/guards.py)): модель не
  повідомляється під guard-ом поля, якого не має жодна модель залежностей
  (`if "payment_provider_id" in lines._fields:`), і у функції, яка передає назву addon-а,
  що визначає модель, у виклик (`cls.ensure_installed("sale")`); `"f" in env["model"]` —
  guard поля.
- *`env.website` (Odoo 20).* Властивість оголошено як `BaseModel` — записи без моделі,
  тож кожне поле з них було «немає атрибута» (близько 75 у контролерах `website_sale`).
  Атрибут `Environment`, оголошений як `BaseModel`, — записи моделі, чиє ім'я він має;
  скан додає таке ім'я до моделей файлу (ім'я, що не є моделлю, ребра не дає).
- *Ланцюжки `table.a.b` (Odoo 20).* `__getattr__` у `TableSQL` і `FieldSQL` оголошено
  `-> SQL`, їхня спільна база, яка такого хука не має: наступний атрибут ланцюжка був
  помилкою. Те, що дає `__getattr__`, оголошений з базовим класом власного класу, — `Any`.
- *Поле приймає те, що дає його читання.* На 20 двійкове поле читається як `BinaryValue`,
  і `tag.image = product.image_1920` був `odoo-vals-type` (18 на шести addons).
- *Список команд.* `commands = [Command.update(...)]` приймає і сирі кортежі
  (`commands.append((2, line.id))`): на 19–20 `Command` — перелік `int`.
- *Записи через два зв'язки* (`partner.user_ids` у файлі, який не називає модель
  користувачів). mypy завантажує класи моделі, якими їх бачить addon, лише для моделей,
  названих у файлі; інакше плагін брав клас, яким модель бачить addon поля, і методи, що
  їх додає інша залежність читача, були «немає атрибута» (знайдено на OCA `web`). Якщо
  наявний клас має менше класів, ніж бачить читач, запис — `Any`.
- *Перевірка встановлення модуля в умові.* Рядки в умові `if`, під якою стоїть рядок
  (або яка завершує функцію перед ним), теж називають addon:
  `if modules.search([("name", "=", "website"), ("state", "=", "installed")]):`.
- *`browse`.* Odoo оголошує `ids: int | Iterable[IdType]`, а приймає більше: `None` і
  `False` (у docstring: «int or iterable(int) or None»; будь-яке хибне значення дає
  порожній набір) і один id будь-якого виду, який може бути у списку (`record.id` на 18 —
  `int | NewId`). `browse(context.get("active_id"))` більше не помилка.
- *Поле, про яке спитали* ([hooks/asked_fields.py](src/odoo_typing/hooks/asked_fields.py)).
  `if 'phone_sanitized' in partner:` (або `in partner._fields`) — запитання, чи є поле,
  яке додає addon поза залежностями; читання під ним було «немає атрибута». mypy звужує
  `hasattr(partner, 'f')`, даючи типу додатковий атрибут, а хука для звуження не має:
  хук `__contains__` записів робить те саме, що mypy для `hasattr`
  (`TypeChecker.hasattr_type_maps`), і кладе звужений тип у binder. Запитання про модель
  (`'f' in self.env['model']`) стосується й записів цієї моделі, які функція отримує
  після нього (`env[...]`, атрибути тестового класу). `__getattr__` класу моделі не
  додається: mypy тоді вважав би записи придатними для будь-якого протоколу.
- *Словник чи список зі змішаними значеннями*
  ([hooks/mixed_values.py](src/odoo_typing/hooks/mixed_values.py)). mypy типізує
  `values = {"partner": partner, "amount": 0.0, "lines": []}` зведенням значень:
  `dict[str, object]`, а якщо серед них є об'єднання (`record.id` — `int | NewId`) —
  об'єднання всього. Кожне використання значення тоді помилка
  (`"object" has no attribute "append"`). Доки записи були `Any`, такий словник був
  `dict[str, Any]`: це найбільша група хибних помилок, яку принесла типізація записів.
  Для літерала `{...}` mypy не викликає жодного хука, тому хуки стоять на читаннях
  (`values[k]`, `.get`, `.pop`, `.setdefault`, `.items`, `.values`, `rows[0]`, ітерація):
  зведений тип у результаті замінюється на `Any`. Ознаки зведення: тип значення `object`,
  а змінна без анотації (`Var.is_inferred`), або змінна локальна і її єдине значення —
  літерал словника зі значеннями різних типів (тоді й `values[k] = x` приймає будь-що).
  Оголошений `dict[str, object]` і словник зі значеннями одного типу читаються як раніше.
  Друга частина правила: зведення до протоколу стандартної бібліотеки
  (`Container[Any]` із записів, `set()` і списку) — такий самий слід, як `object`;
  *словник-запис* — локальна змінна, яку будують лише літерали з рядковими ключами
  (`context = {"default_move_type": "out_invoice"}`, також `{}`) і яку функція потім
  змінює під рядковими ключами (`context.update({"default_partner_id": partner.id})`):
  mypy брав тип значень з першого літерала, і кожна зміна іншого типу була помилкою —
  тепер значення `Any`, а зміни приймають будь-що; контейнер без анотації зі значеннями
  `int` приймає й `float` (`defaultdict(lambda: 0)`, потім `+= line.balance`).
- *`env.ref` без відомої моделі.* Odoo 19–20 оголошують `BaseModel` (`| None`): записи без
  моделі, кожне поле яких — «немає атрибута». Для XML id, складеного під час роботи
  (`env.ref(f"account.{company.id}_tax")`), і для id, модель якого дані не називають,
  запис — `Any` (`None` лишається; `dynamic_env = "base"` повертає `BaseModel`).

**Чого плагін не чіпає.** Правила виправляють те, що оголошує Odoo, а не автор addon-а:
`env: Environment | None` у власному класі addon-а лишається необов'язковим (перша версія
правила для контролерів знімала `None` і там: у сторонньому проєкті зникли 13 помилок, які
є справжніми зауваженнями до його власного оголошення; правило обмежено класами Odoo).

**Результати** (`default`, шість core addons, `dc7704d` → зараз; усі помилки з плагіном):

| Odoo | без плагіна | було | стало | «тільки плагін» | холодний час, с |
|---|---|---|---|---|---|
| 17 | 10 903 | 9 991 | **511** | 184 → 165 | 45,8 → 48,5 |
| 18 | 18 273 | 11 891 | **592** | 204 → 175 | 51,6 → 55,2 |
| 19 | 20 175 | 13 366 | **746** | 214 → 207 | 57,5 → 60,4 |
| 20 | 22 768 | 15 268 | **1 081** | 243 → 240 | 67,3 → 70,7 |

Теплий час і RSS без змін; теплий і rescan-прогони дають ті самі помилки, що й холодний.
**Облік хибних помилок** (кожну нову помилку і кожну зниклу «тільки плагін» прочитано
за рядком коду; повний перелік — `plans/false-positives-2026-11-16.md`, локально):

| Odoo | хибних зникло | хибних з'явилось | «тільки плагін»: хибних зникло / з'явилось | правдивих з'явилось |
|---|---|---|---|---|
| 17 | 9 492 | 0 | 32 / 0 | 13 |
| 18 | 11 319 | 2 | 55 / 2 | 19 |
| 19 | 12 656 | 7 | 47 / 7 | 27 |
| 20 | 14 228 | 8 | 53 / 8 | 31 |

Зниклі хибні: атрибути тестів (9 431 / 11 224 / 12 345 / 12 992; механічно перевірено,
що тести справді присвоюють кожен такий атрибут), помилки «тільки плагін» (обов'язкові
поля через `_inherits`, `create_date`, моделі в опційному коді, `browse(record.id)`, на
19–20 `mapped`, двійкові значення, `env.website`) і базові помилки типів Odoo
(`browse(None)`; на 19–20 імена пакета `odoo`, `env.context`, `self.env` контролера,
`table.field`). Правдивих серед зниклих немає. Нові правдиві — значення не того типу в
тестах (`'zip': 1367` у `Char`, список у `Char`, `float` в `Integer`) і читання
необов'язкових полів, які можуть бути `False`. Нові хибні, що лишились (0 / 2 / 7 / 8): на
19–20 анотації Odoo, вужчі за те, що він приймає (по 7: `Command.set([xml_id])` у
демо-даних плану рахунків, `message_post_with_source` з `mail.template`; правило для них
було б зашитим ім'ям методу), і тип змінної, який mypy бере в місці присвоєння, де хука
немає (18: 2, 20: 1). Серед помилок «тільки плагін» хибних стало менше на всіх
чотирьох версіях (−32, −53, −40, −45). Ще 0 / 5 / 6 / 18 рядків — ті самі помилки,
що й до роботи, з іншим текстом типу (на 18 і 11 із 18 на 20 вони хибні і до, і після).

**Реальні набори addons на 18** (старий плагін проти нового, поза ядром):

| Набір | було | стало | зникло | з'явилось |
|---|---|---|---|---|
| проєкт власника, 11 addons | 174 | 122 | 52 | 0 |
| OCA `web` 18.0, 51 addon | 82 | 19 | 63 | 0 |
| проєкт із першого звіту, 5 addons | 136 | 136 | 0 | 0 |

Усі 115 зниклих — хибні (111 атрибутів тестів, `create_date + timedelta`,
`browse(context.get(...))`, модель під перевіркою встановлення модуля,
`partner.user_ids.notify_info()`). У третьому проєкті тестів із `setUpClass` немає, а
шість помилок плагіна там — правдиві знахідки.

Число «тільки плагін» тепер нижче, ніж до роботи, на всіх чотирьох версіях, хоча в
тестах уперше видно правдиві зауваження, які були сховані за `has no attribute`. З тих, що
були й до роботи, хибними лишаються переважно змінні, перевикористані під інший тип
(`products = []`, потім записи; `proposal = company`, потім `False`): mypy бере тип
змінної з першого значення вже після хука. `allow_redefinition = true` (з
`local_partial_types = true`) на 18 прибирає 97 помилок і додає 50 — опис у
[docs/checks.md](docs/checks.md).
`thresholds-18.json` (`small`): 157 → 122, а всіх помилок на `small` 10 004 → 492.
Тести: 17 — 785, 18 — 791, 19 — 804, 20 — 807.

*Що знайшлось дорогою.* Імена пакета `odoo` (19+) спершу додавались лише в
`get_additional_deps`. mypy розбирає модуль заново без цього хука, коли застарів інший
модуль його циклу імпортів (досить змінити прапорець): дерево пакета знову порожнє, і в
кеш потрапляв `odoo` без імен — `Module "odoo" has no attribute "Command"` у кожному
наступному запуску. Знайшов нестабільний тест; тепер імена додаються в поточне дерево з
кожного запиту хука (один пошук у словнику, коли готово), а запис кешу пакета залежить
від доданих імен. Перша версія цієї правки читала `odoo/init.py` з диска на кожен запит:
холодний прогін на 19–20 став у 4 рази довшим (бенчмарк показав; читається раз на запуск).
mypy викидає `classmethod` зі списку декораторів після аналізу
методу, а клас аналізує не раз (відкладений клас, оновлення демона): хук читає
`original_decorators`, інакше атрибути губились на другому проході (тест демона).
Скомпільований mypy не дає успадкувати свої visitor-и — обхід інструкцій ручний. Злитий
клас моделі, зібраний до того, як mypy проаналізував розширення цієї моделі у власному
addon-і, був мовчки неповним (тест читав поле — «немає атрибута»).

**Лишилось** (усе — типи самого Odoo або звичайна суворість mypy): на 18 з 592 помилок
135 — `var-annotated`, 106 — `X | Literal[False]` необов'язкових полів, 74 — імпорти без
стабів, 41 — `NewId`; на 20 ще `route(website=True)` (закритий `TypedDict` опцій, 93),
`clean_context(env.context)` і `with_context(env.context)` (Odoo оголошує `dict`),
`@api.depends(lambda self: ...)` (параметр оголошено як `BaseModel`). Зчитування поля під `if "f" in records:` mypy повідомляє
сам (плагін бачить guard лише у своїх перевірках).

---

## Перевірка і випуск (P3)

- [x] **Профілі бенчмарку.** ✅ `--profile small|default|big` (3, 6 і 37 core-addons;
  `--addons` має перевагу) і `--project DIR`: кожен addon каталогу з маніфестом (каталог
  додається в `mypy_path` і `addons_path`; addons, що імпортують одне одного через
  `odoo.addons.<x>`, потребують overlay-конфігу проєкту).
- [x] **Пороги в CI.** ✅ `--thresholds FILE` (JSON `{"<код>": max, "*": max}`): помилки
  «тільки плагін» усіх addons за кодом; перевищення друкує `THRESHOLD ...` і дає код
  виходу 1. `tools/bench/thresholds-18.json` — числа поточного прогону `small` на 18,
  workflow `.github/workflows/bench.yml` (вручну і щотижня). Поріг оновлюється разом
  з класифікацією нових помилок.
  Початковий опис:
  - Поріг на кількість помилок «тільки плагін» для кожного коду помилки.
  - Конфлікт: етап 2 навмисно додасть справжні помилки, тому поріг оновлюється разом з
    класифікацією (наскрізна вимога 8). Окремо він не працює.
- [x] **Інші версії Odoo.** ✅ Odoo 19 і 20 — профілі `ODOO_19` / `ODOO_20` у
  `compat.py` (V3, V4: ORM у `odoo.orm`, `Domain`, оператори доменів, декоратори), тести
  і бенчмарк на кожній; Odoo 17 — окремий пункт V5. Початковий опис:
  - **Odoo 19:**
    - `models.Constraint` і `models.Index`;
    - клас `Domain`;
    - імовірне перенесення ORM у пакет `odoo.orm` — перевірити. Якщо так, хуки, прив'язані
      до fullname (`odoo.fields.Many2one`, `odoo.api.Environment.__getitem__`), перестануть
      спрацьовувати.
    - `@api.private` уже є в 18 (перевірено), тож це не відмінність.
  - **Odoo 17:** менше анотацій у `fields.py`/`models.py`, тож потрібні власні типи
    полів.
  - **Як вирішити.** Модуль `compat.py`: fullname-и хуків, оператори доменів, назви
    декораторів залежно від `release.version_info`. Бенчмарк на кожній версії.
- [x] **Сумісність з mypy.** ✅ CI-матриця в `.github/workflows/ci.yml`: 2.1.0 і 2.4.1
  на кожній версії Odoo.
  - Перевірено на 2.1.0 і 2.4.1. Поставити `mypy>=2.1,<2.5` — ✅ зроблено (крок 2).
    Mypy поза цим діапазоном отримує попередження (див. «Стабільність»).
  - CI-матриця: мінімальна і остання версія.
  - Внутрішні API в коді позначені коментарями: `calculate_mro`, `info.mro`,
    `is_incomplete_namespace`, створення `TypeInfo`.
- [x] **Тести залежать від Odoo на хості.** ✅ `ODOO_REQUIRED=1`: Odoo, якого не знайдено
  (або чию версію не прочитано), робить e2e-тести помилкою (`conftest._skip_or_fail`), а
  не пропуском; з «Стабільності» — і версія без профілю. Описано в README (Development).
- [~] **Метадані пакета і випуск.** ✅ classifiers, keywords, `py.typed` (пакет
  `Typing :: Typed`), `CHANGELOG.md`, `.github/workflows/release.yml` (тег `v*` → увесь
  CI → збірка і перевірки → PyPI через trusted publishing, середовище `pypi`; див.
  «Стабільність»); `uv build` збирає wheel і sdist.
  Ліцензія ✅ LGPL-3.0-or-later (рішення власника): `license` і `license-files` (PEP 639),
  `LICENSE` (LGPL-3) і `COPYING` (GPL-3), обидва в wheel і sdist, розділ License у README.
  ✅ (2026-11-13) `authors`, рядок copyright у README і CHANGELOG, `project.urls` на
  репозиторій GitHub (remote `origin`).
  **Не зроблено, відкладено рішенням власника:** налаштування trusted publisher на PyPI,
  тег `v0.1.0` і випуск — не зараз
  (`release.yml` готовий, публікація лише за командою власника).

---

## Залежності і порядок робіт

```
Наскрізні вимоги ──────────────────────────────┐ (діють завжди)
Етап 1: індекс полів і методів ─┬─> 2 vals ─────┼─> 5.1 stubs (TypedDict з 2)
                                ├─> 3.2 related ─> 3.3 depends ─> 3.4 домени ─> 3.5 mapped
                                ├─> 3.1 compute/inverse
                                ├─> 4.1 службові поля, 4.2 Binary/Reference, 4.12 read()
                                └─> 4.8 транзитивне делегування (і навпаки: 4.8 → 1)
Незалежні, можна паралельно: 4.3, 4.4, 4.5, 4.6, 4.9, 4.10, 4.11, інфраструктура
Етап 6: 6.2, 6.5, 6.7 незалежні; 6.1 guard-и полів ─> 6.3 vals у змінній ─> 6.4 домени
        (6.3 і 6.4 ділять трекінг змінної); 6.6 — після 6.1 (той самий guards.py)
```

**Рекомендований порядок:**
1. Наскрізні вимоги 5 (коди помилок) і 6 (опції), плюс перевірка версії Odoo. Це мала
   робота, і від неї залежать усі наступні етапи.
2. Етап 1 разом з 4.8 (без транзитивного делегування індекс неповний).
3. Етап 2: 2.1 → 2.2 → 2.3 → 2.5 → 2.4 → 2.6.
4. Паралельно з етапом 2 (інші файли): 4.3, 4.10, 4.1, 4.4.
5. Етап 3: 3.2 → 3.1 → 3.3 → 3.4 → 3.5.
6. 4.11 (xml-id) і 4.5 (контролери). Вони незалежні і потребують прототипів.
7. 5.1 stubs: після етапу 2, бо використовує TypedDict.
8. Решта етапу 4, інфраструктура, випуск.
9. ✅ Етап 6 (додано і зроблено 2026-11-12): 6.2 → 6.1 → 6.5 → 6.3 → 6.4 → 6.6 → 6.7;
   6.8 відхилено.
10. Етап 7 ✅ (зроблено 2026-11-13; два раунди дебатів,
    ACCEPT з умовами): 7.1 → 7.2 → 7.3 → 7.4 → 7.5 → 7.6.
11. Етап 8 ✅ (зроблено 2026-11-14; два раунди дебатів,
    ACCEPT з умовами): 8.0 і 8.1 → 8.2 → 8.3 → 8.4 → 8.5 → 8.6 → 8.7 → 8.8 → 8.9.
12. Стабільність ✅ (зроблено 2026-11-15, після етапів 9 і 10, перед випуском): чотири
    дослідження паралельно → виправлення в трьох наборах файлів → рев'ю → другий раунд →
    документація; далі — тег `v0.1.0` (розділ `## [Unreleased]` у CHANGELOG перейменувати
    на `## [0.1.0]`, інакше `release.yml` зупиниться).

**Що з уже зробленого може заблокувати наступні етапи і як цього уникнути:**

| Що зроблено | Що може зламати | Як уникнути |
|---|---|---|
| WP3: `self` — це клас, який пишемо | хибні «невідоме поле» у vals, доменах, compute | брати поля з tip view (наскрізна 3) |
| WP4: хуки на `create`/`copy`/`search` | другий хук на ту саму назву не зареєструється | вбудовувати перевірки в наявні функції (наскрізна 4) |
| WP4: дозвільна сигнатура `create(dict \| list[dict])` | TypedDict у сигнатурі зламав би змінні vals | перевіряти літерали в method hook (2.1) |
| WP5: `scan_digest` з явним переліком | нові атрибути скану не інвалідують кеш | додавати їх у digest разом з тестом кешу (наскрізна 1) |
| WP5: fingerprint лише з Python-сканів | зміни XML не видно (4.11) | stat-штампи data-файлів |
| WP6: два написання модулів | stubs і індекс з різними назвами | канонічні назви з `Settings.custom_prefix` |
| WP1: файли поза addons не перевіряються | `vals` у скриптах не перевіряються | задокументувати; за потреби опція з окремим view |
| WP2: `_evaluate` для `_name` | `selection=CONST` не обчислиться | використовувати той самий `_evaluate` в етапі 1 |
