import os
import json
import time
import logging
import requests
import pandas as pd
import asyncio
import re
from collections import Counter

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, JobQueue, CallbackQueryHandler, MessageHandler, filters

# --- Configuración y logging ---
CONFIG_FILE = "config.json"
STATE_FILE = "processed_ids.json"
CATEGORIES_FILE = "categories.json"

# Constantes para rangos de descuento
DISCOUNT_RANGES = {
    "0-10": (0, 10),
    "10-20": (10, 20),
    "20-30": (20, 30),
    "30-50": (30, 50),
    "50+": (50, float('inf'))
}

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Cargar configuración ---
def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

# --- Estado local ---
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"IDs": [], "last_prices": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

# --- Cargar/guardar categorías y objetivos ---
def load_categories():
    if os.path.exists(CATEGORIES_FILE):
        with open(CATEGORIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"categorias": [], "objetivos": []}

def save_categories(categories_data):
    with open(CATEGORIES_FILE, "w", encoding="utf-8") as f:
        json.dump(categories_data, f, indent=2, ensure_ascii=False)

# --- Actualizar categorías y objetivos ---
def update_categories_and_objectives(sheet_data):
    # Cargar datos existentes
    categories_data = load_categories()
    
    # Extraer categorías y objetivos únicos
    categorias = set(categories_data.get("categorias", []))
    objetivos = set(categories_data.get("objetivos", []))
    
    # Añadir nuevas categorías y objetivos
    for product in sheet_data:
        # Manejar categoría
        cat = product.get("Categoria") or product.get("categoria")
        if cat is not None and not pd.isna(cat):  # Verificar que no sea None ni NaN
            if isinstance(cat, (float, int)):
                try:
                    cat = str(int(cat)) if not pd.isna(cat) else None
                except ValueError:
                    cat = str(cat)
            elif isinstance(cat, str):
                cat = cat.strip() if cat.strip() else None
            else:
                cat = None
            
            if cat:
                categorias.add(cat)
        
        # Manejar objetivo
        obj = product.get("Objetivo") or product.get("objetivo")
        if obj is not None and not pd.isna(obj):  # Verificar que no sea None ni NaN
            if isinstance(obj, (float, int)):
                try:
                    obj = str(int(obj)) if not pd.isna(obj) else None
                except ValueError:
                    obj = str(obj)
            elif isinstance(obj, str):
                obj = obj.strip() if obj.strip() else None
            else:
                obj = None
            
            if obj:
                objetivos.add(obj)
    
    # Guardar datos actualizados
    categories_data = {
        "categorias": sorted(list(categorias)),
        "objetivos": sorted(list(objetivos))
    }
    save_categories(categories_data)
    
    return categories_data

# --- Leer Google Sheet como CSV ---
def fetch_sheet_data(csv_url):
    try:
        df = pd.read_csv(csv_url)
        data = df.to_dict("records")
        logger.info(f"Leídos {len(data)} productos del Sheet.")
        return data
    except Exception as e:
        logger.error(f"Error leyendo el Sheet: {e}")
        return None

# --- Formato del mensaje HTML ---
def format_product_message(product, change_type=None, logo_url=None):
    html = []

    # Logo
    if logo_url and logo_url.strip():
        html.append(f'<a href="{logo_url}">&#8205;</a>')

    # Encabezado de cambio
    if change_type == "new":
        html.append('🆕 <b>Nuevo Producto:</b>')
    elif change_type == "discount":
        html.append('🔥 <b>¡Nuevo descuento!</b>')
    elif change_type == "search":
        html.append('🔍 <b>Resultado de búsqueda:</b>')

    html.append("")

    # Nombre y Marca
    nombre = product.get("Nombre") or product.get("nombre")
    if nombre:
        html.append(f'🔹 <b>Nombre:</b> {nombre}')
    marca = product.get("Marca") or product.get("marca")
    if marca:
        html.append(f'🔸 <b>Marca:</b> {marca}')

    html.append("")

    # Precios y descuento
    precio = product.get("Precio") or product.get("precio")
    descuento = product.get("Descuento") or product.get("descuento")
    precio_desc = product.get("Precio_descuento") or product.get("precio_descuento")
    if precio:
        html.append(f'💲 <b>Precio original:</b> <s>{precio}€</s>')
    if descuento:
        html.append(f'🎯 <b>Descuento:</b> {descuento}%')
    if precio_desc:
        html.append(f'✅ <b>Precio con descuento:</b> <b>{precio_desc}€</b>')

    html.append("")

    # Descripción
    descripcion = product.get("Descripcion") or product.get("Descripción") or product.get("descripcion")
    if descripcion:
        html.append(f'📝 <b>Descripción:</b>\n{descripcion}')

    html.append("")

    # Categoria y Objetivo
    categoria = product.get("Categoria") or product.get("categoria")
    if categoria is not None and not pd.isna(categoria):
        if isinstance(categoria, (float, int)):
            try:
                categoria = str(int(categoria)) if not pd.isna(categoria) else None
            except ValueError:
                categoria = str(categoria)
        html.append(f'📦 <b>Categoria:</b> {categoria}')
    
    objetivo = product.get("Objetivo") or product.get("objetivo")
    if objetivo is not None and not pd.isna(objetivo):
        if isinstance(objetivo, (float, int)):
            try:
                objetivo = str(int(objetivo)) if not pd.isna(objetivo) else None
            except ValueError:
                objetivo = str(objetivo)
        html.append(f'🎯 <b>Objetivo:</b> {objetivo}')

    return "\n".join([line for line in html]).strip()

