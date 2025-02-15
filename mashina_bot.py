import logging
import os
import datetime
import re
import gspread

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials



# ==================== НАСТРОЙКИ ====================
logging.basicConfig(level=logging.DEBUG)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")  # Вставь ID своей Google таблицы


# Преобразуем ID администраторов в int
def load_admin_ids():
    gc = gspread.service_account("credentials.json")  # Подключение к Google Sheets
    sheet = gc.open("Client Database").sheet1  # Открываем первый лист
    records = sheet.get_all_records()

    admin_ids = {
        int(row["Telegram ID"]) for row in records
        if row.get("Должность") == "Админ" and str(row.get("Telegram ID")).isdigit()
    }
    return admin_ids  # Возвращаем множество

# Загружаем админов при запуске бота
ADMIN_IDS = load_admin_ids()

# Функция для обновления списка админов (если в будущем изменится)
async def update_admin_ids():
    global ADMIN_IDS
    ADMIN_IDS = load_admin_ids()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
# Количество машин на одной странице
CARS_PER_PAGE = 5
# Настройка Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/spreadsheets",
         "https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(SPREADSHEET_ID).sheet1  # Работаем с первым листом
cars_sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Состояние машины")
changes_sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Изменения")
# Регулярное выражение для проверки номера телефона
PHONE_REGEX = r"^\+7\d{10}$"

# ==================== СОСТОЯНИЯ ====================
class Form(StatesGroup):
    phone_number = State()
    full_name = State()
    waiting_admin_confirmation = State()
    main_menu = State()

class PhysicalStockState(StatesGroup):
    selecting_car = State()
    entering_stock = State()   

# ==================== КЛАВИАТУРЫ ====================
main_menu = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Посмотреть", callback_data="view_cars")],
        [InlineKeyboardButton(text="⛽️Внести физ. остаток", callback_data="enter_physical_stock")]
    ]
)
# ==================== ОБРАБОТЧИКИ ====================
@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    """Проверяет, есть ли пользователь в таблице, и либо запрашивает данные, либо приветствует."""
    telegram_id = message.from_user.id  # ID текущего пользователя
    existing_records = sheet.get_all_records()

    # Проверяем, есть ли пользователь в таблице
    user_exists = False
    user_status = None

    for record in existing_records:
        record_telegram_id = str(record.get("Telegram ID", "")).strip()
        if record_telegram_id.isdigit() and int(record_telegram_id) == telegram_id:
            user_exists = True
            user_status = record.get("Статус", "Ожидает")  # Получаем статус пользователя
            break

    if user_exists:
        if user_status == "Отклонено":
            return
        elif user_status == "Подтвержден":
            await message.answer("🏠Главное меню", reply_markup=main_menu)
        else:
            await message.answer("⏳ Ваша заявка на подтверждение уже отправлена. Ожидайте ответа администратора.")
    else:
        await state.set_state(Form.phone_number)
        await message.answer("Добро пожаловать! Введите ваш 📞номер телефона в формате +7XXXXXXXXXX:")

@dp.message(Form.phone_number)
async def get_phone(message: Message, state: FSMContext):
    """Обрабатывает ввод номера телефона."""
    phone = message.text.strip()

    if not re.match(PHONE_REGEX, phone):
        await message.answer("Пожалуйста, введите корректный 📞номер телефона в формате +7XXXXXXXXXX.")
        return

    await state.update_data(phone_number=phone)
    await state.set_state(Form.full_name)
    await message.answer("Теперь введите ваше 👤ФИО:")

