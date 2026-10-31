# The public game, for Hugging Face Spaces (Docker) or any container host.
# Set MODEL_API_KEY, FIREWORKS_API_KEY and BASETEN_API_KEY as secrets. The spend records live in
# the container, so a restart resets the game's own count: keep a spend limit on each provider too.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[keep-scores]" && useradd --create-home probie && chown -R probie /app
USER probie
ENV PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["sh", "-c", "test -f data/cases.jsonl || python -m probie generate; exec python -m probie game --host 0.0.0.0 --port ${PORT:-7860}"]
