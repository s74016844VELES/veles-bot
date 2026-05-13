import os
import re
import shutil
import logging
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

TEMPLATES_DIR = "templates"
os.makedirs(TEMPLATES_DIR, exist_ok=True)

SYSTEM_PROMPT = """Ты ассистент для заполнения доп. соглашений к договорам поставки нефтепродуктов ООО ТК «Велес».

ПРАВИЛА:
- Номер ДС = дата в формате число/месяц (например, 08.05.2026 → номер 08/05)
- Стоимость партии товара = количество × цена (рассчитывай автоматически, формат: 1 234 567,00)
- Срок поставки = месяц и год из даты (например, 08.05.2026 → Май 2026)

Когда пользователь даёт данные для ДС, извлеки из сообщения:
- контрагент
- дата (дд.мм.гггг)
- количество (тонны)
- цена (руб/тн)

И ответь СТРОГО в формате JSON (без лишнего текста):
{
  "контрагент": "...",
  "дата": "дд.мм.гггг",
  "номер": "дд/мм",
  "количество": "X,XXX",
  "цена": "XX XXX",
  "сумма": "X XXX XXX,XX",
  "месяц": "Май 2026"
}

Если данных недостаточно — напиши что именно не хватает обычным текстом (не JSON).
Если это не запрос на ДС — отвечай обычным текстом по-русски."""

def format_sum(qty: float, price: float) -> str:
    total = qty * price
    # Format as Russian number: 1 234 567,00
    int_part = int(total)
    dec_part = round((total - int_part) * 100)
    int_str = f"{int_part:,}".replace(",", "\u00a0")
    return f"{int_str},{dec_part:02d}"

def get_template_path(contractor: str) -> str | None:
    contractor_upper = contractor.upper().strip()
    for fname in os.listdir(TEMPLATES_DIR):
        name = fname.upper().replace("_ШАБЛОН", "").replace("_TEMPLATE", "").replace(".DOCX", "").strip()
        if contractor_upper in name or name in contractor_upper:
            return os.path.join(TEMPLATES_DIR, fname)
    return None

def fill_document(template_path: str, data: dict, output_path: str) -> bool:
    try:
        import subprocess, tempfile

        unpack_dir = tempfile.mkdtemp()
        result = subprocess.run(
            ["python", "/app/scripts/unpack.py", template_path, unpack_dir],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            logger.error(f"Unpack error: {result.stderr}")
            return False

        xml_path = os.path.join(unpack_dir, "word", "document.xml")
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Extract old values from template via regex
        old_date_match = re.search(r'(\d{2}\.\d{2}\.\d{4}) г', content)
        old_num_match = re.search(r'№\s*(\d{2}/\d{2})', content)
        old_month_match = re.search(r'(Январь|Февраль|Март|Апрель|Май|Июнь|Июль|Август|Сентябрь|Октябрь|Ноябрь|Декабрь)\s+20\d{2}', content)

        if old_date_match:
            content = content.replace(old_date_match.group(0), f"{data['дата']} г")
        if old_num_match:
            content = content.replace(old_num_match.group(1), data['номер'])
        if old_month_match:
            content = content.replace(old_month_match.group(0), data['месяц'])

        # Replace quantity (look for pattern X,XXX (+/-5%))
        qty_match = re.search(r'(\d+[,\.]\d+)\s*\(\+/-5%\)', content)
        if qty_match:
            content = content.replace(qty_match.group(0), f"{data['количество']}(+/-5%)")

        # Replace price
        price_clean = data['цена'].replace(' ', '[\\s\\xa0]?')
        price_match = re.search(r'(\s{0,10}' + data['цена'].replace(' ', r'[\s\xa0]') + r')', content)
        if not price_match:
            # try without spaces
            old_price_match = re.search(r'<w:t[^>]*>\s*(\d[\d\s\xa0]{3,})\s*</w:t>', content)
            if old_price_match:
                content = content.replace(old_price_match.group(1), data['цена'])

        # Replace sum - find any number that looks like a sum (large number with decimals)
        sum_match = re.search(r'(\d[\d\s\xa0]*,\d{2})', content)
        if sum_match:
            content = content.replace(sum_match.group(0), data['сумма'])

        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(content)

        result = subprocess.run(
            ["python", "/app/scripts/pack.py", unpack_dir, output_path, "--original", template_path],
            capture_output=True, text=True
        )
        shutil.rmtree(unpack_dir, ignore_errors=True)
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Fill error: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я помогаю заполнять доп. соглашения ООО ТК «Велес».\n\n"
        "Просто напишите данные, например:\n"
        "«ЛИВА, 23,203 т, 78 000, 08.05.2026»\n\n"
        "Или загрузите шаблон .docx для нового контрагента.\n\n"
        "/templates — список загруженных шаблонов"
    )

async def list_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = [f for f in os.listdir(TEMPLATES_DIR) if f.endswith(".docx")]
    if files:
        names = "\n".join(f"• {f.replace('_шаблон.docx','').replace('_template.docx','').replace('.docx','')}" for f in files)
        await update.message.reply_text(f"📁 Загруженные шаблоны:\n{names}")
    else:
        await update.message.reply_text("Шаблонов пока нет. Загрузите .docx файл.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".docx"):
        await update.message.reply_text("Пожалуйста, загрузите файл .docx")
        return

    await update.message.reply_text(f"📥 Получил файл: {doc.file_name}")
    file = await context.bot.get_file(doc.file_id)
    save_path = os.path.join(TEMPLATES_DIR, doc.file_name)
    await file.download_to_drive(save_path)
    await update.message.reply_text(f"✅ Шаблон сохранён! Теперь можно использовать этого контрагента.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    await update.message.reply_text("⏳ Обрабатываю...")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_text}]
        )
        reply = response.content[0].text.strip()

        # Try to parse as JSON
        json_match = re.search(r'\{.*\}', reply, re.DOTALL)
        if not json_match:
            await update.message.reply_text(reply)
            return

        import json
        data = json.loads(json_match.group(0))

        contractor = data.get("контрагент", "")
        template_path = get_template_path(contractor)

        if not template_path:
            await update.message.reply_text(
                f"⚠️ Шаблон для «{contractor}» не найден.\n"
                f"Загрузите файл .docx для этого контрагента."
            )
            return

        # Build output filename
        date_str = data['дата'].replace('.', '_')
        output_name = f"ДС_{date_str}_Велес-{contractor}.docx"
        output_path = f"/tmp/{output_name}"

        ok = fill_document(template_path, data, output_path)

        if ok and os.path.exists(output_path):
            qty = data['количество']
            price = data['цена']
            summa = data['сумма']
            await update.message.reply_text(
                f"✅ Готово!\n"
                f"📄 {contractor}\n"
                f"📅 {data['дата']} (№ {data['номер']})\n"
                f"⚖️ {qty} тн. × {price} = {summa} руб."
            )
            with open(output_path, "rb") as f:
                await update.message.reply_document(document=f, filename=output_name)
            os.remove(output_path)
        else:
            await update.message.reply_text("❌ Ошибка при заполнении документа. Попробуйте ещё раз.")

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("templates", list_templates))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
