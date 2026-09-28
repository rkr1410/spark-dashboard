FROM python:3.12-slim-bookworm

WORKDIR /app
COPY . .

EXPOSE 8088

CMD ["python3", "server/dev_server.py", "--host", "0.0.0.0", "--port", "8088"]
