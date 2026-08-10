FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV STARK_HOST=0.0.0.0 STARK_PORT=8000 STARK_STATE_DIR=/data
VOLUME /data
EXPOSE 8000
RUN python -c "import app.main"
CMD ["sh", "-c", "python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
