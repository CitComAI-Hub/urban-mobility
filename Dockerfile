FROM ngomariz/sumo-heroku:latest

# Install dependencies
WORKDIR /app
COPY pyproject.toml /app/
RUN apt install python3-venv -y && \
    python3 -m venv .venv
RUN /app/.venv/bin/pip install --upgrade pip setuptools wheel poetry
ENV PATH="/app/.venv/bin:$PATH"
RUN poetry install --only main --no-root

# Copy app source code
COPY ./src ./src

EXPOSE 8080
CMD ["python", "src/kserve_async_server.py", "--model_name", "urban-flow"]
