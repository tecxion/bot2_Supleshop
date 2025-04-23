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

    # Logo (invisible char para preview, pero no añade salto de línea)
    if logo_url and logo_url.strip():
        html.append(f'<a href="{logo_url}">&#8205;</a>')

    # Encabezado de cambio
    if change_type == "new":
        html.append('🆕 <b>Nuevo Producto:</b>')
    elif change_type == "discount":
        html.append('🔥 <b>¡Nuevo descuento!</b>')
    elif change_type == "search":
        html.append('🔍 <b>Resultado de búsqueda:</b>')

    # Línea vacía
    html.append("")

    # Nombre y Marca
    nombre = product.get("Nombre") or product.get("nombre")
    if nombre:
        html.append(f'🔹 <b>Nombre:</b> {nombre}')
    marca = product.get("Marca") or product.get("marca")
    if marca:
        html.append(f'🔸 <b>Marca:</b> {marca}')

    # Línea vacía
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

    # Línea vacía
    html.append("")

    # Descripción
    descripcion = product.get("Descripcion") or product.get("Descripción") or product.get("descripcion")
    if descripcion:
        html.append(f'📝 <b>Descripción:</b>\n{descripcion}')

    # Línea vacía
    html.append("")

    # Categoria y Objetivo
    categoria = product.get("Categoria") or product.get("categoria")
    if categoria:
        html.append(f'📦 <b>Categoria:</b> {categoria}')
    objetivo = product.get("Objetivo") or product.get("objetivo")
    if objetivo:
        html.append(f'🎯 <b>Objetivo:</b> {objetivo}')

    # Unir todo, eliminando líneas vacías al principio y al final
    return "\n".join([line for line in html]).strip()

# --- Enviar mensaje ---
async def send_message(bot: Bot, chat_id: str, text: str, image_url: str = None):
    try:
        # Convertir URLs de Google Drive a formato directo
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

    # Obtener el rango seleccionado
    selected_range = query.data.split('_')[1]
    min_discount, max_discount = DISCOUNT_RANGES[selected_range]

    # Obtener datos del sheet
    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await query.edit_message_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await query.edit_message_text("No se pudo acceder a los datos. Intenta más tarde.")
        return

    # Filtrar productos por rango de descuento
    filtered_products = []
    for product in sheet_data:
        descuento = product.get("Descuento") or product.get("descuento")
        try:
            descuento = float(str(descuento).replace('%', ''))
            if min_discount <= descuento < max_discount or (selected_range == "50+" and descuento >= 50):
                filtered_products.append(product)
        except (ValueError, TypeError):
            continue

    # Enviar mensaje inicial
    if not filtered_products:
        await query.edit_message_text(
            f"No se encontraron productos con descuentos entre {min_discount}% y {max_discount}%"
        )
        return

    # Mensaje de inicio
    await query.edit_message_text(
        f"Encontrados {len(filtered_products)} productos con descuentos entre {min_discount}% y {max_discount}%.\n"
        "Enviando productos..."
    )
    
    # Enviar cada producto
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
        
        # Pequeña pausa entre mensajes
        if i < len(filtered_products) - 1:
            await asyncio.sleep(1)
    
    # Mensaje final
    if filtered_products:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Se han enviado todos los {len(filtered_products)} productos con descuentos entre {min_discount}% y {max_discount}%."
        )

# --- NUEVA FUNCIÓN: Comando de búsqueda ---
async def buscar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Por favor, especifica un término de búsqueda después de /buscar.\n"
            "Ejemplo: /buscar proteína"
        )
        return
    
    # Obtener término de búsqueda
    search_term = " ".join(context.args).lower()
    
    # Obtener datos del sheet
    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await update.message.reply_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await update.message.reply_text("No se pudo acceder a los datos. Intenta más tarde.")
        return
    
    # Filtrar productos que contienen el término de búsqueda
    found_products = []
    
    for product in sheet_data:
        product_text = ""
        
        # Concatenar todos los campos relevantes para la búsqueda
        for field in ["Nombre", "nombre", "Marca", "marca", "Descripcion", "Descripción", "descripcion", 
                      "Categoria", "categoria", "Objetivo", "objetivo"]:
            if product.get(field):
                product_text += str(product.get(field)).lower() + " "
        
        # Comprobar si el término aparece en algún campo
        if search_term in product_text:
            found_products.append(product)
    
    # Enviar resultados
    if not found_products:
        await update.message.reply_text(
            f"No se encontraron productos que coincidan con '{search_term}'."
        )
        return
    
    # Mensaje inicial
    await update.message.reply_text(
        f"Encontrados {len(found_products)} productos que coinciden con '{search_term}'.\n"
        "Enviando resultados..."
    )
    
    # Enviar cada producto encontrado
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
        
        # Pequeña pausa entre mensajes
        if i < len(found_products) - 1:
            await asyncio.sleep(1)
    
    # Mensaje final
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Se han enviado todos los {len(found_products)} productos que coinciden con '{search_term}'."
    )

