# Паспорт проєкту: CRSF → WiFi → MAVLink RC-місток

> Документ-специфікація для передачі в Claude Code. Мета — щоб Claude Code створив
> необхідні інструменти, налаштував пристрої й протестував систему через SSH.
> Технічні ідентифікатори, назви повідомлень і код навмисно англійською.

---

## 1. Огляд

Система передає керування з апаратного RC-пульта на польотний контролер (FC) з ArduPilot
через звичайну домашню WiFi-мережу, без класичного RC-приймача на борту.

Ланцюг даних:

```
[Taranis X9D+]                [RPi Zero 2 W]              [Jetson Orin Nano]           [FC / ArduPilot]
  External RF = CRSF  --UART-->  crsf_gateway   --UDP/-->    mavlink_bridge   --MAVLink2-->  RC input
  (module bay, 3.3V)   3.3V      (parse 16ch)     WiFi       (scale + override)   serial
```

- **RPi Zero 2 W** читає CRSF з відсіку зовнішнього модуля, розпаковує 16 каналів,
  шле їх по UDP у домашню мережу.
- **Jetson Orin Nano** приймає UDP, конвертує канали в мікросекунди, віддає їх у FC
  як `RC_CHANNELS_OVERRIDE` (MAVLink 2), і тримає `HEARTBEAT` як GCS.
- **FC (ArduPilot)** приймає керування по MAVLink і застосовує його як RC-вхід пілота.

**Жорстка вимога:** підтримка **16 каналів** наскрізь.

---

## 2. Апаратна частина

| Пристрій | Роль | Ключове |
|---|---|---|
| FrSky Taranis X9D Plus | джерело RC | OpenTX/EdgeTX, External RF = **CRSF**; baud конфігурований |
| Raspberry Pi Zero 2 W | ground-side gateway | читає CRSF по UART (PL011), шле UDP |
| Jetson Orin Nano | air-side companion | UDP → MAVLink, з'єднання з FC |
| FC з ArduPilot | споживач керування | MAVLink 2 на serial-порту |

### 2.1 Під'єднання Taranis → RPi

CRSF від передавача — **не інвертований UART, 8N1, рівень 3.0–3.3 В**, тож сигнал
заходить прямо на GPIO без level-shifter. Для читання достатньо RX + GND.

- Taranis **PPM (сигнал)** → RPi **GPIO15 / RXD** (фіз. пін 10)
- Taranis **GND** → RPi **GND** (фіз. пін 6)
- ⚠️ Контакт **BATT** у відсіку — це ~8.3 В. **Не підключати до GPIO** (спалить Pi).
  Перед пайкою прозвонити мультиметром і впевнитись, який пін — PPM, а який — BATT.
- RPi живиться **окремо** (свій 5 В по USB); з пультом спільний тільки GND.

### 2.2 Під'єднання Jetson → FC

MAVLink по serial — один із варіантів (уточнити в конфігу):
- **USB**: FC як `/dev/ttyACM0` (найпростіше для стенду), або
- **UART GPIO**: Jetson `/dev/ttyTHS1` ↔ FC TELEM-порт (TX↔RX, RX↔TX, спільний GND).

---

## 3. Компоненти, які треба створити (deliverables)

### 3.1 На RPi Zero 2 W — пакет `rpi_gateway`
1. **UART setup** — зміни в `config.txt` + скрипт (див. §6.1).
2. **CRSF parser** — розбір потоку в кадри, валідація CRC, розпакування 16 каналів.
3. **UDP sender** — пакування каналів у пакет (§5.2) і відправка.
4. **Config** (YAML/ENV) — параметри §7.
5. **systemd service** — автозапуск, авто-рестарт.
6. **Tests** — §8.

### 3.2 На Jetson Orin Nano — пакет `jetson_bridge`
1. **UDP receiver** — прийом, валідація, детекція втрат за seq.
2. **Channel scaler** — CRSF (11-bit) → PWM мкс.
3. **MAVLink sender** (pymavlink) — `HEARTBEAT` (GCS) + `RC_CHANNELS_OVERRIDE`.
4. **Watchdog / failsafe** — див. §4 (ключове для безпеки).
5. **Config** — параметри §7.
6. **systemd service** — автозапуск, авто-рестарт, авто-reconnect до FC.
7. **Tests** — §8.

### 3.3 Спільне
- **`protocol/link_protocol.py`** — єдине джерело правди для формату UDP-пакета
  (використовують обидві сторони). Плюс `PROTOCOL.md`.