# --- Enviar mensaje ---
async def send_message(bot: Bot, chat_id: str, text: str, image_url: str = None):
    try:
        if image_url and isinstance(image_url, str) and "drive.google.com/file/d/" in image_url:
            file_id = re.search(r"/d/([^/]+)/", image_url)
            if file_id:
                image_url = f"https://drive.google.com/uc?export=view&id={file_id.group(1)}"
        
        if image_url and isinstance(image_url, str) and image_url.strip() and image_url.startswith(('http://', 'https://')):
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=image_url,
                    caption=text,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Error enviando con imagen, usando solo texto: {e}")
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML
                )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"Error enviando mensaje: {e}")
        if is_admin_id(chat_id):
            return
        await send_admin_error(bot, f"Error enviando mensaje: {e}")

# --- Notificar errores al admin ---
async def send_admin_error(bot: Bot, message: str):
    admin_id = config.get("ADMIN_CHAT_ID") or config.get("admin_users", [None])[0]
    if admin_id:
        try:
            await bot.send_message(chat_id=admin_id, text=f"⚠️ Bot error:\n{message}")
        except Exception as e:
            logger.error(f"No se pudo notificar al admin: {e}")

# --- Verificar si un usuario es admin ---
def is_admin_id(user_id):
    admin_id = config.get("ADMIN_CHAT_ID")
    admin_users = config.get("admin_users", [])
    
    if admin_id and str(admin_id) == str(user_id):
        return True
    
    return user_id in admin_users or str(user_id) in [str(u) for u in admin_users]

# --- Comando /ofertas ---
async def ofertas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("0-10%", callback_data="discount_0-10"),
            InlineKeyboardButton("10-20%", callback_data="discount_10-20")
        ],
        [
            InlineKeyboardButton("20-30%", callback_data="discount_20-30"),
            InlineKeyboardButton("30-50%", callback_data="discount_30-50")
        ],
        [
            InlineKeyboardButton("Más del 50%", callback_data="discount_50+")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Selecciona el rango de descuento que quieres ver:",
        reply_markup=reply_markup
    )

# --- Manejador de selección de descuento ---
async def handle_discount_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    selected_range = query.data.split('_')[1]
    min_discount, max_discount = DISCOUNT_RANGES[selected_range]

    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await query.edit_message_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await query.edit_message_text("No se pudo acceder a los datos. Intenta más tarde.")
        return

    filtered_products = []
    for product in sheet_data:
        descuento = product.get("Descuento") or product.get("descuento")
        try:
            descuento = float(str(descuento).replace('%', ''))
            if min_discount <= descuento < max_discount or (selected_range == "50+" and descuento >= 50):
                filtered_products.append(product)
        except (ValueError, TypeError):
            continue

    if not filtered_products:
        await query.edit_message_text(
            f"No se encontraron productos con descuentos entre {min_discount}% y {max_discount}%"
        )
        return

    await query.edit_message_text(
        f"Encontrados {len(filtered_products)} productos con descuentos entre {min_discount}% y {max_discount}%.\n"
        "Enviando productos..."
    )
    
    chat_id = update.effective_chat.id
    logo_url = config.get("LOGO_URL") or config.get("logo_url")
    
    for i, product in enumerate(filtered_products):
        text = format_product_message(product, logo_url=logo_url)
        image_url = product.get("imagen") or product.get("Imagen")
        
        await send_message(
            context.bot,
            chat_id,
            text,
            image_url
        )
        
        if i < len(filtered_products) - 1:
            await asyncio.sleep(1)
    
    if filtered_products:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Se han enviado todos los {len(filtered_products)} productos con descuentos entre {min_discount}% y {max_discount}%."
        )

