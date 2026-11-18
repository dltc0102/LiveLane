FROM ghcr.io/osgeo/gdal:ubuntu-small-latest
WORKDIR /app
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages --ignore-installed -r requirements.txt
COPY . .
EXPOSE 8501
EXPOSE 8000