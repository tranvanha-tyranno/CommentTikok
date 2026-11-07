FROM python:3.11-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && python -m playwright install --with-deps chromium
COPY app ./app
ENV SIGNALDESK_DATA_DIR=/app/data
EXPOSE 8765
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
