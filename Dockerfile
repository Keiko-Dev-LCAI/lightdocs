FROM python:3.11-slim-bookworm
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
ENV MPLCONFIGDIR=/tmp/matplotlib
# Cache-bust marker — bump when a deploy must rebuild app layers (not reuse stale image).
ENV LIGHTDOCS_BUILD=0.8.2
CMD gunicorn -b 0.0.0.0:$PORT -w 1 --timeout 300 server:app
