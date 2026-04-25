# Use official Python slim image — includes pip out of the box
FROM python:3.11-slim

# Install ffmpeg and clean up apt cache in one layer
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

CMD ["python", "bot.py"]
