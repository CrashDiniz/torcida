FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway provides $PORT; run bot + web app in one service for the hackathon
CMD ["sh", "-c", "python -m src.bot.main & uvicorn src.web.app:app --host 0.0.0.0 --port ${PORT:-8090}"]
