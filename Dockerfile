FROM python:3.12-slim

WORKDIR /app

# Installa dipendenze
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia tutto il codice
COPY . .

# Railway inietta PORT; run_all.py avvia web_api (HTTP) e bot (Telegram)
# nello stesso processo, condividendo un unico volume su /app/data
# (Railway non supporta volumi condivisi fra servizi).
EXPOSE 8000
CMD ["python", "run_all.py"]