FROM python:3.12-slim

WORKDIR /app

# Installa dipendenze
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia tutto il codice
COPY . .

# Avvia il bot
CMD ["python", "bot.py"]
