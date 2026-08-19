FROM node:22-alpine

WORKDIR /app

# --- Optional corporate TLS root (NetFree and similar) -----------------------
# The glob makes the certificate optional: LICENSE always exists, so COPY stays
# valid when no netfree-ca.crt is supplied and the build works on any machine.
COPY LICENSE netfree-ca.crt* /tmp/ca/
RUN if [ -f /tmp/ca/netfree-ca.crt ]; then \
      cat /tmp/ca/netfree-ca.crt >> /etc/ssl/certs/ca-certificates.crt 2>/dev/null || \
      cat /tmp/ca/netfree-ca.crt >> /etc/ssl/cert.pem; \
      echo "Installed netfree-ca.crt into the trust store"; \
    else \
      echo "No netfree-ca.crt found - using the default trust store"; \
    fi && rm -rf /tmp/ca
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
# ---------------------------------------------------------------------------

COPY ui/package.json ui/tsconfig.json ui/vite.config.ts ui/index.html ./
RUN npm install

COPY ui/src/ ./src/

EXPOSE 3000
CMD ["npm", "run", "dev"]
