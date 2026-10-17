ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apk add --no-cache bind-tools freeradius-utils
WORKDIR /app
COPY pyproject.toml /app/
COPY netadmin /app/netadmin
RUN pip install --no-cache-dir --only-binary :all: .
COPY home-assistant/netadmin/run.sh /run.sh
RUN chmod 755 /run.sh
CMD ["/run.sh"]
