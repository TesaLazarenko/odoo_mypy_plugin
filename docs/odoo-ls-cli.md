# odoo-ls у режимі CLI: налаштування та перевірка на пробному модулі

Перевірено 2026-11-10 з odoo-ls **1.6.2**, Odoo **18.0** (deb-пакет у `/usr/lib/python3/dist-packages/odoo`), Python 3.12, Linux x86_64.

## Швидкий старт

```bash
tools/odoo-ls/install.sh          # завантажує бінарник і typeshed у .tools/odoo-ls/1.6.2/
tools/odoo-ls/run.sh              # аналізує tests/fixtures/addons і друкує звіт
tools/odoo-ls/run.sh path/to/my_addons   # те саме для власних addons
```

Змінні середовища для `run.sh`:

| Змінна | Призначення | За замовчуванням |
|---|---|---|
| `ODOO_PKG` | тека пакета `odoo` (та, де лежить `release.py`) | визначається через `import odoo` |
| `EXTRA_ADDONS` | додаткові addons paths, через `:` | порожньо |
| `ODOO_LS_VERSION` | версія odoo-ls | `1.6.2` |
| `OUTPUT` | куди записати сирий JSON | `.tools/odoo-ls/output.json` |

Що є в `tools/odoo-ls/`:
- `install.sh` — завантаження й розкладка файлів (див. крок 1 нижче);
- `run.sh` — визначає шляхи і запускає `odoo_ls_server --parse`;
- `report.py` — розбирає JSON, друкує `файл:рядок:колонка: severity CODE повідомлення`, зіставляє знахідки з позначками `# seeded:` і повертає код виходу для CI.

## Налаштування вручну, крок за кроком

### 1. Завантажити бінарник і typeshed

Релізи лежать на https://github.com/odoo/odoo-ls/releases. Для кожної версії потрібні **два** файли:
- `odoo-<os>-<arch>-<ver>.tar.gz`, всередині лише `odoo_ls_server`;
- `typeshed.zip` зі стабами stdlib і сторонніх пакетів.

```bash
VER=1.6.2; DEST=.tools/odoo-ls/$VER; mkdir -p $DEST
curl -fsSL https://github.com/odoo/odoo-ls/releases/download/$VER/odoo-linux-x86_64-$VER.tar.gz | tar -xz -C $DEST
curl -fsSL -o /tmp/typeshed.zip https://github.com/odoo/odoo-ls/releases/download/$VER/typeshed.zip
python3 -I -c 'import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' /tmp/typeshed.zip $DEST/typeshed
```

> ⚠️ **Пастка 1: typeshed.** Бінарник шукає `typeshed/stdlib/builtins.pyi` **поруч із собою**. Корінь архіву — це вміст репозиторію typeshed (`stdlib/`, `stubs/`, ...), а не тека `typeshed/`. Тому розпаковувати треба саме в `$DEST/typeshed`. Інакше в лозі буде `Unable to find builtins at ...`, аналіз завершиться за 0.04 с і JSON буде без діагностик.
> Python тут використано, бо `unzip` встановлений не всюди.

### 2. Підготувати `--community-path`

`-c` має вказувати на теку, що **містить** пакет `odoo` (odoo-ls шукає `<path>/odoo/release.py`).

- **Git-checkout Odoo** (`~/src/odoo/odoo-bin`): `-c ~/src/odoo -a ~/src/odoo/addons -a ~/src/odoo/odoo/addons`.
- **pip/deb-встановлення**: пакет лежить у `site-packages`/`dist-packages`.

> ⚠️ **Пастка 2: `dist-packages` як community path.** Якщо передати `-c /usr/lib/python3/dist-packages`, odoo-ls пише `Public entry point ... is equal to odoo path ..., this is not supported and will be ignored`. Ця тека потрібна і як Odoo, і як джерело залежностей (werkzeug, psycopg2, ...), і коректно це не працює. Обхід: окрема тека, де є тільки симлінк:
> ```bash
> mkdir -p .tools/odoo-ls/odoo-root
> ln -sfn /usr/lib/python3/dist-packages/odoo .tools/odoo-ls/odoo-root/odoo
> ```
> У такій розкладці core addons лежать у `odoo/addons`, а не в `<root>/addons`. Попередження `Unable to find odoo addons path at .../odoo-root/addons` можна ігнорувати, якщо `odoo/addons` передано через `-a`.