# --- NUEVA FUNCIÓN: Comando para mostrar categorías ---
async def categoria_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Obtener datos del sheet
    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await update.message.reply_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await update.message.reply_text("No se pudo acceder a los datos. Intenta más tarde.")
        return
    
    # Extraer todas las categorías únicas
    categorias = []
    for product in sheet_data:
        cat = product.get("Categoria") or product.get("categoria")
        if cat and cat not in categorias:
            categorias.append(cat)
    
    # Ordenar alfabéticamente
    categorias.sort()
    
    # Crear botones para cada categoría
    keyboard = []
    row = []
    
    for i, categoria in enumerate(categorias):
        # 2 botones por fila
        if i > 0 and i % 2 == 0:
            keyboard.append(row)
            row = []
            
        row.append(InlineKeyboardButton(categoria, callback_data=f"cat_{categoria}"))
    
    # Añadir la última fila si tiene elementos
    if row:
        keyboard.append(row)
    
    if not keyboard:
        await update.message.reply_text("No se encontraron categorías.")
        return
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Selecciona una categoría para ver sus productos:",
        reply_markup=reply_markup
    )

# --- NUEVA FUNCIÓN: Manejador de selección de categoría ---
async def handle_categoria_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Obtener la categoría seleccionada
    selected_categoria = query.data.split('_', 1)[1]
    
    # Obtener datos del sheet
    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await query.edit_message_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await query.edit_message_text("No se pudo acceder a los datos. Intenta más tarde.")
        return
    
    # Filtrar productos por categoría
    filtered_products = []
    for product in sheet_data:
        categoria = product.get("Categoria") or product.get("categoria")
        if categoria == selected_categoria:
            filtered_products.append(product)
    
    # Enviar mensaje inicial
    if not filtered_products:
        await query.edit_message_text(
            f"No se encontraron productos en la categoría '{selected_categoria}'."
        )
        return
    
    # Mensaje de inicio
    await query.edit_message_text(
        f"Encontrados {len(filtered_products)} productos en la categoría '{selected_categoria}'.\n"
        "Enviando productos..."
    )
    
    # Enviar cada producto
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
        
        # Pequeña pausa entre mensajes
        if i < len(filtered_products) - 1:
            await asyncio.sleep(1)
    
    # Mensaje final
    if filtered_products:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Se han enviado todos los {len(filtered_products)} productos de la categoría '{selected_categoria}' Si quieres estar al día unete al canal @Supleshop_Ofertas."
        )

# --- NUEVA FUNCIÓN: Comando para mostrar objetivos ---
async def objetivo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Obtener datos del sheet
    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await update.message.reply_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await update.message.reply_text("No se pudo acceder a los datos. Intenta más tarde.")
        return
    
    # Extraer todos los objetivos únicos
    objetivos = []
    for product in sheet_data:
        obj = product.get("Objetivo") or product.get("objetivo")
        if obj and obj not in objetivos:
            objetivos.append(obj)
    
    # Ordenar alfabéticamente
    objetivos.sort()
    
    # Crear botones para cada objetivo
    keyboard = []
    row = []
    
    for i, objetivo in enumerate(objetivos):
        # 2 botones por fila
        if i > 0 and i % 2 == 0:
            keyboard.append(row)
            row = []
            
        row.append(InlineKeyboardButton(objetivo, callback_data=f"obj_{objetivo}"))
    
    # Añadir la última fila si tiene elementos
    if row:
        keyboard.append(row)
    
    if not keyboard:
        await update.message.reply_text("No se encontraron objetivos.")
        return
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Selecciona un objetivo para ver sus productos:",
        reply_markup=reply_markup
    )