# --- Comando de búsqueda ---
async def buscar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Por favor, especifica un término de búsqueda después de /buscar.\n"
            "Ejemplo: /buscar proteína"
        )
        return
    
    search_term = " ".join(context.args).lower()
    
    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await update.message.reply_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await update.message.reply_text("No se pudo acceder a los datos. Intenta más tarde.")
        return
    
    found_products = []
    
    for product in sheet_data:
        product_text = ""
        
        for field in ["Nombre", "nombre", "Marca", "marca", "Descripcion", "Descripción", "descripcion", 
                      "Categoria", "categoria", "Objetivo", "objetivo"]:
            if product.get(field):
                field_value = product.get(field)
                if isinstance(field_value, (float, int)):
                    field_value = str(int(field_value))
                product_text += str(field_value).lower() + " "
        
        if search_term in product_text:
            found_products.append(product)
    
    if not found_products:
        await update.message.reply_text(
            f"No se encontraron productos que coincidan con '{search_term}'."
        )
        return
    
    await update.message.reply_text(
        f"Encontrados {len(found_products)} productos que coinciden con '{search_term}'.\n"
        "Enviando resultados..."
    )
    
    chat_id = update.effective_chat.id
    logo_url = config.get("LOGO_URL") or config.get("logo_url")
    
    for i, product in enumerate(found_products):
        text = format_product_message(product, change_type="search", logo_url=logo_url)
        image_url = product.get("imagen") or product.get("Imagen")
        
        await send_message(
            context.bot,
            chat_id,
            text,
            image_url
        )
        
        if i < len(found_products) - 1:
            await asyncio.sleep(1)
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Se han enviado todos los {len(found_products)} productos que coinciden con '{search_term}'."
    )

# --- Comando para mostrar categorías ---
async def categoria_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    categories_data = load_categories()
    categorias = categories_data.get("categorias", [])
    
    if not categorias:
        csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
        if not csv_url:
            await update.message.reply_text("Error: URL de la hoja de cálculo no configurada.")
            return
            
        sheet_data = fetch_sheet_data(csv_url)
        if sheet_data is None:
            await update.message.reply_text("No se pudo acceder a los datos. Intenta más tarde.")
            return
        
        categories_data = update_categories_and_objectives(sheet_data)
        categorias = categories_data.get("categorias", [])
    
    if not categorias:
        await update.message.reply_text("No se encontraron categorías.")
        return
    
    keyboard = []
    row = []
    
    for i, categoria in enumerate(categorias):
        if i > 0 and i % 2 == 0:
            keyboard.append(row)
            row = []
            
        row.append(InlineKeyboardButton(categoria, callback_data=f"cat_{categoria}"))
    
    if row:
        keyboard.append(row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Selecciona una categoría para ver sus productos:",
        reply_markup=reply_markup
    )

# --- Manejador de selección de categoría ---
async def handle_categoria_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    selected_categoria = query.data.split('_', 1)[1]
    
    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await query.edit_message_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await query.edit_message_text("No se pudo acceder a los datos. Intenta más tarde.")
        return
    
    filtered_products = []
    for product in sheet_data:
        categoria = product.get("Categoria") or product.get("categoria")
        if categoria is not None and not pd.isna(categoria):
            if isinstance(categoria, (float, int)):
                try:
                    categoria = str(int(categoria)) if not pd.isna(categoria) else None
                except ValueError:
                    categoria = str(categoria)
            elif isinstance(categoria, str):
                categoria = categoria.strip() if categoria.strip() else None
            else:
                categoria = None
            
            if categoria == selected_categoria:
                filtered_products.append(product)
    
    if not filtered_products:
        await query.edit_message_text(
            f"No se encontraron productos en la categoría '{selected_categoria}'."
        )
        return
    
    await query.edit_message_text(
        f"Encontrados {len(filtered_products)} productos en la categoría '{selected_categoria}'.\n"
        "Enviando productos..."
    )
    
    chat_id = update.effective_chat.id
    logo_url = config.get("LOGO_URL") or config.get("logo_url")
    
    for i, product in enumerate(filtered_products):
        text = format_product_message(product, logo_url=logo_url)
        image_url = product.get("imagen") or product.get("Imagen")
        
        await send_message(
            context.bot,
            chat_id,
            text,
            image_url
        )
        
        if i < len(filtered_products) - 1:
            await asyncio.sleep(1)
    
    if filtered_products:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Se han enviado todos los {len(filtered_products)} productos de la categoría '{selected_categoria}' Si quieres estar al día unete al canal @Supleshop_Ofertas."
        )