### 3. Запуск

```bash
B=.tools/odoo-ls/1.6.2/odoo_ls_server
R=.tools/odoo-ls/odoo-root
$B --parse \
   -c $R \
   -a $R/odoo/addons \
   -a tests/fixtures/addons \
   -t tests/fixtures/addons \
   -o .tools/odoo-ls/output.json \
   --log-level error --color never \
   --logs-directory .tools/odoo-ls/logs
```

| Прапорець | Сенс |
|---|---|
| `-p, --parse` | не запускати LSP-сервер, а зібрати діагностики й завершитись |
| `-c, --community-path` | тека, що містить пакет `odoo` |
| `-a, --addons` | addons path, можна повторювати. **У CLI `odoo/addons` не додається автоматично** |
| `-t, --tracked-folders` | **обов'язковий**. Діагностики видаються лише для файлів у цих теках |
| `-o, --output` | JSON-звіт (за замовчуванням `output.json` у поточній теці) |
| `--log-level` | за замовчуванням `trace`, тобто дуже багатослівно. Для CI краще `error` |
| `--logs-directory` | куди писати логи (за замовчуванням `logs/` поруч із бінарником) |
| `--config-path`, `--selected-config` | взяти налаштування з `odools.toml` (див. нижче) |
| `--python` | інтерпретатор, з якого брати `sys.path` |
| `-s, --stubs`, `--stdlib`, `--no-typeshed-stubs` | власні стаби |

Повний список можна побачити через `odoo_ls_server --help`.

Аналіз 687 модулів Odoo 18 і пробного модуля зайняв **приблизно 10 с**, звіт важить 4.4 МБ, бо в ньому є події для всіх файлів, зокрема з порожніми списками.

### 4. Варіант через `odools.toml`

Замість `-c`/`-a` можна покласти шляхи в конфіг. Цей варіант перевірено, результат той самий:

```toml
[[config]]
name = "default"
odoo_path = "/abs/path/.tools/odoo-ls/odoo-root"
addons_paths = ["/abs/path/.tools/odoo-ls/odoo-root/odoo/addons", "/abs/path/tests/fixtures/addons"]
```

```bash
$B --parse -t tests/fixtures/addons --config-path odools.toml -o out.json --log-level error
```

`-t` все одно обов'язковий. Без `--config-path` odoo-ls шукає `odools.toml`, піднімаючись від tracked-тек угору: у CLI вони виконують роль workspace folders. Той самий файл підхоплюють плагіни для VS Code, Neovim і Zed.

### 5. Формат результату

```json
{"events": [
  {"type": "diagnostic", "uri": "file:///.../partner.py", "version": null,
   "diagnostics": [{"range": {"start": {"line": 7, "character": 34}, "end": {...}},
                    "severity": 1, "code": "OLS03016",
                    "message": "Field comodel_name's values does not exist"}]},
  {"type": "log", "severity": 3, "message": "Odoo version: 18.0.0"}
]}
```

- Рядки й колонки рахуються **з 0**, як у LSP. `severity`: 1 — error, 2 — warning, 3 — info, 4 — hint.
- Події `log` повідомляють про хід роботи: версію Odoo, кількість модулів, помилки читання файлів.
- Повний список кодів OLS*: https://github.com/odoo/odoo-ls/blob/release/server/src/core/diagnostic_codes_list.rs
- Рівень окремих кодів змінюється в `odools.toml` через `diagnostic_settings`, наприклад `OLS03001 = "Warning"`.

### 6. CI

> ⚠️ **Пастка 3: код виходу.** `odoo_ls_server --parse` **завжди повертає 0**, навіть коли знайдено помилки. Тому для CI треба розбирати JSON, і саме це робить `report.py`: код виходу `1`, якщо є хоча б одна діагностика рівня error.

```yaml
# приклад для GitHub Actions
- run: tools/odoo-ls/install.sh 1.6.2
- run: ODOO_PKG=$GITHUB_WORKSPACE/odoo/odoo tools/odoo-ls/run.sh addons/
```

---

## Як проводилась перевірка

### Пробний модуль