@dp.message(Form.full_name)
async def save_full_name(message: Message, state: FSMContext):
    """Обрабатывает ввод ФИО и отправляет заявку админу."""
    full_name = message.text.strip()
    user_data = await state.get_data()

    user_data.update({
        "full_name": full_name,
        "telegram_id": message.from_user.id,  # ID клиента
        "registration_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

    await state.update_data(**user_data)  
    await state.set_state(Form.waiting_admin_confirmation)  
    await message.answer("Ожидайте подтверждения от администратора.")

    # Сохраняем данные клиента в Google Sheets
    sheet.append_row([
        user_data['phone_number'],
        user_data['full_name'],
        user_data['registration_date'],
        "Ожидает",  # Статус "Ожидает"
        user_data['telegram_id']
    ])

    # Клавиатура с персональными данными клиента
    confirmation_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_user:{user_data['telegram_id']}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"block_user:{user_data['telegram_id']}")]
        ]
    )

    # Уведомление администраторам
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"🚗 Новый запрос на авторизацию 🚗\n\n"
            f"📞 Телефон: {user_data['phone_number']}\n"
            f"👤 ФИО: {user_data['full_name']}\n"
            f"🆔 Telegram ID: {user_data['telegram_id']}\n"
            f"⏳ Дата регистрации: {user_data['registration_date']}\n\n"
            f"Подтвердить?",
            reply_markup=confirmation_keyboard
        )


@dp.callback_query(F.data.startswith("confirm_user:"))
async def confirm_user_handler(callback_query: CallbackQuery):
    """Подтверждение пользователя и изменение статуса в Google Sheets"""
    # Получаем Telegram ID клиента из callback_data
    telegram_id = callback_query.data.split(":")[1]

    # Проверяем, что ID является числом
    if not telegram_id.isdigit():
        await callback_query.answer("❌ Ошибка: Неверный формат ID.")
        return

    telegram_id = int(telegram_id)  # Преобразуем в int

    # Получаем все записи из Google Sheets
    existing_records = sheet.get_all_records()

    # Ищем пользователя в таблице по Telegram ID
    user_exists = False
    row = None

    for index, record in enumerate(existing_records, start=2):  # Начинаем со 2-й строки
        record_telegram_id = str(record.get("Telegram ID", "")).strip()

        if record_telegram_id.isdigit() and int(record_telegram_id) == telegram_id:
            user_exists = True
            row = index  # Запоминаем строку пользователя
            break

    if not user_exists:
        await callback_query.answer("❌ Пользователь не найден в базе.")
        await callback_query.message.edit_text(f"❌ Пользователь с Telegram ID {telegram_id} не найден.")
        return

    # Изменяем статус на "Подтвержден" в таблице
    sheet.update_cell(row, 4, "Подтвержден")  # Столбец 4 — это "Статус"
    sheet.update_cell(row, 6, "Свободен")

    # Отправляем клиенту уведомление
    await bot.send_message(telegram_id, "✅ Ваш вход подтвержден! Добро пожаловать!", reply_markup=main_menu)

    # Админу — подтверждение
    await callback_query.answer("✅ Пользователь подтвержден.")
    await callback_query.message.edit_text(f"✅ Пользователь {telegram_id} подтвержден.", reply_markup=main_menu)

@dp.callback_query(F.data.startswith("block_user:"))
async def block_user_handler(callback_query: CallbackQuery):
    """Отклоняет пользователя и меняет статус на 'Отклонено'."""
    telegram_id = callback_query.data.split(":")[1]  # Получаем ID клиента из callback_data

    if not telegram_id.isdigit():
        await callback_query.answer("❌ Ошибка: Неверный формат ID.")
        return

    telegram_id = int(telegram_id)  # Преобразуем в int

    # Получаем все записи из Google Sheets
    existing_records = sheet.get_all_records()
    user_found = False
    row = None

    for index, record in enumerate(existing_records, start=2):
        record_telegram_id = str(record.get("Telegram ID", "")).strip()
        if record_telegram_id.isdigit() and int(record_telegram_id) == telegram_id:
            user_found = True
            row = index  # Номер строки в таблице
            break

    if not user_found:
        await callback_query.answer("❌ Пользователь не найден в базе.")
        await callback_query.message.edit_text(f"❌ Пользователь с Telegram ID {telegram_id} не найден.")
        return

    # Обновляем статус в таблице на "Отклонено"
    sheet.update_cell(row, 4, "Отклонено")

    # Уведомляем пользователя
    await bot.send_message(telegram_id, "🚫 Ваш доступ был отклонен администратором.")

    # Уведомляем администратора
    await callback_query.answer("Пользователь отклонен.")
    await callback_query.message.edit_text(f"🚫 Пользователь {telegram_id} отклонен и заблокирован.", reply_markup=main_menu)

