FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Hugging Face Spaces route to this port.
EXPOSE 7860

# Persistent Space disk. Do not put GAME_API_KEY or BBA_CHAT_SECRET here.
# Set both as Space secrets (Settings → Variables and secrets).
ENV BBA_CHAT_DATA=/data

CMD ["python", "app.py"]
