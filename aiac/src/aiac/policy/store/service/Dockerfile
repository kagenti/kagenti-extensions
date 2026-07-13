FROM python:3.12-slim

WORKDIR /app

COPY aiac/policy/store/service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/src
ENV PYTHONPATH=/app/src

CMD ["uvicorn", "aiac.policy.store.service.main:app", "--host", "0.0.0.0", "--port", "7074"]
