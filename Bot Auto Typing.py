import ctypes
import time
import random
import threading
import queue
from flask import Flask, request, jsonify
from flask_cors import CORS
import telebot
import requests
from telebot import types
from pynput.keyboard import Controller, Key

# ----------------- Flask-сервер -----------------

app = Flask(__name__)
CORS(app)

words_queue = queue.Queue()
stop_event = threading.Event()
typing_thread = None

keyboard = Controller()

# ----------------- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ -----------------

parsing_enabled = True   # Разрешён ли автопарсинг из Tampermonkey
force_parse = False      # Принудительный парсинг (Tampermonkey сбросит индекс)

# CHANGED: память по дефолту = False
memory_enabled = False   # Если False, Tampermonkey не ведёт lastSentIndex

errors_enabled = False   # Включить ли "ошибки" при печати
error_chance = 1         # Шанс ошибки (в %)
custom_delay = 0.0       # Доп. задержка к каждому символу (секунды)

continue_mode = False    # Если False, бот останавливается после окончания очереди

typed_words = []         # Лог уже напечатанных слов/символов

speed_settings = {
    'slow': (0.21, 0.30),
    'medium': (0.13, 0.20),
    'fast': (0.08, 0.12),
    '0.01': (0.00, 0.01)
}
current_speed = 'medium'
min_delay, max_delay = speed_settings[current_speed]
speed_lock = threading.Lock()

# CHANGED: карта для «ручного» ввода точки, запятой и т. д. в RU-раскладке
# Здесь для простоты показываем только точку и запятую.
# Идея: если текущая раскладка — RUS, и символ = '.' -> нажать Shift+ю.
#       если символ = ',' -> нажать Shift+б.
# При желании можно добавить ещё символы.
ru_punct_map = {
    '.': 'ю',  # Shift + ю = точка
    ',': 'б',  # Shift + б = запятая
}

# ----------------- Работа с раскладкой (WinAPI) -----------------

LANG_ENGLISH = '0409'
LANG_RUSSIAN = '0419'

user32 = ctypes.WinDLL('user32', use_last_error=True)

def get_current_layout():
    """Вернёт код раскладки (строка '0409' или '0419' и т.п.)."""
    hkl = user32.GetKeyboardLayout(0)
    lang_id = hkl & 0xFFFF
    return f"{lang_id:04X}"

def switch_to_layout(target_layout):
    """
    Переключает раскладку на target_layout (пример: '0409' = EN, '0419' = RU).
    Именно через WinAPI: LoadKeyboardLayoutW + ActivateKeyboardLayout.
    """
    try:
        hkl = user32.LoadKeyboardLayoutW(target_layout, 1)
        if not hkl:
            print(f"[ERR] Не смогли загрузить раскладку {target_layout}")
            return False
        result = user32.ActivateKeyboardLayout(hkl, 0)
        if result == 0:
            print(f"[ERR] Не смогли активировать раскладку {target_layout}")
            return False
        else:
            print(f"[OK] Раскладка переключена на {target_layout}")
            return True
    except Exception as e:
        print(f"[EXCEPT] switch_to_layout: {e}")
        return False

# ----------------- Вспомогательные функции -----------------

def get_random_delay():
    with speed_lock:
        base = random.uniform(min_delay, max_delay)
    return base + custom_delay

def determine_language_of_char(ch):
    """Определим язык символа (english, russian или other)."""
    if ch.isalpha():
        # Английские
        if 'a' <= ch.lower() <= 'z':
            return 'english'
        # Русские
        elif 'а' <= ch.lower() <= 'я' or ch in ('ё','Ё'):
            return 'russian'
    return 'other'

def determine_word_language(word):
    """Определяем язык слова (english, russian, mixed, other)."""
    langs = set()
    for c in word:
        lc = determine_language_of_char(c)
        if lc in ['english','russian']:
            langs.add(lc)
        if len(langs) > 1:
            return 'mixed'
    if len(langs) == 1:
        return langs.pop()
    return 'other'

