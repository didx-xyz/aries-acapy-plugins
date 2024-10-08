FROM ghcr.io/hyperledger/aries-cloudagent-python:py3.12-1.0.0

RUN pip3 install --no-cache-dir poetry

USER root

# Add docker-compose-wait tool
ENV WAIT_VERSION 2.7.2
ADD https://github.com/ufoscout/docker-compose-wait/releases/download/$WAIT_VERSION/wait /wait
RUN chmod +x /wait

RUN mkdir kafka_queue && touch kafka_queue/__init__.py

COPY ./README.md ./setup.py ./pyproject.toml ./poetry.lock ./
USER root
RUN chown aries:aries -R .
USER aries

ARG install_flags='--with integration --extras aca-py'
RUN poetry install ${install_flags}

# Make site packages location more accessible (for use with volumes)
RUN ln -s $(poetry env info -p)/lib/python3.12/site-packages site-packages

COPY kafka_events/v1_0/kafka_queue kafka_queue
COPY ./docker/default.yml .
COPY ./docker/plugins-config.yaml .

ENTRYPOINT ["/bin/sh", "-c", "/wait && poetry run aca-py \"$@\"", "--"]
CMD ["start", "--arg-file", "default.yml"]