# --- Comando para mostrar objetivos ---
async def objetivo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    categories_data = load_categories()
    objetivos = categories_data.get("objetivos", [])
    
    if not objetivos:
        csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
        if not csv_url:
            await update.message.reply_text("Error: URL de la hoja de cálculo no configurada.")
            return
            
        sheet_data = fetch_sheet_data(csv_url)
        if sheet_data is None:
            await update.message.reply_text("No se pudo acceder a los datos. Intenta más tarde.")
            return
        
        categories_data = update_categories_and_objectives(sheet_data)
        objetivos = categories_data.get("objetivos", [])
    
    if not objetivos:
        await update.message.reply_text("No se encontraron objetivos.")
        return
    
    keyboard = []
    row = []
    
    for i, objetivo in enumerate(objetivos):
        if i > 0 and i % 2 == 0:
            keyboard.append(row)
            row = []
            
        row.append(InlineKeyboardButton(objetivo, callback_data=f"obj_{objetivo}"))
    
    if row:
        keyboard.append(row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Selecciona un objetivo para ver sus productos:",
        reply_markup=reply_markup
    )

# --- Manejador de selección de objetivo ---
async def handle_objetivo_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    selected_objetivo = query.data.split('_', 1)[1]
    
    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await query.edit_message_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await query.edit_message_text("No se pudo acceder a los datos. Intenta más tarde.")
        return
    
    filtered_products = []
    for product in sheet_data:
        objetivo = product.get("Objetivo") or product.get("objetivo")
        if objetivo is not None and not pd.isna(objetivo):  # Verificar que no sea NaN
            if isinstance(objetivo, (float, int)):
                try:
                    objetivo = str(int(objetivo)) if not pd.isna(objetivo) else None
                except ValueError:
                    objetivo = str(objetivo)
            elif isinstance(objetivo, str):
                objetivo = objetivo.strip() if objetivo.strip() else None
            else:
                objetivo = None
            
            if objetivo == selected_objetivo:
                filtered_products.append(product)
    
    if not filtered_products:
        await query.edit_message_text(
            f"No se encontraron productos con el objetivo '{selected_objetivo}'."
        )
        return
    
    await query.edit_message_text(
        f"Encontrados {len(filtered_products)} productos con el objetivo '{selected_objetivo}'.\n"
        "Enviando productos..."
    )
    
    chat_id = update.effective_chat.id
    logo_url = config.get("LOGO_URL") or config.get("logo_url")
    
    for i, product in enumerate(filtered_products):
        text = format_product_message(product, logo_url=logo_url)
        image_url = product.get("imagen") or product.get("Imagen")
        
        await send_message(
            context.bot,
            chat_id,
            text,
            image_url
        )
        
        if i < len(filtered_products) - 1:
            await asyncio.sleep(1)
    
    if filtered_products:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Se han enviado todos los {len(filtered_products)} productos con el objetivo '{selected_objetivo}'."
        )