# CHANGED: Функция печати 1 символа с учётом RU-пунктуации
def type_one_char(ch, current_layout):
    """Печатает один символ ch, учитывая, что если раскладка RUS и ch='.'/',' 
    то надо нажать Shift+ю или Shift+б соответственно.
    В остальных случаях — просто keyboard.type(ch)."""
    if current_layout == LANG_RUSSIAN and ch in ru_punct_map:
        # Нужен "Shift + символ"
        try:
            keyboard.press(Key.shift)
            keyboard.type(ru_punct_map[ch])
            keyboard.release(Key.shift)
        except Exception as e:
            print(f"[ERR] Не смогли напечатать RU-пунктуацию '{ch}': {e}")
    else:
        # Обычная печать
        keyboard.type(ch)

# ----------------- Основная функция ввода -----------------

def type_words_func():
    """
    Цикл, который:
    1. Берёт слова из очереди (words_queue).
    2. Если слово = '', жмём Enter.
    3. Иначе переключаем раскладку на язык слова (или посимвольно, если mixed).
    4. Печатаем слово через keyboard.type(ch) (с обработкой RU-пунктуации).
    5. Вставляем пробел.
    6. При errors_enabled и выпавшем шансе ошибки печатаем неверный символ + Backspace.
    7. Если continue_mode = False и очередь опустела, выходим.
    """

    original_layout = get_current_layout()
    print(f"[INFO] Запуск печати. Исходная раскладка: {original_layout}")

    while not stop_event.is_set():
        # Если выключено продолжение, и очередь пуста — останавливаемся
        if not continue_mode and words_queue.empty():
            print("[INFO] Очередь пуста, continue_mode=FALSE => выходим.")
            break

        try:
            word = words_queue.get(timeout=1)
        except queue.Empty:
            # Очередь пустая, ждём следующего слова
            time.sleep(0.1)
            continue

        if word == "":
            # Пустое слово => это Enter
            keyboard.type('\n')
            typed_words.append("<ENTER>")
            time.sleep(get_random_delay())
            continue

        # Определяем язык
        wlang = determine_word_language(word)
        print(f"[WORD] '{word}' lang={wlang}")

        # Переключаем раскладку, если english/russian
        if wlang == 'english':
            switch_to_layout(LANG_ENGLISH)
        elif wlang == 'russian':
            switch_to_layout(LANG_RUSSIAN)

        typed_correctly = True

        for ch in word:
            if stop_event.is_set():
                break

            # Если слово mixed, переключаемся посимвольно
            if wlang == 'mixed':
                ch_lang = determine_language_of_char(ch)
                if ch_lang == 'english':
                    switch_to_layout(LANG_ENGLISH)
                elif ch_lang == 'russian':
                    switch_to_layout(LANG_RUSSIAN)
                # else: other — остаёмся в текущей

            try:
                # CHANGED: Печатаем с учётом RU-пунктуации
                current_layout = get_current_layout()
                type_one_char(ch, current_layout)
                time.sleep(get_random_delay())

                # Проверяем шанс ошибки
                if errors_enabled and random.randint(1,100) <= error_chance:
                    # Печатаем случайный неверный символ
                    wrong_char = random.choice(
                        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                        "1234567890!@#$%^&*()_+-=,./;:'\"[]{}|?абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
                    )
                    keyboard.type(wrong_char)
                    time.sleep(0.2 + get_random_delay())
                    # Нажимаем Backspace
                    keyboard.press(Key.backspace)
                    keyboard.release(Key.backspace)
                    time.sleep(0.2 + get_random_delay())

            except Exception as e:
                typed_correctly = False
                print(f"[ERR] Не смогли напечатать символ '{ch}': {e}")

        if not stop_event.is_set():
            # Пробел в конце слова
            keyboard.type(' ')
            typed_words.append(word)
            time.sleep(get_random_delay())
            print(f"[OK] Слово '{word}' напечатано." if typed_correctly else f"[WARN] '{word}' c ошибками.")

    # Восстанавливаем раскладку
    switch_to_layout(original_layout)
    print("[INFO] Завершение печати.")

# ----------------- Маршруты Flask -----------------

app.config['JSON_AS_ASCII'] = False

@app.route('/words', methods=['POST'])
def route_words():
    data = request.get_json()
    if not data or 'words' not in data:
        return jsonify({"status":"error","message":"Неверные данные, нужен {words: [...]}"}), 400
    if not isinstance(data['words'], list):
        return jsonify({"status":"error","message":"words должен быть списком"}), 400

    new_words = data['words']
    for w in new_words:
        words_queue.put(w)
    print("[FLASK] Получены слова:", new_words)
    return jsonify({"status":"ok","message":"Слова добавлены в очередь"})