# ================== Главное меню =============
def get_cars_keyboard(page: int):
    """Создаёт клавиатуру с машинами и кнопками листания"""
    cars = cars_sheet.get_all_values()[1:]  # Получаем все строки (кроме заголовков)
    total_pages = (len(cars) + CARS_PER_PAGE - 1) // CARS_PER_PAGE  # Кол-во страниц

    start = (page - 1) * CARS_PER_PAGE
    end = start + CARS_PER_PAGE
    cars_on_page = cars[start:end]

    buttons = [
        [InlineKeyboardButton(text=f"🚙 {car[0]}", callback_data=f"car_info:{car[0]}")]
        for car in cars_on_page
    ]

    # Кнопки перелистывания
    navigation_buttons = []
    if page > 1:
        navigation_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"view_cars_page:{page - 1}"))
    if page < total_pages:
        navigation_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"view_cars_page:{page + 1}"))

    if navigation_buttons:
        buttons.append(navigation_buttons)

    # Добавляем кнопку "Вернуться в главное меню"
    buttons.append([InlineKeyboardButton(text="🏠 Вернуться в главное меню", callback_data="back_to_main_menu")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "view_cars")
async def view_cars(callback_query: CallbackQuery):
    """Вывод первой страницы машин"""
    await callback_query.message.edit_text("📋 Список машин:", reply_markup=get_cars_keyboard(1))

@dp.callback_query(F.data.startswith("view_cars_page:"))
async def change_page(callback_query: CallbackQuery):
    """Переключение страниц списка машин"""
    page = int(callback_query.data.split(":")[1])
    await callback_query.message.edit_text("📋 Список машин:", reply_markup=get_cars_keyboard(page))

@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback_query: CallbackQuery):
    """Возвращает пользователя в главное меню"""
    await callback_query.message.edit_text("🏠 Главное меню", reply_markup=main_menu)

# ===========# ==================== Вывод информации о машине ====================
@dp.callback_query(F.data.startswith("car_info:"))
async def car_info(callback_query: CallbackQuery):
    """Вывод информации о машине (номер, остаток, дата изменения)"""
    car_number = callback_query.data.split(":")[1]
    cars = cars_sheet.get_all_values()[1:]  # Получаем все строки, пропуская заголовок
    users = sheet.get_all_values()[1:]
    user_id = str(callback_query.from_user.id)

    car_row = None
    for index, car in enumerate(cars, start=2):
        if car[0] == car_number:
            car_row = index
            break
    for index, user in enumerate(users,start=2):
        if len(user) > 4 and user[4] == user_id:
            sheet.update_cell(index, 6, "В рейсе")
            break

    for car in cars:
        if car[0] == car_number:  # Номер машины найден
            stock = car[1] if len(car) > 1 else "Нет данных"
            last_update = car[2] if len(car) > 2 else "Неизвестно"

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Назад к выбору машин", callback_data="view_cars")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
                ]
            )
            
            await callback_query.message.edit_text(
                f"🚙 Машина: {car_number}\n⛽️ Остаток: {stock} л\n📅 Последнее изменение: {last_update}",
                reply_markup=keyboard
            )
            return

    await callback_query.answer("❌ Информация не найдена")

# ==================== Главное меню ====================
@dp.callback_query(F.data == "main_menu")
async def back_to_main_menu(callback_query: CallbackQuery):
    """Возвращает главное меню"""
    await callback_query.message.edit_text("🏠 Главное меню:", reply_markup=main_menu)

# ==================== Выбор машин ====================
@dp.callback_query(F.data == "view_cars")
async def view_cars(callback_query: CallbackQuery):
    """Вывод первой страницы машин"""
    await callback_query.message.edit_text("📋 Список машин:", reply_markup=get_cars_keyboard(1))