`tests/fixtures/addons/probe_mod` — мінімальний addon (`depends: ["base"]`), який розширює `res.partner` через `_inherit`. У [`models/partner.py`](../tests/fixtures/addons/probe_mod/models/partner.py) навмисно закладено **10 помилок**. Кожен такий рядок позначено коментарем `# seeded: <опис>`, щоб `report.py` міг автоматично порахувати, що знайдено, а що пропущено.

| Рядок | Код | Тип помилки |
|---|---|---|
| 8 | `fields.Many2one("res.partnr")` | структура: comodel з одруківкою |
| 9 | `fields.Float(compute="_compute_totl")` | структура: compute-методу немає |
| 10 | `fields.Char(related="x_parent_id.citty")` | структура: шлях related веде до неіснуючого поля |
| 12 | `@api.depends("x_parent_id.nmae")` | структура: шлях depends веде до неіснуючого поля |
| 15 | `rec.x_score + rec.name` | тип: `int + (str \| False)` |
| 18 | `self.env["res.partnr"]` | структура: невідома модель |
| 19 | `search([("x_scroe", "=", 1)])` | структура: поля в домені не існує |
| 20 | `create({"x_score": "oops", "nope": 1})` | тип і структура в vals: `str` замість `int`, невідомий ключ |
| 21 | `write({"x_parent_id": "abc"})` | тип у vals: рядок у Many2one |
| 22 | `self.x_parent_id.x_score.upper()` | тип: `int` не має `.upper()` |

### Команди, якими запускались інструменти

**odoo-ls**:
```bash
tools/odoo-ls/run.sh
```

**pylint-odoo** (OCA, 10.0.12):
```bash
cd tests/fixtures/addons
uvx --from pylint-odoo==10.0.12 pylint --load-plugins=pylint_odoo -d all -e odoolint --valid-odoo-versions=18.0 probe_mod
```

**mypy** 2.1.0 без плагіна, як базова лінія:
```bash
mypy --follow-untyped-imports --check-untyped-defs tests/fixtures/addons/probe_mod/models/partner.py
```
`--follow-untyped-imports` потрібен, бо в Odoo немає `py.typed`. `--check-untyped-defs` потрібен, бо методи в модулі без анотацій і без цього прапорця mypy їхнє тіло не перевіряє.

### Результати

| Рядок | Помилка | pylint-odoo | odoo-ls 1.6.2 | mypy без плагіна |
|---|---|---|---|---|
| 8 | comodel `res.partnr` | ❌ | ✅ OLS03016 | ❌ лише `Need type annotation` |
| 9 | compute-методу немає | ❌ | ✅ OLS03018 | ❌ |
| 10 | related `citty` | ❌ | ⚠️ OLS03017, але з повідомленням «not of the same type» | ❌ |
| 12 | depends `nmae` | ❌ | ❌ | ❌ |
| 15 | `int + str\|False` | ❌ | ❌ | ❌ лише `has no attribute "name"`, бо не бачить полів базової моделі |
| 18 | `env["res.partnr"]` | ❌ (тільки `no-search-all`) | ✅ OLS03002 | ❌ |
| 19 | поле `x_scroe` у домені | ❌ | ✅ OLS03011 | ❌ |
| 20 | `create` vals | ❌ | ❌ | ❌ хибне `expected list[dict]` |
| 21 | `write` m2o-значення | ❌ | ❌ | ❌ |
| 22 | `int.upper()` | ❌ | ❌ | ❌ |
| | **Разом** | **0 / 10** | **5 / 10** | **0 / 10** |

pylint-odoo знайшов лише зауваження за конвенціями OCA (`manifest-required-author`, `missing-readme`) і `no-search-all` для `search([])`.

### Висновок для проєкту

- **odoo-ls** перевіряє, чи існують моделі, comodel, методи, related і поля в доменах. Повторювати ці перевірки в mypy-плагіні як основну мету не варто.
- Жоден інструмент не ловить **помилки типів** (рядки 15 і 22) та **vals** у `create`/`write` (рядки 20–21). Це основна ніша плагіна.
- Рядок 12, шлях у `@api.depends`, odoo-ls пропускає. Це додатковий кандидат для плагіна.
- Пробний модуль — готовий регресійний набір: тести плагіна мають знаходити помилки в рядках 15, 20, 21 і 22.
