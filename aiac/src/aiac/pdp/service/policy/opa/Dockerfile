FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9

WORKDIR /app

# Create the non-root user up front so copied files can be owned by it.
RUN useradd --no-create-home --uid 10001 aiac

COPY --chown=10001:10001 aiac/pdp/service/policy/opa/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 . /app/src
ENV PYTHONPATH=/app/src

# Drop privileges.
USER 10001

EXPOSE 7072

CMD ["uvicorn", "aiac.pdp.service.policy.opa.main:app", "--host", "0.0.0.0", "--port", "7072"]
