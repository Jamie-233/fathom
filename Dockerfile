FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
 && useradd --create-home fathom
USER fathom
VOLUME ["/data"]
EXPOSE 8000

# Bind to all interfaces inside the container; publish the port explicitly.
CMD ["fathom", "serve", "--host", "0.0.0.0", "--port", "8000", "--db", "/data/fathom.db"]
