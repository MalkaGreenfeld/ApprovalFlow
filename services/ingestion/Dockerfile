FROM python:3.12-slim

WORKDIR /app

# --- Optional corporate TLS root (NetFree and similar) -----------------------
# The glob makes the certificate optional: LICENSE always exists, so COPY stays
# valid when no netfree-ca.crt is supplied and the build works on any machine.
COPY LICENSE netfree-ca.crt* /tmp/ca/
RUN if [ -f /tmp/ca/netfree-ca.crt ]; then \
      cp /tmp/ca/netfree-ca.crt /usr/local/share/ca-certificates/ && \
      cat /tmp/ca/netfree-ca.crt >> /etc/ssl/certs/ca-certificates.crt && \
      echo "Installed netfree-ca.crt into the trust store"; \
    else \
      echo "No netfree-ca.crt found - using the default trust store"; \
    fi && rm -rf /tmp/ca
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt
# ---------------------------------------------------------------------------

RUN pip install --no-cache-dir setuptools wheel

COPY services/shared /tmp/shared
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared

COPY services/ingestion/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bootstrap policy configuration. The live copy lives in the Dapr state store and
# is edited through the admin API; this is only the first-start seed.
COPY config/ ./config/
COPY services/ingestion/app/ ./app/

ENV POLICY_CONFIG_PATH=/app/config/policy-config.json

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