- **`tools/crsf_replay.py`** — генератор синтетичних CRSF-кадрів / реплей запису
  для тестів без пульта.
- **`tools/latency_probe.py`** — вимірювання наскрізної затримки.
- **`deploy/`** — скрипти розгортання по SSH (rsync/scp + install).
- **`README.md`** — інструкції запуску й тестування.

Рекомендована структура репозиторію:

```
crsf-mavlink-link/
  README.md
  protocol/            link_protocol.py, PROTOCOL.md
  rpi_gateway/         crsf_parser.py, crsf_reader.py, config.yaml,
                       setup_uart.sh, crsf-gateway.service, tests/
  jetson_bridge/       udp_receiver.py, channel_scaler.py, mavlink_sender.py,
                       bridge.py, config.yaml, mavlink-bridge.service, tests/
  tools/               crsf_replay.py, latency_probe.py
  tests/integration/
  deploy/              deploy_rpi.sh, deploy_jetson.sh
```

---

## 4. Failsafe і безпека (читати уважно)

Оскільки фізичного RC-приймача немає, **звичайний RC-failsafe по втраті кадрів не є
основним механізмом**. Стратегія:

**Основний failsafe — GCS heartbeat.** Jetson шле `HEARTBEAT` тільки поки наскрізний
канал пілота живий. Якщо рветься будь-яка ланка — Taranis→RPi (немає CRSF-кадрів),
RPi→Jetson (немає UDP), або сам Jetson падає — heartbeat припиняється, і на FC
спрацьовує **GCS failsafe**. Це напряму відповідає «лінк упав».

Реалізація watchdog на Jetson:
- якщо немає свіжого валідного UDP-пакета протягом `link_timeout_ms` (типово 500 мс) →
  **припинити слати `HEARTBEAT` і `RC_CHANNELS_OVERRIDE`**;
- при відновленні потоку — відновити обидва.

**Вторинний backstop** — `RC_OVERRIDE_TIME` на FC: якщо оверрайди зникли, FC за кілька
секунд перестає їх застосовувати.

> Ця схема (обрив наземного лінку → failsafe) відповідає перевіреному патерну
> EZ-WifiBroadcast. Але вона **обов'язково валідовується на стенді** (див. §8, рівень 4).

**Обов'язкові стендові перевірки перед будь-яким польотом (пропелери зняті):**
- вимкнути WiFi → FC входить у GCS failsafe у межах таймауту;
- вимкнути живлення RPi → те саме;
- від'єднати сигнальний пін від Taranis → те саме;
- вбити кожен процес по черзі → те саме;
- переконатися, що дія failsafe на FC (RTL/Land/…) — саме та, що очікується.

> Домашня WiFi годиться для розробки й стенду. Для реального польоту треба окремо
> оцінити надійність/дальність/регуляторику — цей паспорт покриває валідацію на
> домашній мережі, а не політ.

---

## 5. Специфікації протоколів (точні — щоб не вгадувати)

### 5.1 CRSF (сторона RPi)

**Serial:** `/dev/ttyAMA0` (PL011), лише RX. Baud — параметр, має збігатися з
налаштуванням Taranis. Рекомендація: **921600** (нативний Linux baud, без кастомного
бодрейту) або стандартний CRSF **416666**.

**Формат кадру:** `[addr][len][type][payload...][crc8]`
- `addr` — приймати **0xEE** (вихід EdgeTX з боку пульта) **і 0xC8** (стандарт).
- `len` — кількість байтів після `len` (тобто `type + payload + crc`). Повний кадр = `len + 2`.
- `type` — цільовий **0x16** = `RC_CHANNELS_PACKED`.
- `crc8` — **DVB-S2, поліном 0xD5**, рахується по `type + payload` (тобто `frame[2:-1]`).

**CRC8 (reference):**
```python
def crc8_dvb_s2(crc, b):
    crc ^= b
    for _ in range(8):
        crc = ((crc << 1) ^ 0xD5) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc

def crsf_crc(data):        # data = type + payload
    c = 0
    for b in data:
        c = crc8_dvb_s2(c, b)
    return c
```

**Розпакування каналів (0x16, payload = 22 байти, 16×11 біт, LSB-first):**
```python
def unpack_channels(payload):          # payload: 22 bytes -> list[16] of 0..2047
    bits, nbits, ch = 0, 0, []
    for b in payload:
        bits |= b << nbits
        nbits += 8
        while nbits >= 11:
            ch.append(bits & 0x7FF)
            bits >>= 11
            nbits -= 11
    return ch[:16]
```

