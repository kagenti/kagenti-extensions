# Build context: aiac/src/
FROM python:3.12-slim

WORKDIR /app

COPY aiac/agent/controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/src
ENV PYTHONPATH=/app/src

EXPOSE 7070

CMD ["uvicorn", "aiac.agent.controller.routes:app", "--host", "0.0.0.0", "--port", "7070"]
