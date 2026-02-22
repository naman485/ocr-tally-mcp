FROM python:3.12-slim

# Tesseract OCR + OpenCV system deps (only needed if OCR_PROVIDER=tesseract)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create directories
RUN mkdir -p /app/cheques /app/temp_images

ENV PYTHONUNBUFFERED=1
ENV PORT=8000

EXPOSE 8000

# Graceful shutdown support
STOPSIGNAL SIGTERM

CMD ["python", "main.py"]