# ==================== Изменение физ. остатка ====================
@dp.callback_query(F.data == "enter_physical_stock")
async def select_car_for_physical_stock(callback_query: CallbackQuery, state: FSMContext):
    """Отображает список машин перед внесением физ. остатка."""
    cars = cars_sheet.col_values(1)[1:]  # Получаем список машин (пропускаем заголовок)

    if not cars:
        await callback_query.message.answer("🚗 Список машин пуст.")
        return

    page = 1  # Начинаем с первой страницы
    await callback_query.message.answer("📋 Выберите машину для внесения физ. остатка:", reply_markup=get_cars_keyboard_for_stock(page))

# ==================== Функция создания клавиатуры для физ. остатка ====================
def get_cars_keyboard_for_stock(page: int):
    """Создаёт клавиатуру с машинами и кнопками листания для внесения физ. остатка"""
    cars = cars_sheet.get_all_values()[1:]  # Получаем все строки (кроме заголовков)
    total_pages = (len(cars) + CARS_PER_PAGE - 1) // CARS_PER_PAGE  # Кол-во страниц

    start = (page - 1) * CARS_PER_PAGE
    end = start + CARS_PER_PAGE
    cars_on_page = cars[start:end]

    buttons = [
        [InlineKeyboardButton(text=f"🚙 {car[0]}", callback_data=f"select_physical_car:{car[0]}")]
        for car in cars_on_page
    ]

    # Кнопки перелистывания
    navigation_buttons = []
    if page > 1:
        navigation_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"enter_physical_stock_page:{page - 1}"))
    if page < total_pages:
        navigation_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"enter_physical_stock_page:{page + 1}"))

    if navigation_buttons:
        buttons.append(navigation_buttons)

    # Добавляем кнопку "Вернуться в главное меню"
    buttons.append([InlineKeyboardButton(text="🏠 Вернуться в главное меню", callback_data="back_to_main_menu")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("enter_physical_stock_page:"))
async def change_page_for_physical_stock(callback_query: CallbackQuery):
    """Переключение страниц списка машин для физ. остатка"""
    page = int(callback_query.data.split(":")[1])
    await callback_query.message.edit_text(
        "📋 Выберите машину для внесения физ. остатка:",
        reply_markup=get_cars_keyboard_for_stock(page)
    )


@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback_query: CallbackQuery):
    """Возвращает пользователя в главное меню"""
    await callback_query.message.edit_text("🏠 Главное меню", reply_markup=main_menu)

# ==================== Выбор машины для физ. остатка ====================
@dp.callback_query(F.data.startswith("select_physical_car:"))
async def select_physical_car(callback_query: CallbackQuery, state: FSMContext):
    """Запрашивает ввод физ. остатка для выбранной машины"""
    car_number = callback_query.data.split(":")[1]
    await state.update_data(selected_car=car_number)

    await callback_query.message.answer(f"🚙 Вы выбрали машину {car_number}.\nВведите физ. остаток топлива в баке (л):")
    await state.set_state(PhysicalStockState.entering_stock)

# ==================== Ввод физ. остатка ====================
from datetime import datetime
print(datetime.now())
@dp.message(PhysicalStockState.entering_stock)
async def save_physical_stock(message: Message, state: FSMContext):
    """Сохраняет физический остаток в Google Sheets."""
    physical_stock = message.text.strip()

    if not physical_stock.isdigit():
        await message.answer("❌ Введите корректное число литров.")
        return

    user_data = await state.get_data()
    selected_car = user_data["selected_car"]
    telegram_id = message.from_user.id

    # Получаем данные клиента из первого листа
    clients_sheet = client.open_by_key(SPREADSHEET_ID).sheet1
    records = clients_sheet.get_all_records()

    client_info = None
    row_number = None

    for i, record in enumerate(records, start=2):
        if str(record.get("Telegram ID", "")).strip() == str(telegram_id):
            client_info = record
            row_number = i
            break

    if not client_info:
        await message.answer("❌ Ваши данные не найдены в системе.")
        await state.clear()
        return
    keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
                ]
            )

    full_name = client_info.get("ФИО", "Неизвестно")
    phone_number = client_info.get("Телефон", "Неизвестно")
    from datetime import datetime
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Записываем данные в лист «Изменения»
    changes_sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Изменения")
    changes_sheet.append_row([
        full_name, phone_number, selected_car, physical_stock, current_time
    ])

    clients_sheet.update_cell(row_number,6 , "Свободен")

    await message.answer(f"✅ Данные записаны:\n👤 ФИО: {full_name}\n📞 Телефон: {phone_number}\n🚙 Машина: {selected_car}\n⛽️ Остаток: {physical_stock} л", reply_markup=keyboard)

    await state.clear()



