FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

ENV SEND_DATA_DIR=/data
VOLUME ["/data"]

CMD ["python", "app.py"]
