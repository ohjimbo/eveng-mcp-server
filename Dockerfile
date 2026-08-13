FROM python:3.12-slim

WORKDIR /app

# Code is baked into the image — the server is stateless (inventory is
# derived, config comes from env). No bind mounts, no volumes needed.
COPY pyproject.toml README.md LICENSE ./
COPY eveng_mcp/ ./eveng_mcp/
RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["python", "-m", "eveng_mcp"]