# ============= АДМИН =================
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup

# ==================== СОСТОЯНИЯ ====================
class AdminState(StatesGroup):
    selecting_car = State()
    entering_stock = State()

# ==================== ИНЛАЙН-КЛАВИАТУРА ДЛЯ АДМИНА ====================
admin_inline_menu = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📋 Получить информацию", callback_data="get_info")],
        [InlineKeyboardButton(text="✏️ Внести остаток", callback_data="admin_update_stock")],
        [InlineKeyboardButton(text="📢 Уведомление", callback_data="admin_notify")]
    ]
)
# Кнопка для возврата в админ-меню
admin_inline_go_menu = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Вернуться в админ-меню", callback_data="admin_inline_menu")]
    ]
)
# ==================== ПАНЕЛЬ АДМИНА ====================
@dp.message(Command("admin"))
async def show_admin_panel(message: Message):
    """Показывает инлайн-меню администратора."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔️ У вас нет доступа к панели администратора.")
        return

    await message.answer("🔧 Панель администратора:", reply_markup=admin_inline_menu)

# ==================== ВНЕСЕНИЕ ОСТАТКА ====================
@dp.callback_query(F.data == "admin_update_stock")
async def select_car_for_stock_update(callback: CallbackQuery, state: FSMContext):
    """Отображает список машин перед обновлением остатка."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔️ У вас нет доступа к этой функции.")
        return
   
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Состояние машины")
    cars = sheet.col_values(1)[1:]  # Получаем список машин (пропускаем заголовок)

    if not cars:
        await callback.message.answer("🚗 Список машин пуст.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=car, callback_data=f"select_car:{car}")] for car in cars
        ]
    )

    # Добавляем кнопку для возврата в админ-меню
    return_button = InlineKeyboardButton(text="Вернуться в админ-меню", callback_data="admin_inline_menu")
    keyboard.inline_keyboard.append([return_button])
    
    await state.set_state(AdminState.selecting_car)
    
    # Сохраняем ID сообщения для дальнейшего редактирования
    await state.update_data(message_id=callback.message.message_id)
    
    # Редактируем предыдущее сообщение с предложением выбора машины
    await callback.message.bot.edit_message_text(
        text="Выберите машину для изменения остатка:",
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        reply_markup=keyboard
    )

@dp.callback_query(F.data.startswith("select_car:"))
async def enter_stock_value(callback: CallbackQuery, state: FSMContext):
    """Запрашивает ввод нового остатка."""
    car_number = callback.data.split(":")[1]
    await state.update_data(selected_car=car_number)

    # Получаем ID сообщения для его редактирования
    data = await state.get_data()
    message_id = data.get('message_id')

    # Редактируем предыдущее сообщение с запросом ввода остатка
    await callback.message.bot.edit_message_text(
        text=f"Введите новый остаток для машины {car_number}:",
        chat_id=callback.message.chat.id,
        message_id=message_id
    )

    await state.set_state(AdminState.entering_stock)

