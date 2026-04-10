FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY daily_shop.py .

CMD ["python", "daily_shop.py"]
