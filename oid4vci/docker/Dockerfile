FROM python:3.12-slim-bullseye AS base
WORKDIR /usr/src/app

# Install and configure poetry
USER root

ENV POETRY_VERSION=1.8.3
ENV POETRY_HOME=/opt/poetry
RUN apt-get update && apt-get install -y curl jq && apt-get clean
RUN curl -sSL https://install.python-poetry.org | python -

ENV PATH="/opt/poetry/bin:$PATH"
RUN poetry config virtualenvs.in-project true

# Setup project
RUN mkdir oid4vci && touch oid4vci/__init__.py
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --without dev --extras "aca-py"
USER $user

FROM python:3.12-bullseye

WORKDIR /usr/src/app
COPY --from=base /usr/src/app/.venv /usr/src/app/.venv
ENV PATH="/usr/src/app/.venv/bin:$PATH"
RUN apt-get update && apt-get install -y curl jq && apt-get clean
COPY oid4vci/ oid4vci/
COPY docker/*.yml ./

ENTRYPOINT ["/bin/bash", "-c", "aca-py \"$@\"", "--"]
CMD ["start", "--arg-file", "default.yml"]
