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
  - `tools/odoo/fetch.sh <версія>`: неглибокий sparse-клон без `static/`/`i18n/`
    (141–200 МБ на версію) у `~/.cache/odoo-typing/odoo/<версія>/src` і корінь з
    розкладкою deb-пакета в `.../root/odoo`. Addons там — справжні каталоги з жорсткими
    посиланнями (`cp -al`), бо через symlink-и mypy називав файли двічі.
  - Тести: `ODOO_PATH` (і за потреби `ODOO_VERSION`; без нього версію читають з
    `release.py`), маркер `@pytest.mark.odoo(...)`. Для версії без профілю наскрізні
    тести пропускаються з причиною. На git-копії 18.0 проходять усі 304 тести.
  - Бенчмарк: `--odoo-version`, корінь з `fetch.sh` за замовчуванням, звіти й базова лінія
    в `<bench-dir>/odoo-<версія>/` (для 18 розкладку не змінено).
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
- [ ] **V5. Odoo 17.**
  - Odoo нічого не типізує, тож типи полів має давати сам плагін за класом поля, а comodel
    — за назвою атрибута. Потребує етапу 1 (індекс полів).
  - Без етапу 1 часткова підтримка вже можлива: реєстр, MRO, `env[...]`, `odoo-model`.
    Поля в такому режимі читаються як Any.
- **Наскрізне правило.** Кожна нова зашита назва Odoo додається в `OdooProfile`, а не
  константою в модулі.

---

## Етап 1 — фундамент: індекс полів і методів (P1, передумова для етапів 2–5)

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
- [~] **1c. Звірка з Odoo.** `tools/fields/dump_runtime.py` (інтерпретатором Odoo, без
  бази) і `tools/fields/compare.py` порівнюють визначення класів. Odoo 18, 3749 класів,
  14864 поля: 18325 аргументів збігаються, розбіжностей 0; методи збігаються в усіх
  класах. Лишається 52 `selection` і 2 `selection_add`, які сканер не обчислює
  (константи з інших addons, comprehension-и, функції модуля), і поле
  `IrRule.global` (`setattr` поза класом). Не зроблено: звірка злитого
  `env[model]._fields` — потрібна база PostgreSQL.

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

### 5.1 IDE: Pyright, PyCharm, VS Code
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
- [x] Шаблони CI: `.github/workflows/ci.yml` (lint; тести на Odoo 18/19/20 × mypy 2.1.0
  і 2.4.1, `ODOO_REQUIRED=1`, Odoo з `tools/odoo/fetch.sh` у кеші), `bench.yml` (пороги),
  `release.yml`; Docker-образ з джерелами Odoo і плагіном — `docs/ci/Dockerfile`.
  Workflow-и не запускались: у проєкту немає GitHub-remote.
- [x] Baseline-режим: `odoo-typing-baseline record|check -- <аргументи mypy>`
  (`baseline.py`): файл лічильників ключів `path:code:message` (без номера рядка),
  `check` друкує лише помилки понад baseline і повертає 1.
- [x] Документація: розділ README «Error codes» (таблиця кодів і підкодів, типові хибні
  спрацювання, обхідні шляхи, як вимикати) і «Integration».

---

## Інфраструктура і продуктивність (P3)

- [x] **Перевірка версії Odoo** — ✅ зроблено (крок 2). Плагін зараз мовчки працює з будь-якою версією. При
  старті читати `release.version_info` і попереджати, якщо версія не 18.
- [x] **[22] Ребра поза closure.** Виявилось зробленим попутно: ребра беруться з
  `view_modules` / `class_modules`, а ті — з `model_tips` і `_candidates`, які бачать
  лише класи addons замикання. Тест
  `test_dependency_modules_leave_out_addons_outside_the_closure` це фіксує.
- [ ] **[21] Скан на диску.** Інкрементальний запуск після правки одного файлу пересканує
  closure. `records.py` уже зберігає digest-и. Розширити його до збереження самих сканів
  кожного файлу з ключем `(шлях, stat, версія плагіна)`.
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
- [ ] **Великі проєкти.** Виміряти на 100+ addons. Якщо вийде за межі наскрізної вимоги
  7, профілювати `model_tips`, `linearization` і `view_class`.

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
- [ ] **Інші версії Odoo.**
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
  - CI-матриця: мінімальна і остання версія.
  - Внутрішні API в коді позначені коментарями: `calculate_mro`, `info.mro`,
    `is_incomplete_namespace`, створення `TypeInfo`.
- [x] **Тести залежать від Odoo на хості.** ✅ `ODOO_REQUIRED=1`: Odoo, якого не знайдено
  (або чию версію не прочитано), робить e2e-тести помилкою (`conftest._skip_or_fail`), а
  не пропуском. Описано в README (Development).
- [~] **Метадані пакета і випуск.** ✅ classifiers, keywords, `py.typed` (пакет
  `Typing :: Typed`), `CHANGELOG.md`, `.github/workflows/release.yml` (тег `v*` → збірка
  → PyPI через trusted publishing, середовище `pypi`); `uv build` збирає wheel і sdist.
  **Чекає рішення власника:** ліцензія (`license` і файл `LICENSE`) і `project.urls` (у
  репозиторію немає remote), а також налаштування trusted publisher на PyPI — сам
  випуск не робився.

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
