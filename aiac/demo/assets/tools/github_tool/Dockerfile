FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV PORT=9090
ENV LOG_LEVEL=INFO

EXPOSE 9090

# Drop privileges.
RUN useradd --no-create-home --uid 10001 aiac
USER 10001

CMD ["python", "server.py"]
