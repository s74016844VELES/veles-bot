import os
import re
import logging
import json
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN is not set!")
    raise SystemExit("TELEGRAM_TOKEN is not set!")

if not ANTHROPIC_API_KEY:
    logger.error("ANTHROPIC_API_KEY is not set!")
    raise SystemExit("ANTHROPIC_API_KEY is not set!")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

TEMPLATES_DIR = "templates"
os.makedirs(TEMPLATES_DIR, exist_ok=True)

SYSTEM_PROMPT = """Ты ассистент для заполнения доп. соглашений к договорам поставки нефтепродуктов ООО ТК «Велес».

ПРАВИЛА:
- Номер ДС = дата в формате число/месяц (08.05.2026 → 08/05)
- Стоимость = количество × цена
- Срок поставки = месяц год (08.05.2026 → Май 2026)

Когда получаешь данные для ДС, извлеки и ответь ТОЛЬКО JSON без лишнего текста:
{
  "контрагент": "...",
  "дата": "дд.мм.гггг",
  "номер": "дд/мм",
  "количество": "X,XXX",
  "цена": "XX XXX",
  "сумма": "X XXX XXX,XX",
  "месяц": "Май 2026"
}

Если данных не хватает — напиши обычным текстом что именно нужно.
Если это не запрос на ДС — отвечай обычным текстом по-русски."""


def get_template_path(contractor: str):
    contractor_up = contractor.upper().strip()
    for fname in os.listdir(TEMPLATES_DIR):
        name = fname.upper().replace("_ШАБЛОН", "").replace("_TEMPLATE", "").replace(".DOCX", "").strip()
        if contractor_up in name or name in contractor_up:
            return os.path.join(TEMPLATES_DIR, fname)
    return None


def fill_document(template_path: str, data: dict, output_path: str) -> bool:
    try:
        import zipfile, shutil, tempfile

        # Unpack docx (it's a zip)
        unpack_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(template_path, 'r') as z:
            z.extractall(unpack_dir)

        xml_path = os.path.join(unpack_dir, "word", "document.xml")
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Replace date number (e.g. 17/04 -> 08/05)
        content = re.sub(r'\d{2}/\d{2}', data['номер'], content, count=1)

        # Replace full date (e.g. 17.04.2026 г)
        content = re.sub(r'\d{2}\.\d{2}\.\d{4} г', data['дата'] + ' г', content, count=1)

        # Replace month (e.g. Апрель 2026)
        months = 'Январь|Февраль|Март|Апрель|Май|Июнь|Июль|Август|Сентябрь|Октябрь|Ноябрь|Декабрь'
        content = re.sub(rf'({months})\s+20\d\d', data['месяц'], content, count=1)

        # Replace quantity (e.g. 23,439 (+/-5%))
        content = re.sub(r'\d+[,\.]\d+\s*\(\+/-5%\)', data['количество'] + '(+/-5%)', content, count=1)

        # Replace sum (large number with decimals like 1 652 449,50)
        content = re.sub(r'[\d\s\xa0]{5,},\d{2}', data['сумма'], content, count=1)

        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(content)

        # Repack docx
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for root, dirs, files in os.walk(unpack_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, unpack_dir)
                    zout.write(file_path, arcname)

        shutil.rmtree(unpack_dir, ignore_errors=True)
        return True
    except Exception as e:
        logger.error(f"Fill error: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я заполняю доп. соглашения ООО ТК «Велес».\n\n"
        "Напишите данные, например:\n"
        "«ЛИВА, 23,203 т, 78 000, 08.05.2026»\n\n"
        "Или загрузите шаблон .docx для нового контрагента.\n\n"
        "/templates — список шаблонов"
    )


async def list_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = [f for f in os.listdir(TEMPLATES_DIR) if f.endswith(".docx")]
    if files:
        names = "\n".join(
            f"• {f.replace('_шаблон.docx','').replace('_template.docx','').replace('_шаблон.docx','').replace('.docx','')}"
            for f in files
        )
        await update.message.reply_text(f"📁 Шаблоны:\n{names}")
    else:
        await update.message.reply_text("Шаблонов нет. Загрузите .docx файл.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".docx"):
        await update.message.reply_text("Пожалуйста, загрузите файл .docx")
        return
    file = await context.bot.get_file(doc.file_id)
    save_path = os.path.join(TEMPLATES_DIR, doc.file_name)
    await file.download_to_drive(save_path)
    await update.message.reply_text(f"✅ Шаблон «{doc.file_name}» сохранён!")


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

        json_match = re.search(r'\{.*\}', reply, re.DOTALL)
        if not json_match:
            await update.message.reply_text(reply)
            return

        data = json.loads(json_match.group(0))
        contractor = data.get("контрагент", "")
        template_path = get_template_path(contractor)

        if not template_path:
            await update.message.reply_text(
                f"⚠️ Шаблон для «{contractor}» не найден.\n"
                "Загрузите .docx файл для этого контрагента."
            )
            return

        date_str = data['дата'].replace('.', '_')
        output_name = f"ДС_{date_str}_Велес-{contractor}.docx"
        output_path = f"/tmp/{output_name}"

        ok = fill_document(template_path, data, output_path)

        if ok and os.path.exists(output_path):
            await update.message.reply_text(
                f"✅ Готово!\n"
                f"📄 {contractor}\n"
                f"📅 {data['дата']} (№ {data['номер']})\n"
                f"⚖️ {data['количество']} тн. × {data['цена']} = {data['сумма']} руб."
            )
            with open(output_path, "rb") as f:
                await update.message.reply_document(document=f, filename=output_name)
            os.remove(output_path)
        else:
            await update.message.reply_text("❌ Ошибка при заполнении. Попробуйте ещё раз.")

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
