FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY eth_accumulator_bot.py .

# Health check port
EXPOSE 8000

# Server mode: health check + auto-run every 4h
ENV BOT_MODE=server
ENV CHECK_INTERVAL=14400
ENV PORT=8000

CMD ["python", "-u", "eth_accumulator_bot.py"]
