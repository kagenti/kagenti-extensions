FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir mcp[cli] uvicorn[standard]

COPY server.py .

ENV PORT=9090
ENV LOG_LEVEL=INFO

EXPOSE 9090

CMD ["python", "server.py"]
