FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py .
COPY scripts/ scripts/
COPY templates/ templates/

CMD ["python", "bot.py"]
