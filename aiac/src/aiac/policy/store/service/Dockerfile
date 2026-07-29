FROM python:3.12-slim

WORKDIR /app

COPY aiac/policy/store/service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/src
ENV PYTHONPATH=/app/src

# Directory for the SQLite backend. Must match the dir of SERVICEPOLICY_DB_PATH's
# default (/data/policy_model.db in aiac.policy.store.service.main); in production this
# is typically a mounted PV, but create it so a default-path start does not fail.
RUN mkdir -p /data

CMD ["uvicorn", "aiac.policy.store.service.main:app", "--host", "0.0.0.0", "--port", "7074"]