**Вимоги до парсера:** буферизація байтів; ресинхронізація при неправильному CRC або
невідомому `addr`/`len`; коректна збірка розірваних між читаннями кадрів; парсер
**ніколи не падає** на сміттєвому вході.

### 5.2 UDP-пакет (єдиний формат для обох сторін)

Little-endian, фіксовані 48 байтів:

| Offset | Field | Type | Опис |
|---|---|---|---|
| 0 | `magic` | uint16 | константа (напр. 0x4C43) для відсіву чужих пакетів |
| 2 | `version` | uint8 | версія протоколу = 1 |
| 3 | `flags` | uint8 | bit0 = data_valid; решта reserved |
| 4 | `seq` | uint32 | монотонний лічильник (детекція втрат/порядку) |
| 8 | `t_us` | uint64 | timestamp джерела (monotonic, мкс) для вимірювання затримки |
| 16 | `channels` | uint16 × 16 | **сирі CRSF-значення 0..2047** (не мкс) |

- RPi шле **один пакет на кожен успішно розпарсений `RC_CHANNELS_PACKED`** (частота
  йде за пультом), інкрементуючи `seq`.
- Скейлінг у мкс робиться на Jetson (RPi лишається «тупим», параметри скейлу — централізовано).

### 5.3 MAVLink (сторона Jetson)

- **MAVLink 2** обов'язково (16 каналів вимагають extension-полів). У pymavlink —
  форсувати MAVLink2 (напр. `os.environ['MAVLINK20']='1'` до імпорту, або відповідний dialect).
- З'єднання: `mavutil.mavlink_connection(<device>, baud=<baud>)`; спершу `wait_heartbeat()`,
  щоб узнати `target_system` / `target_component` FC.
- **`source_system`** мусить збігатися з `SYSID_MYGCS` на FC (типово **255**) — інакше
  ArduPilot мовчки ігнорує оверрайди. `source_component` — GCS-компонент (напр. 190).
- **`HEARTBEAT`** ~1 Гц: `type = MAV_TYPE_GCS`, `autopilot = MAV_AUTOPILOT_INVALID`.
- **`RC_CHANNELS_OVERRIDE`** з частотою `override_rate_hz` (типово 50 Гц) з останніх каналів:
  `chan1_raw..chan16_raw` = масштабовані мкс; `chan17_raw/chan18_raw` = **0 (ignore)**.
  > Семантику 0/UINT16_MAX для extension-каналів (9–18) **перевірити** по актуальному
  > `common.xml` і на SITL (0 = ignore, 65535 = release; для розширених полів є нюанс).

**Скейлінг CRSF → мкс (reference):**
```python
def crsf_to_us(v):                     # 172->988, 992->1500, 1811->2012
    return int(round((v - 992) * 5 / 8 + 1500))
# після цього clamp у [us_min, us_max] (типово 988..2012, конфігуровано)
```

---

## 6. Налаштування пристроїв

### 6.1 RPi Zero 2 W — UART (скрипт `setup_uart.sh` має це робити)

У `/boot/firmware/config.txt` (або `/boot/config.txt` на старіших):
```
enable_uart=1
dtoverlay=disable-bt
```
Плюс:
- вимкнути serial-консоль (прибрати `console=serial0,115200` з `cmdline.txt`);
- `sudo systemctl disable --now hciuart`.

Після цього PL011 доступний як `/dev/ttyAMA0` на GPIO14/15.

### 6.2 FC / ArduPilot — параметри (виставляє користувач; задокументувати в README)

- `SERIALx_PROTOCOL = 2` (MAVLink2) на порту до Jetson; `SERIALx_BAUD` відповідно (напр. 921).
- `SYSID_MYGCS = 255` (або під `source_system` містка).
- `FS_GCS_ENABLE = 1` + обрана дія GCS-failsafe (RTL/Land/…) під тип апарата.
- `RC_OVERRIDE_TIME` — таймаут оверрайдів (backstop).
- **RC-калібрування**: `RCx_MIN/MAX/TRIM` мають відповідати діапазону мкс, який віддає
  місток (або відкалібрувати, поки місток подає стіки).
- `ARMING_CHECK`: без фізичного RX частина RC-перевірок може лаятись — **свідомо**
  розібратися, які застосовні (не вимикати все підряд).

> Точні імена/діапазони параметрів звірити з докою під конкретний тип апарата й версію
> прошивки — вони трохи різняться між Copter/Plane/Rover.