@dp.message(AdminState.entering_stock)
async def update_stock(message: Message, state: FSMContext):
    """Обновляет остаток в таблице."""
    new_stock = message.text.strip()

    if not new_stock.isdigit():
        await message.answer("❌ Введите корректное числовое значение остатка.")
        return

    data = await state.get_data()
    car_number = data["selected_car"]

    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Состояние машины")
    records = sheet.get_all_records(expected_headers=["Номер машины", "Остаток", "Дата изменения"])

    row_to_update = None

    # Поиск нужной строки в таблице
    for index, record in enumerate(records, start=2):
        if record["Номер машины"] == car_number:
            row_to_update = index
            break

    if row_to_update:
        # Обновление остатка и даты
        sheet.update(f"B{row_to_update}", [[new_stock]])  # Обновляем остаток
        sheet.update(f"C{row_to_update}", [[datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")]])  # Обновляем дату

        # Отправляем новое сообщение с результатом обновления
        await message.answer(f"✅ Остаток для машины {car_number} обновлен на: {new_stock} л", reply_markup=admin_inline_go_menu)
    else:
        await message.answer("❌ Машина не найдена.")

    await state.clear()



# =============== Получение инфы ======================
from datetime import datetime
from aiogram import types

@dp.callback_query(F.data == "get_info")
async def get_info(callback_query: types.CallbackQuery):
    """Выводит информацию о каждой машине за текущий день"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")  # Форматируем дату ГГГГ-ДД-ММ
    changes = changes_sheet.get_all_values()[1:]  # Данные из "Изменения" (без заголовков)
    cars = cars_sheet.get_all_values()[1:]  # Получаем список всех машин

    car_data = {car[0]: "информации нет" for car in cars}  # Заполняем словарь машинами

    last_entries = {}  # Словарь для хранения последней информации по каждой машине

    for row in changes:
        if len(row) < 5:
            continue  # Пропускаем строки с недостаточным количеством данных

        fio = row[0]  # ФИО из 1-й ячейки
        car_number = row[2]  # Номер машины из 3-й ячейки
        stock = row[3]  # Физ. остаток из 4-й ячейки
        date = row[4].split()[0] if row[4] else ""  # Дата без времени (5-я ячейка)

        if date == today:  # Фильтруем по сегодняшнему дню
            last_entries[car_number] = f"{fio}, {stock} л"  # Запоминаем последнюю запись

    # Обновляем данные по машинам
    for car_number, info in last_entries.items():
        car_data[car_number] = info

    # Формируем текст сообщения
    info_text = "\n".join([f"{car}: {info}" for car, info in car_data.items()])

    await callback_query.message.edit_text(f"📋 Информация по машинам за {today}:\n{info_text}", reply_markup=admin_inline_go_menu)

@dp.callback_query(F.data == "admin_inline_menu")
async def go_back_to_admin_menu(callback_query: CallbackQuery):
    """Возвращает в админ-меню."""
    await callback_query.message.edit_text("🔧 Панель администратора:", reply_markup=admin_inline_menu)

# ========== Уведомление ==============
class AdminState(StatesGroup):
    selecting_user = State()
    sending_message = State()
    waiting_for_message = State()
    selecting_car = State() 
    entering_stock = State()

import logging

# Настроим логирование
logging.basicConfig(level=logging.DEBUG)

# Функция получения пользователей из первого листа таблицы
def get_users_from_first_sheet():
    try:
        sheet = client.open("Client Database").sheet1  # Открываем первый лист
        users = sheet.get_all_records()  # Получаем все строки в виде списка словарей
        logging.debug(f"Получены пользователи: {users}")
        return users
    except Exception as e:
        logging.error(f"Ошибка при получении пользователей: {e}")
        return []

# Обработчик кнопки "Уведомление"
@dp.callback_query(F.data == "admin_notify")
async def notify_users(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
       await callback.answer("⛔️ У вас нет доступа к этой функции.")
       return
    users = get_users_from_first_sheet()
    if not users:
        await callback.message.answer("Не удалось получить список пользователей.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])

    # Добавляем кнопки с ФИО пользователей
    for user in users:
        if "ФИО" in user and "Telegram ID" in user:
            # Преобразуем Telegram ID в строку
            tg_id_str = str(user["Telegram ID"])
            keyboard.inline_keyboard.append([InlineKeyboardButton(text=user["ФИО"], callback_data=f"select_user:{tg_id_str}")])

    if not keyboard.inline_keyboard:
        await callback.message.answer("Нет пользователей для отправки уведомлений.")
        return

    await callback.message.answer("Выберите пользователя для отправки уведомления:", reply_markup=keyboard)

# Обработчик выбора пользователя
@dp.callback_query(F.data.startswith("select_user:"))
async def select_user_for_message(callback: types.CallbackQuery, state: FSMContext):
    tg_id = callback.data.split(":")[1]  # Получаем Telegram ID пользователя

    users = get_users_from_first_sheet()
    # Преобразуем Telegram ID в строку при поиске
    user = next((u for u in users if str(u.get("Telegram ID")) == tg_id), None)

    if user:
        await state.update_data(user_tg_id=tg_id, user_name=user["ФИО"])
        await callback.message.answer(f"Введите сообщение, которое хотите отправить пользователю {user['ФИО']}:")
        await state.set_state(AdminState.waiting_for_message)
    else:
        await callback.message.answer("Ошибка: Пользователь не найден.")

# Обработчик ввода сообщения администратором
@dp.message(AdminState.waiting_for_message)
async def send_message_to_user(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_tg_id = data.get("user_tg_id")
    user_name = data.get("user_name")

    if user_tg_id:
        try:
            main_menu_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Вернуться в главное меню", callback_data="go_main_menu")]
            ])
            # Отправляем сообщение пользователю
            await bot.send_message(user_tg_id, message.text, reply_markup=main_menu_keyboard)

            # Подтверждаем администратору успешную отправку
            await message.answer(f"Сообщение успешно отправлено пользователю {user_name}.", reply_markup=admin_inline_go_menu)
        except Exception as e:
            await message.answer(f"Ошибка при отправке сообщения: {e}")
    else:
        await message.answer("Ошибка: не найден ID пользователя.")

    await state.clear()  # Завершаем состояние

# Обработчик нажатия на "Вернуться в главное меню"
@dp.callback_query(lambda c: c.data == "go_main_menu")
async def go_main_menu(callback: types.CallbackQuery):
    await callback.message.answer("🏠 Главное меню", reply_markup=main_menu)


# ================== Таймер ===============
import pytz
import datetime
# Часовой пояс Москвы
MSK_TZ = pytz.timezone("Europe/Moscow")

async def send_fuel_reminder(bot: Bot):
    """Отправляет напоминания пользователям со статусом 'В рейсе'."""
    now = datetime.datetime.now(MSK_TZ)
    start_time = now.replace(hour=23, minute=5, second=15, microsecond=0)
    end_time = now.replace(hour=23, minute=59, second=0, microsecond=0)

    # Если текущее время позже 23:59, ждем до следующего дня 19:00
    if now >= end_time:
        next_start = now + datetime.timedelta(days=1)
        start_time = next_start.replace(hour=19, minute=0, second=0, microsecond=0)

    # Ожидание до 19:00 МСК
    await asyncio.sleep((start_time - now).total_seconds())

    while datetime.datetime.now(MSK_TZ) < end_time:
        users = sheet.get_all_values()[1:]  # Получаем всех пользователей (без заголовков)

        for user in users:
            if len(user) >= 6 and user[5] == "В рейсе":  # Проверяем столбец F (6-й)
                telegram_id = user[4]  # Столбец E — Telegram ID
                
                # Клавиатура с кнопкой "Вернуться в главное меню"
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🏠 Вернуться в главное меню", callback_data="main_menu")]
                    ]
                )

                try:
                    await bot.send_message(
                        telegram_id, 
                        "🚨 Не забудьте внести физический остаток топлива после окончания рейса!", 
                        reply_markup=keyboard
                    )
                except Exception as e:
                    print(f"Ошибка при отправке сообщения {telegram_id}: {e}")

        # Ждем 15 минут перед следующей проверкой
        await asyncio.sleep(1 * 30)

async def schedule_fuel_reminder(bot: Bot):
    """Запускает напоминания каждый день в 19:00 МСК."""
    while True:
        await send_fuel_reminder(bot)

# ==================== ЗАПУСК БОТА ====================
async def main():
    try:
        print("Бот запущен...")
        asyncio.create_task(schedule_fuel_reminder(bot))  # Запуск фоновой задачи
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
