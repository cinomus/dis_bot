FROM python:3.12-slim

WORKDIR /app

# ffmpeg воспроизводит поток, libopus кодирует голос для Discord.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

CMD ["python", "bot.py"]