@app.route('/start', methods=['POST'])
def route_start():
    """
    Запуск печати. Если поток уже идёт — ошибка.
    Если поток не идёт и force_parse включён/выключен — это не важно, просто стартуем.
    Если continue_mode=False, не очищаем очередь, т.к. пользователь может заранее залить слова.
    
    # CHANGED: по условию «При /start удалять раскладку в телеграмме и создавать заново» — 
    # это касается не Flask, а самого хендлера /start в боте. 
    """
    global typing_thread
    if typing_thread and typing_thread.is_alive():
        return jsonify({"status":"error","message":"Ввод уже запущен"}),400

    stop_event.clear()
    typing_thread = threading.Thread(target=type_words_func, daemon=True)
    typing_thread.start()
    return jsonify({"status":"ok","message":"Ввод запущен"})

@app.route('/stop', methods=['POST'])
def route_stop():
    if typing_thread and typing_thread.is_alive():
        stop_event.set()
        typing_thread.join()
        # CHANGED: «очистить из памяти» напечатанное
        typed_words.clear()
        return jsonify({"status":"ok","message":"Ввод остановлен и typed_words очищены"})
    else:
        return jsonify({"status":"error","message":"Ввод не идёт"}),400

@app.route('/typed', methods=['GET'])
def route_typed():
    return jsonify({"typed_words": typed_words})

# --- АВТОПАРСИНГ & FORCE PARSE ---

@app.route('/toggle_parsing', methods=['POST'])
def route_toggle_parsing():
    global parsing_enabled
    parsing_enabled = not parsing_enabled
    return jsonify({"status":"ok","parsing_enabled": parsing_enabled})

@app.route('/parsing_status', methods=['GET'])
def route_parsing_status():
    global force_parse
    resp = {
        "enabled": parsing_enabled,
        "force": force_parse,
        "memory_enabled": memory_enabled
    }
    # После одного запроса force_parse сбросим
    if force_parse:
        force_parse = False
    return jsonify(resp)

@app.route('/force_parse', methods=['POST'])
def route_force_parse():
    """
    Принудительный парсинг должен работать всегда (даже если печатаем).
    Для гарантии — останавливаем печать, очищаем очередь, typed_words,
    ставим force_parse = True.
    """
    global force_parse
    # Останавливаем печать, если идёт
    if typing_thread and typing_thread.is_alive():
        stop_event.set()
        typing_thread.join()

    with words_queue.mutex:
        words_queue.queue.clear()
    typed_words.clear()

    force_parse = True
    return jsonify({"status":"ok","message":"force_parse=true. Очередь и typed_words очищены."})

@app.route('/toggle_memory', methods=['POST'])
def route_toggle_memory():
    global memory_enabled
    memory_enabled = not memory_enabled
    return jsonify({"status":"ok","memory_enabled": memory_enabled})

# --- ПРОЧИЕ НАСТРОЙКИ (ошибки, задержка, скорость, continue_mode) ---

@app.route('/set_error_chance', methods=['POST'])
def route_set_error_chance():
    global error_chance
    data = request.get_json() or {}
    val = data.get('value')
    try:
        valf = float(val)
        if valf < 0 or valf > 100:
            return jsonify({"status":"error","message":"Значение 0..100"}),400
        error_chance = valf
        return jsonify({"status":"ok","error_chance":error_chance})
    except:
        return jsonify({"status":"error","message":"Неверный формат"}),400

@app.route('/set_custom_delay', methods=['POST'])
def route_set_custom_delay():
    global custom_delay
    data = request.get_json() or {}
    val = data.get('value')
    try:
        valf = float(val)
        if valf<0 or valf>5:
            return jsonify({"status":"error","message":"Допустимый диапазон 0..5"}),400
        custom_delay = valf
        return jsonify({"status":"ok","custom_delay":custom_delay})
    except:
        return jsonify({"status":"error","message":"Неверный формат"}),400

