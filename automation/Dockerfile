FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git openssh-client ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY publisher.py homily_prompt.txt narration.json github_known_hosts ./
RUN useradd --uid 65532 --no-create-home --home-dir /tmp homily
USER 65532:65532
ENV PYTHONUNBUFFERED=1
CMD ["python", "publisher.py", "--publish"]
