FROM python:3.12-slim

WORKDIR /app

# Build context is aiac/src/
COPY aiac/agent/controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aiac/ aiac/

EXPOSE 7070

CMD ["python", "-m", "aiac.agent.controller.routes"]