@app.route('/set_speed', methods=['POST'])
def route_set_speed():
    global current_speed, min_delay, max_delay
    data = request.get_json() or {}
    val = data.get('value')
    if val not in speed_settings:
        return jsonify({"status":"error","message":"Неизвестная скорость"}),400
    with speed_lock:
        current_speed = val
        m1,m2 = speed_settings[val]
        min_delay, max_delay = m1, m2
    return jsonify({"status":"ok","speed":current_speed})

@app.route('/toggle_continue', methods=['POST'])
def route_toggle_continue():
    global continue_mode
    continue_mode = not continue_mode
    return jsonify({"status":"ok","continue_mode":continue_mode})

# ----------------- Telegram-бот -----------------

TOKEN = ''
bot = telebot.TeleBot(TOKEN)
AUTHORIZED_USER_ID = 123

def is_auth(uid):
    return uid == AUTHORIZED_USER_ID

def typing_is_running():
    return typing_thread and typing_thread.is_alive()

def get_settings_text():
    lines = []
    lines.append(f"Парсинг (авто): {'ВКЛ' if parsing_enabled else 'ВЫКЛ'}")
    lines.append(f"Принудительный парсинг: force_parse={force_parse}")
    lines.append(f"Запоминание (memory): {'ВКЛ' if memory_enabled else 'ВЫКЛ'}")
    lines.append(f"Ошибки: {'ВКЛ' if errors_enabled else 'ВЫКЛ'} (шанс={error_chance}%)")
    lines.append(f"Доп. задержка: {custom_delay} c.")
    lines.append(f"Скорость: {current_speed}")
    lines.append(f"Режим продолжения: {'ВКЛ' if continue_mode else 'ВЫКЛ'}")
    lines.append(f"Ввод идёт: {'ДА' if typing_is_running() else 'НЕТ'}")
    return "\n".join(lines)

def build_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)

    btn_toggle_parsing = types.InlineKeyboardButton(
        text=f"Парсинг: {'ВЫКЛ' if parsing_enabled else 'ВКЛ'}",
        callback_data="toggle_parsing"
    )
    btn_force_parse = types.InlineKeyboardButton(
        text="Принудит. парсинг",
        callback_data="force_parse"
    )
    btn_memory = types.InlineKeyboardButton(
        text=f"Память: {'ВЫКЛ' if memory_enabled else 'ВКЛ'}",
        callback_data="toggle_memory"
    )
    btn_errors = types.InlineKeyboardButton(
        text=f"Ошибки: {'ВЫКЛ' if errors_enabled else 'ВКЛ'}",
        callback_data="toggle_errors"
    )
    btn_continue = types.InlineKeyboardButton(
        text=f"Продолж: {'ВЫКЛ' if continue_mode else 'ВКЛ'}",
        callback_data="toggle_continue"
    )
    btn_show_typed = types.InlineKeyboardButton(
        text="Показать набранное",
        callback_data="show_typed"
    )
    btn_err_chance = types.InlineKeyboardButton(
        text="Шанс ошибки",
        callback_data="set_error_chance"
    )
    btn_delay = types.InlineKeyboardButton(
        text="Доп. задержка",
        callback_data="set_custom_delay"
    )
    btn_speed = types.InlineKeyboardButton(
        text=f"Скорость: {current_speed}",
        callback_data="show_speed_menu"
    )

    # Разбрасываем кнопки по строкам
    markup.add(btn_toggle_parsing, btn_force_parse)
    markup.add(btn_memory, btn_errors)
    markup.add(btn_continue, btn_show_typed)
    markup.add(btn_err_chance, btn_delay)
    markup.add(btn_speed)

    return markup

def build_speed_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    for spd in speed_settings.keys():
        markup.add(
            types.InlineKeyboardButton(
                text=spd,
                callback_data="speed_" + spd
            )
        )
    return markup

def redraw_menu(call):
    txt = get_settings_text()
    mk = build_main_menu()
    bot.edit_message_text(
        text=f"<b>Текущие настройки</b>:\n{txt}",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=mk
    )

# ----------------- Bot commands -----------------