# --- Lógica principal de monitoreo ---
async def process_sheet_data(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    
    csv_url = config.get("SHEET_CSV_URL") or config.get("sheet_url")
    if not csv_url:
        await send_admin_error(bot, "Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await send_admin_error(bot, "No se pudo acceder al Google Sheet. Reintentando en 1 minuto.")
        return

    update_categories_and_objectives(sheet_data)

    state = load_state()
    ids = set(state["IDs"])
    last_prices = state.get("last_prices", {})

    logo_url = config.get("LOGO_URL") or config.get("logo_url")
    channel_id = config.get("TELEGRAM_CHANNEL_ID")
    
    if not channel_id:
        for product in sheet_data:
            pid = str(product.get("ID") or product.get("id") or "").strip()
            if not pid:
                continue
                
            precio_desc = str(product.get("Precio_descuento") or product.get("precio_descuento") or "").strip()
            
            if pid not in ids:
                ids.add(pid)
                last_prices[pid] = precio_desc
            elif precio_desc and last_prices.get(pid) != precio_desc:
                last_prices[pid] = precio_desc
                
        save_state({"IDs": list(ids), "last_prices": last_prices})
        return
        
    nuevos = []
    descuentos = []

    for product in sheet_data:
        pid = str(product.get("ID") or product.get("id") or "").strip()
        if not pid:
            continue

        precio_desc = str(product.get("Precio_descuento") or product.get("precio_descuento") or "").strip()

        if pid not in ids:
            nuevos.append(product)
            ids.add(pid)
            last_prices[pid] = precio_desc
        elif precio_desc and last_prices.get(pid) != precio_desc:
            descuentos.append(product)
            last_prices[pid] = precio_desc

    for product in nuevos:
        text = format_product_message(product, change_type="new", logo_url=logo_url)
        image_url = product.get("imagen") or product.get("Imagen")
        await send_message(bot, channel_id, text, image_url)
        await asyncio.sleep(1)

    for product in descuentos:
        text = format_product_message(product, change_type="discount", logo_url=logo_url)
        image_url = product.get("imagen") or product.get("Imagen")
        await send_message(bot, channel_id, text, image_url)
        await asyncio.sleep(1)

    save_state({"IDs": list(ids), "last_prices": last_prices})

# --- Comando /force_update ---
async def force_update(update, context):
    user_id = str(update.effective_user.id)
    if not is_admin_id(user_id):
        await update.message.reply_text("No tienes permiso para usar este comando.")
        return
    await update.message.reply_text("Forzando actualización...")
    await process_sheet_data(context)
    await update.message.reply_text("Actualización completada.")

# --- Comando /start ---
async def start_command(update, context):
    await update.message.reply_text(
        "¡Bienvenido al Bot de Búsqueda de Productos de Supleshop! 🎉\n\n"
        "Este bot te permite buscar productos y sus descuentos.\n\n"
        "Comandos disponibles:\n"
        "/ofertas - Ver productos por rango de descuento\n"
        "/buscar [término] - Buscar productos por palabra clave\n"
        "/categoria - Ver productos por categoría\n"
        "/objetivo - Ver productos por objetivo\n"
    )

# --- Comando /help ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🆘 <b>Ayuda - Comandos disponibles:</b>\n\n"
        "🔹 <b>/start</b> - Muestra el mensaje de bienvenida\n"
        "🔹 <b>/help</b> - Muestra esta ayuda\n"
        "🔹 <b>/ofertas</b> - Muestra productos por rango de descuento\n"
        "🔹 <b>/buscar [término]</b> - Busca productos por palabra clave\n"
        "🔹 <b>/categoria</b> - Muestra productos por categoría\n"
        "🔹 <b>/objetivo</b> - Muestra productos por objetivo\n\n"
        "No dudes en preguntar si tienes alguna duda o necesitas ayuda adicional. "
        "¡Estamos aquí para ayudarte y para realizar pedidos al 608.195.146! 😊\n\n"
        "Si quieres estar al día de todas las ofertas y novedades, únete a nuestro canal de Telegram: "
        "<a href='https://t.me/Supleshop_Ofertas'>@Supleshop_Ofertas</a>\n\n"
        "Si quieres ver los productos de Supleshop, puedes hacerlo en su web: "
        "<a href='https://www.supleshop.es'>www.supleshop.es</a>"
    )
    
    # Alternativamente, puedes dividir el mensaje en dos partes si es muy largo
    try:
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error al enviar mensaje de ayuda: {e}")
        # Enviar versión simplificada si falla
        await update.message.reply_text(
            "ℹ️ Consulta los comandos disponibles: /start",
            parse_mode=ParseMode.HTML
        )

# --- Función principal ---
def main():
    bot_token = config.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("No se encontró el token del bot en la configuración.")
        return
        
    application = Application.builder().token(bot_token).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ofertas", ofertas_command))
    application.add_handler(CommandHandler("buscar", buscar_command))
    application.add_handler(CommandHandler("categoria", categoria_command))
    application.add_handler(CommandHandler("objetivo", objetivo_command))
    
    application.add_handler(CommandHandler("force_update", force_update))
    
    application.add_handler(CallbackQueryHandler(handle_discount_selection, pattern="^discount_"))
    application.add_handler(CallbackQueryHandler(handle_categoria_selection, pattern="^cat_"))
    application.add_handler(CallbackQueryHandler(handle_objetivo_selection, pattern="^obj_"))
    
    job_interval = config.get("UPDATE_INTERVAL_MINUTES", 10)
    application.job_queue.run_repeating(process_sheet_data, interval=job_interval*60, first=10)
    
    logger.info("Bot iniciado exitosamente.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

# --- Iniciar aplicación ---
if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot detenido manualmente.")
    except Exception as e:
        logger.error(f"Error fatal: {e}")
        import traceback
        traceback.print_exc()
