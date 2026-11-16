# Mock Petstore (Swagger 2.0 sample) for local backend testing. Standard library only.
FROM python:3.12-slim
RUN useradd --system --uid 1001 --no-create-home --shell /usr/sbin/nologin app
COPY run.py /srv/run.py
USER 1001
WORKDIR /srv
EXPOSE 18081
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/srv/run.py"]