@bot.message_handler(commands=['start'])
def cmd_start(message):
    """Команда /start: приветствие + пересоздание меню."""
    if not is_auth(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")

    # CHANGED: «При /start удалять раскладку и создавать заново».
    # Проще всего просто отправить новое меню. Если нужно удалить старое —
    # нужно знать старый message_id. У нас не хранится, так что делаем "как есть".
    text = ("Привет! Я бот для печати.\n"
            "Доступные команды:\n"
            "/starttyping — начать ввод\n"
            "/stopping — остановить\n"
            "/menu — показать настройки\n")
    bot.send_message(message.chat.id, text)

    # Сразу показываем текущее меню
    txt = get_settings_text()
    mk = build_main_menu()
    bot.send_message(
        message.chat.id,
        f"<b>Текущие настройки</b>:\n{txt}",
        parse_mode='HTML',
        reply_markup=mk
    )

@bot.message_handler(commands=['starttyping'])
def cmd_starttyping(message):
    if not is_auth(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    try:
        r = requests.post("http://127.0.0.1:5000/start")
        j = r.json()
        bot.reply_to(message, f"Сервер: {j}")
        # Обновляем меню
        txt = get_settings_text()
        mk = build_main_menu()
        bot.send_message(
            message.chat.id,
            f"<b>Текущие настройки</b>:\n{txt}",
            parse_mode='HTML',
            reply_markup=mk
        )
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

@bot.message_handler(commands=['stopping'])
def cmd_stopping(message):
    if not is_auth(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    try:
        r = requests.post("http://127.0.0.1:5000/stop")
        j = r.json()
        bot.reply_to(message, f"Сервер: {j}")
        # Обновляем меню
        txt = get_settings_text()
        mk = build_main_menu()
        bot.send_message(
            message.chat.id,
            f"<b>Текущие настройки</b>:\n{txt}",
            parse_mode='HTML',
            reply_markup=mk
        )
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

@bot.message_handler(commands=['menu'])
def cmd_menu(message):
    if not is_auth(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    txt = get_settings_text()
    mk = build_main_menu()
    bot.send_message(
        message.chat.id,
        f"<b>Текущие настройки</b>:\n{txt}",
        parse_mode='HTML',
        reply_markup=mk
    )

# ----------------- Инлайн-кнопки -----------------

@bot.callback_query_handler(func=lambda call: True)
def cb_inline(call):
    global parsing_enabled, force_parse, memory_enabled
    global errors_enabled, error_chance, custom_delay, continue_mode
    global current_speed, min_delay, max_delay

    if not is_auth(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа")
        return

    if call.data == 'toggle_parsing':
        try:
            rr = requests.post("http://127.0.0.1:5000/toggle_parsing")
            if rr.status_code == 200:
                j = rr.json()
                parsing_enabled = j.get('parsing_enabled', True)
                bot.answer_callback_query(call.id, f"Парсинг={parsing_enabled}")
            else:
                bot.answer_callback_query(call.id, "Ошибка toggle_parsing")
        except:
            bot.answer_callback_query(call.id, "Сетевая ошибка toggle_parsing")
        redraw_menu(call)

    elif call.data == 'force_parse':
        try:
            rr = requests.post("http://127.0.0.1:5000/force_parse")
            if rr.status_code == 200:
                j = rr.json()
                bot.answer_callback_query(call.id, f"Принудительный парсинг: {j}")
            else:
                bot.answer_callback_query(call.id, "Ошибка force_parse")
        except:
            bot.answer_callback_query(call.id, "Сетевая ошибка force_parse")
        redraw_menu(call)

    elif call.data == 'toggle_memory':
        try:
            rr = requests.post("http://127.0.0.1:5000/toggle_memory")
            if rr.status_code == 200:
                j = rr.json()
                memory_enabled = j.get('memory_enabled', True)
                bot.answer_callback_query(call.id, f"memory_enabled={memory_enabled}")
            else:
                bot.answer_callback_query(call.id, "Ошибка toggle_memory")
        except:
            bot.answer_callback_query(call.id, "Сетевая ошибка toggle_memory")
        redraw_menu(call)

    elif call.data == 'toggle_errors':
        errors_enabled = not errors_enabled
        bot.answer_callback_query(call.id, f"Ошибки={'ВКЛ' if errors_enabled else 'ВЫКЛ'}")
        redraw_menu(call)

    elif call.data == 'toggle_continue':
        try:
            rr = requests.post("http://127.0.0.1:5000/toggle_continue")
            if rr.status_code==200:
                j = rr.json()
                continue_mode = j.get('continue_mode', False)
                bot.answer_callback_query(call.id, f"continue_mode={continue_mode}")
            else:
                bot.answer_callback_query(call.id, "Ошибка toggle_continue")
        except:
            bot.answer_callback_query(call.id, "Сетевая ошибка toggle_continue")
        redraw_menu(call)

    elif call.data == 'show_typed':
        try:
            r = requests.get("http://127.0.0.1:5000/typed")
            if r.status_code == 200:
                d = r.json()
                tw = d.get('typed_words', [])
                if tw:
                    tail = tw[-20:]
                    joined = "\n".join(tail)
                    bot.answer_callback_query(call.id, "Вот что набрано в последнее время:")
                    bot.send_message(call.message.chat.id, joined)
                else:
                    bot.answer_callback_query(call.id, "Пока ничего не набрано.")
            else:
                bot.answer_callback_query(call.id, "Ошибка /typed")
        except Exception as e:
            bot.answer_callback_query(call.id, f"Сетевая ошибка /typed: {e}")

    elif call.data == 'set_error_chance':
        bot.answer_callback_query(call.id, "Пришлите шанс ошибки (0..100) в чат.")
        msg = bot.send_message(
            call.message.chat.id,
            "Введите шанс ошибки (0..100), например 5:"
        )
        bot.register_next_step_handler(msg, process_error_chance_input)

    elif call.data == 'set_custom_delay':
        bot.answer_callback_query(call.id, "Пришлите доп. задержку (0..5) в чат.")
        msg = bot.send_message(
            call.message.chat.id,
            "Введите дополнительную задержку (0..5), например 0.2:"
        )
        bot.register_next_step_handler(msg, process_custom_delay_input)

    elif call.data == 'show_speed_menu':
        sm = build_speed_menu()
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"Выберите скорость (текущая: {current_speed}):",
            reply_markup=sm
        )

    elif call.data.startswith('speed_'):
        spd = call.data.split('_',1)[1]
        if spd in speed_settings:
            try:
                rr = requests.post("http://127.0.0.1:5000/set_speed", json={"value":spd})
                if rr.status_code==200:
                    j = rr.json()
                    current_speed = j.get('speed','medium')
                    with speed_lock:
                        m1,m2 = speed_settings[current_speed]
                        min_delay, max_delay = m1,m2
                    bot.answer_callback_query(call.id, f"Скорость установлена: {current_speed}")
                else:
                    bot.answer_callback_query(call.id, "Ошибка set_speed")
            except Exception as e:
                bot.answer_callback_query(call.id, f"Сетевая ошибка: {e}")
        else:
            bot.answer_callback_query(call.id, "Неизвестная скорость")
        redraw_menu(call)

def process_error_chance_input(message):
    if not is_auth(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    valstr = message.text.strip()
    try:
        valf = float(valstr)
    except:
        return bot.reply_to(message, "Ошибка: введите число 0..100.")

    try:
        rr = requests.post("http://127.0.0.1:5000/set_error_chance", json={"value":valf})
        if rr.status_code==200:
            j = rr.json()
            global error_chance
            error_chance = j.get('error_chance', 1)
            bot.reply_to(message, f"Шанс ошибки: {error_chance}%")
        else:
            bot.reply_to(message, f"Ошибка: {rr.text}")
    except Exception as e:
        bot.reply_to(message, f"Сетевая ошибка: {e}")

def process_custom_delay_input(message):
    if not is_auth(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    valstr = message.text.strip()
    try:
        valf = float(valstr)
    except:
        return bot.reply_to(message, "Ошибка: введите число 0..5.")

    try:
        rr = requests.post("http://127.0.0.1:5000/set_custom_delay", json={"value":valf})
        if rr.status_code==200:
            j = rr.json()
            global custom_delay
            custom_delay = j.get('custom_delay',0.0)
            bot.reply_to(message, f"Доп. задержка: {custom_delay}")
        else:
            bot.reply_to(message, f"Ошибка: {rr.text}")
    except Exception as e:
        bot.reply_to(message, f"Сетевая ошибка: {e}")

# ----------------- Запуск -----------------

def run_flask():
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1)
    bot.infinity_polling()
