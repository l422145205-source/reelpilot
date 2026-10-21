FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN reelpilot demo --collection data/demo
EXPOSE 8765
CMD ["reelpilot", "serve", "--collection", "data/demo", "--host", "0.0.0.0", "--port", "8765"]
