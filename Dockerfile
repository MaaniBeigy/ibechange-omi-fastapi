FROM python:3.13-slim AS runtime

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 3200

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3200"]

FROM runtime AS development
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
