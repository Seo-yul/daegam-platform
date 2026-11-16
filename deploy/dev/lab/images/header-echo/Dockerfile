# Header-checking echo MCP server for the e2e-gateway header steps (P86). Standard library only.
FROM python:3.12-slim
RUN useradd --system --uid 1001 --no-create-home --shell /usr/sbin/nologin app
COPY run.py /srv/run.py
USER 1001
WORKDIR /srv
EXPOSE 18082
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/srv/run.py"]