---

## 7. Параметри конфігурації (мають бути винесені в config)

**RPi (`rpi_gateway/config.yaml`):**
`serial_port`, `baud`, `accept_sync_bytes` (за замовч. [0xEE, 0xC8]),
`udp_target_ip`, `udp_target_port`, `log_level`.

**Jetson (`jetson_bridge/config.yaml`):**
`udp_listen_ip`, `udp_listen_port`, `mavlink_device`, `mavlink_baud` (або UDP-endpoint),
`source_system` (=SYSID_MYGCS), `source_component`, `num_channels` (=16),
`override_rate_hz` (=50), `heartbeat_rate_hz` (=1), `link_timeout_ms` (=500),
`us_min`/`us_max` (clamp), `log_level`.

---

## 8. План тестування через SSH

Claude Code розгортає код і ганяє тести по SSH на обох пристроях.

**Доступ / деплой (assumptions, винести в `deploy/`):**
- SSH до `rpi-zero` (user `pi`) і `jetson` (user за замовч.), обидва в одній LAN.
- Деплой: `git clone` або `rsync` з інсталяцією залежностей (`pyserial`, `pymavlink`,
  `pyyaml`).

**Рівень 1 — unit (без заліза, на пристрої або хості):**
- CRSF parser: подати рукотворні кадри (вкл. приклад `RC_CHANNELS_PACKED` з відомими
  значеннями каналів) → звірити розпаковані канали; тест CRC pass/fail; ресинк після
  сміття; збірка розірваного кадру.
- CRC8 DVB-S2 — known-answer.
- **round-trip pack↔unpack** каналів як KAT.
- Скейлінг: якорі 172→988, 992→1500, 1811→2012.
- UDP-пакет encode↔decode round-trip.
- Побудова `RC_CHANNELS_OVERRIDE` → декод → звірка полів (16 каналів, MAVLink2).

**Рівень 2 — loopback/інтеграція (без FC і без пульта):**
- `crsf_replay.py` подає синтетичні CRSF-кадри у парсер (через pty / virtual serial) →
  UDP → місток → мок-приймач MAVLink. Перевірити наскрізне проходження каналів.
- Прогнати RPi-sender і Jetson-bridge на реальних пристроях через реальну WiFi, подати
  синтетичний CRSF на RPi, зловити MAVLink на Jetson. Виміряти **втрати (seq)** і
  **затримку (t_us)**.

**Рівень 3 — SITL:**
- Підняти **ArduPilot SITL**; спрямувати місток на нього. Слати оверрайди, читати назад
  `RC_CHANNELS` (MAVProxy/pymavlink) → звірити, що канали відповідають стікам.
- Failsafe: зупинити подачу → підтвердити, що SITL логує GCS failsafe / завершення оверрайдів.

**Рівень 4 — стенд із залізом (ручний, пропелери зняті):**
- Справжній Taranis → RPi → Jetson → FC. У Mission Planner (екран RC-калібрування)
  перевірити коректний рух **усіх 16 каналів**.
- Перевірки failsafe з §4 (обрив WiFi, живлення RPi, сигналу, процесів).

**Метрики й логи:**
- Наскрізна затримка (p50/p95/max) через `latency_probe.py`.
- Обидва сервіси логують у journald; тести збирають логи по SSH.

---

## 9. Нефункціональні вимоги

- **16 каналів** наскрізь (hard).
- Частота оверрайдів ≥ **50 Гц** (конфігуровано).
- Watchdog-таймаут за замовч. **500 мс** (конфігуровано).
- Затримку **виміряти** й задокументувати (орієнтир — десятки мс на домашній WiFi).
- Стійкість: парсер не падає на сміттєвому вході й авто-ресинхронізується; місток
  авто-reconnect до FC і повторно відкриває serial при обриві.
- Обидва застосунки — **systemd services** з авто-рестартом.

---

## 10. Відкриті рішення (підтвердити перед реалізацією)

1. **Baud Taranis→RPi**: 921600 (нативний, простіше) чи 416666 (стандартний CRSF)?
2. **IP-адресація**: статичні IP чи mDNS/hostnames?
3. **Підключення FC до Jetson**: USB (`/dev/ttyACM0`) чи UART (`/dev/ttyTHS1`) + який baud?
4. **Дія GCS-failsafe** на FC: RTL / Land / інше?
5. **Частота оверрайдів** і **watchdog-таймаут**: лишити 50 Гц / 500 мс?
6. **SSH-хости/креденшали** для деплою й тестів.
