FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# yt-dlp sempre na versão mais recente a cada deploy (o Instagram muda com frequência)
RUN pip install --no-cache-dir -U "yt-dlp[default]"
WORKDIR /app
COPY app.py .
ENV PYTHONUNBUFFERED=1 DATA_DIR=/tmp/baixador
CMD ["python", "app.py"]
