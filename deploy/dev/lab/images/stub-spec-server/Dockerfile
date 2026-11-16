# Stub spec server for the P1 spike: one OpenAPI spec behind a Bearer token.
FROM python:3.12-slim

RUN useradd --system --uid 1000 --no-create-home spec
COPY server.py /srv/server.py
USER 1000
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/srv/server.py"]
