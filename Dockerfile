FROM python:3.11-slim

# Install system dependencies including ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Verify ffmpeg installation
RUN ffmpeg -version

# Set working directory
WORKDIR /app

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY main.py .

# Create downloads directory with proper permissions
RUN mkdir -p /app/downloads && chmod 777 /app/downloads

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Expose port for health checks
EXPOSE 8000

# Run the bot
CMD ["python", "-u", "main.py"]