# --- NUEVA FUNCIÓN: Manejador de selección de objetivo ---
async def handle_objetivo_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Obtener el objetivo seleccionado
    selected_objetivo = query.data.split('_', 1)[1]
    
    # Obtener datos del sheet
    csv_url = config.get("sheet_url") or config.get("SHEET_CSV_URL")
    if not csv_url:
        await query.edit_message_text("Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await query.edit_message_text("No se pudo acceder a los datos. Intenta más tarde.")
        return
    
    # Filtrar productos por objetivo
    filtered_products = []
    for product in sheet_data:
        objetivo = product.get("Objetivo") or product.get("objetivo")
        if objetivo == selected_objetivo:
            filtered_products.append(product)
    
    # Enviar mensaje inicial
    if not filtered_products:
        await query.edit_message_text(
            f"No se encontraron productos con el objetivo '{selected_objetivo}'."
        )
        return
    
    # Mensaje de inicio
    await query.edit_message_text(
        f"Encontrados {len(filtered_products)} productos con el objetivo '{selected_objetivo}'.\n"
        "Enviando productos..."
    )
    
    # Enviar cada producto
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
        
        # Pequeña pausa entre mensajes
        if i < len(filtered_products) - 1:
            await asyncio.sleep(1)
    
    # Mensaje final
    if filtered_products:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Se han enviado todos los {len(filtered_products)} productos con el objetivo '{selected_objetivo}'."
        )

# --- Lógica principal de monitoreo ---
async def process_sheet_data(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    
    # Obtener URL del Sheet
    csv_url = config.get("SHEET_CSV_URL") or config.get("sheet_url")
    if not csv_url:
        await send_admin_error(bot, "Error: URL de la hoja de cálculo no configurada.")
        return
        
    sheet_data = fetch_sheet_data(csv_url)
    if sheet_data is None:
        await send_admin_error(bot, "No se pudo acceder al Google Sheet. Reintentando en 1 minuto.")
        return

    state = load_state()
    ids = set(state["IDs"])
    last_prices = state.get("last_prices", {})

    logo_url = config.get("LOGO_URL") or config.get("logo_url")
    channel_id = config.get("TELEGRAM_CHANNEL_ID")
    
    # Si no hay canal configurado, solo actualizar el estado
    if not channel_id:
        # Solo actualizar el estado
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
                
        # Guardar estado
        save_state({"IDs": list(ids), "last_prices": last_prices})
        return
        
    # Si hay canal configurado, continuar con la lógica original
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

    # Enviar nuevos productos
    for product in nuevos:
        text = format_product_message(product, change_type="new", logo_url=logo_url)
        image_url = product.get("imagen") or product.get("Imagen")
        await send_message(bot, channel_id, text, image_url)
        await asyncio.sleep(1)

    # Enviar descuentos
    for product in descuentos:
        text = format_product_message(product, change_type="discount", logo_url=logo_url)
        image_url = product.get("imagen") or product.get("Imagen")
        await send_message(bot, channel_id, text, image_url)
        await asyncio.sleep(1)

    # Guardar estado
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
    help_text = """
🆘 <b>Ayuda - Comandos disponibles:</b>

🔹 <b>/start</b> - Muestra el mensaje de bienvenida
🔹 <b>/help</b> - Muestra esta ayuda
🔹 <b>/ofertas</b> - Muestra productos por rango de descuento
🔹 <b>/buscar [término]</b> - Busca productos por palabra clave
🔹 <b>/categoria</b> - Muestra productos por categoría
🔹 <b>/objetivo</b> - Muestra productos por objetivo

📌 <b>Ejemplos:</b>
<code>/buscar proteína</code> - Busca productos con "proteína"
<code>/ofertas</code> - Muestra productos con descuento

👉 Únete a nuestro canal: @Supleshop_Ofertas
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

# --- Main ---
def main():
    application = Application.builder().token(config["token"]).build()
    job_queue: JobQueue = application.job_queue

    # Para mantener el bot activo
    job_queue.run_repeating(
        process_sheet_data, 
        interval=1500,  # 25 minutos en segundos
        first=10
    )

    # Comandos
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("force_update", force_update))
    application.add_handler(CommandHandler("ofertas", ofertas_command))
    application.add_handler(CommandHandler("buscar", buscar_command))
    application.add_handler(CommandHandler("categoria", categoria_command))
    application.add_handler(CommandHandler("objetivo", objetivo_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # Manejadores de callback
    application.add_handler(CallbackQueryHandler(handle_discount_selection, pattern="^discount_"))
    application.add_handler(CallbackQueryHandler(handle_categoria_selection, pattern="^cat_"))
    application.add_handler(CallbackQueryHandler(handle_objetivo_selection, pattern="^obj_"))


    logger.info("Bot iniciado. Esperando eventos...")
    application.run_polling()

if __name__ == "__main__":
    main()