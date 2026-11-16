# Mock SOAP backend for the P1 spike.
# Base: spec2openapi runtime image (already has lxml). Build with
#   docker build --build-context s2o=<spec2openapi checkout> ...
# so tests/mock_soap_server.py can be copied without vendoring it here.
ARG S2O_VERSION=0.5.0
FROM spec2openapi:${S2O_VERSION}

USER root
COPY --from=s2o tests/mock_soap_server.py /srv/mock_soap_server.py
COPY run.py /srv/run.py
USER app
WORKDIR /srv
EXPOSE 18080
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/srv/run.py"]
CMD []
